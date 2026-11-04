"""Large-run (opt-in) streaming output profile: sink, exporter, viewer validation."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("netCDF4"); pytest.importorskip("shapefile"); pytest.importorskip("pyproj")

from hydrodynamics.frame_sink import FrameSinkError, LargeNativeResult, NativeUgridNetcdfSink
from hydrodynamics.large_run_export import (SparseCompatibilityGrid, _step_records, export_large_run_bundle,
                                            verify_large_run_bundle)
from hydrodynamics.large_run_profile import (LargeRunOutputProfile, LargeRunProfileError, RAIN_ONLY_NOTICE,
                                             RIVER_ASSUMED_SECTIONS_NOTICE, load_profile, parse_metadata, parse_profile,
                                             rain_only_forcing, river_assumed_sections_forcing)
from hydrodynamics.nc2web import _flow_dir_deg, _fmt_num
from hydrodynamics.viewer_server import ViewerBundleError, validate_bundle

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "large_run_output"
GROUPS = [
    {"group_id": "daqinghe_west", "role": "inflow", "name": "大清河 west (assumed)",
     "section_status": "ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE", "discharge_series_status": "RECONSTRUCTED_NOT_MEASURED"},
    {"group_id": "duliujianhe", "role": "outflow", "name": "独流减河 (assumed)",
     "section_status": "ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE", "discharge_series_status": "COMPUTED_OUTFLOW"},
]
PROFILE = LargeRunOutputProfile(display_grid_cell_size_m=50.0, disk_preflight=False)


def _mesh(nx: int = 12, ny: int = 9, step: float = 37.0):
    xs = 474300.0 + np.arange(nx + 1) * step; ys = 4300010.0 + np.arange(ny + 1) * (step * 1.1)
    X, Y = np.meshgrid(xs, ys); xy = np.column_stack([X.ravel(), Y.ravel()])
    faces = []
    for r in range(ny):
        for c in range(nx):
            a = r * (nx + 1) + c; b = a + 1; d = a + nx + 1; e = d + 1
            faces += [(a, b, e), (a, e, d)]
    faces = np.asarray(faces)
    centroid = xy[faces].mean(1)
    return {"node_xy_m": xy, "face_nodes": faces, "face_id": np.arange(1001, 1001 + len(faces), dtype=np.int64),
            "bed_elevation_m": 3.0 + 1e-3 * (centroid[:, 0] - 474300.0)}


def _meta(times, forcing="river", **extra):
    value = {"run_id": "test-large-001", "case_id": "case_large_test", "time_origin": "2023-08-01T02:00:00+08:00",
             "coordinate_system": "EPSG:4548", "vertical_datum": "TEST_DATUM", "dry_depth_threshold_m": 0.001,
             "expected_times_s": list(times), "data_origin": "SYNTHETIC_TEST_FIELDS", "live_solver_ran": False,
             "forcing": (river_assumed_sections_forcing(GROUPS) if forcing == "river" else rain_only_forcing()).model_dump(mode="json")}
    value.update(extra)
    return value


def _run(tmp_path: Path, forcing: str = "river", frames: int = 4) -> Path:
    bundle = tmp_path / "bundle"; bundle.mkdir()
    mesh = _mesh(); times = [3600.0 * i for i in range(frames)]
    sink = NativeUgridNetcdfSink(bundle / "result_native.nc", PROFILE)
    sink.open(mesh, _meta(times, forcing))
    x = (mesh["node_xy_m"][mesh["face_nodes"]].mean(1)[:, 0] - 474300.0) / 444.0
    for i, t in enumerate(times):
        h = np.clip((0.2 + 0.25 * i) - x, 0.0, None); h[h < 0.001] = 0.0
        q = np.where(h > 0, 0.3 * h, 0.0)
        ledger = {"volume_m3": float(np.dot(h, sink.mesh.face_area_m2))}
        if forcing == "river":
            ledger.update({"inflow_m3_s": 10.0 + i, "outflow_m3_s": 2.0 * i,
                           "groups": {"daqinghe_west": {"q_m3_s": 10.0 + i}, "duliujianhe": {"q_m3_s": 2.0 * i}}})
        else:
            ledger["rain_depth_mm"] = 0.0 if i == 0 else 2.0
        sink.write_frame(i, t, {"water_depth_m": h, "unit_discharge_x_m2_s": q, "unit_discharge_y_m2_s": -0.5 * q}, ledger=ledger)
    close_ledger = {"groups": {"daqinghe_west": {"role": "inflow", "volume_m3": 1.5e5},
                               "duliujianhe": {"role": "outflow", "volume_m3": 2.0e4, "outflow_shortfall_m3": 1.2e3}},
                    "outflow_shortfall_m3": 1.2e3, "final_volume_series_m3": [1.0] * frames} if forcing == "river" else {}
    report = sink.close({"max_water_depth_m": sink._max_h + 0.01}, close_ledger)
    assert report["status"] == "COMPLETE" and report["frame_count"] == frames
    return bundle


def test_profile_defaults_schema_and_example_are_synchronized():
    profile = load_profile(EXAMPLES / "large_run_output_profile.example.yaml")
    assert profile == LargeRunOutputProfile()
    schema = json.loads((EXAMPLES / "large_run_output_profile.schema.json").read_text(encoding="utf-8"))
    assert set(schema["properties"]) == set(LargeRunOutputProfile.model_fields)
    for name, field in LargeRunOutputProfile.model_fields.items():
        assert schema["properties"][name]["default"] == field.default, name
    assert profile.display_grid_cell_size_m == 50.0
    meta = parse_metadata(json.loads((EXAMPLES / "large_run_metadata.river_assumed_sections.example.json").read_text(encoding="utf-8")))
    assert meta.forcing.notice == RIVER_ASSUMED_SECTIONS_NOTICE and not meta.forcing.rainfall_included


@pytest.mark.parametrize("value,message", [
    ({"display_grid_cell_size_m": 0.5}, "greater than or equal to 1"),
    ({"display_grid_cell_size_m": "50"}, "valid number"),
    ({"native_nc_complevel": 10}, "less than or equal to 9"),
    ({"stepjson_still_direction_deg": 360}, "less than 360"),
    ({"unknown_option": 1}, "Extra inputs"),
])
def test_profile_validation_errors(value, message):
    with pytest.raises(LargeRunProfileError, match=message):
        parse_profile(value)


def test_forcing_label_validation():
    base = _meta([0.0])
    for change, message in (({"scope": "RIVER_ONLY", "boundary_policy": "closed_wall"}, "boundary_policy=open"),
                            ({"rainfall_included": True}, "rainfall_included must be False"),
                            ({"boundary_groups": []}, "inflow boundary group"),
                            ({"river_section_status": "NOT_APPLICABLE"}, "river_section_status")):
        forcing = {**base["forcing"], **change}
        with pytest.raises(LargeRunProfileError, match=message):
            parse_metadata({**base, "forcing": forcing})
    with pytest.raises(LargeRunProfileError, match="forcing"):
        parse_metadata({k: v for k, v in base.items() if k != "forcing"})
    with pytest.raises(LargeRunProfileError, match="UTC offset"):
        parse_metadata({**base, "time_origin": "2023-08-01T02:00:00"})
    with pytest.raises(LargeRunProfileError, match="synthetic"):
        parse_metadata({**base, "live_solver_ran": True})
    assert parse_metadata(_meta([0.0], "rain")).forcing.notice == RAIN_ONLY_NOTICE


def test_sink_rejects_bad_calls_and_keeps_failed_partial(tmp_path):
    mesh = _mesh(); times = [0.0, 3600.0]
    sink = NativeUgridNetcdfSink(tmp_path / "r.nc", PROFILE); sink.open(mesh, _meta(times))
    n = len(mesh["face_id"]); ok = {"water_depth_m": np.zeros(n), "velocity_x_m_s": np.zeros(n), "velocity_y_m_s": np.zeros(n)}
    with pytest.raises(FrameSinkError, match="contiguous"):
        sink.write_frame(1, 0.0, ok)
    with pytest.raises(FrameSinkError, match="negative"):
        sink.write_frame(0, 0.0, {**ok, "water_depth_m": -np.ones(n)})
    with pytest.raises(FrameSinkError, match="expected_times_s"):
        sink.write_frame(0, 5.0, ok)
    with pytest.raises(FrameSinkError, match="unit discharge and/or velocity"):
        sink.write_frame(0, 0.0, {"water_depth_m": np.zeros(n)})
    with pytest.raises(FrameSinkError, match="shape"):
        sink.write_frame(0, 0.0, {**ok, "water_depth_m": np.zeros(n - 1)})
    sink.write_frame(0, 0.0, ok)
    with pytest.raises(FrameSinkError, match="only 1 of 2"):
        sink.close()
    assert not (tmp_path / "r.nc").exists() and (tmp_path / "r.nc.failed").is_file()
    sink2 = NativeUgridNetcdfSink(tmp_path / "s.nc", PROFILE); sink2.open(mesh, _meta([0.0]))
    sink2.write_frame(0, 0.0, {**ok, "water_depth_m": np.full(n, 0.5)})
    with pytest.raises(FrameSinkError, match="below a saved-frame depth"):
        sink2.close({"max_water_depth_m": np.zeros(n)})
    with pytest.raises(FrameSinkError, match="exceeds the profile caps"):
        NativeUgridNetcdfSink(tmp_path / "t.nc", LargeRunOutputProfile(max_face_count=10, disk_preflight=False)).open(mesh, _meta([0.0]))


def test_sparse_grid_conserves_area_and_uniform_fields():
    mesh = _mesh(); xy, faces = mesh["node_xy_m"], mesh["face_nodes"]
    p = xy[faces]; area = 0.5 * np.abs((p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1]) - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1]))
    grid = SparseCompatibilityGrid(xy, faces, area, mesh["bed_elevation_m"], "EPSG:4548", PROFILE)
    assert grid.x0 == 474300.0 and grid.dx == 50.0 and (grid.x0 - 474150.0) % 50.0 == 0
    assert np.isclose(grid.covered.sum(), area.sum(), rtol=1e-12)
    assert np.any((grid.coverage > 0) & (grid.coverage < 1))
    mapped = grid.project(np.full(len(area), 0.7), np.full(len(area), 0.35), np.zeros(len(area)))
    active = grid.active.reshape(grid.ny, grid.nx)
    assert np.allclose(mapped["water_depth"][active], 0.7) and np.allclose(mapped["velocity_x"][active], 0.5)
    assert np.isnan(mapped["water_depth"][~active]).all()


def test_native_step_records_match_legacy_formatter():
    rng = np.random.default_rng(3)
    h = rng.uniform(0, 5, 2000); u = rng.normal(0, 1, 2000); v = rng.normal(0, 1, 2000)
    u[:50] = 0.0; v[:50] = 0.0
    ids = [str(i) for i in range(2000)]
    records = _step_records(ids, h, u, v, 180.0)
    expected = []
    for i in range(2000):
        speed, direction = _flow_dir_deg(float(u[i]), float(v[i]), still_dir=180.0)
        expected.append(f"{ids[i]}_{_fmt_num(h[i], 3)}_{_fmt_num(speed, 2)}_{_fmt_num(direction, 1)}")
    assert records == expected


@pytest.mark.parametrize("forcing", ["river", "rain"])
def test_end_to_end_large_profile_bundle(tmp_path, forcing):
    bundle = _run(tmp_path, forcing)
    with LargeNativeResult(bundle / "result_native.nc") as native:
        assert native._ds.dimensions["time"].isunlimited() and native.n_frame == 4
        if forcing == "river":
            assert "groups__daqinghe_west__q_m3_s" in native.ledger_keys
            assert native.mass_ledger["groups"]["duliujianhe"]["outflow_shortfall_m3"] == 1.2e3
            assert native._ds.getncattr("ledger_groups__duliujianhe__outflow_shortfall_m3") == 1.2e3
            assert native._ds.river_inflow_outflow_included == 1 and native._ds.rainfall_included == 0
    report = export_large_run_bundle(bundle, PROFILE)
    assert report["frame_count"] == 4
    verified = verify_large_run_bundle(bundle)
    assert verified["status"] == "PASS"
    run = json.loads((bundle / "run.json").read_text(encoding="utf-8"))
    summary = json.loads((bundle / "summary.json").read_text(encoding="utf-8"))
    import netCDF4
    with netCDF4.Dataset(str(bundle / "case_large_test.nc")) as ds:
        inflow = np.asarray(ds["inflow"][:]); rain = np.asarray(ds["rainfall"][:])
        if forcing == "river":
            assert np.allclose(inflow, [10, 11, 12, 13]) and np.allclose(rain, 0.0)
            assert ds["rainfall"].availability == "ZERO_NO_RAINFALL_IN_RUN"
            assert ds["inflow"].availability.endswith("ASSUMED_SECTIONS_RECONSTRUCTED_Q")
        else:
            assert np.allclose(inflow, 0.0) and np.allclose(rain, [0, 2, 2, 2])
        assert ds.forcing_notice == run["forcing_notice"]
    if forcing == "river":
        assert run["forcing_notice"] == RIVER_ASSUMED_SECTIONS_NOTICE and run["river_inflow_outflow_included"] is True
        assert summary["outflow_shortfall"]["outflow_shortfall_m3"] == 1.2e3
        assert "groups__duliujianhe__q_m3_s" in summary["boundary_group_ledger_series"]
    else:
        assert run["forcing_notice"] == RAIN_ONLY_NOTICE and run["river_inflow_outflow_included"] is False
    step = json.loads((bundle / "stepjson" / "step_002.json").read_text(encoding="utf-8"))
    native_step = json.loads((bundle / "stepjson_native" / "step_002.json").read_text(encoding="utf-8"))
    assert list(step) == ["all"] and step["all"] and list(native_step) == ["all"]
    assert all(int(r.split("_")[0]) >= 1001 for r in native_step["all"])
    roles = {row["role"]: row for row in run["shp_packages"]}
    assert set(roles) == {"full_domain", "full_domain_4490", "maximum_wet_surface", "maximum_wet_surface_4490"}
    assert roles["full_domain"]["feature_count"] == roles["full_domain_4490"]["feature_count"] == 216
    assert 0 < roles["maximum_wet_surface"]["feature_count"] <= 216
    assert "GEOGCS" in (bundle / "shp" / "full_domain_4490" / "faces.prj").read_text(encoding="utf-8")
    root, served = validate_bundle(bundle, explicit=True)
    assert served["format"] == "large-run-output-bundle-v1"
    with pytest.raises(ViewerBundleError, match="explicit"):
        validate_bundle(bundle, explicit=False)
    (bundle / "native_web" / "frame_000001.bin").write_bytes(b"x")
    with pytest.raises(ViewerBundleError, match="size differs"):
        validate_bundle(bundle, explicit=True)
    with pytest.raises(Exception, match="no-clobber"):
        export_large_run_bundle(bundle, PROFILE)


def test_viewer_asset_supports_large_bundles_and_keeps_m1_paths():
    html = (ROOT / "hydrodynamics" / "assets" / "viewer.html").read_text(encoding="utf-8")
    for marker in ("NTW3F02", "large-run-output-bundle-v1", "hydrodynamics-legacy-grid-compatibility-v2", "drawNativeLarge",
                   "m1-output-bundle-v1", "NTW3F01", "forcing_notice", "PATH_CHUNK_FACES"):
        assert marker in html
