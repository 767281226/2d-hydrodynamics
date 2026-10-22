from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path

import pytest

from hydrodynamics.security_exclusion import SecurityExclusionEvidence
from hydrodynamics.t004_k0 import (
    K0CellStageInput,
    K0CflEdgeInput,
    K0Error,
    K0StageDomainInput,
    K0State,
    check_two_stage_domain_cfl,
    diagnose_single_edge,
    diagnose_synthetic_hll_interface,
    make_closed_wall_ghost,
    prepare_p2_edge_request,
    resolve_p2_closed_wall_edge,
    root_precision_gate,
)
from hydrodynamics.triangular_business import (
    TriangularBoundaryBinding,
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    topology_fingerprint,
)
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh


def _single_edge_package(*, depth: float = 1.0, discharge: float = 0.2,
                         group: str = "open", protected: bool = False):
    if protected:
        groups = {"security_exclusion_boundary": ((0, 1),), group: ((0, 1),)}
    else:
        groups = {group: ((0, 1),)}
    mesh = TriangularMesh(
        ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)), ((0, 1, 2),), "EPSG:4548",
        mesh_id=f"k0-{depth}-{discharge}-{group}-{protected}", face_id=(42,), boundary_groups=groups,
    )
    terrain = FaceTerrainField(mesh.mesh_id, (0.0,), (True,), (0.5,), (0.5,), (1.0,), "EPSG:4548")
    roughness = TriangularRoughnessField.from_constant(mesh, 0.03, source="synthetic K0 fixture")
    initial = TriangularInitialConditionField.from_constant(mesh, terrain, "depth", depth, source="synthetic K0 fixture")
    edge_id = mesh.boundary_groups[group][0]
    binding = TriangularBoundaryBinding(
        mesh.mesh_id, topology_fingerprint(mesh), group, "discharge", "constant",
        (edge_id,), (mesh.edge_nodes[edge_id],), (discharge,), None,
        "m3/s", "total_group_discharge", "synthetic K0 fixture",
    )
    package = TriangularBusinessPackage(mesh, terrain, roughness, initial, (binding,))
    return mesh, package, binding, edge_id


def _request(depth: float, normal_velocity: float, discharge: float, direction: str,
             *, stage_index: int = 0, max_bisections: int = 128,
             tangent: float | None = None):
    _, package, binding, edge_id = _single_edge_package(depth=depth, discharge=discharge)
    owner = K0State(depth, 0.0, -normal_velocity)
    request = prepare_p2_edge_request(
        package, binding, edge_id, owner, stage_index=stage_index, direction=direction,
        inflow_tangential_velocity_m_s=tangent, max_bisections=max_bisections,
    )
    return package, binding, edge_id, owner, request


