from __future__ import annotations

import json
import math
from pathlib import Path
import shutil

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from hydrodynamics.mesh_preparation import prepare_mesh, write_ugrid_preparation
from hydrodynamics.triangular_business import (
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    prepare_triangular_business,
    read_triangular_business_package,
    write_triangular_business_package,
)
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh
from hydrodynamics.triangular_solver import run_triangular_solver
from hydrodynamics.triangular_solver_contract import TriangularSolverContractError, validate_triangular_run_request
from hydrodynamics.triangular_solver_netcdf import read_triangular_result
from hydrodynamics.triangular_sources import (
    ManningSourceRequest,
    TriangularSourceContractError,
    TriangularSourcePreparationConfig,
    load_triangular_source_preparation_config,
    main as source_preparation_main,
    prepare_triangular_sources,
    read_triangular_source_preparation,
)


def _build_inputs(
    root: Path,
    *,
    output_times: list[float] | None = None,
    initial_depth_m: float = 0.2,
    partial_roughness: bool = False,
) -> tuple[Path, Path, TriangularMesh]:
    root.mkdir(parents=True, exist_ok=True)
    columns = rows = 4
    width = height = 2.5
    node_xy = tuple(
        (500_000.0 + i * width, j * height)
        for j in range(rows + 1)
        for i in range(columns + 1)
    )
    faces = []
    for j in range(rows):
        for i in range(columns):
            ll = j * (columns + 1) + i
            lr = ll + 1
            ul = ll + columns + 1
            ur = ul + 1
            faces.extend(((ll, lr, ur), (ll, ur, ul)))
    mesh = TriangularMesh(
        node_xy, tuple(faces), "EPSG:32650", mesh_id=f"source-{root.name}",
        face_id=tuple(range(800_001, 800_001 + len(faces))),
    )
    terrain = FaceTerrainField(
        mesh.mesh_id, (0.0,) * mesh.n_face, (True,) * mesh.n_face,
        tuple(mesh.face_area), tuple(mesh.face_area), (1.0,) * mesh.n_face,
        mesh.coordinate_system,
    )
    mesh_path = root / "mesh.nc"
    write_ugrid_preparation(mesh_path, mesh, terrain)
    initial = TriangularInitialConditionField.from_constant(
        mesh, terrain, "depth", initial_depth_m, source="synthetic source-preparation initial depth",
    )
    if partial_roughness:
        roughness = TriangularRoughnessField.from_assignments(
            mesh, ((mesh.face_id[0], 0.035),), source="synthetic intentionally partial roughness",
        )
    else:
        roughness = TriangularRoughnessField.from_constant(
            mesh, 0.035, source="synthetic P3-A unit-1 Manning provenance",
        )
    business_path = root / "business.json"
    write_triangular_business_package(
        business_path,
        TriangularBusinessPackage(mesh, terrain, roughness, initial, ()),
        mesh_artifact=mesh_path,
    )
    run_config = {
        "format": "triangular-solver-run-v1",
        "schema_version": "1.0",
        "case_id": "synthetic-source-contract-case",
        "business_file": "business.json",
        "result_file": "result.nc",
        "time_origin": None,
        "output_times_s": output_times or [0.0, 1.0],
        "boundary_policy": "closed_wall",
        "initial_momentum": "zero",
        "sources": "none",
        "minimum_time_step_s": 1e-6,
        "maximum_time_step_s": 60.0,
    }
    run_path = root / "run.yaml"
    run_path.write_text(yaml.safe_dump(run_config, sort_keys=False), encoding="utf-8")
    return run_path, business_path, mesh


def _write_source_config(
    root: Path,
    *,
    manning: dict | None = None,
    rainfall: dict | None = None,
    source_cfl_fraction: float = 0.5,
) -> Path:
    data = {
        "format": "triangular-source-preparation-v1",
        "schema_version": "1.0",
        "case_id": "synthetic-source-contract-case",
        "run_config_file": "run.yaml",
        "output_file": "prepared_sources.json",
        "source_cfl_fraction": source_cfl_fraction,
    }
    if manning is not None:
        data["manning"] = manning
    if rainfall is not None:
        data["rainfall"] = rainfall
    path = root / "sources.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _rainfall_profile(unit: str = "mm/h") -> dict:
    return {
        "enabled": True,
        "unit": unit,
        "intervals": [
            {"start_time_s": 0.0, "end_time_s": 30.0, "intensity": 10.0},
            {"start_time_s": 30.0, "end_time_s": 90.0, "intensity": 0.0},
            {"start_time_s": 90.0, "end_time_s": 120.0, "intensity": 20.0},
        ],
    }


