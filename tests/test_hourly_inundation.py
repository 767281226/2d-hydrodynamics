from pathlib import Path
import numpy as np
import pytest

from hydrodynamics.hourly_inundation import (
    HourlyInundationSolver,
    classify_dongdian_mask,
    fill_volume,
    write_hourly_netcdf,
)


def test_fill_volume_matches_requested_storage_on_bowl():
    z = np.array([[4.0, 3.0], [2.0, 1.0]])
    active = np.array([[True, True], [True, True]])
    h, eta, stored = fill_volume(z, active, volume=3.0, cell_area=1.0)
    assert stored == pytest.approx(3.0, rel=1e-6)
    assert eta == pytest.approx(3.0, abs=1e-6)
    assert h[1, 1] == pytest.approx(2.0, abs=1e-6)
    assert h[0, 0] == pytest.approx(0.0, abs=1e-6)


def test_classify_qingbei_keeps_only_north_of_daqing_ridge():
    from hydrodynamics.hourly_inundation import classify_qingbei_mask

    x = np.array([0.0, 10.0, 20.0, 30.0])
    y = np.array([0.0, 10.0, 20.0, 30.0, 40.0])
    active = np.ones((5, 4), dtype=bool)
    elevation = np.ones((5, 4))
    elevation[2, :] = 8.0
    line = ((-5.0, 0.0), (-5.0, 40.0))
    qingbei, excluded = classify_qingbei_mask(active, x, y, line, elevation)
    assert int(qingbei[:3].sum()) == 0
    assert int(qingbei[3:].sum()) == 8
    assert bool(np.all(excluded == (active & ~qingbei)))
    assert int(excluded[2].sum()) == 4


def test_classify_dongdian_keeps_northeast_side_only():
    ny, nx = 4, 4
    active = np.ones((ny, nx), dtype=bool)
    x = np.array([0.0, 10.0, 20.0, 30.0])
    y = np.array([0.0, 10.0, 20.0, 30.0])
    line = ((15.0, 0.0), (15.0, 30.0))
    dong, wen = classify_dongdian_mask(active, x, y, line)
    assert int(dong[:, :2].sum()) == 0
    assert int(dong[:, 2:].sum()) == 8
    assert bool(np.all(wen == (active & ~dong)))


def test_still_water_keeps_depth_and_near_zero_velocity():
    z = np.ones((2, 3))
    active = np.ones((2, 3), dtype=bool)
    solver = HourlyInundationSolver(z, active, dx=50.0, dy=50.0, xmin=0.0, ymin=0.0)
    solver.depth[:] = 1.0
    solver.volume = float(solver.depth.sum() * solver.cell_area)
    solver.eta = 2.0
    state = solver.step(inflow_m3s=0.0, outflow_m3s=0.0, rain_mm=0.0, dt_s=3600.0)
    assert float(np.max(np.abs(state.velocity_x))) < 1e-6
    assert float(np.max(np.abs(state.velocity_y))) < 1e-6
    assert np.allclose(state.depth, 1.0, atol=1e-6)
    assert state.time == 3600.0


def test_hourly_step_conserves_volume_and_keeps_inactive_dry():
    z = np.array([[3.0, 2.0, 1.0], [3.0, 2.0, 1.0]])
    active = np.array([[False, True, True], [False, True, True]])
    solver = HourlyInundationSolver(
        elevation=z,
        active_mask=active,
        dx=10.0,
        dy=10.0,
        xmin=0.0,
        ymin=0.0,
        manning_n=0.035,
        eta_crest=2.5,
        min_depth=0.001,
    )
    state = solver.step(inflow_m3s=1.0, outflow_m3s=0.0, rain_mm=0.0, dt_s=100.0)
    assert state.volume == pytest.approx(100.0, rel=1e-5)
    assert np.all(state.depth[~active] == 0.0)
    assert state.wet_area > 0


