import json
import hashlib
from pathlib import Path
import shutil

import pytest
import yaml

from hydrodynamics.mesh_preparation import prepare_mesh
from hydrodynamics.triangular_business import prepare_triangular_business
from hydrodynamics.triangular_manning import run_triangular_manning
from hydrodynamics.triangular_manning_netcdf import read_triangular_manning_result
from hydrodynamics.triangular_shp_export import (
    NativeTriangleSHPError,
    NativeSourceTriangleSHPExportRequest,
    export_native_source_triangle_shp,
    export_native_triangle_shp,
    main,
    package_schema,
    source_package_schema,
    validate_native_source_triangle_shp_package,
)
from hydrodynamics.triangular_source import run_triangular_source
from hydrodynamics.triangular_source_netcdf import read_triangular_source_result
from hydrodynamics.triangular_sources import prepare_triangular_sources


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "security_exclusion"


def _prepare_source_example(tmp_path: Path) -> Path:
    case = tmp_path / "native-source-shp"
    shutil.copytree(EXAMPLE, case)
    prepare_mesh(case / "mesh_preparation.yaml")
    prepare_triangular_business(case / "business_preparation.yaml")
    prepare_triangular_business(case / "business_dynamic_preparation.yaml")
    prepare_triangular_business(case / "business_dry_preparation.yaml")
    return case