def _security_evidence(mesh: TriangularMesh, edge_id: int) -> SecurityExclusionEvidence:
    physical_pairs = [
        tuple(sorted(tuple(float(value) for value in mesh.node_xy[node]) for node in mesh.edge_nodes[edge_id]))
    ]
    edge_digest = hashlib.sha256(
        json.dumps(physical_pairs, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    zeros = "0" * 64
    return SecurityExclusionEvidence(
        format="security-exclusion-evidence-v1", schema_version="1.0", enabled=True,
        constraint="excluded_closed_wall", mesh_id=mesh.mesh_id, mesh_crs=mesh.coordinate_system,
        topology_fingerprint=topology_fingerprint(mesh), security_input_config_path="security.json",
        security_input_config_sha256=zeros, source_file_path="security.geojson", source_file_sha256=zeros,
        source_shx_sha256=None, source_prj_sha256=None, source_crs=mesh.coordinate_system,
        source_feature_count=1, source_feature_indices=[0], source_geometry_sha256=zeros,
        source_union_geometry_sha256=zeros, domain_source_kind="P2_GMSH_CONFIG",
        domain_source_path="domain.json", domain_source_sha256=zeros, domain_source_crs=mesh.coordinate_system,
        domain_geometry_sha256=zeros, domain_geometry_wkb_hex="00", security_union_wkb_hex="00",
        original_domain_area_m2=0.5, security_union_area_m2=0.1, security_inside_area_m2=0.1,
        security_outside_area_m2=0.0, active_domain_area_m2=0.4, active_mesh_area_m2=0.4,
        coverage_residual_m2=0.0, mesh_face_count=mesh.n_face, active_face_id_sha256=zeros,
        security_boundary_group_name="security_exclusion_boundary", security_boundary_edge_count=1,
        security_boundary_edge_sha256=edge_digest,
    )


def test_equal_state_single_edge_root_reports_actual_hll_and_diagnostic_only_flags():
    _, _, _, _, request = _request(1.0, 0.2, 0.2, "outflow")
    result = diagnose_single_edge(request)
    assert result.root.proof_type == "equal_state_identity_and_negative_derivative"
    assert result.root.steps_used == 0
    assert result.root.c_g_interval_m_s[1] - result.root.c_g_interval_m_s[0] <= result.root.target_half_ulp_m_s
    assert result.owner_discharge_m2_s == result.q_requested_m2_s
    assert result.group_discharge_requested_signed_m3_s == request.group_discharge_m3_s
    assert result.group_discharge_actual_signed_m3_s == Fraction.from_float(result.hll_flux.mass_m2_s)
    assert result.hll_flux.mass_m2_s > 0.0
    assert result.hll_flux.mass_m2_s != float(result.q_requested_m2_s)
    assert result.continuous_trace["status"] == "CERTIFIED_CONTINUOUS_TRACE_ROOT"
    assert result.continuous_trace["trace_discharge_m2_s"] == pytest.approx(float(result.q_requested_m2_s))
    assert result.uncertainty.actual_abs_q_residual_m2_s <= result.uncertainty.tau_edge_m2_s
    assert result.uncertainty.actual_abs_group_residual_m3_s <= result.uncertainty.tau_group_m3_s
    assert result.calculation_fingerprint == result.root_consumer_fingerprint
    assert result.calculation_fingerprint == result.uncertainty_consumer_fingerprint == result.flux_consumer_fingerprint
    assert result.boundary_applied is False
    assert result.solver_completed is False
    assert result.result_artifacts_written is False


def test_nonzero_hll_q_root_is_certified_and_exhausted_budget_has_no_diagnostic():
    _, _, _, _, request = _request(1.0, 0.2, 0.3, "outflow")
    result = diagnose_single_edge(request)
    assert result.root.proof_type == "strict_endpoint_signs_and_negative_derivative"
    assert result.root.steps_used > 0
    assert result.hll_flux.mass_m2_s == pytest.approx(0.3, abs=2e-14)
    assert result.continuous_trace["continuous_trace_HLL_mass_flux_m2_s"] != result.hll_flux.mass_m2_s

    _, _, _, _, low_budget = _request(1.0, 0.2, 0.3, "outflow", max_bisections=1)
    with pytest.raises(K0Error, match="D0_HLL_Q_ROOT_NOT_CERTIFIED"):
        diagnose_single_edge(low_budget)


def test_rotating_geometry_and_reindexing_nodes_preserve_physical_edge_flux():
    def diagnose(mesh_id: str, nodes, face, boundary_pair, owner):
        mesh = TriangularMesh(nodes, (face,), "EPSG:4548", mesh_id=mesh_id, face_id=(42,),
                              boundary_groups={"open": (boundary_pair,)})
        terrain = FaceTerrainField(mesh.mesh_id, (0.0,), (True,), (0.5,), (0.5,), (1.0,), "EPSG:4548")
        rough = TriangularRoughnessField.from_constant(mesh, 0.03, source="rotation fixture")
        initial = TriangularInitialConditionField.from_constant(mesh, terrain, "depth", 1.0, source="rotation fixture")
        edge_id = mesh.boundary_groups["open"][0]
        binding = TriangularBoundaryBinding(
            mesh.mesh_id, topology_fingerprint(mesh), "open", "discharge", "constant", (edge_id,),
            (mesh.edge_nodes[edge_id],), (0.2,), None, "m3/s", "total_group_discharge", "rotation fixture",
        )
        package = TriangularBusinessPackage(mesh, terrain, rough, initial, (binding,))
        request = prepare_p2_edge_request(package, binding, edge_id, owner, stage_index=0, direction="outflow")
        return diagnose_single_edge(request)

    base = diagnose("k0-rotation-base", ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
                    (0, 1, 2), (0, 1), K0State(1.0, 0.0, -0.2))
    rotated = diagnose("k0-rotation-90", ((0.0, 0.0), (0.0, 1.0), (-1.0, 0.0)),
                       (0, 1, 2), (0, 1), K0State(1.0, 0.2, 0.0))
    # Old node IDs become 2,0,1 in a cyclic permutation; the CCW physical face
    # and the same physical edge are preserved while local indices change.
    reindexed = diagnose("k0-rotation-reindexed", ((1.0, 0.0), (0.0, 1.0), (0.0, 0.0)),
                         (2, 0, 1), (2, 0), K0State(1.0, 0.0, -0.2))
    assert rotated.hll_flux.mass_m2_s == pytest.approx(base.hll_flux.mass_m2_s, abs=2e-14)
    assert rotated.hll_flux.momentum_x_m3_s2 == pytest.approx(-base.hll_flux.momentum_y_m3_s2, abs=2e-13)
    assert rotated.hll_flux.momentum_y_m3_s2 == pytest.approx(base.hll_flux.momentum_x_m3_s2, abs=2e-13)
    assert reindexed.hll_flux.mass_m2_s == pytest.approx(base.hll_flux.mass_m2_s, abs=2e-14)
    assert reindexed.edge.node_pair != base.edge.node_pair


def test_r4_strict_integer_and_boolean_proof_types_reject_integral_floats_and_bools():
    identity_gate = root_precision_gate(root_exists_bracketed=True, strict_endpoint_signs=False,
                                        equal_state_identity=True, bracket_width_m_s=Fraction(0),
                                        target_width_m_s=Fraction(1), steps_used=0, max_bisections=1)
    assert identity_gate["accepted"] and identity_gate["strict_endpoint_signs"] is False
    _, package, binding, edge_id = _single_edge_package()
    owner = K0State(1.0, 0.0, -0.2)
    cloned_binding = replace(binding)
    with pytest.raises(K0Error, match="not one of this validated business package's bindings"):
        prepare_p2_edge_request(package, cloned_binding, edge_id, owner, stage_index=0, direction="outflow")
    for kwargs, message in (
        ({"stage_index": 0.0, "direction": "outflow"}, "stage_index must be a strict int"),
        ({"stage_index": False, "direction": "outflow"}, "stage_index must be a strict int"),
        ({"stage_index": 0, "direction": "outflow", "max_bisections": 1.5}, "max_bisections must be a strict int"),
    ):
        with pytest.raises(K0Error, match=message):
            prepare_p2_edge_request(package, binding, edge_id, owner, **kwargs)
    for value in (0.5, True):
        with pytest.raises(K0Error, match="steps_used must be a strict int"):
            root_precision_gate(root_exists_bracketed=True, strict_endpoint_signs=True,
                                bracket_width_m_s=Fraction(0), target_width_m_s=Fraction(1),
                                steps_used=value, max_bisections=1)
    with pytest.raises(K0Error, match="proof flags must be strict bool"):
        root_precision_gate(root_exists_bracketed=1, strict_endpoint_signs=True,
                            bracket_width_m_s=Fraction(0), target_width_m_s=Fraction(1),
                            steps_used=0, max_bisections=1)


def test_nonzero_no_root_and_domain_direction_and_zero_discharge_fail_closed():
    _, _, _, _, no_root = _request(0.8, 0.1, 1.2, "outflow")
    with pytest.raises(K0Error, match="D0_HLL_Q_ROOT_NOT_CERTIFIED"):
        diagnose_single_edge(no_root)

    _, _, _, _, out_of_depth = _request(1.0, 0.2, 0.2, "outflow")
    bad_owner = replace(out_of_depth.owner_state, h_m=1.01)
    with pytest.raises(K0Error, match="D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED"):
        prepare_p2_edge_request(out_of_depth.package, out_of_depth.binding, out_of_depth.edge.edge_index,
                                bad_owner, stage_index=0, direction="outflow")

    _, package, _, edge_id = _single_edge_package(discharge=0.0)
    zero_binding = package.boundary_bindings[0]
    with pytest.raises(K0Error, match="D0_HLL_Q_INPUT_DIRECTION_INVALID"):
        prepare_p2_edge_request(package, zero_binding, edge_id, K0State(1.0, 0.0, -0.2),
                                stage_index=0, direction="outflow")

    _, package, binding, edge_id = _single_edge_package()
    with pytest.raises(K0Error, match="inflow requires an explicit tangential velocity"):
        prepare_p2_edge_request(package, binding, edge_id, K0State(1.0, 0.0, -0.2),
                                stage_index=0, direction="inflow")
    with pytest.raises(K0Error, match="explicit zero inflow tangential velocity"):
        prepare_p2_edge_request(package, binding, edge_id, K0State(1.0, 0.0, -0.2),
                                stage_index=0, direction="inflow", inflow_tangential_velocity_m_s=0.1)
    with pytest.raises(K0Error, match="must be finite and greater than zero"):
        K0State(float("nan"), 0.0, 0.0)


@pytest.mark.parametrize(
    "owner_u_n,q,direction",
    ((0.0, 0.2, "outflow"), (0.0, -0.2, "inflow"),
     (-0.2, 0.2, "outflow"), (0.2, -0.2, "inflow")),
)
def test_adapter_and_diagnostic_reject_zero_or_opposed_owner_direction(owner_u_n, q, direction):
    _, package, binding, edge_id = _single_edge_package(discharge=abs(q))
    owner = K0State(1.0, 0.0, -owner_u_n)
    with pytest.raises(K0Error, match="D0_HLL_Q_OWNER_DIRECTION_INVALID"):
        prepare_p2_edge_request(package, binding, edge_id, owner, stage_index=0, direction=direction,
                                inflow_tangential_velocity_m_s=0.0 if direction == "inflow" else None)

    # Bypass the adapter with a replaced, internally typed request and ensure
    # diagnose revalidates the owner direction as well.
    _, _, _, _, valid = _request(1.0, 0.2 if direction == "outflow" else -0.2,
                                 abs(q), direction,
                                 tangent=0.0 if direction == "inflow" else None)
    forged = replace(valid, owner_state=owner)
    with pytest.raises(K0Error, match="D0_HLL_Q_OWNER_DIRECTION_INVALID"):
        diagnose_single_edge(forged)


def test_security_edge_identity_is_physical_even_through_a_group_alias():
    mesh = TriangularMesh(
        ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)), ((0, 1, 2),), "EPSG:4548",
        mesh_id="k0-security-alias", face_id=(42,),
        boundary_groups={"security_exclusion_boundary": ((0, 1),), "alias": ((0, 1),)},
    )
    edge_id = mesh.boundary_groups["alias"][0]
    evidence = _security_evidence(mesh, edge_id)
    terrain = FaceTerrainField(mesh.mesh_id, (0.0,), (True,), (0.5,), (0.5,), (1.0,), "EPSG:4548")
    rough = TriangularRoughnessField.from_constant(mesh, 0.03, source="security fixture")
    initial = TriangularInitialConditionField.from_constant(mesh, terrain, "depth", 1.0, source="security fixture")
    package = TriangularBusinessPackage(mesh, terrain, rough, initial, (), security_exclusion=evidence)
    forged_alias = TriangularBoundaryBinding(
        mesh.mesh_id, topology_fingerprint(mesh), "alias", "discharge", "constant", (edge_id,),
        (mesh.edge_nodes[edge_id],), (0.2,), None, "m3/s", "total_group_discharge", "forged alias",
    )
    with pytest.raises(K0Error, match="D0_SECURITY_EXCLUSION_EDGE_FORBIDDEN"):
        prepare_p2_edge_request(package, forged_alias, edge_id, K0State(1.0, 0.0, -0.2),
                                stage_index=0, direction="outflow")


