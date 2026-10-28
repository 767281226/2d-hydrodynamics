from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
from typing import Any, Mapping

import pytest

ROOT = Path(__file__).resolve().parents[1]
if "hydrodynamics" not in sys.modules:
    _package = types.ModuleType("hydrodynamics")
    _package.__path__ = [str(ROOT / "hydrodynamics")]
    _package.__package__ = "hydrodynamics"
    _package.__spec__ = importlib.machinery.ModuleSpec("hydrodynamics", loader=None, is_package=True)
    _package.__spec__.submodule_search_locations = _package.__path__
    sys.modules["hydrodynamics"] = _package

import hydrodynamics.restricted_worker as restricted_worker
from hydrodynamics.domain_mesh_only import _component_file_records_v11
from hydrodynamics.restricted_worker import run_restricted_preparation
from hydrodynamics.b1_synthetic_pilot import _stable_ring_area

RUNNER_PATH = ROOT / "examples" / "triangular_mesh" / "run_domain_mesh_only_synthetic.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("domain_mesh_r1_fix_example", RUNNER_PATH)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
mesh_example = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(mesh_example)


@pytest.fixture
def experiment_root() -> Path:
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="domain-mesh-r1-fix-", dir=work) as temp:
        yield Path(temp) / "experiment"


def _create_case(experiment_root: Path, *, run_id: str, **kwargs: Any):
    return mesh_example.create_synthetic_domain_mesh_case(
        ROOT, experiment_root.relative_to(ROOT), run_id=run_id,
        derived_domain_exterior=True, include_constraint=False, **kwargs,
    )


