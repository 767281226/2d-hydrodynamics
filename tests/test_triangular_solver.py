from __future__ import annotations

import json
import math
from pathlib import Path
import shutil

import numpy as np
import pytest
import yaml

from hydrodynamics.mesh_preparation import prepare_mesh, write_ugrid_preparation
from hydrodynamics.triangular_business import (
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    prepare_triangular_business,
    write_triangular_business_package,
)
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh
from hydrodynamics.triangular_solver import (
    TriangularSolverExecutionError,
    _attempt_step,
    _edges,
    _hll_flux,
    _integrate,
    _rhs_and_cfl,
    _sanitize_stage,
    run_triangular_solver,
)
from hydrodynamics.triangular_solver_contract import validate_triangular_run_request
from hydrodynamics.triangular_solver_netcdf import TriangularResultNetCDFError, read_triangular_result


def _build_run(
    root: Path,
    *,
    columns: int = 4,
    rows: int = 4,
    width_m: float = 2.5,
    height_m: float = 2.5,
    bed: list[float] | None = None,
    depth: list[float] | None = None,
    level: float | None = None,
    times: list[float] | None = None,
    cfl: float = 0.25,
    min_dt: float = 1e-6,
    max_dt: float = 60.0,
    retries: int = 8,
    face_id_start: int = 100_000,
    rotate: bool = False,
    permutation: list[int] | None = None,
    reverse_node_order: bool = False,
    renumber_ids: bool = False,
    output_name: str = "result.nc",
) -> tuple[Path, TriangularMesh, list[float], list[float]]:
    root.mkdir(parents=True, exist_ok=True)
    node_xy = []
    for j in range(rows + 1):
        for i in range(columns + 1):
            x, y = i * width_m, j * height_m
            if rotate:
                x, y = -y + 500_000.0, x
            else:
                x += 500_000.0
            node_xy.append((x, y))
    old_faces = []
    for j in range(rows):
        for i in range(columns):
            ll = j * (columns + 1) + i
            lr = ll + 1
            ul = ll + columns + 1
            ur = ul + 1
            old_faces.extend(((ll, lr, ur), (ll, ur, ul)))
    if permutation is None:
        permutation = list(range(len(old_faces)))
    node_map = {index: (len(node_xy) - 1 - index if reverse_node_order else index) for index in range(len(node_xy))}
    if reverse_node_order:
        node_xy = list(reversed(node_xy))
    faces = [tuple(node_map[node] for node in old_faces[index]) for index in permutation]
    ids_by_original = {
        index: (face_id_start + (len(old_faces) - 1 - index if renumber_ids else index))
        for index in range(len(old_faces))
    }
    ids = [ids_by_original[index] for index in permutation]
    mesh = TriangularMesh(node_xy, faces, "EPSG:32650", mesh_id=f"synthetic-{root.name}", face_id=ids)
    base_bed = list(bed if bed is not None else [0.0] * len(old_faces))
    if len(base_bed) != len(old_faces):
        raise ValueError("bed array must contain one value per original face")
    ordered_bed = [base_bed[index] for index in permutation]
    if depth is None:
        if level is not None:
            base_depth = [max(level - elevation, 0.0) for elevation in base_bed]
        else:
            base_depth = [1.0] * len(old_faces)
    else:
        base_depth = list(depth)
    ordered_depth = [base_depth[index] for index in permutation]
    terrain = FaceTerrainField(
        mesh_id=mesh.mesh_id,
        elevation_m=tuple(ordered_bed),
        valid_mask=(True,) * mesh.n_face,
        covered_area_m2=tuple(mesh.face_area),
        valid_area_m2=tuple(mesh.face_area),
        coverage_ratio=(1.0,) * mesh.n_face,
        coordinate_system=mesh.coordinate_system,
    )
    mesh_path = root / "mesh.nc"
    write_ugrid_preparation(mesh_path, mesh, terrain)
    initial = TriangularInitialConditionField.from_assignments(
        mesh, terrain, "depth", tuple(zip(mesh.face_id, ordered_depth)), source="frozen synthetic benchmark state"
    )
    business = TriangularBusinessPackage(
        mesh, terrain,
        TriangularRoughnessField.from_constant(mesh, 0.035, source="retained synthetic provenance; not applied"),
        initial,
        (),
    )
    business_path = root / "business.json"
    write_triangular_business_package(business_path, business, mesh_artifact=mesh_path)
    run_config = {
        "format": "triangular-solver-run-v1",
        "schema_version": "1.0",
        "case_id": root.name,
        "business_file": "business.json",
        "result_file": output_name,
        "time_origin": None,
        "output_times_s": times or [0.0, 0.1],
        "boundary_policy": "closed_wall",
        "initial_momentum": "zero",
        "sources": "none",
        "gravity_m_s2": 9.81,
        "cfl_number": cfl,
        "minimum_time_step_s": min_dt,
        "maximum_time_step_s": max_dt,
        "maximum_step_retries": retries,
    }
    config_path = root / "run.yaml"
    config_path.write_text(yaml.safe_dump(run_config, sort_keys=False), encoding="utf-8")
    return config_path, mesh, ordered_bed, ordered_depth


def _stoker_reference(x_relative_m: float, t_s: float, gravity: float = 9.81) -> tuple[float, float]:
    h_left, h_right = 2.0, 1.0
    h_star, u_star = 1.453840892374573, 1.3058337531817275
    c_left = math.sqrt(gravity * h_left)
    tail = u_star - math.sqrt(gravity * h_star)
    shock = h_star * u_star / (h_star - h_right)
    xi = (x_relative_m - 1.0) / t_s
    if xi < -c_left:
        return h_left, 0.0
    if xi < tail:
        velocity = (2.0 / 3.0) * (c_left + xi)
        c = (1.0 / 3.0) * (2.0 * c_left - xi)
        h = c * c / gravity
        return h, h * velocity
    if xi < shock:
        return h_star, h_star * u_star
    return h_right, 0.0