def _manning_request() -> dict:
    return {
        "enabled": True,
        "legacy_unit1_policy": "legacy_unit1_as_si_manning",
        "interpretation_rationale": "Synthetic source-contract case explicitly interprets the unchanged P3-A values as SI Manning n.",
    }


def test_source_preparation_defaults_to_disabled_and_keeps_p4_1_none_execution(tmp_path):
    run_path, business_path, mesh = _build_inputs(tmp_path, initial_depth_m=0.0)
    original_run = run_path.read_bytes()
    original_business = business_path.read_bytes()
    source_config = _write_source_config(tmp_path)
    result = prepare_triangular_sources(source_config)

    assert result.preparation_only is True
    assert result.simulation_result is False
    assert result.solver_completed is False
    assert result.sources_applied == "none"
    assert result.requested_sources == []
    assert result.status == "prepared_no_source_request"
    assert result.execution_available is True
    assert result.manning_applied is False and result.rainfall_applied is False
    assert result.manning.source_unit == "1"
    assert result.manning.face_id == list(mesh.face_id)
    assert result.manning.source_values == [0.035] * mesh.n_face
    assert result.manning.interpreted_values == [None] * mesh.n_face
    assert result.volume_correction_budget_m3 == validate_triangular_run_request(run_path).report["volume_correction_budget_m3"]
    assert run_path.read_bytes() == original_run
    assert business_path.read_bytes() == original_business
    assert read_triangular_source_preparation(tmp_path / "prepared_sources.json", source_config) == result

    run_triangular_solver(run_path)
    simulation = read_triangular_result(tmp_path / "result.nc", run_path)
    assert all(value == 0.0 for row in simulation.water_depth_m for value in row)
    assert simulation.sources_applied == "none"
    assert simulation.manning_enabled is False and simulation.rainfall_enabled is False


def test_source_cfl_fraction_is_dimensionless_and_has_open_unit_interval_range():
    base = {"case_id": "case", "run_config_file": "run.yaml", "output_file": "sources.json"}
    assert TriangularSourcePreparationConfig.model_validate(base).source_cfl_fraction == 0.5
    for value in (0.0, -0.1, 1.0001, True):
        with pytest.raises(ValidationError):
            TriangularSourcePreparationConfig.model_validate({**base, "source_cfl_fraction": value})


def test_bundled_source_preparation_example_runs_from_synthetic_p2_p3a(tmp_path, capsys):
    repo = Path(__file__).parents[1]
    solver_source = repo / "examples" / "triangular_solver"
    solver_dir = tmp_path / "examples" / "triangular_solver"
    mesh_dir = tmp_path / "examples" / "triangular_mesh"
    source_dir = tmp_path / "examples" / "triangular_sources"
    solver_dir.mkdir(parents=True)
    mesh_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    shutil.copy2(repo / "examples" / "triangular_mesh" / "mesh.json", mesh_dir / "mesh.json")
    for name in ("closed_wall_preparation.yaml", "full_terrain.json", "closed_wall_business.yaml", "closed_wall_run.yaml"):
        shutil.copy2(solver_source / name, solver_dir / name)
    shutil.copy2(repo / "examples" / "triangular_sources" / "source_preparation.yaml", source_dir / "source_preparation.yaml")
    prepare_mesh(solver_dir / "closed_wall_preparation.yaml")
    prepared_business = prepare_triangular_business(solver_dir / "closed_wall_business.yaml")
    assert prepared_business.artifact_path.exists()
    assert source_preparation_main([str(source_dir / "source_preparation.yaml")]) == 0
    source_preparation = read_triangular_source_preparation(
        source_dir / "source_prepared.json", source_dir / "source_preparation.yaml",
    )
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["status"] == "prepared_not_executable"
    assert source_preparation.preparation_only is True
    assert source_preparation.simulation_result is False
    assert source_preparation.execution_available is False
    assert source_preparation.requested_sources == ["manning", "rainfall"]
    assert source_preparation.rainfall.total_rain_depth_m == pytest.approx(1e-5, abs=1e-18)
    assert source_preparation.manning.source_unit == "1"
    assert source_preparation.manning.numeric_values_changed is False


