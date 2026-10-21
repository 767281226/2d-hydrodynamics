from __future__ import annotations

import json
import math
from pathlib import Path
import shutil

import pytest
import yaml

from hydrodynamics.mesh_preparation import prepare_mesh, write_ugrid_preparation
from hydrodynamics.triangular_business import (
    TriangularBoundaryBinding,
    TriangularBusinessError,
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    prepare_triangular_business,
    topology_fingerprint,
    write_triangular_business_package,
)
from hydrodynamics.triangular_boundaries import (
    TriangularBoundaryPreparationError,
    load_triangular_boundary_preparation_config,
    main as boundary_main,
    prepare_triangular_boundaries,
    read_triangular_boundary_preparation,
)
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh
from hydrodynamics.triangular_solver_contract import TriangularSolverContractError, validate_triangular_run_request


SYNTHETIC_DATUM = "synthetic local datum"


def _build_case(
    root: Path,
    *,
    bindings: list[dict] | None = None,
    config_groups: list[dict] | None = None,
    output_times: list[float] | None = None,
    config_overrides: dict | None = None,
    rotate: bool = False,
    reindex_nodes: bool = False,
    reverse_faces: bool = False,
    reverse_group_pairs: bool = False,
) -> tuple[Path, Path, TriangularMesh]:
    root.mkdir(parents=True, exist_ok=True)
    original_xy = ((0.0, 0.0), (3.0, 0.0), (3.0, 1.0), (0.0, 1.0))
    if rotate:
        original_xy = tuple((-y, x) for x, y in original_xy)
    node_order = (3, 2, 1, 0) if reindex_nodes else (0, 1, 2, 3)
    old_to_new = {old: new for new, old in enumerate(node_order)}
    face_nodes = [tuple(old_to_new[index] for index in face) for face in ((0, 1, 2), (0, 2, 3))]
    face_ids = [71, 72]
    if reverse_faces:
        face_nodes.reverse()
        face_ids.reverse()

    def remap_pairs(pairs):
        result = [(old_to_new[first], old_to_new[second]) for first, second in pairs]
        if reverse_group_pairs:
            result = [(second, first) for first, second in reversed(result)]
        return tuple(result)

    mesh = TriangularMesh(
        node_xy=tuple(original_xy[old] for old in node_order),
        face_nodes=tuple(face_nodes),
        coordinate_system="EPSG:32650",
        mesh_id="synthetic-boundary-contract-v1",
        face_id=tuple(face_ids),
        boundary_groups={
            "inlet": remap_pairs(((0, 3), (0, 1))),  # 1 m then 3 m
            "outlet": remap_pairs(((1, 2), (2, 3))),  # 1 m then 3 m
        },
        constraint_groups={"internal_cut": remap_pairs(((0, 2),))},
    )
    terrain = FaceTerrainField(
        mesh_id=mesh.mesh_id,
        elevation_m=(0.0, 0.0),
        valid_mask=(True, True),
        covered_area_m2=tuple(mesh.face_area),
        valid_area_m2=tuple(mesh.face_area),
        coverage_ratio=(1.0, 1.0),
        coordinate_system=mesh.coordinate_system,
        vertical_datum=SYNTHETIC_DATUM,
        vertical_datum_source="config_declaration",
    )
    mesh_path = root / "mesh.nc"
    write_ugrid_preparation(mesh_path, mesh, terrain)

    if bindings is None:
        bindings = [
            {"group_name": "inlet", "quantity": "discharge", "mode": "timeseries", "time_s": [0.0, 10.0, 20.0, 30.0], "values": [0.01, 0.0, 0.02, 0.02]},
            {"group_name": "outlet", "quantity": "discharge", "mode": "timeseries", "time_s": [0.0, 10.0, 20.0, 30.0], "values": [0.005, 0.005, 0.005, 0.005]},
        ]
    prepared_bindings = []
    for raw in bindings:
        group = raw["group_name"]
        edge_ids = tuple(mesh.boundary_groups[group])
        prepared_bindings.append(TriangularBoundaryBinding(
            mesh_id=mesh.mesh_id,
            topology_fingerprint=topology_fingerprint(mesh),
            group_name=group,
            quantity=raw["quantity"],
            mode=raw["mode"],
            edge_ids=edge_ids,
            edge_nodes=tuple(mesh.edge_nodes[index] for index in edge_ids),
            values=tuple(raw["values"]),
            time_s=None if raw["mode"] == "constant" else tuple(raw["time_s"]),
            unit="m3/s" if raw["quantity"] == "discharge" else "m",
            interpretation="total_group_discharge" if raw["quantity"] == "discharge" else "absolute_water_level",
            source="synthetic boundary request source",
            vertical_datum=None if raw["quantity"] == "discharge" else raw.get("vertical_datum", SYNTHETIC_DATUM),
        ))
    package = TriangularBusinessPackage(
        mesh=mesh,
        terrain=terrain,
        roughness=TriangularRoughnessField.from_constant(mesh, 0.035, source="synthetic roughness"),
        initial_condition=TriangularInitialConditionField.from_constant(mesh, terrain, "depth", 0.2, source="synthetic initial depth"),
        boundary_bindings=tuple(prepared_bindings),
    )
    business_path = root / "business.json"
    write_triangular_business_package(business_path, package, mesh_artifact=mesh_path)

    if config_groups is None:
        config_groups = [
            {"group_name": "inlet", "direction": "inflow", "allocation_policy": "uniform_per_unit_length", "temporal_policy": "piecewise_constant"},
            {"group_name": "outlet", "direction": "outflow", "allocation_policy": "uniform_per_unit_length", "temporal_policy": "piecewise_constant"},
        ]
    config = {
        "format": "triangular-boundary-preparation-v1",
        "schema_version": "1.0",
        "case_id": "synthetic-boundary-contract",
        "business_file": "business.json",
        "output_file": "prepared-boundaries.json",
        "output_times_s": output_times or [0.0, 10.0, 20.0, 30.0],
        "time_origin": None,
        "unbound_exterior_policy": "closed_wall",
        "boundary_bindings": config_groups,
    }
    if config_overrides:
        config.update(config_overrides)
    config_path = root / "boundary-preparation.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path, business_path, mesh


