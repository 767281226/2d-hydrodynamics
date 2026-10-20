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
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh
from hydrodynamics.triangular_manning import (
    TriangularManningExecutionError,
    TriangularManningRunConfig,
    load_triangular_manning_run_config,
    main as manning_main,
    run_triangular_manning,
    validate_triangular_manning_run_request,
)
from hydrodynamics.triangular_manning_netcdf import read_triangular_manning_result
from hydrodynamics.triangular_solver import (
    TriangularSolverExecutionError,
    _attempt_step,
    _edges,
    _manning_source,
    run_triangular_solver,
)
from hydrodynamics.triangular_solver_contract import (
    TriangularSolverContractError,
    _source_cfl_bound_exceeded,
    _source_cfl_bound_tolerance_s,
)
from hydrodynamics.triangular_solver_netcdf import TriangularResultNetCDFError, read_triangular_result
from hydrodynamics.triangular_sources import (
    TriangularSourcePreparationConfig,
    load_triangular_source_preparation_config,
    prepare_triangular_sources,
    read_triangular_source_preparation,
)


def _build_stoker_manning_case(
    root: Path,
    *,
    columns: int = 8,
    duration_s: float = 0.1,
    manning_n: float = 0.1,
    source_cfl_fraction: float = 0.5,
    minimum_time_step_s: float = 1e-6,
    maximum_time_step_s: float = 0.01,
    times: list[float] | None = None,
    initial_profile: str = "stoker",
    manning_values: list[float] | None = None,
    permutation: list[int] | None = None,
    rotate: bool = False,
    unequal_area: bool = False,
) -> tuple[Path, Path, Path, TriangularMesh]:
    root.mkdir(parents=True, exist_ok=True)
    dx = 2.0 / columns
    rows = 1
    node_xy = tuple(
        (
            (500_000.0 - (float(j) + (0.1 * i if unequal_area and j == 1 else 0.0)), float(i * dx))
            if rotate else
            (500_000.0 + i * dx, float(j) + (0.1 * i if unequal_area and j == 1 else 0.0))
        )
        for j in range(rows + 1) for i in range(columns + 1)
    )
    faces: list[tuple[int, int, int]] = []
    initial_depths: list[float] = []
    for i in range(columns):
        ll = i
        lr = i + 1
        ul = columns + 1 + i
        ur = ul + 1
        faces.extend(((ll, lr, ur), (ll, ur, ul)))
        x_first = (i + 2.0 / 3.0) * dx
        x_second = (i + 1.0 / 3.0) * dx
        initial_depths.extend((2.0 if x_first < 1.0 else 1.0, 2.0 if x_second < 1.0 else 1.0))
    if initial_profile == "still_water":
        initial_depths = [1.0] * len(faces)
    elif initial_profile == "fully_dry":
        initial_depths = [0.0] * len(faces)
    elif initial_profile != "stoker":
        raise ValueError(f"unsupported synthetic initial profile: {initial_profile}")
    original_face_count = len(faces)
    if permutation is None:
        permutation = list(range(original_face_count))
    if sorted(permutation) != list(range(original_face_count)):
        raise ValueError("permutation must contain every original face index exactly once")
    faces = [faces[index] for index in permutation]
    initial_depths = [initial_depths[index] for index in permutation]
    mesh = TriangularMesh(
        node_xy, tuple(faces), "EPSG:32650", mesh_id=f"manning-stoker-{root.name}",
        face_id=tuple(4_900_001 + index for index in permutation),
    )
    terrain = FaceTerrainField(
        mesh.mesh_id, (0.0,) * mesh.n_face, (True,) * mesh.n_face,
        tuple(mesh.face_area), tuple(mesh.face_area), (1.0,) * mesh.n_face,
        mesh.coordinate_system,
    )
    mesh_path = root / "mesh.nc"
    write_ugrid_preparation(mesh_path, mesh, terrain)
    initial = TriangularInitialConditionField.from_assignments(
        mesh, terrain, "depth", tuple(zip(mesh.face_id, initial_depths)), source="synthetic zero-momentum wet-bed Stoker profile",
    )
    if manning_values is None:
        roughness = TriangularRoughnessField.from_constant(mesh, manning_n, source="synthetic P3-A Manning n unit 1")
    else:
        if len(manning_values) != original_face_count:
            raise ValueError("manning_values must be supplied in original face order")
        roughness = TriangularRoughnessField.from_assignments(
            mesh, tuple((face_id, manning_values[index]) for face_id, index in zip(mesh.face_id, permutation)),
            source="synthetic face-ID-aligned Manning n unit 1",
        )
    business_path = root / "business.json"
    write_triangular_business_package(
        business_path, TriangularBusinessPackage(mesh, terrain, roughness, initial, ()), mesh_artifact=mesh_path,
    )
    case_id = "synthetic-stoker-manning"
    base_config_path = root / "base-run.yaml"
    base_config_path.write_text(yaml.safe_dump({
        "format": "triangular-solver-run-v1", "schema_version": "1.0", "case_id": case_id,
        "business_file": "business.json", "result_file": "base-none.nc", "time_origin": None,
        "output_times_s": times or [0.0, duration_s], "boundary_policy": "closed_wall",
        "initial_momentum": "zero", "sources": "none", "gravity_m_s2": 9.81,
        "cfl_number": 0.25, "minimum_time_step_s": minimum_time_step_s,
        "maximum_time_step_s": maximum_time_step_s, "maximum_step_retries": 8,
    }, sort_keys=False), encoding="utf-8")
    source_config_path = root / "source-preparation.yaml"
    source_config_path.write_text(yaml.safe_dump({
        "format": "triangular-source-preparation-v1", "schema_version": "1.0", "case_id": case_id,
        "run_config_file": "base-run.yaml", "output_file": "prepared-sources.json",
        "source_cfl_fraction": source_cfl_fraction,
        "manning": {
            "enabled": True,
            "legacy_unit1_policy": "legacy_unit1_as_si_manning",
            "interpretation_rationale": "Synthetic Manning execution test explicitly assigns SI meaning to unchanged P3-A unit-1 values.",
        },
        "rainfall": {"enabled": False},
    }, sort_keys=False), encoding="utf-8")
    prepare_triangular_sources(source_config_path)
    manning_config_path = root / "manning-run.yaml"
    manning_config_path.write_text(yaml.safe_dump({
        "format": "triangular-manning-run-v1", "schema_version": "1.0", "case_id": case_id,
        "sources": "manning", "base_run_config_file": "base-run.yaml",
        "source_config_file": "source-preparation.yaml", "result_file": "manning.nc",
        "overwrite_existing_result": False,
    }, sort_keys=False), encoding="utf-8")
    return base_config_path, source_config_path, manning_config_path, mesh