def _tamper_dbf_and_rehash_manifest(package: Path, tmp_path: Path) -> None:
    import shapefile

    reader = shapefile.Reader(str(package / "faces.shp"), encoding="utf-8")
    shapes = reader.shapes()
    records = [list(row) for row in reader.records()]
    fields = list(reader.fields[1:])
    shape_type = reader.shapeType
    reader.close()
    records[0][2] = 0.9
    writer = shapefile.Writer(str(tmp_path / "tampered"), shapeType=shape_type, encoding="utf-8")
    for field in fields:
        writer.field(field[0], field[1], size=field[2], decimal=field[3])
    for shape, row in zip(shapes, records):
        writer.poly([list(shape.points)])
        writer.record(*row)
    writer.close()
    (package / "faces.dbf").write_bytes((tmp_path / "tampered.dbf").read_bytes())
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dbf = (package / "faces.dbf").read_bytes()
    file_record = next(item for item in manifest["files"] if item["key"] == "faces.dbf")
    file_record["sha256"] = hashlib.sha256(dbf).hexdigest()
    file_record["byte_count"] = len(dbf)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_native_source_v2_exports_all_families_with_exact_graph_and_budgets(tmp_path):
    case = _prepare_source_example(tmp_path)

    # Source-only routes use the base config as an input contract. Its result
    # files are intentionally absent and must remain absent throughout export.
    prepare_triangular_sources(case / "source_preparation.yaml")
    run_triangular_source(case / "source_run.yaml")
    rainfall_result = read_triangular_source_result(case / "rainfall.nc", case / "source_run.yaml")
    assert rainfall_result.rainfall_nonzero_effect_applied is True
    assert not (case / "result.nc").exists()

    prepare_triangular_sources(case / "manning_zero_source_preparation.yaml")
    run_triangular_manning(case / "manning_zero_run.yaml")
    manning_zero = read_triangular_manning_result(case / "manning_zero.nc", case / "manning_zero_run.yaml")
    assert manning_zero.manning_applied is True
    assert manning_zero.nonzero_manning_effect_applied is False
    assert not (case / "base_none_dry.nc").exists()

    prepare_triangular_sources(case / "manning_source_preparation.yaml")
    run_triangular_manning(case / "manning_run.yaml")
    manning_result = read_triangular_manning_result(case / "manning.nc", case / "manning_run.yaml")
    assert manning_result.nonzero_manning_effect_applied is True
    assert not (case / "base_none_dynamic.nc").exists()

    prepare_triangular_sources(case / "rainfall_zero_source_preparation.yaml")
    run_triangular_source(case / "rainfall_zero_run.yaml")
    rainfall_zero = read_triangular_source_result(case / "rainfall_zero.nc", case / "rainfall_zero_run.yaml")
    assert rainfall_zero.rainfall_applied is True
    assert rainfall_zero.rainfall_nonzero_effect_applied is False

    prepare_triangular_sources(case / "combined_source_preparation.yaml")
    run_triangular_source(case / "combined_run.yaml")
    combined_result = read_triangular_source_result(case / "combined.nc", case / "combined_run.yaml")
    assert combined_result.nonzero_manning_effect_applied is True
    assert combined_result.rainfall_nonzero_effect_applied is True

    assert package_schema()["format"] == "native-triangle-shp-schema-set-v1"
    assert source_package_schema()["format"] == "native-triangle-shp-schema-set-v2"
    v2_schema_path = Path(__file__).resolve().parents[1] / "hydrodynamics" / "shapefile_schemas" / "triangular_source_result_shp_v2.schema.json"
    assert json.loads(v2_schema_path.read_text(encoding="utf-8")) == source_package_schema()
    cases = (
        ("export_manning_zero_v2.yaml", "manning", manning_zero, False, False),
        ("export_manning_v2.yaml", "manning", manning_result, True, False),
        ("export_rainfall_zero_v2.yaml", "rainfall", rainfall_zero, False, False),
        ("export_rainfall_v2.yaml", "rainfall", rainfall_result, False, True),
        ("export_combined_v2.yaml", "manning_and_rainfall", combined_result, True, True),
    )
    result_set_ids = set()
    for request_name, kind, native_result, has_manning_effect, has_rainfall_effect in cases:
        request_path = case / request_name
        parsed_request = NativeSourceTriangleSHPExportRequest.model_validate_json(
            json.dumps(__import__("yaml").safe_load(request_path.read_text(encoding="utf-8")))
        )
        assert parsed_request.result_kind == kind
        if request_name == "export_manning_zero_v2.yaml":
            assert main(["export", "--request", str(request_path)]) == 0
            report = validate_native_source_triangle_shp_package(request_path)
            assert main(["validate", "--request", str(request_path)]) == 0
        else:
            report = export_native_source_triangle_shp(request_path)
        assert report.valid and report.complete and report.feature_count == 4
        package = case / parsed_request.output_directory
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["format"] == "native-triangular-result-shapefile-v2"
        assert manifest["result_kind"] == kind
        assert manifest["native_format"] == native_result.format
        assert manifest["native_product_type"] == native_result.product_type
        assert manifest["solver_version"] == native_result.solver_version
        assert manifest["sources_applied"] == kind
        assert manifest["nonzero_manning_effect_applied"] is has_manning_effect
        assert manifest["rainfall_nonzero_effect_applied"] is has_rainfall_effect
        assert manifest["native_face_count"] == len(native_result.face_id) == 4
        assert len(manifest["frame_budgets"]) == len(native_result.time_s) == 3
        assert manifest["source_graph_sha256"]
        roles = {item["role"] for item in manifest["source_graph"]}
        assert {"request", "top_level_run_config", "base_run_config", "source_config", "source_preparation",
                "p3_business", "p2_mesh", "native_result", "security_input_config", "security_geometry",
                "security_domain"}.issubset(roles)
        assert "base_none_dynamic.nc" not in {Path(item["path"]).name for item in manifest["source_graph"]}
        assert manifest["security_exclusion_evidence"] is not None
        assert manifest["security_exclusion_sha256"]
        assert manifest["security_active_face_id_sha256"] == manifest["native_face_id_sha256"]
        if kind in {"rainfall", "manning_and_rainfall"}:
            summary = manifest["source_summary"]
            assert summary["rainfall_accepted_active_volume_m3_by_interval"] == pytest.approx(
                native_result.rainfall_volume_m3_by_interval,
            )
            assert summary["rainfall_excluded_unmodeled_volume_m3_by_interval"] == pytest.approx(
                native_result.security_rainfall_accounting.security_excluded_unmodeled_rainfall_volume_m3_by_interval,
            )
            assert summary["excluded_rain_is_accepted_budget_loss"] is False
            assert summary["automatic_flow_transfer_from_excluded_area"] is False
        assert validate_native_source_triangle_shp_package(request_path).valid
        assert manifest["result_set_id"] not in result_set_ids
        result_set_ids.add(manifest["result_set_id"])

    zero_manning_package = case / "manning_zero_native_shp_v2"
    _tamper_dbf_and_rehash_manifest(zero_manning_package, tmp_path)
    with pytest.raises(NativeTriangleSHPError, match="DBF"):
        validate_native_source_triangle_shp_package(case / "export_manning_zero_v2.yaml")

    wrong_family = yaml.safe_load((case / "export_manning_v2.yaml").read_text(encoding="utf-8"))
    wrong_family["result_kind"] = "rainfall"
    wrong_family["output_directory"] = "wrong_family_shp"
    wrong_family_path = case / "wrong_family.yaml"
    wrong_family_path.write_text(yaml.safe_dump(wrong_family), encoding="utf-8")
    with pytest.raises(NativeTriangleSHPError, match="sources"):
        export_native_source_triangle_shp(wrong_family_path)
    assert not (case / "wrong_family_shp").exists()

    source_config_path = case / "source_preparation.yaml"
    source_config_path.write_text(source_config_path.read_text(encoding="utf-8") + "\n# source drift\n", encoding="utf-8")
    with pytest.raises(NativeTriangleSHPError):
        validate_native_source_triangle_shp_package(case / "export_rainfall_v2.yaml")

    legacy_source_request = case / "legacy_manning_request.yaml"
    legacy_source_request.write_text(
        "format: native-triangle-result-shp-export-request-v1\n"
        "schema_version: '1.0'\nrun_config_file: manning_run.yaml\n"
        "output_directory: legacy_manning_shp\n"
        "selector: {aggregation: FRAME, frame_index: 2, time_s: 0.1, window_start_s: null, window_end_s: null}\n"
        "scope_selection: FULL_DOMAIN\nwet_depth_threshold_m: 0.0\ntarget_crs: NATIVE\n",
        encoding="utf-8",
    )
    with pytest.raises((NativeTriangleSHPError, ValueError)):
        export_native_triangle_shp(legacy_source_request)
    assert not (case / "legacy_manning_shp").exists()


def test_native_source_v2_post_publish_validation_failure_retires_marker_and_raises(tmp_path, monkeypatch):
    import hydrodynamics.triangular_shp_export as shp_module

    case = _prepare_source_example(tmp_path)
    prepare_triangular_sources(case / "source_preparation.yaml")
    run_triangular_source(case / "source_run.yaml")
    request = case / "export_rainfall_v2.yaml"
    output = case / "rainfall_native_shp_v2"
    original_validator = shp_module.validate_native_source_triangle_shp_package

    def fail_after_final_validation(request_path, package_directory=None):
        report = original_validator(request_path, package_directory)
        if package_directory is not None and Path(package_directory).resolve() == output.resolve():
            raise NativeTriangleSHPError("injected post-publish source SHP validation failure")
        return report

    monkeypatch.setattr(shp_module, "validate_native_source_triangle_shp_package", fail_after_final_validation)
    with pytest.raises(NativeTriangleSHPError, match="owned success marker was safely retired"):
        shp_module.export_native_source_triangle_shp(request)
    assert output.is_dir()
    assert not (output / "manifest.json").exists()
    assert (output / "faces.shp").is_file()