def test_prepares_length_weighted_signed_requests_without_applying_flux(tmp_path):
    config, _, _ = _build_case(tmp_path / "weighted")
    result = prepare_triangular_boundaries(config)

    inlet = next(group for group in result.boundary_groups if group.group_name == "inlet")
    segment = inlet.segments[0]
    assert [edge.length_m for edge in inlet.edges] == [1.0, 3.0]
    assert [edge.length_weight for edge in inlet.edges] == [0.25, 0.75]
    assert [edge.requested_edge_discharge_m3_s for edge in segment.edge_requests] == pytest.approx([-0.0025, -0.0075], abs=1e-15)
    assert [edge.requested_normal_discharge_m2_s for edge in segment.edge_requests] == pytest.approx([-0.0025, -0.0025], abs=1e-15)
    assert math.fsum(edge.requested_edge_discharge_m3_s for edge in segment.edge_requests) == pytest.approx(-0.01, abs=1e-15)
    assert all(math.isclose(math.hypot(edge.outward_normal_x, edge.outward_normal_y), 1.0, abs_tol=1e-12) for edge in inlet.edges)
    assert result.preparation_only is True
    assert result.simulation_result is False
    assert result.solver_completed is False
    assert result.execution_available is False
    assert result.boundary_applied is False

    outlet = next(group for group in result.boundary_groups if group.group_name == "outlet")
    out_segment = outlet.segments[0]
    assert [edge.requested_edge_discharge_m3_s for edge in out_segment.edge_requests] == pytest.approx([0.00125, 0.00375], abs=1e-15)
    assert [edge.requested_normal_discharge_m2_s for edge in out_segment.edge_requests] == pytest.approx([0.00125, 0.00125], abs=1e-15)