def _q_magnitude(result, mesh: TriangularMesh) -> float:
    hx = result.momentum_x_m2_s[-1]
    hy = result.momentum_y_m2_s[-1]
    return math.fsum(area * math.hypot(x, y) for area, x, y in zip(mesh.face_area, hx, hy))


def test_versioned_manning_run_applies_drag_and_round_trips_new_native_format(tmp_path):
    base_config, source_config, manning_config, mesh = _build_stoker_manning_case(tmp_path / "wet-stoker")
    source_prep_path = source_config.parent / "prepared-sources.json"
    base_report = run_triangular_solver(base_config)
    base_bytes = (base_config.parent / "base-none.nc").read_bytes()
    base_result = read_triangular_result(base_config.parent / "base-none.nc", base_config)

    capability = validate_triangular_manning_run_request(manning_config)
    assert capability.report["base_sources"] == "none"
    assert capability.report["requested_sources"] == ["manning"]
    report = run_triangular_manning(manning_config)
    result_path = manning_config.parent / "manning.nc"
    result = read_triangular_manning_result(result_path, manning_config)

    assert report["status"] == "completed"
    assert result.format == "triangular-manning-result-v1"
    assert result.product_type == "experimental_triangular_manning_shallow_water_result"
    assert result.simulation_result is True and result.experimental is True
    assert result.production_approved is False
    assert result.manning_enabled is True and result.manning_applied is True
    assert result.rainfall_enabled is False and result.rainfall_applied is False
    assert result.sources_applied == "manning"
    assert result.manning_source_unit == "1"
    assert result.manning_physical_unit == "s/m^(1/3)"
    assert result.manning_face_id == list(mesh.face_id)
    assert result.manning_n == [0.1] * mesh.n_face
    assert result.nonzero_manning_effect_applied is True
    assert result.manning_nonzero_face_stage_evaluation_count > 0
    assert result.maximum_manning_gamma_per_s > 0.0
    assert result.maximum_manning_source_magnitude_m2_s2 > 0.0
    assert result.source_cfl_limit_evaluation_count > 0
    assert result.minimum_source_cfl_limit_s is not None
    assert result.maximum_source_cfl_limit_s >= result.minimum_source_cfl_limit_s
    assert _q_magnitude(result, mesh) < _q_magnitude(base_result, mesh)
    assert abs(result.mass_budget[-1].residual_m3) <= 1e-10 * result.volume_correction_reference_scale_m3
    assert result.mass_budget[-1].source_net_volume_m3 == 0.0
    assert result.mass_correction_count == 0
    assert (base_config.parent / "base-none.nc").read_bytes() == base_bytes
    prepared = read_triangular_source_preparation(source_prep_path, source_config)
    assert prepared.manning.applied is False
    assert prepared.rainfall.applied is False
    assert base_report["status"] == "completed"


