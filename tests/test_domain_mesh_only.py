from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from typing import Any, Mapping

import pytest

import hydrodynamics.restricted_worker as restricted_worker
from hydrodynamics.domain_mesh_only import (
    DomainMeshOnlyError,
    _crs_equivalent,
    read_domain_mesh_bundle_identity,
    validate_domain_mesh_bundle_semantics,
    validate_domain_mesh_only_config_shape,
    validate_source_manifest_shape,
)
from hydrodynamics.restricted_worker import RestrictedWorkerError, run_restricted_preparation


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "examples/triangular_mesh/run_domain_mesh_only_synthetic.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("domain_mesh_only_synthetic_example", RUNNER_PATH)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
mesh_only_example = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(mesh_only_example)


@pytest.fixture
def experiment_root():
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="domain-mesh-only-test-", dir=work) as temp:
        yield Path(temp) / "experiment"


def _make_case(experiment: Path, **kwargs: Any) -> tuple[Path, dict[str, Any], Path, dict[str, Any]]:
    return mesh_only_example.create_synthetic_domain_mesh_case(ROOT, experiment.relative_to(ROOT), **kwargs)


def _modified_epsg4548_wkt(parameter: str) -> str:
    from pyproj import CRS

    patterns = {
        "central_meridian": (r'(PARAMETER\["Longitude of natural origin",)\s*117(?=,)', "118"),
        "false_easting": (r'(PARAMETER\["False easting",)\s*500000(?=,)', "500100"),
        "scale_factor": (r'(PARAMETER\["Scale factor at natural origin",)\s*1(?=,)', "0.9996"),
    }
    pattern, replacement = patterns[parameter]
    wkt = CRS.from_epsg(4548).to_wkt(version="WKT2_2019", pretty=False)
    changed, count = re.subn(pattern, rf"\g<1>{replacement}", wkt, count=1)
    assert count == 1, parameter
    return changed


def _synthetic_false_easting_crs_wkt() -> str:
    from pyproj import CRS

    wkt = CRS.from_epsg(4548).to_wkt(version="WKT2_2019", pretty=False)
    changed, count = re.subn(
        r'(PARAMETER\["False easting",)\s*500000(?=,)', r"\g<1>499900", wkt, count=1)
    assert count == 1
    changed, count = re.subn(r',ID\["EPSG",4548\]\]$', "]", changed, count=1)
    assert count == 1
    return changed


def _wkt2_without_tm_parameter(parameter_name: str) -> str:
    from pyproj import CRS

    wkt = CRS.from_epsg(4548).to_wkt(version="WKT2_2019", pretty=False)
    start = wkt.index(f'PARAMETER["{parameter_name}",')
    depth = 0
    end = start
    for end in range(wkt.index("[", start), len(wkt)):
        if wkt[end] == "[":
            depth += 1
        elif wkt[end] == "]":
            depth -= 1
            if depth == 0:
                end += 1
                break
    prefix = start - 1 if start > 0 and wkt[start - 1] == "," else start
    return wkt[:prefix] + wkt[end:]


def _wkt2_with_extra_tm_parameter() -> str:
    from pyproj import CRS

    wkt = CRS.from_epsg(4548).to_wkt(version="WKT2_2019", pretty=False)
    start = wkt.index('PARAMETER["False northing",')
    depth = 0
    end = start
    for end in range(wkt.index("[", start), len(wkt)):
        if wkt[end] == "[":
            depth += 1
        elif wkt[end] == "]":
            depth -= 1
            if depth == 0:
                end += 1
                break
    return wkt[:end] + ',PARAMETER["Extra parameter",1,SCALEUNIT["unity",1]]' + wkt[end:]


def _projjson_tm_crs_without_parameter(parameter_name: str):
    from pyproj import CRS

    payload = CRS.from_epsg(4548).to_json_dict()
    payload["conversion"]["parameters"] = [
        parameter for parameter in payload["conversion"]["parameters"]
        if parameter["name"] != parameter_name
    ]
    return CRS.from_json_dict(payload)


def _wkt2_with_extra_tm_parameter() -> str:
    from pyproj import CRS

    wkt = CRS.from_epsg(4548).to_wkt(version="WKT2_2019", pretty=False)
    start = wkt.index('PARAMETER["False northing",')
    depth = 0
    end = start
    for end in range(wkt.index("[", start), len(wkt)):
        if wkt[end] == "[":
            depth += 1
        elif wkt[end] == "]":
            depth -= 1
            if depth == 0:
                end += 1
                break
    return wkt[:end] + ',PARAMETER["Extra parameter",1,SCALEUNIT["unity",1]]' + wkt[end:]


