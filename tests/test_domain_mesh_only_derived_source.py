from __future__ import annotations

import hashlib
import importlib.util
import importlib.machinery
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import textwrap
import tomllib
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
from hydrodynamics.domain_mesh_only import (
    DOMAIN_D_EXTERIOR_GEOMETRY_HASH_FORMAT,
    MAX_DERIVED_LINE_POINTS,
    DomainMeshOnlyError,
    _canonical_domain_exterior_sha256,
    validate_source_manifest_shape,
)
from hydrodynamics.restricted_worker import run_restricted_preparation


EXAMPLES = ROOT / "examples" / "triangular_mesh"
RUNNER_PATH = EXAMPLES / "run_domain_mesh_only_synthetic.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("derived_domain_mesh_example", RUNNER_PATH)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
mesh_example = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(mesh_example)


@pytest.fixture
def experiment_root() -> Path:
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="domain-mesh-derived-test-", dir=work) as temp:
        yield Path(temp) / "experiment"


def _manifest_11() -> dict[str, Any]:
    return json.loads(
        (EXAMPLES / "domain_mesh_only_source_manifest_v1_1_derived.example.json").read_text(encoding="utf-8")
    )


def _refresh_manifest_and_config(
    config_path: Path,
    config: dict[str, Any],
    manifest_path: Path,
    manifest: dict[str, Any],
) -> None:
    manifest_bytes = mesh_example._write_json(manifest_path, manifest)
    config["source_manifest"]["sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    config["source_manifest"]["expected_stat_identity"] = restricted_worker._stat_identity(manifest_path)
    mesh_example._write_json(config_path, config)


def _assert_failed_without_ready(
    result: Any, config: Mapping[str, Any], *, status: str, exit_code: int, worker_started: bool
) -> dict[str, Any]:
    assert result.status == status, result
    assert result.exit_code == exit_code
    assert result.worker_started is worker_started
    output = ROOT / config["output_root"]
    assert not (output / result.run_id).exists()
    assert not (output / "latest").exists()
    pending = list(output.glob(".*.pending"))
    assert not pending, [(path.name, [child.name for child in path.iterdir()]) for path in pending]
    diagnostic = result.diagnostic_path
    assert diagnostic is not None
    for marker in ("MESH_READY", "MESH_READY.pending", "READY", "READY.pending", "latest"):
        assert not (diagnostic / marker).exists()
    failure = json.loads((diagnostic / "status.json").read_text(encoding="utf-8"))
    assert failure["mesh_only"] is True and failure["mesh_ready"] is False
    assert failure["simulation_result"] is False and failure["engineering_ready"] is False
    if result.worker_receipt is not None:
        assert result.worker_receipt["job_active_processes_at_exit"] == 0
    return failure


def _write_worker_sentinel(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    forge_mode: str | None = None,
    forged_geometry_sha256: str | None = None,
) -> Path:
    sentinel = directory / "worker_sentinel"
    sentinel.mkdir()
    log = sentinel / "forbidden-access.log"
    script = r'''
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys

_LOG = Path(os.environ["DOMAIN_MESH_DERIVED_SENTINEL_LOG"])
_MODE = os.environ.get("DOMAIN_MESH_DERIVED_AUDIT_FORGE")
_BLOCKED = (
    "rasterio", "osgeo", "hydrodynamics.dem_reader", "hydrodynamics.terrain_mapping",
    "hydrodynamics.terrain_windowed_mapping", "hydrodynamics.solver",
)

class _BlockForbidden(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in _BLOCKED):
            _LOG.write_text("forbidden import: " + fullname, encoding="utf-8")
            raise ImportError("blocked in derived-source Job test: " + fullname)
        return None

sys.meta_path.insert(0, _BlockForbidden())

import shapefile
def _reject_dbf(*args, **kwargs):
    _LOG.write_text("DBF record access", encoding="utf-8")
    raise RuntimeError("DBF record access is forbidden")
shapefile.Reader.record = _reject_dbf
shapefile.Reader.records = _reject_dbf

if _MODE:
    class _Loader:
        def __init__(self, wrapped):
            self._wrapped = wrapped
        def create_module(self, spec):
            create = getattr(self._wrapped, "create_module", None)
            return None if create is None else create(spec)
        def exec_module(self, module):
            self._wrapped.exec_module(module)
            original = module.validate_domain_mesh_bundle_semantics
            def _forge_then_read(bundle_directory, *args, **kwargs):
                audit_path = Path(bundle_directory) / "artifacts" / "domain-source-audit.json"
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                derived = next(item for item in audit["source_components"] if item.get("source_kind") == "derived")
                provenance = derived["derivation_provenance"]
                if _MODE in {"geometry", "geometry_reverse"}:
                    forged = os.environ["DOMAIN_MESH_DERIVED_FORGED_GEOMETRY_SHA256"]
                    derived["geometry_sha256"] = forged
                    provenance["geometry_sha256"] = forged
                elif _MODE == "parent":
                    derived["parent_component_id"] = "S"
                    provenance["parent_component_id"] = "S"
                    provenance["parent_component_role"] = "security_S"
                elif _MODE == "rule":
                    provenance["derivation_rule"] = "forged_rule_v99"
                elif _MODE == "operation":
                    provenance["coordinate_operation"]["operation_identity_sha256"] = "0" * 64
                    derived["coordinate_operation"]["operation_identity_sha256"] = "0" * 64
                else:
                    raise RuntimeError("unknown synthetic forgery mode")
                forged_provenance_sha = hashlib.sha256(
                    (json.dumps(provenance, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
                ).hexdigest()
                derived["provenance_sha256"] = forged_provenance_sha
                derived["derivation_provenance"] = provenance
                for line in audit["boundary_and_constraint_components"]:
                    if line.get("component_id") == derived["component_id"]:
                        line.update({
                            "parent_component_id": derived["parent_component_id"],
                            "geometry_sha256": derived["geometry_sha256"],
                            "provenance_sha256": forged_provenance_sha,
                            "derivation_provenance": provenance,
                            "coordinate_operation": derived["coordinate_operation"],
                        })
                encoded = (json.dumps(audit, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
                audit_path.write_bytes(encoded)
                kwargs["expected_source_audit_sha256"] = hashlib.sha256(encoded).hexdigest()
                return original(bundle_directory, *args, **kwargs)
            module.validate_domain_mesh_bundle_semantics = _forge_then_read

    class _WrapDomainMeshOnly(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "hydrodynamics.domain_mesh_only":
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None and spec.loader is not None:
                spec.loader = _Loader(spec.loader)
            return spec

    sys.meta_path.insert(0, _WrapDomainMeshOnly())
'''
    (sentinel / "sitecustomize.py").write_text(textwrap.dedent(script), encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(sentinel))
    monkeypatch.setenv("DOMAIN_MESH_DERIVED_SENTINEL_LOG", str(log))
    if forge_mode is not None:
        monkeypatch.setenv("DOMAIN_MESH_DERIVED_AUDIT_FORGE", forge_mode)
        if forged_geometry_sha256 is not None:
            monkeypatch.setenv("DOMAIN_MESH_DERIVED_FORGED_GEOMETRY_SHA256", forged_geometry_sha256)
    else:
        monkeypatch.delenv("DOMAIN_MESH_DERIVED_AUDIT_FORGE", raising=False)
        monkeypatch.delenv("DOMAIN_MESH_DERIVED_FORGED_GEOMETRY_SHA256", raising=False)
    return log


def _expected_xy_hash(points: tuple[tuple[float, float], ...]) -> str:
    digest = hashlib.sha256()
    digest.update(b"triangle-domain-D-exterior-xy-m-f64le-v1\0")
    digest.update(struct.pack("<Q", len(points)))
    for x, y in points:
        digest.update(struct.pack("<dd", 0.0 if x == 0.0 else x, 0.0 if y == 0.0 else y))
    return digest.hexdigest()


def _staged_domain_union(manifest_path: Path, manifest: Mapping[str, Any]) -> Any:
    import shapefile
    from shapely.geometry import shape
    from shapely.ops import unary_union

    d = next(item for item in manifest["components"] if item["role"] == "domain_D")
    shp = manifest_path.parent / d["files"]["shp"]["path"]
    shx = manifest_path.parent / d["files"]["shx"]["path"]
    reader = shapefile.Reader(shp=str(shp), shx=str(shx))
    try:
        assert reader.dbf is None
        polygons = [shape(record.__geo_interface__) for record in reader.shapes()]
    finally:
        reader.close()
    return unary_union(polygons)


def test_manifest_1_1_schema_resource_and_v1_compatibility() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    old_schema_path = EXAMPLES / "domain_mesh_only_source_manifest.schema.json"
    new_schema_path = EXAMPLES / "domain_mesh_only_source_manifest_v1_1.schema.json"
    packaged_schema_path = ROOT / "hydrodynamics" / "schemas" / new_schema_path.name
    old_schema = json.loads(old_schema_path.read_text(encoding="utf-8"))
    new_schema = json.loads(new_schema_path.read_text(encoding="utf-8"))
    old_example = json.loads((EXAMPLES / "domain_mesh_only_source_manifest.example.json").read_text(encoding="utf-8"))
    new_example = _manifest_11()

    jsonschema.Draft202012Validator.check_schema(old_schema)
    jsonschema.Draft202012Validator.check_schema(new_schema)
    jsonschema.Draft202012Validator(old_schema).validate(old_example)
    jsonschema.Draft202012Validator(new_schema).validate(new_example)
    assert validate_source_manifest_shape(old_example) is old_example
    assert validate_source_manifest_shape(new_example) is new_example
    assert old_example["schema_version"] == "1.0"
    assert new_example["schema_version"] == "1.1"
    assert packaged_schema_path.read_bytes() == new_schema_path.read_bytes()
    assert packaged_schema_path.is_file()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["optional-dependencies"]["domain-mesh-only"]
    assert any(value.startswith("pyproj>=3.8") for value in dependencies)
    package_data = pyproject["tool"]["setuptools"]["package-data"]["hydrodynamics"]
    assert "schemas/*.json" in package_data


@pytest.mark.parametrize(
    ("mutation", "schema_accepts"),
    [
        ("missing_discriminator", False),
        ("external_files", False),
        ("self_reported_geometry_hash", False),
        ("self_reported_provenance_hash", False),
        ("caller_coordinates", False),
        ("q_zero", False),
        ("wrong_role", False),
        ("parent_is_role_text", True),
        ("parent_is_S", True),
        ("parent_missing", True),
        ("reserved_group", False),
        ("path_escape", False),
        ("duplicate_component_id", True),
        ("origin_equals_staged", True),
        ("missing_origin_identity", False),
        ("only_D_and_S", False),
    ],
)
def test_manifest_1_1_rejects_ambiguous_or_self_asserted_sources(mutation: str, schema_accepts: bool) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((EXAMPLES / "domain_mesh_only_source_manifest_v1_1.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    manifest = _manifest_11()
    derived = manifest["components"][2]
    if mutation == "missing_discriminator":
        derived.pop("source_kind")
    elif mutation == "external_files":
        derived["files"] = {}
    elif mutation == "self_reported_geometry_hash":
        derived["geometry_sha256"] = "a" * 64
    elif mutation == "self_reported_provenance_hash":
        derived["provenance_sha256"] = "b" * 64
    elif mutation == "caller_coordinates":
        derived["coordinates"] = [[0.0, 0.0], [1.0, 1.0]]
    elif mutation == "q_zero":
        derived["q_binding"] = 0
    elif mutation == "wrong_role":
        derived["role"] = "constraint_line"
    elif mutation == "parent_is_role_text":
        derived["parent_component_id"] = "domain_D"
    elif mutation == "parent_is_S":
        derived["parent_component_id"] = "S"
    elif mutation == "parent_missing":
        derived["parent_component_id"] = "missing_D"
    elif mutation == "reserved_group":
        derived["group_name"] = "hole_0"
    elif mutation == "path_escape":
        manifest["components"][0]["files"]["shp"]["origin"]["path"] = "../outside/domain_D.shp"
    elif mutation == "duplicate_component_id":
        derived["component_id"] = "D"
    elif mutation == "origin_equals_staged":
        manifest["components"][0]["files"]["shp"]["origin"]["path"] = manifest["components"][0]["files"]["shp"]["path"]
    elif mutation == "missing_origin_identity":
        manifest["components"][0]["files"]["shp"].pop("origin")
    elif mutation == "only_D_and_S":
        manifest["components"].pop()
    else:
        raise AssertionError(mutation)

    if schema_accepts:
        validator.validate(manifest)
    else:
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(manifest)
    with pytest.raises(ValueError):
        validate_source_manifest_shape(manifest)


def test_derived_D_exterior_is_exact_ordered_outer_ring_and_semantically_read_back(
    experiment_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hole_ccw = [[473516.0, 4335908.0], [473526.0, 4335908.0],
                [473526.0, 4335918.0], [473516.0, 4335918.0]]
    s_other = [[473576.0, 4335952.0], [473581.0, 4335952.0],
               [473581.0, 4335957.0], [473576.0, 4335957.0]]
    config_path, config, manifest_path, source_manifest = mesh_example.create_synthetic_domain_mesh_case(
        ROOT, experiment_root.relative_to(ROOT), run_id="derived-D-exterior-positive",
        d_polygons=[[mesh_example.OUTER[::-1], hole_ccw]],
        s_polygons=[[mesh_example.SECURITY], [s_other]],
        derived_domain_exterior=True, include_constraint=False,
    )
    log = _write_worker_sentinel(experiment_root, monkeypatch)
    completed = mesh_example.dispatch_restricted_worker_cli(config_path, ROOT)
    assert completed.returncode == 0, {"stdout": completed.stdout, "stderr": completed.stderr}
    assert not log.exists()

    d = next(item for item in source_manifest["components"] if item["role"] == "domain_D")
    derived = next(item for item in source_manifest["components"] if item["source_kind"] == "derived")
    assert d["component_id"] == "D"
    assert derived["parent_component_id"] == "D"
    assert "files" not in derived and "geometry_sha256" not in derived and "source_crs" not in derived
    assert len(source_manifest["components"]) == 3
    assert not list(manifest_path.parent.rglob("domain_boundary.geojson"))

    domain = _staged_domain_union(manifest_path, source_manifest)
    assert domain.geom_type == "Polygon" and len(domain.interiors) == 1
    expected_points = tuple((float(x), float(y)) for x, y in domain.exterior.coords)
    expected_hash = _expected_xy_hash(expected_points)
    hole_hash = _expected_xy_hash(tuple((float(x), float(y)) for x, y in domain.interiors[0].coords))
    assert expected_hash != hole_hash

    bundle = ROOT / config["output_root"] / config["run_id"]
    assert (bundle / "MESH_READY").is_file()
    bundle_manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    bundle_status = json.loads((bundle / "status.json").read_text(encoding="utf-8"))
    assert bundle_manifest["mesh_ready"] is True
    assert bundle_manifest["worker"]["job_active_processes_at_exit"] == 0
    assert bundle_status["status"] == "mesh_generation_diagnostic_complete"
    audit = json.loads((bundle / "artifacts" / "domain-source-audit.json").read_text(encoding="utf-8"))
    mesh_payload = json.loads((bundle / "artifacts" / "domain-mesh.json").read_text(encoding="utf-8"))
    worker_result = json.loads((bundle / "artifacts" / "worker-result.json").read_text(encoding="utf-8"))
    line = next(item for item in audit["source_components"] if item["component_id"] == "D-exterior")
    assert audit["format"] == "triangle-domain-mesh-only-source-audit-v2"
    assert line["role"] == "boundary_line" and line["source_kind"] == "derived"
    assert line["parent_component_id"] == "D"
    assert line["geometry_only"] is True and line["q_binding"] == "NONE" and line["closed"] is True
    assert line["ordered_point_count"] == len(expected_points) == 5
    assert line["geometry_hash_format"] == DOMAIN_D_EXTERIOR_GEOMETRY_HASH_FORMAT
    assert line["geometry_sha256"] == expected_hash
    assert line["provenance_sha256"] != line["geometry_sha256"]
    assert line["derivation_provenance"]["parent_component_role"] == "domain_D"
    assert line["derivation_provenance"]["parent_geometry_stage"] == "all_valid_transformed_D_features_then_unary_union_then_exterior"
    assert next(item for item in audit["source_components"] if item["component_id"] == "S")["source_feature_count"] == 2
    parent_D_audit = next(item for item in audit["source_components"] if item["component_id"] == "D")
    dimension = parent_D_audit["source_dimension_identity"]
    assert dimension["source_encoding"] == "shapefile"
    assert dimension["header_shape_type"] == 5
    assert dimension["record_shape_type_counts"] == {"5": 1}
    assert dimension["coordinate_dimension"] == 2
    parent_files = parent_D_audit["files"]
    assert len(parent_files) == 10
    assert {item["identity_kind"] for item in parent_files} == {"original_source", "staged_copy"}
    assert {item["role"] for item in parent_files} == {"shp", "shx", "prj", "dbf", "cpg"}
    assert "synthetic_D_exterior" in mesh_payload["boundary_groups"]
    assert worker_result["semantic_readback"]["semantic_readback_passed"] is True
    assert audit["dem_pixel_reader_calls"] == 0
    assert audit["terrain_mapper_calls"] == 0 and audit["solver_calls"] == 0
    assert not (bundle / "READY").exists() and not (bundle / "latest").exists()


@pytest.mark.skipif(os.name != "nt", reason="CRS operation chain and mesh generation require the Windows Job worker")
def test_derived_parent_records_actual_non_ballpark_CRS_operation(experiment_root: Path) -> None:
    from pyproj import CRS, Transformer

    target = CRS.from_epsg(4548)
    source = CRS.from_epsg(4490)
    inverse = Transformer.from_crs(target, source, always_xy=True, allow_ballpark=False, only_best=True)
    x0, y0 = 500000.0, 2501348.473180312
    target_outer = [[x0, y0], [x0, y0 + 1000.0], [x0 + 1000.0, y0 + 1000.0], [x0 + 1000.0, y0]]
    source_outer = [list(inverse.transform(x, y)) for x, y in target_outer]
    target_security = [[x0 + 200.0, y0 + 200.0], [x0 + 210.0, y0 + 200.0],
                       [x0 + 210.0, y0 + 210.0], [x0 + 200.0, y0 + 210.0]]
    config_path, _config, _manifest_path, _source_manifest = mesh_example.create_synthetic_domain_mesh_case(
        ROOT, experiment_root.relative_to(ROOT), run_id="derived-D-exterior-operation",
        d_polygons=[[source_outer]], d_prj_crs="EPSG:4490", d_source_crs="EPSG:4490",
        s_polygons=[[target_security]],
        derived_domain_exterior=True, include_constraint=False,
    )
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "mesh_generation_diagnostic_complete", result
    assert result.bundle_path is not None
    audit = json.loads((result.bundle_path / "artifacts" / "domain-source-audit.json").read_text(encoding="utf-8"))
    d = next(item for item in audit["source_components"] if item["component_id"] == "D")
    operation = d["coordinate_operation"]
    assert operation["coordinate_transform_applied"] is True
    assert operation["coordinate_array_axis_policy"] == "always_xy"
    assert operation["allow_ballpark"] is False and operation["only_best"] is True
    assert operation["best_available"] is True
    assert operation["ballpark_flag"] is False
    assert operation["ballpark_flags"] and all(type(flag) is bool and flag is False for flag in operation["ballpark_flags"])
    assert operation["operation_chain"] and all(
        item["has_ballpark_transformation"] is False for item in operation["operation_chain"]
    )
    assert operation["pyproj_version"] and operation["proj_version"]
    assert operation["pyshp_version"] and operation["shapely_version"] and operation["python_version"]
    assert operation["epsg_database_version"] and operation["epsg_database_date"]
    assert operation["proj_network_enabled"] is False
    assert len(operation["operation_identity_sha256"]) == 64


def test_derived_exterior_hash_enforces_fixed_point_budget() -> None:
    class OverLimitSequence:
        def __len__(self) -> int:
            return MAX_DERIVED_LINE_POINTS + 1
        def __getitem__(self, index: int) -> tuple[float, float]:
            raise AssertionError("the over-limit sequence must be rejected before reading any point")

    with pytest.raises(DomainMeshOnlyError, match="DERIVED_LINE_POINT_COUNT_LIMIT"):
        _canonical_domain_exterior_sha256(OverLimitSequence())  # type: ignore[arg-type]


@pytest.mark.skipif(os.name != "nt", reason="source identity drift is dispatched through the Windows Job worker")
@pytest.mark.parametrize("identity_kind", ["origin", "staged"])
@pytest.mark.parametrize("sidecar", ["shp", "shx", "dbf", "prj", "cpg"])
def test_each_derived_parent_sidecar_stat_drift_is_rejected(
    experiment_root: Path, identity_kind: str, sidecar: str
) -> None:
    config_path, config, manifest_path, manifest = mesh_example.create_synthetic_domain_mesh_case(
        ROOT, experiment_root.relative_to(ROOT), run_id=f"derived-D-stat-{identity_kind}-{sidecar}",
        derived_domain_exterior=True, include_constraint=False,
    )
    d = next(item for item in manifest["components"] if item["role"] == "domain_D")
    record = d["files"][sidecar]["origin"] if identity_kind == "origin" else d["files"][sidecar]
    path = manifest_path.parent / record["path"]
    path.write_bytes(path.read_bytes() + b"\x00")

    result = run_restricted_preparation(config_path, project_root=ROOT)
    _assert_failed_without_ready(
        result, config, status="source_identity_rejected", exit_code=54, worker_started=False
    )


@pytest.mark.skipif(os.name != "nt", reason="source identity hash drift is dispatched through the Windows Job worker")
def test_derived_parent_origin_hash_drift_fails_after_fresh_stat_and_manifest_binding(
    experiment_root: Path,
) -> None:
    config_path, config, manifest_path, manifest = mesh_example.create_synthetic_domain_mesh_case(
        ROOT, experiment_root.relative_to(ROOT), run_id="derived-D-origin-hash-drift",
        derived_domain_exterior=True, include_constraint=False,
    )
    d = next(item for item in manifest["components"] if item["role"] == "domain_D")
    origin = manifest_path.parent / d["files"]["dbf"]["origin"]["path"]
    data = bytearray(origin.read_bytes())
    data[-1] ^= 1
    origin.write_bytes(data)
    d["files"]["dbf"]["origin"]["expected_stat_identity"] = restricted_worker._stat_identity(origin)
    _refresh_manifest_and_config(config_path, config, manifest_path, manifest)

    result = run_restricted_preparation(config_path, project_root=ROOT)
    failure = _assert_failed_without_ready(
        result, config, status="source_identity_rejected", exit_code=54, worker_started=True
    )
    assert "bytes differ from their manifest identity" in failure["message"]


@pytest.mark.skipif(os.name != "nt", reason="multipart D refusal is checked in the bounded geometry worker")
def test_derived_exterior_fails_closed_for_multipart_D(experiment_root: Path) -> None:
    rectangle_a = [[473506.0, 4335888.0], [473546.0, 4335888.0],
                   [473546.0, 4335928.0], [473506.0, 4335928.0]]
    rectangle_b = [[473566.0, 4335948.0], [473606.0, 4335948.0],
                   [473606.0, 4335988.0], [473566.0, 4335988.0]]
    config_path, config, _manifest_path, _manifest = mesh_example.create_synthetic_domain_mesh_case(
        ROOT, experiment_root.relative_to(ROOT), run_id="derived-D-multipart-refused",
        d_polygons=[[rectangle_a], [rectangle_b]],
        derived_domain_exterior=True, include_constraint=False,
    )
    result = run_restricted_preparation(config_path, project_root=ROOT)
    failure = _assert_failed_without_ready(
        result, config, status="worker_failed", exit_code=53, worker_started=True
    )
    assert "UNSUPPORTED_MULTIPOLYGON_DOMAIN_D" in failure["message"]


@pytest.mark.skipif(os.name != "nt", reason="D-exterior coverage is checked inside the bounded geometry worker")
def test_derived_exterior_refuses_S_that_cuts_the_parent_D_boundary(experiment_root: Path) -> None:
    x0, y0 = mesh_example.OUTER[0]
    x1 = mesh_example.OUTER[1][0]
    touching_s = [[x1 - 10.0, y0 + 30.0], [x1, y0 + 30.0],
                  [x1, y0 + 40.0], [x1 - 10.0, y0 + 40.0]]
    config_path, config, _manifest_path, _manifest = mesh_example.create_synthetic_domain_mesh_case(
        ROOT, experiment_root.relative_to(ROOT), run_id="derived-D-exterior-S-touch-refused",
        s_polygons=[[touching_s]], derived_domain_exterior=True, include_constraint=False,
    )
    result = run_restricted_preparation(config_path, project_root=ROOT)
    failure = _assert_failed_without_ready(
        result, config, status="worker_failed", exit_code=53, worker_started=True
    )
    assert "D−S/line geometry is not representable" in failure["message"]


@pytest.mark.skipif(os.name != "nt", reason="semantic audit verification is performed inside the Job worker")
@pytest.mark.parametrize("forge_mode", ["geometry", "geometry_reverse", "parent", "rule", "operation"])
def test_semantic_reader_rejects_resigned_derived_audit_forgery(
    experiment_root: Path, monkeypatch: pytest.MonkeyPatch, forge_mode: str
) -> None:
    config_path, config, manifest_path, manifest = mesh_example.create_synthetic_domain_mesh_case(
        ROOT, experiment_root.relative_to(ROOT), run_id=f"derived-D-audit-forgery-{forge_mode}",
        derived_domain_exterior=True, include_constraint=False,
    )
    forged_geometry_sha256 = None
    if forge_mode == "geometry":
        points = tuple((float(x), float(y)) for x, y in _staged_domain_union(manifest_path, manifest).exterior.coords)
        rotated = points[1:-1] + points[:1]
        forged_geometry_sha256 = _expected_xy_hash(rotated + (rotated[0],))
        assert forged_geometry_sha256 != _expected_xy_hash(points)
    elif forge_mode == "geometry_reverse":
        points = tuple((float(x), float(y)) for x, y in _staged_domain_union(manifest_path, manifest).exterior.coords)
        reversed_points = tuple(reversed(points[:-1]))
        forged_geometry_sha256 = _expected_xy_hash(reversed_points + (reversed_points[0],))
        assert forged_geometry_sha256 != _expected_xy_hash(points)
    _write_worker_sentinel(
        experiment_root, monkeypatch, forge_mode=forge_mode,
        forged_geometry_sha256=forged_geometry_sha256,
    )
    result = run_restricted_preparation(config_path, project_root=ROOT)
    failure = _assert_failed_without_ready(
        result, config, status="worker_failed", exit_code=53, worker_started=True
    )
    assert "source audit differs from the trusted source/config recomputation" in failure["message"]