@pytest.mark.parametrize("profile,expected_depth", [("still_water", 1.0), ("fully_dry", 0.0)])
def test_manning_enabled_still_water_and_fully_dry_states_remain_static(tmp_path, profile, expected_depth):
    _, _, config, mesh = _build_stoker_manning_case(
        tmp_path / profile, duration_s=0.01, initial_profile=profile,
    )
    run_triangular_manning(config)
    result = read_triangular_manning_result(config.parent / "manning.nc", config)
    assert all(value == pytest.approx(expected_depth, abs=1e-12) for row in result.water_depth_m for value in row)
    assert max(result.maximum_speed_m_s_by_time) == 0.0
    assert result.manning_applied is True
    assert result.nonzero_manning_effect_applied is False
    assert result.manning_nonzero_face_stage_evaluation_count == 0
    assert result.mass_correction_count == 0
    assert all(abs(row.residual_m3) <= 1e-10 * result.volume_correction_reference_scale_m3 for row in result.mass_budget)
    assert mesh.n_face == len(result.manning_n) == len(result.manning_face_id)


def test_face_id_aligned_manning_survives_unequal_area_rotation_and_face_reordering(tmp_path):
    face_count = 2 * 8
    n_by_original_face = [0.025 + index * 0.002 for index in range(face_count)]
    _, _, normal_config, normal_mesh = _build_stoker_manning_case(
        tmp_path / "unequal-normal", duration_s=0.01, manning_values=n_by_original_face,
        unequal_area=True,
    )
    permutation = list(reversed(range(face_count)))
    _, _, rotated_config, _ = _build_stoker_manning_case(
        tmp_path / "unequal-rotated-reordered", duration_s=0.01,
        manning_values=n_by_original_face, permutation=permutation, rotate=True, unequal_area=True,
    )
    run_triangular_manning(normal_config)
    run_triangular_manning(rotated_config)
    normal = read_triangular_manning_result(normal_config.parent / "manning.nc", normal_config)
    rotated = read_triangular_manning_result(rotated_config.parent / "manning.nc", rotated_config)

    def keyed(result):
        final = {face_id: (h, math.hypot(hx, hy)) for face_id, h, hx, hy in zip(
            result.face_id, result.water_depth_m[-1], result.momentum_x_m2_s[-1], result.momentum_y_m2_s[-1],
        )}
        n_values = dict(zip(result.manning_face_id, result.manning_n))
        return final, n_values

    normal_state, normal_n = keyed(normal)
    rotated_state, rotated_n = keyed(rotated)
    assert len({round(area, 12) for area in normal_mesh.face_area}) > 1
    assert rotated_n == normal_n == {
        4_900_001 + index: value for index, value in enumerate(n_by_original_face)
    }
    for face_id in normal_state:
        assert rotated_state[face_id][0] == pytest.approx(normal_state[face_id][0], rel=1e-10, abs=1e-12)
        assert rotated_state[face_id][1] == pytest.approx(normal_state[face_id][1], rel=1e-10, abs=1e-12)
    for result in (normal, rotated):
        assert result.nonzero_manning_effect_applied is True
        assert abs(result.mass_budget[-1].residual_m3) <= 1e-10 * result.volume_correction_reference_scale_m3


def test_production_attempt_step_matches_frozen_local_manning_decay(tmp_path, monkeypatch):
    _, _, _, mesh = _build_stoker_manning_case(tmp_path / "local-source", duration_s=0.1)
    from hydrodynamics import triangular_solver as solver
    monkeypatch.setattr(
        solver, "_rhs_and_cfl",
        lambda state, *args, **kwargs: (np.zeros_like(state), math.inf, 0.0),
    )
    references_path = Path(__file__).parents[1] / "examples" / "triangular_sources" / "source_benchmark_references.json"
    references = json.loads(references_path.read_text(encoding="utf-8"))
    reference = references["cases"][0]
    state = np.zeros((mesh.n_face, 3), dtype=np.float64)
    state[:, 0] = reference["h_m"]
    state[:, 1] = reference["initial_q_m2_s"][0]
    state[:, 2] = reference["initial_q_m2_s"][1]
    n_values = [reference["manning_n"]] * mesh.n_face
    edges = _edges(mesh)
    errors = []
    for dt, tolerance in zip(reference["comparison_time_steps_s"], reference["candidate_max_component_error_tolerances_m2_s"]):
        current = state.copy()
        initial_norm = math.hypot(current[0, 1], current[0, 2])
        for _ in range(round(reference["duration_s"] / dt)):
            attempt = _attempt_step(
                current, dt, mesh, (0.0,) * mesh.n_face, edges,
                gravity=reference["gravity_m_s2"], cfl=0.25, dry_depth_m=1e-6,
                negative_tolerance_m=1e-12, correction_budget_remaining_m3=1.0,
                manning_n_m=n_values, source_cfl_fraction=0.5,
            )
            assert attempt[4] is None
            current = attempt[0]
            assert attempt[1] == 0.0
        qx, qy = float(current[0, 1]), float(current[0, 2])
        exact = reference["exact_q_m2_s_at_duration"]
        error = max(abs(qx - exact[0]), abs(qy - exact[1]))
        errors.append(error)
        assert error <= tolerance
        assert qx * reference["initial_q_m2_s"][0] + qy * reference["initial_q_m2_s"][1] > 0.0
        assert math.hypot(qx, qy) <= initial_norm
        assert max(abs(float(h) - reference["h_m"]) for h in current[:, 0]) <= 1e-12
    assert errors[2] < errors[1] < errors[0]