def test_piecewise_constant_request_integrals_and_terminal_sample(tmp_path):
    config, _, _ = _build_case(tmp_path / "schedule")
    result = prepare_triangular_boundaries(config)
    inlet = next(group for group in result.boundary_groups if group.group_name == "inlet")
    outlet = next(group for group in result.boundary_groups if group.group_name == "outlet")

    assert [item.requested_group_discharge_m3_s for item in inlet.segments] == [0.01, 0.0, 0.02]
    assert [item.requested_inflow_volume_m3 for item in (inlet,)] == pytest.approx([0.3], abs=1e-12)
    assert inlet.requested_outflow_volume_m3 == 0.0
    assert outlet.requested_outflow_volume_m3 == pytest.approx(0.15, abs=1e-12)
    assert outlet.requested_net_inflow_volume_m3 == pytest.approx(-0.15, abs=1e-12)
    assert math.fsum(group.requested_net_inflow_volume_m3 for group in result.boundary_groups) == pytest.approx(0.15, abs=1e-12)
    assert inlet.terminal_sample.raw_time_s == inlet.terminal_sample.effective_time_s == 30.0
    assert inlet.terminal_sample.value == 0.02
    assert result.unbound_exterior_policy == "closed_wall"
    assert result.unbound_exterior_edges == []


def test_raw_times_are_preserved_when_matched_to_effective_output_time(tmp_path):
    bindings = [
        {"group_name": "inlet", "quantity": "discharge", "mode": "timeseries", "time_s": [1e-13, 10.0, 20.0, 30.0], "values": [0.01, 0.0, 0.02, 0.02]},
        {"group_name": "outlet", "quantity": "discharge", "mode": "timeseries", "time_s": [0.0, 10.0, 20.0, 30.0], "values": [0.005, 0.005, 0.005, 0.005]},
    ]
    config, _, _ = _build_case(tmp_path / "raw-effective", bindings=bindings)
    result = prepare_triangular_boundaries(config)
    first = result.boundary_groups[0].segments[0]
    assert first.raw_start_time_s == 1e-13
    assert first.effective_start_time_s == 0.0


def test_boundary_endpoint_orientation_is_derived_from_geometry(tmp_path):
    config, _, _ = _build_case(tmp_path / "normal")
    result = prepare_triangular_boundaries(config)
    inlet = next(group for group in result.boundary_groups if group.group_name == "inlet")
    # The lower horizontal edge points outward south; the left vertical edge points outward west.
    assert [(round(edge.outward_normal_x), round(edge.outward_normal_y)) for edge in inlet.edges] == [(-1, 0), (0, -1)]


def test_rotation_node_reindex_face_reorder_and_reversed_endpoint_pairs_preserve_requests(tmp_path):
    base_config, _, _ = _build_case(tmp_path / "invariant-base")
    rotated_config, _, _ = _build_case(
        tmp_path / "invariant-rotated", rotate=True, reindex_nodes=True,
        reverse_faces=True, reverse_group_pairs=True,
    )
    base = prepare_triangular_boundaries(base_config)
    rotated = prepare_triangular_boundaries(rotated_config)
    for base_group, rotated_group in zip(base.boundary_groups, rotated.boundary_groups):
        assert base_group.group_name == rotated_group.group_name
        assert sorted(edge.length_m for edge in base_group.edges) == sorted(edge.length_m for edge in rotated_group.edges)
        assert sorted(edge.length_weight for edge in base_group.edges) == sorted(edge.length_weight for edge in rotated_group.edges)
        base_edges = {edge.length_m: edge for edge in base_group.edges}
        rotated_edges = {edge.length_m: edge for edge in rotated_group.edges}
        for length, edge in base_edges.items():
            turned = rotated_edges[length]
            assert turned.outward_normal_x == pytest.approx(-edge.outward_normal_y, abs=1e-12)
            assert turned.outward_normal_y == pytest.approx(edge.outward_normal_x, abs=1e-12)
        base_requests = {edge.length_m: request.requested_edge_discharge_m3_s for edge, request in zip(base_group.edges, base_group.segments[0].edge_requests)}
        rotated_requests = {edge.length_m: request.requested_edge_discharge_m3_s for edge, request in zip(rotated_group.edges, rotated_group.segments[0].edge_requests)}
        assert rotated_requests == pytest.approx(base_requests, abs=1e-12)