def test_manning_requires_explicit_legacy_interpretation_and_preserves_numbers(tmp_path):
    run_path, business_path, mesh = _build_inputs(tmp_path)
    original_package = business_path.read_bytes()
    with pytest.raises(ValidationError, match="legacy_unit1_as_si_manning"):
        ManningSourceRequest.model_validate({"enabled": True})

    with pytest.raises(ValidationError):
        ManningSourceRequest.model_validate({
            "enabled": True,
            "legacy_unit1_policy": "legacy_unit1_as_si_manning",
            "interpretation_rationale": "",
        })

    source_config = _write_source_config(tmp_path, manning=_manning_request())
    prepared = prepare_triangular_sources(source_config)
    package = read_triangular_business_package(business_path)
    assert package.roughness.unit == "1"
    assert tuple(package.roughness.manning_n) == tuple(prepared.manning.source_values)
    assert prepared.manning.physical_unit == "s/m^(1/3)"
    assert prepared.manning.legacy_unit1_policy == "legacy_unit1_as_si_manning"
    assert prepared.manning.numeric_values_changed is False
    assert prepared.manning.interpreted_values == prepared.manning.source_values
    assert prepared.manning.applied is False
    assert prepared.execution_available is False
    assert prepared.sources_applied == "none"
    assert business_path.read_bytes() == original_package
    assert prepared.face_id == list(mesh.face_id)
    assert validate_triangular_run_request(run_path).config.sources == "none"


def test_manning_preparation_rejects_partial_roughness(tmp_path):
    _build_inputs(tmp_path, partial_roughness=True)
    source_config = _write_source_config(tmp_path, manning=_manning_request())
    with pytest.raises(TriangularSourceContractError, match="requires roughness on every face"):
        prepare_triangular_sources(source_config)
    assert not (tmp_path / "prepared_sources.json").exists()


def test_uniform_rainfall_conversion_and_stop_jump_reference_are_prepared_not_applied(tmp_path):
    run_path, _, mesh = _build_inputs(tmp_path, output_times=[0.0, 30.0, 90.0, 120.0], initial_depth_m=0.0)
    source_config = _write_source_config(tmp_path, manning=_manning_request(), rainfall=_rainfall_profile())
    base_capability = validate_triangular_run_request(run_path)
    prepared = prepare_triangular_sources(source_config)

    assert prepared.requested_sources == ["manning", "rainfall"]
    assert prepared.status == "prepared_not_executable"
    assert prepared.execution_available is False
    assert prepared.sources_applied == "none"
    assert prepared.manning_applied is False and prepared.rainfall_applied is False
    assert prepared.rainfall.input_unit == "mm/h"
    assert [item.start_output_index for item in prepared.rainfall.intervals] == [0, 1, 2]
    assert [item.end_output_index for item in prepared.rainfall.intervals] == [1, 2, 3]
    assert [item.rainfall_rate_m_s for item in prepared.rainfall.intervals] == pytest.approx(
        [10.0 / 3_600_000.0, 0.0, 20.0 / 3_600_000.0], rel=1e-15,
    )
    assert prepared.rainfall.total_rain_depth_m == pytest.approx(0.00025, abs=1e-15)
    assert prepared.rainfall.total_rain_volume_m3 == pytest.approx(0.025, abs=1e-12)
    assert prepared.volume_correction_budget_m3 == base_capability.report["volume_correction_budget_m3"]
    assert prepared.volume_correction_budget_m3 == 1e-10
    assert prepared.rainfall.total_rain_volume_m3 > prepared.volume_correction_budget_m3
    assert prepared.face_id == list(mesh.face_id)
    assert read_triangular_source_preparation(tmp_path / "prepared_sources.json", source_config) == prepared

    run_triangular_solver(run_path)
    simulation = read_triangular_result(tmp_path / "result.nc", run_path)
    assert all(value == 0.0 for row in simulation.water_depth_m for value in row)
    assert simulation.sources_applied == "none"
    assert simulation.rainfall_enabled is False and simulation.manning_enabled is False


def test_rainfall_m_per_second_is_preserved_without_unit_conversion(tmp_path):
    _build_inputs(tmp_path, output_times=[0.0, 1.0], initial_depth_m=0.2)
    request = {"enabled": True, "unit": "m/s", "intervals": [{"start_time_s": 0.0, "end_time_s": 1.0, "intensity": 1e-5}]}
    source_config = _write_source_config(tmp_path, rainfall=request)
    prepared = prepare_triangular_sources(source_config)
    interval = prepared.rainfall.intervals[0]
    assert interval.input_unit == "m/s" and interval.input_intensity == 1e-5
    assert interval.rainfall_rate_m_s == 1e-5
    assert interval.rainfall_depth_m == 1e-5
    assert prepared.rainfall.total_rain_volume_m3 == pytest.approx(0.001, abs=1e-12)