def test_manning_vector_norm_and_depth_exponent_match_frozen_probes():
    path = Path(__file__).parents[1] / "examples" / "triangular_sources" / "source_benchmark_references.json"
    references = json.loads(path.read_text(encoding="utf-8"))
    probes = references["cases"][1]["probes"]
    for probe in probes:
        qx, qy = probe["q_m2_s"]
        state = np.asarray([[probe["h_m"], qx, qy]], dtype=np.float64)
        source, bound, diagnostics, reason = _manning_source(
            state, [probe["manning_n"]], gravity=9.81,
            source_cfl_fraction=0.5, dry_depth_m=1e-6,
        )
        assert reason is None and math.isfinite(bound) and bound > 0.0
        assert diagnostics["maximum_manning_gamma_per_s"] == pytest.approx(probe["gamma_per_s"], rel=1e-14)
        assert source[0, 1:].tolist() == pytest.approx(probe["momentum_source_m2_s2"], rel=1e-14)


def test_manning_source_cfl_controls_real_stoker_steps(tmp_path):
    _, _, manning_config, mesh = _build_stoker_manning_case(
        tmp_path / "source-cfl", duration_s=0.01, manning_n=1.0,
        source_cfl_fraction=1e-4, maximum_time_step_s=0.01,
    )
    report = run_triangular_manning(manning_config)
    result = read_triangular_manning_result(manning_config.parent / "manning.nc", manning_config)
    assert result.source_cfl_fraction == 1e-4
    assert result.manning_source_cfl_limited_step_count > 0
    assert result.manning_source_cfl_rejected_step_count > 0
    assert result.rejected_step_reasons["second_stage_source_cfl"] == result.manning_source_cfl_rejected_step_count
    assert result.accepted_step_count > 0
    assert result.nonzero_manning_effect_applied is True
    assert abs(result.mass_budget[-1].residual_m3) <= 1e-10 * result.volume_correction_reference_scale_m3
    assert report["result_file"] == str(manning_config.parent / "manning.nc")
    assert mesh.n_face == 2 * 8


def test_manning_reader_rejects_metadata_state_injection_and_base_conflicts(tmp_path):
    import netCDF4

    for mutation in ("state_arrays", "base_case_id"):
        case_root = tmp_path / mutation
        _, _, config, _ = _build_stoker_manning_case(case_root, columns=2, duration_s=0.01)
        run_triangular_manning(config)
        path = case_root / "manning.nc"
        original = read_triangular_manning_result(path, config)
        with netCDF4.Dataset(path, "r+") as dataset:
            metadata = json.loads(dataset.manning_metadata_json)
            if mutation == "state_arrays":
                for name in ("water_depth_m", "momentum_x_m2_s", "momentum_y_m2_s"):
                    rows = getattr(original, name)
                    metadata[name] = [rows[0], *[list(reversed(row)) for row in rows[1:]]]
                expected_error = "metadata keys do not match"
            else:
                metadata["case_id"] = "injected-case-id"
                expected_error = "metadata case_id conflicts"
            dataset.manning_metadata_json = json.dumps(metadata)
        with netCDF4.Dataset(path) as dataset:
            saved_depth = dataset.variables["water_depth"][:].tolist()
        assert saved_depth == original.water_depth_m
        with pytest.raises(TriangularResultNetCDFError, match=expected_error):
            read_triangular_manning_result(path, config)


def test_unrepresentable_source_bound_fails_but_small_positive_and_zero_drag_are_distinct():
    config = TriangularSourcePreparationConfig.model_validate({
        "case_id": "tiny-fraction", "run_config_file": "base.yaml", "output_file": "prep.json",
        "source_cfl_fraction": 1e-320,
    })
    underflow_state = np.asarray([[1e-4, 0.6, 0.8]], dtype=np.float64)
    _, bound, diagnostics, error = _manning_source(
        underflow_state, [0.1], gravity=9.81,
        source_cfl_fraction=config.source_cfl_fraction, dry_depth_m=1e-6,
    )
    assert diagnostics["maximum_manning_gamma_per_s"] > 0.0
    assert bound == 0.0
    assert error == "source_cfl_limit_underflow"

    _, representable_bound, _, representable_error = _manning_source(
        underflow_state, [0.1], gravity=9.81,
        source_cfl_fraction=1e-300, dry_depth_m=1e-6,
    )
    assert representable_error is None
    assert math.isfinite(representable_bound) and 0.0 < representable_bound < 1e-6

    overflow_state = np.asarray([[1e5, 1e-300, 0.0]], dtype=np.float64)
    _, overflow_bound, overflow_diagnostics, overflow_error = _manning_source(
        overflow_state, [0.1], gravity=9.81, source_cfl_fraction=0.5, dry_depth_m=1e-6,
    )
    assert overflow_diagnostics["maximum_manning_gamma_per_s"] > 0.0
    assert overflow_bound == 0.0 and overflow_error == "source_cfl_limit_overflow"

    zero_momentum = np.asarray([[1e-4, 0.0, 0.0]], dtype=np.float64)
    zero_source, zero_bound, zero_diagnostics, zero_error = _manning_source(
        zero_momentum, [0.1], gravity=9.81,
        source_cfl_fraction=1e-320, dry_depth_m=1e-6,
    )
    assert zero_error is None and math.isinf(zero_bound)
    assert zero_diagnostics["maximum_manning_gamma_per_s"] == 0.0
    assert np.array_equal(zero_source, np.zeros_like(zero_source))

    normal_state = np.asarray([[1.0, 0.6, 0.8]], dtype=np.float64)
    _, normal_bound, _, normal_error = _manning_source(
        normal_state, [0.1], gravity=9.81, source_cfl_fraction=0.5, dry_depth_m=1e-6,
    )
    assert normal_error is None and math.isfinite(normal_bound) and normal_bound > 0.0