def test_replaced_p2_mesh_invalidates_linked_business_identity(tmp_path):
    config, _, _ = _build_case(tmp_path / "stale-p2")
    prepare_triangular_boundaries(config)
    altered_mesh = TriangularMesh(
        node_xy=((0.0, 0.0), (3.1, 0.0), (3.1, 1.0), (0.0, 1.0)),
        face_nodes=((0, 1, 2), (0, 2, 3)),
        coordinate_system="EPSG:32650", mesh_id="synthetic-boundary-contract-v1",
        face_id=(71, 72),
        boundary_groups={"inlet": ((0, 3), (0, 1)), "outlet": ((1, 2), (2, 3))},
        constraint_groups={"internal_cut": ((0, 2),)},
    )
    altered_terrain = FaceTerrainField(
        mesh_id=altered_mesh.mesh_id, elevation_m=(0.0, 0.0), valid_mask=(True, True),
        covered_area_m2=tuple(altered_mesh.face_area), valid_area_m2=tuple(altered_mesh.face_area),
        coverage_ratio=(1.0, 1.0), coordinate_system=altered_mesh.coordinate_system,
        vertical_datum=SYNTHETIC_DATUM, vertical_datum_source="config_declaration",
    )
    write_ugrid_preparation(tmp_path / "stale-p2" / "mesh.nc", altered_mesh, altered_terrain)
    with pytest.raises(TriangularBoundaryPreparationError, match="linked P3-A/P2 input is invalid"):
        read_triangular_boundary_preparation(tmp_path / "stale-p2" / "prepared-boundaries.json", config)


def test_static_absolute_water_level_is_prepared_with_datum_and_no_flux_claim(tmp_path):
    bindings = [{"group_name": "inlet", "quantity": "water_level", "mode": "constant", "values": [1.0], "vertical_datum": SYNTHETIC_DATUM}]
    groups = [{"group_name": "inlet"}]
    config, _, _ = _build_case(tmp_path / "stage", bindings=bindings, config_groups=groups)
    result = prepare_triangular_boundaries(config)
    prepared = result.boundary_groups[0]
    assert prepared.vertical_datum == SYNTHETIC_DATUM
    assert prepared.segments[0].requested_absolute_water_level_m == 1.0
    assert prepared.segments[0].edge_requests == []
    assert prepared.requested_inflow_volume_m3 == prepared.requested_outflow_volume_m3 == 0.0
    assert prepared.requested_net_inflow_volume_m3 == 0.0
    assert {edge.edge_id for edge in result.unbound_exterior_edges} == (
        set(result.exterior_edge_ids) - {edge.edge_id for edge in prepared.edges}
    )
    assert len(result.unbound_exterior_edges) == 2
    # This artifact records a stage request only; h=1 m on bed 0 m is an ADR analytic reference.
    assert result.boundary_applied is False


def test_reads_preparation_by_rebuilding_against_current_inputs(tmp_path):
    config, _, _ = _build_case(tmp_path / "readback")
    expected = prepare_triangular_boundaries(config)
    loaded = read_triangular_boundary_preparation(tmp_path / "readback" / "prepared-boundaries.json", config)
    assert loaded.model_dump(mode="json") == expected.model_dump(mode="json")

    artifact = tmp_path / "readback" / "prepared-boundaries.json"
    data = json.loads(artifact.read_text(encoding="utf-8"))
    data["boundary_applied"] = True
    artifact.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(TriangularBoundaryPreparationError, match="artifact is invalid"):
        read_triangular_boundary_preparation(artifact, config)