def test_inlet_velocity_stays_within_floodplain_cap():
    z = np.zeros((2, 4))
    active = np.ones((2, 4), dtype=bool)
    solver = HourlyInundationSolver(z, active, dx=50.0, dy=50.0, xmin=0.0, ymin=0.0, eta_crest=0.05)
    state = solver.step(inflow_m3s=2790.0, outflow_m3s=0.0, rain_mm=0.0, dt_s=3600.0)
    assert float(np.max(np.hypot(state.velocity_x, state.velocity_y))) <= 5.0 + 1e-6


def test_hourly_netcdf_writes_complete_variables(tmp_path):
    z = np.array([[2.0, 1.0], [1.5, 0.5]])
    active = np.ones((2, 2), dtype=bool)
    solver = HourlyInundationSolver(z, active, dx=50.0, dy=50.0, xmin=100.0, ymin=200.0)
    path = tmp_path / "hour.nc"
    write_hourly_netcdf(
        path,
        solver,
        times_s=[0.0, 3600.0],
        inflow=[0.0, 2.0],
        outflow=[0.0, 0.0],
        rain_mm=[0.0, 0.0],
        coordinate_system="EPSG:4548",
        vertical_datum="1985国家高程基准",
    )
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        for name in (
            "water_depth",
            "water_level",
            "velocity_x",
            "velocity_y",
            "velocity_magnitude",
            "discharge",
            "maximum_depth",
            "maximum_velocity",
            "arrival_time",
        ):
            assert name in ds.variables
        assert ds.variables["water_depth"].shape == (2, 2, 2)
        assert ds.variables["water_level"].vertical_datum == "1985国家高程基准"
        assert float(ds.variables["water_depth"][-1].max()) > 0


def test_classify_qingbei_excludes_southern_basin():
    from hydrodynamics.hourly_inundation import classify_qingbei_mask
    x = np.linspace(0.0, 90.0, 10)
    y = np.linspace(0.0, 90.0, 10)
    active = np.ones((10, 10), dtype=bool)
    elevation = np.full((10, 10), 1.0)
    elevation[4, :] = 8.0
    elevation[:4, :] = 2.0
    line = ((-5.0, 0.0), (-5.0, 90.0))
    qingbei, excluded = classify_qingbei_mask(active, x, y, line, elevation)
    assert int(qingbei[:5].sum()) == 0
    assert int(qingbei[4:].sum()) > 0
    assert bool(np.all(excluded[:3]))



def test_classify_qingbei_keeps_western_basin():
    from hydrodynamics.hourly_inundation import classify_qingbei_mask

    x = np.linspace(0.0, 90.0, 10)
    y = np.linspace(0.0, 90.0, 10)
    active = np.zeros((10, 10), dtype=bool)
    elevation = np.full((10, 10), 1.0)
    active[3:5, :4] = True
    elevation[4, :4] = 8.0
    active[:, 6:] = True
    elevation[4, 6:] = 8.0
    line = ((-5.0, 0.0), (-5.0, 90.0))
    qingbei, excluded = classify_qingbei_mask(active, x, y, line, elevation)
    assert int(qingbei[3:5, :4].sum()) == 8
    assert int(qingbei[:4, 6:].sum()) == 0
    assert int(qingbei[5:, 6:].sum()) > 0
    assert bool(np.all(excluded == (active & ~qingbei)))


def test_classify_qingbei_follows_daqing_river_track():
    from hydrodynamics.hourly_inundation import classify_qingbei_mask

    x = np.linspace(0.0, 90.0, 10)
    y = np.linspace(0.0, 90.0, 10)
    active = np.ones((10, 10), dtype=bool)
    elevation = np.ones((10, 10))
    line = ((-5.0, 0.0), (-5.0, 90.0))
    river = (((0.0, 40.0), (90.0, 40.0)),)
    qingbei, excluded = classify_qingbei_mask(
        active, x, y, line, elevation, daqing_river=river
    )
    assert int(qingbei[:5].sum()) == 0
    assert int(qingbei[5:].sum()) == 50
    assert bool(np.all(excluded == (active & ~qingbei)))




