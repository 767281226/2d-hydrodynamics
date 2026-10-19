from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil

import pytest
import yaml
from pydantic import ValidationError

from hydrodynamics.mesh_preparation import write_ugrid_preparation
from hydrodynamics.triangular_business import (
    TriangularBoundaryBinding,
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    prepare_triangular_business,
    terrain_fingerprint,
    topology_fingerprint,
    write_triangular_business_package,
)
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh
from hydrodynamics.triangular_solver_contract import (
    TriangularConservedState,
    TriangularMassBudget,
    TriangularResultContract,
    TriangularSolverContractError,
    TriangularSolverRunConfig,
    validate_triangular_result_for_run,
    validate_triangular_run_request,
)


def _mesh_and_terrain(tmp_path: Path):
    mesh = TriangularMesh(
        ((500_000.0, 0.0), (500_002.0, 0.0), (500_002.0, 2.0), (500_000.0, 2.0)),
        ((0, 1, 2), (0, 2, 3)),
        "EPSG:32650",
        mesh_id="contract-square-v1",
        face_id=(1001, 1002),
        boundary_groups={"outer": ((0, 1), (1, 2), (2, 3), (3, 0))},
    )
    terrain = FaceTerrainField(
        mesh_id=mesh.mesh_id,
        elevation_m=(10.0, 10.0),
        valid_mask=(True, True),
        covered_area_m2=tuple(mesh.face_area),
        valid_area_m2=tuple(mesh.face_area),
        coverage_ratio=(1.0, 1.0),
        coordinate_system=mesh.coordinate_system,
    )
    mesh_path = tmp_path / "mesh.nc"
    write_ugrid_preparation(mesh_path, mesh, terrain)
    return mesh, terrain, mesh_path


def _write_business(tmp_path: Path, *, terrain=None, partial_initial=False, with_boundary=False):
    mesh, full_terrain, mesh_path = _mesh_and_terrain(tmp_path)
    terrain = full_terrain if terrain is None else terrain
    if terrain is not full_terrain:
        write_ugrid_preparation(mesh_path, mesh, terrain)
    if partial_initial:
        initial = TriangularInitialConditionField.from_assignments(
            mesh, terrain, "depth", ((mesh.face_id[0], 0.2),), source="one-face synthetic initial field"
        )
    else:
        initial = TriangularInitialConditionField.from_constant(
            mesh, terrain, "depth", 0.2, source="synthetic complete initial depth"
        )
    bindings = ()
    if with_boundary:
        edge_ids = tuple(mesh.boundary_groups["outer"])
        bindings = (TriangularBoundaryBinding(
            mesh.mesh_id,
            topology_fingerprint(mesh),
            "outer",
            "discharge",
            "constant",
            edge_ids,
            tuple(mesh.edge_nodes[edge_id] for edge_id in edge_ids),
            (1.0,),
            None,
            "m3/s",
            "total_group_discharge",
            "synthetic exterior forcing",
        ),)
    package = TriangularBusinessPackage(
        mesh,
        terrain,
        TriangularRoughnessField.from_constant(mesh, 0.035, source="synthetic roughness not applied"),
        initial,
        bindings,
    )
    business_path = tmp_path / "business.json"
    write_triangular_business_package(business_path, package, mesh_artifact=mesh_path)
    return mesh, terrain, mesh_path, business_path


