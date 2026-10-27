from __future__ import annotations

import ctypes
import copy
from collections import Counter
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from ctypes import wintypes
from typing import Any, Mapping

import pytest

from hydrodynamics.prepared_terrain import PreparedTerrainError, read_prepared_terrain
import hydrodynamics.restricted_worker as restricted_worker
from hydrodynamics.restricted_worker import RestrictedWorkerError, run_restricted_preparation


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "examples/triangular_mesh/run_b1_synthetic_pilot.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("b1_synthetic_pilot_example", RUNNER_PATH)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
pilot_example = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(pilot_example)
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0
R1_EVIDENCE_ENV = "B1_P0G0_R1_EVIDENCE_DIR"


def _write_optional_r1_evidence(name: str, value: Any) -> None:
    destination = os.environ.get(R1_EVIDENCE_ENV)
    if not destination:
        return
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                             encoding="utf-8")


def _open_owned_process_handle(pid: int) -> tuple[Any, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return kernel32, handle


@pytest.fixture
def experiment_root():
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    temp = tempfile.TemporaryDirectory(prefix="b1-pilot-test-", dir=work)
    path = Path(temp.name) / "experiment"
    path.mkdir()
    (path / "inputs").mkdir()
    try:
        yield path
    finally:
        temp.cleanup()


def _p0_config(experiment: Path, *, face_count: int, pattern: str, repeat: int = 1) -> tuple[Path, dict[str, Any], Path, Path]:
    inputs = experiment / "inputs"
    raster_path = inputs / f"source_{pattern}.tif"
    if not raster_path.exists():
        pilot_example._write_source_raster(raster_path, masked=(pattern != "planar_all_valid"))
    mesh_path = inputs / f"mesh_{face_count}.json"
    if not mesh_path.exists():
        pilot_example._write_json(mesh_path, pilot_example._make_mesh(face_count, seed=pilot_example.SEED))
    run_id = f"test-p0-{pattern.removeprefix('planar_')}-f{face_count}-r{repeat}"
    case = {"case_id": run_id, "seed": pilot_example.SEED, "face_count": face_count,
            "face_size_m": 50.0, "repeat_index": repeat, "source_pattern": pattern}
    config_path = pilot_example._worker_config(
        ROOT, experiment, run_id=run_id, action="p0_windowed_mapping",
        raster_path=raster_path, mesh_path=mesh_path, mesh_generation=None, pilot_case=case,
    )
    return config_path, json.loads(config_path.read_text(encoding="utf-8")), raster_path, mesh_path


def _g0_config(experiment: Path, *, recipe_mutator: Any | None = None,
               minimum_face_quality: float = 0.1, project_root: Path = ROOT) -> tuple[Path, dict[str, Any], Path, Path]:
    inputs = experiment / "inputs"
    raster_path = inputs / "source_planar_all_valid.tif"
    if not raster_path.exists():
        pilot_example._write_source_raster(raster_path, masked=False)
    recipe_path = inputs / "gmsh_recipe.json"
    recipe = json.loads((ROOT / "examples/triangular_mesh/b1_synthetic_gmsh_recipe.example.json").read_text(encoding="utf-8"))
    if recipe_mutator is not None:
        recipe_mutator(recipe)
    pilot_example._write_json(recipe_path, recipe)
    runtime = pilot_example._dependency_identity()
    roles = {
        "outer_boundary": "synthetic_D",
        "holes": {f"hole_{i}": "synthetic_safety_area_exclusion" for i, _ in enumerate(recipe["gmsh_config"]["holes"])},
        "constraints": {group["name"]: "synthetic_constraint_line" for group in recipe["gmsh_config"]["constraints"]},
    }
    generation = {
        "format": "triangle-b1-gmsh-generation-v1", "maximum_faces": 500_000,
        "maximum_nodes": 2_000_000, "minimum_face_quality": minimum_face_quality,
        "area_abs_tolerance_m2": 1e-7, "area_rel_tolerance": 1e-9,
        "expected_gmsh_version": runtime["expected_gmsh_version"],
        "expected_gmsh_library_sha256": runtime["expected_gmsh_library_sha256"],
        "area_roles": roles,
    }
    run_id = f"test-g0-q{int(minimum_face_quality * 100)}"
    config_path = pilot_example._worker_config(
        project_root, experiment, run_id=run_id, action="g0_gmsh_windowed_mapping",
        raster_path=raster_path, mesh_path=recipe_path, mesh_generation=generation, pilot_case=None,
    )
    return config_path, json.loads(config_path.read_text(encoding="utf-8")), raster_path, recipe_path


def _assert_failed_attempt(result: Any, output_root: Path, *, exit_code: int,
                           expected_status: str = "worker_failed") -> None:
    assert result.status == expected_status, result
    assert result.exit_code == exit_code
    assert result.worker_started is True
    assert result.diagnostic_path is not None
    status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
    assert status["worker_started"] is result.worker_started
    assert status["exit_code"] == exit_code
    assert not (result.diagnostic_path / "READY").exists()
    assert not (result.diagnostic_path / "READY.pending").exists()
    assert not (output_root / result.run_id).exists()
    assert not list(output_root.glob(".*.pending"))
    assert result.worker_receipt is not None
    assert result.worker_receipt["job_active_processes_at_exit"] == 0
    assert not (output_root / "latest").exists()


def _write_canonical_json(path: Path, value: Any) -> bytes:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return payload


def _file_record(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {"name": path.name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _bundle_tree_record(bundle: Path) -> dict[str, Any]:
    files = []
    for path in sorted(item for item in bundle.rglob("*") if item.is_file()):
        payload = path.read_bytes()
        files.append({"path": path.relative_to(bundle).as_posix(), "bytes": len(payload),
                      "sha256": hashlib.sha256(payload).hexdigest()})
    digest = hashlib.sha256()
    for item in files:
        digest.update(item["path"].encode("utf-8") + b"\0" + bytes.fromhex(item["sha256"]) + b"\n")
    return {"file_count": len(files), "bytes": sum(item["bytes"] for item in files),
            "tree_sha256": digest.hexdigest(), "files": files}


def _make_resigned_g0_probe(destination: Path, case: str) -> Path:
    """Mutate a successful bundle at its configured output path and resign every enclosing identity."""
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    body_path = destination / "artifacts" / "prepared_body.json"
    body = json.loads(body_path.read_text(encoding="utf-8"))
    pilot = body["synthetic_pilot"]
    generation = pilot["mesh_generation"]
    if case == "constraint_wrong_edges":
        mesh_path = destination / "artifacts" / "gmsh_mesh.json"
        mesh = json.loads(mesh_path.read_text(encoding="utf-8"))
        edge_counts = Counter(tuple(sorted((face[index], face[(index + 1) % 3])))
                              for face in mesh["face_nodes"] for index in range(3))
        existing = {tuple(sorted(edge)) for edge in mesh["constraint_groups"]["synthetic_constraint"]}
        substituted = [edge for edge, count in edge_counts.items() if count == 2 and edge not in existing][:2]
        assert len(substituted) == 2
        mesh["constraint_groups"]["synthetic_constraint"] = [list(edge) for edge in substituted]
        mesh_bytes = _write_canonical_json(mesh_path, mesh)
        generation["generated_mesh"]["sha256"] = hashlib.sha256(mesh_bytes).hexdigest()
        generation["generated_mesh"]["bytes"] = len(mesh_bytes)
    elif case == "false_area_residual":
        generation["area_audit"]["difference_m2"] = 0.5
        generation["area_audit"]["tolerance_m2"] = 1.0
    elif case == "false_area_tolerance":
        generation["area_audit"]["tolerance_m2"] = 1.0
    elif case == "false_semantic_roles":
        generation["area_roles"] = {
            "outer_boundary": "wrong_outer",
            "holes": {"hole_0": "not_safety_area"},
            "constraints": {"synthetic_constraint": "wrong_constraint"},
        }
    else:
        raise AssertionError(f"unsupported resigned probe: {case}")

    # Copying a bundle creates a new file identity even when its bytes are
    # unchanged; bind that test copy before resigning the enclosing JSON files.
    body["mesh"]["stat_identity"] = restricted_worker._stat_identity(
        destination / "artifacts" / "gmsh_mapping_mesh.json")
    manifest["mesh"] = body["mesh"]
    manifest["synthetic_pilot"] = pilot
    body_bytes = _write_canonical_json(body_path, body)
    body_record = {"name": body_path.name, "bytes": len(body_bytes),
                   "sha256": hashlib.sha256(body_bytes).hexdigest()}
    manifest["prepared_body_file"] = body_record
    manifest["files"]["prepared_body_file"] = body_record
    worker_path = destination / "artifacts" / "worker-result.json"
    worker_result = json.loads(worker_path.read_text(encoding="utf-8"))
    worker_result["pilot"] = pilot
    worker_result["prepared_files"]["prepared_body_file"] = body_record
    _write_canonical_json(worker_path, worker_result)
    manifest["files"]["worker_result_file"] = _file_record(worker_path)
    _write_canonical_json(manifest_path, manifest)
    ready_path = destination / "READY"
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    ready["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    _write_canonical_json(ready_path, ready)
    return destination


@pytest.mark.skipif(os.name != "nt", reason="the pilot contract requires the Windows Job Object")
def test_pilot_schemas_and_examples_are_strict_and_b0_v1_remains_closed() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = ROOT / "examples/triangular_mesh/b1_synthetic_pilot_config.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    for name in ("b1_p0_synthetic_pilot_config.example.json", "b1_g0_synthetic_pilot_config.example.json"):
        validator.validate(json.loads((schema_path.parent / name).read_text(encoding="utf-8")))
    recipe_schema = json.loads((ROOT / "examples/triangular_mesh/b1_synthetic_gmsh_recipe.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(recipe_schema)
    recipe_schema_validator = jsonschema.Draft202012Validator(recipe_schema)
    recipe_schema_validator.validate(json.loads((schema_path.parent / "b1_synthetic_gmsh_recipe.example.json").read_text(encoding="utf-8")))

    b0 = json.loads((ROOT / "examples/triangular_mesh/restricted_worker_config.example.json").read_text(encoding="utf-8"))
    b0["pilot_action"] = "g0_gmsh_windowed_mapping"
    with pytest.raises(RestrictedWorkerError, match="unknown config fields"):
        restricted_worker.validate_config_shape(b0)
    with pytest.raises(RestrictedWorkerError, match="unknown"):
        restricted_worker.validate_route_config_shape({**b0, "format": restricted_worker.FORMAT})
    invalid = json.loads((schema_path.parent / "b1_p0_synthetic_pilot_config.example.json").read_text(encoding="utf-8"))
    invalid["resources"]["job_memory_limit_mib"] = 4096
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    with pytest.raises(RestrictedWorkerError, match="frozen"):
        restricted_worker.validate_route_config_shape(invalid)


@pytest.mark.skipif(os.name != "nt", reason="the pilot validation contract is exercised with the Windows worker environment")
def test_pilot_schema_and_runtime_share_closed_boundary_matrix() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    example_root = ROOT / "examples/triangular_mesh"
    schema = json.loads((example_root / "b1_synthetic_pilot_config.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    p0_base = json.loads((example_root / "b1_p0_synthetic_pilot_config.example.json").read_text(encoding="utf-8"))
    g0_base = json.loads((example_root / "b1_g0_synthetic_pilot_config.example.json").read_text(encoding="utf-8"))

    cases: list[tuple[str, tuple[str, ...], Any, bool]] = []
    cases.extend(("p0", ("pilot_case", "case_id"), value, accepted) for value, accepted in [
        ("x" * 64, True), ("x" * 65, False), ("", False), (None, False),
    ])
    cases.extend(("p0", ("pilot_case", "face_size_m"), value, accepted) for value, accepted in [
        (50, True), (50.0, True), (49.99999999, False), (50.00000001, False), (True, False), ("50", False),
    ])
    cases.extend(("g0", ("mesh_generation", "area_abs_tolerance_m2"), value, accepted) for value, accepted in [
        (1e-12, True), (1.0, True), (1e-13, False), (math.nextafter(1e-12, 0.0), False),
        (math.nextafter(1.0, math.inf), False), (0.0, False), (True, False), ("1e-7", False),
    ])
    cases.extend(("g0", ("mesh_generation", "area_rel_tolerance"), value, accepted) for value, accepted in [
        (1e-12, True), (1e-3, True), (1e-13, False), (math.nextafter(1e-12, 0.0), False),
        (math.nextafter(1e-3, math.inf), False), (0.0, False), (True, False), ("1e-9", False),
    ])
    cases.extend(("g0", ("mesh_generation", "area_roles", "holes"), value, accepted) for value, accepted in [
        ({"hole_0": "synthetic_safety_area_exclusion"}, True), ({"unexpected": "x"}, False),
    ])
    cases.extend(("g0", ("mesh_generation", "area_roles", "outer_boundary"), value, accepted) for value, accepted in [
        ("synthetic_D", True), (" ", False), ("", False), (None, False),
    ])
    cases.extend(("g0", ("mesh_generation", "area_roles", "holes"), {"hole_0": value}, accepted)
                 for value, accepted in [("role", True), (" ", False), ("", False), (1, False)])
    cases.extend(("g0", ("mesh_generation", "expected_gmsh_version"), value, accepted) for value, accepted in [
        ("4.11.0", True), ("4.15.2", True), ("4.100.0", True),
        ("4.10.99", False), ("4.010.0", False), ("4.15", False),
        ("4.15.2rc1", False), ("5.0.0", False), (" 4.15.2", False),
    ])
    cases.extend(("g0", ("mesh_generation", "expected_gmsh_version"), "4.15.2" + suffix, False)
                 for suffix in ("\n", "\r\n", "\t", " "))
    cases.extend(("g0", ("mesh_generation", "area_roles", "holes"), {"hole_0" + suffix: "role"}, False)
                 for suffix in ("\n", "\r\n", "\t", " "))

    matrix_evidence: list[dict[str, Any]] = []
    for mode, path, value, expected in cases:
        candidate = copy.deepcopy(p0_base if mode == "p0" else g0_base)
        current: Any = candidate
        for key in path[:-1]:
            current = current[key]
        current[path[-1]] = value
        schema_accepts = not list(validator.iter_errors(candidate))
        try:
            restricted_worker.validate_route_config_shape(candidate)
            runtime_accepts = True
        except RestrictedWorkerError:
            runtime_accepts = False
        assert schema_accepts is expected, (mode, path, value, "schema")
        assert runtime_accepts is expected, (mode, path, value, "runtime")
        matrix_evidence.append({"mode": mode, "path": ".".join(path), "value": value,
                                "expected_accept": expected, "schema_accept": schema_accepts,
                                "runtime_accept": runtime_accepts})
    _write_optional_r1_evidence("schema_runtime_matrix.json", matrix_evidence)


def test_g0_geometry_audit_checks_gap_outside_overlap_and_group_coverage() -> None:
    from hydrodynamics.b1_synthetic_pilot import _audit_g0_mesh
    from hydrodynamics.mesh_preparation import GmshMeshConfig

    recipe = GmshMeshConfig.model_validate({
        "type": "gmsh",
        "outer_boundary": [[0, 0], [1, 0], [1, 1], [0, 1]],
        "holes": [], "boundary_groups": [], "constraints": [],
        "default_size_m": 0.5, "minimum_size_m": 0.1,
    })
    generation = {
        "area_abs_tolerance_m2": 1e-12, "area_rel_tolerance": 1e-12,
        "area_roles": {"outer_boundary": "synthetic_D", "holes": {}, "constraints": {}},
    }
    nodes = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    edges = ((0, 1), (1, 2), (2, 3), (0, 3), (0, 2))

    def mesh(faces: tuple[tuple[int, int, int], ...], areas: tuple[float, ...], *,
             node_xy: tuple[tuple[float, float], ...] = nodes,
             boundary_groups: dict[str, tuple[int, ...]] | None = None) -> Any:
        return SimpleNamespace(
            node_xy=node_xy, face_nodes=faces, face_area=areas, edge_nodes=edges,
            geometry_tolerance_m=1e-8,
            boundary_groups={"outer_boundary": (0, 1, 2, 3)} if boundary_groups is None else boundary_groups,
            constraint_groups={},
        )

    valid = mesh(((0, 1, 2), (0, 2, 3)), (0.5, 0.5))
    valid_audit = _audit_g0_mesh(valid, recipe, generation)
    tolerance = valid_audit["area_audit"]["tolerance_m2"]
    assert valid_audit["area_audit"]["coverage_gap_m2"] <= tolerance
    assert valid_audit["area_audit"]["outside_domain_m2"] <= tolerance
    assert valid_audit["area_audit"]["overlap_m2"] <= tolerance

    cases: list[dict[str, Any]] = [{"case": "valid", "accepted": True,
                                    "area_audit": valid_audit["area_audit"]}]
    invalid_meshes = {
        "gap": mesh(((0, 1, 2),), (0.5,)),
        "outside": mesh(((0, 1, 2), (0, 2, 3), (4, 5, 6)), (0.5, 0.5, 0.5),
                        node_xy=nodes + ((2.0, 2.0), (3.0, 2.0), (2.0, 3.0))),
        "overlap": mesh(((0, 1, 2), (0, 2, 3), (0, 1, 2)), (0.5, 0.5, 0.5)),
        "missing_boundary_group": mesh(((0, 1, 2), (0, 2, 3)), (0.5, 0.5), boundary_groups={}),
        "extra_boundary_group": mesh(((0, 1, 2), (0, 2, 3)), (0.5, 0.5),
                                     boundary_groups={"outer_boundary": (0, 1, 2, 3), "unexpected": (0,)}),
        "boundary_wrong_edge": mesh(((0, 1, 2), (0, 2, 3)), (0.5, 0.5),
                                     boundary_groups={"outer_boundary": (0, 1, 2, 4)}),
    }
    for name, invalid in invalid_meshes.items():
        with pytest.raises(ValueError):
            _audit_g0_mesh(invalid, recipe, generation)
        cases.append({"case": name, "accepted": False})
    _write_optional_r1_evidence("g0_geometry_audit_matrix.json", cases)


@pytest.mark.skipif(os.name != "nt", reason="P0 requires the Windows Job Object worker")
@pytest.mark.parametrize(("face_count", "pattern"), [(2, "planar_all_valid"), (32, "planar_nodata_nonfinite")])
def test_p0_uses_real_window_mapper_and_matches_independent_synthetic_oracle(
        experiment_root: Path, face_count: int, pattern: str) -> None:
    config_path, _config, raster_path, mesh_path = _p0_config(
        experiment_root, face_count=face_count, pattern=pattern)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "preparation_diagnostic_complete", result
    assert result.exit_code == 0 and result.worker_started is True
    assert result.bundle_path is not None
    manifest = json.loads((result.bundle_path / "manifest.json").read_text(encoding="utf-8"))
    status = json.loads((result.bundle_path / "status.json").read_text(encoding="utf-8"))
    pilot = manifest["synthetic_pilot"]
    stats = manifest["mapping"]["statistics"]
    assert manifest["route_version"] == "triangle-b1-p0-windowed-mapping-v1"
    assert manifest["coverage_policy"] == "diagnostic_only"
    assert manifest["preparation_only"] is True and manifest["simulation_result"] is False
    assert manifest["production_approved"] is False and manifest["engineering_ready"] is False
    assert status["worker_started"] is result.worker_started
    assert status["pilot_readback_seconds"] >= 0.0
    assert pilot["case"]["face_count"] == face_count
    assert stats["tiles_enumerated"] == 4
    assert stats["tiles_selected"] == 4
    assert stats["candidate_overlap_checks"] == stats["clip_calls"] > 0
    assert pilot["mapping_profile_counts"]["clip_calls"] == stats["clip_calls"]
    assert pilot["mapping_profile_counts"]["raw_windows_read"] == stats["tiles_selected"]
    assert pilot["mapping_timing_seconds"]["mapper_seconds"] > 0
    assert pilot["mapping_timing_seconds"]["candidate_search_seconds"] > 0
    assert manifest["worker"]["job_memory_limit_bytes"] == 2_048 * 1024 * 1024
    assert manifest["worker"]["wall_clock_limit_s"] == 900.0
    assert manifest["worker"]["output_budget_bytes"] == 1_073_741_824
    assert manifest["worker"]["scratch_budget_bytes"] == 16_777_216
    assert manifest["worker"]["job_active_processes_at_exit"] == 0
    assert not (result.bundle_path / "latest").exists()
    golden = pilot_example._compare_golden(raster_path, mesh_path, result.bundle_path)
    assert golden["maximum_absolute_errors"]["area_abs_m2"] <= pilot_example.GOLDEN_TOLERANCES["area_abs_m2"]
    assert golden["maximum_absolute_errors"]["coverage_abs"] <= pilot_example.GOLDEN_TOLERANCES["coverage_abs"]
    assert golden["maximum_absolute_errors"]["elevation_abs_m"] <= pilot_example.GOLDEN_TOLERANCES["elevation_abs_m"]
    if pattern == "planar_nodata_nonfinite":
        face_rows = list(read_prepared_terrain(result.bundle_path).iter_faces())
        assert any(row["nodata_area_m2"] > 0 for row in face_rows)
        assert any(row["nonfinite_area_m2"] > 0 for row in face_rows)


@pytest.mark.skipif(os.name != "nt", reason="G0 requires the Windows Job Object and native Gmsh SDK")
def test_g0_generates_gmsh_inside_job_and_reads_bound_route_artifacts_without_remapping(
        experiment_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "gmsh" not in sys.modules
    config_path, config, _raster_path, _recipe_path = _g0_config(experiment_root)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "preparation_diagnostic_complete", result
    assert result.exit_code == 0 and result.worker_started is True
    assert result.bundle_path is not None
    manifest = json.loads((result.bundle_path / "manifest.json").read_text(encoding="utf-8"))
    status = json.loads((result.bundle_path / "status.json").read_text(encoding="utf-8"))
    pilot = manifest["synthetic_pilot"]
    generation = pilot["mesh_generation"]
    worker = manifest["worker"]
    assert manifest["route_version"] == "triangle-b1-g0-gmsh-preparation-v1"
    assert manifest["coverage_policy_pending"] is True
    assert manifest["preparation_only"] is True and manifest["simulation_result"] is False
    assert status["worker_started"] is result.worker_started
    assert status["pilot_readback_seconds"] >= 0.0
    assert generation["process_in_job_before_gmsh"] is True
    assert generation["gmsh"]["worker_pid"] in worker["job_process_ids_observed"]
    assert generation["gmsh"]["python_binding_loaded_in_worker"] is True
    assert generation["gmsh"]["native_library_loaded_in_worker"] is True
    assert generation["gmsh"]["python_binding_version"] == config["mesh_generation"]["expected_gmsh_version"]
    assert generation["gmsh"]["native_library_sha256"] == config["mesh_generation"]["expected_gmsh_library_sha256"]
    assert generation["mesh_quality"]["faces_below_quality_threshold"] == []
    assert generation["area_audit"]["maximum_face_hole_intersection_m2"] <= config["mesh_generation"]["area_abs_tolerance_m2"]
    assert abs(generation["area_audit"]["difference_m2"]) <= generation["area_audit"]["tolerance_m2"]
    assert generation["area_audit"]["coverage_gap_m2"] <= generation["area_audit"]["tolerance_m2"]
    assert generation["area_audit"]["outside_domain_m2"] <= generation["area_audit"]["tolerance_m2"]
    assert generation["area_audit"]["overlap_m2"] <= generation["area_audit"]["tolerance_m2"]
    assert generation["area_roles"] == config["mesh_generation"]["area_roles"]
    assert set(generation["line_coverage_audit"]["boundary_groups"]) == {
        "outer_boundary", "hole_0", "synthetic_D_boundary", "synthetic_S_boundary",
    }
    assert set(generation["line_coverage_audit"]["constraint_groups"]) == {"synthetic_constraint"}
    assert all(metric["gap_length_m"] <= 1e-8
               for groups in generation["line_coverage_audit"].values() for metric in groups.values())
    assert worker["job_active_processes_at_exit"] == 0
    assert not (result.bundle_path / "scratch").exists()
    assert not (result.bundle_path / "latest").exists()
    assert all((result.bundle_path / "artifacts" / name).is_file()
               for name in ("gmsh_mesh.json", "gmsh_mapping_mesh.json", "g0_source_manifest.json"))
    assert "gmsh" not in sys.modules

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("G0 readback must use saved artifacts; it cannot reopen the DEM or remap")

    import rasterio
    import hydrodynamics.terrain_windowed_mapping as mapper
    monkeypatch.setattr(rasterio, "open", forbidden)
    monkeypatch.setattr(mapper, "map_triangular_mesh_from_windows", forbidden)
    prepared = read_prepared_terrain(result.bundle_path, pilot_config=config, project_root=ROOT)
    from hydrodynamics.b1_synthetic_pilot import validate_pilot_prepared_bundle
    checked = validate_pilot_prepared_bundle(result.bundle_path, prepared.manifest, config, ROOT)
    assert checked["mesh_face_count"] == manifest["mesh"]["face_count"]
    assert checked["area_audit"] == generation["area_audit"]
    assert checked["line_coverage_audit"] == generation["line_coverage_audit"]

    pristine_bundle = experiment_root / "pristine" / result.bundle_path.name
    pristine_bundle.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(result.bundle_path, pristine_bundle)
    resigned_evidence: list[dict[str, Any]] = []
    for failure in ("constraint_wrong_edges", "false_area_residual", "false_area_tolerance", "false_semantic_roles"):
        if result.bundle_path.exists():
            shutil.rmtree(result.bundle_path)
        shutil.copytree(pristine_bundle, result.bundle_path)
        resigned = _make_resigned_g0_probe(result.bundle_path, failure)
        generic = read_prepared_terrain(resigned)
        assert generic.manifest["run_id"] == config["run_id"]
        with pytest.raises(PreparedTerrainError) as standard_error:
            read_prepared_terrain(resigned, pilot_config=config, project_root=ROOT)
        with pytest.raises(PreparedTerrainError) as route_error:
            validate_pilot_prepared_bundle(resigned, generic.manifest, config, ROOT)
        bundle_record = _bundle_tree_record(resigned)
        evidence_root = os.environ.get(R1_EVIDENCE_ENV)
        if evidence_root:
            preserved_bundle = Path(evidence_root) / "resigned_reader_bundles" / failure / resigned.name
            preserved_bundle.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(resigned, preserved_bundle)
        resigned_evidence.append({
            "case": failure,
            "generic_identity_reader_accept": True,
            "standard_reader_with_trusted_pilot_config_reject": True,
            "standard_reader_error": str(standard_error.value),
            "route_reader_reject": True,
            "route_reader_error": str(route_error.value),
            "body_worker_result_manifest_ready_resigned": True,
            "dem_opened_or_remapping_performed": False,
            "bundle_evidence": bundle_record,
            "preserved_bundle_relative_path": (f"resigned_reader_bundles/{failure}/{resigned.name}"
                                                if evidence_root else None),
        })
    _write_optional_r1_evidence("resigned_reader_evidence.json", resigned_evidence)


@pytest.mark.skipif(os.name != "nt", reason="G0 failure containment requires Windows Job Objects")
@pytest.mark.parametrize(("failure", "expected_message"), [
    ("geometry", "outer_boundary self-intersects"),
    ("quality", "below minimum_face_quality"),
])
def test_g0_geometry_or_quality_refusal_has_no_ready_and_cleans_job(
        experiment_root: Path, failure: str, expected_message: str) -> None:
    def make_invalid_recipe(recipe: dict[str, Any]) -> None:
        if failure == "geometry":
            recipe["gmsh_config"]["outer_boundary"] = [
                [473506.0, 4335888.0], [473606.0, 4335988.0],
                [473506.0, 4335988.0], [473581.0, 4335888.0],
            ]

    config_path, config, _raster_path, _recipe_path = _g0_config(
        experiment_root, recipe_mutator=make_invalid_recipe if failure == "geometry" else None,
        minimum_face_quality=1.0 if failure == "quality" else 0.1)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    _assert_failed_attempt(result, ROOT / config["output_root"], exit_code=53)
    assert expected_message in result.message


@pytest.mark.skipif(os.name != "nt", reason="G0 dependency containment requires Windows Job Objects")
def test_g0_missing_native_dll_fails_inside_job_without_ready(
        experiment_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolated = experiment_root / "isolated_project"
    isolated.mkdir()
    shutil.copytree(ROOT / "hydrodynamics", isolated / "hydrodynamics",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (isolated / "docs").mkdir()
    shutil.copyfile(ROOT / "docs/external_evidence_contract.md", isolated / "docs/external_evidence_contract.md")
    (isolated / "examples/triangular_mesh").mkdir(parents=True)
    shutil.copyfile(ROOT / "examples/triangular_mesh/restricted_worker_config.example.json",
                    isolated / "examples/triangular_mesh/restricted_worker_config.example.json")
    (isolated / "gmsh.py").write_text("raise ImportError('test-only missing Gmsh binding')\n", encoding="utf-8")
    isolated_experiment = isolated / "work" / "dependency-load-failure"
    isolated_experiment.mkdir(parents=True)
    (isolated_experiment / "inputs").mkdir()
    config_path, config, _raster_path, _recipe_path = _g0_config(isolated_experiment, project_root=isolated)
    native_library = Path(pilot_example._dependency_identity()["native_library_path_for_diagnostics"])
    native_parent = str(native_library.parent.resolve())
    path_entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep)
                    if entry and Path(entry).resolve() != Path(native_parent)]
    monkeypatch.setenv("PATH", os.pathsep.join(path_entries))
    assert "gmsh" not in sys.modules
    result = run_restricted_preparation(config_path, project_root=isolated)
    output_root = isolated / config["output_root"]
    _assert_failed_attempt(result, output_root, exit_code=53)
    assert "native SDK DLL is unavailable" in result.message
    assert "gmsh" not in sys.modules


@pytest.mark.skipif(os.name != "nt", reason="G0 cancellation requires the Windows Job Object")
def test_g0_cancel_after_native_mesh_generation_has_no_ready_and_cleans_job(
        experiment_root: Path) -> None:
    config_path, config, _raster_path, _recipe_path = _g0_config(experiment_root)
    output_root = ROOT / config["output_root"]
    cancel = threading.Event()
    held: dict[int, tuple[Any, Any]] = {}
    observed_marker: dict[str, Path] = {}
    monitor_errors: list[str] = []
    monitor_thread: threading.Thread | None = None

    def hold_created_process_and_watch(info: Mapping[str, Any]) -> None:
        nonlocal monitor_thread
        pid = int(info["process_id"])
        kernel32, handle = _open_owned_process_handle(pid)
        assert handle
        held[pid] = (kernel32, handle)

        def monitor_gmsh_completion() -> None:
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    stages = list(output_root.glob(".*.pending"))
                    for stage in stages:
                        marker = stage / "scratch" / "gmsh-generated.json"
                        if marker.is_file():
                            observed_marker["path"] = marker
                            record = json.loads(marker.read_text(encoding="utf-8"))
                            assert record["native_library_loaded_in_worker"] is True
                            cancel.set()
                            return
                    time.sleep(0.01)
                monitor_errors.append("G0 worker did not reach its post-Gmsh marker before timeout")
            except Exception as exc:
                monitor_errors.append(f"{type(exc).__name__}: {exc}")

        monitor_thread = threading.Thread(target=monitor_gmsh_completion, daemon=True)
        monitor_thread.start()

    try:
        result = run_restricted_preparation(config_path, project_root=ROOT, cancel_event=cancel,
                                            _on_worker_created_suspended=hold_created_process_and_watch)
        if monitor_thread is not None:
            monitor_thread.join(timeout=2)
        assert cancel.is_set() and observed_marker, {"result": result, "monitor_errors": monitor_errors}
        assert not monitor_errors, monitor_errors
        assert result.status == "cancelled"
        assert result.exit_code == 44
        assert result.worker_started is True
        assert result.worker_receipt is not None
        assert result.worker_receipt["job_active_processes_at_exit"] == 0
        assert result.diagnostic_path is not None
        status = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
        assert status["worker_started"] is result.worker_started
        assert not (result.diagnostic_path / "READY").exists()
        assert not (result.diagnostic_path / "READY.pending").exists()
        assert not list(output_root.glob(".*.pending"))
        assert not (output_root / "latest").exists()
        assert held
        assert all(kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0
                   for kernel32, handle in held.values())
    finally:
        for kernel32, handle in held.values():
            kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="the B0 compatibility route uses the Windows Job Object")
def test_b0_v1_worker_still_publishes_normal_synthetic_case_and_rejects_unknown_field(
        experiment_root: Path) -> None:
    config_path, config, _raster_path, _mesh_path = _p0_config(
        experiment_root, face_count=2, pattern="planar_all_valid")
    config["format"] = restricted_worker.FORMAT
    config["run_id"] = "b0-v1-pilot-compat"
    config.pop("action")
    config.pop("pilot_case")
    config.pop("mesh_generation")
    config["output_root"] = (experiment_root / "prepared" / "b0-v1-compat").relative_to(ROOT).as_posix()
    config_path = experiment_root / "configs" / "b0-v1-compat.json"
    pilot_example._write_json(config_path, config)
    with pytest.raises(RestrictedWorkerError, match="unknown config fields"):
        restricted_worker.validate_config_shape({**config, "pilot_action": "g0_gmsh_windowed_mapping"})
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "preparation_diagnostic_complete", result
    assert result.exit_code == 0 and result.worker_started is True
    assert result.bundle_path is not None
    manifest = json.loads((result.bundle_path / "manifest.json").read_text(encoding="utf-8"))
    status = json.loads((result.bundle_path / "status.json").read_text(encoding="utf-8"))
    assert manifest["route_version"] == restricted_worker.ROUTE_VERSION
    assert manifest["coverage_policy"] == "diagnostic_only"
    assert manifest["preparation_only"] is True and manifest["simulation_result"] is False
    assert status["worker_started"] is result.worker_started
    assert not (result.bundle_path / "latest").exists()
