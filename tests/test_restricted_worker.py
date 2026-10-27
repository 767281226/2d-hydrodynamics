from __future__ import annotations

import ctypes
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from ctypes import wintypes
from typing import Any, Mapping

import numpy as np
import pytest
import rasterio
from rasterio.env import Env
from rasterio.transform import from_origin

from hydrodynamics.prepared_terrain import PreparedTerrainError, PreparedTerrainStopped, read_prepared_terrain
import hydrodynamics.restricted_worker as restricted_worker
from hydrodynamics.restricted_worker import (
    CHILD_POLICY,
    ResourceBudgetError,
    RestrictedWorkerError,
    parse_worker_resources,
    recover_incomplete_attempts,
    run_restricted_preparation,
)


ROOT = Path(__file__).resolve().parents[1]
MEMORY_LIMIT_FLAG_CODES = {9, 10}
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
TH32CS_SNAPPROCESS = 0x00000002


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _temporary_case() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    temp = tempfile.TemporaryDirectory(prefix="m2b0-test-", dir=work)
    return temp, Path(temp.name)


def _make_case(case: Path, *, run_id: str = "b0-test-run", face_count: int = 2,
               valid_raster: bool = True, resources: dict[str, Any] | None = None,
               output_budget_bytes: int = 1_073_741_824,
               vertical_unit_metadata: bool = True) -> Path:
    case.mkdir(parents=True, exist_ok=False)
    raster_path = case / "source_256.tif"
    mesh_path = case / "mesh.json"
    source_manifest_path = case / "source_manifest.json"
    config_path = case / "worker_config.json"
    xmin, ymax = 473506.78215896204, 4335988.239580579
    if valid_raster:
        with Env(GDAL_PAM_ENABLED="NO"):
            with rasterio.open(
                raster_path, "w", driver="GTiff", width=256, height=256, count=1,
                dtype="float32", nodata=-32767.0,
                transform=from_origin(xmin, ymax, 1.0, 1.0), crs="EPSG:4548",
            ) as dataset:
                dataset.write(np.full((256, 256), 2.0, dtype=np.float32), 1)
                if vertical_unit_metadata:
                    dataset.set_band_unit(1, "metre")
    else:
        raster_path.write_bytes(b"not a GeoTIFF source")

    square_count = max(1, (face_count + 1) // 2)
    nx = min(50, square_count)
    ny = (square_count + nx - 1) // nx
    base_y = ymax - 200.0
    node_xy = [[xmin + x, base_y + y] for y in range(ny + 1) for x in range(nx + 1)]
    face_nodes: list[list[int]] = []
    for y in range(ny):
        current = y * (nx + 1)
        above = (y + 1) * (nx + 1)
        for x in range(nx):
            bl, br, tl, tr = current + x, current + x + 1, above + x, above + x + 1
            for face in ([bl, br, tr], [bl, tr, tl]):
                if len(face_nodes) < face_count:
                    face_nodes.append(face)
    mesh = {
        "format": "triangle-mesh-xy-json-v1", "mesh_id": f"b0-test-{face_count}",
        "coordinate_system": "EPSG:4548", "node_xy": node_xy,
        "face_nodes": face_nodes, "face_id": list(range(1000, 1000 + face_count)),
        "geometry_tolerance_m": 1e-8, "minimum_face_area_m2": 1e-12,
    }
    mesh_path.write_text(json.dumps(mesh, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    raster_sha = _sha256(raster_path)
    mesh_sha = _sha256(mesh_path)
    source_manifest = {
        "format": "b0-test-source-manifest-v1", "synthetic_only": True,
        "raster_path": raster_path.relative_to(ROOT).as_posix(), "raster_sha256": raster_sha,
        "mesh_path": mesh_path.relative_to(ROOT).as_posix(), "mesh_sha256": mesh_sha,
    }
    source_manifest_path.write_text(json.dumps(source_manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    manifest_sha = _sha256(source_manifest_path)
    alias = None
    if valid_raster:
        with Env(GDAL_PAM_ENABLED="NO", GDAL_GEOREF_SOURCES="INTERNAL"):
            with rasterio.open(raster_path, sharing=False) as dataset:
                source_wkt = dataset.crs.to_wkt(version="WKT1_GDAL")
        alias = {
            "mode": "verify_embedded_epsg4548_xy", "target_crs": "EPSG:4548",
            "expected_source_wkt_sha256": hashlib.sha256(source_wkt.encode("utf-8")).hexdigest(),
        }
    config = {
        "format": "triangle-terrain-preparation-worker-config-v1", "schema_version": "1.0",
        "run_id": run_id, "output_root": (case.relative_to(ROOT) / "runs").as_posix(),
        "resources": resources or {
            "max_workers": 1, "job_memory_limit_mib": 2048, "wall_clock_limit_s": 900.0,
            "output_budget_bytes": output_budget_bytes, "scratch_budget_bytes": 67_108_864,
            "child_process_policy": CHILD_POLICY,
        },
        "source": {
            "raster_path": raster_path.relative_to(ROOT).as_posix(), "sha256": raster_sha,
            "source_manifest_path": source_manifest_path.relative_to(ROOT).as_posix(),
            "source_manifest_sha256": manifest_sha,
            "expected_stat_identity": restricted_worker._stat_identity(raster_path), "components": [],
        },
        "mesh": {"path": mesh_path.relative_to(ROOT).as_posix(), "sha256": mesh_sha},
        "mapping_config": {
            "format": "triangle-terrain-windowed-v1", "max_source_pixels": 65_536,
            "tile_rows": 256, "tile_cols": 256, "max_inflight_tiles": 1, "gdal_cache_mib": 64,
            "max_faces": 500_000, "max_nodes": 2_000_000,
            "max_candidate_overlap_checks": 1_000_000_000, "coverage_policy": "diagnostic_only",
        },
        "crs_alias": alias,
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return config_path


def _config(config_path: Path) -> dict[str, Any]:
    return json.loads(config_path.read_text(encoding="utf-8"))


def _save_config(path: Path, config: dict[str, Any]) -> None:
    path.write_text(json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")


def _install_resume_state_trace(monkeypatch: pytest.MonkeyPatch,
                                events: list[tuple[Any, ...]]) -> list[Any]:
    states: list[Any] = []
    original_win_api = restricted_worker._win_api
    original_record_resume = restricted_worker._WorkerStartState.record_resume_success

    class ResumeThreadProxy:
        def __init__(self, kernel32: Any) -> None:
            self._kernel32 = kernel32

        def __getattr__(self, name: str) -> Any:
            return getattr(self._kernel32, name)

        def ResumeThread(self, thread_handle: Any) -> int:
            previous_count = int(self._kernel32.ResumeThread(thread_handle))
            events.append(("native_resume_return", previous_count))
            return previous_count

    def traced_win_api() -> tuple[Any, Any]:
        kernel32, port = original_win_api()
        return ResumeThreadProxy(kernel32), port

    def traced_record_resume(state: Any, *, process_id: int, previous_suspend_count: int) -> None:
        assert events and events[-1] == ("native_resume_return", 1)
        original_record_resume(state, process_id=process_id,
                               previous_suspend_count=previous_suspend_count)
        states.append(state)
        events.append(("ever_started_true", state.ever_started, process_id, previous_suspend_count))

    monkeypatch.setattr(restricted_worker, "_win_api", traced_win_api)
    monkeypatch.setattr(restricted_worker._WorkerStartState,
                        "record_resume_success", traced_record_resume)
    return states


def _open_owned_process_handle(pid: int) -> tuple[Any, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return kernel32, handle


def _track_termination_empty_job(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    active_at_termination: list[int] = []
    original_terminate_job = restricted_worker._terminate_job

    def tracked_terminate_job(kernel32: Any, job: Any, process: Any, exit_code: int) -> None:
        original_terminate_job(kernel32, job, process, exit_code)
        deadline = time.monotonic() + 3
        active = restricted_worker._job_counters(job)[2]
        while active and time.monotonic() < deadline:
            time.sleep(0.01)
            active = restricted_worker._job_counters(job)[2]
        active_at_termination.append(active)

    monkeypatch.setattr(restricted_worker, "_terminate_job", tracked_terminate_job)
    return active_at_termination


@pytest.fixture
def case_directory():
    temp, path = _temporary_case()
    try:
        yield path
    finally:
        temp.cleanup()


def test_resource_budgets_are_required_and_strict() -> None:
    good = {
        "max_workers": 1, "job_memory_limit_mib": 2048, "wall_clock_limit_s": 900.0,
        "output_budget_bytes": 1_073_741_824, "scratch_budget_bytes": 67_108_864,
        "child_process_policy": CHILD_POLICY,
    }
    assert parse_worker_resources(good).job_memory_limit_mib == 2048
    with pytest.raises(ResourceBudgetError, match="missing"):
        parse_worker_resources({key: value for key, value in good.items() if key != "job_memory_limit_mib"})
    for field, invalid in (("max_workers", True), ("max_workers", 2),
                           ("job_memory_limit_mib", False), ("job_memory_limit_mib", 2048.5),
                           ("wall_clock_limit_s", float("inf")), ("output_budget_bytes", 2.5),
                           ("scratch_budget_bytes", 0), ("child_process_policy", "allow_breakaway")):
        candidate = dict(good)
        candidate[field] = invalid
        with pytest.raises(ResourceBudgetError):
            parse_worker_resources(candidate)


def test_worker_schema_example_and_runtime_contract_agree() -> None:
    import jsonschema

    schema_path = ROOT / "examples/triangular_mesh/restricted_worker_config.schema.json"
    example_path = ROOT / "examples/triangular_mesh/restricted_worker_config.example.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    example = json.loads(example_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(example)
    restricted_worker.validate_config_shape(example)
    parse_worker_resources(example["resources"])


def test_worker_mapping_defs_match_m2a_schema_and_null_evidence_rules() -> None:
    import jsonschema
    from pydantic import ValidationError
    from hydrodynamics.terrain_windowed_mapping import WindowedTriangleMappingConfig

    worker_schema = json.loads((ROOT / "examples/triangular_mesh/restricted_worker_config.schema.json").read_text(encoding="utf-8"))
    m2a_schema = json.loads((ROOT / "examples/triangular_mesh/windowed_mapping_config.schema.json").read_text(encoding="utf-8"))
    for name in ("$schema", "$id", "title"):
        m2a_schema.pop(name, None)
    assert worker_schema["$defs"]["windowedMappingConfig"] == m2a_schema
    validator = jsonschema.Draft202012Validator(worker_schema)
    example = json.loads((ROOT / "examples/triangular_mesh/restricted_worker_config.example.json").read_text(encoding="utf-8"))
    map_validator = jsonschema.Draft202012Validator(worker_schema["$defs"]["windowedMappingConfig"])
    base_mapping = example["mapping_config"]

    invalid_cases = []
    unit_null_evidence = dict(base_mapping)
    unit_null_evidence.update({"source_vertical_unit_declaration": "m",
                               "source_vertical_unit_evidence_path": None,
                               "source_vertical_unit_evidence_sha256": None})
    invalid_cases.append(unit_null_evidence)
    unit_missing_declaration = dict(base_mapping)
    unit_missing_declaration.update({"source_vertical_unit_declaration": None,
                                     "source_vertical_unit_evidence_path": "unit.txt",
                                     "source_vertical_unit_evidence_sha256": "0" * 64})
    invalid_cases.append(unit_missing_declaration)
    datum_null_evidence = dict(base_mapping)
    datum_null_evidence.update({"source_vertical_datum_declaration": "大沽高程",
                                "source_vertical_datum_evidence_path": None,
                                "source_vertical_datum_evidence_sha256": None})
    invalid_cases.append(datum_null_evidence)
    datum_missing_declaration = dict(base_mapping)
    datum_missing_declaration.update({"source_vertical_datum_declaration": None,
                                      "source_vertical_datum_evidence_path": "datum.txt",
                                      "source_vertical_datum_evidence_sha256": "1" * 64})
    invalid_cases.append(datum_missing_declaration)
    for candidate in invalid_cases:
        with pytest.raises(jsonschema.ValidationError):
            map_validator.validate(candidate)
        with pytest.raises(ValidationError):
            WindowedTriangleMappingConfig.model_validate(candidate)

    integral_stat = dict(example)
    integral_stat["source"] = dict(example["source"])
    integral_stat["source"]["expected_stat_identity"] = {
        "size_bytes": 1.0, "mtime_ns": 2, "ctime_ns": 3, "device": 4, "file_id": 5,
    }
    validator.validate(integral_stat)
    normalized = restricted_worker.validate_config_shape(integral_stat)
    assert normalized["source"]["expected_stat_identity"]["size_bytes"] == 1
    fractional_stat = json.loads(json.dumps(integral_stat))
    fractional_stat["source"]["expected_stat_identity"]["size_bytes"] = 1.5
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(fractional_stat)
    with pytest.raises(RestrictedWorkerError):
        restricted_worker.validate_config_shape(fractional_stat)


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_small_case_is_atomically_published_as_preparation_diagnostic(case_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _make_case(case_directory / "case", run_id="small-positive")
    config = _config(config_path)
    old = case_directory / "old-result"
    old.mkdir()
    sentinel = old / "keep.txt"
    sentinel.write_bytes(b"existing result remains byte-identical")
    old_sha = _sha256(sentinel)

    result = run_restricted_preparation(config_path, project_root=ROOT)

    assert result.status == "preparation_diagnostic_complete"
    assert result.exit_code == 0
    assert result.worker_started is True
    assert result.bundle_path is not None and result.bundle_path.is_dir()
    assert result.diagnostic_path is None
    receipt = dict(result.worker_receipt or {})
    assert receipt["os_mechanism"] == "Windows Job Object"
    assert receipt["os_limit_enforced"] is True
    assert receipt["limit_installed_before_resume"] is True
    assert receipt["creation_suspended"] is True
    assert receipt["breakaway_allowed"] is False
    assert receipt["kill_on_job_close"] is True
    assert receipt["job_active_processes_at_exit"] == 0
    assert receipt["job_peak_commit_bytes"] <= receipt["job_memory_limit_bytes"]
    assert receipt["scratch_peak_bytes"] <= config["resources"]["scratch_budget_bytes"]
    assert receipt["output_peak_bytes"] <= config["resources"]["output_budget_bytes"]
    assert receipt["job_process_ids_observed"]
    assert not (result.bundle_path / "scratch").exists()
    assert not (result.bundle_path / "latest").exists()
    assert not any(result.bundle_path.glob("*.nc"))
    assert not any(result.bundle_path.glob("*.stepjson"))
    assert not any(result.bundle_path.glob("*.h"))
    assert not any(result.bundle_path.glob("*.uv"))
    assert _sha256(sentinel) == old_sha

    manifest = json.loads((result.bundle_path / "manifest.json").read_text(encoding="utf-8"))
    status = json.loads((result.bundle_path / "status.json").read_text(encoding="utf-8"))
    assert status["worker_started"] is result.worker_started
    assert manifest["preparation_only"] is True
    assert manifest["simulation_result"] is False
    assert manifest["production_approved"] is False
    assert manifest["engineering_ready"] is False
    assert manifest["coverage_status"] == "policy_pending_diagnostic_only"
    assert manifest["coverage_policy_pending"] is True
    assert manifest["source"]["terrain_source_vertical_datum_status"] == "unknown_source_datum_unattested"
    relation = manifest["source"]["vertical_relation_context"]
    assert relation["target_minus_source_m"] == -1.56
    assert relation["scope"] == "下游东淀-文安洼区域"
    assert relation["applied"] is False
    assert relation["proves_dem_source_datum"] is False
    assert manifest["source"]["coordinate_system_alias_evidence"]["coordinate_transform_performed"] is False
    assert manifest["source"]["coordinate_system_alias_evidence"]["raw_source_wkt_retained"] is True

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("prepared readback must not read the DEM or rerun mapping")

    monkeypatch.setattr("hydrodynamics.dem_contract.GeoTIFFDEMReader.read_dataset", forbidden)
    monkeypatch.setattr("hydrodynamics.terrain_windowed_mapping.map_triangular_mesh_from_windows", forbidden)
    prepared = read_prepared_terrain(result.bundle_path, expected_bindings=manifest["bindings"])
    assert len(list(prepared.iter_faces())) == 2

    from hydrodynamics.native_result_stream import NativeFrameSourceError, open_validated_native_result_frames

    with pytest.raises(NativeFrameSourceError):
        open_validated_native_result_frames(result.bundle_path / "manifest.json")


@pytest.mark.skipif(os.name != "nt", reason="B0 production reader profile is Windows-targeted")
def test_crs_alias_reader_releases_each_raw_tile_before_next_read(case_directory: Path) -> None:
    from hydrodynamics.dem_contract import GeoTIFFDEMReader
    from hydrodynamics.restricted_worker import _CRSAliasReader

    source = case_directory / "alias-stream.tif"
    with Env(GDAL_PAM_ENABLED="NO"):
        with rasterio.open(source, "w", driver="GTiff", width=8, height=8, count=1,
                           dtype="float32", nodata=-32767.0,
                           transform=from_origin(473506.0, 4335988.0, 1.0, 1.0),
                           crs="EPSG:4548") as dataset:
            dataset.write(np.ones((8, 8), dtype=np.float32), 1)
            dataset.set_band_unit(1, "metre")
    with Env(GDAL_PAM_ENABLED="NO", GDAL_GEOREF_SOURCES="INTERNAL"):
        with rasterio.open(source, sharing=False) as dataset:
            source_wkt = dataset.crs.to_wkt(version="WKT1_GDAL")
    alias = {"mode": "verify_embedded_epsg4548_xy", "target_crs": "EPSG:4548",
             "expected_source_wkt_sha256": hashlib.sha256(source_wkt.encode("utf-8")).hexdigest()}

    class TrackingReader(GeoTIFFDEMReader):
        def __init__(self) -> None:
            self.refs: list[weakref.ReferenceType[Any]] = []
            self.peak_live = 0

        def _read_open_window(self, *args: Any, **kwargs: Any) -> Any:
            gc.collect()
            live = sum(reference() is not None for reference in self.refs)
            self.peak_live = max(self.peak_live, live + 1)
            chunk = super()._read_open_window(*args, **kwargs)
            self.refs.append(weakref.ref(chunk))
            return chunk

    for alias_value in (alias, None):
        tracking_reader = TrackingReader()
        wrapped = _CRSAliasReader(tracking_reader, alias_value)
        source_metadata = wrapped.read_metadata(source)
        expected_crs = "EPSG:4548" if alias_value else source_metadata.crs
        windows = wrapped.iter_windows_selected(
            source, window_width=2, window_height=2, gdal_cache_mib=64,
            include_window=lambda metadata, _window: metadata.crs == expected_crs,
        )
        count = 0
        try:
            while True:
                try:
                    item = next(windows)
                except StopIteration:
                    break
                window, chunk = item
                assert chunk.shape == (2, 2)
                count += 1
                del chunk, item, window
        finally:
            windows.close()
        gc.collect()
        assert count == 16
        assert tracking_reader.peak_live == 1
        assert all(reference() is None for reference in tracking_reader.refs)


@pytest.mark.skipif(os.name != "nt", reason="B0 path handling targets Windows filesystem limits")
def test_224_character_final_receipt_path_uses_short_owned_temporary_names(case_directory: Path) -> None:
    config_path = _make_case(case_directory / "case", run_id="longr001")
    config = _config(config_path)
    target_output_units = 186
    leaf_length = target_output_units - len(str(case_directory / "case")) - 1
    assert 1 <= leaf_length < 240
    output_root = case_directory / "case" / ("o" * leaf_length)
    assert len(str(output_root)) == target_output_units
    config["output_root"] = output_root.relative_to(ROOT).as_posix()
    _save_config(config_path, config)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "preparation_diagnostic_complete", result
    assert result.bundle_path is not None
    final_receipt = result.bundle_path / "artifacts" / "worker-result.json"
    assert len(str(final_receipt)) == 224
    assert final_receipt.is_file()
    assert (result.bundle_path / "READY").is_file()

    mismatch_config = _make_case(case_directory / "mismatch", run_id="longr002")
    mismatch = _config(mismatch_config)
    mismatch["output_root"] = config["output_root"]
    mismatch["source"]["sha256"] = "f" * 64
    _save_config(mismatch_config, mismatch)
    mismatch_result = run_restricted_preparation(mismatch_config, project_root=ROOT)
    assert mismatch_result.status == "source_identity_rejected", mismatch_result
    assert mismatch_result.exit_code == 54
    assert mismatch_result.diagnostic_path is not None
    assert not (mismatch_result.diagnostic_path / "READY").exists()
    worker_result_path = mismatch_result.diagnostic_path / "artifacts" / "worker-result.json"
    assert worker_result_path.is_file()
    worker_result = json.loads(worker_result_path.read_text(encoding="utf-8"))
    assert worker_result["category"] == "source_identity_rejected"
    assert worker_result["worker_exit_code"] == 54


@pytest.mark.skipif(os.name != "nt", reason="B0 path handling targets Windows filesystem limits")
def test_overlong_worker_paths_are_refused_before_directory_or_worker_creation(case_directory: Path) -> None:
    config_path = _make_case(case_directory / "case", run_id="overlong1")
    config = _config(config_path)
    overlong_root = case_directory / "case" / ("q" * (250 - len(str(case_directory / "case")) - 1))
    config["output_root"] = overlong_root.relative_to(ROOT).as_posix()
    _save_config(config_path, config)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "path_rejected"
    assert result.exit_code == 55
    assert result.worker_started is False
    assert result.diagnostic_path is None
    assert not overlong_root.exists()


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_missing_memory_budget_is_a_prelaunch_resource_refusal(case_directory: Path) -> None:
    config_path = _make_case(case_directory / "case", run_id="missing-memory-budget")
    config = _config(config_path)
    del config["resources"]["job_memory_limit_mib"]
    _save_config(config_path, config)
    sentinel_dir = case_directory / "existing-output"
    sentinel_dir.mkdir()
    sentinel = sentinel_dir / "keep.bin"
    sentinel.write_bytes(b"keep")
    old_sha = _sha256(sentinel)

    result = run_restricted_preparation(config_path, project_root=ROOT)

    assert result.status == "resource_rejected"
    assert result.exit_code == 40
    assert result.worker_started is False
    assert result.diagnostic_path is not None
    assert not (result.diagnostic_path / "READY").exists()
    status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "resource_rejected"
    assert status["ready"] is False
    assert not (result.diagnostic_path.parent / "latest").exists()
    assert _sha256(sentinel) == old_sha


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_prelaunch_stat_mismatch_returns_source_identity_refusal_without_worker(
        case_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _make_case(case_directory / "case", run_id="prelaunch-stat-mismatch")
    config = _config(config_path)
    config["source"]["expected_stat_identity"]["size_bytes"] += 1
    _save_config(config_path, config)
    launches: list[bool] = []
    original = restricted_worker._run_contained_command

    def track_launch(command: list[str], **kwargs: Any) -> dict[str, Any]:
        launches.append(True)
        return original(command, **kwargs)

    monkeypatch.setattr(restricted_worker, "_run_contained_command", track_launch)
    result = run_restricted_preparation(config_path, project_root=ROOT)

    output_root = ROOT / config["output_root"]
    assert result.status == "source_identity_rejected", result
    assert result.exit_code == 54
    assert result.worker_started is False
    assert result.worker_receipt is None
    assert result.message == "source raster stat identity differs before worker launch"
    assert launches == []
    assert result.diagnostic_path is not None
    assert result.diagnostic_path.name == "prelaunch-stat-mismatch.failed"
    assert not list(output_root.glob(".*.pending"))
    assert not (result.diagnostic_path / "scratch/worker-created.json").exists()
    assert not (result.diagnostic_path / "scratch/worker-resumed.json").exists()
    assert not (result.diagnostic_path / "READY").exists()
    assert not (result.diagnostic_path / "READY.pending").exists()
    status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "source_identity_rejected"
    assert status["exit_code"] == 54
    assert status["worker_started"] is False
    assert status["message"] == result.message


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_early_io_failure_preserves_reason_and_reports_no_worker(
        case_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _make_case(case_directory / "case", run_id="early-audit-io-failure")
    config = _config(config_path)
    _save_config(config_path, config)

    def fail_code_audit(_project_root: Path) -> dict[str, str]:
        raise OSError("injected early code-audit I/O failure")

    monkeypatch.setattr(restricted_worker, "_module_code_hashes", fail_code_audit)
    result = run_restricted_preparation(config_path, project_root=ROOT)

    output_root = ROOT / config["output_root"]
    assert result.status == "supervisor_failed"
    assert result.exit_code == 70
    assert result.worker_started is False
    assert result.worker_receipt is None
    assert result.message == "OSError: injected early code-audit I/O failure"
    assert result.diagnostic_path is not None
    assert result.diagnostic_path.name == "early-audit-io-failure.failed"
    assert not list(output_root.glob(".*.pending"))
    assert not (result.diagnostic_path / "scratch/worker-created.json").exists()
    assert not (result.diagnostic_path / "scratch/worker-resumed.json").exists()
    assert not (result.diagnostic_path / "READY").exists()
    assert not (result.diagnostic_path / "READY.pending").exists()
    status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "supervisor_failed"
    assert status["exit_code"] == 70
    assert status["worker_started"] is False
    assert status["message"] == result.message


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
@pytest.mark.parametrize(("stop_status", "expected_exit_code"), [("cancelled", 44), ("timed_out", 43)])
def test_early_prepared_stop_is_classified_before_worker_launch(
        case_directory: Path, monkeypatch: pytest.MonkeyPatch,
        stop_status: str, expected_exit_code: int) -> None:
    config_path = _make_case(case_directory / "case", run_id=f"early-stop-{stop_status}")
    config = _config(config_path)

    def stop_before_launch(_project_root: Path) -> dict[str, str]:
        raise PreparedTerrainStopped(stop_status)

    monkeypatch.setattr(restricted_worker, "_module_code_hashes", stop_before_launch)
    result = run_restricted_preparation(config_path, project_root=ROOT)

    output_root = ROOT / config["output_root"]
    assert result.status == stop_status
    assert result.exit_code == expected_exit_code
    assert result.worker_started is False
    assert result.worker_receipt is None
    assert result.message == f"prepared readback stopped: {stop_status}"
    assert result.diagnostic_path is not None
    assert result.diagnostic_path.name == f"early-stop-{stop_status}.failed"
    assert not list(output_root.glob(".*.pending"))
    assert not (result.diagnostic_path / "scratch/worker-created.json").exists()
    assert not (result.diagnostic_path / "scratch/worker-resumed.json").exists()
    assert not (result.diagnostic_path / "READY").exists()
    assert not (result.diagnostic_path / "READY.pending").exists()
    status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == stop_status
    assert status["exit_code"] == expected_exit_code
    assert status["worker_started"] is False
    assert status["message"] == result.message


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_resumed_job_process_hook_failure_returns_started_consistently(
        case_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _make_case(case_directory / "case", run_id="resume-hook-failure")
    output_root = case_directory / "output-root"
    events: list[tuple[Any, ...]] = []
    states = _install_resume_state_trace(monkeypatch, events)
    active_at_termination = _track_termination_empty_job(monkeypatch)
    original_write = restricted_worker._write_no_clobber_bytes
    held: dict[int, tuple[Any, Any]] = {}

    def hold(pid: int) -> None:
        if pid in held:
            return
        kernel32, handle = _open_owned_process_handle(pid)
        assert handle
        held[pid] = (kernel32, handle)

    def track_resume_journal(path: Path, payload: bytes, **kwargs: Any) -> None:
        if path.name == "worker-resumed.json":
            assert states and states[0].ever_started is True
            events.append(("resume_journal_write_begin", states[0].ever_started))
            original_write(path, payload, **kwargs)
            events.append(("resume_journal_write_complete",))
            return
        original_write(path, payload, **kwargs)

    def created_suspended(info: Mapping[str, Any]) -> None:
        hold(int(info["process_id"]))
        events.append(("created_suspended_hook",))

    def fail_after_journal(info: Mapping[str, Any]) -> None:
        assert states and states[0].ever_started is True
        journal = next(output_root.glob(".*.pending")) / "scratch" / "worker-resumed.json"
        assert journal.is_file()
        for pid in info["process_ids"]:
            hold(int(pid))
        events.append(("post_resume_process_hook",))
        raise OSError("injected after-resume process hook failure")

    monkeypatch.setattr(restricted_worker, "_write_no_clobber_bytes", track_resume_journal)
    try:
        result = run_restricted_preparation(
            config_path, project_root=ROOT, output_root_override=output_root,
            _on_worker_created_suspended=created_suspended,
            _on_worker_job_processes=fail_after_journal,
        )
        assert result.status == "supervisor_failed"
        assert result.exit_code == 70
        assert result.message == "OSError: injected after-resume process hook failure"
        assert result.worker_started is True
        assert result.worker_receipt is None
        assert result.diagnostic_path is not None
        status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
        assert status["worker_started"] is result.worker_started
        assert status["message"] == result.message
        assert not (result.diagnostic_path / "READY").exists()
        assert not (result.diagnostic_path / "READY.pending").exists()
        assert not list(output_root.glob(".*.pending"))
        journal = json.loads((result.diagnostic_path / "scratch/worker-resumed.json").read_text(encoding="utf-8"))
        assert journal["format"] == "triangle-worker-resumed-v2"
        assert journal["resume_succeeded"] is True
        assert journal["resume_previous_suspend_count"] == 1
        assert events.index(("native_resume_return", 1)) < events.index(("ever_started_true", True, states[0].process_id, 1))
        assert events.index(("ever_started_true", True, states[0].process_id, 1)) < events.index(("resume_journal_write_begin", True))
        assert events.index(("resume_journal_write_complete",)) < events.index(("post_resume_process_hook",))
        assert active_at_termination == [0]
        assert held
        assert all(kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0
                   for kernel32, handle in held.values())
    finally:
        for kernel32, handle in held.values():
            kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_resume_journal_write_failure_returns_started_consistently(
        case_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _make_case(case_directory / "case", run_id="resume-journal-failure")
    output_root = case_directory / "output-root"
    events: list[tuple[Any, ...]] = []
    states = _install_resume_state_trace(monkeypatch, events)
    active_at_termination = _track_termination_empty_job(monkeypatch)
    original_write = restricted_worker._write_no_clobber_bytes
    held: dict[int, tuple[Any, Any]] = {}

    def hold_created_process(info: Mapping[str, Any]) -> None:
        kernel32, handle = _open_owned_process_handle(int(info["process_id"]))
        assert handle
        held[int(info["process_id"])] = (kernel32, handle)

    def fail_resume_journal(path: Path, payload: bytes, **kwargs: Any) -> None:
        if path.name == "worker-resumed.json":
            assert states and states[0].ever_started is True
            events.append(("resume_journal_write_failure", states[0].ever_started))
            time.sleep(0.05)
            raise OSError("injected after-resume journal I/O failure")
        original_write(path, payload, **kwargs)

    monkeypatch.setattr(restricted_worker, "_write_no_clobber_bytes", fail_resume_journal)
    try:
        result = run_restricted_preparation(
            config_path, project_root=ROOT, output_root_override=output_root,
            _on_worker_created_suspended=hold_created_process,
        )
        assert result.status == "supervisor_failed"
        assert result.exit_code == 70
        assert result.message == "OSError: injected after-resume journal I/O failure"
        assert result.worker_started is True
        assert result.worker_receipt is None
        assert result.diagnostic_path is not None
        status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
        assert status["worker_started"] is result.worker_started
        assert status["message"] == result.message
        assert not (result.diagnostic_path / "scratch/worker-resumed.json").exists()
        assert not (result.diagnostic_path / "READY").exists()
        assert not (result.diagnostic_path / "READY.pending").exists()
        assert not list(output_root.glob(".*.pending"))
        assert events.index(("native_resume_return", 1)) < events.index(("ever_started_true", True, states[0].process_id, 1))
        assert events.index(("ever_started_true", True, states[0].process_id, 1)) < events.index(("resume_journal_write_failure", True))
        assert active_at_termination == [0]
        assert held
        assert all(kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0
                   for kernel32, handle in held.values())
    finally:
        for kernel32, handle in held.values():
            kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_created_suspended_io_failure_and_forged_resume_file_stay_not_started(
        case_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _make_case(case_directory / "case", run_id="created-suspended-failure")
    output_root = case_directory / "output-root"
    events: list[tuple[Any, ...]] = []
    states = _install_resume_state_trace(monkeypatch, events)
    active_at_termination = _track_termination_empty_job(monkeypatch)
    held: dict[int, tuple[Any, Any]] = {}

    def fail_before_resume(info: Mapping[str, Any]) -> None:
        pid = int(info["process_id"])
        kernel32, handle = _open_owned_process_handle(pid)
        assert handle
        held[pid] = (kernel32, handle)
        stage = next(output_root.glob(".*.pending"))
        forged_resume = {"format": "triangle-worker-resumed-v2", "resume_succeeded": True,
                         "resume_previous_suspend_count": 1, "process_id": pid,
                         "process_ids": [pid], "recorded_at": "test-hook-forgery"}
        (stage / "scratch" / "worker-resumed.json").write_text(
            json.dumps(forged_resume), encoding="utf-8")
        events.append(("forged_resume_file_written_before_resume",))
        raise OSError("injected created-suspended supervisor I/O failure")

    try:
        result = run_restricted_preparation(
            config_path, project_root=ROOT, output_root_override=output_root,
            _on_worker_created_suspended=fail_before_resume,
        )
        assert result.status == "supervisor_failed"
        assert result.exit_code == 70
        assert result.message == "OSError: injected created-suspended supervisor I/O failure"
        assert result.worker_started is False
        assert result.worker_receipt is None
        assert result.diagnostic_path is not None
        status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
        assert status["worker_started"] is result.worker_started
        assert status["message"] == result.message
        assert status["worker_created_suspended"] is True
        assert (result.diagnostic_path / "scratch/worker-resumed.json").is_file()
        assert not any(event[0] == "native_resume_return" for event in events)
        assert not states
        assert not (result.diagnostic_path / "READY").exists()
        assert not (result.diagnostic_path / "READY.pending").exists()
        assert not list(output_root.glob(".*.pending"))
        assert active_at_termination == [0]
        assert held
        assert all(kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0
                   for kernel32, handle in held.values())
    finally:
        for kernel32, handle in held.values():
            kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_corrupt_source_is_reported_as_read_failure_without_ready(case_directory: Path) -> None:
    config_path = _make_case(case_directory / "case", run_id="read-failure", valid_raster=False)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "failed_read"
    assert result.exit_code == 51
    assert result.worker_started is True
    assert result.diagnostic_path is not None
    assert not (result.diagnostic_path / "READY").exists()
    assert not (result.diagnostic_path / "latest").exists()
    status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed_read"
    assert status["worker"]["job_active_processes_at_exit"] == 0


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_source_manifest_must_bind_the_configured_raster_sha(case_directory: Path) -> None:
    config_path = _make_case(case_directory / "case", run_id="source-manifest-mismatch")
    config = _config(config_path)
    config["source"]["sha256"] = "f" * 64
    _save_config(config_path, config)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "source_identity_rejected"
    assert result.exit_code == 54
    assert result.worker_started is True
    assert result.diagnostic_path is not None
    assert not (result.diagnostic_path / "READY").exists()
    status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "source_identity_rejected"


@pytest.mark.skipif(os.name != "nt", reason="B0 diagnostics require the Windows Job Object worker")
def test_source_mismatch_survives_worker_result_receipt_write_failure(case_directory: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _make_case(case_directory / "case", run_id="source-receipt-failure")
    config = _config(config_path)
    config["source"]["sha256"] = "e" * 64
    _save_config(config_path, config)
    original = restricted_worker._run_contained_command

    def fail_main_receipt(command: list[str], **kwargs: Any) -> dict[str, Any]:
        (kwargs["output_directory"] / "artifacts" / "worker-result.json").mkdir()
        return original(command, **kwargs)

    monkeypatch.setattr(restricted_worker, "_run_contained_command", fail_main_receipt)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "source_identity_rejected", result
    assert result.exit_code == 54
    assert result.worker_receipt is not None
    assert result.worker_receipt["root_exit_code"] == 54
    assert result.worker_receipt["worker_result_write_failed"] is True
    assert "worker-result receipt write failed" in result.message
    assert result.diagnostic_path is not None
    assert not (result.diagnostic_path / "READY").exists()
    fallback = json.loads((result.diagnostic_path / "scratch/worker-failure.json").read_text(encoding="utf-8"))
    assert fallback["category"] == "source_identity_rejected"
    assert any(name in fallback["worker_result_write_error"] for name in ("PreparedTerrainWriteError", "FileExistsError", "OSError"))


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_null_raw_vertical_unit_uses_verified_declaration_provenance(case_directory: Path) -> None:
    config_path = _make_case(case_directory / "case", run_id="declared-unit", vertical_unit_metadata=False)
    config = _config(config_path)
    evidence = config_path.parent / "source-unit-evidence.txt"
    evidence.write_text("Synthetic source declaration: elevations are metres.\n", encoding="utf-8")
    evidence_sha = _sha256(evidence)
    config["mapping_config"].update({
        "source_vertical_unit_declaration": "m",
        "source_vertical_unit_evidence_path": evidence.name,
        "source_vertical_unit_evidence_sha256": evidence_sha,
    })
    _save_config(config_path, config)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "preparation_diagnostic_complete"
    manifest = json.loads((result.bundle_path / "manifest.json").read_text(encoding="utf-8"))
    source = manifest["source"]
    assert source["metadata"]["vertical_unit"] is None
    assert source["metadata"]["vertical_unit_source"] == "config_declaration"
    assert source["terrain_vertical_unit_status"] == "source_declared_metre_with_verified_evidence"
    assert source["source_vertical_unit_evidence"]["sha256"] == evidence_sha
    assert source["terrain_source_vertical_datum_status"] == "unknown_source_datum_unattested"
    assert source["vertical_relation_context"]["applied"] is False


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_prepared_writer_filesystem_error_is_not_published(case_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _make_case(case_directory / "case", run_id="write-failure")
    original = restricted_worker._run_contained_command

    def collide_with_prepared_target(command: list[str], **kwargs: Any) -> dict[str, Any]:
        target = kwargs["output_directory"] / "artifacts" / "prepared_faces.jsonl"
        target.mkdir()
        return original(command, **kwargs)

    monkeypatch.setattr(restricted_worker, "_run_contained_command", collide_with_prepared_target)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "failed_write", result
    assert result.exit_code == 52
    assert result.diagnostic_path is not None
    assert not (result.diagnostic_path / "READY").exists()
    assert not (result.diagnostic_path / "latest").exists()
    status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed_write"
    assert status["worker"]["job_active_processes_at_exit"] == 0


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_output_budget_rejection_preserves_old_outputs(case_directory: Path) -> None:
    config_path = _make_case(case_directory / "case", run_id="output-budget", face_count=1000,
                             output_budget_bytes=131_072)
    old = case_directory / "old"
    old.mkdir()
    sentinel = old / "existing.txt"
    sentinel.write_bytes(b"do not replace")
    old_sha = _sha256(sentinel)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "resource_rejected"
    assert result.exit_code == 40
    assert result.diagnostic_path is not None
    assert not (result.diagnostic_path / "READY").exists()
    assert _sha256(sentinel) == old_sha


def _job_directories(case: Path) -> tuple[Path, Path]:
    scratch, output = case / "scratch", case / "output"
    scratch.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    return scratch, output


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_job_object_kills_worker_tree_on_memory_limit_and_rejects_breakaway(case_directory: Path) -> None:
    scratch, output = _job_directories(case_directory)
    child_code = (
        "import ctypes,time; size=320*1024*1024; data=bytearray(size); "
        "ctypes.memset(ctypes.addressof(ctypes.c_char.from_buffer(data)),1,size); time.sleep(10)"
    )
    root_code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c'," + repr(child_code) + "]); p.wait()"
    receipt = restricted_worker._run_contained_command(
        [sys.executable, "-c", root_code], cwd=ROOT, environment=dict(os.environ),
        memory_limit_mib=128, wall_clock_limit_s=15, scratch_directory=scratch,
        output_directory=output, scratch_budget_bytes=1_048_576, output_budget_bytes=1_048_576,
    )
    assert receipt["termination_reason"] == "resource_rejected"
    assert set(receipt["memory_limit_messages"]) & MEMORY_LIMIT_FLAG_CODES
    assert receipt["job_active_processes_at_exit"] == 0

    scratch2, output2 = _job_directories(case_directory / "breakaway")
    marker = scratch2 / "breakaway-probe.json"
    child_code = (
        "import ctypes,json,time\nfrom ctypes import wintypes\nfrom pathlib import Path\n"
        "kernel=ctypes.WinDLL('kernel32',use_last_error=True)\n"
        "kernel.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]\n"
        "kernel.IsProcessInJob.restype=wintypes.BOOL\nflag=wintypes.BOOL()\n"
        "ok=kernel.IsProcessInJob(wintypes.HANDLE(-1),None,ctypes.byref(flag))\n"
        "Path(" + repr(str(marker)) + ").write_text(json.dumps({'pid':__import__('os').getpid(),'query_ok':bool(ok),'in_job':bool(flag.value)}))\n"
        "time.sleep(0.1)\n"
    )
    breakaway_code = (
        "import subprocess,sys,json\nfrom pathlib import Path\n"
        "marker=Path(" + repr(str(marker)) + ")\n"
        "try:\n    p=subprocess.Popen([sys.executable,'-c'," + repr(child_code) + "],creationflags=0x01000000)\n    p.wait(timeout=5)\n"
        "except OSError as error:\n    marker.write_text(json.dumps({'launch_rejected':True,'winerror':getattr(error,'winerror',None)}))\n"
    )
    receipt2 = restricted_worker._run_contained_command(
        [sys.executable, "-c", breakaway_code], cwd=ROOT, environment=dict(os.environ),
        memory_limit_mib=512, wall_clock_limit_s=15, scratch_directory=scratch2,
        output_directory=output2, scratch_budget_bytes=1_048_576, output_budget_bytes=1_048_576,
    )
    assert receipt2["termination_reason"] is None
    assert receipt2["job_active_processes_at_exit"] == 0
    assert marker.is_file()
    probe = json.loads(marker.read_text(encoding="utf-8"))
    assert probe.get("launch_rejected") is True or probe.get("query_ok") is True and probe.get("in_job") is True
    if probe.get("in_job") is True:
        assert probe["pid"] in receipt2["job_process_ids_observed"]


@pytest.mark.skipif(os.name != "nt", reason="B0 resource enforcement requires Windows Job Objects")
def test_high_level_memory_timeout_and_cancel_paths_leave_no_success_marker(case_directory: Path) -> None:
    memory_config = _make_case(case_directory / "memory", run_id="memory-refusal")
    memory = _config(memory_config)
    memory["resources"]["job_memory_limit_mib"] = 256
    _save_config(memory_config, memory)
    memory_result = run_restricted_preparation(memory_config, project_root=ROOT)
    assert memory_result.status == "resource_rejected"
    assert memory_result.diagnostic_path is not None
    assert not (memory_result.diagnostic_path / "READY").exists()
    assert memory_result.worker_receipt is not None
    assert set(memory_result.worker_receipt["memory_limit_messages"]) & MEMORY_LIMIT_FLAG_CODES
    assert memory_result.worker_receipt["job_active_processes_at_exit"] == 0

    timeout_config = _make_case(case_directory / "timeout", run_id="timeout")
    timeout = _config(timeout_config)
    timeout["resources"]["wall_clock_limit_s"] = 0.1
    _save_config(timeout_config, timeout)
    timeout_result = run_restricted_preparation(timeout_config, project_root=ROOT)
    assert timeout_result.status == "timed_out"
    assert timeout_result.diagnostic_path is not None
    assert not (timeout_result.diagnostic_path / "READY").exists()
    assert timeout_result.worker_receipt is not None
    assert timeout_result.worker_receipt["job_active_processes_at_exit"] == 0

    cancel_config = _make_case(case_directory / "cancel", run_id="cancel")
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    try:
        cancel_result = run_restricted_preparation(cancel_config, project_root=ROOT, cancel_event=cancel)
    finally:
        timer.join(timeout=2)
    assert cancel_result.status == "cancelled"
    assert cancel_result.diagnostic_path is not None
    assert not (cancel_result.diagnostic_path / "READY").exists()
    assert cancel_result.worker_receipt is not None
    assert cancel_result.worker_receipt["job_active_processes_at_exit"] == 0


@pytest.mark.skipif(os.name != "nt", reason="B0 cancellation/readback contract is exercised with the Windows worker")
def test_cancel_during_internal_prepared_readback_aborts_before_commit(case_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hydrodynamics.prepared_terrain as prepared_module

    config_path = _make_case(case_directory / "case", run_id="cancel-during-readback")
    cancel = threading.Event()
    original_reader = prepared_module.read_prepared_terrain
    calls = 0

    def cancel_on_internal_readback(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        if kwargs.get("allow_pending_marker") is True:
            calls += 1
            cancel.set()
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(prepared_module, "read_prepared_terrain", cancel_on_internal_readback)
    result = run_restricted_preparation(config_path, project_root=ROOT, cancel_event=cancel)
    assert calls == 1
    assert result.status == "cancelled"
    assert result.exit_code == 44
    assert result.diagnostic_path is not None
    assert not (result.diagnostic_path / "READY").exists()
    assert not (result.diagnostic_path / "READY.pending").exists()
    assert result.worker_receipt is not None
    assert result.worker_receipt["job_active_processes_at_exit"] == 0
    assert result.worker_receipt["termination_reason"] is None


@pytest.mark.skipif(os.name != "nt", reason="B0 publication commit semantics are exercised on Windows")
def test_pending_directory_with_ready_is_not_consumable_and_cancel_before_commit_rolls_back(case_directory: Path) -> None:
    config_path = _make_case(case_directory / "case", run_id="cancel-before-directory-commit")
    config = _config(config_path)
    cancel = threading.Event()
    output_root = ROOT / config["output_root"]
    previous = output_root / "older-prepared-result"
    previous.mkdir(parents=True)
    sentinel = previous / "keep.txt"
    sentinel.write_bytes(b"retain old diagnostic")
    old_sha = _sha256(sentinel)
    pending_paths: list[Path] = []

    def stop_at_commit(stage: Path) -> None:
        assert (stage / "READY").is_file()
        with pytest.raises(PreparedTerrainError, match="private pending attempt"):
            read_prepared_terrain(stage)
        pending_paths.append(stage)
        cancel.set()

    result = run_restricted_preparation(config_path, project_root=ROOT,
                                        cancel_event=cancel,
                                        _before_directory_commit=stop_at_commit)
    assert pending_paths
    assert result.status == "cancelled"
    assert result.exit_code == 44
    assert result.diagnostic_path is not None
    assert not (result.diagnostic_path / "READY").exists()
    assert not (result.diagnostic_path / "READY.pending").exists()
    assert not (output_root / config["run_id"]).exists()
    assert _sha256(sentinel) == old_sha


@pytest.mark.skipif(os.name != "nt", reason="B0 parent-exit publication test uses Windows process handles")
def test_parent_exit_after_ready_before_directory_rename_is_recovered(case_directory: Path) -> None:
    config_path = _make_case(case_directory / "case", run_id="parent-before-rename")
    config = _config(config_path)
    output_root = ROOT / config["output_root"]
    old = output_root / "old-result"
    old.mkdir(parents=True)
    sentinel = old / "keep.bin"
    sentinel.write_bytes(b"old result stays")
    old_sha = _sha256(sentinel)
    state_path = case_directory / "pending-publish.json"
    pid_path = case_directory / "publisher.pid"
    helper = case_directory / "publisher_helper.py"
    helper.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "sys.path.insert(0, " + repr(str(ROOT)) + ")\n"
        "from hydrodynamics.restricted_worker import run_restricted_preparation\n"
        "Path(" + repr(str(pid_path)) + ").write_text(str(os.getpid()))\n"
        "def pause(stage):\n"
        " Path(" + repr(str(state_path)) + ").write_text(json.dumps({'stage':str(stage),'ready':(stage/'READY').is_file()}))\n"
        " time.sleep(30)\n"
        "run_restricted_preparation(Path(" + repr(str(config_path)) + "), project_root=Path(" + repr(str(ROOT)) + "), _before_directory_commit=pause)\n",
        encoding="utf-8",
    )
    publisher = subprocess.Popen([sys.executable, str(helper)], cwd=ROOT,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not state_path.is_file():
            time.sleep(0.05)
        assert state_path.is_file()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        stage = Path(state["stage"])
        assert state["ready"] is True
        assert (stage / "READY").is_file()
        assert not (output_root / config["run_id"]).exists()
        with pytest.raises(PreparedTerrainError, match="private pending attempt"):
            read_prepared_terrain(stage)

        publisher_pid = int(pid_path.read_text(encoding="utf-8"))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        publisher_handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, publisher_pid)
        assert publisher_handle
        try:
            assert kernel32.TerminateProcess(publisher_handle, 99)
            assert kernel32.WaitForSingleObject(publisher_handle, 5000) == WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(publisher_handle)
        publisher.wait(timeout=5)
        assert not (output_root / config["run_id"]).exists()
        recovered = recover_incomplete_attempts(output_root)
        assert len(recovered) == 1
        recovered_status = json.loads((recovered[0] / "status.json").read_text(encoding="utf-8"))
        assert recovered_status["status"] == "supervisor_lost"
        assert not (recovered[0] / "READY").exists()
        assert not (recovered[0] / "READY.pending").exists()
        assert _sha256(sentinel) == old_sha
    finally:
        if publisher.poll() is None:
            publisher.terminate()
            publisher.wait(timeout=5)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
    ]


def _process_snapshot() -> dict[int, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or int(snapshot) == -1:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    result: dict[int, int] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                result[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def _descendants(parent_pid: int) -> set[int]:
    parents = _process_snapshot()
    found = {parent_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parents.items():
            if ppid in found and pid not in found:
                found.add(pid)
                changed = True
    return found


def _pid_running(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # Access denied still means the PID exists.
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="B0 parent-close lifecycle uses Windows Job Objects")
def test_supervisor_process_exit_closes_job_and_recovery_removes_ready(case_directory: Path) -> None:
    # A larger synthetic mesh leaves enough time to terminate the self-spawned
    # supervisor while its mapped worker is active.
    config_path = _make_case(case_directory / "large-case", run_id="parent-exit-large", face_count=20_000)
    config = _config(config_path)
    pid_file = case_directory / "supervisor.pid"
    job_state = case_directory / "worker-job-state.json"
    helper = case_directory / "supervisor_helper.py"
    helper.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "sys.path.insert(0, " + repr(str(ROOT)) + ")\n"
        "from hydrodynamics.restricted_worker import run_restricted_preparation\n"
        "Path(" + repr(str(pid_file)) + ").write_text(str(os.getpid()))\n"
        "def hold_worker(state):\n"
        " if len(state.get('process_ids', [])) < 3:\n    return\n"
        " Path(" + repr(str(job_state)) + ").write_text(json.dumps(dict(state)))\n"
        " time.sleep(30)\n"
        "run_restricted_preparation(Path(" + repr(str(config_path)) + "), project_root=Path(" + repr(str(ROOT)) + "), _on_worker_job_processes=hold_worker)\n",
        encoding="utf-8",
    )
    supervisor = subprocess.Popen([sys.executable, str(helper)], cwd=ROOT,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, close_fds=True)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.05)
        assert pid_file.is_file()
        supervisor_pid = int(pid_file.read_text(encoding="utf-8"))
        output_root = ROOT / config["output_root"]
        while time.monotonic() < deadline and not job_state.is_file():
            time.sleep(0.05)
        assert job_state.is_file()
        job_status = json.loads(job_state.read_text(encoding="utf-8"))
        assert job_status["process_ids"]
        assert job_status["active_processes"] >= 2
        tree_before = _descendants(supervisor_pid)
        assert len(set(job_status["process_ids"]) & tree_before) >= 1

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        process_handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, supervisor_pid)
        assert process_handle
        try:
            assert kernel32.TerminateProcess(process_handle, 99)
            assert kernel32.WaitForSingleObject(process_handle, 5000) == WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(process_handle)
        supervisor.wait(timeout=5)

        wait_deadline = time.monotonic() + 8
        while time.monotonic() < wait_deadline and any(_pid_running(pid) for pid in tree_before - {supervisor_pid}):
            time.sleep(0.05)
        assert not any(_pid_running(pid) for pid in tree_before - {supervisor_pid})
        assert not (output_root / config["run_id"]).exists()
        recovered = recover_incomplete_attempts(output_root)
        assert len(recovered) == 1
        failed = recovered[0]
        recovered_status = json.loads((failed / "status.json").read_text(encoding="utf-8"))
        assert recovered_status["status"] == "supervisor_lost"
        assert recovered_status["worker_started"] is True
        assert recovered_status["last_job_active_processes_observed"] >= 1
        recorded_job_pids = set(recovered_status["last_job_process_ids_observed"])
        process_deadline = time.monotonic() + 8
        while time.monotonic() < process_deadline and any(_pid_running(pid) for pid in recorded_job_pids):
            time.sleep(0.05)
        assert not any(_pid_running(pid) for pid in recorded_job_pids)
        assert not (failed / "READY").exists()
        assert not (output_root / "latest").exists()
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="B0 publication lifecycle uses Windows Job Objects")
def test_pending_ready_is_not_consumable_and_parent_exit_before_directory_commit_recovers(case_directory: Path) -> None:
    config_path = _make_case(case_directory / "case", run_id="publish-window")
    config = _config(config_path)
    state_path = case_directory / "precommit-state.json"
    pid_path = case_directory / "publisher.pid"
    helper = case_directory / "publisher_helper.py"
    helper.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "sys.path.insert(0, " + repr(str(ROOT)) + ")\n"
        "from hydrodynamics.restricted_worker import run_restricted_preparation\n"
        "Path(" + repr(str(pid_path)) + ").write_text(str(os.getpid()))\n"
        "def hold(stage):\n"
        " Path(" + repr(str(state_path)) + ").write_text(json.dumps({'stage':str(stage),'ready':(stage/'READY').is_file()}))\n"
        " time.sleep(30)\n"
        "run_restricted_preparation(Path(" + repr(str(config_path)) + "), project_root=Path(" + repr(str(ROOT)) + "), _before_directory_commit=hold)\n",
        encoding="utf-8",
    )
    publisher = subprocess.Popen([sys.executable, str(helper)], cwd=ROOT,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not state_path.is_file():
            time.sleep(0.05)
        assert state_path.is_file()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        stage = Path(state["stage"])
        run_id = config["run_id"]
        output_root = ROOT / config["output_root"]
        assert state["ready"] is True
        assert (stage / "READY").is_file()
        assert not (output_root / run_id).exists()
        with pytest.raises(PreparedTerrainError, match="pending attempt"):
            read_prepared_terrain(stage)
        pending_status = json.loads((stage / "status.json").read_text(encoding="utf-8"))
        assert pending_status["worker"]["job_active_processes_at_exit"] == 0

        publisher_pid = int(pid_path.read_text(encoding="utf-8"))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        process = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, publisher_pid)
        assert process
        try:
            assert kernel32.TerminateProcess(process, 98)
            assert kernel32.WaitForSingleObject(process, 5000) == WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(process)
        publisher.wait(timeout=5)
        assert not (output_root / run_id).exists()
        recovered = recover_incomplete_attempts(output_root)
        assert len(recovered) == 1
        recovered_status = json.loads((recovered[0] / "status.json").read_text(encoding="utf-8"))
        assert recovered_status["status"] == "supervisor_lost"
        assert not (recovered[0] / "READY").exists()
        assert not (recovered[0] / "READY.pending").exists()
    finally:
        if publisher.poll() is None:
            publisher.terminate()
            publisher.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="B0 birth-window closure uses Windows Job Objects")
def test_create_time_job_assignment_survives_supervisor_exit_before_resume(case_directory: Path) -> None:
    config_path = _make_case(case_directory / "case", run_id="create-time-job-list")
    state_path = case_directory / "created-suspended.json"
    supervisor_pid_path = case_directory / "supervisor.pid"
    helper_path = case_directory / "hold-before-resume.py"
    helper_path.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "sys.path.insert(0, " + repr(str(ROOT)) + ")\n"
        "from hydrodynamics.restricted_worker import run_restricted_preparation\n"
        "Path(" + repr(str(supervisor_pid_path)) + ").write_text(str(os.getpid()))\n"
        "def hold(info):\n"
        " Path(" + repr(str(state_path)) + ").write_text(json.dumps(dict(info)))\n"
        " time.sleep(30)\n"
        "run_restricted_preparation(Path(" + repr(str(config_path)) + "), project_root=Path(" + repr(str(ROOT)) + "), _on_worker_created_suspended=hold)\n",
        encoding="utf-8",
    )
    supervisor = subprocess.Popen([sys.executable, str(helper_path)], cwd=ROOT,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, close_fds=True)
    child_handle = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not state_path.is_file():
            time.sleep(0.05)
        assert state_path.is_file()
        created = json.loads(state_path.read_text(encoding="utf-8"))
        assert created["job_list_assigned_at_create"] is True
        assert created["process_in_job_before_resume"] is True
        assert created["initial_thread_suspended"] is True
        child_pid = int(created["process_id"])
        supervisor_pid = int(supervisor_pid_path.read_text(encoding="utf-8"))

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        child_handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, child_pid)
        assert child_handle
        assert kernel32.WaitForSingleObject(child_handle, 0) == WAIT_TIMEOUT
        supervisor_handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, supervisor_pid)
        assert supervisor_handle
        try:
            assert kernel32.TerminateProcess(supervisor_handle, 97)
            assert kernel32.WaitForSingleObject(supervisor_handle, 5000) == WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(supervisor_handle)
        supervisor.wait(timeout=5)
        assert kernel32.WaitForSingleObject(child_handle, 5000) == WAIT_OBJECT_0

        output_root = ROOT / _config(config_path)["output_root"]
        stages = list(output_root.glob(".*.pending"))
        assert len(stages) == 1
        stage = stages[0]
        assert not (stage / "READY").exists()
        assert not (output_root / "create-time-job-list").exists()
        recovered = recover_incomplete_attempts(output_root)
        assert len(recovered) == 1
        recovered_status = json.loads((recovered[0] / "status.json").read_text(encoding="utf-8"))
        assert recovered_status["status"] == "supervisor_lost"
        assert recovered_status["worker_created_suspended"] is True
        assert recovered_status["worker_started"] is False
        assert not (recovered[0] / "READY").exists()
    finally:
        if child_handle:
            kernel32.CloseHandle(child_handle)
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=5)
