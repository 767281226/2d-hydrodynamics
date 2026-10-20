import json
from pathlib import Path
import shutil

import pytest
import yaml

from hydrodynamics.mesh_preparation import (
    prepare_mesh,
    read_ugrid_preparation,
    read_ugrid_security_exclusion,
)
from hydrodynamics.nc2web import Nc2WebError, convert_netcdf_to_step_json, convert_netcdf_to_web
from hydrodynamics.security_exclusion import SecurityExclusionError, SecurityExclusionInput, load_security_area_geometry
from hydrodynamics.triangular_business import prepare_triangular_business
from hydrodynamics.triangular_shp_export import export_native_triangle_shp
from hydrodynamics.triangular_solver import run_triangular_solver
from hydrodynamics.triangular_solver_netcdf import read_triangular_result
from hydrodynamics.triangular_source import run_triangular_source
from hydrodynamics.triangular_source_netcdf import read_triangular_source_result
from hydrodynamics.triangular_sources import prepare_triangular_sources


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "security_exclusion"


def _copy_example(tmp_path: Path) -> Path:
    case = tmp_path / "security-exclusion"
    shutil.copytree(EXAMPLE, case)
    return case


def test_geojson_embedded_crs_conflict_is_rejected(tmp_path):
    policy_path = tmp_path / "p2.yaml"
    policy_path.write_text("security_exclusion: {}\n", encoding="utf-8")
    source_path = tmp_path / "security.geojson"
    source_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": [{"type": "Feature", "geometry": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        }, "properties": {}}],
    }), encoding="utf-8")
    policy = SecurityExclusionInput(
        format="security-area-exclusion-v1", schema_version="1.0", enabled=True,
        source_file="security.geojson", source_crs="EPSG:32650", constraint="excluded_closed_wall",
    )
    with pytest.raises(SecurityExclusionError, match="conflicts with explicit source_crs"):
        load_security_area_geometry(policy, policy_path, "EPSG:32650")


def test_mesh_preparation_result_metadata_is_explicit_with_and_without_security(tmp_path):
    unprotected = tmp_path / "unprotected"
    shutil.copytree(EXAMPLE, unprotected)
    unprotected_config = yaml.safe_load((unprotected / "mesh_preparation.yaml").read_text(encoding="utf-8"))
    unprotected_config.pop("security_exclusion")
    unprotected_config["output_file"] = "unprotected_mesh.nc"
    unprotected_config_path = unprotected / "unprotected_mesh_preparation.yaml"
    unprotected_config_path.write_text(yaml.safe_dump(unprotected_config), encoding="utf-8")

    unprotected_result = prepare_mesh(unprotected_config_path)
    assert unprotected_result.preparation_only is True
    assert unprotected_result.security_exclusion is None

    protected = tmp_path / "protected"
    shutil.copytree(EXAMPLE, protected)
    protected_result = prepare_mesh(protected / "mesh_preparation.yaml")
    assert protected_result.preparation_only is True
    assert protected_result.security_exclusion is not None


def test_security_exclusion_none_rainfall_native_reader_and_shp_share_active_faces(tmp_path):
    case = _copy_example(tmp_path)

    mesh_result = prepare_mesh(case / "mesh_preparation.yaml")
    mesh, _ = read_ugrid_preparation(mesh_result.artifact_path)
    evidence = read_ugrid_security_exclusion(mesh_result.artifact_path)
    assert evidence is not None
    assert mesh.n_face == evidence.mesh_face_count == 4
    assert evidence.original_domain_area_m2 == pytest.approx(8.0)
    assert evidence.security_inside_area_m2 == pytest.approx(2.0)
    assert evidence.active_domain_area_m2 == pytest.approx(6.0)
    assert evidence.active_mesh_area_m2 == pytest.approx(6.0)
    assert evidence.security_boundary_group_name in mesh.boundary_groups

    prepare_triangular_business(case / "business_preparation.yaml")
    none_report = run_triangular_solver(case / "run.yaml")
    none_result = read_triangular_result(none_report["result_file"], case / "run.yaml")
    assert none_result.security_exclusion is not None
    assert none_result.security_exclusion.active_face_id_sha256 == evidence.active_face_id_sha256
    assert none_result.face_id == [101, 102, 201, 202]
    assert all(row == pytest.approx([0.2, 0.2, 0.2, 0.2]) for row in none_result.water_depth_m)
    assert all(row.final_volume_m3 == pytest.approx(1.2) for row in none_result.mass_budget)

    prepare_triangular_sources(case / "source_preparation.yaml")
    rainfall_report = run_triangular_source(case / "source_run.yaml")
    rainfall_result = read_triangular_source_result(
        case / "rainfall.nc", case / "source_run.yaml",
    )
    accounting = rainfall_result.security_rainfall_accounting
    assert accounting is not None
    assert accounting.accepted_active_rainfall_volume_m3 == pytest.approx(0.00006)
    assert accounting.security_excluded_unmodeled_rainfall_volume_m3 == pytest.approx(0.00002)
    assert rainfall_result.mass_budget[-1].final_volume_m3 == pytest.approx(1.20006)
    assert rainfall_report["security_rainfall_accounting"]["security_excluded_unmodeled_rainfall_volume_m3"] == pytest.approx(0.00002)

    shp_report = export_native_triangle_shp(case / "export_request.yaml")
    manifest = json.loads((case / "result_shp" / "manifest.json").read_text(encoding="utf-8"))
    assert shp_report.feature_count == 4
    assert manifest["security_active_face_id_sha256"] == evidence.active_face_id_sha256
    assert manifest["security_exclusion_sha256"]
    assert manifest["native_face_count"] == evidence.mesh_face_count

    web_out = case / "legacy_web_output"
    with pytest.raises(Nc2WebError, match="UNSUPPORTED_NATIVE_TRIANGULAR_RESULT"):
        convert_netcdf_to_web(case / "result.nc", web_out)
    assert not web_out.exists()
    with pytest.raises(Nc2WebError, match="UNSUPPORTED_NATIVE_TRIANGULAR_RESULT"):
        convert_netcdf_to_step_json(case / "result.nc", web_out)
    assert not web_out.exists()