def test_original_direction_counterexamples_remain_labeled_synthetic_interfaces():
    inflow = diagnose_synthetic_hll_interface(
        K0State(1.0, -1.5660459763, 0.0), K0State(0.5776, -0.0626418391, 0.0),
        1.0, 0.0, direction="inflow",
    )
    outflow = diagnose_synthetic_hll_interface(
        K0State(1.0, 1.5660459763, 0.0), K0State(1.5376, 0.0626418391, 0.0),
        1.0, 0.0, direction="outflow",
    )
    assert inflow.provenance == outflow.provenance == "SYNTHETIC_INTERFACE"
    assert inflow.hll_flux.mass_m2_s > 0.0 and not inflow.direction_matches_actual_flux
    assert outflow.hll_flux.mass_m2_s < 0.0 and not outflow.direction_matches_actual_flux
    assert inflow.boundary_applied is outflow.boundary_applied is False


def _center_star_package():
    nodes = ((0.0, 0.0), (2.0, 0.0), (1.0, 2.0), (1.0, -1.0), (3.0, 2.0), (-1.0, 2.0))
    faces = ((0, 1, 2), (0, 3, 1), (1, 4, 2), (2, 5, 0))
    mesh = TriangularMesh(nodes, faces, "EPSG:4548", mesh_id="k0-center-star", face_id=(10, 11, 12, 13))
    areas = tuple(mesh.face_area)
    terrain = FaceTerrainField(mesh.mesh_id, (0.0,) * mesh.n_face, (True,) * mesh.n_face,
                               areas, areas, (1.0,) * mesh.n_face, "EPSG:4548")
    rough = TriangularRoughnessField.from_constant(mesh, 0.03, source="CFL fixture")
    initial = TriangularInitialConditionField.from_constant(mesh, terrain, "depth", 1.0, source="CFL fixture")
    return TriangularBusinessPackage(mesh, terrain, rough, initial, ())