def test_attempt_step_rejects_underflow_at_both_rk_stages_without_mutating_state(tmp_path, monkeypatch):
    _, _, _, mesh = _build_stoker_manning_case(tmp_path / "underflow-rk", columns=2, duration_s=0.01)
    from hydrodynamics import triangular_solver as solver
    bed = (0.0,) * mesh.n_face
    edges = _edges(mesh)
    common = {
        "gravity": 9.81, "cfl": 0.25, "dry_depth_m": 1e-6,
        "negative_tolerance_m": 1e-12, "correction_budget_remaining_m3": 0.0,
        "manning_n_m": [0.1] * mesh.n_face, "source_cfl_fraction": 1e-320,
    }
    initial = np.tile([1e-4, 0.6, 0.8], (mesh.n_face, 1)).astype(np.float64)
    original = initial.copy()
    monkeypatch.setattr(
        solver, "_rhs_and_cfl",
        lambda state, *args, **kwargs: (np.zeros_like(state), math.inf, 0.0),
    )
    stage1 = _attempt_step(initial, 1e-3, mesh, bed, edges, **common)
    assert stage1[0] is None and stage1[4] == "source_cfl_limit_underflow"
    assert stage1[7] == "stage1" and stage1[1] == 0.0 and stage1[2] == 0
    assert np.array_equal(initial, original)

    zero_initial = np.tile([1e-4, 0.0, 0.0], (mesh.n_face, 1)).astype(np.float64)
    zero_original = zero_initial.copy()

    def create_stage1_momentum(state, *args, **kwargs):
        derivative = np.zeros_like(state)
        derivative[:, 1] = 0.03
        return derivative, math.inf, 0.0

    monkeypatch.setattr(solver, "_rhs_and_cfl", create_stage1_momentum)
    stage2 = _attempt_step(zero_initial, 1e-3, mesh, bed, edges, **common)
    assert stage2[0] is None and stage2[4] == "source_cfl_limit_underflow"
    assert stage2[7] == "stage2" and stage2[1] == 0.0 and stage2[2] == 0
    assert np.array_equal(zero_initial, zero_original)


def test_output_alignment_step_does_not_ignore_an_unrepresentable_source_bound(tmp_path, monkeypatch):
    _, _, config, _ = _build_stoker_manning_case(
        tmp_path / "underflow-tail", duration_s=1.5e-6, minimum_time_step_s=1e-6,
        maximum_time_step_s=1e-6, times=[0.0, 1.5e-6],
    )
    from hydrodynamics import triangular_solver as solver
    real_source = solver._manning_source
    calls = {"count": 0}

    def reject_alignment_limit(state, *args, **kwargs):
        calls["count"] += 1
        source, bound, diagnostics, error = real_source(state, *args, **kwargs)
        if calls["count"] == 5:
            return source, 0.0, diagnostics, "source_cfl_limit_underflow"
        return source, bound, diagnostics, error

    monkeypatch.setattr(solver, "_manning_source", reject_alignment_limit)
    with pytest.raises(TriangularSolverExecutionError) as caught:
        run_triangular_manning(config)
    assert caught.value.reason == "source_cfl_limit_underflow"
    assert caught.value.stage == "step_selection"
    assert caught.value.accepted_step_count == 1
    assert not (config.parent / "manning.nc").exists()


def test_strong_manning_limit_below_minimum_step_fails_without_publishing(tmp_path):
    _, _, manning_config, _ = _build_stoker_manning_case(
        tmp_path / "strong-source-failure", duration_s=0.01, manning_n=1.0,
        source_cfl_fraction=1e-12, minimum_time_step_s=1e-6, maximum_time_step_s=0.01,
    )
    with pytest.raises(TriangularSolverExecutionError) as caught:
        run_triangular_manning(manning_config)
    assert caught.value.reason == "source_cfl_below_minimum_time_step"
    assert caught.value.last_reason == "second_stage_source_cfl"
    assert caught.value.stage == "stage2"
    assert not (manning_config.parent / "manning.nc").exists()