def _config(tmp_path: Path, **updates):
    config = {
        "schema_version": "1.0",
        "case_id": "synthetic-contract-case",
        "business_file": "business.json",
        "result_file": "result.nc",
        "time_origin": None,
        "output_times_s": [0.0, 1.0, 10.0],
        "boundary_policy": "closed_wall",
        "initial_momentum": "zero",
        "sources": "none",
    }
    config.update(updates)
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _valid_result_for_capability(capability):
    mesh, terrain = capability.business.mesh, capability.business.terrain
    initial = capability.business.initial_condition
    if initial.quantity.value == "depth":
        depths = tuple(float(value) for value in initial.values_m)
    else:
        depths = tuple(max(float(level) - float(elevation), 0.0) for level, elevation in zip(initial.values_m, terrain.elevation_m))
    volume = sum(depth * area for depth, area in zip(depths, mesh.face_area))
    scale = max(volume, sum(mesh.face_area) * max(depths, default=0.0), 1.0)
    times = list(capability.config.output_times_s)
    intervals = [right - left for left, right in zip(times, times[1:])]
    boundaries = {name: [list(mesh.edge_nodes[index]) for index in ids] for name, ids in mesh.boundary_groups.items()}
    constraints = {name: [list(mesh.edge_nodes[index]) for index in ids] for name, ids in mesh.constraint_groups.items()}
    ledger = {"initial_volume_m3": volume, "boundary_net_volume_m3": 0.0, "source_net_volume_m3": 0.0,
              "correction_volume_m3": 0.0, "final_volume_m3": volume, "residual_m3": 0.0}
    return {
        "format": "triangular-solver-result-v1", "product_type": "experimental_triangular_shallow_water_result",
        "simulation_result": True, "experimental": True, "production_approved": False,
        "gate_hydraulics_enabled": False, "manning_enabled": False, "rainfall_enabled": False,
        "sources_applied": "none", "boundary_policy": "closed_wall", "case_id": capability.config.case_id,
        "time_origin": capability.config.time_origin, "coordinate_system": mesh.coordinate_system, "mesh_id": mesh.mesh_id,
        "mesh_artifact": Path(os.path.relpath(capability.mesh_path, capability.result_path.parent)).as_posix(),
        "business_package": Path(os.path.relpath(capability.business_path, capability.result_path.parent)).as_posix(),
        "solver_version": "p4-1-swe-experimental-v1",
        "run_config_sha256": hashlib.sha256(capability.config_path.read_bytes()).hexdigest(),
        "topology_fingerprint": topology_fingerprint(mesh), "terrain_fingerprint": terrain_fingerprint(mesh, terrain),
        "mesh_sha256": hashlib.sha256(capability.mesh_path.read_bytes()).hexdigest(),
        "business_package_sha256": hashlib.sha256(capability.business_path.read_bytes()).hexdigest(),
        "gravity_m_s2": capability.config.gravity_m_s2, "cfl_number": capability.config.cfl_number,
        "minimum_time_step_s": capability.config.minimum_time_step_s, "maximum_time_step_s": capability.config.maximum_time_step_s,
        "maximum_step_retries": capability.config.maximum_step_retries,
        "dry_depth_threshold_m": capability.config.dry_depth_threshold_m,
        "negative_depth_tolerance_m": capability.config.negative_depth_tolerance_m,
        "volume_correction_budget_fraction": capability.config.volume_correction_budget_fraction,
        "volume_correction_reference_scale_m3": scale,
        "volume_correction_budget_m3": capability.config.volume_correction_budget_fraction * scale,
        "mesh_geometry_tolerance_m": mesh.geometry_tolerance_m, "minimum_face_area_m2": mesh.minimum_face_area_m2,
        "node_xy_m": [list(point) for point in mesh.node_xy], "face_nodes": [list(face) for face in mesh.face_nodes],
        "face_id": list(mesh.face_id), "face_area_m2": list(mesh.face_area),
        "boundary_groups": boundaries, "constraint_groups": constraints,
        "time_s": times, "water_depth_m": [list(depths) for _ in times],
        "momentum_x_m2_s": [[0.0] * mesh.n_face for _ in times],
        "momentum_y_m2_s": [[0.0] * mesh.n_face for _ in times],
        "minimum_water_depth_m_by_time": [min(depths)] * len(times),
        "maximum_speed_m_s_by_time": [0.0] * len(times),
        "maximum_internal_edge_mass_pair_residual_m3_s": 0.0,
        "rejected_step_reasons": {},
        "mass_budget": [dict(ledger) for _ in times],
        "solver_scheme": "cell_centered_first_order_hll_hydrostatic_ssprk2",
        "accepted_step_count": len(intervals), "accepted_cfl_step_count": len(intervals),
        "rejected_step_count": 0, "mass_correction_count": 0,
        "minimum_cfl_step_s": min(intervals), "maximum_cfl_step_s": max(intervals),
        "minimum_accepted_step_s": min(intervals), "maximum_accepted_step_s": max(intervals),
        "subminimum_alignment_step_s": [0.0] * len(intervals),
        "subminimum_alignment_cfl_limit_s": [0.0] * len(intervals),
        "geometry_metadata_inferred": False, "step_statistics_inferred": False,
    }