def _stage_domain(package: TriangularBusinessPackage, stage_index: int,
                  states_by_face: dict[int, K0State]) -> K0StageDomainInput:
    mesh = package.mesh
    cells = []
    for face_index, face_id_raw in enumerate(mesh.face_id):
        face_id = int(face_id_raw)
        owner = states_by_face[face_id]
        edges = []
        for edge_id_raw in mesh.face_edges[face_index]:
            edge_id = int(edge_id_raw)
            owner_faces = mesh.edge_faces[edge_id]
            neighbor_local = next((local for local in owner_faces if local != face_index), -1)
            if neighbor_local >= 0:
                neighbor_id = int(mesh.face_id[neighbor_local])
                edges.append(K0CflEdgeInput(edge_id, states_by_face[neighbor_id], neighbor_face_id=neighbor_id))
            else:
                edge = resolve_p2_closed_wall_edge(package, edge_id)
                wall, ghost_id = make_closed_wall_ghost(edge, owner)
                edges.append(K0CflEdgeInput(edge_id, wall, ghost_state_id=ghost_id))
        cells.append(K0CellStageInput(face_id, owner, tuple(edges)))
    return K0StageDomainInput(stage_index, tuple(cells))


def test_domain_cfl_uses_each_actual_neighbor_per_edge_and_checks_both_stages():
    package = _center_star_package()
    mesh = package.mesh
    center_index = 0
    center_id = int(mesh.face_id[center_index])
    center_state = K0State(1.0, 0.2, 0.0)
    states = {int(face_id): center_state for face_id in mesh.face_id}
    east_edge = max(mesh.face_edges[center_index], key=lambda eid: _edge_nx(mesh, int(eid), center_index))
    neighbor_local = next(local for local in mesh.edge_faces[east_edge] if local != center_index)
    fast_id = int(mesh.face_id[neighbor_local])
    states[fast_id] = K0State(1.0, 1.2, 0.0)
    stage0 = _stage_domain(package, 0, states)
    stage1 = _stage_domain(package, 1, states)
    probe = check_two_stage_domain_cfl(package, stage0, stage1, 1.0)
    per_edge_center_limit = dict(probe.per_stage_cell_limits_s)[center_id]

    n_fast = _edge_nx(mesh, int(east_edge), center_index)
    slow_alpha = abs(center_state.u_x_m_s * n_fast + center_state.u_y_m_s * _edge_ny(mesh, int(east_edge), center_index)) + (9.81 ** 0.5)
    fast_alpha = abs(1.2 * n_fast) + (9.81 ** 0.5)
    old_shared_state_denominator = Fraction(0)
    for edge_id in mesh.face_edges[center_index]:
        nx = _edge_nx(mesh, int(edge_id), center_index)
        ny = _edge_ny(mesh, int(edge_id), center_index)
        alpha = slow_alpha if int(edge_id) == int(east_edge) else abs(center_state.u_x_m_s * nx + center_state.u_y_m_s * ny) + (9.81 ** 0.5)
        old_shared_state_denominator += Fraction.from_float(float(mesh.edge_length[edge_id])) * Fraction.from_float(alpha)
    legacy_slow_limit = Fraction(1, 4) * Fraction.from_float(float(mesh.face_area[center_index])) / old_shared_state_denominator
    dt_between_legacy_and_per_edge = float((per_edge_center_limit + legacy_slow_limit) / 2)
    result = check_two_stage_domain_cfl(package, stage0, stage1, dt_between_legacy_and_per_edge)
    limits = dict(result.per_stage_cell_limits_s)
    per_edge_center_limit = limits[center_id]
    assert per_edge_center_limit < legacy_slow_limit
    assert fast_alpha > slow_alpha
    assert per_edge_center_limit < Fraction.from_float(dt_between_legacy_and_per_edge) < legacy_slow_limit
    center_alphas = {edge_id: alpha for stage_index, face_id, edge_id, alpha in result.per_edge_alpha_upper_m_s
                     if stage_index == 0 and face_id == center_id}
    assert len(center_alphas) == 3
    assert center_alphas[int(east_edge)] > max(value for edge_id, value in center_alphas.items()
                                               if edge_id != int(east_edge))
    assert result.accepted is False
    assert result.limiting_stage_index in (0, 1)
    assert result.state_advanced is False

    changed = replace(stage0, stage_index=0.0)
    with pytest.raises(K0Error, match="stage_index must be a strict int"):
        check_two_stage_domain_cfl(package, changed, stage1, 0.001)

    center_cell = stage0.cells[0]
    duplicate_edges = replace(center_cell, edges=(center_cell.edges[0], center_cell.edges[0], center_cell.edges[2]))
    duplicate_stage = replace(stage0, cells=(duplicate_edges, *stage0.cells[1:]))
    with pytest.raises(K0Error, match="repeats an edge identity"):
        check_two_stage_domain_cfl(package, duplicate_stage, stage1, 0.001)

    internal = next(item for item in center_cell.edges if item.neighbor_face_id is not None)
    wrong_neighbor = replace(internal, neighbor_face_id=True)
    wrong_identity_cell = replace(center_cell, edges=tuple(wrong_neighbor if item is internal else item for item in center_cell.edges))
    wrong_identity_stage = replace(stage0, cells=(wrong_identity_cell, *stage0.cells[1:]))
    with pytest.raises(K0Error, match="neighbor_face_id must be a strict int"):
        check_two_stage_domain_cfl(package, wrong_identity_stage, stage1, 0.001)

    changed_right = replace(internal, right_state=K0State(0.9, 0.2, 0.0))
    wrong_state_cell = replace(center_cell, edges=tuple(changed_right if item is internal else item for item in center_cell.edges))
    wrong_state_stage = replace(stage0, cells=(wrong_state_cell, *stage0.cells[1:]))
    with pytest.raises(K0Error, match="right state bits must equal the actual neighbor"):
        check_two_stage_domain_cfl(package, wrong_state_stage, stage1, 0.001)


