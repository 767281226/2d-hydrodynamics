from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from hydrodynamics.mesh_preparation import write_ugrid_preparation
from hydrodynamics.triangular_business import (
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    write_triangular_business_package,
)
from hydrodynamics.triangular_manning_netcdf import read_triangular_manning_result
from hydrodynamics.triangular_manning import run_triangular_manning
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh
from hydrodynamics.triangular_solver import (
    TriangularSolverExecutionError,
    _attempt_step,
    _edges,
    run_triangular_solver,
)
from hydrodynamics.triangular_solver_netcdf import TriangularResultNetCDFError, read_triangular_result
from hydrodynamics.triangular_source import main as source_main, run_triangular_source
from hydrodynamics.triangular_source_contract import (
    TriangularSourceContractError,
    TriangularSourceRunConfig,
    load_triangular_source_run_config,
    validate_triangular_source_run_request,
    validate_triangular_source_result_for_run,
)
from hydrodynamics.triangular_source_netcdf import (
    read_triangular_source_result,
    write_triangular_source_result_atomic,
)
from hydrodynamics.triangular_sources import load_triangular_source_preparation_config
from hydrodynamics.triangular_sources import prepare_triangular_sources
from hydrodynamics.triangular_sources import read_triangular_source_preparation


def _build_source_case(
    root: Path,
    *,
    width_m: float = 2.0,
    height_m: float = 1.0,
    columns: int = 2,
    initial_depth_m: float = 0.2,
    initial_profile: str = "still_water",
    output_times_s: list[float] | None = None,
    rainfall_intervals: list[tuple[float, float, float]] | None = None,
    rainfall_unit: str = "m/s",
    manning_enabled: bool = False,
    manning_values_by_original_face: list[float] | None = None,
    source_cfl_fraction: float = 0.5,
    minimum_time_step_s: float = 1e-6,
    maximum_time_step_s: float = 0.01,
    cfl_number: float = 0.25,
    reverse_faces: bool = False,
    rotate: bool = False,
    unequal_area: bool = False,
    source_mode: str | None = None,
) -> tuple[Path, Path, Path, TriangularMesh]:
    root.mkdir(parents=True, exist_ok=True)
    dx = width_m / columns
    node_xy = []
    for row in range(2):
        for column in range(columns + 1):
            x = column * dx
            y = 0.0 if row == 0 else height_m + (0.1 * x if unequal_area else 0.0)
            point = (500_000.0 - y, x) if rotate else (500_000.0 + x, y)
            node_xy.append(point)
    original_faces: list[tuple[int, int, int]] = []
    original_depths: list[float] = []
    for column in range(columns):
        bottom_left = column
        bottom_right = column + 1
        top_left = columns + 1 + column
        top_right = top_left + 1
        original_faces.extend(((bottom_left, bottom_right, top_right), (bottom_left, top_right, top_left)))
        first_centroid_x = (column + 2.0 / 3.0) * dx
        second_centroid_x = (column + 1.0 / 3.0) * dx
        if initial_profile == "stoker":
            original_depths.extend((2.0 if first_centroid_x < width_m / 2 else 1.0,
                                    2.0 if second_centroid_x < width_m / 2 else 1.0))
        elif initial_profile == "fully_dry":
            original_depths.extend((0.0, 0.0))
        elif initial_profile == "still_water":
            original_depths.extend((initial_depth_m, initial_depth_m))
        else:
            raise ValueError(f"unknown synthetic profile {initial_profile!r}")

    face_count = len(original_faces)
    order = list(reversed(range(face_count))) if reverse_faces else list(range(face_count))
    faces = [original_faces[index] for index in order]
    depths = [original_depths[index] for index in order]
    face_ids = [9_300_001 + index for index in order]
    mesh = TriangularMesh(
        node_xy, faces, "EPSG:32650", mesh_id=f"synthetic-source-{root.name}", face_id=face_ids,
    )
    terrain = FaceTerrainField(
        mesh.mesh_id,
        (0.0,) * mesh.n_face,
        (True,) * mesh.n_face,
        tuple(mesh.face_area),
        tuple(mesh.face_area),
        (1.0,) * mesh.n_face,
        mesh.coordinate_system,
    )
    mesh_path = root / "mesh.nc"
    write_ugrid_preparation(mesh_path, mesh, terrain)
    initial = TriangularInitialConditionField.from_assignments(
        mesh, terrain, "depth", tuple(zip(mesh.face_id, depths)), source="synthetic source-run initial field",
    )
    if manning_enabled:
        values = manning_values_by_original_face or [0.1] * face_count
        if len(values) != face_count:
            raise ValueError("Manning n sequence must be supplied in original face order")
        roughness = TriangularRoughnessField.from_assignments(
            mesh,
            tuple((face_id, values[original_index]) for face_id, original_index in zip(mesh.face_id, order)),
            source="synthetic face-ID-aligned Manning field",
        )
    else:
        roughness = TriangularRoughnessField.from_constant(mesh, 0.035, source="synthetic retained roughness")
    business_path = root / "business.json"
    write_triangular_business_package(
        business_path, TriangularBusinessPackage(mesh, terrain, roughness, initial, ()), mesh_artifact=mesh_path,
    )
    case_id = f"synthetic-source-{root.name}"
    times = output_times_s or [0.0, 0.1]
    base_path = root / "base-run.yaml"
    base_path.write_text(yaml.safe_dump({
        "format": "triangular-solver-run-v1", "schema_version": "1.0", "case_id": case_id,
        "business_file": "business.json", "result_file": "base-none.nc", "time_origin": None,
        "output_times_s": times, "boundary_policy": "closed_wall", "initial_momentum": "zero",
        "sources": "none", "gravity_m_s2": 9.81, "cfl_number": cfl_number,
        "minimum_time_step_s": minimum_time_step_s, "maximum_time_step_s": maximum_time_step_s,
        "maximum_step_retries": 8, "dry_depth_threshold_m": 1e-6,
        "negative_depth_tolerance_m": 1e-12, "volume_correction_budget_fraction": 1e-10,
    }, sort_keys=False), encoding="utf-8")
    source_config_path = root / "source-preparation.yaml"
    manning_request: dict[str, object] = {"enabled": False}
    if manning_enabled:
        manning_request = {
            "enabled": True,
            "legacy_unit1_policy": "legacy_unit1_as_si_manning",
            "interpretation_rationale": "Synthetic P4-2C case explicitly interprets unchanged P3-A unit-1 values as SI Manning n.",
        }
    interval_data = rainfall_intervals or [(float(times[0]), float(times[-1]), 0.0)]
    source_config_path.write_text(yaml.safe_dump({
        "format": "triangular-source-preparation-v1", "schema_version": "1.0", "case_id": case_id,
        "run_config_file": "base-run.yaml", "output_file": "prepared-sources.json",
        "source_cfl_fraction": source_cfl_fraction,
        "manning": manning_request,
        "rainfall": {
            "enabled": True, "unit": rainfall_unit,
            "intervals": [
                {"start_time_s": start, "end_time_s": end, "intensity": intensity}
                for start, end, intensity in interval_data
            ],
        },
    }, sort_keys=False), encoding="utf-8")
    source_mode = source_mode or ("manning_and_rainfall" if manning_enabled else "rainfall")
    source_run_path = root / "source-run.yaml"
    source_run_path.write_text(yaml.safe_dump({
        "format": "triangular-source-run-v1", "schema_version": "1.0", "case_id": case_id,
        "sources": source_mode, "base_run_config_file": "base-run.yaml",
        "source_config_file": "source-preparation.yaml", "result_file": "source-result.nc",
        "overwrite_existing_result": False,
    }, sort_keys=False), encoding="utf-8")
    from hydrodynamics.triangular_sources import prepare_triangular_sources
    prepare_triangular_sources(source_config_path)
    return base_path, source_config_path, source_run_path, mesh


