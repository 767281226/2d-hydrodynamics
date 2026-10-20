from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import netCDF4
import pytest
import shapefile
import yaml
from pydantic import ValidationError

from hydrodynamics.mesh_preparation import write_ugrid_preparation
from hydrodynamics.triangular_business import (
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    write_triangular_business_package,
)
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh
from hydrodynamics.triangular_shp_export import (
    NativeTriangleSHPError,
    NativeTriangleSHPExportRequest,
    export_native_triangle_shp,
    main,
    package_schema,
    validate_native_triangle_shp_package,
)
from hydrodynamics.triangular_solver import run_triangular_solver


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_native_case(root: Path, *, depth_m: float = 0.2, output_name: str = "package", target_crs: str = "NATIVE",
                       aggregation: str = "FRAME", scope_selection: str = "FULL_DOMAIN", threshold_m: float = 0.0) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    face_ids = [9_007_199_254_740_993, 9_007_199_254_740_995]
    mesh = TriangularMesh(
        node_xy=[(500_000.0, 4_300_000.0), (500_002.0, 4_300_000.0),
                 (500_002.0, 4_300_001.0), (500_000.0, 4_300_001.0)],
        face_nodes=[(0, 1, 2), (0, 2, 3)], coordinate_system="EPSG:32650",
        mesh_id="native-shp-two-face-v1", face_id=face_ids,
        boundary_groups={"outer_boundary": ((0, 1), (1, 2), (2, 3), (3, 0))},
    )
    terrain = FaceTerrainField(
        mesh_id=mesh.mesh_id or "", elevation_m=(0.0, 0.0), valid_mask=(True, True),
        covered_area_m2=tuple(mesh.face_area), valid_area_m2=tuple(mesh.face_area),
        coverage_ratio=(1.0, 1.0), coordinate_system=mesh.coordinate_system,
        vertical_datum="Native SHP synthetic local datum", vertical_datum_source="config_declaration",
    )
    mesh_path = root / "mesh.nc"
    write_ugrid_preparation(mesh_path, mesh, terrain)
    initial = TriangularInitialConditionField.from_assignments(
        mesh, terrain, "depth", tuple((face_id, depth_m) for face_id in face_ids),
        source="small native triangle SHP fixture",
    )
    business = TriangularBusinessPackage(
        mesh, terrain,
        TriangularRoughnessField.from_constant(mesh, 0.035, source="test provenance only"),
        initial, (),
    )
    business_path = root / "business.json"
    write_triangular_business_package(business_path, business, mesh_artifact=mesh_path)
    run_config_path = root / "run.yaml"
    run_config_path.write_text(yaml.safe_dump({
        "format": "triangular-solver-run-v1", "schema_version": "1.0",
        "case_id": "native-triangle-shp-two-face-v1", "business_file": business_path.name,
        "result_file": "result.nc", "time_origin": None, "output_times_s": [0.0, 0.5, 1.0],
        "boundary_policy": "closed_wall", "initial_momentum": "zero", "sources": "none",
        "gravity_m_s2": 9.81, "cfl_number": 0.25, "minimum_time_step_s": 1e-6,
        "maximum_time_step_s": 0.1, "maximum_step_retries": 8, "maximum_face_count": 64,
    }, sort_keys=False), encoding="utf-8")
    if aggregation == "FRAME":
        selector = {"aggregation": "FRAME", "frame_index": 1, "time_s": 0.5,
                    "window_start_s": None, "window_end_s": None}
    else:
        selector = {"aggregation": "MAX_OVER_WINDOW", "frame_index": None, "time_s": None,
                    "window_start_s": 0.0, "window_end_s": 1.0}
    request_path = root / "export_request.yaml"
    request_path.write_text(yaml.safe_dump({
        "format": "native-triangle-result-shp-export-request-v1", "schema_version": "1.0",
        "run_config_file": run_config_path.name, "output_directory": output_name,
        "selector": selector, "scope_selection": scope_selection,
        "wet_depth_threshold_m": threshold_m, "target_crs": target_crs,
    }, sort_keys=False), encoding="utf-8")
    run_triangular_solver(run_config_path)
    return {"root": root, "mesh": mesh_path, "business": business_path, "run_config": run_config_path,
            "request": request_path, "result": root / "result.nc", "package": root / output_name}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_shapefile_component(package: Path, temp: Path, *, component: str) -> None:
    original = shapefile.Reader(str(package / "faces.shp"), encoding="utf-8")
    original_shapes = original.shapes()
    original_records = original.records()
    original_fields = list(original.fields[1:])
    original.close()
    writer = shapefile.Writer(str(temp / "altered"), shapeType=shapefile.POLYGON, encoding="utf-8")
    for field in original_fields:
        writer.field(field[0], field[1], size=field[2], decimal=field[3])
    for index, (shape, record) in enumerate(zip(original_shapes, original_records)):
        points = list(shape.points)
        values = list(record)
        if index == 0 and component == "shp":
            points[1] = (points[1][0] + 0.1, points[1][1])
        if index == 0 and component == "dbf":
            values[2] = 0.9
        writer.poly([points])
        writer.record(*values)
    writer.close()
    extensions = {"dbf": (".dbf",), "shp": (".shp", ".shx")}[component]
    for extension in extensions:
        raw = (temp / f"altered{extension}").read_bytes()
        (package / f"faces{extension}").write_bytes(raw)


