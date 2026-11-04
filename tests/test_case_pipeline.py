"""case_pipeline: one config, any projected DEM -> mesh, terrain, sections, run, bundle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydrodynamics.case_pipeline import CasePipelineError, Pipeline, load_case_config, run_pipeline

X0, Y0, DX, NX, NY = 500000.0, 4001000.0, 10.0, 200, 100


def _write_dem(path: Path, *, nodata_block: bool = False) -> None:
    np = pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    xs = X0 + DX * (np.arange(NX) + 0.5)
    ys = Y0 - DX * (np.arange(NY) + 0.5)
    x, y = np.meshgrid(xs, ys)
    z = (3.0 - 0.0005 * (x - X0) + np.minimum(np.abs(y - (Y0 - 500.0)), 300.0) / 50.0).astype("float32")
    if nodata_block:
        z[40:60, 90:110] = -9999.0
    with rasterio.open(path, "w", driver="GTiff", width=NX, height=NY, count=1, dtype="float32", crs="EPSG:32650",
                       transform=from_origin(X0, Y0, DX, DX), nodata=-9999.0) as dataset:
        dataset.write(z, 1)


def _case(tmp_path: Path, **overrides) -> Path:
    _write_dem(tmp_path / "dem.tif", nodata_block=overrides.pop("nodata_block", False))
    (tmp_path / "unit.txt").write_text("synthetic DEM in metres\n", encoding="utf-8")
    (tmp_path / "forcing.csv").write_text("time_s,q_in,q_out_ref\n0,0,0\n1800,20,0\n7200,20,0\n", encoding="utf-8")
    case = {
        "format": "case-pipeline-config-v1", "schema_version": "1.0", "case_id": "synthetic", "work_dir": "work",
        "dem": {"path": "dem.tif", "crs": "EPSG:32650", "vertical_unit": "m", "vertical_unit_evidence": "unit.txt"},
        "domain": {"polygon": {"type": "coordinates", "coordinates": [[500050, 4000050], [501950, 4000050], [501950, 4000950], [500050, 4000950]]}},
        "mesh": {"default_size_m": 60, "minimum_size_m": 5},
        "sections": {"rule": {"window_m": 400, "thalweg_search_m": 200}, "items": [
            {"name": "A", "role": "inflow", "xy": [500050, 4000500], "column": "q_in"},
            {"name": "B", "role": "outflow", "xy": [501950, 4000500], "column": "q_out_ref", "status": "COMPUTED_OUTFLOW",
             "boundary_type": "free_outflow"}]},
        "forcing": {"path": "forcing.csv"},
        "run": {"hours": 1, "time_origin": "2026-01-01T00:00:00+08:00", "checkpoint_interval_s": None},
        "output": {"root": "results", "run_id": "synthetic_run"},
    }
    for key, value in overrides.items():
        case[key] = value
    path = tmp_path / "case.json"
    path.write_text(json.dumps(case), encoding="utf-8")
    return path


def test_config_rejects_unpaired_vertical_unit(tmp_path: Path) -> None:
    path = _case(tmp_path, dem={"path": "dem.tif", "crs": "EPSG:32650", "vertical_unit": "m"})
    with pytest.raises(CasePipelineError, match="vertical_unit and vertical_unit_evidence"):
        load_case_config(path)


def test_config_rejects_free_outflow_fields_on_inflow(tmp_path: Path) -> None:
    path = _case(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["sections"]["items"][0]["crest_elevation_m"] = 1.0
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CasePipelineError, match="outflow-only"):
        load_case_config(path)


def test_check_flags_domain_outside_dem_and_far_section(tmp_path: Path) -> None:
    pytest.importorskip("shapely")
    path = _case(tmp_path, domain={"polygon": {"type": "coordinates", "coordinates": [[499000, 4000050], [501950, 4000050], [501950, 4000950], [499000, 4000950]]}})
    config, base = load_case_config(path)
    report = Pipeline(config, base).check()
    assert report["status"] == "invalid"
    assert any("outside the DEM" in e for e in report["errors"])
    assert any("section A" in e for e in report["errors"])


def test_geometry_from_geojson_with_hole_and_constraint(tmp_path: Path) -> None:
    pytest.importorskip("shapely")
    path = _case(tmp_path)
    geojson = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[
        [500050, 4000050], [501950, 4000050], [501950, 4000950], [500050, 4000950], [500050, 4000050]]]}}]}
    (tmp_path / "domain.geojson").write_text(json.dumps(geojson), encoding="utf-8")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["domain"] = {"polygon": {"type": "geojson", "path": "domain.geojson"},
                     "holes": [{"type": "coordinates", "coordinates": [[500900, 4000200], [501100, 4000200], [501100, 4000300], [500900, 4000300]]}],
                     "constraints": [{"name": "dike", "points": [[500300, 4000700], [501700, 4000700]]}]}
    path.write_text(json.dumps(raw), encoding="utf-8")
    manifest = run_pipeline(path, stage="geometry")
    geometry = json.loads(Path(manifest["outputs"]["geometry"]).read_text(encoding="utf-8"))
    assert len(geometry["holes"]) == 1 and geometry["constraints"][0]["name"] == "dike"
    assert manifest["summary"]["area_km2"] == pytest.approx(1.71 - 0.02, abs=1e-6)


def test_terrain_refuses_nodata_faces(tmp_path: Path) -> None:
    pytest.importorskip("gmsh")
    path = _case(tmp_path, nodata_block=True)
    with pytest.raises(CasePipelineError, match="zero valid DEM overlap.*NoData is never filled"):
        run_pipeline(path, stage="terrain")


def test_end_to_end_run_and_stage_cache(tmp_path: Path) -> None:
    pytest.importorskip("gmsh")
    pytest.importorskip("netCDF4")
    pytest.importorskip("shapefile")
    path = _case(tmp_path)
    report = run_pipeline(path, stage="run")
    assert report["status"] == "complete" and report["verify"]["status"] == "PASS"
    assert (tmp_path / "results" / "synthetic_run" / "bundle" / "run.json").is_file()
    mesh_manifest = json.loads((tmp_path / "work" / "mesh" / "stage.json").read_text(encoding="utf-8"))
    again = run_pipeline(path, stage="sections")
    assert json.loads((tmp_path / "work" / "mesh" / "stage.json").read_text(encoding="utf-8")) == mesh_manifest
    assert set(again["summary"]) == {"A", "B"}
