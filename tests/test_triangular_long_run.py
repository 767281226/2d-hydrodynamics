"""Vectorised-kernel equivalence and streaming long-run driver tests."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from hydrodynamics import triangular_solver as solver
from hydrodynamics.triangular_long_run import (
    MemoryFrameSink,
    OpenBoundaryGroupSpec,
    TriangularLongRunError,
    TriangularLongRunInputs,
    TriangularLongRunOptions,
    load_long_run_checkpoint,
    long_run_options_json_schema,
    run_triangular_long_run,
)
from hydrodynamics.triangular_mesh import TriangularMesh
from hydrodynamics.triangular_solver import TriangularSolverExecutionError, _edges, _integrate
from hydrodynamics.triangular_solver_kernels import numba_status, rhs_numba, rhs_numpy
from hydrodynamics.triangular_source_contract import validate_triangular_source_run_request

import triangular_scalar_reference as reference
from test_triangular_source_run import _build_source_case

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "examples" / "triangular_long_run" / "long_run_options.schema.json"
EXAMPLE_PATH = ROOT / "examples" / "triangular_long_run" / "long_run_options.example.json"
HAS_NUMBA = numba_status() == "available"


def _grid_mesh(columns: int = 8, rows: int = 4, dx: float = 10.0, jitter: float = 0.0, seed: int = 0) -> TriangularMesh:
    rng = np.random.default_rng(seed)
    nodes = []
    for j in range(rows + 1):
        for i in range(columns + 1):
            interior = 0 < i < columns and 0 < j < rows
            ox = jitter * dx * rng.uniform(-1.0, 1.0) if interior else 0.0
            oy = jitter * dx * rng.uniform(-1.0, 1.0) if interior else 0.0
            nodes.append((500_000.0 + i * dx + ox, 3_000_000.0 + j * dx + oy))
    faces = []
    for j in range(rows):
        for i in range(columns):
            a = j * (columns + 1) + i
            b, c, d = a + 1, a + columns + 2, a + columns + 1
            faces.extend([(a, b, c), (a, c, d)] if (i + j) % 2 == 0 else [(a, b, d), (b, c, d)])
    return TriangularMesh(nodes, faces, "EPSG:32650")


def _centroid_x(mesh: TriangularMesh) -> np.ndarray:
    return np.asarray([point[0] for point in mesh.face_centroid]) - 500_000.0


def _side_edges(mesh: TriangularMesh, side: str) -> list[int]:
    xs = [point[0] for point in mesh.node_xy]
    target = min(xs) if side == "west" else max(xs)
    answer = []
    for index, ((a, b), (_, right)) in enumerate(zip(mesh.edge_nodes, mesh.edge_faces)):
        if right == -1 and mesh.node_xy[a][0] == target and mesh.node_xy[b][0] == target:
            answer.append(index)
    return answer


def _random_states(mesh: TriangularMesh, rng: np.random.Generator, count: int):
    for _ in range(count):
        depth = np.clip(rng.normal(0.1, 0.15, mesh.n_face), 0.0, None)
        depth[rng.random(mesh.n_face) < 0.2] = 0.0
        depth[rng.random(mesh.n_face) < 0.1] = 5e-7
        wet = depth > 1e-6
        yield np.stack([depth, rng.normal(0, 0.05, mesh.n_face) * wet, rng.normal(0, 0.05, mesh.n_face) * wet], axis=1)


# ---------------------------------------------------------------------------
# 1. Kernel equivalence with the frozen scalar implementation
# ---------------------------------------------------------------------------
def test_vectorised_kernels_are_bitwise_identical_to_scalar_reference():
    mesh = _grid_mesh(10, 7, jitter=0.25, seed=3)
    edges = _edges(mesh)
    x = _centroid_x(mesh)
    bed = 0.01 * x + 0.3 * np.sin(x / 7.0)
    rng = np.random.default_rng(11)
    manning = rng.uniform(0.01, 0.1, mesh.n_face)
    for state in _random_states(mesh, rng, 25):
        expected = reference._rhs_and_cfl(state, mesh, tuple(bed), edges, gravity=9.81, cfl=0.25, dry_depth_m=1e-6)
        actual = solver._rhs_and_cfl(state, mesh, tuple(bed), edges, gravity=9.81, cfl=0.25, dry_depth_m=1e-6)
        assert np.array_equal(actual[0], expected[0]) and actual[1] == expected[1] and actual[2] == expected[2]
        if HAS_NUMBA:
            compiled = rhs_numba(state, bed, edges.geometry, gravity=9.81, cfl=0.25, dry_depth_m=1e-6)
            assert np.array_equal(compiled.derivative, expected[0]) and compiled.cfl_limit_s == expected[1]
            assert compiled.maximum_pair_residual_m3_s == expected[2]
        source_expected = reference._manning_source(state, tuple(manning), gravity=9.81, source_cfl_fraction=0.5, dry_depth_m=1e-6)
        source_actual = solver._manning_source(state, manning, gravity=9.81, source_cfl_fraction=0.5, dry_depth_m=1e-6)
        assert np.array_equal(source_actual[0], source_expected[0])
        assert source_actual[1:] == source_expected[1:]
        noisy = state.copy()
        noisy[rng.random(mesh.n_face) < 0.1, 0] = -1e-13
        clean_expected = reference._sanitize_stage(noisy, mesh, dry_depth_m=1e-6, negative_tolerance_m=1e-12)
        clean_actual = solver._sanitize_stage(noisy, mesh, dry_depth_m=1e-6, negative_tolerance_m=1e-12)
        assert np.array_equal(clean_actual.state, clean_expected.state)
        assert clean_actual.correction_volume_m3 == clean_expected.correction_volume_m3
        assert clean_actual.correction_count == clean_expected.correction_count
        assert solver._volume(state, mesh) == reference._volume(state, mesh)
        assert solver._speed_max(state, 1e-6) == reference._speed_max(state, 1e-6)


@pytest.mark.parametrize("columns,profile,manning", [(8, "stoker", True), (4, "stoker", False), (6, "still_water", True)])
def test_legacy_integrate_matches_scalar_reference_on_accepted_source_cases(tmp_path, monkeypatch, columns, profile, manning):
    intervals = [(0.0, 0.05, 0.01), (0.05, 0.1, 0.02)]
    _, _, source_config, mesh = _build_source_case(
        tmp_path / "case", columns=columns, initial_profile=profile, output_times_s=[0.0, 0.05, 0.1],
        rainfall_intervals=intervals, manning_enabled=manning,
        manning_values_by_original_face=[0.1] * (2 * columns) if manning else None, maximum_time_step_s=0.01,
    )
    capability = validate_triangular_source_run_request(source_config)
    n_values = capability.preparation.manning.interpreted_values if manning else None
    kwargs = dict(
        manning_n_m=n_values, source_cfl_fraction=capability.source_config.source_cfl_fraction if manning else None,
        rainfall_rate_m_s_by_interval=capability.rainfall_rate_m_s_by_interval,
    )
    vector_result, vector_stats = _integrate(capability.base_run, **kwargs)
    for name in ("_rhs_and_cfl", "_manning_source", "_sanitize_stage", "_speed_max", "_volume"):
        monkeypatch.setattr(solver, name, getattr(reference, name))
    scalar_result, scalar_stats = _integrate(capability.base_run, **kwargs)
    # Tolerance: exact binary64 equality of every saved state and diagnostic.
    for name in ("water_depth_m", "momentum_x_m2_s", "momentum_y_m2_s", "maximum_speed_m_s_by_time", "mass_budget"):
        assert vector_result[name] == scalar_result[name]
    for name in ("accepted_step_count", "rejected_reasons", "minimum_accepted_step_s", "maximum_accepted_step_s",
                 "maximum_pair_residual", "cumulative_rainfall_volume_m3_by_time"):
        assert vector_stats[name] == scalar_stats[name]


def test_long_run_explicit_mode_reproduces_legacy_integrator(tmp_path):
    intervals = [(0.0, 0.05, 0.01), (0.05, 0.1, 0.01)]
    _, _, source_config, mesh = _build_source_case(
        tmp_path / "combined", columns=8, initial_profile="stoker", output_times_s=[0.0, 0.05, 0.1],
        rainfall_intervals=intervals, manning_enabled=True, manning_values_by_original_face=[0.1] * 16,
        maximum_time_step_s=0.01,
    )
    capability = validate_triangular_source_run_request(source_config)
    fraction = capability.source_config.source_cfl_fraction
    legacy, stats = _integrate(
        capability.base_run, manning_n_m=capability.preparation.manning.interpreted_values,
        source_cfl_fraction=fraction, rainfall_rate_m_s_by_interval=capability.rainfall_rate_m_s_by_interval,
    )
    inputs = TriangularLongRunInputs.from_capability(
        capability.base_run, manning_n=capability.preparation.manning.interpreted_values,
        rainfall_rate_m_s_by_interval=capability.rainfall_rate_m_s_by_interval,
    )
    config = capability.base_run.config
    options = TriangularLongRunOptions(
        cfl_number=config.cfl_number, minimum_time_step_s=config.minimum_time_step_s,
        maximum_time_step_s=config.maximum_time_step_s, step_cfl_fraction=1.0,
        manning_friction="explicit_source_cfl", source_cfl_fraction=fraction,
        volume_correction_reference="initial_state", kernel="numpy",
    )
    sink = MemoryFrameSink()
    summary = run_triangular_long_run(inputs, options, frame_sink=sink)
    assert summary.accepted_step_count == stats["accepted_step_count"]
    assert summary.rejected_step_reasons == stats["rejected_reasons"]
    for frame, depth, mx, my in zip(sink.frames, legacy["water_depth_m"], legacy["momentum_x_m2_s"], legacy["momentum_y_m2_s"]):
        assert frame.water_depth_m.tolist() == depth
        assert frame.unit_discharge_x_m2_s.tolist() == mx
        assert frame.unit_discharge_y_m2_s.tolist() == my
    # Rain ledger: legacy sums per-face products with fsum, the long run uses
    # rate*dt*total_area with compensated accumulation; agree to 1e-12 relative.
    for row, expected in zip(summary.ledger, stats["cumulative_rainfall_volume_m3_by_time"]):
        assert row["cumulative_rainfall_volume_m3"] == pytest.approx(expected, rel=1e-12, abs=1e-15)
    if HAS_NUMBA:
        compiled = run_triangular_long_run(inputs, options.model_copy(update={"kernel": "numba"}))
        assert np.array_equal(compiled.water_depth_m, summary.water_depth_m)
        assert np.array_equal(compiled.unit_discharge_x_m2_s, summary.unit_discharge_x_m2_s)


# ---------------------------------------------------------------------------
# 2. Physics regression: well-balancing, positivity, mass, friction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("friction", ["none", "point_implicit"])
def test_lake_at_rest_on_non_flat_bed_with_wet_dry_mix_stays_at_rest(friction):
    mesh = _grid_mesh(12, 6, jitter=0.2, seed=5)
    x = _centroid_x(mesh)
    bed = 0.4 * np.sin(x / 15.0) + 0.002 * x
    level = 0.3
    depth = np.maximum(level - bed, 0.0)
    assert (depth == 0.0).any() and (depth > 0.0).any()
    inputs = TriangularLongRunInputs.build(
        mesh, bed_m=bed, initial_depth_m=depth, output_times_s=[0.0, 5.0, 20.0],
        manning_n=None if friction == "none" else np.full(mesh.n_face, 0.03),
    )
    summary = run_triangular_long_run(inputs, {"manning_friction": friction, "kernel": "numpy"})
    assert np.max(np.abs(summary.water_depth_m - depth)) <= 1e-12
    assert np.max(np.abs(summary.unit_discharge_x_m2_s)) <= 1e-12
    assert np.max(np.abs(summary.unit_discharge_y_m2_s)) <= 1e-12
    assert summary.ledger[-1]["cumulative_correction_volume_m3"] == 0.0


def test_dry_start_rain_on_sloped_bed_is_positive_and_mass_conservative():
    mesh = _grid_mesh(10, 5, jitter=0.2, seed=7)
    x = _centroid_x(mesh)
    bed = 0.02 * x + 0.05 * np.cos(x / 9.0)
    rate = 50e-3 / 3600.0
    sink = MemoryFrameSink()
    inputs = TriangularLongRunInputs.build(
        mesh, bed_m=bed, initial_depth_m=np.zeros(mesh.n_face), output_times_s=[0.0, 60.0, 300.0, 600.0],
        manning_n=np.full(mesh.n_face, 0.035), rainfall_rate_m_s_by_interval=[rate] * 3,
    )
    summary = run_triangular_long_run(inputs, {"kernel": "numpy"}, frame_sink=sink)
    assert all((frame.water_depth_m >= 0.0).all() for frame in sink.frames)
    total_area = math.fsum(mesh.face_area)
    final = summary.ledger[-1]
    assert final["cumulative_rainfall_volume_m3"] == pytest.approx(rate * 600.0 * total_area, rel=1e-12)
    balance = final["stored_volume_m3"] - final["cumulative_rainfall_volume_m3"] + final["cumulative_correction_volume_m3"]
    assert abs(balance) <= 1e-10 * final["cumulative_rainfall_volume_m3"]
    assert all(abs(row["residual_m3"]) <= 1e-10 * max(row["cumulative_rainfall_volume_m3"], 1e-30) for row in summary.ledger[1:])
    # Water collects in the low (west) half of the slope.
    assert summary.water_depth_m[x < 30].mean() > summary.water_depth_m[x > 70].mean()


def test_point_implicit_friction_agrees_with_explicit_where_explicit_is_stable():
    mesh = _grid_mesh(16, 3, dx=5.0)
    x = _centroid_x(mesh)
    depth = np.where(x < 40.0, 1.0, 0.5)
    common = dict(bed_m=np.zeros(mesh.n_face), initial_depth_m=depth, output_times_s=[0.0, 2.0, 4.0],
                  manning_n=np.full(mesh.n_face, 0.03))
    inputs = TriangularLongRunInputs.build(mesh, **common)
    explicit = run_triangular_long_run(inputs, {"manning_friction": "explicit_source_cfl", "source_cfl_fraction": 0.5,
                                                "kernel": "numpy", "maximum_time_step_s": 0.05})
    implicit = run_triangular_long_run(inputs, {"manning_friction": "point_implicit", "kernel": "numpy",
                                                "maximum_time_step_s": 0.05})
    frictionless = run_triangular_long_run(TriangularLongRunInputs.build(mesh, **{**common, "manning_n": None}),
                                           {"manning_friction": "none", "kernel": "numpy", "maximum_time_step_s": 0.05})
    scale = np.abs(explicit.unit_discharge_x_m2_s).max()
    difference = np.abs(implicit.unit_discharge_x_m2_s - explicit.unit_discharge_x_m2_s).max()
    friction_effect = np.abs(frictionless.unit_discharge_x_m2_s - explicit.unit_discharge_x_m2_s).max()
    assert friction_effect > 1e-3 * scale  # the comparison actually exercises friction
    assert difference <= 0.05 * friction_effect  # splitting/step-sequence difference << friction effect
    assert np.abs(implicit.water_depth_m - explicit.water_depth_m).max() <= 1e-3


def test_point_implicit_friction_removes_the_explicit_thin_film_step_limit():
    mesh = _grid_mesh(10, 3, dx=10.0)
    x = _centroid_x(mesh)
    bed = 0.01 * (100.0 - x)
    inputs = TriangularLongRunInputs.build(
        mesh, bed_m=bed, initial_depth_m=np.full(mesh.n_face, 2e-4), output_times_s=[0.0, 30.0],
        manning_n=np.full(mesh.n_face, 0.05),
    )
    explicit = run_triangular_long_run(inputs, {"manning_friction": "explicit_source_cfl", "source_cfl_fraction": 0.5, "kernel": "numpy"})
    implicit = run_triangular_long_run(inputs, {"manning_friction": "point_implicit", "kernel": "numpy"})
    assert implicit.accepted_step_count * 3 < explicit.accepted_step_count
    assert (implicit.water_depth_m >= 0.0).all()
    assert abs(implicit.ledger[-1]["residual_m3"]) <= 1e-12 * max(implicit.ledger[0]["stored_volume_m3"], 1.0)


# ---------------------------------------------------------------------------
# 3. Streaming, ledger, step cap, checkpoints
# ---------------------------------------------------------------------------
def test_frame_sink_receives_each_output_as_read_only_arrays():
    mesh = _grid_mesh(6, 3)
    x = _centroid_x(mesh)
    inputs = TriangularLongRunInputs.build(mesh, bed_m=np.zeros(mesh.n_face), initial_depth_m=np.where(x < 30, 1.0, 0.2),
                                           output_times_s=[0.0, 0.5, 1.0, 1.5])
    received = []
    summary = run_triangular_long_run(inputs, {"manning_friction": "none", "kernel": "numpy"}, frame_sink=received.append)
    assert [frame.output_index for frame in received] == [0, 1, 2, 3]
    assert [frame.time_s for frame in received] == [0.0, 0.5, 1.0, 1.5]
    frame = received[-1]
    assert not frame.water_depth_m.flags.writeable
    assert np.array_equal(frame.water_level_m, frame.water_depth_m)  # flat zero bed
    wet = frame.water_depth_m > 1e-6
    assert np.allclose(frame.velocity_x_m_s[wet] * frame.water_depth_m[wet], frame.unit_discharge_x_m2_s[wet])
    assert np.array_equal(frame.water_depth_m, summary.water_depth_m)
    assert len(summary.ledger) == 4 and summary.ledger[-1]["interval_accepted_step_count"] > 0
    assert (summary.maximum_depth_m >= frame.water_depth_m).all() and summary.maximum_speed_m_s.max() > 0.0
    assert not hasattr(summary, "frames")


def test_step_cap_is_configurable():
    mesh = _grid_mesh(6, 3)
    x = _centroid_x(mesh)
    inputs = TriangularLongRunInputs.build(mesh, bed_m=np.zeros(mesh.n_face), initial_depth_m=np.where(x < 30, 1.0, 0.2),
                                           output_times_s=[0.0, 1.0])
    with pytest.raises(TriangularSolverExecutionError) as caught:
        run_triangular_long_run(inputs, {"manning_friction": "none", "kernel": "numpy", "maximum_accepted_step_count": 5})
    assert caught.value.reason == "step_safety_limit"


def test_checkpoint_restart_reproduces_uninterrupted_run_exactly(tmp_path):
    mesh = _grid_mesh(8, 3, jitter=0.1, seed=2)
    x = _centroid_x(mesh)
    bed = 0.01 * x
    inputs = TriangularLongRunInputs.build(
        mesh, bed_m=bed, initial_depth_m=np.where(x < 40, 0.5, 0.0), output_times_s=[0.0, 1.0, 2.0, 3.0, 4.0],
        manning_n=np.full(mesh.n_face, 0.03), rainfall_rate_m_s_by_interval=[1e-4] * 4,
    )
    options = {"kernel": "numpy", "checkpoint_interval_s": 2.0, "checkpoint_keep_count": 5}
    full = run_triangular_long_run(inputs, options, checkpoint_directory=tmp_path / "ckpt")
    assert [Path(path).name for path in full.checkpoints_written] == ["checkpoint_00000002.npz", "checkpoint_00000004.npz"]
    metadata, arrays = load_long_run_checkpoint(full.checkpoints_written[0])
    assert metadata["output_index"] == 2 and arrays["state"].shape == (mesh.n_face, 3)
    received = []
    resumed = run_triangular_long_run(inputs, options, checkpoint_directory=tmp_path / "ckpt2",
                                      resume_from=full.checkpoints_written[0], frame_sink=received.append)
    assert [frame.output_index for frame in received] == [3, 4]
    assert np.array_equal(resumed.water_depth_m, full.water_depth_m)
    assert np.array_equal(resumed.unit_discharge_x_m2_s, full.unit_discharge_x_m2_s)
    assert np.array_equal(resumed.maximum_depth_m, full.maximum_depth_m)
    assert resumed.accepted_step_count == full.accepted_step_count
    def without_wall_clock(rows):
        return [{key: value for key, value in row.items() if key != "interval_wall_clock_s"} for row in rows]

    assert without_wall_clock(resumed.ledger) == without_wall_clock(full.ledger)
    other = TriangularLongRunInputs.build(mesh, bed_m=bed * 2, initial_depth_m=inputs.initial_depth_m,
                                          output_times_s=inputs.output_times_s, manning_n=inputs.manning_n,
                                          rainfall_rate_m_s_by_interval=inputs.rainfall_rate_m_s_by_interval)
    with pytest.raises(TriangularLongRunError, match="does not match"):
        run_triangular_long_run(other, options, checkpoint_directory=tmp_path / "x", resume_from=full.checkpoints_written[0])


def test_correction_reference_grows_with_cumulative_inflow():
    mesh = _grid_mesh(4, 2)
    inputs = TriangularLongRunInputs.build(mesh, bed_m=np.zeros(mesh.n_face), initial_depth_m=np.zeros(mesh.n_face),
                                           output_times_s=[0.0, 100.0], rainfall_rate_m_s_by_interval=[1e-4])
    grown = run_triangular_long_run(inputs, {"manning_friction": "none", "kernel": "numpy"})
    fixed = run_triangular_long_run(inputs, {"manning_friction": "none", "kernel": "numpy",
                                             "volume_correction_reference": "initial_state"})
    rain = grown.ledger[-1]["cumulative_rainfall_volume_m3"]
    assert fixed.volume_correction_reference_scale_m3 == 1.0
    assert grown.volume_correction_reference_scale_m3 == pytest.approx(max(rain, 1.0))
    assert grown.ledger[0]["volume_correction_reference_scale_m3"] == 1.0


@pytest.mark.parametrize("update,message", [
    ({"manning_friction": "explicit_source_cfl"}, "source_cfl_fraction"),
    ({"source_cfl_fraction": 0.5}, "only valid"),
    ({"step_cfl_fraction": 0.0}, "step_cfl_fraction"),
    ({"maximum_accepted_step_count": 0}, "maximum_accepted_step_count"),
    ({"volume_correction_reference": "rain"}, "volume_correction_reference"),
    ({"kernel": "gpu"}, "kernel"),
    ({"unknown": 1}, "unknown"),
])
def test_long_run_options_reject_invalid_values(update, message):
    with pytest.raises(ValueError, match=message):
        TriangularLongRunOptions.model_validate(update)


def test_options_schema_and_example_are_current():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema == long_run_options_json_schema()
    example = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    options_schema = {**schema["$defs"]["TriangularLongRunOptions"], "$defs": schema["$defs"]["TriangularLongRunOptions"].get("$defs", {})}
    jsonschema.validate(example["options"], options_schema)
    TriangularLongRunOptions.model_validate(example["options"])
    for group in example["open_boundaries"]:
        jsonschema.validate(group, schema["$defs"]["OpenBoundaryGroupSpec"])
        OpenBoundaryGroupSpec.model_validate(group)


# ---------------------------------------------------------------------------
# 4. Prescribed-discharge open boundaries
# ---------------------------------------------------------------------------
def _series(value: float, end: float = 1_036_800.0) -> dict:
    return {"time_s": [0.0, end], "discharge_m3_s": [value, value]}


def test_inflow_into_dry_closed_basin_stores_exactly_the_inflow_volume():
    mesh = _grid_mesh(10, 4, dx=10.0)
    x = _centroid_x(mesh)
    bed = 0.005 * x + 0.1 * np.sin(x / 13.0)
    q = 0.4
    group = {"name": "A", "role": "inflow", "edge_indices": _side_edges(mesh, "west"), **_series(q)}
    sink = MemoryFrameSink()
    inputs = TriangularLongRunInputs.build(mesh, bed_m=bed, initial_depth_m=np.zeros(mesh.n_face),
                                           output_times_s=[0.0, 30.0, 120.0, 300.0], manning_n=np.full(mesh.n_face, 0.035),
                                           open_boundaries=[group])
    summary = run_triangular_long_run(inputs, {"kernel": "numpy"}, frame_sink=sink)
    assert all((frame.water_depth_m >= 0.0).all() for frame in sink.frames)
    for row in summary.ledger:
        assert row["cumulative_inflow_volume_m3"] == pytest.approx(q * row["time_s"], rel=1e-12, abs=1e-12)
        assert abs(row["stored_volume_m3"] - q * row["time_s"] + row["cumulative_correction_volume_m3"]) <= 1e-10 * max(q * row["time_s"], 1.0)
        assert row["groups"]["A"]["cumulative_inflow_volume_m3"] == pytest.approx(row["cumulative_inflow_volume_m3"], rel=1e-12, abs=1e-12)
    assert summary.water_depth_m[x < 20].mean() > 0.0
    assert summary.volume_correction_reference_scale_m3 == pytest.approx(q * 300.0, rel=1e-12)


@pytest.mark.parametrize("distribution", ["length", "conveyance_depth_5_3"])
def test_outflow_is_capped_by_available_water_and_records_shortfall(distribution):
    mesh = _grid_mesh(6, 3, dx=10.0)
    x = _centroid_x(mesh)
    bed = 0.01 * (60.0 - x)  # slopes down to the east outlet
    depth = np.full(mesh.n_face, 0.02)
    initial_volume = math.fsum((depth * np.asarray(mesh.face_area)).tolist())
    q = 5.0  # far more than the basin holds over the run
    group = {"name": "B", "role": "outflow", "edge_indices": _side_edges(mesh, "east"), "distribution": distribution, **_series(q)}
    inputs = TriangularLongRunInputs.build(mesh, bed_m=bed, initial_depth_m=depth, output_times_s=[0.0, 20.0, 200.0],
                                           manning_n=np.full(mesh.n_face, 0.03), open_boundaries=[group])
    summary = run_triangular_long_run(inputs, {"kernel": "numpy"})
    final = summary.ledger[-1]
    assert (summary.water_depth_m >= 0.0).all()
    assert final["cumulative_outflow_requested_volume_m3"] == pytest.approx(q * 200.0, rel=1e-12)
    delivered = final["cumulative_outflow_delivered_volume_m3"]
    assert 0.5 * initial_volume < delivered <= initial_volume
    assert final["cumulative_outflow_shortfall_volume_m3"] == pytest.approx(q * 200.0 - delivered, rel=1e-12)
    assert final["groups"]["B"]["cumulative_outflow_shortfall_volume_m3"] == pytest.approx(final["cumulative_outflow_shortfall_volume_m3"], rel=1e-12)
    assert abs(final["stored_volume_m3"] - (initial_volume - delivered) - final["cumulative_correction_volume_m3"]) <= 1e-10 * initial_volume


def test_balanced_inflow_and_outflow_approach_steady_state():
    mesh = _grid_mesh(12, 3, dx=10.0)
    x = _centroid_x(mesh)
    bed = 0.001 * (120.0 - x)
    q = 0.3
    groups = [
        {"name": "A", "role": "inflow", "edge_indices": _side_edges(mesh, "west"), **_series(q)},
        {"name": "B", "role": "outflow", "edge_indices": _side_edges(mesh, "east"), **_series(q)},
    ]
    times = [0.0] + [600.0 * k for k in range(1, 7)]
    inputs = TriangularLongRunInputs.build(mesh, bed_m=bed, initial_depth_m=np.full(mesh.n_face, 0.3), output_times_s=times,
                                           manning_n=np.full(mesh.n_face, 0.03), open_boundaries=groups)
    summary = run_triangular_long_run(inputs, {"kernel": "numpy"})
    stored = [row["stored_volume_m3"] for row in summary.ledger]
    changes = [abs(b - a) for a, b in zip(stored, stored[1:])]
    assert changes[-1] < 0.05 * q * 600.0  # storage change per interval << interval throughput
    assert summary.ledger[-1]["interval_outflow_shortfall_volume_m3"] <= 1e-9 * q * 600.0
    for row in summary.ledger:
        assert abs(row["residual_m3"]) <= 1e-10 * max(row["stored_volume_m3"], 1.0)
    assert np.isfinite(summary.unit_discharge_x_m2_s).all() and (summary.water_depth_m > 0.0).all()


def test_zero_discharge_groups_are_exactly_closed_walls():
    mesh = _grid_mesh(8, 3, jitter=0.2, seed=4)
    x = _centroid_x(mesh)
    bed = 0.3 * np.sin(x / 11.0)
    level = np.where(x < 40, 0.6, 0.2)
    common = dict(bed_m=bed, initial_depth_m=np.maximum(level - bed, 0.0), output_times_s=[0.0, 1.0, 3.0],
                  manning_n=np.full(mesh.n_face, 0.03))
    walls = run_triangular_long_run(TriangularLongRunInputs.build(mesh, **common), {"kernel": "numpy"})
    groups = [{"name": "A", "role": "inflow", "edge_indices": _side_edges(mesh, "west"), **_series(0.0)},
              {"name": "B", "role": "outflow", "edge_indices": _side_edges(mesh, "east"), **_series(0.0)}]
    open_zero = run_triangular_long_run(TriangularLongRunInputs.build(mesh, **common, open_boundaries=groups), {"kernel": "numpy"})
    assert np.array_equal(walls.water_depth_m, open_zero.water_depth_m)
    assert np.array_equal(walls.unit_discharge_x_m2_s, open_zero.unit_discharge_x_m2_s)


def test_open_boundary_groups_are_validated_against_the_mesh_and_run():
    mesh = _grid_mesh(4, 2)
    west = _side_edges(mesh, "west")
    interior = next(index for index, (_, right) in enumerate(mesh.edge_faces) if right != -1)
    common = dict(bed_m=np.zeros(mesh.n_face), initial_depth_m=np.zeros(mesh.n_face), output_times_s=[0.0, 10.0])

    def build(*groups):
        return TriangularLongRunInputs.build(mesh, **common, open_boundaries=list(groups))

    with pytest.raises(TriangularLongRunError, match="interior edge"):
        build({"name": "A", "role": "inflow", "edge_indices": [interior], **_series(1.0)})
    with pytest.raises(TriangularLongRunError, match="more than one group"):
        build({"name": "A", "role": "inflow", "edge_indices": west, **_series(1.0)},
              {"name": "B", "role": "outflow", "edge_indices": west[:1], **_series(1.0)})
    with pytest.raises(TriangularLongRunError, match="cover"):
        build({"name": "A", "role": "inflow", "edge_indices": west, **_series(1.0, end=5.0)})
    with pytest.raises(TriangularLongRunError, match="magnitudes"):
        build({"name": "A", "role": "inflow", "edge_indices": west, "time_s": [0.0, 10.0], "discharge_m3_s": [1.0, -1.0]})
    with pytest.raises(TriangularLongRunError, match="unique"):
        build({"name": "A", "role": "inflow", "edge_indices": west, **_series(1.0)},
              {"name": "A", "role": "outflow", "edge_indices": _side_edges(mesh, "east"), **_series(1.0)})
    with pytest.raises(TriangularLongRunError, match="role"):
        build({"name": "A", "role": "both", "edge_indices": west, **_series(1.0)})


def test_discharge_series_is_linearly_interpolated():
    mesh = _grid_mesh(6, 2, dx=10.0)
    group = {"name": "A", "role": "inflow", "edge_indices": _side_edges(mesh, "west"),
             "time_s": [0.0, 100.0, 200.0], "discharge_m3_s": [0.0, 1.0, 0.0]}
    inputs = TriangularLongRunInputs.build(mesh, bed_m=np.zeros(mesh.n_face), initial_depth_m=np.zeros(mesh.n_face),
                                           output_times_s=[0.0, 100.0, 200.0], open_boundaries=[group])
    summary = run_triangular_long_run(inputs, {"manning_friction": "none", "kernel": "numpy"})
    # Trapezoidal stage sampling of a piecewise-linear Q(t) is exact.
    assert summary.ledger[1]["cumulative_inflow_volume_m3"] == pytest.approx(50.0, rel=1e-12)
    assert summary.ledger[2]["cumulative_inflow_volume_m3"] == pytest.approx(100.0, rel=1e-12)


# ---------------------------------------------------------------------------
# 5. Stage-dependent free outflow (critical-flow weir law)
# ---------------------------------------------------------------------------
KERNELS = ["numpy"] + (["numba"] if HAS_NUMBA else [])


def _free(mesh: TriangularMesh, side: str = "east", **extra) -> dict:
    return {"name": "C", "role": "free_outflow", "edge_indices": _side_edges(mesh, side), **extra}


@pytest.mark.parametrize("kernel", KERNELS)
def test_free_outflow_below_crest_is_exactly_a_closed_wall(kernel):
    mesh = _grid_mesh(8, 3, jitter=0.2, seed=4)
    x = _centroid_x(mesh)
    bed = 0.3 * np.sin(x / 11.0)
    level = 0.1
    depth = np.maximum(level - bed, 0.0)
    assert (depth == 0.0).any() and (depth > 0.0).any()
    common = dict(bed_m=bed, initial_depth_m=depth, output_times_s=[0.0, 2.0, 6.0], manning_n=np.full(mesh.n_face, 0.03))
    walls = run_triangular_long_run(TriangularLongRunInputs.build(mesh, **common), {"kernel": kernel})
    group = _free(mesh, crest_elevation_m=0.5, reference_time_s=[0.0, 6.0], reference_discharge_m3_s=[1.0, 1.0])
    free = run_triangular_long_run(TriangularLongRunInputs.build(mesh, **common, open_boundaries=[group]), {"kernel": kernel})
    # Lake at rest below the crest: H = 0 on every open edge, so the boundary is exactly a wall.
    assert np.array_equal(free.water_depth_m, walls.water_depth_m)
    assert np.array_equal(free.unit_discharge_x_m2_s, walls.unit_discharge_x_m2_s)
    assert np.max(np.abs(free.water_depth_m - depth)) <= 1e-12
    final = free.ledger[-1]
    assert final["cumulative_outflow_delivered_volume_m3"] == 0.0 and final["groups"]["C"]["outflow_discharge_m3_s"] == 0.0
    # The reference series is only recorded, never applied.
    assert final["groups"]["C"]["cumulative_reference_outflow_volume_m3"] == pytest.approx(6.0, rel=1e-12)
    assert final["groups"]["C"]["reference_outflow_discharge_m3_s"] == 1.0


@pytest.mark.parametrize("crest,coefficient", [(None, None), (0.1, 0.4)])
def test_free_outflow_initial_discharge_equals_critical_weir_law(crest, coefficient):
    from hydrodynamics.triangular_long_run import FREE_OUTFLOW_DEFAULT_DISCHARGE_COEFFICIENT, _boundary_stage, _prepare_boundary_groups
    mesh = _grid_mesh(6, 3, dx=10.0)
    depth = 0.4
    east = _side_edges(mesh, "east")
    extra = {}
    if crest is not None:
        extra["crest_elevation_m"] = crest
    if coefficient is not None:
        extra["discharge_coefficient"] = coefficient
    group = {"name": "C", "role": "free_outflow", "edge_indices": east[:1], **extra}
    inputs = TriangularLongRunInputs.build(mesh, bed_m=np.zeros(mesh.n_face), initial_depth_m=np.full(mesh.n_face, depth),
                                           output_times_s=[0.0, 1.0], open_boundaries=[group])
    edges = _edges(mesh)
    length = float(edges.geometry.length[east[0]])
    assert length == pytest.approx(10.0, rel=1e-12)
    head = depth - (0.0 if crest is None else crest)
    cd = FREE_OUTFLOW_DEFAULT_DISCHARGE_COEFFICIENT if coefficient is None else coefficient
    assert FREE_OUTFLOW_DEFAULT_DISCHARGE_COEFFICIENT == pytest.approx(0.5443310539518174, rel=1e-15)
    expected = cd * math.sqrt(9.81) * head ** 1.5 * length
    # Stage-0 boundary term on the flat pool: requested == delivered == C_d sqrt(g) H^1.5 L (the cap is not active).
    groups = _prepare_boundary_groups(inputs, edges.geometry)
    state = np.zeros((mesh.n_face, 3)); state[:, 0] = depth
    stage = _boundary_stage(groups, state, 0.0, 1e-3, edges.geometry.area, gravity=9.81, dry_depth_m=1e-6, outflow_fraction=0.5)
    assert stage.outflow_requested_m3_s["C"] == pytest.approx(expected, rel=1e-14)
    assert stage.outflow_delivered_m3_s["C"] == pytest.approx(expected, rel=1e-14)
    face = edges.geometry.left[east[0]]
    assert stage.derivative[face, 0] == pytest.approx(-expected / edges.geometry.area[face], rel=1e-14)
    summary = run_triangular_long_run(inputs, {"manning_friction": "none", "kernel": "numpy"})
    assert summary.ledger[0]["groups"]["C"]["outflow_discharge_m3_s"] == pytest.approx(expected, rel=1e-14)
    assert "cumulative_reference_outflow_volume_m3" not in summary.ledger[0]["groups"]["C"]
    assert 0.0 < summary.ledger[-1]["cumulative_outflow_delivered_volume_m3"] <= expected * 1.0 * 1.001


@pytest.mark.parametrize("kernel", KERNELS)
def test_free_outflow_drains_basin_above_crest_and_ledger_closes(kernel):
    mesh = _grid_mesh(12, 3, dx=10.0)
    area = math.fsum(mesh.face_area)
    crest = 0.2
    depth = np.full(mesh.n_face, 0.5)
    initial = math.fsum((depth * np.asarray(mesh.face_area)).tolist())
    inputs = TriangularLongRunInputs.build(mesh, bed_m=np.zeros(mesh.n_face), initial_depth_m=depth,
                                           output_times_s=[0.0, 60.0, 300.0, 900.0], manning_n=np.full(mesh.n_face, 0.03),
                                           open_boundaries=[_free(mesh, crest_elevation_m=crest)])
    summary = run_triangular_long_run(inputs, {"kernel": kernel})
    stored = [row["stored_volume_m3"] for row in summary.ledger]
    assert all(b < a for a, b in zip(stored, stored[1:]))
    discharge = [row["groups"]["C"]["outflow_discharge_m3_s"] for row in summary.ledger]
    assert discharge[0] > discharge[-1] > 0.0
    above_crest = initial - crest * area
    assert stored[-1] < initial - 0.5 * above_crest      # most water above the crest has left
    assert stored[-1] > 0.95 * crest * area               # nothing below the crest drains through the weir
    for row in summary.ledger:
        delivered = row["cumulative_outflow_delivered_volume_m3"]
        # Delivered volume equals the stored-volume decrease to round-off.
        assert abs((initial - row["stored_volume_m3"]) - delivered + row["cumulative_correction_volume_m3"]) <= 1e-11 * initial
        assert abs(row["residual_m3"]) <= 1e-11 * initial
        assert row["groups"]["C"]["cumulative_outflow_delivered_volume_m3"] == pytest.approx(delivered, rel=1e-12, abs=1e-12)
        assert row["cumulative_outflow_requested_volume_m3"] >= delivered - 1e-9
    assert (summary.water_depth_m >= 0.0).all()


def test_free_outflow_spec_validation():
    mesh = _grid_mesh(4, 2)
    east = _side_edges(mesh, "east")
    common = dict(bed_m=np.zeros(mesh.n_face), initial_depth_m=np.zeros(mesh.n_face), output_times_s=[0.0, 10.0])

    def build(**group):
        return TriangularLongRunInputs.build(mesh, **common, open_boundaries=[{"name": "C", "edge_indices": east, **group}])

    build(role="free_outflow")
    with pytest.raises(TriangularLongRunError, match="time_s/discharge_m3_s are not allowed"):
        build(role="free_outflow", **_series(1.0))
    with pytest.raises(TriangularLongRunError, match="distribution does not apply"):
        build(role="free_outflow", distribution="conveyance_depth_5_3")
    with pytest.raises(TriangularLongRunError, match="together"):
        build(role="free_outflow", reference_time_s=[0.0, 10.0])
    with pytest.raises(TriangularLongRunError, match="reference_discharge_m3_s values are magnitudes"):
        build(role="free_outflow", reference_time_s=[0.0, 10.0], reference_discharge_m3_s=[1.0, -1.0])
    with pytest.raises(TriangularLongRunError, match="reference discharge samples cover"):
        build(role="free_outflow", reference_time_s=[0.0, 5.0], reference_discharge_m3_s=[1.0, 1.0])
    for bad in (0.0, 1.6, True, "0.5", float("nan")):
        with pytest.raises(TriangularLongRunError, match="discharge_coefficient"):
            build(role="free_outflow", discharge_coefficient=bad)
    with pytest.raises(TriangularLongRunError, match="crest_elevation_m"):
        build(role="free_outflow", crest_elevation_m=20000.0)
    with pytest.raises(TriangularLongRunError, match="only valid for role free_outflow"):
        build(role="outflow", discharge_coefficient=0.5, **_series(1.0))
    with pytest.raises(TriangularLongRunError, match="requires time_s and discharge_m3_s"):
        build(role="outflow")


def test_prescribed_group_fingerprint_is_unchanged_by_free_outflow_fields():
    from hydrodynamics.triangular_long_run import _inputs_fingerprint
    mesh = _grid_mesh(4, 2)
    group = OpenBoundaryGroupSpec.model_validate({"name": "B", "role": "outflow", "edge_indices": _side_edges(mesh, "east"), **_series(1.0)})
    assert set(group.model_dump(mode="json", exclude_none=True)) == {"format", "name", "role", "edge_indices", "time_s", "discharge_m3_s", "distribution"}
    common = dict(bed_m=np.zeros(mesh.n_face), initial_depth_m=np.zeros(mesh.n_face), output_times_s=[0.0, 10.0])
    geometry = _edges(mesh).geometry
    free_default = TriangularLongRunInputs.build(mesh, **common, open_boundaries=[_free(mesh)])
    free_explicit = TriangularLongRunInputs.build(mesh, **common, open_boundaries=[_free(mesh, discharge_coefficient=(2.0 / 3.0) ** 1.5)])
    free_other = TriangularLongRunInputs.build(mesh, **common, open_boundaries=[_free(mesh, discharge_coefficient=0.4)])
    free_crest = TriangularLongRunInputs.build(mesh, **common, open_boundaries=[_free(mesh, crest_elevation_m=0.0)])
    prints = [_inputs_fingerprint(inputs, geometry) for inputs in (free_default, free_explicit, free_other, free_crest)]
    assert prints[0] == prints[1] and len({prints[0], prints[2], prints[3]}) == 3


def test_free_outflow_checkpoint_restart_reproduces_uninterrupted_run(tmp_path):
    mesh = _grid_mesh(10, 3, dx=10.0, jitter=0.1, seed=6)
    x = _centroid_x(mesh)
    groups = [{"name": "A", "role": "inflow", "edge_indices": _side_edges(mesh, "west"), **_series(0.4)},
              _free(mesh, reference_time_s=[0.0, 20.0, 40.0], reference_discharge_m3_s=[0.0, 0.5, 0.1])]
    inputs = TriangularLongRunInputs.build(mesh, bed_m=0.002 * (100.0 - x), initial_depth_m=np.full(mesh.n_face, 0.1),
                                           output_times_s=[0.0, 10.0, 20.0, 30.0, 40.0], manning_n=np.full(mesh.n_face, 0.03),
                                           open_boundaries=groups)
    options = {"kernel": "numpy", "checkpoint_interval_s": 20.0, "checkpoint_keep_count": 5}
    full = run_triangular_long_run(inputs, options, checkpoint_directory=tmp_path / "ckpt")
    resumed = run_triangular_long_run(inputs, options, checkpoint_directory=tmp_path / "ckpt2", resume_from=full.checkpoints_written[0])
    assert np.array_equal(resumed.water_depth_m, full.water_depth_m)
    assert np.array_equal(resumed.unit_discharge_y_m2_s, full.unit_discharge_y_m2_s)
    strip = lambda rows: [{k: v for k, v in row.items() if k != "interval_wall_clock_s"} for row in rows]  # noqa: E731
    assert strip(resumed.ledger) == strip(full.ledger)
    final = full.ledger[-1]["groups"]["C"]
    assert final["cumulative_reference_outflow_volume_m3"] == pytest.approx(0.5 * 20 * 0.5 + 0.5 * 20 * 0.6, rel=1e-12)
    assert final["cumulative_outflow_delivered_volume_m3"] > 0.0
    other = TriangularLongRunInputs.build(mesh, bed_m=inputs.bed_m, initial_depth_m=inputs.initial_depth_m,
                                          output_times_s=inputs.output_times_s, manning_n=inputs.manning_n,
                                          open_boundaries=[groups[0], {**groups[1], "discharge_coefficient": 0.4}])
    with pytest.raises(TriangularLongRunError, match="does not match"):
        run_triangular_long_run(other, options, checkpoint_directory=tmp_path / "x", resume_from=full.checkpoints_written[0])


@pytest.mark.skipif(not HAS_NUMBA, reason="optional numba accelerator is not installed")
def test_numba_and_numpy_agree_with_free_outflow():
    mesh = _grid_mesh(10, 4, dx=10.0, jitter=0.15, seed=9)
    x = _centroid_x(mesh)
    bed = 0.004 * (100.0 - x) + 0.05 * np.sin(x / 9.0)
    groups = [{"name": "A", "role": "inflow", "edge_indices": _side_edges(mesh, "west"), **_series(0.5)},
              _free(mesh, reference_time_s=[0.0, 300.0], reference_discharge_m3_s=[0.3, 0.3])]
    inputs = TriangularLongRunInputs.build(mesh, bed_m=bed, initial_depth_m=np.where(x > 50.0, 0.05, 0.0), output_times_s=[0.0, 60.0, 300.0],
                                           manning_n=np.full(mesh.n_face, 0.035), rainfall_rate_m_s_by_interval=[1e-5, 1e-5],
                                           open_boundaries=groups)
    vector = run_triangular_long_run(inputs, {"kernel": "numpy"})
    compiled = run_triangular_long_run(inputs, {"kernel": "numba"})
    assert vector.ledger[-1]["cumulative_outflow_delivered_volume_m3"] > 0.0
    assert np.allclose(compiled.water_depth_m, vector.water_depth_m, rtol=1e-9, atol=1e-12)
    assert np.allclose(compiled.unit_discharge_x_m2_s, vector.unit_discharge_x_m2_s, rtol=1e-8, atol=1e-12)
    for key in ("stored_volume_m3", "cumulative_outflow_delivered_volume_m3"):
        assert compiled.ledger[-1][key] == pytest.approx(vector.ledger[-1][key], rel=1e-9)
    assert compiled.ledger[-1]["groups"]["C"]["outflow_discharge_m3_s"] == pytest.approx(
        vector.ledger[-1]["groups"]["C"]["outflow_discharge_m3_s"], rel=1e-8)


@pytest.mark.skipif(not HAS_NUMBA, reason="optional numba accelerator is not installed")
def test_numba_and_numpy_long_run_paths_agree_with_friction_rain_and_boundaries():
    mesh = _grid_mesh(10, 4, dx=10.0, jitter=0.15, seed=9)
    x = _centroid_x(mesh)
    bed = 0.004 * x + 0.05 * np.sin(x / 9.0)
    groups = [
        {"name": "A", "role": "inflow", "edge_indices": _side_edges(mesh, "west"), "distribution": "conveyance_depth_5_3", **_series(0.5)},
        {"name": "B", "role": "outflow", "edge_indices": _side_edges(mesh, "east"), **_series(0.2)},
    ]
    inputs = TriangularLongRunInputs.build(mesh, bed_m=bed, initial_depth_m=np.zeros(mesh.n_face), output_times_s=[0.0, 60.0, 240.0],
                                           manning_n=np.full(mesh.n_face, 0.035), rainfall_rate_m_s_by_interval=[1e-5, 1e-5],
                                           open_boundaries=groups)
    vector = run_triangular_long_run(inputs, {"kernel": "numpy"})
    compiled = run_triangular_long_run(inputs, {"kernel": "numba"})
    assert compiled.kernel == "numba" and vector.kernel == "numpy"
    # Only the implicit-friction magnitude uses a different hypot routine
    # (numpy vs C math); the trajectories agree to round-off.
    assert np.allclose(compiled.water_depth_m, vector.water_depth_m, rtol=1e-9, atol=1e-12)
    assert np.allclose(compiled.unit_discharge_x_m2_s, vector.unit_discharge_x_m2_s, rtol=1e-8, atol=1e-12)
    assert compiled.ledger[-1]["stored_volume_m3"] == pytest.approx(vector.ledger[-1]["stored_volume_m3"], rel=1e-12)


@pytest.mark.parametrize("depth", [1e-5, 0.05, 0.4])
def test_free_outflow_head_never_exceeds_face_depth_when_crest_is_below_bed(depth):
    """A crest below the face bed must not draw more than critical flow from depth h (thin films kept the CFL bound finite)."""
    from hydrodynamics.triangular_long_run import FREE_OUTFLOW_DEFAULT_DISCHARGE_COEFFICIENT, _boundary_stage, _prepare_boundary_groups
    mesh = _grid_mesh(6, 3, dx=10.0)
    east = _side_edges(mesh, "east")
    bed = np.full(mesh.n_face, 1.5)  # crest 1.03 m lies 0.47 m below the bed
    group = {"name": "B", "role": "free_outflow", "edge_indices": east[:1], "crest_elevation_m": 1.03}
    inputs = TriangularLongRunInputs.build(mesh, bed_m=bed, initial_depth_m=np.full(mesh.n_face, depth),
                                           output_times_s=[0.0, 1.0], open_boundaries=[group])
    edges = _edges(mesh)
    groups = _prepare_boundary_groups(inputs, edges.geometry)
    state = np.zeros((mesh.n_face, 3)); state[:, 0] = depth
    stage = _boundary_stage(groups, state, 0.0, 1e-3, edges.geometry.area, gravity=9.81, dry_depth_m=1e-6, outflow_fraction=0.5)
    length = float(edges.geometry.length[east[0]])
    expected = FREE_OUTFLOW_DEFAULT_DISCHARGE_COEFFICIENT * math.sqrt(9.81) * depth ** 1.5 * length  # H = h, not h + 0.47
    assert stage.outflow_requested_m3_s["B"] == pytest.approx(expected, rel=1e-12)