def test_run_config_requires_explicit_wall_zero_momentum_and_no_sources():
    base = {
        "case_id": "case",
        "business_file": "business.json",
        "result_file": "result.nc",
        "output_times_s": [0.0, 1.0],
        "boundary_policy": "closed_wall",
        "initial_momentum": "zero",
        "sources": "none",
    }
    for missing in ("boundary_policy", "initial_momentum", "sources"):
        with pytest.raises(ValidationError):
            TriangularSolverRunConfig(**{key: value for key, value in base.items() if key != missing})
    with pytest.raises(ValidationError, match="strictly increasing"):
        TriangularSolverRunConfig.model_validate({**base, "output_times_s": [0.0, 1.0, 1.0]})
    with pytest.raises(ValidationError, match="interval must be at least minimum_time_step_s"):
        TriangularSolverRunConfig.model_validate({**base, "output_times_s": [0.0, 5e-7]})
    decimal_base = {**base, "minimum_time_step_s": 0.1, "maximum_time_step_s": 0.1}
    for times in ([0.0, 0.1, 0.2, 0.3], [0.0, 0.1, 0.2, 0.30000000000000004]):
        TriangularSolverRunConfig.model_validate({**decimal_base, "output_times_s": list(times)})
    for invalid_times in ([0.0, 0.1, 0.1], [0.0, 0.1, 0.09], [0.0, 0.1, 0.199999]):
        with pytest.raises(ValidationError):
            TriangularSolverRunConfig.model_validate({**decimal_base, "output_times_s": list(invalid_times)})
    with pytest.raises(ValidationError, match="less than or equal to 0.5"):
        TriangularSolverRunConfig.model_validate({**base, "cfl_number": 0.6})
    with pytest.raises(ValidationError, match="less than dry_depth_threshold_m"):
        TriangularSolverRunConfig.model_validate({**base, "negative_depth_tolerance_m": 1e-6})
    with pytest.raises(ValidationError, match="relative to the run config"):
        TriangularSolverRunConfig.model_validate({**base, "business_file": "D:/outside/business.json"})


def test_capability_check_accepts_complete_explicit_closed_wall_input_without_running(tmp_path):
    _, _, _, business_path = _write_business(tmp_path)
    config_path = _config(tmp_path)
    capability = validate_triangular_run_request(config_path)
    assert capability.business_path == business_path
    assert capability.report["status"] == "ready_for_experimental_closed_wall_run"
    assert capability.report["contract_only"] is False
    assert capability.report["execution_available"] is True
    assert capability.report["preflight_only"] is True
    assert capability.report["solver_completed"] is False
    assert capability.report["solver_completed"] is False
    assert capability.report["production_approved"] is False
    assert capability.report["manning_enabled"] is False
    assert capability.report["rainfall_enabled"] is False
    assert capability.report["gate_hydraulics_enabled"] is False
    assert capability.report["checks"]["roughness"] == "retained_as_input_provenance_not_applied"
    assert capability.report["initial_water_volume_m3"] == pytest.approx(0.8)
    assert capability.report["volume_correction_reference_scale_m3"] == pytest.approx(1.0)
    assert capability.report["volume_correction_budget_m3"] == pytest.approx(1e-10)
    assert not capability.result_path.exists()


def test_capability_check_rejects_partial_initial_and_open_boundary_bindings(tmp_path):
    _write_business(tmp_path, partial_initial=True)
    with pytest.raises(TriangularSolverContractError, match="complete initial field"):
        validate_triangular_run_request(_config(tmp_path))

    second_dir = tmp_path / "open-boundary"
    second_dir.mkdir()
    _write_business(second_dir, with_boundary=True)
    with pytest.raises(TriangularSolverContractError, match="does not accept P3-A open/group boundary bindings"):
        validate_triangular_run_request(_config(second_dir))