def _integrated_momentum(result, areas: tuple[float, ...]) -> float:
    return math.fsum(area * math.hypot(hx, hy) for area, hx, hy in zip(
        areas, result.momentum_x_m2_s[-1], result.momentum_y_m2_s[-1],
    ))


def test_source_run_schema_and_examples_are_strict():
    examples = Path(__file__).parents[1] / "examples" / "triangular_sources"
    for name, expected in (
        ("rainfall_only_run.yaml", "rainfall"),
        ("manning_and_rainfall_run.yaml", "manning_and_rainfall"),
    ):
        config = load_triangular_source_run_config(examples / name)
        assert config.sources == expected
    for name in ("rainfall_only_source_preparation.yaml", "manning_and_rainfall_source_preparation.yaml"):
        prep = load_triangular_source_preparation_config(examples / name)
        assert prep.rainfall.enabled is True
    assert load_triangular_source_run_config(examples / "rainfall_only_run.yaml").sources == "rainfall"
    assert load_triangular_source_run_config(examples / "manning_and_rainfall_run.yaml").sources == "manning_and_rainfall"

    valid = {
        "format": "triangular-source-run-v1", "schema_version": "1.0", "case_id": "case",
        "sources": "rainfall", "base_run_config_file": "base.yaml",
        "source_config_file": "prep.yaml", "result_file": "source.nc",
    }
    assert TriangularSourceRunConfig.model_validate(valid).sources == "rainfall"
    for mutation in ({"sources": "none"}, {"sources": "manning"}, {"format": "triangular-manning-run-v1"}, {"unknown": True}):
        with pytest.raises(ValidationError):
            TriangularSourceRunConfig.model_validate({**valid, **mutation})