def test_zero_rainfall_still_marks_rain_requested_and_not_executable(tmp_path):
    _build_inputs(tmp_path, output_times=[0.0, 1.0])
    source_config = _write_source_config(tmp_path, rainfall={
        "enabled": True, "unit": "mm/h", "intervals": [{"start_time_s": 0.0, "end_time_s": 1.0, "intensity": 0.0}],
    })
    prepared = prepare_triangular_sources(source_config)
    assert prepared.requested_sources == ["rainfall"]
    assert prepared.rainfall.requested_enabled is True
    assert prepared.rainfall.total_rain_depth_m == 0.0
    assert prepared.rainfall.applied is False
    assert prepared.execution_available is False
    assert prepared.sources_applied == "none"


def test_source_preparation_output_protects_linked_inputs_and_existing_files(tmp_path):
    _build_inputs(tmp_path)
    source_config = _write_source_config(tmp_path)
    for protected_name in ("sources.yaml", "run.yaml", "business.json", "mesh.nc", "result.nc"):
        data = yaml.safe_load(source_config.read_text(encoding="utf-8"))
        data["output_file"] = protected_name
        source_config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        with pytest.raises(TriangularSourceContractError, match="must not overwrite"):
            prepare_triangular_sources(source_config)
    data = yaml.safe_load(source_config.read_text(encoding="utf-8"))
    data["output_file"] = "prepared_sources.json"
    source_config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    (tmp_path / "prepared_sources.json").write_bytes(b"existing preparation artifact")
    with pytest.raises(TriangularSourceContractError, match="already exists"):
        prepare_triangular_sources(source_config)


@pytest.mark.parametrize(
    "intervals,message",
    [
        ([{"start_time_s": 0.0, "end_time_s": 30.0, "intensity": 1.0},
          {"start_time_s": 90.0, "end_time_s": 120.0, "intensity": 1.0}], "gap"),
        ([{"start_time_s": 0.0, "end_time_s": 90.0, "intensity": 1.0},
          {"start_time_s": 30.0, "end_time_s": 120.0, "intensity": 1.0}], "overlap"),
        ([{"start_time_s": 0.0, "end_time_s": 30.0, "intensity": 1.0},
          {"start_time_s": 30.0, "end_time_s": 60.0, "intensity": 1.0},
          {"start_time_s": 60.0, "end_time_s": 120.0, "intensity": 1.0}], "must match exactly one requested output time"),
    ],
)
def test_rainfall_gaps_overlaps_and_unmatched_breaks_are_rejected(tmp_path, intervals, message):
    _build_inputs(tmp_path, output_times=[0.0, 30.0, 90.0, 120.0])
    source_config = _write_source_config(tmp_path, rainfall={"enabled": True, "unit": "mm/h", "intervals": intervals})
    with pytest.raises(TriangularSourceContractError, match=message):
        prepare_triangular_sources(source_config)
    assert not (tmp_path / "prepared_sources.json").exists()


def test_rainfall_request_rejects_negative_string_boolean_and_contradictory_payload(tmp_path):
    _build_inputs(tmp_path, output_times=[0.0, 1.0])
    bad_rainfall = [
        {"enabled": True, "unit": "m/s", "intervals": [{"start_time_s": 0.0, "end_time_s": 1.0, "intensity": -1e-6}]},
        {"enabled": True, "unit": "m/s", "intervals": [{"start_time_s": 0.0, "end_time_s": 1.0, "intensity": "1e-6"}]},
        {"enabled": True, "unit": "m/s", "intervals": [{"start_time_s": 0.0, "end_time_s": 1.0, "intensity": True}]},
        {"enabled": True, "unit": "mm/s", "intervals": [{"start_time_s": 0.0, "end_time_s": 1.0, "intensity": 1.0}]},
        {"enabled": False, "unit": "mm/h", "intervals": [{"start_time_s": 0.0, "end_time_s": 1.0, "intensity": 1.0}]},
        {"enabled": True, "unit": "m/s", "intervals": [{"start_time_s": 0.0, "end_time_s": 1.0, "intensity": float("nan")}]},
        {"enabled": True, "unit": "m/s", "intervals": [{"start_time_s": 0.0, "end_time_s": 1.0, "intensity": float("inf")}]},
    ]
    for rainfall in bad_rainfall:
        data = {
            "case_id": "synthetic-source-contract-case", "run_config_file": "run.yaml", "output_file": "prepared.json",
            "rainfall": rainfall,
        }
        with pytest.raises(ValidationError):
            TriangularSourcePreparationConfig.model_validate(data)