def _refresh_manifest_and_config(
    config_path: Path, config: dict[str, Any], manifest_path: Path, manifest: dict[str, Any]
) -> None:
    manifest_bytes = mesh_example._write_json(manifest_path, manifest)
    config["source_manifest"]["sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    config["source_manifest"]["expected_stat_identity"] = restricted_worker._stat_identity(manifest_path)
    mesh_example._write_json(config_path, config)


def _refresh_D_sidecars(manifest_path: Path, component: dict[str, Any]) -> None:
    for record in component["files"].values():
        original = manifest_path.parent / record["origin"]["path"]
        staged = manifest_path.parent / record["path"]
        staged.write_bytes(original.read_bytes())
        record["origin"]["sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
        record["origin"]["expected_stat_identity"] = restricted_worker._stat_identity(original)
        record["sha256"] = hashlib.sha256(staged.read_bytes()).hexdigest()
        record["expected_stat_identity"] = restricted_worker._stat_identity(staged)


def _write_D_dimension_shape(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    shape_type: int,
) -> None:
    import shapefile

    component = next(item for item in manifest["components"] if item["role"] == "domain_D")
    shp_record = component["files"]["shp"]
    original_shp = manifest_path.parent / shp_record["origin"]["path"]
    points_xy = [list(point) for point in mesh_example.OUTER[::-1]]
    if shape_type == shapefile.POLYGONZ:
        points = [[x, y, 17.0] for x, y in points_xy]
    elif shape_type == shapefile.POLYGONM:
        points = [[x, y, 42.0] for x, y in points_xy]
    else:
        raise AssertionError(shape_type)
    writer = shapefile.Writer(str(original_shp), shapeType=shape_type, encoding="utf-8")
    writer.field("source", "C", size=32)
    if shape_type == shapefile.POLYGONZ:
        writer.polyz([points])
    else:
        writer.polym([points])
    writer.record("synthetic dimension")
    writer.close()
    _refresh_D_sidecars(manifest_path, component)


def _set_mixed_header_record_types(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    header_type: int,
    record_type: int,
) -> None:
    component = next(item for item in manifest["components"] if item["role"] == "domain_D")
    record = component["files"]["shp"]
    original = manifest_path.parent / record["origin"]["path"]
    staged = manifest_path.parent / record["path"]
    content = bytearray(original.read_bytes())
    struct.pack_into("<i", content, 32, header_type)
    struct.pack_into("<i", content, 108, record_type)
    original.write_bytes(content)
    staged.write_bytes(content)
    _refresh_D_sidecars(manifest_path, component)


def _replace_D_with_xyz_geojson(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    dimension: int,
) -> None:
    component = next(item for item in manifest["components"] if item["role"] == "domain_D")
    for record in component["files"].values():
        (manifest_path.parent / record["origin"]["path"]).unlink()
        (manifest_path.parent / record["path"]).unlink()
    original = manifest_path.parent / "originals" / "domain" / "domain_D.geojson"
    staged = manifest_path.parent / "files" / "domain" / "domain_D.geojson"
    original.parent.mkdir(parents=True, exist_ok=True)
    staged.parent.mkdir(parents=True, exist_ok=True)
    source_ring = list(mesh_example.OUTER[::-1])
    if source_ring and source_ring[0] == source_ring[-1]:
        source_ring.pop()
    coordinates = [
        [float(x), float(y)] if dimension == 2 else [float(x), float(y), 17.0]
        for x, y in source_ring
    ]
    coordinates.append(coordinates[0].copy())
    payload = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4548"}},
        "features": [{
            "type": "Feature", "properties": {"synthetic": True},
            "geometry": {"type": "Polygon", "coordinates": [coordinates]},
        }],
    }
    mesh_example._write_json(original, payload)
    staged.write_bytes(original.read_bytes())
    identity = mesh_example._file_identity(staged, base=manifest_path.parent)
    identity["origin"] = mesh_example._file_identity(original, base=manifest_path.parent)
    component["format"] = "polygon_geojson"
    component["source_crs"] = "EPSG:4548"
    component["files"] = {"geometry": identity}


def _assert_failed_without_ready(
    result: Any, config: Mapping[str, Any], *, status: str, exit_code: int, worker_started: bool,
) -> dict[str, Any]:
    assert result.status == status, result
    assert result.exit_code == exit_code
    assert result.worker_started is worker_started
    output = ROOT / config["output_root"]
    assert not (output / result.run_id).exists()
    assert not (output / "latest").exists()
    diagnostic = result.diagnostic_path
    assert diagnostic is not None
    for marker in ("MESH_READY", "MESH_READY.pending", "READY", "READY.pending", "latest"):
        assert not (diagnostic / marker).exists()
    status_doc = json.loads((diagnostic / "status.json").read_text(encoding="utf-8"))
    assert status_doc["ready"] is False
    if result.worker_receipt is not None:
        assert result.worker_receipt["job_active_processes_at_exit"] == 0
    return status_doc


@pytest.mark.skipif(os.name != "nt", reason="schema 1.1 parent-D dimension proof is checked in the Job worker")
def test_parent_D_polygonZ_with_nonzero_Z_is_rejected_before_interface_flattening(
    experiment_root: Path,
) -> None:
    import shapefile

    config_path, config, manifest_path, manifest = _create_case(
        experiment_root, run_id="r1-parent-D-polygonZ-z17"
    )
    _write_D_dimension_shape(manifest_path, manifest, shape_type=shapefile.POLYGONZ)
    _refresh_manifest_and_config(config_path, config, manifest_path, manifest)

    component = next(item for item in manifest["components"] if item["role"] == "domain_D")
    reader = shapefile.Reader(
        shp=str(manifest_path.parent / component["files"]["shp"]["path"]),
        shx=str(manifest_path.parent / component["files"]["shx"]["path"]),
    )
    try:
        assert reader.dbf is None and reader.shapeType == shapefile.POLYGONZ
        shape = reader.shapes()[0]
        assert set(shape.z) == {17.0}
        first_position = shape.__geo_interface__["coordinates"][0][0]
        assert len(first_position) == 2
    finally:
        reader.close()

    result = run_restricted_preparation(config_path, project_root=ROOT)
    failure = _assert_failed_without_ready(
        result, config, status="worker_failed", exit_code=53, worker_started=True
    )
    assert "UNSUPPORTED_PARENT_D_SHAPE_DIMENSION" in failure["message"]


@pytest.mark.skipif(os.name != "nt", reason="raw SHP header/record checks run in the Job worker")
@pytest.mark.parametrize(("header_type", "record_type"), [(5, 15), (15, 5)])
def test_parent_D_mixed_SHP_header_and_record_types_are_rejected(
    experiment_root: Path, header_type: int, record_type: int,
) -> None:
    config_path, config, manifest_path, manifest = _create_case(
        experiment_root, run_id=f"r1-parent-D-mixed-{header_type}-{record_type}"
    )
    _set_mixed_header_record_types(
        manifest_path, manifest, header_type=header_type, record_type=record_type,
    )
    _refresh_manifest_and_config(config_path, config, manifest_path, manifest)

    result = run_restricted_preparation(config_path, project_root=ROOT)
    failure = _assert_failed_without_ready(
        result, config, status="worker_failed", exit_code=53, worker_started=True
    )
    assert "PARENT_D_SHP_HEADER_RECORD_TYPE_MISMATCH" in failure["message"]


@pytest.mark.skipif(os.name != "nt", reason="raw SHP dimension checks run in the Job worker")
def test_parent_D_polygonM_is_rejected(experiment_root: Path) -> None:
    import shapefile

    config_path, config, manifest_path, manifest = _create_case(
        experiment_root, run_id="r1-parent-D-polygonM"
    )
    _write_D_dimension_shape(manifest_path, manifest, shape_type=shapefile.POLYGONM)
    _refresh_manifest_and_config(config_path, config, manifest_path, manifest)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    failure = _assert_failed_without_ready(
        result, config, status="worker_failed", exit_code=53, worker_started=True
    )
    assert "UNSUPPORTED_PARENT_D_SHAPE_DIMENSION" in failure["message"]


@pytest.mark.skipif(os.name != "nt", reason="raw GeoJSON dimensions are checked in the Job worker")
@pytest.mark.parametrize("dimension", [2, 3])
def test_parent_D_geojson_positions_are_checked_before_shapely(
    experiment_root: Path, dimension: int,
) -> None:
    config_path, config, manifest_path, manifest = _create_case(
        experiment_root, run_id=f"r1-parent-D-geojson-{dimension}d"
    )
    _replace_D_with_xyz_geojson(manifest_path, manifest, dimension=dimension)
    _refresh_manifest_and_config(config_path, config, manifest_path, manifest)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    if dimension == 3:
        failure = _assert_failed_without_ready(
            result, config, status="worker_failed", exit_code=53, worker_started=True
        )
        assert "UNSUPPORTED_PARENT_D_GEOJSON_DIMENSION" in failure["message"]
    else:
        assert result.status == "mesh_generation_diagnostic_complete", result
        assert result.bundle_path is not None and result.worker_receipt is not None
        assert result.worker_receipt["job_active_processes_at_exit"] == 0
        audit = json.loads((result.bundle_path / "artifacts" / "domain-source-audit.json").read_text(encoding="utf-8"))
        d = next(item for item in audit["source_components"] if item["role"] == "domain_D")
        assert d["source_dimension_identity"] == {
            "source_encoding": "geojson", "feature_count": 1,
            "coordinate_position_count": 5, "coordinate_dimension": 2,
        }
        assert (result.bundle_path / "MESH_READY").is_file()


def test_unavailable_file_id_is_typed_and_fails_closed(experiment_root: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    config_path, _config, manifest_path, manifest = _create_case(
        experiment_root, run_id="r1-file-id-unavailable"
    )
    original = restricted_worker._stat_identity

    def no_file_id(path: Path) -> dict[str, int]:
        identity = original(path)
        identity["file_id"] = 0
        return identity

    monkeypatch.setattr(restricted_worker, "_stat_identity", no_file_id)
    with pytest.raises(restricted_worker.SourceIdentityError, match="SOURCE_PHYSICAL_IDENTITY_UNAVAILABLE"):
        _component_file_records_v11(ROOT, manifest_path, manifest, verify_sha=False)


@pytest.mark.skipif(os.name != "nt", reason="physical file identity aliases are checked during bounded preflight")
def test_d_original_and_staged_hardlinks_are_rejected(experiment_root: Path) -> None:
    config_path, config, manifest_path, manifest = _create_case(
        experiment_root, run_id="r1-d-original-staged-hardlink"
    )
    d = next(item for item in manifest["components"] if item["role"] == "domain_D")
    for record in d["files"].values():
        original = manifest_path.parent / record["origin"]["path"]
        staged = manifest_path.parent / record["path"]
        staged.unlink()
        os.link(original, staged)
        record["sha256"] = hashlib.sha256(staged.read_bytes()).hexdigest()
        record["expected_stat_identity"] = restricted_worker._stat_identity(staged)
        record["origin"]["sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
        record["origin"]["expected_stat_identity"] = restricted_worker._stat_identity(original)
        assert record["expected_stat_identity"]["file_id"] == record["origin"]["expected_stat_identity"]["file_id"]
    _refresh_manifest_and_config(config_path, config, manifest_path, manifest)

    result = run_restricted_preparation(config_path, project_root=ROOT)
    failure = _assert_failed_without_ready(
        result, config, status="source_identity_rejected", exit_code=54, worker_started=False
    )
    assert "SOURCE_PHYSICAL_IDENTITY_REUSED" in failure["message"]


@pytest.mark.skipif(os.name != "nt", reason="cross-component physical aliases are checked during bounded preflight")
def test_cross_component_hardlink_alias_is_rejected(experiment_root: Path) -> None:
    config_path, config, manifest_path, manifest = _create_case(
        experiment_root, run_id="r1-cross-component-hardlink"
    )
    d = next(item for item in manifest["components"] if item["role"] == "domain_D")
    s = next(item for item in manifest["components"] if item["role"] == "security_S")
    d_original = manifest_path.parent / d["files"]["dbf"]["origin"]["path"]
    d_staged = manifest_path.parent / d["files"]["dbf"]["path"]
    s_record = s["files"]["geometry"]
    s_original = manifest_path.parent / s_record["origin"]["path"]
    s_staged = manifest_path.parent / s_record["path"]
    s_original.unlink()
    s_staged.unlink()
    os.link(d_original, s_original)
    os.link(d_staged, s_staged)
    s_record["sha256"] = hashlib.sha256(s_staged.read_bytes()).hexdigest()
    s_record["expected_stat_identity"] = restricted_worker._stat_identity(s_staged)
    s_record["origin"]["sha256"] = hashlib.sha256(s_original.read_bytes()).hexdigest()
    s_record["origin"]["expected_stat_identity"] = restricted_worker._stat_identity(s_original)
    _refresh_manifest_and_config(config_path, config, manifest_path, manifest)

    result = run_restricted_preparation(config_path, project_root=ROOT)
    failure = _assert_failed_without_ready(
        result, config, status="source_identity_rejected", exit_code=54, worker_started=False
    )
    assert "SOURCE_PHYSICAL_IDENTITY_REUSED" in failure["message"]


def test_stable_ring_area_is_translation_invariant_for_winding_holes_and_large_offsets() -> None:
    outer_ccw = [(0.0, 0.0), (200.0, 0.0), (200.0, 200.0), (0.0, 200.0)]
    outer_cw = list(reversed(outer_ccw))
    hole_ccw = [(50.0, 50.0), (150.0, 50.0), (150.0, 150.0), (50.0, 150.0)]
    assert _stable_ring_area(outer_ccw) == pytest.approx(40_000.0, abs=1e-12)
    assert _stable_ring_area(outer_cw) == pytest.approx(-40_000.0, abs=1e-12)
    assert _stable_ring_area(hole_ccw) == pytest.approx(10_000.0, abs=1e-12)
    assert abs(_stable_ring_area(outer_ccw)) - abs(_stable_ring_area(hole_ccw)) == pytest.approx(30_000.0)
    for dx, dy in ((500_000.0, 4_320_000.0), (-1_000_000_000.0, 1_000_000_000.0)):
        outer = [(x + dx, y + dy) for x, y in outer_ccw]
        hole = [(x + dx, y + dy) for x, y in hole_ccw]
        assert _stable_ring_area(outer) == pytest.approx(40_000.0, abs=1e-8)
        assert _stable_ring_area(list(reversed(outer))) == pytest.approx(-40_000.0, abs=1e-8)
        assert _stable_ring_area(hole) == pytest.approx(10_000.0, abs=1e-8)


@pytest.mark.skipif(os.name != "nt", reason="geographic-to-projected G0 replays require the Windows Job worker")
@pytest.mark.parametrize(("size_m", "expected_active_area_m2"), [(200.0, 39950.0), (1000.0, 999950.0)])
def test_g0_area_gate_replays_200m_and_1000m_geographic_parent_D(
    experiment_root: Path, size_m: float, expected_active_area_m2: float,
) -> None:
    from pyproj import CRS, Transformer

    target = CRS.from_epsg(4548)
    source = CRS.from_epsg(4490)
    inverse = Transformer.from_crs(target, source, always_xy=True, allow_ballpark=False, only_best=True)
    x0, y0 = 500000.0, 4320000.0
    ring_target = [[x0, y0], [x0, y0 + size_m], [x0 + size_m, y0 + size_m], [x0 + size_m, y0]]
    ring_source = [list(inverse.transform(x, y)) for x, y in ring_target]
    s1 = [[x0 + 30.0, y0 + 30.0], [x0 + 35.0, y0 + 30.0],
          [x0 + 35.0, y0 + 35.0], [x0 + 30.0, y0 + 35.0]]
    s2 = [[x0 + 100.0, y0 + 100.0], [x0 + 105.0, y0 + 100.0],
          [x0 + 105.0, y0 + 105.0], [x0 + 100.0, y0 + 105.0]]
    config_path, config, _manifest_path, _manifest = mesh_example.create_synthetic_domain_mesh_case(
        ROOT, experiment_root.relative_to(ROOT), run_id=f"r1-g0-stable-area-{int(size_m)}m",
        d_polygons=[[ring_source]], s_polygons=[[s1], [s2]],
        d_prj_crs="EPSG:4490", d_source_crs="EPSG:4490",
        derived_domain_exterior=True, include_constraint=False,
    )
    assert config["mesh_generation"]["area_abs_tolerance_m2"] == pytest.approx(1e-7, abs=0.0)
    assert config["mesh_generation"]["area_rel_tolerance"] == pytest.approx(1e-9, abs=0.0)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "mesh_generation_diagnostic_complete", result
    assert result.bundle_path is not None and result.worker_receipt is not None
    assert result.worker_receipt["job_active_processes_at_exit"] == 0
    audit = json.loads((result.bundle_path / "artifacts" / "domain-source-audit.json").read_text(encoding="utf-8"))
    assert audit["domain_D"]["area_m2"] == pytest.approx(size_m * size_m, abs=3e-6)
    assert audit["security_S"]["active_D_minus_S_area_m2"] == pytest.approx(expected_active_area_m2, abs=3e-6)
    assert abs(audit["area_audit"]["difference_m2"]) <= audit["area_audit"]["tolerance_m2"]
    d = next(item for item in audit["source_components"] if item["role"] == "domain_D")
    assert d["coordinate_operation"]["coordinate_transform_applied"] is True
    assert all(flag is False for flag in d["coordinate_operation"]["ballpark_flags"])
    assert (result.bundle_path / "MESH_READY").is_file()


@pytest.mark.skipif(os.name != "nt", reason="schema 1.0 file-backed mesh-only route runs in the Windows Job")
def test_v1_file_backed_line_route_remains_executable_after_r1_fixes(experiment_root: Path) -> None:
    config_path, config, _manifest_path, manifest = mesh_example.create_synthetic_domain_mesh_case(
        ROOT, experiment_root.relative_to(ROOT), run_id="r1-legacy-v1-file-line",
    )
    assert manifest["schema_version"] == "1.0"
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "mesh_generation_diagnostic_complete", result
    assert result.bundle_path is not None and result.worker_receipt is not None
    assert result.worker_receipt["job_active_processes_at_exit"] == 0
    audit = json.loads((result.bundle_path / "artifacts" / "domain-source-audit.json").read_text(encoding="utf-8"))
    assert audit["format"] == "triangle-domain-mesh-only-source-audit-v1"
    assert (result.bundle_path / "MESH_READY").is_file()