def test_connected_fill_does_not_wet_disconnected_pit():
    z = np.array([[1.0, 1.0, 8.0, 0.5, 0.5], [1.0, 1.0, 8.0, 0.5, 0.5]])
    active = np.ones_like(z, dtype=bool)
    inlet = np.zeros_like(active)
    inlet[:, 0] = True
    solver = HourlyInundationSolver(
        z, active, dx=50.0, dy=50.0, xmin=0.0, ymin=0.0,
        inlet_mask=inlet, water_surface_slope=4.77e-5, eta_crest=3.0,
    )
    state = solver.step(inflow_m3s=50.0, outflow_m3s=0.0, rain_mm=0.0, dt_s=3600.0)
    assert float(np.max(state.depth[:, 3:])) == 0.0
    assert float(np.max(state.depth[:, :2])) > 0.0


def test_connected_fill_keeps_pending_volume_until_front_arrives():
    z = np.zeros((2, 40))
    active = np.ones_like(z, dtype=bool)
    inlet = np.zeros_like(active)
    inlet[:, 0] = True
    solver = HourlyInundationSolver(
        z, active, dx=50.0, dy=50.0, xmin=0.0, ymin=0.0,
        manning_n=0.035, inlet_mask=inlet, water_surface_slope=4.77e-5, eta_crest=2.0,
    )
    state = solver.step(inflow_m3s=20.0, outflow_m3s=0.0, rain_mm=0.0, dt_s=3600.0)
    assert float(np.max(state.depth[:, -1])) == 0.0
    wet_cols = int(np.any(state.depth > 0.0, axis=0).sum())
    assert 1 <= wet_cols < 40


def test_conveyance_mask_allows_front_around_barrier():
    z = np.array([[1.0, 1.0, 8.0, 1.0, 1.0], [8.0, 8.0, 8.0, 8.0, 8.0], [1.0, 1.0, 8.0, 1.0, 1.0]])
    active = z < 5.0
    inlet = np.zeros_like(active); inlet[0, 0] = True
    conv = np.zeros_like(active); conv[1, :] = True
    solver = HourlyInundationSolver(
        z, active, dx=50.0, dy=50.0, xmin=0.0, ymin=0.0,
        inlet_mask=inlet, water_surface_slope=4.77e-5, eta_crest=3.0,
        conveyance_mask=conv, manning_n=0.035,
    )
    last = None
    for _ in range(12):
        last = solver.step(inflow_m3s=80.0, outflow_m3s=0.0, rain_mm=0.0, dt_s=3600.0)
    assert last is not None
    assert float(np.max(last.depth[0, 3:])) > 0.0
    assert float(np.max(last.depth[1, :])) == 0.0


def test_area_schedule_fills_lowest_connected_cells_first():
    z = np.array([[1.0, 2.0, 3.0, 9.0, 0.2], [1.2, 2.2, 3.2, 9.0, 0.3]])
    active = z < 8.0
    inlet = np.zeros_like(active); inlet[:, 0] = True
    solver = HourlyInundationSolver(
        z, active, dx=50.0, dy=50.0, xmin=0.0, ymin=0.0,
        inlet_mask=inlet, eta_crest=4.0,
        area_schedule=((0.0, 0.0), (1.0, 0.5), (2.0, 1.0)),
    )
    s1 = solver.step(inflow_m3s=200.0, outflow_m3s=0.0, rain_mm=0.0, dt_s=3600.0)
    assert float(np.max(s1.depth[:, :3])) > 0.0
    assert float(np.max(s1.depth[:, 4])) == 0.0
    s2 = solver.step(inflow_m3s=200.0, outflow_m3s=0.0, rain_mm=0.0, dt_s=3600.0)
    assert float(np.max(s2.depth[:, :3])) > 0.0
    assert float(np.max(s2.depth[:, 4])) == 0.0