def _relative_l1(actual: np.ndarray, reference: np.ndarray, areas: np.ndarray) -> float:
    denominator = float(np.sum(areas * np.abs(reference)))
    assert denominator > 0.0
    return float(np.sum(areas * np.abs(actual - reference)) / denominator)


def _copy_result_with_netcdf_mutation(source: Path, destination: Path, mutation: str) -> None:
    import netCDF4

    with netCDF4.Dataset(source, "r") as old, netCDF4.Dataset(destination, "w", format="NETCDF4") as new:
        for name in old.ncattrs():
            new.setncattr(name, old.getncattr(name))
        for name, dimension in old.dimensions.items():
            new.createDimension(name, None if dimension.isunlimited() else len(dimension))
        for name, variable in old.variables.items():
            dimensions = variable.dimensions
            if mutation == "face_x_dimension" and name == "face_x":
                dimensions = ("node",)
            fill_value = None
            if mutation == "water_depth_fill" and name == "water_depth":
                fill_value = 1.0
            elif "_FillValue" in variable.ncattrs():
                fill_value = variable.getncattr("_FillValue")
            new_variable = new.createVariable(name, variable.datatype, dimensions, fill_value=fill_value)
            for attribute in variable.ncattrs():
                if attribute != "_FillValue":
                    new_variable.setncattr(attribute, variable.getncattr(attribute))
            values = variable[()] if variable.ndim == 0 else variable[:]
            if mutation == "face_x_dimension" and name == "face_x":
                values = np.resize(np.asarray(values), len(new.dimensions["node"]))
            if variable.ndim == 0:
                new_variable.assignValue(values)
            else:
                new_variable[:] = values
        if mutation in {"face_nodes_offset", "face_id_offset", "water_depth_missing", "crs_wkt", "mesh_role", "water_depth_location", "budget_units"}:
            if mutation == "face_nodes_offset":
                new.variables["face_nodes"].add_offset = 0.25
            elif mutation == "face_id_offset":
                new.variables["face_id"].add_offset = 0.25
            elif mutation == "water_depth_missing":
                new.variables["water_depth"].missing_value = 1.0
            elif mutation == "crs_wkt":
                new.variables["crs"].crs_wkt = "EPSG:4326"
            elif mutation == "mesh_role":
                new.variables["mesh2d"].cf_role = "timeseries_id"
            elif mutation == "water_depth_location":
                new.variables["water_depth"].location = "node"
            elif mutation == "budget_units":
                new.variables["mass_budget_final_volume"].units = "km3"