def test_capability_check_rejects_incomplete_terrain_instead_of_filling_it(tmp_path):
    mesh, terrain, _ = _mesh_and_terrain(tmp_path)
    partial = FaceTerrainField(
        mesh_id=mesh.mesh_id,
        elevation_m=(10.0, None),
        valid_mask=(True, False),
        covered_area_m2=tuple(mesh.face_area),
        valid_area_m2=(mesh.face_area[0], 0.0),
        coverage_ratio=(1.0, 0.0),
        coordinate_system=mesh.coordinate_system,
    )
    _write_business(tmp_path, terrain=partial)
    with pytest.raises(TriangularSolverContractError, match="requires complete terrain"):
        validate_triangular_run_request(_config(tmp_path))


def test_result_path_cannot_overwrite_p3a_package_or_indirect_linked_mesh(tmp_path):
    _, _, mesh_path, business_path = _write_business(tmp_path)
    with pytest.raises(TriangularSolverContractError, match="run config, P3-A package, or its linked P2 mesh"):
        validate_triangular_run_request(_config(tmp_path, result_file=mesh_path.name))
    with pytest.raises(TriangularSolverContractError, match="run config, P3-A package, or its linked P2 mesh"):
        validate_triangular_run_request(_config(tmp_path, result_file="./" + business_path.name))


def test_existing_result_is_protected_unless_overwrite_is_explicit(tmp_path):
    _write_business(tmp_path)
    result_path = tmp_path / "result.nc"
    result_path.write_bytes(b"existing synthetic result")
    with pytest.raises(TriangularSolverContractError, match="overwrite_existing_result"):
        validate_triangular_run_request(_config(tmp_path))
    capability = validate_triangular_run_request(_config(tmp_path, overwrite_existing_result=True))
    assert capability.result_path == result_path
    assert result_path.read_bytes() == b"existing synthetic result"


def test_conserved_state_validates_face_order_and_rejects_negative_depth(tmp_path):
    mesh, _, _ = _mesh_and_terrain(tmp_path)
    state = TriangularConservedState(
        face_id=list(mesh.face_id),
        water_depth_m=[0.2, 0.0],
        momentum_x_m2_s=[0.0, 0.0],
        momentum_y_m2_s=[0.0, 0.0],
    )
    assert state.validate_for(mesh) is state
    with pytest.raises(TriangularSolverContractError, match="face_id order"):
        state.model_copy(update={"face_id": list(reversed(mesh.face_id))}).validate_for(mesh)
    with pytest.raises(ValidationError, match="non-negative"):
        TriangularConservedState(face_id=[1], water_depth_m=[-1e-3], momentum_x_m2_s=[0.0], momentum_y_m2_s=[0.0])