def test_manning_mode_rejects_rainfall_and_protected_result_paths(tmp_path):
    base_config, source_config, manning_config, _ = _build_stoker_manning_case(tmp_path / "invalid-mode")
    source_data = yaml.safe_load(source_config.read_text(encoding="utf-8"))
    source_data["rainfall"] = {"enabled": True, "unit": "mm/h", "intervals": [{"start_time_s": 0.0, "end_time_s": 0.1, "intensity": 0.0}]}
    source_config.write_text(yaml.safe_dump(source_data, sort_keys=False), encoding="utf-8")
    with pytest.raises(TriangularManningExecutionError, match="does not support rainfall"):
        validate_triangular_manning_run_request(manning_config)

    source_data["rainfall"] = {"enabled": False}
    source_config.write_text(yaml.safe_dump(source_data, sort_keys=False), encoding="utf-8")
    (source_config.parent / "prepared-sources.json").unlink()
    prepare_triangular_sources(source_config)
    run_data = yaml.safe_load(manning_config.read_text(encoding="utf-8"))
    run_data["result_file"] = "base-none.nc"
    manning_config.write_text(yaml.safe_dump(run_data, sort_keys=False), encoding="utf-8")
    with pytest.raises(TriangularManningExecutionError, match="must not overwrite"):
        validate_triangular_manning_run_request(manning_config)
    assert base_config.exists()


def test_manning_execution_config_is_versioned_strict_and_uses_relative_paths():
    valid = {
        "format": "triangular-manning-run-v1", "schema_version": "1.0", "case_id": "case",
        "sources": "manning", "base_run_config_file": "base.yaml",
        "source_config_file": "source.yaml", "result_file": "manning.nc",
    }
    assert TriangularManningRunConfig.model_validate(valid).sources == "manning"
    for mutation in (
        {"sources": None},
        {"format": "triangular-solver-run-v1"},
        {"result_file": "D:/outside.nc"},
        {"unknown": True},
    ):
        candidate = dict(valid)
        if mutation == {"sources": None}:
            candidate.pop("sources")
        else:
            candidate.update(mutation)
        with pytest.raises(ValidationError):
            TriangularManningRunConfig.model_validate(candidate)

    examples = Path(__file__).parents[1] / "examples" / "triangular_sources"
    source_example = load_triangular_source_preparation_config(examples / "manning_only_source_preparation.yaml")
    run_example = load_triangular_manning_run_config(examples / "manning_run.yaml")
    assert source_example.manning.enabled is True
    assert source_example.rainfall.enabled is False
    assert run_example.sources == "manning"