def test_rainfall_derived_overflow_is_rejected(tmp_path):
    _build_inputs(tmp_path, output_times=[0.0, 120.0])
    source_config = _write_source_config(tmp_path, rainfall={
        "enabled": True, "unit": "m/s", "intervals": [{"start_time_s": 0.0, "end_time_s": 120.0, "intensity": 1e308}],
    })
    with pytest.raises(TriangularSourceContractError, match="non-finite converted rate, depth or volume"):
        prepare_triangular_sources(source_config)

    total_root = tmp_path / "total-overflow"
    _build_inputs(total_root, output_times=[0.0, 1.0, 2.0])
    total_config = _write_source_config(total_root, rainfall={
        "enabled": True,
        "unit": "m/s",
        "intervals": [
            {"start_time_s": 0.0, "end_time_s": 1.0, "intensity": 1e306},
            {"start_time_s": 1.0, "end_time_s": 2.0, "intensity": 1e306},
        ],
    })
    with pytest.raises(TriangularSourceContractError, match="interval totals overflow"):
        prepare_triangular_sources(total_config)


def _independent_manning_rk2(q0: float, h: float, n: float, gravity: float, duration: float, dt: float) -> float:
    q = q0
    steps = round(duration / dt)
    for _ in range(steps):
        coefficient = gravity * n * n / h ** (7.0 / 3.0)
        stage1 = q - dt * coefficient * abs(q) * q
        stage2_euler = stage1 - dt * coefficient * abs(stage1) * stage1
        q = 0.5 * (q + stage2_euler)
    return q


def _independent_combined_rk2(q0: float, h0: float, rainfall: float, n: float, gravity: float, duration: float, dt: float) -> tuple[float, float]:
    q, h = q0, h0
    for _ in range(round(duration / dt)):
        gamma0 = gravity * n * n * abs(q) / h ** (7.0 / 3.0)
        q1 = q - dt * gamma0 * q
        h1 = h + dt * rainfall
        gamma1 = gravity * n * n * abs(q1) / h1 ** (7.0 / 3.0)
        q2_euler = q1 - dt * gamma1 * q1
        q = 0.5 * (q + q2_euler)
        h = 0.5 * (h + (h1 + dt * rainfall))
    return h, q


