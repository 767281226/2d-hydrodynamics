"""Socio-economic exposure overlay on a tiny synthetic mesh + tiny synthetic grid shapefile."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("netCDF4"); shapefile = pytest.importorskip("shapefile"); pytest.importorskip("pyproj")
pytest.importorskip("shapely", minversion="2.0")

from pyproj import CRS, Transformer

from hydrodynamics.frame_sink import NativeUgridNetcdfSink
from hydrodynamics.large_run_export import LargeRunExportError, export_large_run_bundle, verify_large_run_bundle
from hydrodynamics.large_run_profile import LargeRunOutputProfile, LargeRunProfileError, parse_profile, rain_only_forcing
from hydrodynamics.socio_exposure import (ExposureAccumulator, SocioExposureError, SocioOptions, apply_socio_exposure_to_bundle,
                                          build_face_mapping, load_or_build_mapping, mesh_fingerprint, read_socio_grid)

X0, Y0, STEP, NX, NY = 474300.0, 4300010.0, 37.0, 12, 9
XMAX, YMAX = X0 + NX * STEP, Y0 + NY * STEP * 1.1
GXE = 474675.0
GX0, GY0, CELL, GCOLS, GROWS = 474275.0, 4299985.0, 50.0, 8, 8   # grid covers x 474275..474675 only


def _mesh():
    xs = X0 + np.arange(NX + 1) * STEP; ys = Y0 + np.arange(NY + 1) * (STEP * 1.1)
    X, Y = np.meshgrid(xs, ys); xy = np.column_stack([X.ravel(), Y.ravel()])
    faces = []
    for r in range(NY):
        for c in range(NX):
            a = r * (NX + 1) + c; b = a + 1; d = a + NX + 1; e = d + 1
            faces += [(a, b, e), (a, e, d)]
    faces = np.asarray(faces, dtype=np.int64)
    tri = xy[faces]
    area = 0.5 * np.abs((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1]) - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1]))
    return xy, faces, np.arange(1001, 1001 + len(faces), dtype=np.int64), area


def _cells():
    """(ring, pop, arable_mu, code, name) per cell; one extra cell far outside the domain."""
    out = []
    for r in range(GROWS):
        for c in range(GCOLS):
            x0 = GX0 + c * CELL; y0 = GY0 + r * CELL
            ring = [(x0, y0), (x0, y0 + CELL), (x0 + CELL, y0 + CELL), (x0 + CELL, y0), (x0, y0)]  # clockwise
            pop = float(r * 10 + c + 1)
            out.append((ring, pop, 2.0 * pop, "A01" if c < 4 else "B02", "甲区" if c < 4 else "乙区"))
    far = [(480000.0, 4310000.0), (480000.0, 4310050.0), (480050.0, 4310050.0), (480050.0, 4310000.0), (480000.0, 4310000.0)]
    out.append((far, 999.0, 999.0, "A01", "甲区"))
    return out


def _write_grid(path: Path, epsg: int = 4548) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tr = None if epsg == 4548 else Transformer.from_crs(4548, epsg, always_xy=True)
    w = shapefile.Writer(str(path), shapeType=shapefile.POLYGON, encoding="utf-8")
    for name, kind, size, dec in (("s_pop", "F", 12, 4), ("s_ar_mu", "F", 12, 4), ("qu_dm", "C", 8, 0), ("qu", "C", 24, 0)):
        w.field(name, kind, size=size, decimal=dec)
    for ring, pop, ar, code, name in _cells():
        if tr is not None:
            ring = [tuple(tr.transform(x, y)) for x, y in ring]
        w.poly([ring]); w.record(pop, ar, code, name)
    w.close()
    path.with_suffix(".prj").write_text(CRS.from_epsg(epsg).to_wkt(version="WKT1_ESRI"), encoding="utf-8")
    path.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
    return path


def _expected_inside():
    """Analytic population/arable inside the rectangular mesh (cells are axis-aligned squares)."""
    pop = ar = 0.0
    for ring, p, a, *_ in _cells():
        xs = [x for x, _ in ring]; ys = [y for _, y in ring]
        ox = max(0.0, min(max(xs), XMAX) - max(min(xs), X0)); oy = max(0.0, min(max(ys), YMAX) - max(min(ys), Y0))
        frac = ox * oy / CELL ** 2
        pop += p * frac; ar += a * frac
    return pop, ar


@pytest.fixture()
def grid_path(tmp_path):
    return _write_grid(tmp_path / "grid" / "cells.shp")


def _mapping(path, **opts):
    xy, faces, ids, area = _mesh()
    options = SocioOptions(grid_cells_path=path, **opts)
    grid = read_socio_grid(path, "EPSG:4548", options)
    return build_face_mapping(xy, faces, area, grid, fingerprint=mesh_fingerprint(xy, faces, ids, "EPSG:4548")), options, (xy, faces, ids, area)


def test_conservation_partial_overlap_and_nan_for_uncovered(grid_path):
    m, _, (xy, faces, ids, area) = _mapping(grid_path)
    pop_in, ar_in = _expected_inside()
    rep = m.report
    assert rep["conservation"]["status"] == "PASS"
    assert rep["population_inside_domain"] == pytest.approx(pop_in, rel=1e-10)
    assert rep["arable_mu_inside_domain"] == pytest.approx(ar_in, rel=1e-10)
    assert np.nansum(m.pop) == pytest.approx(pop_in, rel=1e-10) and np.nansum(m.arable_mu) == pytest.approx(ar_in, rel=1e-10)
    # cells partly outside contribute only their inside fraction: total inside < total of touching cells
    assert rep["grid_cells_partially_inside_domain"] > 0 and rep["grid_cells_outside_domain"] == 1
    assert rep["grid_population_total_all_cells"] > pop_in
    # faces right of x=GXE are not covered -> NaN, not 0
    cx = xy[faces].mean(1)[:, 0]
    right = xy[faces][:, :, 0].min(1) >= GX0 + GCOLS * CELL
    assert right.any() and np.isnan(m.pop[right]).all() and np.isnan(m.arable_mu[right]).all()
    assert rep["faces_not_covered_unavailable"] == int(right.sum())
    assert rep["faces_partially_covered"] > 0
    straddle = (xy[faces][:, :, 0].min(1) < GXE) & (xy[faces][:, :, 0].max(1) > GXE)
    assert np.all((m.coverage_fraction[straddle] > 0) & (m.coverage_fraction[straddle] < 1))
    assert np.isfinite(m.pop[cx < 474600]).all()
    cover = (np.minimum(XMAX, GXE) - X0) * (YMAX - Y0)
    assert rep["grid_covered_domain_area_m2"] == pytest.approx(cover, rel=1e-10)
    assert rep["grid_coverage_fraction_of_domain"] == pytest.approx(cover / area.sum(), rel=1e-10)
    # per-district split sums to the total
    assert m.w_pop[:, 1:].sum() == pytest.approx(pop_in, rel=1e-10)
    assert set(m.district_codes) == {"A01", "B02"}


def test_wgs84_grid_is_reprojected_and_conserves(tmp_path):
    path = _write_grid(tmp_path / "g84" / "cells.shp", epsg=4326)
    m, _, _ = _mapping(path)
    pop_in, _ = _expected_inside()
    assert m.report["conservation"]["status"] == "PASS"
    assert m.report["population_inside_domain"] == pytest.approx(pop_in, rel=1e-6)


def test_threshold_and_depth_classes(grid_path):
    m, options, (xy, faces, ids, area) = _mapping(grid_path)
    rng = np.random.default_rng(3)
    h = rng.choice([0.0, 0.1, 0.15, 0.3, 0.5, 1.0, 1.5, 2.9, 3.0, 7.0], size=faces.shape[0])
    acc = ExposureAccumulator(m, options, "2023-08-01T02:00:00+08:00")
    acc.add_frame(0, 3600.0, h)
    rows = {r["district_code"]: r for r in acc.rows}
    pop = np.nan_to_num(m.pop); ar = np.nan_to_num(m.arable_mu)
    exp = h >= 0.15
    total = rows["ALL"]
    assert total["time_iso"] == "2023-08-01T03:00:00+08:00"
    assert total["exposed_pop"] == pytest.approx(pop[exp].sum()) and total["exposed_arable_mu"] == pytest.approx(ar[exp].sum())
    assert total["exposed_arable_km2"] == pytest.approx(ar[exp].sum() / 1500.0)
    assert total["exposed_face_count"] == int(exp.sum()) and total["exposed_area_m2"] == pytest.approx(area[exp].sum())
    assert total["exposed_area_without_socio_m2"] == pytest.approx(area[exp & np.isnan(m.pop)].sum())
    for lab, lo, hi in (("0.15_0.5", 0.15, 0.5), ("0.5_1.5", 0.5, 1.5), ("1.5_3", 1.5, 3.0), ("3_inf", 3.0, np.inf)):
        sel = (h >= lo) & (h < hi)
        assert total[f"pop_d{lab}"] == pytest.approx(pop[sel].sum()), lab
    assert sum(total[f"pop_d{lab}"] for lab in ("0.15_0.5", "0.5_1.5", "1.5_3", "3_inf")) == pytest.approx(total["exposed_pop"])
    assert rows["A01"]["exposed_pop"] + rows["B02"]["exposed_pop"] == pytest.approx(total["exposed_pop"])
    # a higher threshold never increases exposure
    hi = ExposureAccumulator(m, SocioOptions(grid_cells_path=grid_path, exposure_depth_threshold_m=1.0, depth_class_edges_m=(1.0, 2.0)))
    hi.add_frame(0, 0.0, h)
    assert hi.rows[0]["exposed_pop"] == pytest.approx(pop[h >= 1.0].sum())
    assert hi.rows[0]["exposed_pop"] < total["exposed_pop"]


def test_mapping_cache_keyed_by_mesh_fingerprint(grid_path, tmp_path):
    xy, faces, ids, area = _mesh()
    options = SocioOptions(grid_cells_path=grid_path, mapping_cache_dir=tmp_path / "cache")
    m1, _, info1 = load_or_build_mapping(xy, faces, ids, area, "EPSG:4548", options)
    m2, _, info2 = load_or_build_mapping(xy, faces, ids, area, "EPSG:4548", options)
    assert not info1["hit"] and info2["hit"] and info1["mesh_fingerprint"] in info2["mesh_fingerprint"]
    assert np.array_equal(m1.pop, m2.pop, equal_nan=True)
    xy2 = xy.copy(); xy2[0, 0] += 0.5; tri = xy2[faces]   # different mesh -> different fingerprint -> no hit
    area = 0.5 * np.abs((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1]) - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1]))
    _, _, info3 = load_or_build_mapping(xy2, faces, ids, area, "EPSG:4548", options)
    assert not info3["hit"] and info3["mesh_fingerprint"] != info1["mesh_fingerprint"]


@pytest.mark.parametrize("value,message", [
    ({"socio_exposure": {}}, "grid_cells_path"),
    ({"socio_exposure": {"grid_cells_path": "x.shp", "exposure_depth_threshold_m": 0}}, "greater than 0"),
    ({"socio_exposure": {"grid_cells_path": "x.shp", "depth_class_edges_m": [0.5, 0.15]}}, "strictly increasing"),
    ({"socio_exposure": {"grid_cells_path": "x.shp", "bogus": 1}}, "Extra inputs"),
])
def test_profile_socio_validation(value, message):
    with pytest.raises(LargeRunProfileError, match=message):
        parse_profile(value)


def test_bad_inputs_are_rejected(tmp_path, grid_path):
    with pytest.raises(SocioExposureError, match="not found"):
        read_socio_grid(tmp_path / "missing.shp", "EPSG:4548", SocioOptions(grid_cells_path=tmp_path / "missing.shp"))
    with pytest.raises(SocioExposureError, match="lacks field"):
        read_socio_grid(grid_path, "EPSG:4548", SocioOptions(grid_cells_path=grid_path, population_field="nope"))
    noprj = tmp_path / "noprj" / "cells.shp"; _write_grid(noprj); noprj.with_suffix(".prj").unlink()
    with pytest.raises(SocioExposureError, match="component missing"):
        read_socio_grid(noprj, "EPSG:4548", SocioOptions(grid_cells_path=noprj))
    with pytest.raises(SocioExposureError, match="strictly increasing"):
        SocioOptions(grid_cells_path=grid_path, depth_class_edges_m=(1.0, 1.0))


# ------------------------------------------------------------------ bundles
def _bundle(tmp_path: Path, frames: int = 3) -> Path:
    xy, faces, ids, _ = _mesh()
    bundle = tmp_path / "bundle"; bundle.mkdir()
    times = [3600.0 * i for i in range(frames)]
    mesh = {"node_xy_m": xy, "face_nodes": faces, "face_id": ids, "bed_elevation_m": np.full(len(faces), 3.0)}
    meta = {"run_id": "socio-test-001", "case_id": "case_socio_test", "time_origin": "2023-08-01T02:00:00+08:00",
            "coordinate_system": "EPSG:4548", "vertical_datum": "TEST_DATUM", "dry_depth_threshold_m": 0.001,
            "expected_times_s": times, "data_origin": "SYNTHETIC_TEST_FIELDS", "live_solver_ran": False,
            "forcing": rain_only_forcing().model_dump(mode="json")}
    profile = LargeRunOutputProfile(disk_preflight=False)
    sink = NativeUgridNetcdfSink(bundle / "result_native.nc", profile)
    sink.open(mesh, meta)
    x = (xy[faces].mean(1)[:, 0] - X0) / (XMAX - X0)
    for i, t in enumerate(times):
        h = np.clip(0.8 * i - 2.0 * (1.0 - x), 0.0, None); h[h < 0.001] = 0.0
        sink.write_frame(i, t, {"water_depth_m": h, "unit_discharge_x_m2_s": 0.1 * h, "unit_discharge_y_m2_s": 0 * h},
                         ledger={"volume_m3": float(np.dot(h, sink.mesh.face_area_m2)), "rain_depth_mm": 0.0})
    assert sink.close()["status"] == "COMPLETE"
    return bundle


def _check_socio_bundle(bundle: Path, frames: int):
    report = verify_large_run_bundle(bundle)
    assert report["status"] == "PASS" and report["checks"]["socio_exposure"]["status"] == "PASS"
    run = json.loads((bundle / "run.json").read_text(encoding="utf-8"))
    assert run["social_economy_status"] == "COMPLETE_EXPOSURE_ONLY"
    assert run["socio_exposure"]["economic_loss"] == "NOT_COMPUTED_NO_ASSET_VALUES"
    listed = {row["path"] for row in json.loads((bundle / "checksums.json").read_text(encoding="utf-8"))["files"]}
    for name in ("exposure_by_frame.csv", "exposure_summary.json", "face_socio_mapping.npz"):
        assert f"socio_exposure/{name}" in listed
    summary = json.loads((bundle / "socio_exposure" / "exposure_summary.json").read_text(encoding="utf-8"))
    assert summary["inputs"]["grid_cells"]["shp"]["sha256"] and summary["inputs"]["result_native.nc"]["sha256"]
    assert summary["limitations"] and summary["mapping"]["conservation"]["status"] == "PASS"
    assert summary["mapping"]["population_inside_domain"] == pytest.approx(_expected_inside()[0], rel=1e-10)
    with (bundle / "socio_exposure" / "exposure_by_frame.csv").open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len([r for r in rows if r["aggregation"] == "FRAME" and r["district_code"] == "ALL"]) == frames
    env = next(r for r in rows if r["aggregation"] == "MAX_OVER_SAVED_FRAMES" and r["district_code"] == "ALL")
    assert float(env["exposed_pop"]) > 0
    with shapefile.Reader(str(bundle / "shp" / "maximum_wet_surface" / "faces.shp"), encoding="utf-8") as reader:
        names = [f[0] for f in reader.fields[1:]]
        assert {"POP", "ARABLE_MU", "SOCIO_COV", "QU_DM", "EXPOSED"} <= set(names)
        recs = reader.records()
    pops = [r["POP"] for r in recs]
    assert any(p is None for p in pops) and any(p is not None and p > 0 for p in pops)   # blank = unavailable, not 0
    assert {r["EXPOSED"] for r in recs} <= {0, 1}
    return summary


def test_export_with_socio_profile_and_verify(tmp_path, grid_path):
    bundle = _bundle(tmp_path)
    profile = LargeRunOutputProfile(disk_preflight=False, socio_exposure={"grid_cells_path": str(grid_path)})
    export_large_run_bundle(bundle, profile)
    summary = _check_socio_bundle(bundle, 3)
    assert summary["options"]["exposure_depth_threshold_m"] == 0.15
    # tampering is detected by verify
    (bundle / "socio_exposure" / "exposure_by_frame.csv").write_text("x", encoding="utf-8")
    with pytest.raises(LargeRunExportError):
        verify_large_run_bundle(bundle)


def test_post_hoc_apply_to_exported_bundle(tmp_path, grid_path):
    bundle = _bundle(tmp_path)
    export_large_run_bundle(bundle, LargeRunOutputProfile(disk_preflight=False))
    assert verify_large_run_bundle(bundle)["checks"]["socio_exposure"]["status"] == "NOT_REQUESTED"
    report = apply_socio_exposure_to_bundle(bundle, SocioOptions(grid_cells_path=grid_path))
    assert report["verify"]["status"] == "PASS"
    _check_socio_bundle(bundle, 3)
    with pytest.raises(SocioExposureError, match="no-clobber"):
        apply_socio_exposure_to_bundle(bundle, SocioOptions(grid_cells_path=grid_path))