def test_manning_cli_runs_the_versioned_execution_config(tmp_path, capsys):
    _, _, config, _ = _build_stoker_manning_case(tmp_path / "cli", duration_s=0.001)
    assert manning_main([str(config)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"
    assert report["sources_applied"] == "manning"
    assert (config.parent / "manning.nc").is_file()


def test_manning_output_overwrite_is_explicit_and_base_result_is_preserved(tmp_path):
    base_config, _, manning_config, _ = _build_stoker_manning_case(tmp_path / "output-protection")
    run_triangular_solver(base_config)
    base_result_path = base_config.parent / "base-none.nc"
    base_result_bytes = base_result_path.read_bytes()
    manning_result_path = manning_config.parent / "manning.nc"
    sentinel = b"previous Manning result"
    manning_result_path.write_bytes(sentinel)

    with pytest.raises(TriangularManningExecutionError, match="overwrite_existing_result"):
        run_triangular_manning(manning_config)
    assert manning_result_path.read_bytes() == sentinel

    raw = yaml.safe_load(manning_config.read_text(encoding="utf-8"))
    raw["overwrite_existing_result"] = True
    manning_config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    run_triangular_manning(manning_config)
    assert read_triangular_manning_result(manning_result_path, manning_config).format == "triangular-manning-result-v1"
    assert base_result_path.read_bytes() == base_result_bytes


def test_source_aware_alignment_accepts_legal_tail_and_rejects_source_bound_tail(tmp_path, monkeypatch):
    _, _, legal_config, _ = _build_stoker_manning_case(
        tmp_path / "legal-tail", duration_s=1.5e-6, minimum_time_step_s=1e-6,
        maximum_time_step_s=1e-6, times=[0.0, 1.5e-6],
    )
    legal_result_path = legal_config.parent / "manning.nc"
    run_triangular_manning(legal_config)
    legal = read_triangular_manning_result(legal_result_path, legal_config)
    assert legal.subminimum_alignment_step_s[-1] == pytest.approx(5e-7, abs=1e-15)
    assert legal.subminimum_alignment_cfl_limit_s[-1] >= legal.subminimum_alignment_step_s[-1]

    _, _, illegal_config, _ = _build_stoker_manning_case(
        tmp_path / "illegal-tail", duration_s=1.5e-6, minimum_time_step_s=1e-6,
        maximum_time_step_s=1e-6, times=[0.0, 1.5e-6],
    )
    from hydrodynamics import triangular_solver as solver
    real_source = solver._manning_source
    calls = {"count": 0}

    def impose_small_tail_bound(state, *args, **kwargs):
        calls["count"] += 1
        source, bound, diagnostics, reason = real_source(state, *args, **kwargs)
        if calls["count"] == 5:
            return source, 2.5e-7, diagnostics, reason
        return source, bound, diagnostics, reason

    monkeypatch.setattr(solver, "_manning_source", impose_small_tail_bound)
    with pytest.raises(TriangularSolverExecutionError) as caught:
        run_triangular_manning(illegal_config)
    assert caught.value.reason == "source_cfl_alignment"
    assert caught.value.stage == "output_alignment"
    assert caught.value.accepted_step_count > 0
    assert not (illegal_config.parent / "manning.nc").exists()


def test_source_bound_tolerance_is_scaled_only_to_the_positive_bound():
    bound = 1.075337974611429e-13
    tolerance = _source_cfl_bound_tolerance_s(bound)
    assert tolerance == min(8.0 * math.ulp(bound), 1e-12 * bound)
    assert tolerance < 1e-12
    assert not _source_cfl_bound_exceeded(bound, bound)
    assert not _source_cfl_bound_exceeded(bound + tolerance, bound)
    assert _source_cfl_bound_exceeded(bound + 2.0 * tolerance, bound)

    smallest_positive = math.nextafter(0.0, math.inf)
    assert _source_cfl_bound_tolerance_s(smallest_positive) == 0.0
    assert not _source_cfl_bound_exceeded(smallest_positive, smallest_positive)
    assert _source_cfl_bound_exceeded(math.nextafter(smallest_positive, math.inf), smallest_positive)
    assert not _source_cfl_bound_exceeded(1.0, math.inf)


def test_attempt_step_accepts_equal_and_ulp_below_stage_two_source_limits(tmp_path, monkeypatch):
    _, _, _, mesh = _build_stoker_manning_case(tmp_path / "bound-rounding", columns=2, duration_s=0.01)
    from hydrodynamics import triangular_solver as solver
    dt = 1.1e-12
    state = np.tile([1e-4, 0.0, 0.0], (mesh.n_face, 1)).astype(np.float64)

    def spatial_residual(current, *args, **kwargs):
        derivative = np.zeros_like(current)
        derivative[:, 1] = 0.03
        return derivative, math.inf, 0.0

    monkeypatch.setattr(solver, "_rhs_and_cfl", spatial_residual)
    stage1 = state.copy()
    stage1[:, 1] = dt * 0.03
    _, _, diagnostics, error = _manning_source(
        stage1, [0.1] * mesh.n_face, gravity=9.81,
        source_cfl_fraction=1.0, dry_depth_m=1e-6,
    )
    assert error is None
    equal_fraction = diagnostics["maximum_manning_gamma_per_s"] * dt
    slightly_lower_fraction = math.nextafter(equal_fraction, 0.0)
    assert 0.0 < slightly_lower_fraction < equal_fraction < 1.0
    for fraction in (equal_fraction, slightly_lower_fraction):
        attempt = _attempt_step(
            state, dt, mesh, (0.0,) * mesh.n_face, _edges(mesh),
            gravity=9.81, cfl=0.25, dry_depth_m=1e-6,
            negative_tolerance_m=1e-12, correction_budget_remaining_m3=0.0,
            manning_n_m=[0.1] * mesh.n_face, source_cfl_fraction=fraction,
        )
        assert attempt[0] is not None and attempt[4] is None


@pytest.mark.parametrize(
    "momentum_derivative,source_fraction",
    [(0.03, 7.5e-19), (2e16, 0.5)],
)
def test_attempt_step_rejects_tiny_positive_stage_two_source_bound(tmp_path, monkeypatch, momentum_derivative, source_fraction):
    _, _, _, mesh = _build_stoker_manning_case(tmp_path / f"small-bound-{momentum_derivative:g}", columns=2, duration_s=0.01)
    from hydrodynamics import triangular_solver as solver
    dt = 1.1e-12
    initial = np.tile([1e-4, 0.0, 0.0], (mesh.n_face, 1)).astype(np.float64)
    original = initial.copy()

    def forced_spatial_residual(current, *args, **kwargs):
        derivative = np.zeros_like(current)
        derivative[:, 1] = momentum_derivative
        return derivative, math.inf, 0.0

    monkeypatch.setattr(solver, "_rhs_and_cfl", forced_spatial_residual)
    result = _attempt_step(
        initial, dt, mesh, [0.0] * mesh.n_face, _edges(mesh),
        gravity=9.81, cfl=0.25, dry_depth_m=1e-6,
        negative_tolerance_m=1e-12, correction_budget_remaining_m3=0.0,
        manning_n_m=[0.1] * mesh.n_face, source_cfl_fraction=source_fraction,
    )
    assert np.array_equal(initial, original)
    assert result[0] is None and result[4] == "second_stage_source_cfl"
    assert result[7] == "stage2" and result[1] == 0.0 and result[2] == 0


def test_runner_tail_with_subclock_positive_source_bound_fails_without_publication(tmp_path, monkeypatch):
    # This controlled spatial-residual harness reaches the real runner's output-tail path;
    # it isolates the CFL comparison and is not an unmodified hydrodynamic scenario.
    duration = 1e-6 + 1.1e-12
    _, _, config, _ = _build_stoker_manning_case(
        tmp_path / "actual-tail-bound", columns=2, duration_s=duration,
        minimum_time_step_s=1e-6, maximum_time_step_s=1e-6,
        times=[0.0, duration], initial_profile="still_water", source_cfl_fraction=4e-28,
    )
    from hydrodynamics import triangular_solver as solver
    calls = {"count": 0}

    def start_momentum_during_tail(state, *args, **kwargs):
        calls["count"] += 1
        derivative = np.zeros_like(state)
        if calls["count"] > 4:
            derivative[:, 1] = 0.03
        return derivative, math.inf, 0.0

    monkeypatch.setattr(solver, "_rhs_and_cfl", start_momentum_during_tail)
    with pytest.raises(TriangularSolverExecutionError) as caught:
        run_triangular_manning(config)
    assert caught.value.reason == "source_cfl_alignment"
    assert caught.value.stage == "stage2" and caught.value.accepted_step_count == 1
    assert not (config.parent / "manning.nc").exists()


def test_manning_result_reader_rejects_alignment_step_above_recorded_limit(tmp_path):
    import netCDF4

    duration = 1e-6 + 1.1e-12
    _, _, config, _ = _build_stoker_manning_case(
        tmp_path / "tampered-tail-limit", columns=2, duration_s=duration,
        minimum_time_step_s=1e-6, maximum_time_step_s=1e-6,
        times=[0.0, duration], initial_profile="still_water",
    )
    run_triangular_manning(config)
    path = config.parent / "manning.nc"
    with netCDF4.Dataset(path, "r+") as dataset:
        tail = float(dataset.variables["subminimum_alignment_step"][0])
        assert tail > 1e-12
        dataset.variables["subminimum_alignment_cfl_limit"][0] = 1.075337974611429e-13
    with pytest.raises(TriangularResultNetCDFError, match="Manning alignment step may not exceed"):
        read_triangular_manning_result(path, config)


def test_manning_reader_rejects_a_p4_1_format_spoof(tmp_path):
    _, _, config, _ = _build_stoker_manning_case(tmp_path / "format-spoof")
    run_triangular_manning(config)
    path = config.parent / "manning.nc"
    import netCDF4
    with netCDF4.Dataset(path, "r+") as dataset:
        dataset.setncattr("format", "triangular-solver-result-v1")
    with pytest.raises(TriangularResultNetCDFError, match="unsupported Manning result format"):
        read_triangular_manning_result(path, config)


def test_stale_source_preparation_is_rejected_before_result_creation(tmp_path):
    _, source_config, manning_config, _ = _build_stoker_manning_case(tmp_path / "stale-preparation")
    preparation_path = source_config.parent / "prepared-sources.json"
    preparation = json.loads(preparation_path.read_text(encoding="utf-8"))
    preparation["manning"]["interpreted_values"][0] = 0.25
    preparation_path.write_text(json.dumps(preparation), encoding="utf-8")
    with pytest.raises(TriangularManningExecutionError, match="source-preparation artifact"):
        run_triangular_manning(manning_config)
    assert not (manning_config.parent / "manning.nc").exists()


def test_readback_failure_and_late_no_clobber_race_preserve_outputs(tmp_path, monkeypatch):
    import hydrodynamics.triangular_manning_netcdf as manning_io
    real_readback = manning_io.read_triangular_manning_result

    _, _, readback_config, _ = _build_stoker_manning_case(tmp_path / "readback-failure")
    readback_path = readback_config.parent / "manning.nc"
    readback_path.write_bytes(b"old Manning result")
    raw = yaml.safe_load(readback_config.read_text(encoding="utf-8"))
    raw["overwrite_existing_result"] = True
    readback_config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    def fail_readback(*args, **kwargs):
        raise TriangularResultNetCDFError("injected read-back failure")

    monkeypatch.setattr(manning_io, "read_triangular_manning_result", fail_readback)
    with pytest.raises(TriangularResultNetCDFError, match="read-back failure"):
        run_triangular_manning(readback_config)
    assert readback_path.read_bytes() == b"old Manning result"

    _, _, race_config, _ = _build_stoker_manning_case(tmp_path / "late-target")
    race_path = race_config.parent / "manning.nc"
    monkeypatch.setattr(manning_io, "read_triangular_manning_result", real_readback)

    def create_target_after_readback(path, *args, **kwargs):
        result = real_readback(path, *args, **kwargs)
        race_path.write_bytes(b"late competing target")
        return result

    monkeypatch.setattr(manning_io, "read_triangular_manning_result", create_target_after_readback)
    with pytest.raises(TriangularResultNetCDFError, match="appeared before publication"):
        run_triangular_manning(race_config)
    assert race_path.read_bytes() == b"late competing target"