def test_source_reference_manifest_matches_independent_odes_and_integrals():
    path = Path(__file__).parents[1] / "examples" / "triangular_sources" / "source_benchmark_references.json"
    references = json.loads(path.read_text(encoding="utf-8"))
    assert references["status"] == "frozen_before_P4-2B_source_implementation"
    g = references["source_configuration"]["reference_gravity_m_s2"]
    manning = references["cases"][0]
    h, n, duration = manning["h_m"], manning["manning_n"], manning["duration_s"]
    q0 = np.asarray(manning["initial_q_m2_s"], dtype=float)
    exact_scale = 1.0 / (1.0 + g * n * n * np.linalg.norm(q0) * duration / h ** (7.0 / 3.0))
    exact_vector = q0 * exact_scale
    assert exact_vector == pytest.approx(manning["exact_q_m2_s_at_duration"], abs=1e-15)
    errors = []
    for dt, tolerance in zip(manning["comparison_time_steps_s"], manning["candidate_max_component_error_tolerances_m2_s"]):
        q_num = _independent_manning_rk2(float(np.linalg.norm(q0)), h, n, g, duration, dt)
        error = float(np.max(np.abs(q0 * (q_num / np.linalg.norm(q0)) - exact_vector)))
        errors.append(error)
        assert error <= tolerance
        assert q_num >= 0.0 and q_num <= np.linalg.norm(q0)
    assert errors[2] < errors[1] < errors[0]

    for probe in references["cases"][1]["probes"]:
        q = np.asarray(probe["q_m2_s"], dtype=float)
        gamma = g * probe["manning_n"] ** 2 * float(np.linalg.norm(q)) / probe["h_m"] ** (7.0 / 3.0)
        source = -gamma * q
        assert gamma == pytest.approx(probe["gamma_per_s"], rel=1e-14)
        assert source == pytest.approx(probe["momentum_source_m2_s2"], rel=1e-14)

    rain = references["cases"][2]
    rain_rate = rain["input_intensity_mm_h"] / 3_600_000.0
    rain_depth = rain_rate * rain["duration_s"]
    assert rain_rate == pytest.approx(rain["converted_intensity_m_s"], abs=1e-20)
    assert rain_depth == pytest.approx(rain["reference_depth_increments_m"][0], abs=1e-18)
    assert rain_depth * rain["domain_area_m2"] == pytest.approx(rain["reference_volume_increment_m3"], abs=1e-15)

    jump = references["cases"][3]
    integrated_depth = math.fsum(
        interval["intensity_mm_h"] / 3_600_000.0 * (interval["end_time_s"] - interval["start_time_s"])
        for interval in jump["intervals_left_closed_right_open_mm_h"]
    )
    assert integrated_depth == pytest.approx(jump["total_depth_m"], abs=1e-18)
    assert integrated_depth * jump["domain_area_m2"] == pytest.approx(jump["total_volume_m3"], abs=1e-15)

    combined = references["cases"][4]
    h_final = combined["h0_m"] + combined["rainfall_rate_m_s"] * combined["duration_s"]
    j = 3.0 * g * combined["manning_n"] ** 2 / (4.0 * combined["rainfall_rate_m_s"]) * (
        combined["h0_m"] ** (-4.0 / 3.0) - h_final ** (-4.0 / 3.0)
    )
    q0_magnitude = math.hypot(*combined["initial_q_m2_s"])
    exact_scale = 1.0 / (1.0 + q0_magnitude * j)
    exact_q = [component * exact_scale for component in combined["initial_q_m2_s"]]
    assert h_final == pytest.approx(combined["exact_final_depth_m"], abs=1e-15)
    assert j == pytest.approx(combined["integral_J"], rel=1e-14)
    assert exact_q == pytest.approx(combined["exact_q_m2_s"], abs=1e-15)
    combined_errors = []
    for dt, tolerance in zip(combined["comparison_time_steps_s"], combined["candidate_max_component_error_tolerances_m2_s"]):
        h_num, q_scale_num = _independent_combined_rk2(
            q0_magnitude, combined["h0_m"], combined["rainfall_rate_m_s"],
            combined["manning_n"], g, combined["duration_s"], dt,
        )
        q_num = [component * q_scale_num / q0_magnitude for component in combined["initial_q_m2_s"]]
        error = max(abs(a - b) for a, b in zip(q_num, exact_q))
        combined_errors.append(error)
        assert error <= tolerance
        assert abs(h_num - h_final) <= combined["candidate_depth_error_tolerance_m"]
    assert combined_errors[2] < combined_errors[1] < combined_errors[0]


def test_source_preparation_artifact_rechecks_face_ids_and_source_config_digest(tmp_path):
    _build_inputs(tmp_path)
    source_config = _write_source_config(tmp_path, manning=_manning_request())
    prepared_path = tmp_path / "prepared_sources.json"
    prepare_triangular_sources(source_config)
    raw = json.loads(prepared_path.read_text(encoding="utf-8"))
    raw["face_id"][0] += 1
    forged = tmp_path / "forged_sources.json"
    forged.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(TriangularSourceContractError):
        read_triangular_source_preparation(forged, source_config, enforce_configured_output_path=False)

    config_data = yaml.safe_load(source_config.read_text(encoding="utf-8"))
    config_data["source_cfl_fraction"] = 0.75
    source_config.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    with pytest.raises(TriangularSourceContractError, match="does not match re-read config"):
        read_triangular_source_preparation(prepared_path, source_config)


@pytest.mark.parametrize("source_mode", ["manning", "rainfall", "manning+rainfall"])
def test_p4_1_runner_explicitly_rejects_unimplemented_source_modes(tmp_path, source_mode):
    run_path, _, _ = _build_inputs(tmp_path)
    data = yaml.safe_load(run_path.read_text(encoding="utf-8"))
    data["sources"] = source_mode
    run_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(TriangularSolverContractError, match="sources"):
        run_triangular_solver(run_path)
    assert not (tmp_path / "result.nc").exists()