def test_result_contract_checks_time_face_shape_and_mass_ledger():
    mesh = TriangularMesh(
        ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)),
        ((0, 1, 2), (0, 2, 3)), "EPSG:32650", mesh_id="result-contract", face_id=(10, 11),
    )
    budgets = [
        {"initial_volume_m3": 0.8, "boundary_net_volume_m3": 0.0, "source_net_volume_m3": 0.0, "correction_volume_m3": 0.0, "final_volume_m3": 0.8, "residual_m3": 0.0},
        {"initial_volume_m3": 0.8, "boundary_net_volume_m3": 0.0, "source_net_volume_m3": 0.0, "correction_volume_m3": 0.0, "final_volume_m3": 0.8, "residual_m3": 0.0},
    ]
    data = {
        "format": "triangular-solver-result-v1",
        "product_type": "experimental_triangular_shallow_water_result",
        "simulation_result": True,
        "experimental": True,
        "production_approved": False,
        "gate_hydraulics_enabled": False,
        "manning_enabled": False,
        "rainfall_enabled": False,
        "sources_applied": "none",
        "boundary_policy": "closed_wall",
        "case_id": "case",
        "time_origin": None,
        "coordinate_system": mesh.coordinate_system,
        "mesh_id": mesh.mesh_id,
        "mesh_artifact": "mesh.nc",
        "business_package": "business.json",
        "solver_version": "p4-1-swe-experimental-v1",
        "run_config_sha256": "e" * 64,
        "topology_fingerprint": topology_fingerprint(mesh),
        "terrain_fingerprint": "b" * 64,
        "mesh_sha256": "c" * 64,
        "business_package_sha256": "d" * 64,
        "gravity_m_s2": 9.81,
        "cfl_number": 0.25,
        "minimum_time_step_s": 1e-6,
        "maximum_time_step_s": 60.0,
        "maximum_step_retries": 8,
        "dry_depth_threshold_m": 1e-6,
        "negative_depth_tolerance_m": 1e-12,
        "volume_correction_budget_fraction": 1e-10,
        "volume_correction_reference_scale_m3": 1.0,
        "volume_correction_budget_m3": 1e-10,
        "mesh_geometry_tolerance_m": mesh.geometry_tolerance_m,
        "minimum_face_area_m2": mesh.minimum_face_area_m2,
        "node_xy_m": [list(point) for point in mesh.node_xy],
        "face_nodes": [list(face) for face in mesh.face_nodes],
        "face_id": list(mesh.face_id),
        "face_area_m2": list(mesh.face_area),
        "boundary_groups": {},
        "constraint_groups": {},
        "time_s": [0.0, 1.0],
        "water_depth_m": [[0.2, 0.2], [0.2, 0.2]],
        "momentum_x_m2_s": [[0.0, 0.0], [0.0, 0.0]],
        "momentum_y_m2_s": [[0.0, 0.0], [0.0, 0.0]],
        "minimum_water_depth_m_by_time": [0.2, 0.2],
        "maximum_speed_m_s_by_time": [0.0, 0.0],
        "maximum_internal_edge_mass_pair_residual_m3_s": 0.0,
        "rejected_step_reasons": {},
        "mass_budget": budgets,
        "solver_scheme": "cell_centered_first_order_hll_hydrostatic_ssprk2",
        "accepted_step_count": 1,
        "accepted_cfl_step_count": 1,
        "rejected_step_count": 0,
        "mass_correction_count": 0,
        "minimum_cfl_step_s": 1.0,
        "maximum_cfl_step_s": 1.0,
        "minimum_accepted_step_s": 1.0,
        "maximum_accepted_step_s": 1.0,
        "subminimum_alignment_step_s": [0.0],
        "subminimum_alignment_cfl_limit_s": [0.0],
    }
    result = TriangularResultContract.model_validate(data)
    assert len(result.water_depth_m) == len(result.time_s)
    aligned_result = TriangularResultContract.model_validate({
        **data,
        "accepted_step_count": 2,
        "accepted_cfl_step_count": 1,
        "minimum_cfl_step_s": 0.9999995,
        "maximum_cfl_step_s": 0.9999995,
        "minimum_accepted_step_s": 0.0000005,
        "maximum_accepted_step_s": 0.9999995,
        "subminimum_alignment_step_s": [0.0000005],
        "subminimum_alignment_cfl_limit_s": [0.0000006],
    })
    assert aligned_result.subminimum_alignment_step_s == [0.0000005]
    with pytest.raises(ValidationError, match="may not exceed its recorded CFL limit"):
        TriangularResultContract.model_validate({**data, "accepted_step_count": 2, "accepted_cfl_step_count": 1,
                                                "minimum_cfl_step_s": 0.9999995, "maximum_cfl_step_s": 0.9999995,
                                                "minimum_accepted_step_s": 0.0000005, "maximum_accepted_step_s": 0.9999995,
                                                "subminimum_alignment_step_s": [0.0000005],
                                                "subminimum_alignment_cfl_limit_s": [0.0000004]})
    with pytest.raises(ValidationError, match="subminimum alignment remainders"):
        TriangularResultContract.model_validate({**data, "subminimum_alignment_step_s": [1e-6]})
    with pytest.raises(ValidationError, match="must equal 0 m3"):
        TriangularMassBudget(**{**budgets[0], "boundary_net_volume_m3": 0.1})
    with pytest.raises(ValidationError, match=r"shape \(time, face\)"):
        TriangularResultContract.model_validate({**data, "momentum_y_m2_s": [[0.0], [0.0]]})
    with pytest.raises(ValidationError, match="embedded triangular geometry/topology is invalid"):
        TriangularResultContract.model_validate({**data, "node_xy_m": [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0]]})
    with pytest.raises(ValidationError, match="face_area_m2 does not match"):
        TriangularResultContract.model_validate({**data, "face_area_m2": [20.0, 20.0]})
    wrong_initial_ledger = [
        {**entry, "initial_volume_m3": 1.8, "residual_m3": -1.0}
        for entry in budgets
    ]
    with pytest.raises(ValidationError, match="first-frame initial_volume_m3"):
        TriangularResultContract.model_validate({**data, "mass_budget": wrong_initial_ledger})
    with pytest.raises(ValidationError, match="reference_scale_m3 must be computed"):
        TriangularResultContract.model_validate({**data, "volume_correction_reference_scale_m3": 1e12,
                                                  "volume_correction_budget_m3": 100.0})
    initial_correction = [
        {**entry, "correction_volume_m3": 5e-11, "residual_m3": -5e-11}
        for entry in budgets
    ]
    with pytest.raises(ValidationError, match="first-frame cumulative correction_volume_m3"):
        TriangularResultContract.model_validate({**data, "mass_budget": initial_correction, "mass_correction_count": 1})
    with pytest.raises(ValidationError, match="minimum_accepted_step_s does not match"):
        TriangularResultContract.model_validate({**data, "minimum_accepted_step_s": 100.0, "maximum_accepted_step_s": 100.0})
    with pytest.raises(ValidationError, match="accepted_step_count must equal"):
        TriangularResultContract.model_validate({**data, "accepted_step_count": 2})
    over_budget = [budgets[0], {**budgets[1], "correction_volume_m3": 1.1e-10, "residual_m3": -1.1e-10}]
    with pytest.raises(ValidationError, match="may not exceed volume_correction_budget_m3"):
        TriangularResultContract.model_validate({**data, "mass_budget": over_budget, "mass_correction_count": 1})
    with pytest.raises(ValidationError, match="single CFL step must equal"):
        TriangularResultContract.model_validate({
            **data, "minimum_time_step_s": 0.5, "maximum_time_step_s": 2.0,
            "minimum_cfl_step_s": 0.5, "maximum_cfl_step_s": 2.0,
            "minimum_accepted_step_s": 0.5, "maximum_accepted_step_s": 2.0,
        })
    with pytest.raises(ValidationError, match="CFL step extrema/count cannot realize"):
        TriangularResultContract.model_validate({
            **data, "accepted_step_count": 2, "accepted_cfl_step_count": 2,
            "minimum_cfl_step_s": 0.5, "maximum_cfl_step_s": 0.7,
            "minimum_accepted_step_s": 0.5, "maximum_accepted_step_s": 0.7,
        })
    split_interval = {
        **data,
        "minimum_time_step_s": 0.05,
        "maximum_time_step_s": 2.0,
        "time_s": [0.0, 0.1, 1.0],
        "water_depth_m": [[0.2, 0.2]] * 3,
        "momentum_x_m2_s": [[0.0, 0.0]] * 3,
        "momentum_y_m2_s": [[0.0, 0.0]] * 3,
        "minimum_water_depth_m_by_time": [0.2] * 3,
        "maximum_speed_m_s_by_time": [0.0] * 3,
        "mass_budget": [dict(budgets[0]) for _ in range(3)],
        "accepted_step_count": 2,
        "accepted_cfl_step_count": 2,
        "minimum_cfl_step_s": 0.5,
        "maximum_cfl_step_s": 0.5,
        "minimum_accepted_step_s": 0.5,
        "maximum_accepted_step_s": 0.5,
        "subminimum_alignment_step_s": [0.0, 0.0],
        "subminimum_alignment_cfl_limit_s": [0.0, 0.0],
    }
    with pytest.raises(ValidationError, match="output interval 0 cannot be covered"):
        TriangularResultContract.model_validate(split_interval)