def _projjson_tm_crs_without_parameter(parameter_name: str):
    from pyproj import CRS

    payload = CRS.from_epsg(4548).to_json_dict()
    payload["conversion"]["parameters"] = [
        parameter for parameter in payload["conversion"]["parameters"]
        if parameter["name"] != parameter_name
    ]
    return CRS.from_json_dict(payload)


def _refresh_component_identity(config_path: Path, config: dict[str, Any],
                                manifest_path: Path, source_manifest: dict[str, Any],
                                component_id: str, file_role: str, file_path: Path) -> None:
    component = next(item for item in source_manifest["components"]
                     if item["component_id"] == component_id)
    component["files"][file_role] = mesh_only_example._file_identity(file_path, base=manifest_path.parent)
    manifest_bytes = mesh_only_example._write_json(manifest_path, source_manifest)
    config["source_manifest"]["sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    config["source_manifest"]["expected_stat_identity"] = restricted_worker._stat_identity(manifest_path)
    mesh_only_example._write_json(config_path, config)


def _write_line_geojson_crs(path: Path, crs_declaration: str) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["crs"]["properties"]["name"] = crs_declaration
    mesh_only_example._write_json(path, payload)


def _transform_xy_sequence(points: list[list[float]], transformer: Any) -> list[list[float]]:
    return [list(transformer.transform(float(x), float(y))) for x, y, *_rest in points]


def _shift_xy_sequence(points: list[list[float]], delta_x: float) -> list[list[float]]:
    return [[float(x) + delta_x, float(y)] for x, y, *_rest in points]


def _assert_failed_mesh_only(result: Any, config: Mapping[str, Any], *,
                             status: str, exit_code: int, worker_started: bool) -> None:
    assert result.status == status, result
    assert result.exit_code == exit_code
    assert result.worker_started is worker_started
    output = ROOT / config["output_root"]
    assert not (output / result.run_id).exists()
    assert not (output / "latest").exists()
    assert not list(output.glob(".*.pending"))
    diagnostic = result.diagnostic_path
    assert diagnostic is not None
    assert not (diagnostic / "MESH_READY").exists()
    assert not (diagnostic / "MESH_READY.pending").exists()
    assert not (diagnostic / "READY").exists()
    assert not (diagnostic / "READY.pending").exists()
    failure = json.loads((diagnostic / "status.json").read_text(encoding="utf-8"))
    assert failure["mesh_only"] is True and failure["mesh_ready"] is False
    assert failure["simulation_result"] is False and failure["engineering_ready"] is False
    if result.worker_receipt is not None:
        assert result.worker_receipt["job_active_processes_at_exit"] == 0


def _write_forbidden_import_sentinel(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sentinel = directory / "sitecustomize"
    sentinel.mkdir()
    log = sentinel / "forbidden-access.log"
    script = f'''import importlib.abc
import os
from pathlib import Path
import sys

_LOG = Path(os.environ["DOMAIN_MESH_ONLY_FORBIDDEN_LOG"])
class _RejectTerrainImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "rasterio" or fullname.startswith("rasterio.") or fullname == "hydrodynamics.terrain_windowed_mapping":
            _LOG.write_text(fullname, encoding="utf-8")
            raise RuntimeError("mesh-only sentinel rejected terrain/DEM import: " + fullname)
        return None
sys.meta_path.insert(0, _RejectTerrainImports())
import shapefile
def _reject_dbf(*args, **kwargs):
    _LOG.write_text("DBF record access", encoding="utf-8")
    raise RuntimeError("mesh-only sentinel rejected DBF record access")
shapefile.Reader.record = _reject_dbf
shapefile.Reader.records = _reject_dbf
'''
    (sentinel / "sitecustomize.py").write_text(script, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(sentinel))
    monkeypatch.setenv("DOMAIN_MESH_ONLY_FORBIDDEN_LOG", str(log))
    return log


@pytest.mark.skipif(os.name != "nt", reason="route input validation is tested with the Windows worker package")
def test_domain_mesh_only_schemas_examples_and_b0_v1_remain_closed() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    examples = ROOT / "examples/triangular_mesh"
    config_schema = json.loads((examples / "domain_mesh_only_config.schema.json").read_text(encoding="utf-8"))
    source_schema = json.loads((examples / "domain_mesh_only_source_manifest.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(config_schema)
    jsonschema.Draft202012Validator.check_schema(source_schema)
    config = json.loads((examples / "domain_mesh_only_config.example.json").read_text(encoding="utf-8"))
    manifest = json.loads((examples / "domain_mesh_only_source_manifest.example.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(config_schema).validate(config)
    jsonschema.Draft202012Validator(source_schema).validate(manifest)
    assert validate_domain_mesh_only_config_shape(config) is config
    assert validate_source_manifest_shape(manifest) is manifest
    assert restricted_worker.validate_route_config_shape(config) is config
    b0 = json.loads((examples / "restricted_worker_config.example.json").read_text(encoding="utf-8"))
    with pytest.raises(RestrictedWorkerError, match="unknown config fields"):
        restricted_worker.validate_config_shape({**b0, "domain_mesh_only": True})
    with pytest.raises(RestrictedWorkerError, match="unknown"):
        restricted_worker.validate_route_config_shape({**b0, "format": restricted_worker.FORMAT,
                                                       "domain_mesh_only": True})


@pytest.mark.parametrize("parameter", ["central_meridian", "false_easting", "scale_factor"])
def test_domain_mesh_only_crs_equivalence_rejects_low_confidence_epsg_guess(parameter: str) -> None:
    pyproj = pytest.importorskip("pyproj")
    canonical = pyproj.CRS.from_epsg(4548)
    modified = pyproj.CRS.from_wkt(_modified_epsg4548_wkt(parameter))
    assert not modified.equals(canonical, ignore_axis_order=True)
    assert modified.to_epsg(min_confidence=0) == 4548
    assert not _crs_equivalent(modified, canonical)


def test_domain_mesh_only_crs_equivalence_uses_explicit_xy_axis_policy() -> None:
    pyproj = pytest.importorskip("pyproj")
    canonical = pyproj.CRS.from_epsg(4326)
    axis_variant = pyproj.CRS.from_user_input("OGC:CRS84")
    assert not axis_variant.equals(canonical)
    assert axis_variant.equals(canonical, ignore_axis_order=True)
    assert _crs_equivalent(axis_variant, canonical)
    projected = pyproj.CRS.from_epsg(4548)
    shapefile_prj = pyproj.CRS.from_wkt(projected.to_wkt(version="WKT1_GDAL"))
    assert not shapefile_prj.equals(projected, ignore_axis_order=True)
    assert _crs_equivalent(shapefile_prj, projected)


@pytest.mark.parametrize("representation", ["WKT2", "PROJJSON"])
@pytest.mark.parametrize("parameter_name", [
    "Latitude of natural origin", "False northing", "Scale factor at natural origin",
])
def test_domain_mesh_only_projected_crs_rejects_missing_transverse_mercator_parameter(
        representation: str, parameter_name: str) -> None:
    pyproj = pytest.importorskip("pyproj")
    canonical = pyproj.CRS.from_epsg(4548)
    incomplete = (pyproj.CRS.from_wkt(_wkt2_without_tm_parameter(parameter_name))
                  if representation == "WKT2"
                  else _projjson_tm_crs_without_parameter(parameter_name))
    assert len(incomplete.coordinate_operation.params) == 4
    assert incomplete.equals(canonical, ignore_axis_order=True)
    assert not _crs_equivalent(incomplete, canonical)


@pytest.mark.parametrize("representation", ["WKT2", "PROJJSON"])
def test_domain_mesh_only_projected_crs_rejects_extra_transverse_mercator_parameter(
        representation: str) -> None:
    pyproj = pytest.importorskip("pyproj")
    canonical = pyproj.CRS.from_epsg(4548)
    if representation == "WKT2":
        extra = pyproj.CRS.from_wkt(_wkt2_with_extra_tm_parameter())
    else:
        payload = canonical.to_json_dict()
        payload["conversion"]["parameters"].append(
            {"name": "Extra parameter", "value": 1, "unit": "unity"})
        extra = pyproj.CRS.from_json_dict(payload)
    assert len(extra.coordinate_operation.params) == 6
    assert extra.equals(canonical, ignore_axis_order=True)
    assert not _crs_equivalent(extra, canonical)


def test_domain_mesh_only_projected_crs_rejects_missing_or_wrong_parameter_units() -> None:
    pyproj = pytest.importorskip("pyproj")
    canonical = pyproj.CRS.from_epsg(4548)
    payload = canonical.to_json_dict()
    false_easting = next(item for item in payload["conversion"]["parameters"]
                         if item["name"] == "False easting")
    false_easting["unit"] = "degree"
    wrong_unit = pyproj.CRS.from_json_dict(payload)
    assert next(item for item in wrong_unit.coordinate_operation.params
                if item.name == "False easting").unit_category == "angular"
    assert not _crs_equivalent(wrong_unit, canonical)

    payload = canonical.to_json_dict()
    latitude = next(item for item in payload["conversion"]["parameters"]
                    if item["name"] == "Latitude of natural origin")
    latitude.pop("unit")
    with pytest.raises(pyproj.exceptions.CRSError):
        pyproj.CRS.from_json_dict(payload)


def test_domain_mesh_only_complete_wkt_and_xy_crs_and_vector_conversion_remain_valid() -> None:
    pyproj = pytest.importorskip("pyproj")
    projected = pyproj.CRS.from_epsg(4548)
    shapefile_prj = pyproj.CRS.from_wkt(projected.to_wkt(version="WKT1_GDAL"))
    assert _crs_equivalent(shapefile_prj, projected)
    epsg_4326 = pyproj.CRS.from_epsg(4326)
    crs84 = pyproj.CRS.from_user_input("OGC:CRS84")
    assert _crs_equivalent(crs84, epsg_4326)

    from pyproj import Transformer
    custom_source = pyproj.CRS.from_wkt(_synthetic_false_easting_crs_wkt())
    assert not _crs_equivalent(custom_source, projected)
    transformed = Transformer.from_crs(custom_source, projected, always_xy=True).transform(
        470900.0, 4_331_000.0)
    assert transformed == pytest.approx((471000.0, 4_331_000.0), abs=2e-9)


def test_domain_mesh_only_uncontracted_projected_method_is_not_admitted() -> None:
    pyproj = pytest.importorskip("pyproj")
    uncontracted = pyproj.CRS.from_epsg(3857)
    assert uncontracted.equals(uncontracted, ignore_axis_order=True)
    assert not _crs_equivalent(uncontracted, uncontracted)


@pytest.mark.parametrize("representation", ["WKT2", "PROJJSON"])
@pytest.mark.parametrize("parameter_name", [
    "Latitude of natural origin", "False northing", "Scale factor at natural origin",
])
def test_domain_mesh_only_projected_crs_rejects_missing_transverse_mercator_parameter(
        representation: str, parameter_name: str) -> None:
    pyproj = pytest.importorskip("pyproj")
    canonical = pyproj.CRS.from_epsg(4548)
    incomplete = (pyproj.CRS.from_wkt(_wkt2_without_tm_parameter(parameter_name))
                  if representation == "WKT2"
                  else _projjson_tm_crs_without_parameter(parameter_name))
    assert len(incomplete.coordinate_operation.params) == 4
    assert incomplete.equals(canonical, ignore_axis_order=True)
    assert not _crs_equivalent(incomplete, canonical)


@pytest.mark.parametrize("representation", ["WKT2", "PROJJSON"])
def test_domain_mesh_only_projected_crs_rejects_extra_transverse_mercator_parameter(
        representation: str) -> None:
    pyproj = pytest.importorskip("pyproj")
    canonical = pyproj.CRS.from_epsg(4548)
    if representation == "WKT2":
        extra = pyproj.CRS.from_wkt(_wkt2_with_extra_tm_parameter())
    else:
        payload = canonical.to_json_dict()
        payload["conversion"]["parameters"].append(
            {"name": "Extra parameter", "value": 1, "unit": "unity"})
        extra = pyproj.CRS.from_json_dict(payload)
    assert len(extra.coordinate_operation.params) == 6
    assert extra.equals(canonical, ignore_axis_order=True)
    assert not _crs_equivalent(extra, canonical)


def test_domain_mesh_only_projected_crs_rejects_missing_or_wrong_parameter_units() -> None:
    pyproj = pytest.importorskip("pyproj")
    canonical = pyproj.CRS.from_epsg(4548)
    payload = canonical.to_json_dict()
    false_easting = next(item for item in payload["conversion"]["parameters"]
                         if item["name"] == "False easting")
    false_easting["unit"] = "degree"
    wrong_unit = pyproj.CRS.from_json_dict(payload)
    assert next(item for item in wrong_unit.coordinate_operation.params
                if item.name == "False easting").unit_category == "angular"
    assert not _crs_equivalent(wrong_unit, canonical)

    payload = canonical.to_json_dict()
    latitude = next(item for item in payload["conversion"]["parameters"]
                    if item["name"] == "Latitude of natural origin")
    latitude.pop("unit")
    with pytest.raises(pyproj.exceptions.CRSError):
        pyproj.CRS.from_json_dict(payload)


def test_domain_mesh_only_complete_wkt_and_xy_crs_and_vector_conversion_remain_valid() -> None:
    pyproj = pytest.importorskip("pyproj")
    projected = pyproj.CRS.from_epsg(4548)
    shapefile_prj = pyproj.CRS.from_wkt(projected.to_wkt(version="WKT1_GDAL"))
    assert _crs_equivalent(shapefile_prj, projected)
    epsg_4326 = pyproj.CRS.from_epsg(4326)
    crs84 = pyproj.CRS.from_user_input("OGC:CRS84")
    assert _crs_equivalent(crs84, epsg_4326)

    from pyproj import Transformer
    custom_source = pyproj.CRS.from_wkt(_synthetic_false_easting_crs_wkt())
    assert not _crs_equivalent(custom_source, projected)
    transformed = Transformer.from_crs(custom_source, projected, always_xy=True).transform(
        470900.0, 4_331_000.0)
    assert transformed == pytest.approx((471000.0, 4_331_000.0), abs=2e-9)


@pytest.mark.skipif(os.name != "nt", reason="CRS source conflicts are rejected by the Windows Job worker")
@pytest.mark.parametrize(("conflict", "expected_message"), [
    ("domain_prj", "explicit source_crs differs from its PRJ"),
    ("line_geojson", "GeoJSON CRS differs from its explicit source_crs"),
])
def test_domain_mesh_only_job_rejects_self_consistent_crs_source_conflicts(
        experiment_root: Path, conflict: str, expected_message: str) -> None:
    wrong_crs = _modified_epsg4548_wkt("central_meridian")
    config_path, config, manifest_path, source_manifest = _make_case(
        experiment_root, run_id=f"mesh-only-crs-conflict-{conflict}")
    if conflict == "domain_prj":
        sidecar = experiment_root / "inputs" / "domain" / "domain_D.prj"
        sidecar.write_text(wrong_crs + "\n", encoding="utf-8")
        _refresh_component_identity(config_path, config, manifest_path, source_manifest,
                                    "domain_D", "prj", sidecar)
    else:
        line_path = experiment_root / "inputs" / "lines" / "domain_boundary.geojson"
        _write_line_geojson_crs(line_path, wrong_crs)
        component = next(item for item in source_manifest["components"]
                         if item["component_id"] == "domain_boundary_01")
        component["source_crs"] = "EPSG:4548"
        _refresh_component_identity(config_path, config, manifest_path, source_manifest,
                                    "domain_boundary_01", "geometry", line_path)

    result = run_restricted_preparation(config_path, project_root=ROOT)
    _assert_failed_mesh_only(result, config, status="worker_failed", exit_code=53, worker_started=True)
    assert result.diagnostic_path is not None
    failure = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
    assert expected_message in failure["message"]


@pytest.mark.skipif(os.name != "nt", reason="incomplete projected CRS is rejected by the Windows Job worker")
def test_domain_mesh_only_job_rejects_missing_scale_with_consistent_source_identities(
        experiment_root: Path) -> None:
    config_path, config, manifest_path, source_manifest = _make_case(
        experiment_root, run_id="mesh-only-crs-missing-scale")
    line_path = experiment_root / "inputs" / "lines" / "constraint_01.geojson"
    _write_line_geojson_crs(
        line_path, _wkt2_without_tm_parameter("Scale factor at natural origin"))
    _refresh_component_identity(config_path, config, manifest_path, source_manifest,
                                "constraint_01", "geometry", line_path)

    result = run_restricted_preparation(config_path, project_root=ROOT)
    _assert_failed_mesh_only(result, config, status="worker_failed", exit_code=53, worker_started=True)
    assert result.diagnostic_path is not None
    failure = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
    assert "GeoJSON CRS differs from its explicit source_crs" in failure["message"]


@pytest.mark.skipif(os.name != "nt", reason="incomplete projected CRS is rejected by the Windows Job worker")
def test_domain_mesh_only_job_rejects_missing_scale_with_consistent_identities(
        experiment_root: Path) -> None:
    config_path, config, manifest_path, source_manifest = _make_case(
        experiment_root, run_id="mesh-only-crs-missing-scale")
    constraint_path = experiment_root / "inputs" / "lines" / "constraint_01.geojson"
    _write_line_geojson_crs(
        constraint_path, _wkt2_without_tm_parameter("Scale factor at natural origin"))
    _refresh_component_identity(config_path, config, manifest_path, source_manifest,
                                "constraint_01", "geometry", constraint_path)

    result = run_restricted_preparation(config_path, project_root=ROOT)
    _assert_failed_mesh_only(result, config, status="worker_failed", exit_code=53, worker_started=True)
    assert result.diagnostic_path is not None
    failure = json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8"))
    assert "GeoJSON CRS differs from its explicit source_crs" in failure["message"]


@pytest.mark.skipif(os.name != "nt", reason="vector CRS transformation is verified in the Windows Job worker")
def test_domain_mesh_only_job_transforms_declared_projected_vectors_to_target_xy(
        experiment_root: Path) -> None:
    from pyproj import Transformer

    source_wkt = _synthetic_false_easting_crs_wkt()
    from pyproj import CRS
    source_crs = CRS.from_wkt(source_wkt)
    target_crs = CRS.from_epsg(4548)
    assert not _crs_equivalent(source_crs, target_crs)
    source_to_target = Transformer.from_crs(source_crs, target_crs, always_xy=True)
    assert source_to_target.transform(470900.0, 4_331_000.0) == pytest.approx((471000.0, 4_331_000.0), abs=2e-9)
    target_ring = [[471000.0, 4_331_000.0], [471100.0, 4_331_000.0],
                   [471100.0, 4_331_100.0], [471000.0, 4_331_100.0]]
    source_ring = _shift_xy_sequence(target_ring, -100.0)
    target_security = [[471020.0, 4_331_020.0], [471030.0, 4_331_020.0],
                       [471030.0, 4_331_030.0], [471020.0, 4_331_030.0]]
    target_constraint = [[471040.0, 4_331_060.0], [471050.0, 4_331_060.0]]
    source_boundary = _shift_xy_sequence(target_ring + [target_ring[0]], -100.0)
    source_constraint = _shift_xy_sequence(target_constraint, -100.0)
    config_path, config, manifest_path, source_manifest = _make_case(
        experiment_root, run_id="mesh-only-projected-vector-conversion",
        d_polygons=[[source_ring]], s_polygons=[[target_security]],
        boundary_points=source_boundary, constraint_points=source_constraint,
        d_prj_crs=source_wkt, d_source_crs=source_wkt)
    for component in source_manifest["components"]:
        if component["role"] not in {"boundary_line", "constraint_line"}:
            continue
        line_path = manifest_path.parent / component["files"]["geometry"]["path"]
        payload = json.loads(line_path.read_text(encoding="utf-8"))
        payload["crs"]["properties"]["name"] = source_wkt
        mesh_only_example._write_json(line_path, payload)
        component["source_crs"] = source_wkt
        _refresh_component_identity(config_path, config, manifest_path, source_manifest,
                                    component["component_id"], "geometry", line_path)

    result = run_restricted_preparation(config_path, project_root=ROOT)
    if result.status != "mesh_generation_diagnostic_complete":
        failure = (None if result.diagnostic_path is None else
                   json.loads((result.diagnostic_path / "status.json").read_text(encoding="utf-8")))
        raise AssertionError(f"{result.status}: {result.message}; worker message={None if failure is None else failure.get('message')}")
    assert result.exit_code == 0 and result.worker_started is True
    assert result.bundle_path is not None and result.worker_receipt is not None
    assert result.worker_receipt["job_active_processes_at_exit"] == 0
    bundle = result.bundle_path
    audit = json.loads((bundle / "artifacts" / "domain-source-audit.json").read_text(encoding="utf-8"))
    by_id = {item["component_id"]: item for item in audit["source_components"]}
    for component_id in ("domain_D", "domain_boundary_01", "constraint_01"):
        assert by_id[component_id]["source_crs"] == source_wkt
        assert by_id[component_id]["target_crs"] == "EPSG:4548"
        assert by_id[component_id]["coordinate_transform_applied"] is True
    assert by_id["security_S"]["coordinate_transform_applied"] is False
    assert audit["domain_D"]["area_m2"] == pytest.approx(10_000.0, rel=1e-6)
    assert audit["security_S"]["area_m2"] == pytest.approx(100.0, rel=1e-6)
    assert audit["security_S"]["active_D_minus_S_area_m2"] == pytest.approx(9_900.0, rel=1e-6)
    mesh_payload = json.loads((bundle / "artifacts" / "domain-mesh.json").read_text(encoding="utf-8"))
    for expected_node in target_ring:
        assert any(node == pytest.approx(expected_node, rel=0, abs=1e-7)
                   for node in mesh_payload["node_xy"])
    worker_result = json.loads((bundle / "artifacts" / "worker-result.json").read_text(encoding="utf-8"))
    assert worker_result["semantic_readback"]["semantic_readback_passed"] is True
    assert audit["dem_pixel_reader_calls"] == audit["terrain_mapper_calls"] == audit["solver_calls"] == 0
    assert (bundle / "MESH_READY").is_file() and not (bundle / "READY").exists()
    assert not (ROOT / config["output_root"] / "latest").exists()


@pytest.mark.skipif(os.name != "nt", reason="domain mesh-only generation requires Windows Job Objects")
def test_domain_mesh_only_generates_d_minus_s_in_job_without_terrain_or_solver(
        experiment_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s_other = [[473576.0, 4335952.0], [473581.0, 4335952.0],
               [473581.0, 4335957.0], [473576.0, 4335957.0]]
    d_hole = [[473566.0, 4335960.0], [473576.0, 4335960.0],
              [473576.0, 4335970.0], [473566.0, 4335970.0]]
    config_path, config, _manifest_path, source_manifest = _make_case(
        experiment_root, d_polygons=[[mesh_only_example.OUTER[::-1], d_hole]],
        s_polygons=[[mesh_only_example.SECURITY], [s_other]],
    )
    forbidden_log = _write_forbidden_import_sentinel(experiment_root, monkeypatch)
    assert "gmsh" not in sys.modules

    def pending_stage_is_not_consumable(stage: Path) -> None:
        with pytest.raises(DomainMeshOnlyError, match="mesh-only manifest flags"):
            read_domain_mesh_bundle_identity(
                stage, expected_config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
                expected_source_manifest_sha256=config["source_manifest"]["sha256"],
            )

    result = run_restricted_preparation(
        config_path, project_root=ROOT, _before_directory_commit=pending_stage_is_not_consumable)
    assert result.status == "mesh_generation_diagnostic_complete", result
    assert result.exit_code == 0 and result.worker_started is True
    assert result.bundle_path is not None and result.worker_receipt is not None
    bundle = result.bundle_path
    assert (bundle / "MESH_READY").is_file()
    assert not (bundle / "READY").exists() and not (bundle / "READY.pending").exists()
    assert not (bundle / "latest").exists()
    assert not (bundle / "artifacts" / "prepared_faces.jsonl").exists()
    assert not (bundle / "artifacts" / "prepared_body.json").exists()
    assert not list(bundle.rglob("*.nc"))
    assert not forbidden_log.exists(), forbidden_log.read_text(encoding="utf-8") if forbidden_log.exists() else ""
    assert "gmsh" not in sys.modules

    expected_config_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
    identity = read_domain_mesh_bundle_identity(
        bundle, expected_config_sha256=expected_config_sha,
        expected_source_manifest_sha256=config["source_manifest"]["sha256"],
    )
    assert identity["identity_only"] is True and identity["semantic_validation_performed"] is False
    with pytest.raises(DomainMeshOnlyError, match="dispatcher-bound Job context"):
        validate_domain_mesh_bundle_semantics(
            bundle, config_path=config_path, expected_config_sha256=expected_config_sha,
            project_root=ROOT, expected_mesh_sha256=identity["manifest"]["files"]["mesh_file"]["sha256"],
            expected_source_audit_sha256=identity["manifest"]["files"]["source_audit_file"]["sha256"],
            code_hashes=identity["manifest"]["route_code_sha256"],
        )

    manifest = identity["manifest"]
    mesh_payload = json.loads((bundle / "artifacts" / "domain-mesh.json").read_text(encoding="utf-8"))
    source_audit = json.loads((bundle / "artifacts" / "domain-source-audit.json").read_text(encoding="utf-8"))
    assert manifest["mesh_only"] is True and manifest["preparation_only"] is True
    assert manifest["simulation_result"] is False and manifest["production_approved"] is False
    assert manifest["engineering_ready"] is False and manifest["mesh_ready"] is True
    assert manifest["worker"]["job_active_processes_at_exit"] == 0
    assert manifest["mesh_generation"]["gmsh"]["native_library_loaded_in_worker"] is True
    assert manifest["mesh_generation"]["gmsh"]["worker_pid"] in manifest["worker"]["job_process_ids_observed"]
    assert manifest["mesh_generation"]["quality_report"]["faces_below_quality_threshold"] == []
    assert source_audit["dem_pixel_reader_calls"] == source_audit["terrain_mapper_calls"] == source_audit["solver_calls"] == 0
    components = source_audit["source_components"]
    assert {item["component_id"] for item in components} == {item["component_id"] for item in source_manifest["components"]}
    shapefile_record = next(item for item in components if item["component_id"] == "domain_D")
    assert {item["role"] for item in shapefile_record["files"]} == {"shp", "shx", "prj", "dbf", "cpg"}
    security_record = next(item for item in components if item["component_id"] == "security_S")
    assert security_record["source_feature_count"] == 2
    assert source_audit["domain_D"]["feature_count"] == 1
    assert source_audit["domain_D"]["area_m2"] == pytest.approx(9900.0)
    assert source_audit["security_S"]["area_m2"] == pytest.approx(125.0)
    assert source_audit["security_S"]["active_D_minus_S_area_m2"] == pytest.approx(9775.0)
    assert {name for name in mesh_payload["boundary_groups"] if name.startswith("hole_")} == {"hole_0", "hole_1", "hole_2"}
    assert "synthetic_domain_boundary" in mesh_payload["boundary_groups"]
    assert "synthetic_constraint" in mesh_payload["constraint_groups"]
    with pytest.raises(RestrictedWorkerError, match="already exists"):
        run_restricted_preparation(config_path, project_root=ROOT)


@pytest.mark.skipif(os.name != "nt", reason="domain geometry refusal requires Windows Job Objects")
@pytest.mark.parametrize("failure", ["crs_mismatch", "invalid_domain", "multipart_domain", "security_outside", "constraint_crosses_s"])
def test_domain_mesh_only_refuses_invalid_or_unsupported_synthetic_geometry(
        experiment_root: Path, failure: str) -> None:
    def rectangle(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

    kwargs: dict[str, Any] = {}
    if failure == "crs_mismatch":
        kwargs["d_prj_crs"] = "EPSG:4326"
    elif failure == "invalid_domain":
        kwargs["d_polygons"] = [[[[473506, 4335888], [473606, 4335988],
                                   [473506, 4335988], [473606, 4335888]]]]
    elif failure == "multipart_domain":
        kwargs["d_polygons"] = [[rectangle(473506, 4335888, 473546, 4335928)],
                                [rectangle(473566, 4335948, 473606, 4335988)]]
    elif failure == "security_outside":
        kwargs["s_polygons"] = [[rectangle(473600, 4335975, 473620, 4335995)]]
    elif failure == "constraint_crosses_s":
        kwargs["constraint_points"] = [[473536, 4335932], [473566, 4335932]]
    config_path, config, _manifest_path, _manifest = _make_case(
        experiment_root, run_id=f"mesh-only-{failure}", **kwargs)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    _assert_failed_mesh_only(result, config, status="worker_failed", exit_code=53, worker_started=True)


@pytest.mark.skipif(os.name != "nt", reason="source identity checks are dispatched with the Windows worker")
def test_domain_mesh_only_binds_dbf_sidecar_identity(experiment_root: Path) -> None:
    config_path, config, _manifest_path, _manifest = _make_case(
        experiment_root, run_id="mesh-only-db-record-change")
    sidecar = experiment_root / "inputs" / "domain" / "domain_D.dbf"
    with sidecar.open("ab") as stream:
        stream.write(b"synthetic-identity-change")
    result = run_restricted_preparation(config_path, project_root=ROOT)
    _assert_failed_mesh_only(result, config, status="source_identity_rejected", exit_code=54, worker_started=False)


@pytest.mark.skipif(os.name != "nt", reason="face/quality cap refusal requires Windows Job Objects")
@pytest.mark.parametrize(("failure", "minimum_face_quality", "maximum_faces"), [
    ("quality", 1.0, 500_000),
    ("face-cap", 0.1, 1),
])
def test_domain_mesh_only_quality_and_face_caps_refuse_without_mesh_ready(
        experiment_root: Path, failure: str, minimum_face_quality: float, maximum_faces: int) -> None:
    config_path, config, _manifest_path, _manifest = _make_case(
        experiment_root, run_id=f"mesh-only-{failure}",
        minimum_face_quality=minimum_face_quality, maximum_faces=maximum_faces)
    result = run_restricted_preparation(config_path, project_root=ROOT)
    _assert_failed_mesh_only(result, config, status="worker_failed", exit_code=53, worker_started=True)


@pytest.mark.skipif(os.name != "nt", reason="mesh-only writer failure uses the restricted worker")
def test_domain_mesh_only_writer_failure_has_no_mesh_ready(experiment_root: Path) -> None:
    config_path, config, _manifest_path, _manifest = _make_case(
        experiment_root, run_id="mesh-only-writer-failure")

    def fail_before_commit(_stage: Path) -> None:
        raise OSError("synthetic mesh-only commit failure")

    result = run_restricted_preparation(config_path, project_root=ROOT,
                                        _before_directory_commit=fail_before_commit)
    _assert_failed_mesh_only(result, config, status="supervisor_failed", exit_code=70, worker_started=True)


@pytest.mark.skipif(os.name != "nt", reason="mesh-only cancellation uses the restricted worker Job")
def test_domain_mesh_only_cancel_after_gmsh_marker_has_no_mesh_ready(experiment_root: Path) -> None:
    config_path, config, _manifest_path, _manifest = _make_case(
        experiment_root, run_id="mesh-only-cancel-after-gmsh")
    output = ROOT / config["output_root"]
    cancel = threading.Event()
    marker_seen = threading.Event()
    monitor_errors: list[str] = []
    monitor_thread: threading.Thread | None = None

    def on_worker_created(_info: Mapping[str, Any]) -> None:
        nonlocal monitor_thread

        def monitor() -> None:
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    for stage in output.glob(".*.pending"):
                        marker = stage / "scratch" / "domain-mesh-generated.json"
                        if marker.is_file():
                            marker_seen.set()
                            cancel.set()
                            return
                    time.sleep(0.01)
                monitor_errors.append("worker did not reach its post-Gmsh marker")
            except Exception as exc:
                monitor_errors.append(f"{type(exc).__name__}: {exc}")

        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()

    result = run_restricted_preparation(
        config_path, project_root=ROOT, cancel_event=cancel,
        _on_worker_created_suspended=on_worker_created,
    )
    if monitor_thread is not None:
        monitor_thread.join(timeout=2)
    assert marker_seen.is_set() and cancel.is_set() and not monitor_errors, {"result": result, "errors": monitor_errors}
    _assert_failed_mesh_only(result, config, status="cancelled", exit_code=44, worker_started=True)