def _edge_nx(mesh: TriangularMesh, edge_id: int, owner_face: int) -> float:
    face = mesh.face_nodes[owner_face]
    first, second = next((a, b) for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0]))
                         if set((a, b)) == set(mesh.edge_nodes[edge_id]))
    x0, y0 = mesh.node_xy[first]
    x1, y1 = mesh.node_xy[second]
    length = mesh.edge_length[edge_id]
    return (y1 - y0) / length


def _edge_ny(mesh: TriangularMesh, edge_id: int, owner_face: int) -> float:
    face = mesh.face_nodes[owner_face]
    first, second = next((a, b) for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0]))
                         if set((a, b)) == set(mesh.edge_nodes[edge_id]))
    x0, y0 = mesh.node_xy[first]
    x1, y1 = mesh.node_xy[second]
    length = mesh.edge_length[edge_id]
    return -(x1 - x0) / length


def test_actual_open_ghost_bits_are_the_cfl_consumer_and_unbound_edges_stay_walls():
    mesh, package, binding, edge_id = _single_edge_package()
    owner = K0State(1.0, 0.0, -0.2)
    request0 = prepare_p2_edge_request(package, binding, edge_id, owner, stage_index=0, direction="outflow")
    request1 = prepare_p2_edge_request(package, binding, edge_id, owner, stage_index=1, direction="outflow")
    diag0, diag1 = diagnose_single_edge(request0), diagnose_single_edge(request1)

    def one_stage(stage_index: int, diagnostic, request, *, current_package=package, current_owner=owner):
        edges = []
        for edge_raw in mesh.face_edges[0]:
            edge_index = int(edge_raw)
            if edge_index == edge_id:
                edges.append(K0CflEdgeInput(edge_index, diagnostic.ghost_state,
                                           ghost_state_id=diagnostic.ghost_state_id,
                                           boundary_request=request))
            else:
                wall_edge = resolve_p2_closed_wall_edge(current_package, edge_index)
                wall, ghost_id = make_closed_wall_ghost(wall_edge, current_owner)
                edges.append(K0CflEdgeInput(edge_index, wall, ghost_state_id=ghost_id))
        return K0StageDomainInput(stage_index, (K0CellStageInput(42, current_owner, tuple(edges)),))

    result = check_two_stage_domain_cfl(
        package, one_stage(0, diag0, request0), one_stage(1, diag1, request1), 0.001,
        stage0_boundary_diagnostics=(diag0,), stage1_boundary_diagnostics=(diag1,),
    )
    assert result.accepted is True
    assert result.input_fingerprints == (diag0.input_fingerprint, diag1.input_fingerprint)
    assert result.calculation_fingerprints == (diag0.calculation_fingerprint, diag1.calculation_fingerprint)
    assert all(item is False for item in (diag0.boundary_applied, diag1.boundary_applied, result.state_advanced))

    changed_binding = TriangularBoundaryBinding(
        mesh.mesh_id, topology_fingerprint(mesh), binding.group_name, "discharge", "constant",
        (edge_id,), (mesh.edge_nodes[edge_id],), (0.3,), None,
        "m3/s", "total_group_discharge", "new legal forcing",
    )
    changed_package = TriangularBusinessPackage(mesh, package.terrain, package.roughness,
                                                package.initial_condition, (changed_binding,))
    changed_req0 = prepare_p2_edge_request(changed_package, changed_binding, edge_id, owner,
                                           stage_index=0, direction="outflow")
    changed_req1 = prepare_p2_edge_request(changed_package, changed_binding, edge_id, owner,
                                           stage_index=1, direction="outflow")
    # Old package-bound request is rejected; a fresh request cannot legitimize
    # an old Q=0.2 diagnostic or ghost under the new Q=0.3 binding.
    with pytest.raises(K0Error, match="current package object"):
        check_two_stage_domain_cfl(
            changed_package, one_stage(0, diag0, request0, current_package=changed_package),
            one_stage(1, diag1, request1, current_package=changed_package), 0.001,
            stage0_boundary_diagnostics=(diag0,), stage1_boundary_diagnostics=(diag1,),
        )
    with pytest.raises(K0Error, match="cached diagnostic input identity"):
        check_two_stage_domain_cfl(
            changed_package, one_stage(0, diag0, changed_req0, current_package=changed_package),
            one_stage(1, diag1, changed_req1, current_package=changed_package), 0.001,
            stage0_boundary_diagnostics=(diag0,), stage1_boundary_diagnostics=(diag1,),
        )

    fresh0, fresh1 = diagnose_single_edge(changed_req0), diagnose_single_edge(changed_req1)
    fresh = check_two_stage_domain_cfl(
        changed_package, one_stage(0, fresh0, changed_req0, current_package=changed_package),
        one_stage(1, fresh1, changed_req1, current_package=changed_package), 0.001,
        stage0_boundary_diagnostics=(fresh0,), stage1_boundary_diagnostics=(fresh1,),
    )
    assert fresh.accepted is True

    changed_terrain = replace(package.terrain, elevation_m=(1.0,))
    terrain_package = TriangularBusinessPackage(mesh, changed_terrain, package.roughness,
                                                package.initial_condition, (binding,))
    terrain_request = prepare_p2_edge_request(terrain_package, binding, edge_id, owner,
                                              stage_index=0, direction="outflow")
    with pytest.raises(K0Error, match="cached diagnostic input identity"):
        check_two_stage_domain_cfl(
            terrain_package, one_stage(0, diag0, terrain_request, current_package=terrain_package),
            one_stage(1, diag1, request1, current_package=terrain_package), 0.001,
            stage0_boundary_diagnostics=(diag0,), stage1_boundary_diagnostics=(diag1,),
        )

    changed_initial = TriangularInitialConditionField.from_constant(
        mesh, package.terrain, "depth", 0.9, source="new stage-0 depth",
    )
    initial_package = TriangularBusinessPackage(mesh, package.terrain, package.roughness,
                                                changed_initial, (binding,))
    initial_owner = K0State(0.9, 0.0, -0.2)
    initial_request = prepare_p2_edge_request(initial_package, binding, edge_id, initial_owner,
                                              stage_index=0, direction="outflow")
    with pytest.raises(K0Error, match="cached diagnostic input identity"):
        check_two_stage_domain_cfl(
            initial_package,
            one_stage(0, diag0, initial_request, current_package=initial_package, current_owner=initial_owner),
            one_stage(1, diag1, request1, current_package=initial_package), 0.001,
            stage0_boundary_diagnostics=(diag0,), stage1_boundary_diagnostics=(diag1,),
        )

    changed_owner = K0State(1.0, 0.0, -0.3)
    owner_request = prepare_p2_edge_request(package, binding, edge_id, changed_owner,
                                           stage_index=1, direction="outflow")
    with pytest.raises(K0Error, match="request owner bits differ"):
        check_two_stage_domain_cfl(
            package, one_stage(0, diag0, request0),
            one_stage(1, diag1, request1, current_owner=changed_owner), 0.001,
            stage0_boundary_diagnostics=(diag0,), stage1_boundary_diagnostics=(diag1,),
        )
    with pytest.raises(K0Error, match="cached diagnostic input identity"):
        check_two_stage_domain_cfl(
            package, one_stage(0, diag0, request0),
            one_stage(1, diag1, owner_request, current_owner=changed_owner), 0.001,
            stage0_boundary_diagnostics=(diag0,), stage1_boundary_diagnostics=(diag1,),
        )

    opposite_owner = K0State(1.0, 0.0, 0.2)
    inflow_request = prepare_p2_edge_request(
        package, binding, edge_id, opposite_owner, stage_index=1, direction="inflow",
        inflow_tangential_velocity_m_s=0.0,
    )
    with pytest.raises(K0Error, match="cached diagnostic input identity"):
        check_two_stage_domain_cfl(
            package, one_stage(0, diag0, request0),
            one_stage(1, diag1, inflow_request, current_owner=opposite_owner), 0.001,
            stage0_boundary_diagnostics=(diag0,), stage1_boundary_diagnostics=(diag1,),
        )

    resource_request = prepare_p2_edge_request(
        package, binding, edge_id, owner, stage_index=0, direction="outflow", max_bisections=64,
    )
    with pytest.raises(K0Error, match="cached diagnostic input identity"):
        check_two_stage_domain_cfl(
            package, one_stage(0, diag0, resource_request),
            one_stage(1, diag1, request1), 0.001,
            stage0_boundary_diagnostics=(diag0,), stage1_boundary_diagnostics=(diag1,),
        )

    with pytest.raises(K0Error, match="diagnostic stage_index does not match"):
        check_two_stage_domain_cfl(
            package, one_stage(0, diag0, request0),
            one_stage(1, diag0, request0), 0.001,
            stage0_boundary_diagnostics=(diag0,), stage1_boundary_diagnostics=(diag0,),
        )
    with pytest.raises(K0Error, match="D0_BREAKPOINT_CROSSED"):
        check_two_stage_domain_cfl(package, one_stage(0, diag0, request0), one_stage(1, diag1, request1), 0.001,
                                  remaining_to_breakpoint_s=0.0005,
                                  stage0_boundary_diagnostics=(diag0,), stage1_boundary_diagnostics=(diag1,))


def test_new_kernel_against_frozen_r0_r1_and_three_r3_root_references():
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "t004_k0_root_references.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["format"] == "t004-k0-independent-root-regressions-v1"
    assert len(fixture["cases"]) == 6
    cases = [
        (row["case_id"], float(row["owner_depth_m"]), float(row["owner_normal_velocity_m_s"]),
         float(row["q_n_m2_s"]), row["direction"],
         tuple(map(Fraction, row["reference_c_g_interval_m_s"])),
         Fraction(row["reference_representation_allowance_m_s"]))
        for row in fixture["cases"]
    ]
    for case_id, h, un, q, direction, old, allowance in cases:
        _, _, _, _, request = _request(h, un, abs(q), direction,
                                       tangent=0.0 if direction == "inflow" else None)
        result = diagnose_single_edge(request)
        low, high = result.root.c_g_interval_m_s
        gap = max(Fraction(0), old[0] - high, low - old[1])
        assert gap <= allowance, case_id
        assert result.uncertainty.actual_abs_q_residual_m2_s <= result.uncertainty.tau_edge_m2_s