def test_source_cli_runs_versioned_rainfall_config(tmp_path, capsys):
    _, _, config, _ = _build_source_case(
        tmp_path / "source-cli", width_m=10.0, height_m=10.0, columns=1,
        output_times_s=[0.0, 0.01], rainfall_intervals=[(0.0, 0.01, 0.0)],
        rainfall_unit="mm/h", maximum_time_step_s=0.01,
    )
    assert source_main([str(config)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"
    assert report["sources_applied"] == "rainfall"
    assert (config.parent / "source-result.nc").is_file()


def test_source_cli_executes_the_versioned_run(tmp_path, capsys):
    _, _, config, _ = _build_source_case(
        tmp_path / "source-cli", width_m=10.0, height_m=10.0, columns=1,
        initial_depth_m=0.2, output_times_s=[0.0, 0.01],
        rainfall_intervals=[(0.0, 0.01, 0.0)], rainfall_unit="mm/h", maximum_time_step_s=0.01,
    )
    assert source_main([str(config)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"
    assert report["sources_applied"] == "rainfall"
    assert (config.parent / "source-result.nc").is_file()


@pytest.mark.parametrize(
    "initial_depth_m,unit,intensity,reference_index",
    [(0.0, "mm/h", 36.0, 0), (0.2, "mm/h", 36.0, 1), (0.2, "m/s", 1e-5, 1)],
)
def test_rain_only_uniform_accumulation_matches_frozen_area_integral(
    tmp_path, initial_depth_m, unit, intensity, reference_index,
):
    _, _, config, mesh = _build_source_case(
        tmp_path / f"rain-{unit}-{initial_depth_m}", width_m=10.0, height_m=10.0, columns=1,
        initial_depth_m=initial_depth_m, output_times_s=[0.0, 100.0],
        rainfall_intervals=[(0.0, 100.0, intensity)], rainfall_unit=unit,
        maximum_time_step_s=1.0,
    )
    report = run_triangular_source(config)
    result = read_triangular_source_result(config.parent / "source-result.nc", config)
    reference = json.loads(
        (Path(__file__).parents[1] / "examples/triangular_sources/source_benchmark_references.json").read_text(encoding="utf-8")
    )["cases"][2]
    expected_depth = reference["reference_final_depths_m"][reference_index]
    expected_rain_depth = reference["reference_depth_increments_m"][reference_index]
    expected_volume = reference["reference_volume_increment_m3"]
    assert report["status"] == "completed"
    assert result.format == "triangular-source-result-v1"
    assert result.sources_applied == "rainfall"
    assert result.rainfall_enabled and result.rainfall_applied
    assert not result.manning_enabled and not result.manning_applied
    assert all(value == pytest.approx(initial_depth_m, abs=1e-12) for value in result.water_depth_m[0])
    assert all(value == pytest.approx(expected_depth, abs=1e-10) for value in result.water_depth_m[-1])
    assert all(abs(value) <= 1e-10 for row in result.momentum_x_m2_s for value in row), result.momentum_x_m2_s
    assert all(abs(value) <= 1e-10 for row in result.momentum_y_m2_s for value in row), result.momentum_y_m2_s
    assert result.rainfall_total_volume_m3 == pytest.approx(expected_volume, abs=reference["candidate_volume_error_tolerance_m3"])
    assert result.rainfall_total_depth_m == pytest.approx(expected_rain_depth, abs=1e-12)
    assert result.rainfall_volume_m3_by_interval == pytest.approx([expected_volume], abs=reference["candidate_volume_error_tolerance_m3"])
    assert result.mass_budget[-1].source_net_volume_m3 == pytest.approx(expected_volume, abs=reference["candidate_volume_error_tolerance_m3"])
    assert result.mass_budget[-1].residual_m3 == pytest.approx(0.0, abs=reference["candidate_volume_error_tolerance_m3"])
    assert result.mass_correction_count == 0
    assert result.rainfall_nonzero_effect_applied is True
    assert result.rainfall_stage_evaluation_count == 2 * result.accepted_step_count
    assert math.fsum(mesh.face_area) == pytest.approx(100.0)


def test_enabled_zero_rain_is_applied_but_has_no_nonzero_effect(tmp_path):
    _, _, config, _ = _build_source_case(
        tmp_path / "zero-rain", width_m=10.0, height_m=10.0, columns=1,
        initial_depth_m=0.2, output_times_s=[0.0, 0.1],
        rainfall_intervals=[(0.0, 0.1, 0.0)], rainfall_unit="m/s", maximum_time_step_s=0.1,
    )
    run_triangular_source(config)
    result = read_triangular_source_result(config.parent / "source-result.nc", config)
    assert result.rainfall_enabled and result.rainfall_applied
    assert result.rainfall_nonzero_effect_applied is False
    assert result.rainfall_stage_evaluation_count > 0
    assert result.rainfall_nonzero_source_stage_evaluation_count == 0
    assert result.rainfall_total_volume_m3 == 0.0
    assert result.water_depth_m[-1] == pytest.approx(result.water_depth_m[0], abs=1e-12)


def test_zero_rain_result_cannot_claim_a_nonzero_effect_from_consistent_metadata(tmp_path):
    import netCDF4

    _, _, config, _ = _build_source_case(
        tmp_path / "zero-rain-metadata-tamper", width_m=10.0, height_m=10.0, columns=1,
        initial_depth_m=0.2, output_times_s=[0.0, 0.01],
        rainfall_intervals=[(0.0, 0.01, 0.0)], rainfall_unit="m/s", maximum_time_step_s=0.01,
    )
    run_triangular_source(config)
    path = config.parent / "source-result.nc"
    actual = read_triangular_source_result(path, config)
    assert actual.rainfall_total_volume_m3 == 0.0
    assert actual.rainfall_stage_evaluation_count > 0
    assert actual.rainfall_nonzero_source_stage_evaluation_count == 0
    with netCDF4.Dataset(path, "r+") as dataset:
        metadata = json.loads(dataset.contract_metadata_json)
        metadata["rainfall_nonzero_source_stage_evaluation_count"] = actual.rainfall_stage_evaluation_count
        metadata["rainfall_nonzero_effect_applied"] = True
        dataset.contract_metadata_json = json.dumps(metadata)
        dataset.rainfall_nonzero_effect_applied = np.int8(1)
    with pytest.raises(TriangularResultNetCDFError, match="all-zero rainfall"):
        read_triangular_source_result(path, config)


def test_rainfall_stop_and_jump_uses_old_interval_through_breakpoint_stage_two(tmp_path):
    intervals = [(0.0, 30.0, 10.0), (30.0, 90.0, 0.0), (90.0, 120.0, 20.0)]
    _, _, config, _ = _build_source_case(
        tmp_path / "rain-jump", width_m=10.0, height_m=10.0, columns=1,
        initial_depth_m=0.2, output_times_s=[0.0, 30.0, 90.0, 120.0],
        rainfall_intervals=intervals, rainfall_unit="mm/h", maximum_time_step_s=1.0,
    )
    run_triangular_source(config)
    result = read_triangular_source_result(config.parent / "source-result.nc", config)
    reference = json.loads((Path(__file__).parents[1] / "examples/triangular_sources/source_benchmark_references.json").read_text(encoding="utf-8"))["cases"][3]
    assert result.rainfall_volume_m3_by_interval == pytest.approx(
        [item["volume_m3"] for item in reference["intervals_left_closed_right_open_mm_h"]], abs=1e-12,
    )
    assert result.cumulative_rainfall_volume_m3_by_time == pytest.approx([0.0, 0.008333333333333333, 0.008333333333333333, 0.025], abs=1e-12)
    assert result.rainfall_rate_m_s_by_interval == pytest.approx([10.0 / 3_600_000.0, 0.0, 20.0 / 3_600_000.0])
    assert result.water_depth_m[-1] == pytest.approx([0.20025, 0.20025], abs=1e-10)
    assert result.mass_budget[-1].source_net_volume_m3 == pytest.approx(0.025, abs=1e-12)
    assert abs(result.mass_budget[-1].residual_m3) <= 1e-10 * result.volume_correction_reference_scale_m3


def test_production_ssprk2_joint_ode_matches_frozen_rainfall_manning_reference(tmp_path, monkeypatch):
    _, _, _, mesh = _build_source_case(
        tmp_path / "joint-local-ode", width_m=1.0, height_m=1.0, columns=1,
        output_times_s=[0.0, 10.0], rainfall_intervals=[(0.0, 10.0, 0.01)],
        rainfall_unit="m/s", manning_enabled=True, maximum_time_step_s=1.0,
    )
    from hydrodynamics import triangular_solver as solver
    monkeypatch.setattr(solver, "_rhs_and_cfl", lambda state, *args, **kwargs: (np.zeros_like(state), math.inf, 0.0))
    references = json.loads((Path(__file__).parents[1] / "examples/triangular_sources/source_benchmark_references.json").read_text(encoding="utf-8"))
    reference = references["cases"][4]
    initial = np.zeros((mesh.n_face, 3), dtype=np.float64)
    initial[:, 0] = reference["h0_m"]
    initial[:, 1] = reference["initial_q_m2_s"][0]
    initial[:, 2] = reference["initial_q_m2_s"][1]
    errors = []
    for dt, tolerance in zip(reference["comparison_time_steps_s"], reference["candidate_max_component_error_tolerances_m2_s"]):
        state = initial.copy()
        for _ in range(round(reference["duration_s"] / dt)):
            attempt = _attempt_step(
                state, dt, mesh, (0.0,) * mesh.n_face, _edges(mesh),
                gravity=reference["gravity_m_s2"], cfl=0.25, dry_depth_m=1e-6,
                negative_tolerance_m=1e-12, correction_budget_remaining_m3=1.0,
                manning_n_m=[reference["manning_n"]] * mesh.n_face, source_cfl_fraction=0.5,
                rainfall_rate_m_s=reference["rainfall_rate_m_s"],
            )
            assert attempt[4] is None
            state = attempt[0]
        exact = reference["exact_q_m2_s"]
        error = max(abs(float(state[0, 1]) - exact[0]), abs(float(state[0, 2]) - exact[1]))
        errors.append(error)
        assert error <= tolerance
        assert float(state[0, 0]) == pytest.approx(reference["exact_final_depth_m"], abs=1e-12)
    assert errors[2] < errors[1] < errors[0]


def test_rainfall_source_increases_depth_without_direct_momentum_source(tmp_path, monkeypatch):
    _, _, _, mesh = _build_source_case(
        tmp_path / "rain-momentum-local", width_m=1.0, height_m=1.0, columns=1,
        output_times_s=[0.0, 0.1], rainfall_intervals=[(0.0, 0.1, 0.01)],
    )
    from hydrodynamics import triangular_solver as solver
    monkeypatch.setattr(solver, "_rhs_and_cfl", lambda state, *args, **kwargs: (np.zeros_like(state), math.inf, 0.0))
    state = np.tile([1.0, 0.6, 0.8], (mesh.n_face, 1)).astype(np.float64)
    attempt = _attempt_step(
        state, 0.1, mesh, (0.0,) * mesh.n_face, _edges(mesh),
        gravity=9.81, cfl=0.25, dry_depth_m=1e-6,
        negative_tolerance_m=1e-12, correction_budget_remaining_m3=1.0,
        rainfall_rate_m_s=0.01,
    )
    assert attempt[4] is None
    assert attempt[0][:, 0] == pytest.approx([1.001] * mesh.n_face, abs=1e-14)
    assert attempt[0][:, 1] == pytest.approx([0.6] * mesh.n_face, abs=1e-14)
    assert attempt[0][:, 2] == pytest.approx([0.8] * mesh.n_face, abs=1e-14)


def test_combined_dynamic_case_preserves_base_result_and_integrates_rain_mass(tmp_path):
    intervals = [(0.0, 0.05, 0.01), (0.05, 0.1, 0.01)]
    base_config, _, source_config, mesh = _build_source_case(
        tmp_path / "combined-dynamic", width_m=2.0, height_m=1.0, columns=8,
        initial_profile="stoker", output_times_s=[0.0, 0.05, 0.1],
        rainfall_intervals=intervals, rainfall_unit="m/s", manning_enabled=True,
        manning_values_by_original_face=[0.1] * 16, maximum_time_step_s=0.01,
    )
    run_triangular_solver(base_config)
    base_path = base_config.parent / "base-none.nc"
    base_bytes = base_path.read_bytes()
    base = read_triangular_result(base_path, base_config)
    report = run_triangular_source(source_config)
    result = read_triangular_source_result(source_config.parent / "source-result.nc", source_config)
    assert report["status"] == "completed"
    assert result.sources_applied == "manning_and_rainfall"
    assert result.manning_enabled and result.manning_applied
    assert result.rainfall_enabled and result.rainfall_applied
    assert result.nonzero_manning_effect_applied and result.rainfall_nonzero_effect_applied
    assert result.manning_n == [0.1] * mesh.n_face
    assert result.rainfall_volume_m3_by_interval == pytest.approx([0.001, 0.001], abs=1e-12)
    assert result.cumulative_rainfall_volume_m3_by_time == pytest.approx([0.0, 0.001, 0.002], abs=1e-12)
    assert result.mass_budget[-1].source_net_volume_m3 == pytest.approx(0.002, abs=1e-12)
    assert abs(result.mass_budget[-1].residual_m3) <= 1e-10 * result.volume_correction_reference_scale_m3
    assert result.volume_correction_budget_m3 == pytest.approx(base.volume_correction_budget_m3, abs=1e-15)
    assert _integrated_momentum(result, mesh.face_area) < _integrated_momentum(base, mesh.face_area)
    assert base_path.read_bytes() == base_bytes
    with pytest.raises(TriangularResultNetCDFError, match="unsupported triangular result format"):
        read_triangular_result(source_config.parent / "source-result.nc", base_config, enforce_configured_result_path=False)
    preparation = read_triangular_source_preparation(
        source_config.parent / "prepared-sources.json", source_config.parent / "source-preparation.yaml",
    )
    assert preparation.manning.applied is False and preparation.rainfall.applied is False


def test_combined_source_fraction_binds_direct_validation_writer_and_reader(tmp_path):
    _, _, config, _ = _build_source_case(
        tmp_path / "source-fraction-binding", width_m=2.0, height_m=1.0, columns=2,
        initial_profile="stoker", output_times_s=[0.0, 0.01],
        rainfall_intervals=[(0.0, 0.01, 0.01)], rainfall_unit="m/s",
        manning_enabled=True, manning_values_by_original_face=[0.1] * 4,
        source_cfl_fraction=0.5, maximum_time_step_s=0.01,
    )
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["overwrite_existing_result"] = True
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    run_triangular_source(config)
    path = config.parent / "source-result.nc"
    prior_bytes = path.read_bytes()
    original = read_triangular_source_result(path, config)
    assert original.source_cfl_fraction == 0.5

    changed = original.model_copy(update={"source_cfl_fraction": 0.75})
    with pytest.raises(TriangularSourceContractError, match="source_cfl_fraction"):
        validate_triangular_source_result_for_run(changed, config)
    with pytest.raises(TriangularSourceContractError, match="source_cfl_fraction"):
        write_triangular_source_result_atomic(changed, config)
    assert path.read_bytes() == prior_bytes

    import netCDF4
    with netCDF4.Dataset(path, "r+") as dataset:
        metadata = json.loads(dataset.contract_metadata_json)
        metadata["source_cfl_fraction"] = 0.75
        dataset.contract_metadata_json = json.dumps(metadata)
    with pytest.raises(TriangularResultNetCDFError, match="source_cfl_fraction"):
        read_triangular_source_result(path, config)


def test_combined_source_handles_unequal_area_rotation_face_ids_and_full_q_vector(tmp_path):
    face_count = 4
    n_by_face_id_order = [0.05, 0.08, 0.11, 0.14]
    common = {
        "width_m": 2.0, "height_m": 1.0, "columns": 2,
        "initial_profile": "stoker", "output_times_s": [0.0, 0.01],
        "rainfall_intervals": [(0.0, 0.01, 0.01)], "rainfall_unit": "m/s",
        "manning_enabled": True, "manning_values_by_original_face": n_by_face_id_order,
        "maximum_time_step_s": 0.01, "unequal_area": True,
    }
    _, _, normal_config, normal_mesh = _build_source_case(tmp_path / "combined-normal", **common)
    _, _, rotated_config, rotated_mesh = _build_source_case(
        tmp_path / "combined-rotated", reverse_faces=True, rotate=True, **common,
    )
    assert len({round(area, 12) for area in normal_mesh.face_area}) > 1
    run_triangular_source(normal_config)
    run_triangular_source(rotated_config)
    normal = read_triangular_source_result(normal_config.parent / "source-result.nc", normal_config)
    rotated = read_triangular_source_result(rotated_config.parent / "source-result.nc", rotated_config)

    def keyed(result):
        state = {
            face_id: (h, qx, qy)
            for face_id, h, qx, qy in zip(
                result.face_id, result.water_depth_m[-1],
                result.momentum_x_m2_s[-1], result.momentum_y_m2_s[-1],
            )
        }
        n_by_id = dict(zip(result.manning_face_id, result.manning_n))
        return state, n_by_id

    normal_state, normal_n = keyed(normal)
    rotated_state, rotated_n = keyed(rotated)
    expected_n = {9_300_001 + index: value for index, value in enumerate(n_by_face_id_order)}
    assert normal_n == rotated_n == expected_n
    assert set(normal_state) == set(rotated_state) == set(normal.face_id)
    for face_id, (h, qx, qy) in normal_state.items():
        rotated_h, rotated_qx, rotated_qy = rotated_state[face_id]
        assert rotated_h == pytest.approx(h, rel=1e-10, abs=1e-12)
        assert rotated_qx == pytest.approx(-qy, rel=1e-10, abs=1e-12)
        assert rotated_qy == pytest.approx(qx, rel=1e-10, abs=1e-12)
    assert normal.rainfall_total_volume_m3 == pytest.approx(rotated.rainfall_total_volume_m3, abs=1e-12)
    for result in (normal, rotated):
        assert result.nonzero_manning_effect_applied is True
        assert abs(result.mass_budget[-1].residual_m3) <= 1e-10 * result.volume_correction_reference_scale_m3


def test_rainfall_ledger_rolls_back_with_manning_source_cfl_rejections(tmp_path):
    base_config, _, source_config, mesh = _build_source_case(
        tmp_path / "rain-source-retries", width_m=2.0, height_m=1.0, columns=2,
        initial_profile="stoker", output_times_s=[0.0, 0.01],
        rainfall_intervals=[(0.0, 0.01, 0.01)], rainfall_unit="m/s",
        manning_enabled=True, manning_values_by_original_face=[1.0] * 4,
        source_cfl_fraction=1e-4, maximum_time_step_s=0.01,
    )
    report = run_triangular_source(source_config)
    result = read_triangular_source_result(source_config.parent / "source-result.nc", source_config)
    expected_rain_volume = 0.01 * 0.01 * math.fsum(mesh.face_area)
    assert result.rejected_step_reasons.get("second_stage_source_cfl", 0) > 0
    assert result.rainfall_volume_m3_by_interval == pytest.approx([expected_rain_volume], abs=1e-12)
    assert result.mass_budget[-1].source_net_volume_m3 == pytest.approx(expected_rain_volume, abs=1e-12)
    assert result.mass_budget[-1].residual_m3 == pytest.approx(0.0, abs=1e-10 * result.volume_correction_reference_scale_m3)
    assert report["rejected_step_reasons"] == result.rejected_step_reasons


def test_combined_source_minimum_step_failure_does_not_publish_rain_ledger(tmp_path):
    _, _, config, _ = _build_source_case(
        tmp_path / "rain-minimum-failure", width_m=2.0, height_m=1.0, columns=2,
        initial_profile="stoker", output_times_s=[0.0, 0.01],
        rainfall_intervals=[(0.0, 0.01, 0.01)], rainfall_unit="m/s",
        manning_enabled=True, manning_values_by_original_face=[1.0] * 4,
        source_cfl_fraction=1e-12, maximum_time_step_s=0.01,
    )
    with pytest.raises(TriangularSolverExecutionError) as caught:
        run_triangular_source(config)
    assert caught.value.reason == "source_cfl_below_minimum_time_step"
    assert caught.value.last_reason == "second_stage_source_cfl"
    assert not (config.parent / "source-result.nc").exists()


def test_combined_source_alignment_tail_rechecks_both_stages_and_does_not_publish(tmp_path, monkeypatch):
    duration = 1e-6 + 1.1e-12
    _, _, config, _ = _build_source_case(
        tmp_path / "rain-tail-failure", width_m=2.0, height_m=1.0, columns=2,
        initial_depth_m=1e-4, output_times_s=[0.0, duration],
        rainfall_intervals=[(0.0, duration, 0.01)], rainfall_unit="m/s",
        manning_enabled=True, manning_values_by_original_face=[0.1] * 4,
        source_cfl_fraction=4e-28, minimum_time_step_s=1e-6, maximum_time_step_s=1e-6,
    )
    from hydrodynamics import triangular_solver as solver
    calls = {"count": 0}

    def inject_momentum_only_during_tail(current, *args, **kwargs):
        calls["count"] += 1
        derivative = np.zeros_like(current)
        if calls["count"] > 4:
            derivative[:, 1] = 0.03
        return derivative, math.inf, 0.0

    monkeypatch.setattr(solver, "_rhs_and_cfl", inject_momentum_only_during_tail)
    with pytest.raises(TriangularSolverExecutionError) as caught:
        run_triangular_source(config)
    assert caught.value.reason == "source_cfl_alignment"
    assert caught.value.stage == "stage2" and caught.value.accepted_step_count == 1
    assert not (config.parent / "source-result.nc").exists()


def test_source_result_clobber_and_format_protection(tmp_path):
    _, _, config, _ = _build_source_case(
        tmp_path / "source-protection", width_m=10.0, height_m=10.0, columns=1,
        output_times_s=[0.0, 0.01], rainfall_intervals=[(0.0, 0.01, 0.0)],
        rainfall_unit="mm/h", maximum_time_step_s=0.01,
    )
    result_path = config.parent / "source-result.nc"
    result_path.write_bytes(b"existing non-source artifact")
    with pytest.raises(TriangularSourceContractError, match="overwrite_existing_result"):
        run_triangular_source(config)
    assert result_path.read_bytes() == b"existing non-source artifact"

    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["overwrite_existing_result"] = True
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    result_path.unlink()

    # Create a genuine old Manning-format result at the target path. The new
    # source overwrite permission must not authorize replacing that artifact.
    old_source_path = config.parent / "old-manning-source-preparation.yaml"
    case_id = yaml.safe_load((config.parent / "base-run.yaml").read_text(encoding="utf-8"))["case_id"]
    old_source_path.write_text(yaml.safe_dump({
        "format": "triangular-source-preparation-v1", "schema_version": "1.0", "case_id": case_id,
        "run_config_file": "base-run.yaml", "output_file": "old-manning-prepared.json",
        "manning": {
            "enabled": True, "legacy_unit1_policy": "legacy_unit1_as_si_manning",
            "interpretation_rationale": "A synthetic prior Manning result used only for output-format protection.",
        },
        "rainfall": {"enabled": False},
    }, sort_keys=False), encoding="utf-8")
    prepare_triangular_sources(old_source_path)
    old_manning_config = config.parent / "old-manning-run.yaml"
    old_manning_config.write_text(yaml.safe_dump({
        "format": "triangular-manning-run-v1", "schema_version": "1.0", "case_id": case_id,
        "sources": "manning", "base_run_config_file": "base-run.yaml",
        "source_config_file": old_source_path.name, "result_file": "source-result.nc",
        "overwrite_existing_result": False,
    }, sort_keys=False), encoding="utf-8")
    run_triangular_manning(old_manning_config)
    old_manning_bytes = result_path.read_bytes()
    with pytest.raises(TriangularResultNetCDFError, match="only a prior result bound"):
        run_triangular_source(config)
    assert result_path.read_bytes() == old_manning_bytes

    result_path.unlink()
    run_triangular_source(config)
    valid = read_triangular_source_result(result_path, config)
    assert valid.format == "triangular-source-result-v1"
    run_triangular_source(config)
    assert read_triangular_source_result(result_path, config).format == "triangular-source-result-v1"


def test_explicit_overwrite_can_replace_a_prior_same_source_result(tmp_path):
    _, _, config, _ = _build_source_case(
        tmp_path / "source-overwrite", width_m=10.0, height_m=10.0, columns=1,
        output_times_s=[0.0, 0.01], rainfall_intervals=[(0.0, 0.01, 0.0)],
        rainfall_unit="m/s", maximum_time_step_s=0.01,
    )
    run_triangular_source(config)
    result_path = config.parent / "source-result.nc"
    prior = read_triangular_source_result(result_path, config)
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["overwrite_existing_result"] = True
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    run_triangular_source(config)
    replaced = read_triangular_source_result(result_path, config)
    assert replaced.source_run_config_sha256 != prior.source_run_config_sha256
    assert replaced.format == "triangular-source-result-v1"


@pytest.mark.parametrize("mutation", ["rainfall_rate_units", "rainfall_volume", "state_metadata", "base_case_id"])
def test_source_result_reader_rejects_tampered_rainfall_variables(tmp_path, mutation):
    import netCDF4

    _, _, config, _ = _build_source_case(
        tmp_path / f"tampered-{mutation}", width_m=10.0, height_m=10.0, columns=1,
        output_times_s=[0.0, 0.1], rainfall_intervals=[(0.0, 0.1, 0.01)],
        rainfall_unit="m/s", maximum_time_step_s=0.1,
    )
    run_triangular_source(config)
    path = config.parent / "source-result.nc"
    with netCDF4.Dataset(path, "r+") as dataset:
        if mutation == "state_metadata":
            metadata = json.loads(dataset.contract_metadata_json)
            metadata["water_depth_m"] = [[1.0] * len(dataset.dimensions["face"])] * len(dataset.dimensions["time"])
            dataset.contract_metadata_json = json.dumps(metadata)
        elif mutation == "base_case_id":
            metadata = json.loads(dataset.contract_metadata_json)
            metadata["case_id"] = "injected-case"
            dataset.contract_metadata_json = json.dumps(metadata)
        elif mutation == "rainfall_rate_units":
            dataset.variables["rainfall_rate_m_s"].units = "mm/h"
        else:
            dataset.variables["rainfall_volume"][0] += 0.001
    with pytest.raises(TriangularResultNetCDFError):
        read_triangular_source_result(path, config)


def test_reader_checks_each_output_rain_volume_even_when_schedule_total_matches(tmp_path):
    import netCDF4

    _, _, config, mesh = _build_source_case(
        tmp_path / "per-output-rain-integral", width_m=2.0, height_m=1.0, columns=2,
        initial_profile="stoker", output_times_s=[0.0, 0.05, 0.1],
        rainfall_intervals=[(0.0, 0.1, 0.01)], rainfall_unit="m/s", maximum_time_step_s=0.01,
    )
    run_triangular_source(config)
    path = config.parent / "source-result.nc"
    delta = 1e-10
    expected_volume = 0.01 * 0.05 * math.fsum(mesh.face_area)
    assert expected_volume == pytest.approx(0.001, abs=1e-15)
    with netCDF4.Dataset(path, "r+") as dataset:
        interval_volumes = dataset.variables["rainfall_volume"][:]
        interval_volumes[0] += delta
        interval_volumes[1] -= delta
        dataset.variables["rainfall_volume"][:] = interval_volumes
        cumulative = dataset.variables["rainfall_cumulative_volume"][:]
        cumulative[1] += delta
        dataset.variables["rainfall_cumulative_volume"][:] = cumulative
        source_volume = dataset.variables["mass_budget_source_volume"][:]
        source_volume[1] += delta
        dataset.variables["mass_budget_source_volume"][:] = source_volume
        residual = dataset.variables["mass_budget_residual"][:]
        residual[1] -= delta
        dataset.variables["mass_budget_residual"][:] = residual
    with pytest.raises(TriangularResultNetCDFError, match="output interval 0 volume"):
        read_triangular_source_result(path, config)


def test_source_result_readback_failure_and_late_no_clobber_race_preserve_files(tmp_path, monkeypatch):
    import hydrodynamics.triangular_source_netcdf as source_io
    real_reader = source_io.read_triangular_source_result

    _, _, replace_config, _ = _build_source_case(
        tmp_path / "source-readback-failure", width_m=10.0, height_m=10.0, columns=1,
        output_times_s=[0.0, 0.01], rainfall_intervals=[(0.0, 0.01, 0.0)],
        rainfall_unit="m/s", maximum_time_step_s=0.01,
    )
    raw = yaml.safe_load(replace_config.read_text(encoding="utf-8"))
    raw["overwrite_existing_result"] = True
    replace_config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    run_triangular_source(replace_config)
    result_path = replace_config.parent / "source-result.nc"
    previous = result_path.read_bytes()

    def fail_temporary_readback(path, *args, **kwargs):
        if Path(path).resolve() != result_path.resolve():
            raise TriangularResultNetCDFError("injected source read-back failure")
        return real_reader(path, *args, **kwargs)

    monkeypatch.setattr(source_io, "read_triangular_source_result", fail_temporary_readback)
    with pytest.raises(TriangularResultNetCDFError, match="read-back failure"):
        run_triangular_source(replace_config)
    assert result_path.read_bytes() == previous
    assert not list(result_path.parent.glob(".source-result.nc.*.tmp.nc"))

    _, _, race_config, _ = _build_source_case(
        tmp_path / "source-late-target", width_m=10.0, height_m=10.0, columns=1,
        output_times_s=[0.0, 0.01], rainfall_intervals=[(0.0, 0.01, 0.0)],
        rainfall_unit="m/s", maximum_time_step_s=0.01,
    )
    race_path = race_config.parent / "source-result.nc"
    late_bytes = b"late competing source result"

    def create_competing_target_after_readback(path, *args, **kwargs):
        decoded = real_reader(path, *args, **kwargs)
        race_path.write_bytes(late_bytes)
        return decoded

    monkeypatch.setattr(source_io, "read_triangular_source_result", create_competing_target_after_readback)
    with pytest.raises(TriangularResultNetCDFError, match="appeared before publication"):
        run_triangular_source(race_config)
    assert race_path.read_bytes() == late_bytes
    assert not list(race_path.parent.glob(".source-result.nc.*.tmp.nc"))


def test_source_run_mode_mismatch_and_base_output_protection(tmp_path):
    _, _, config, _ = _build_source_case(
        tmp_path / "mode-protection", width_m=10.0, height_m=10.0, columns=1,
        output_times_s=[0.0, 0.1], rainfall_intervals=[(0.0, 0.1, 0.0)],
        rainfall_unit="m/s", maximum_time_step_s=0.1,
    )
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["sources"] = "manning_and_rainfall"
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(TriangularSourceContractError, match="requires Manning enabled"):
        validate_triangular_source_run_request(config)

    raw["sources"] = "rainfall"
    for protected_name in (
        "base-none.nc", "base-run.yaml", "source-preparation.yaml",
        "prepared-sources.json", "business.json", "mesh.nc",
    ):
        raw["result_file"] = protected_name
        config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        with pytest.raises(TriangularSourceContractError, match="must not overwrite"):
            validate_triangular_source_run_request(config)
    assert not (config.parent / "source-result.nc").exists()


def test_stale_source_preparation_is_rejected_without_publishing(tmp_path):
    _, source_config, run_config, _ = _build_source_case(
        tmp_path / "stale-source-preparation", width_m=10.0, height_m=10.0, columns=1,
        output_times_s=[0.0, 0.1], rainfall_intervals=[(0.0, 0.1, 0.01)],
        rainfall_unit="m/s", maximum_time_step_s=0.1,
    )
    prep_path = source_config.parent / "prepared-sources.json"
    prep = json.loads(prep_path.read_text(encoding="utf-8"))
    prep["rainfall"]["intervals"][0]["rainfall_rate_m_s"] = 0.02
    prep_path.write_text(json.dumps(prep), encoding="utf-8")
    with pytest.raises(TriangularSourceContractError, match="source-preparation artifact"):
        run_triangular_source(run_config)
    assert not (run_config.parent / "source-result.nc").exists()