def test_bundled_synthetic_closed_wall_request_preflights_without_solver(tmp_path):
    repo = Path(__file__).parents[1]
    source_solver = repo / "examples" / "triangular_solver"
    solver_dir = tmp_path / "examples" / "triangular_solver"
    mesh_dir = tmp_path / "examples" / "triangular_mesh"
    solver_dir.mkdir(parents=True)
    mesh_dir.mkdir(parents=True)
    shutil.copy2(repo / "examples" / "triangular_mesh" / "mesh.json", mesh_dir / "mesh.json")
    for name in ("closed_wall_preparation.yaml", "full_terrain.json", "closed_wall_business.yaml", "closed_wall_run.yaml"):
        shutil.copy2(source_solver / name, solver_dir / name)
    from hydrodynamics.mesh_preparation import prepare_mesh

    prepare_mesh(solver_dir / "closed_wall_preparation.yaml")
    prepared_business = prepare_triangular_business(solver_dir / "closed_wall_business.yaml")
    assert prepared_business.artifact_path.exists()
    capability = validate_triangular_run_request(solver_dir / "closed_wall_run.yaml")
    assert capability.report["status"] == "ready_for_experimental_closed_wall_run"
    assert capability.report["face_count"] == capability.business.mesh.n_face
    assert not capability.result_path.exists()