def test_boundary_cli_reports_preparation_without_solver_completion(tmp_path, capsys):
    config, _, _ = _build_case(tmp_path / "cli")
    assert boundary_main([str(config)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "prepared_not_executable"
    assert report["execution_available"] is False
    assert report["boundary_applied"] is False
    assert report["solver_completed"] is False


def test_changed_p3_values_invalidate_old_preparation_even_with_valid_input(tmp_path):
    config, business, _ = _build_case(tmp_path / "stale")
    prepare_triangular_boundaries(config)
    package = json.loads(business.read_text(encoding="utf-8"))
    package["boundary_bindings"][0]["values"][1] = 0.001
    business.write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(TriangularBoundaryPreparationError, match="does not match re-read"):
        read_triangular_boundary_preparation(tmp_path / "stale" / "prepared-boundaries.json", config)


@pytest.mark.parametrize(
    "mutated_groups, message",
    [
        ([{"group_name": "inlet", "allocation_policy": "uniform_per_unit_length", "temporal_policy": "piecewise_constant"}, {"group_name": "outlet", "direction": "outflow", "allocation_policy": "uniform_per_unit_length", "temporal_policy": "piecewise_constant"}], "explicit direction"),
        ([{"group_name": "inlet", "direction": "inflow", "temporal_policy": "piecewise_constant"}, {"group_name": "outlet", "direction": "outflow", "allocation_policy": "uniform_per_unit_length", "temporal_policy": "piecewise_constant"}], "allocation_policy"),
        ([{"group_name": "inlet", "direction": "inflow", "allocation_policy": "uniform_per_unit_length"}, {"group_name": "outlet", "direction": "outflow", "allocation_policy": "uniform_per_unit_length", "temporal_policy": "piecewise_constant"}], "temporal_policy"),
    ],
)
def test_missing_explicit_request_choices_are_rejected(tmp_path, mutated_groups, message):
    config, _, _ = _build_case(tmp_path / "missing-choice", config_groups=mutated_groups)
    with pytest.raises(TriangularBoundaryPreparationError, match=message):
        prepare_triangular_boundaries(config)


def test_unknown_and_internal_groups_are_rejected(tmp_path):
    groups = [
        {"group_name": "internal_cut", "direction": "inflow", "allocation_policy": "uniform_per_unit_length", "temporal_policy": "piecewise_constant"},
        {"group_name": "outlet", "direction": "outflow", "allocation_policy": "uniform_per_unit_length", "temporal_policy": "piecewise_constant"},
    ]
    config, _, _ = _build_case(tmp_path / "internal", config_groups=groups)
    with pytest.raises(TriangularBoundaryPreparationError, match="internal constraint group"):
        prepare_triangular_boundaries(config)

    groups[0]["group_name"] = "not-a-group"
    config2, _, _ = _build_case(tmp_path / "unknown", config_groups=groups)
    with pytest.raises(TriangularBoundaryPreparationError, match="unknown exterior boundary group"):
        prepare_triangular_boundaries(config2)


def test_overlapping_p3_boundary_edge_bindings_are_rejected_before_preparation():
    mesh = TriangularMesh(
        node_xy=((0.0, 0.0), (3.0, 0.0), (3.0, 1.0), (0.0, 1.0)),
        face_nodes=((0, 1, 2), (0, 2, 3)), coordinate_system="EPSG:32650",
        mesh_id="synthetic-overlap", face_id=(81, 82),
        boundary_groups={"first": ((0, 3),), "second": ((0, 3),)},
    )
    terrain = FaceTerrainField(
        mesh_id=mesh.mesh_id, elevation_m=(0.0, 0.0), valid_mask=(True, True),
        covered_area_m2=tuple(mesh.face_area), valid_area_m2=tuple(mesh.face_area),
        coverage_ratio=(1.0, 1.0), coordinate_system=mesh.coordinate_system,
    )
    roughness = TriangularRoughnessField.from_constant(mesh, 0.035, source="synthetic")
    initial = TriangularInitialConditionField.from_constant(mesh, terrain, "depth", 0.2, source="synthetic")
    bindings = []
    for name in ("first", "second"):
        edge_ids = tuple(mesh.boundary_groups[name])
        bindings.append(TriangularBoundaryBinding(
            mesh_id=mesh.mesh_id, topology_fingerprint=topology_fingerprint(mesh), group_name=name,
            quantity="discharge", mode="constant", edge_ids=edge_ids,
            edge_nodes=tuple(mesh.edge_nodes[index] for index in edge_ids), values=(0.01,), time_s=None,
            unit="m3/s", interpretation="total_group_discharge", source="synthetic",
        ))
    with pytest.raises(TriangularBusinessError, match="bound more than once"):
        TriangularBusinessPackage(mesh, terrain, roughness, initial, tuple(bindings))


def test_missing_mapping_for_p3_boundary_group_is_rejected(tmp_path):
    groups = [{"group_name": "inlet", "direction": "inflow", "allocation_policy": "uniform_per_unit_length", "temporal_policy": "piecewise_constant"}]
    config, _, _ = _build_case(tmp_path / "unmapped", config_groups=groups)
    with pytest.raises(TriangularBoundaryPreparationError, match="requires an explicit"):
        prepare_triangular_boundaries(config)


@pytest.mark.parametrize(
    "times, message",
    [
        ([0.0, 9.0, 20.0, 30.0], "does not match exactly one output time"),
        ([0.0, 10.0, 20.0], "explicitly cover"),
    ],
)
def test_timeseries_must_cover_full_axis_and_match_breakpoints(tmp_path, times, message):
    bindings = [
        {"group_name": "inlet", "quantity": "discharge", "mode": "timeseries", "time_s": times, "values": [0.01] * len(times)},
        {"group_name": "outlet", "quantity": "discharge", "mode": "timeseries", "time_s": [0.0, 10.0, 20.0, 30.0], "values": [0.005] * 4},
    ]
    config, _, _ = _build_case(tmp_path / f"times-{len(times)}-{int(times[-1])}", bindings=bindings)
    with pytest.raises(TriangularBoundaryPreparationError, match=message):
        prepare_triangular_boundaries(config)


def test_time_axis_and_strict_schema_reject_invalid_values(tmp_path):
    config, _, _ = _build_case(tmp_path / "bad-axis", output_times=[0.0, 10.0, 10.0])
    with pytest.raises(TriangularBoundaryPreparationError, match="output_times_s"):
        load_triangular_boundary_preparation_config(config)
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["output_times_s"] = [0.0, True]
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(TriangularBoundaryPreparationError, match="output_times_s"):
        load_triangular_boundary_preparation_config(config)


def test_time_origin_must_be_explicitly_present_as_timestamp_or_null(tmp_path):
    config, _, _ = _build_case(tmp_path / "missing-time-origin")
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw.pop("time_origin")
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(TriangularBoundaryPreparationError, match="time_origin"):
        load_triangular_boundary_preparation_config(config)


def test_synthetic_boundary_config_example_matches_the_strict_schema():
    example = Path(__file__).parents[1] / "examples" / "triangular_boundaries" / "boundary_preparation.yaml"
    config = load_triangular_boundary_preparation_config(example)
    assert config.format == "triangular-boundary-preparation-v1"
    assert [item.group_name for item in config.boundary_bindings] == ["outer_boundary", "hole_0"]


def test_machine_readable_reference_is_independent_and_pending_review():
    references_path = Path(__file__).parents[1] / "examples" / "triangular_boundaries" / "boundary_preparation_references.json"
    references = json.loads(references_path.read_text(encoding="utf-8"))
    assert references["status"] == "pending_main_review"
    weighted = references["references"][0]
    assert weighted["expected"]["length_weights"] == [0.25, 0.75]
    schedule = references["references"][1]
    assert schedule["expected"]["net_inflow_volume_m3"] == pytest.approx(0.15, abs=1e-12)
    assert "not solver flux validation" in references["scope"]


def test_wrong_p3_discharge_unit_is_rejected_against_native_contract(tmp_path):
    config, business, _ = _build_case(tmp_path / "bad-unit")
    package = json.loads(business.read_text(encoding="utf-8"))
    package["boundary_bindings"][0]["unit"] = "m/s"
    business.write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(TriangularBoundaryPreparationError, match="linked P3-A/P2 input is invalid"):
        prepare_triangular_boundaries(config)


def test_water_level_datum_mismatch_is_rejected_against_linked_p2(tmp_path):
    bindings = [{"group_name": "inlet", "quantity": "water_level", "mode": "constant", "values": [1.0], "vertical_datum": SYNTHETIC_DATUM}]
    config, business, _ = _build_case(tmp_path / "bad-datum", bindings=bindings, config_groups=[{"group_name": "inlet"}])
    package = json.loads(business.read_text(encoding="utf-8"))
    package["boundary_bindings"][0]["vertical_datum"] = "other datum"
    business.write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(TriangularBoundaryPreparationError, match="datum"):
        prepare_triangular_boundaries(config)


def test_output_cannot_replace_linked_input_or_unrelated_existing_file(tmp_path):
    config, business, _ = _build_case(tmp_path / "collision")
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["output_file"] = "business.json"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(TriangularBoundaryPreparationError, match="distinct from business_file"):
        prepare_triangular_boundaries(config)

    raw["output_file"] = "mesh.nc"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(TriangularBoundaryPreparationError, match="must not overwrite the boundary config, P3-A package or linked P2 mesh"):
        prepare_triangular_boundaries(config)

    config, _, _ = _build_case(tmp_path / "overwrite")
    output = tmp_path / "overwrite" / "prepared-boundaries.json"
    output.write_text("not a boundary artifact", encoding="utf-8")
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["overwrite_existing_output"] = True
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(TriangularBoundaryPreparationError, match="limited to an existing"):
        prepare_triangular_boundaries(config)


def test_default_output_refuses_an_existing_file_without_changing_its_bytes(tmp_path):
    config, _, _ = _build_case(tmp_path / "existing-default-output")
    output = config.parent / "prepared-boundaries.json"
    sentinel = b"existing unrelated file must remain unchanged"
    output.write_bytes(sentinel)
    with pytest.raises(TriangularBoundaryPreparationError, match="already exists"):
        prepare_triangular_boundaries(config)
    assert output.read_bytes() == sentinel


@pytest.mark.parametrize("field", ["format", "schema_version"])
def test_boundary_preparation_version_fields_are_required(tmp_path, field):
    config, _, _ = _build_case(tmp_path / f"missing-{field}")
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw.pop(field)
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(TriangularBoundaryPreparationError, match=field):
        load_triangular_boundary_preparation_config(config)


def test_boundary_version_and_fixed_marker_schema_contract():
    from hydrodynamics.triangular_boundaries import (
        TriangularBoundaryPreparationConfig,
        TriangularBoundaryPreparationResult,
    )

    config_schema = TriangularBoundaryPreparationConfig.model_json_schema()
    assert {"format", "schema_version"}.issubset(set(config_schema["required"]))
    result_schema = TriangularBoundaryPreparationResult.model_json_schema()["properties"]
    fixed_markers = {
        "preparation_only": True,
        "simulation_result": False,
        "solver_completed": False,
        "production_approved": False,
        "boundary_applied": False,
        "execution_available": False,
    }
    for field, value in fixed_markers.items():
        assert result_schema[field]["type"] == "boolean"
        assert result_schema[field]["const"] is value


@pytest.mark.parametrize(
    "field,value",
    [
        ("preparation_only", 1),
        ("simulation_result", 0),
        ("solver_completed", 0),
        ("production_approved", 0),
        ("boundary_applied", 0),
        ("execution_available", 0),
    ],
)
def test_preparation_reader_rejects_integer_marker_flags(tmp_path, field, value):
    config, _, _ = _build_case(tmp_path / f"integer-{field}")
    prepare_triangular_boundaries(config)
    artifact = config.parent / "prepared-boundaries.json"
    raw = json.loads(artifact.read_text(encoding="utf-8"))
    raw[field] = value
    artifact.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(TriangularBoundaryPreparationError, match="artifact is invalid"):
        read_triangular_boundary_preparation(artifact, config)


@pytest.mark.parametrize(
    "quantity,alias,partial",
    [
        ("discharge", False, False),
        ("water_level", False, False),
        ("discharge", True, False),
        ("water_level", True, True),
    ],
)
def test_p3_rejects_security_closed_wall_edges_by_physical_edge_id(tmp_path, quantity, alias, partial):
    case = tmp_path / f"security-{quantity}-{alias}-{partial}"
    case.mkdir()
    examples = Path(__file__).parents[1] / "examples" / "security_exclusion"
    for name in ("mesh.json", "dem.json", "domain.geojson", "security_area.geojson",
                 "mesh_preparation.yaml", "business_preparation.yaml"):
        shutil.copy2(examples / name, case / name)
    group_name = "security_exclusion_boundary"
    if alias:
        mesh_path = case / "mesh.json"
        mesh_data = json.loads(mesh_path.read_text(encoding="utf-8"))
        group_name = "innocent_alias"
        edge_pairs = [[1, 2], [7, 4]]
        if partial:
            edge_pairs.append([0, 1])
        mesh_data["boundary_groups"][group_name] = edge_pairs
        mesh_path.write_text(json.dumps(mesh_data), encoding="utf-8")

    prepare_mesh(case / "mesh_preparation.yaml")
    business_config = case / "business_preparation.yaml"
    business_data = yaml.safe_load(business_config.read_text(encoding="utf-8"))
    binding = {
        "type": "constant", "group_name": group_name, "quantity": quantity,
        "value": 0.01 if quantity == "discharge" else 1.0,
        "source": "synthetic security closed-wall regression",
    }
    if quantity == "water_level":
        binding["vertical_datum"] = "SYNTHETIC_LOCAL"
    business_data["boundary_bindings"] = [binding]
    business_config.write_text(yaml.safe_dump(business_data, sort_keys=False), encoding="utf-8")

    with pytest.raises(TriangularBusinessError, match="security-exclusion closed-wall P2 edge IDs"):
        prepare_triangular_business(business_config)
    assert not (case / "business.json").exists()


@pytest.mark.parametrize("overwrite", [False, True])
def test_late_destination_is_preserved_for_both_overwrite_settings(tmp_path, monkeypatch, overwrite):
    config, _, _ = _build_case(
        tmp_path / f"late-{overwrite}",
        config_overrides={"overwrite_existing_output": overwrite},
    )
    output = config.parent / "prepared-boundaries.json"
    sentinel = b"unrelated result created after boundary readback"
    module = __import__("hydrodynamics.triangular_boundaries", fromlist=["read_triangular_boundary_preparation"])
    real_read = module.read_triangular_boundary_preparation

    def interloper(path, *args, **kwargs):
        result = real_read(path, *args, **kwargs)
        if Path(path).resolve() != output.resolve():
            output.write_bytes(sentinel)
        return result

    monkeypatch.setattr(module, "read_triangular_boundary_preparation", interloper)
    with pytest.raises(TriangularBoundaryPreparationError, match="refusing to overwrite"):
        prepare_triangular_boundaries(config)
    assert output.read_bytes() == sentinel
    assert not list(output.parent.glob(".*.tmp.json"))


def test_explicit_overwrite_requires_and_replaces_a_valid_prior_artifact(tmp_path):
    config, _, _ = _build_case(
        tmp_path / "prior-boundary-artifact",
        config_overrides={"overwrite_existing_output": True},
    )
    first = prepare_triangular_boundaries(config)
    output = config.parent / "prepared-boundaries.json"
    previous_bytes = output.read_bytes()
    second = prepare_triangular_boundaries(config)
    assert second.model_dump(mode="json") == first.model_dump(mode="json")
    assert output.is_file() and output.read_bytes() == previous_bytes


def test_explicit_overwrite_preserves_an_existing_artifact_changed_during_publication(tmp_path, monkeypatch):
    config, _, _ = _build_case(
        tmp_path / "prior-artifact-race",
        config_overrides={"overwrite_existing_output": True},
    )
    prepare_triangular_boundaries(config)
    output = config.parent / "prepared-boundaries.json"
    sentinel = b"unrelated file replaced the prior preparation during staging"
    module = __import__("hydrodynamics.triangular_boundaries", fromlist=["read_triangular_boundary_preparation"])
    real_read = module.read_triangular_boundary_preparation

    def interloper(path, *args, **kwargs):
        result = real_read(path, *args, **kwargs)
        if Path(path).resolve() != output.resolve():
            output.write_bytes(sentinel)
        return result

    monkeypatch.setattr(module, "read_triangular_boundary_preparation", interloper)
    with pytest.raises(TriangularBoundaryPreparationError, match="changed during staging"):
        prepare_triangular_boundaries(config)
    assert output.read_bytes() == sentinel
    assert not list(output.parent.glob(".*.tmp.json"))


def test_new_preparation_does_not_make_old_closed_wall_route_accept_open_binding(tmp_path):
    config, business, _ = _build_case(tmp_path / "route-guard")
    prepare_triangular_boundaries(config)
    run = {
        "format": "triangular-solver-run-v1", "schema_version": "1.0", "case_id": "synthetic-boundary-contract",
        "business_file": "business.json", "result_file": "old-none-result.nc", "time_origin": None,
        "output_times_s": [0.0, 10.0, 20.0, 30.0], "boundary_policy": "closed_wall",
        "initial_momentum": "zero", "sources": "none",
    }
    run_path = tmp_path / "route-guard" / "closed-wall.yaml"
    run_path.write_text(yaml.safe_dump(run), encoding="utf-8")
    with pytest.raises(TriangularSolverContractError, match="does not accept P3-A open/group boundary bindings"):
        validate_triangular_run_request(run_path)