def _refresh_manifest_file_digest(package: Path, key: str) -> None:
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw = (package / key).read_bytes()
    record = next(item for item in manifest["files"] if item["key"] == key)
    record["sha256"] = hashlib.sha256(raw).hexdigest()
    record["byte_count"] = len(raw)
    _write_json(manifest_path, manifest)


def test_frame_native_export_has_exact_face_ids_units_and_triangles(tmp_path):
    case = _build_native_case(tmp_path / "frame")
    source_hashes = {key: _sha(case[key]) for key in ("mesh", "business", "run_config", "request", "result")}
    report = export_native_triangle_shp(case["request"])
    assert report.valid and report.complete and report.feature_count == 2
    shp_path = case["package"] / "faces.shp"
    reader = shapefile.Reader(str(shp_path), encoding="utf-8")
    assert reader.shapeType == shapefile.POLYGON
    assert [field[0] for field in reader.fields[1:]] == ["face_id", "time_s", "depth_m", "water_lvl", "ux_m_s", "uy_m_s", "area_m2"]
    records = [list(record) for record in reader.records()]
    assert [record[0].strip() for record in records] == ["9007199254740993", "9007199254740995"]
    assert all(record[1] == 0.5 and record[2] == 0.2 and record[3] == 0.2 for record in records)
    assert all(record[4] == 0.0 and record[5] == 0.0 and record[6] == 1.0 for record in records)
    assert all(len(shape.points) == 4 and shape.points[0] == shape.points[-1] and len(set(shape.points[:-1])) == 3 for shape in reader.shapes())
    manifest = json.loads((case["package"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["experimental"] is True
    assert manifest["engineering_validated"] is False and manifest["can_publish"] is False
    assert manifest["face_id_encoding"] == "exact_decimal_text"
    assert manifest["vertical_datum"] == "Native SHP synthetic local datum"
    assert {item["key"] for item in manifest["files"]} == {"faces.shp", "faces.shx", "faces.dbf", "faces.prj", "faces.cpg"}
    assert validate_native_triangle_shp_package(case["request"]).valid
    assert {key: _sha(case[key]) for key in source_hashes} == source_hashes


def test_max_window_reprojects_geometry_but_keeps_native_square_metre_area(tmp_path):
    case = _build_native_case(tmp_path / "max", target_crs="EPSG:4490", aggregation="MAX_OVER_WINDOW",
                              scope_selection="MAXIMUM_WET_SURFACE", threshold_m=0.001)
    report = export_native_triangle_shp(case["request"])
    assert report.valid and report.feature_count == 2
    manifest = json.loads((case["package"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["target_crs"] == "EPSG:4490"
    assert manifest["selector"]["window_start_s"] == 0.0 and manifest["selector"]["window_end_s"] == 1.0
    reader = shapefile.Reader(str(case["package"] / "faces.shp"), encoding="utf-8")
    records = [list(record) for record in reader.records()]
    assert all(record[1] == 0.2 and record[2] == 0.0 and record[3] == 1.0 for record in records)
    from pyproj import CRS
    assert CRS.from_wkt((case["package"] / "faces.prj").read_text(encoding="utf-8")).to_epsg() == 4490
    assert all(100.0 < point[0] < 130.0 and 20.0 < point[1] < 50.0 for shape in reader.shapes() for point in shape.points)
    assert validate_native_triangle_shp_package(case["request"]).complete


def test_maximum_wet_surface_can_be_a_valid_empty_triangle_layer(tmp_path):
    case = _build_native_case(tmp_path / "empty", scope_selection="MAXIMUM_WET_SURFACE", threshold_m=0.3)
    report = export_native_triangle_shp(case["request"])
    assert report.valid and report.complete and report.feature_count == 0
    reader = shapefile.Reader(str(case["package"] / "faces.shp"), encoding="utf-8")
    assert reader.shapeType == shapefile.POLYGON
    assert len(reader.shapes()) == 0 and len(reader.records()) == 0
    assert validate_native_triangle_shp_package(case["request"]).valid


def test_all_dry_native_result_still_exports_a_valid_empty_wet_layer(tmp_path):
    case = _build_native_case(tmp_path / "all-dry", depth_m=0.0,
                              scope_selection="MAXIMUM_WET_SURFACE", threshold_m=0.0)
    report = export_native_triangle_shp(case["request"])
    assert report.valid and report.complete and report.feature_count == 0
    reader = shapefile.Reader(str(case["package"] / "faces.shp"), encoding="utf-8")
    assert reader.shapeType == shapefile.POLYGON and reader.shapes() == [] and reader.records() == []
    assert validate_native_triangle_shp_package(case["request"]).valid


@pytest.mark.parametrize("selector,match", [
    ({"aggregation": "FRAME", "frame_index": 1, "time_s": 0.4, "window_start_s": None, "window_end_s": None}, "exactly equal"),
    ({"aggregation": "MAX_OVER_WINDOW", "frame_index": None, "time_s": None, "window_start_s": 0.25, "window_end_s": 1.0}, "exactly match"),
])
def test_selector_must_match_native_saved_time_axis(tmp_path, selector, match):
    case = _build_native_case(tmp_path / "bad-selector")
    request = yaml.safe_load(case["request"].read_text(encoding="utf-8"))
    request["selector"] = selector
    case["request"].write_text(yaml.safe_dump(request, sort_keys=False), encoding="utf-8")
    with pytest.raises(NativeTriangleSHPError, match=match):
        export_native_triangle_shp(case["request"])
    assert not case["package"].exists()


@pytest.mark.parametrize(("component", "match"), [
    ("dbf", "DBF numeric field depth_m"),
    ("shp", "SHP feature geometry differs"),
    ("prj", "faces.prj CRS differs"),
])
def test_validator_rejects_tampered_sides_even_when_component_digest_is_recomputed(tmp_path, component, match):
    case = _build_native_case(tmp_path / f"tampered-{component}")
    export_native_triangle_shp(case["request"])
    if component in {"dbf", "shp"}:
        _rewrite_shapefile_component(case["package"], tmp_path, component=component)
        for key in (("faces.dbf",) if component == "dbf" else ("faces.shp", "faces.shx")):
            _refresh_manifest_file_digest(case["package"], key)
    else:
        from pyproj import CRS
        (case["package"] / "faces.prj").write_text(CRS.from_epsg(4326).to_wkt(version="WKT1_ESRI"), encoding="utf-8")
        _refresh_manifest_file_digest(case["package"], "faces.prj")
    with pytest.raises(NativeTriangleSHPError, match=match):
        validate_native_triangle_shp_package(case["request"])


def test_export_rejects_existing_directory_and_output_traversal(tmp_path):
    case = _build_native_case(tmp_path / "paths")
    case["package"].mkdir()
    sentinel = case["package"] / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(NativeTriangleSHPError, match="overwrite"):
        export_native_triangle_shp(case["request"])
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    request = yaml.safe_load(case["request"].read_text(encoding="utf-8"))
    request["output_directory"] = "../escape"
    with pytest.raises(ValidationError):
        NativeTriangleSHPExportRequest.model_validate(request)


def test_mutated_native_depth_is_rejected_by_source_reader(tmp_path):
    case = _build_native_case(tmp_path / "native-mutation")
    export_native_triangle_shp(case["request"])
    with netCDF4.Dataset(case["result"], "r+") as dataset:
        values = dataset.variables["water_depth"][:]
        values[1, 0] = 0.9
        dataset.variables["water_depth"][:] = values
    with pytest.raises(NativeTriangleSHPError):
        validate_native_triangle_shp_package(case["request"])


def test_post_publish_modified_manifest_is_recovered_before_marker_removal(tmp_path, monkeypatch):
    import hydrodynamics.triangular_shp_export as shp_module

    case = _build_native_case(tmp_path / "manifest-race")
    destination = case["package"]
    original_rename = shp_module.os.rename
    changed_bytes: dict[str, bytes] = {}

    def rename_then_modify_manifest(source, target):
        result = original_rename(source, target)
        if Path(target).resolve() == destination.resolve():
            manifest_path = destination / "manifest.json"
            payload = json.loads(manifest_path.read_bytes())
            payload["concurrent_user_note"] = "preserve these exact bytes"
            changed_bytes["manifest"] = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
            manifest_path.write_bytes(changed_bytes["manifest"])
        return result

    monkeypatch.setattr(shp_module.os, "rename", rename_then_modify_manifest)
    with pytest.raises(NativeTriangleSHPError, match="manifest.recovered"):
        shp_module.export_native_triangle_shp(case["request"])
    recoveries = list(destination.glob("manifest.recovered.*.json"))
    assert not (destination / "manifest.json").exists()
    assert len(recoveries) == 1 and recoveries[0].read_bytes() == changed_bytes["manifest"]


def test_post_publish_replaced_root_is_preserved_and_reported(tmp_path, monkeypatch):
    import hydrodynamics.triangular_shp_export as shp_module

    case = _build_native_case(tmp_path / "root-race")
    destination = case["package"]
    detached = case["root"] / "detached-original-package"
    original_rename = shp_module.os.rename
    replaced_bytes: dict[str, bytes] = {}

    def rename_then_replace_root(source, target):
        result = original_rename(source, target)
        if Path(target).resolve() == destination.resolve():
            original = destination / "manifest.json"
            replaced_bytes["manifest"] = original.read_bytes()
            original_rename(destination, detached)
            destination.mkdir()
            (destination / "manifest.json").write_bytes(replaced_bytes["manifest"])
            (destination / "foreign.keep").write_bytes(b"leave this replacement tree untouched")
        return result

    monkeypatch.setattr(shp_module.os, "rename", rename_then_replace_root)
    with pytest.raises(NativeTriangleSHPError, match="unowned/replaced identity"):
        shp_module.export_native_triangle_shp(case["request"])
    assert (destination / "manifest.json").read_bytes() == replaced_bytes["manifest"]
    assert (destination / "foreign.keep").read_bytes() == b"leave this replacement tree untouched"
    assert (detached / "manifest.json").read_bytes() == replaced_bytes["manifest"]


def test_prepublication_failure_does_not_delete_a_replaced_stage_root(tmp_path, monkeypatch):
    import hydrodynamics.triangular_shp_export as shp_module

    case = _build_native_case(tmp_path / "stage-root-race")
    moved = case["root"] / "moved-original-stage"
    original_writer = shp_module._write_layer
    observed: dict[str, Path] = {}

    def write_then_replace_stage(stage, context, built):
        original_writer(stage, context, built)
        stage = Path(stage)
        shp_module.os.rename(stage, moved)
        stage.mkdir()
        (stage / "independent_foreign_file.txt").write_bytes(b"preserve this unrelated concurrent stage root")
        observed["stage"] = stage
        raise NativeTriangleSHPError("test write failure after concurrent stage-root replacement")

    monkeypatch.setattr(shp_module, "_write_layer", write_then_replace_stage)
    with pytest.raises(NativeTriangleSHPError, match="replaced directory"):
        export_native_triangle_shp(case["request"])

    assert not case["package"].exists()
    assert (moved / "faces.shp").is_file()
    assert (observed["stage"] / "independent_foreign_file.txt").read_bytes() == b"preserve this unrelated concurrent stage root"


def test_owned_failed_stage_retires_marker_but_preserves_concurrent_file(tmp_path, monkeypatch):
    import hydrodynamics.triangular_shp_export as shp_module

    case = _build_native_case(tmp_path / "stage-owned-file-race")
    observed: dict[str, Path] = {}

    def add_foreign_file_then_fail(request_path, package_directory=None):
        stage = Path(package_directory)
        observed["stage"] = stage
        (stage / "concurrent.keep").write_bytes(b"preserve this concurrent file")
        raise NativeTriangleSHPError("synthetic stage validation failure")

    monkeypatch.setattr(shp_module, "validate_native_triangle_shp_package", add_foreign_file_then_fail)
    with pytest.raises(NativeTriangleSHPError, match="success marker was safely retired"):
        export_native_triangle_shp(case["request"])

    stage = observed["stage"]
    assert not case["package"].exists()
    assert not (stage / "manifest.json").exists()
    assert (stage / "concurrent.keep").read_bytes() == b"preserve this concurrent file"
    assert (stage / "faces.shp").is_file()


def test_request_drift_during_stage_validation_leaves_no_success_marker(tmp_path, monkeypatch):
    import hydrodynamics.triangular_shp_export as shp_module

    case = _build_native_case(tmp_path / "stage-request-drift")
    original_validator = shp_module.validate_native_triangle_shp_package
    observed: dict[str, Path] = {}

    def drift_request_then_validate(request_path, package_directory=None):
        stage = Path(package_directory)
        observed["stage"] = stage
        case["request"].write_bytes(case["request"].read_bytes() + b"\n# changed during stage validation\n")
        return original_validator(request_path, package_directory)

    monkeypatch.setattr(shp_module, "validate_native_triangle_shp_package", drift_request_then_validate)
    with pytest.raises(NativeTriangleSHPError):
        export_native_triangle_shp(case["request"])

    stage = observed["stage"]
    assert not case["package"].exists()
    assert stage.is_dir()
    assert not (stage / "manifest.json").exists()


def test_manifest_schema_and_example_request_are_packaged_in_wheel(tmp_path):
    from setuptools.build_meta import build_wheel
    import hydrodynamics.triangular_shp_export as shp_module

    example = Path(__file__).resolve().parents[1] / "examples/native_triangle_shp/export_frame.yaml"
    request = yaml.safe_load(example.read_text(encoding="utf-8"))
    NativeTriangleSHPExportRequest.model_validate(request)
    schema_resource = Path(__file__).resolve().parents[1] / "hydrodynamics/shapefile_schemas/triangular_result_shp.schema.json"
    assert json.loads(schema_resource.read_text(encoding="utf-8")) == package_schema()
    assert shp_module.main(["schema"]) == 0
    wheel_name = build_wheel(str(tmp_path))
    with zipfile.ZipFile(tmp_path / wheel_name) as archive:
        assert "hydrodynamics/triangular_shp_export.py" in archive.namelist()
        assert "hydrodynamics/shapefile_schemas/triangular_result_shp.schema.json" in archive.namelist()
        metadata = next(name for name in archive.namelist() if name.endswith("entry_points.txt"))
        assert "hydrodynamics-triangle-shp = hydrodynamics.triangular_shp_export:main" in archive.read(metadata).decode("utf-8")