def test_result_run_binding_rechecks_real_mesh_terrain_initial_field_and_hashes(tmp_path):
    _write_business(tmp_path)
    config_path = _config(tmp_path)
    capability = validate_triangular_run_request(config_path)
    result = _valid_result_for_capability(capability)
    bound = validate_triangular_result_for_run(result, config_path)
    assert bound.mesh_id == capability.business.mesh.mesh_id

    valid_instance = TriangularResultContract.model_validate(result)
    assert validate_triangular_result_for_run(valid_instance, config_path).mesh_id == capability.business.mesh.mesh_id
    with pytest.raises(TriangularSolverContractError, match="must explicitly embed P2 geometry metadata"):
        validate_triangular_result_for_run({**result, "geometry_metadata_inferred": True}, config_path)
    with pytest.raises(TriangularSolverContractError, match="must explicitly embed P2 geometry metadata"):
        validate_triangular_result_for_run({**result, "step_statistics_inferred": True}, config_path)
    mutated_frame = TriangularResultContract.model_validate(result)
    mutated_frame.water_depth_m[1][0] = -1e-3  # Frozen models still contain mutable nested lists.
    with pytest.raises(TriangularSolverContractError, match="result contract is invalid"):
        validate_triangular_result_for_run(mutated_frame, config_path)
    mutated_budget = TriangularResultContract.model_validate(result)
    object.__setattr__(mutated_budget.mass_budget[1], "correction_volume_m3", 1e-3)
    with pytest.raises(TriangularSolverContractError, match="result contract is invalid"):
        validate_triangular_result_for_run(mutated_budget, config_path)

    wrong_hash = {**result, "mesh_sha256": "f" * 64}
    with pytest.raises(TriangularSolverContractError, match="mesh_sha256 does not match"):
        validate_triangular_result_for_run(wrong_hash, config_path)

    wrong_initial = {
        **result,
        "water_depth_m": [[0.21, 0.21]] * len(result["time_s"]),
        "minimum_water_depth_m_by_time": [0.21] * len(result["time_s"]),
    }
    initial_volume = 0.84
    wrong_initial["mass_budget"] = [
        {**entry, "initial_volume_m3": initial_volume, "final_volume_m3": initial_volume, "residual_m3": 0.0}
        for entry in result["mass_budget"]
    ]
    with pytest.raises(TriangularSolverContractError, match="does not match complete linked P3-A initial condition"):
        validate_triangular_result_for_run(wrong_initial, config_path)


def test_frozen_benchmark_manifest_is_not_solver_output():
    path = Path(__file__).parents[1] / "examples" / "triangular_solver" / "benchmark_references.json"
    reference = json.loads(path.read_text(encoding="utf-8"))
    assert reference["status"] == "frozen_before_P4-1"
    assert reference["scope"].startswith("synthetic")
    dam_break = next(case for case in reference["cases"] if case["case_id"] == "wet_bed_stoker_riemann_dam_break")
    assert dam_break["analytic_intermediate_state"]["star_depth_m"] == pytest.approx(1.453840892374573)
    assert dam_break["grid_refinement"]["strict_error_decrease_required"] is True
    assert len(dam_break["coarse_8_column_face_centroid_reference"]) == 16