def _run_stoker(root: Path, columns: int, cfl: float = 0.25, *, rotate: bool = False, permutation: list[int] | None = None,
                reverse_node_order: bool = False, renumber_ids: bool = False):
    rows = 1
    width = 2.0 / columns
    base_depth = []
    for cell in range(columns):
        base_depth.extend([2.0 if (cell + 2.0 / 3.0) * width < 1.0 else 1.0,
                           2.0 if (cell + 1.0 / 3.0) * width < 1.0 else 1.0])
    if permutation is None:
        permutation = list(range(2 * columns))
    config, mesh, _, _ = _build_run(
        root, columns=columns, rows=rows, width_m=width, height_m=1.0,
        depth=base_depth, times=[0.0, 0.1], cfl=cfl, max_dt=0.01,
        face_id_start=400_001, rotate=rotate, permutation=permutation,
        reverse_node_order=reverse_node_order, renumber_ids=renumber_ids,
    )
    report = run_triangular_solver(config)
    result = read_triangular_result(root / "result.nc", config)
    # Use the exact local mesh construction coordinates. Subtracting a 500 km
    # projected origin from a stored centroid would discard several binary64
    # bits before evaluating the frozen analytic reference.
    local_x = [((index // 2) + (2.0 / 3.0 if index % 2 == 0 else 1.0 / 3.0)) * width
               for index in permutation]
    refs = [_stoker_reference(x, 0.1) for x in local_x]
    h_ref = np.asarray([pair[0] for pair in refs])
    hx_ref = np.asarray([pair[1] for pair in refs])
    return config, mesh, report, result, h_ref, hx_ref


def test_static_lakes_all_dry_and_native_round_trip(tmp_path):
    config, mesh, _, _ = _build_run(tmp_path / "flat", times=[0.0, 100.0], depth=[1.0] * 32)
    report = run_triangular_solver(config)
    result = read_triangular_result(config.parent / "result.nc", config)
    assert max(abs(h - 1.0) for row in result.water_depth_m for h in row) <= 1e-10
    assert max(result.maximum_speed_m_s_by_time) <= 1e-10
    assert abs(result.mass_budget[-1].residual_m3) <= 1e-8
    assert result.mass_correction_count == 0
    assert report["rejected_step_count"] == 0
    assert result.face_id == list(mesh.face_id)
    assert result.face_nodes == [list(face) for face in mesh.face_nodes]
    assert result.accepted_step_count > 0

    _, _, _, _ = _build_run(
        tmp_path / "step", columns=3, rows=1, width_m=1.0, height_m=1.0,
        bed=[0.0, 0.25, 0.75, 1.5, 2.0, 2.5], level=2.0, times=[0.0, 100.0],
    )
    step_config = tmp_path / "step" / "run.yaml"
    run_triangular_solver(step_config)
    step_result = read_triangular_result(tmp_path / "step" / "result.nc", step_config)
    expected = [2.0, 1.75, 1.25, 0.5, 0.0, 0.0]
    assert max(abs(h - r) for row in step_result.water_depth_m for h, r in zip(row, expected)) <= 1e-10
    assert max(step_result.maximum_speed_m_s_by_time) <= 1e-10
    assert step_result.mass_correction_count == 0
    assert abs(step_result.mass_budget[-1].residual_m3) <= 1e-8

    dry_config, _, _, _ = _build_run(tmp_path / "dry", times=[0.0, 100.0], depth=[0.0] * 32)
    dry_report = run_triangular_solver(dry_config)
    dry_result = read_triangular_result(tmp_path / "dry" / "result.nc", dry_config)
    assert all(value == 0.0 for row in dry_result.water_depth_m for value in row)
    assert all(value == 0.0 for row in dry_result.momentum_x_m2_s for value in row)
    assert dry_result.accepted_step_count > 0
    assert dry_result.rejected_step_count == 0
    assert dry_result.mass_budget[-1].residual_m3 == 0.0
    assert dry_report["minimum_accepted_step_s"] > 0.0


@pytest.mark.parametrize(
    "maximum_step,minimum_step,expected_steps",
    [(0.1, 1e-6, 10), (0.2, 1e-6, 5), (0.1, 0.1, 10)],
)
def test_public_runner_accepts_decimal_step_boundaries(tmp_path, maximum_step, minimum_step, expected_steps):
    root = tmp_path / f"decimal-{maximum_step}-{minimum_step}"
    config, _, _, _ = _build_run(
        root, columns=1, rows=1, width_m=1.0, height_m=1.0,
        depth=[0.0, 0.0], times=[0.0, 1.0], min_dt=minimum_step, max_dt=maximum_step,
    )
    run_triangular_solver(config)
    result = read_triangular_result(root / "result.nc", config)
    assert result.time_s == [0.0, 1.0]
    assert result.accepted_step_count == expected_steps
    assert result.rejected_step_count == 0
    tolerance = max(1e-12, 8.0 * math.ulp(maximum_step))
    assert result.maximum_cfl_step_s <= maximum_step + tolerance
    assert result.minimum_cfl_step_s + max(1e-12, 8.0 * math.ulp(minimum_step)) >= minimum_step
    assert result.mass_budget[-1].residual_m3 == 0.0


@pytest.mark.parametrize(
    "output_times",
    ([0.0, 0.1, 0.2, 0.3], [0.0, 0.1, 0.2, 0.30000000000000004]),
    ids=("rounded-down-end", "rounded-up-end"),
)
def test_public_runner_reads_back_decimal_output_intervals_at_minimum_step(tmp_path, output_times):
    root = tmp_path / f"output-interval-{output_times[-1]}"
    config, _, _, _ = _build_run(
        root, columns=1, rows=1, width_m=1.0, height_m=1.0,
        depth=[0.0, 0.0], times=list(output_times), min_dt=0.1, max_dt=0.1,
    )
    run_triangular_solver(config)
    result = read_triangular_result(root / "result.nc", config)
    assert result.time_s == list(output_times)
    assert result.accepted_step_count == result.accepted_cfl_step_count == 3
    assert result.subminimum_alignment_step_s == [0.0, 0.0, 0.0]
    assert result.minimum_cfl_step_s + 1e-12 >= 0.1
    assert result.maximum_cfl_step_s <= 0.1 + 1e-12


def test_bundled_p2_p3a_example_executes_and_publishes_native_result(tmp_path):
    repo = Path(__file__).parents[1]
    source_solver = repo / "examples" / "triangular_solver"
    solver_dir = tmp_path / "examples" / "triangular_solver"
    mesh_dir = tmp_path / "examples" / "triangular_mesh"
    solver_dir.mkdir(parents=True)
    mesh_dir.mkdir(parents=True)
    shutil.copy2(repo / "examples" / "triangular_mesh" / "mesh.json", mesh_dir / "mesh.json")
    for name in ("closed_wall_preparation.yaml", "full_terrain.json", "closed_wall_business.yaml", "closed_wall_run.yaml"):
        shutil.copy2(source_solver / name, solver_dir / name)
    prepare_mesh(solver_dir / "closed_wall_preparation.yaml")
    business = prepare_triangular_business(solver_dir / "closed_wall_business.yaml")
    assert business.artifact_path.exists()
    config = solver_dir / "closed_wall_run.yaml"
    report = run_triangular_solver(config)
    result_path = solver_dir / "closed_wall_result.nc"
    result = read_triangular_result(result_path, config)
    assert report["status"] == "completed"
    assert result.case_id == "synthetic-complete-terrain-closed-wall"
    assert len(result.time_s) == 3 and len(result.water_depth_m) == 3
    assert result.water_depth_m[0] == pytest.approx([0.2] * len(result.face_id))
    assert result.production_approved is False


@pytest.mark.parametrize("columns,max_h,max_hx", [(8, 0.35, 0.4), (16, 0.25, 0.3), (32, 0.18, 0.22)])
def test_frozen_wet_bed_stoker_refinement(tmp_path, columns, max_h, max_hx):
    _, mesh, report, result, h_ref, hx_ref = _run_stoker(tmp_path / f"stoker-{columns}", columns)
    h_error = _relative_l1(np.asarray(result.water_depth_m[-1]), h_ref, np.asarray(mesh.face_area))
    hx_error = _relative_l1(np.asarray(result.momentum_x_m2_s[-1]), hx_ref, np.asarray(mesh.face_area))
    assert h_error <= max_h
    assert hx_error <= max_hx
    assert min(result.water_depth_m[-1]) >= 0.0
    assert abs(result.mass_budget[-1].residual_m3) <= 1e-10 * result.volume_correction_reference_scale_m3
    assert result.mass_correction_count == 0
    assert report["accepted_step_count"] > 0
    assert report["maximum_speed_m_s_by_time"][-1] > 0.0
    assert result.maximum_internal_edge_mass_pair_residual_m3_s <= 1e-12
    if columns == 8:
        manifest = json.loads((Path(__file__).parents[1] / "examples" / "triangular_solver" / "benchmark_references.json").read_text(encoding="utf-8"))
        frozen = manifest["cases"][3]["coarse_8_column_face_centroid_reference"]
        local_x = [((index // 2) + (2.0 / 3.0 if index % 2 == 0 else 1.0 / 3.0)) * 0.25
                   for index in range(16)]
        by_x = sorted(zip(local_x, h_ref, hx_ref), key=lambda item: item[0])
        assert len(by_x) == len(frozen)
        for (x, h_value, hx_value), expected in zip(by_x, frozen):
            assert x == pytest.approx(expected["x_m"], abs=1e-15)
            assert h_value == pytest.approx(expected["water_depth_m"], abs=1e-12)
            assert hx_value == pytest.approx(expected["momentum_x_m2_s"], abs=1e-12)


def test_stoker_errors_decrease_with_mesh_refinement(tmp_path):
    errors = []
    for columns in (8, 16, 32):
        _, mesh, _, result, h_ref, hx_ref = _run_stoker(tmp_path / str(columns), columns)
        areas = np.asarray(mesh.face_area)
        errors.append((_relative_l1(np.asarray(result.water_depth_m[-1]), h_ref, areas),
                       _relative_l1(np.asarray(result.momentum_x_m2_s[-1]), hx_ref, areas)))
    assert errors[1][0] < errors[0][0] and errors[2][0] < errors[1][0]
    assert errors[1][1] < errors[0][1] and errors[2][1] < errors[1][1]


def test_cfl_time_step_sensitivity_and_internal_edge_conservation(tmp_path):
    runs = []
    for cfl in (0.25, 0.125, 0.0625):
        runs.append(_run_stoker(tmp_path / f"cfl-{cfl}", 32, cfl))
    reference_h, reference_hx = runs[-1][4:]
    results = [item[3] for item in runs]
    areas = np.asarray(runs[-1][1].face_area)
    denominator_h = float(np.sum(areas * np.abs(reference_h)))
    denominator_hx = float(np.sum(areas * np.abs(reference_hx)))
    d_h = [float(np.sum(areas * np.abs(np.asarray(results[a].water_depth_m[-1]) - np.asarray(results[b].water_depth_m[-1]))) / denominator_h)
           for a, b in ((0, 1), (1, 2))]
    d_hx = [float(np.sum(areas * np.abs(np.asarray(results[a].momentum_x_m2_s[-1]) - np.asarray(results[b].momentum_x_m2_s[-1]))) / denominator_hx)
            for a, b in ((0, 1), (1, 2))]
    for item, result in zip(runs, results):
        h_error = _relative_l1(np.asarray(result.water_depth_m[-1]), reference_h, areas)
        hx_error = _relative_l1(np.asarray(result.momentum_x_m2_s[-1]), reference_hx, areas)
        assert h_error <= 0.18 and hx_error <= 0.22
        assert min(result.water_depth_m[-1]) >= 0.0
        assert abs(result.mass_budget[-1].residual_m3) <= 1e-10 * result.volume_correction_reference_scale_m3
        assert item[2]["accepted_step_count"] == result.accepted_step_count
    assert d_h[1] <= d_h[0] + 1e-10
    assert d_hx[1] <= d_hx[0] + 1e-10
    assert d_h[0] > 0.0 and d_hx[0] > 0.0
    assert results[0].accepted_step_count < results[1].accepted_step_count < results[2].accepted_step_count
    assert all(item[2]["maximum_accepted_step_s"] > 0.0 for item in runs)
    assert all(item[3].maximum_internal_edge_mass_pair_residual_m3_s == 0.0 for item in runs)


def test_rotated_and_face_reordered_dam_break_preserves_vector_direction(tmp_path):
    base = _run_stoker(tmp_path / "base", 8)
    perm = list(reversed(range(16)))
    rotated = _run_stoker(tmp_path / "rotated", 8, rotate=True, permutation=perm,
                          reverse_node_order=True, renumber_ids=True)
    base_mesh, base_result = base[1], base[3]
    rotated_mesh, rotated_result = rotated[1], rotated[3]
    base_by_xy = {tuple(round(value, 9) for value in point): i for i, point in enumerate(base_mesh.face_centroid)}
    rotated_by_xy = {tuple(round(value, 9) for value in point): i for i, point in enumerate(rotated_mesh.face_centroid)}
    assert len(base_by_xy) == len(rotated_by_xy)
    for (x, y), a in base_by_xy.items():
        rotated_point = (round(500_000.0 - y, 9), round(x - 500_000.0, 9))
        b = rotated_by_xy[rotated_point]
        assert rotated_result.water_depth_m[-1][b] == pytest.approx(base_result.water_depth_m[-1][a], abs=1e-12)
        assert rotated_result.momentum_x_m2_s[-1][b] == pytest.approx(-base_result.momentum_y_m2_s[-1][a], abs=1e-11)
        assert rotated_result.momentum_y_m2_s[-1][b] == pytest.approx(base_result.momentum_x_m2_s[-1][a], abs=1e-11)


def test_unequal_face_areas_conserve_closed_domain_lake(tmp_path):
    root = tmp_path / "unequal-area"
    root.mkdir()
    mesh = TriangularMesh(
        ((500_000.0, 0.0), (500_002.0, 0.0), (500_001.46, 1.0), (500_000.0, 1.0)),
        ((0, 1, 2), (0, 2, 3)), "EPSG:32650", mesh_id="unequal-face-fixture", face_id=(11, 19),
    )
    assert mesh.face_area[0] != mesh.face_area[1]
    terrain = FaceTerrainField(mesh.mesh_id, (0.0, 0.0), (True, True), mesh.face_area, mesh.face_area,
                               (1.0, 1.0), mesh.coordinate_system)
    mesh_path = root / "mesh.nc"
    write_ugrid_preparation(mesh_path, mesh, terrain)
    initial = TriangularInitialConditionField.from_constant(mesh, terrain, "depth", 1.0, source="unequal-area lake")
    business = TriangularBusinessPackage(mesh, terrain,
        TriangularRoughnessField.from_constant(mesh, 0.035, source="unused"), initial, ())
    business_path = root / "business.json"
    write_triangular_business_package(business_path, business, mesh_artifact=mesh_path)
    config_path = root / "run.yaml"
    config_path.write_text(yaml.safe_dump({"case_id":"unequal-area", "business_file":"business.json", "result_file":"result.nc",
        "output_times_s":[0.0, 10.0], "boundary_policy":"closed_wall", "initial_momentum":"zero", "sources":"none"}), encoding="utf-8")
    run_triangular_solver(config_path)
    result = read_triangular_result(root / "result.nc", config_path)
    assert max(abs(depth - 1.0) for row in result.water_depth_m for depth in row) <= 1e-10
    assert abs(result.mass_budget[-1].residual_m3) <= 1e-8

    dynamic_initial = TriangularInitialConditionField.from_assignments(
        mesh, terrain, "depth", ((11, 2.0), (19, 1.0)), source="unequal-area nonzero internal flux",
    )
    dynamic_business_path = root / "dynamic-business.json"
    write_triangular_business_package(
        dynamic_business_path,
        TriangularBusinessPackage(mesh, terrain,
            TriangularRoughnessField.from_constant(mesh, 0.035, source="unused"), dynamic_initial, ()),
        mesh_artifact=mesh_path,
    )
    dynamic_config = root / "dynamic.yaml"
    dynamic_config.write_text(yaml.safe_dump({
        "case_id": "unequal-area-dynamic", "business_file": "dynamic-business.json", "result_file": "dynamic.nc",
        "output_times_s": [0.0, 0.01], "boundary_policy": "closed_wall", "initial_momentum": "zero", "sources": "none",
    }, sort_keys=False), encoding="utf-8")
    state = np.zeros((mesh.n_face, 3), dtype=np.float64)
    state[:, 0] = [2.0, 1.0]
    derivative, _, pair_residual = _rhs_and_cfl(
        state, mesh, terrain.elevation_m, _edges(mesh), gravity=9.81, cfl=0.25, dry_depth_m=1e-6,
    )
    assert derivative[0, 0] != 0.0 and derivative[1, 0] != 0.0
    integrated_mass_rate = math.fsum(area * rate for area, rate in zip(mesh.face_area, derivative[:, 0]))
    assert abs(integrated_mass_rate) <= 1e-12
    assert pair_residual > 0.0
    assert pair_residual == pytest.approx(abs(integrated_mass_rate), abs=1e-12)
    run_triangular_solver(dynamic_config)
    dynamic_result = read_triangular_result(root / "dynamic.nc", dynamic_config)
    assert dynamic_result.water_depth_m[-1] != pytest.approx([2.0, 1.0], abs=1e-12)
    assert abs(dynamic_result.mass_budget[-1].residual_m3) <= 1e-12
    assert 0.0 < dynamic_result.maximum_internal_edge_mass_pair_residual_m3_s <= 1e-12


def test_subminimum_output_alignment_is_recorded_and_cfl_checked(tmp_path, monkeypatch):
    config, _, _, _ = _build_run(tmp_path / "alignment", columns=1, rows=1, width_m=1.0, height_m=1.0,
                                 times=[0.0, 1.0], min_dt=1e-3, depth=[1.0, 1.0])
    capability = validate_triangular_run_request(config)
    from hydrodynamics import triangular_solver as solver
    mesh = capability.business.mesh
    state = np.zeros((mesh.n_face, 3), dtype=np.float64)
    state[:, 0] = 1.0
    dt_cfl = _rhs_and_cfl(state, mesh, capability.business.terrain.elevation_m, _edges(mesh),
                          gravity=capability.config.gravity_m_s2, cfl=capability.config.cfl_number,
                          dry_depth_m=capability.config.dry_depth_threshold_m)[1]
    remainder = 0.5e-3
    run_data = yaml.safe_load(config.read_text(encoding="utf-8"))
    run_data["output_times_s"] = [0.0, float(dt_cfl) + remainder]
    config.write_text(yaml.safe_dump(run_data, sort_keys=False), encoding="utf-8")
    run_triangular_solver(config)
    result = read_triangular_result(config.parent / "result.nc", config)
    assert 0.0 < result.subminimum_alignment_step_s[0] < 1e-3
    assert result.subminimum_alignment_cfl_limit_s[0] >= result.subminimum_alignment_step_s[0]
    assert result.minimum_accepted_step_s == pytest.approx(result.subminimum_alignment_step_s[0])

    bad_config, _, _, _ = _build_run(tmp_path / "bad-alignment", columns=1, rows=1, width_m=1.0, height_m=1.0,
                                     times=[0.0, 1.0], min_dt=1e-3, depth=[1.0, 1.0])
    bad_data = yaml.safe_load(bad_config.read_text(encoding="utf-8"))
    bad_data["output_times_s"] = [0.0, float(dt_cfl) + remainder]
    bad_config.write_text(yaml.safe_dump(bad_data, sort_keys=False), encoding="utf-8")
    bad_capability = validate_triangular_run_request(bad_config)
    real_rhs = solver._rhs_and_cfl
    count = {"value": 0}

    def tightened_after_first_step(*args, **kwargs):
        count["value"] += 1
        derivative, bound, pair = real_rhs(*args, **kwargs)
        if count["value"] >= 5:
            bound = 1e-6
        return derivative, bound, pair

    monkeypatch.setattr(solver, "_rhs_and_cfl", tightened_after_first_step)
    with pytest.raises(TriangularSolverExecutionError) as caught:
        _integrate(bad_capability)
    assert caught.value.reason == "alignment_cfl"
    assert caught.value.accepted_step_count == 1
    assert not (bad_config.parent / "result.nc").exists()


def test_reflected_wall_hll_flux_retains_pressure_and_has_zero_mass():
    h, u, v = 1.25, 0.7, -0.4
    normal = np.asarray([0.6, 0.8])
    un = u * normal[0] + v * normal[1]
    left = np.asarray([h, h * u, h * v])
    right = np.asarray([h, h * (u - 2.0 * un * normal[0]), h * (v - 2.0 * un * normal[1])])
    flux = _hll_flux(left, right, float(normal[0]), float(normal[1]), 9.81)
    assert flux[0] == pytest.approx(0.0, abs=1e-15)
    normal_pressure_flux = float(flux[1] * normal[0] + flux[2] * normal[1])
    assert normal_pressure_flux >= 0.5 * 9.81 * h * h
    assert normal_pressure_flux > 0.0


def test_stage_sanitation_refuses_significant_negative_and_non_finite_states(tmp_path):
    _, mesh, _, _ = _build_run(tmp_path / "state-check")
    assert _sanitize_stage(np.asarray([[1.0, 0.0, 0.0]] * mesh.n_face), mesh, dry_depth_m=1e-6, negative_tolerance_m=1e-12).reason is None
    negative = np.asarray([[1.0, 0.0, 0.0]] * mesh.n_face)
    negative[0, 0] = -1e-8
    assert _sanitize_stage(negative, mesh, dry_depth_m=1e-6, negative_tolerance_m=1e-12).reason == "negative_depth"
    nonfinite = np.asarray([[1.0, 0.0, 0.0]] * mesh.n_face)
    nonfinite[0, 1] = math.inf
    assert _sanitize_stage(nonfinite, mesh, dry_depth_m=1e-6, negative_tolerance_m=1e-12).reason == "non_finite_state"


def test_step_attempt_distinguishes_stage_failures_and_correction_budget(tmp_path, monkeypatch):
    _, mesh, bed, _ = _build_run(tmp_path / "attempt", times=[0.0, 1.0])
    from hydrodynamics import triangular_solver as solver
    edges = _edges(mesh)
    base = np.zeros((mesh.n_face, 3), dtype=np.float64)
    base[:, 0] = 1.0
    common = dict(gravity=9.81, cfl=0.25, dry_depth_m=1e-6, negative_tolerance_m=1e-12)

    def fake_rhs(derivative):
        def value(state, *args, **kwargs):
            return derivative.copy(), math.inf, 0.0
        return value

    derivative = np.zeros_like(base)
    derivative[0, 0] = -1.1
    monkeypatch.setattr(solver, "_rhs_and_cfl", fake_rhs(derivative))
    attempt = _attempt_step(base, 1.0, mesh, bed, edges, correction_budget_remaining_m3=1.0, **common)
    assert attempt[4] == "negative_depth" and attempt[5].startswith("stage 1")

    derivative = np.zeros_like(base)
    derivative[0, 1] = math.inf
    monkeypatch.setattr(solver, "_rhs_and_cfl", fake_rhs(derivative))
    attempt = _attempt_step(base, 1.0, mesh, bed, edges, correction_budget_remaining_m3=1.0, **common)
    assert attempt[4] == "non_finite_state" and attempt[5].startswith("stage 1")

    derivative = np.zeros_like(base)
    derivative[0, 0] = -1.0 - 5e-13
    monkeypatch.setattr(solver, "_rhs_and_cfl", fake_rhs(derivative))
    attempt = _attempt_step(base, 1.0, mesh, bed, edges, correction_budget_remaining_m3=0.0, **common)
    assert attempt[4] == "correction_budget"

    calls = {"value": 0}
    def fail_second_stage(state, *args, **kwargs):
        calls["value"] += 1
        derivative = np.zeros_like(state)
        if calls["value"] == 3:
            derivative[0, 0] = -2.0
        return derivative, math.inf, 0.0
    monkeypatch.setattr(solver, "_rhs_and_cfl", fail_second_stage)
    attempt = _attempt_step(base, 1.0, mesh, bed, edges, correction_budget_remaining_m3=1.0, **common)
    assert attempt[4] == "negative_depth" and attempt[5].startswith("stage 2")
    assert attempt[7] == "stage2"


def test_successful_tiny_negative_corrections_use_half_weight_per_rk_stage(tmp_path, monkeypatch):
    _, mesh, bed, _ = _build_run(tmp_path / "weighted-correction", times=[0.0, 1.0])
    from hydrodynamics import triangular_solver as solver
    state = np.zeros((mesh.n_face, 3), dtype=np.float64)
    state[:, 0] = 1e-5
    state[0, 0] = 0.0
    derivative_stage1 = np.zeros_like(state)
    derivative_stage1[0, 0] = -2e-13
    derivative_stage1[1, 0] = 2e-13 * mesh.face_area[0] / mesh.face_area[1]
    derivative_stage2 = np.zeros_like(state)
    derivative_stage2[0, 0] = -4e-13
    derivative_stage2[1, 0] = 4e-13 * mesh.face_area[0] / mesh.face_area[1]
    calls = {"count": 0}

    def conservative_tiny_negative_rhs(current, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return derivative_stage1.copy(), math.inf, 0.0
        if calls["count"] == 2:
            return np.zeros_like(current), math.inf, 0.0
        return derivative_stage2.copy(), math.inf, 0.0

    monkeypatch.setattr(solver, "_rhs_and_cfl", conservative_tiny_negative_rhs)
    attempt = _attempt_step(
        state, 1.0, mesh, bed, _edges(mesh), gravity=9.81, cfl=0.25,
        dry_depth_m=1e-6, negative_tolerance_m=1e-12, correction_budget_remaining_m3=1.0,
    )
    candidate, correction, count = attempt[:3]
    expected = 0.5 * (2e-13 * mesh.face_area[0] + 4e-13 * mesh.face_area[0])
    assert attempt[4] is None and attempt[7] is None
    assert count == 2
    assert correction == pytest.approx(expected, rel=1e-12, abs=1e-27)
    assert np.min(candidate[:, 0]) >= 0.0
    before = math.fsum(float(h) * area for h, area in zip(state[:, 0], mesh.face_area))
    after = math.fsum(float(h) * area for h, area in zip(candidate[:, 0], mesh.face_area))
    assert after - before == pytest.approx(expected, rel=1e-12, abs=1e-19)


def test_real_attempt_stage2_cfl_recheck_retries_from_unchanged_state(tmp_path, monkeypatch):
    config, _, _, _ = _build_run(
        tmp_path / "real-stage2-cfl", columns=1, rows=1, width_m=1.0, height_m=1.0,
        depth=[5e-13, 5e-13], times=[0.0, 0.2], min_dt=0.01, max_dt=0.2,
    )
    from hydrodynamics import triangular_solver as solver
    real_rhs = solver._rhs_and_cfl
    calls: list[np.ndarray] = []
    first_attempt_base: list[np.ndarray] = []

    def tightened_stage_bound(state, *args, **kwargs):
        calls.append(state.copy())
        derivative, bound, pair = real_rhs(state, *args, **kwargs)
        call_index = len(calls)
        if call_index == 2:
            first_attempt_base.append(state.copy())
            derivative[0, 0] = -5e-12  # h1 = -5e-13 m: a permitted stage correction.
        if call_index == 3:
            bound = 0.1
        elif call_index != 3:
            bound = 0.2
        return derivative, bound, pair

    monkeypatch.setattr(solver, "_rhs_and_cfl", tightened_stage_bound)
    result, _ = _integrate(validate_triangular_run_request(config))
    assert result.rejected_step_reasons == {"second_stage_cfl": 1}
    assert result.accepted_step_count == 2
    assert len(first_attempt_base) == 1 and len(calls) >= 10
    assert np.array_equal(calls[3], first_attempt_base[0])
    assert result.mass_correction_count == 0
    assert all(item.correction_volume_m3 == 0.0 for item in result.mass_budget)
    assert result.mass_budget[-1].residual_m3 == pytest.approx(0.0, abs=1e-24)


def test_retry_exhaustion_keeps_real_stage2_failure_context(tmp_path, monkeypatch):
    config, _, _, _ = _build_run(tmp_path / "stage2-failure-context", times=[0.0, 0.1], retries=0)
    from hydrodynamics import triangular_solver as solver
    real_rhs = solver._rhs_and_cfl
    calls = {"count": 0}

    def force_real_stage2_negative(state, *args, **kwargs):
        calls["count"] += 1
        derivative, limit, pair = real_rhs(state, *args, **kwargs)
        if calls["count"] == 4:
            derivative[0, 0] = -1000.0
        return derivative, limit, pair

    monkeypatch.setattr(solver, "_rhs_and_cfl", force_real_stage2_negative)
    with pytest.raises(TriangularSolverExecutionError) as caught:
        _integrate(validate_triangular_run_request(config))
    assert caught.value.reason == "retry_exhausted"
    assert caught.value.stage == "stage2"
    assert caught.value.last_reason == "negative_depth"
    assert caught.value.step_kind == "cfl_step"
    assert caught.value.rejected_step_reasons == {"negative_depth": 1}
    assert not (config.parent / "result.nc").exists()


def test_executor_retries_from_last_accepted_state_and_reports_retry_exhaustion(tmp_path, monkeypatch):
    config, _, _, _ = _build_run(tmp_path / "retry", times=[0.0, 0.1])
    capability = validate_triangular_run_request(config)
    from hydrodynamics import triangular_solver as solver
    real_attempt = solver._attempt_step
    calls = {"count": 0}
    rejected_state = []

    def one_rejection(state, dt_s, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            rejected_state.append(state.copy())
            return None, 3.14, 7, dt_s / 2, "second_stage_cfl", "injected stage-2 CFL reduction", 0.0, "stage2", {}
        if calls["count"] == 2:
            assert np.array_equal(state, rejected_state[0])
        return real_attempt(state, dt_s, *args, **kwargs)

    monkeypatch.setattr(solver, "_attempt_step", one_rejection)
    result, _ = _integrate(capability)
    assert result.rejected_step_count == 1
    assert result.rejected_step_reasons == {"second_stage_cfl": 1}
    assert result.mass_budget[-1].initial_volume_m3 == pytest.approx(result.mass_budget[-1].final_volume_m3, abs=1e-10)
    assert result.mass_correction_count == 0
    assert result.mass_budget[-1].correction_volume_m3 == 0.0

    config2, _, _, _ = _build_run(tmp_path / "exhausted", times=[0.0, 0.1], retries=1)
    cap2 = validate_triangular_run_request(config2)

    def always_reject(state, dt_s, *args, **kwargs):
        return None, 0.0, 0, math.inf, "negative_depth", "injected significant negative state", 0.0, "stage1", {}

    monkeypatch.setattr(solver, "_attempt_step", always_reject)
    with pytest.raises(TriangularSolverExecutionError) as caught:
        _integrate(cap2)
    assert caught.value.reason == "retry_exhausted"
    assert caught.value.stage == "stage1"
    assert caught.value.last_reason == "negative_depth"
    assert caught.value.accepted_step_count == 0
    assert caught.value.rejected_step_count == 2
    assert caught.value.rejected_step_reasons == {"negative_depth": 2}
    assert not (tmp_path / "exhausted" / "result.nc").exists()

    config3, _, _, _ = _build_run(tmp_path / "nonfinite", times=[0.0, 0.1])
    attempts = {"count": 0}
    def nonfinite_attempt(state, dt_s, *args, **kwargs):
        attempts["count"] += 1
        return None, 0.0, 0, math.inf, "non_finite_state", "injected non-finite stage", 0.0, "stage1", {}
    monkeypatch.setattr(solver, "_attempt_step", nonfinite_attempt)
    with pytest.raises(TriangularSolverExecutionError) as caught_nonfinite:
        run_triangular_solver(config3)
    assert caught_nonfinite.value.reason == "non_finite_state"
    assert caught_nonfinite.value.stage == "stage1"
    assert caught_nonfinite.value.last_reason == "non_finite_state"
    assert caught_nonfinite.value.rejected_step_count == 1
    assert attempts["count"] == 1
    assert not (tmp_path / "nonfinite" / "result.nc").exists()

    config4, _, _, _ = _build_run(tmp_path / "correction-overrun", times=[0.0, 0.1], retries=1)
    config4_data = yaml.safe_load(config4.read_text(encoding="utf-8"))
    config4_data["overwrite_existing_result"] = True
    config4.write_text(yaml.safe_dump(config4_data, sort_keys=False), encoding="utf-8")
    prior_result = config4.parent / "result.nc"
    prior_result.write_bytes(b"previous successful artifact")
    def over_budget(state, dt_s, *args, **kwargs):
        return None, 0.0, 0, math.inf, "correction_budget", "injected correction-budget overrun", 0.0, "stage2", {}
    monkeypatch.setattr(solver, "_attempt_step", over_budget)
    with pytest.raises(TriangularSolverExecutionError) as caught_budget:
        run_triangular_solver(config4)
    assert caught_budget.value.reason == "retry_exhausted"
    assert caught_budget.value.stage == "stage2"
    assert caught_budget.value.last_reason == "correction_budget"
    assert caught_budget.value.rejected_step_reasons == {"correction_budget": 2}
    assert prior_result.read_bytes() == b"previous successful artifact"


def test_existing_output_is_preserved_without_overwrite_and_can_be_read_back(tmp_path):
    config, _, _, _ = _build_run(tmp_path / "protected", times=[0.0, 1.0])
    result_path = config.parent / "result.nc"
    result_path.write_bytes(b"prior artifact")
    with pytest.raises(Exception, match="overwrite_existing_result"):
        run_triangular_solver(config)
    assert result_path.read_bytes() == b"prior artifact"

    config_data = yaml.safe_load(config.read_text(encoding="utf-8"))
    config_data["overwrite_existing_result"] = True
    config.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    run_triangular_solver(config)
    contract = read_triangular_result(result_path, config)
    assert contract.case_id == config.parent.name

    tampered = config.parent / "tampered.nc"
    shutil.copy2(result_path, tampered)
    import netCDF4
    with netCDF4.Dataset(tampered, "r+") as dataset:
        dataset.variables["water_depth"][0, 0] += 0.01
    with pytest.raises(TriangularResultNetCDFError, match="triangular result contract is invalid"):
        read_triangular_result(tampered, config, enforce_configured_result_path=False)


@pytest.mark.parametrize(
    "mutation,message",
    [
        ("face_nodes_offset", "may not use scale_factor or add_offset"),
        ("face_id_offset", "may not use scale_factor or add_offset"),
        ("water_depth_missing", "masked or missing values"),
        ("water_depth_fill", "masked or missing values"),
        ("crs_wkt", "crs.crs_wkt"),
        ("mesh_role", "mesh2d.cf_role"),
        ("water_depth_location", "water_depth must be associated"),
        ("budget_units", "mass_budget_final_volume.units"),
        ("face_x_dimension", "face_x dimensions"),
    ],
)
def test_native_result_reader_rejects_decoding_masks_and_metadata_conflicts(tmp_path, mutation, message):
    config, mesh, _, _ = _build_run(
        tmp_path / mutation, columns=1, rows=1, width_m=1.0, height_m=1.0,
        depth=[1.0, 1.0], times=[0.0, 0.01], face_id_start=(1 << 53) + 1,
    )
    run_triangular_solver(config)
    source = config.parent / "result.nc"
    result = read_triangular_result(source, config)
    assert result.face_id == [(1 << 53) + 1, (1 << 53) + 2]
    corrupted = config.parent / f"{mutation}.nc"
    _copy_result_with_netcdf_mutation(source, corrupted, mutation)
    with pytest.raises(TriangularResultNetCDFError, match=message):
        read_triangular_result(corrupted, config, enforce_configured_result_path=False)


def test_atomic_no_clobber_preserves_target_created_after_preflight(tmp_path, monkeypatch):
    config, _, _, _ = _build_run(tmp_path / "publish-race", columns=1, rows=1, width_m=1.0, height_m=1.0,
                                 depth=[1.0, 1.0], times=[0.0, 0.01])
    destination = config.parent / "result.nc"
    sentinel = b"created by a competing task after preflight"
    from hydrodynamics import triangular_solver_netcdf as result_io
    real_reader = result_io.read_triangular_result
    calls = {"count": 0}

    def create_competing_result(path, run_config_path, **kwargs):
        contract = real_reader(path, run_config_path, **kwargs)
        if Path(path).name.endswith(".tmp.nc"):
            calls["count"] += 1
            destination.write_bytes(sentinel)
        return contract

    monkeypatch.setattr(result_io, "read_triangular_result", create_competing_result)
    with pytest.raises(TriangularResultNetCDFError, match="appeared before atomic publication"):
        run_triangular_solver(config)
    assert calls["count"] == 1
    assert destination.read_bytes() == sentinel
    assert list(config.parent.glob(".result.nc.*.tmp.nc")) == []


def test_readback_failure_cleans_temp_and_preserves_old_overwrite_target(tmp_path, monkeypatch):
    config, _, _, _ = _build_run(tmp_path / "readback-failure", columns=1, rows=1, width_m=1.0, height_m=1.0,
                                 depth=[1.0, 1.0], times=[0.0, 0.01])
    config_data = yaml.safe_load(config.read_text(encoding="utf-8"))
    config_data["overwrite_existing_result"] = True
    config.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    destination = config.parent / "result.nc"
    sentinel = b"old result remains if read-back fails"
    destination.write_bytes(sentinel)
    from hydrodynamics import triangular_solver_netcdf as result_io

    def failed_readback(*args, **kwargs):
        raise TriangularResultNetCDFError("injected read-back validation failure")

    monkeypatch.setattr(result_io, "read_triangular_result", failed_readback)
    with pytest.raises(TriangularResultNetCDFError, match="injected read-back validation failure"):
        run_triangular_solver(config)
    assert destination.read_bytes() == sentinel
    assert list(config.parent.glob(".result.nc.*.tmp.nc")) == []


def test_frozen_benchmark_manifest_is_unchanged():
    manifest_path = Path(__file__).parents[1] / "examples" / "triangular_solver" / "benchmark_references.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "frozen_before_P4-1"
    assert [case["case_id"] for case in manifest["cases"]] == [
        "flat_lake_at_rest", "stepped_bed_lake_at_rest_with_dry_faces", "all_dry_closed_box", "wet_bed_stoker_riemann_dam_break",
    ]
