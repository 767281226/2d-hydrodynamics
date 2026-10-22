"""Diagnostic-only example for the bounded T004-K0 single-edge API."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hydrodynamics.t004_k0 import K0State, diagnose_single_edge, prepare_p2_edge_request
from hydrodynamics.triangular_business import (
    TriangularBoundaryBinding,
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    topology_fingerprint,
)
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh


def main() -> None:
    mesh = TriangularMesh(
        ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        ((0, 1, 2),),
        "EPSG:4548",
        mesh_id="t004-k0-synthetic-flat-triangle",
        face_id=(42001,),
        boundary_groups={"synthetic_outflow": ((0, 1),)},
    )
    terrain = FaceTerrainField(
        mesh.mesh_id, (0.0,), (True,), (0.5,), (0.5,), (1.0,), "EPSG:4548"
    )
    roughness = TriangularRoughnessField.from_constant(mesh, 0.03, source="synthetic example")
    initial = TriangularInitialConditionField.from_constant(
        mesh, terrain, "depth", 1.0, source="synthetic example"
    )
    edge_id = mesh.boundary_groups["synthetic_outflow"][0]
    binding = TriangularBoundaryBinding(
        mesh_id=mesh.mesh_id,
        topology_fingerprint=topology_fingerprint(mesh),
        group_name="synthetic_outflow",
        quantity="discharge",
        mode="constant",
        edge_ids=(edge_id,),
        edge_nodes=(mesh.edge_nodes[edge_id],),
        values=(0.2,),
        time_s=None,
        unit="m3/s",
        interpretation="total_group_discharge",
        source="synthetic example",
    )
    package = TriangularBusinessPackage(mesh, terrain, roughness, initial, (binding,))

    request = prepare_p2_edge_request(
        package,
        binding,
        edge_id,
        K0State(h_m=1.0, u_x_m_s=0.0, u_y_m_s=-0.2),
        stage_index=0,
        direction="outflow",
        max_bisections=128,
    )
    diagnostic = diagnose_single_edge(request)
    root_lo, root_hi = diagnostic.root.c_g_interval_m_s
    report = {
        "scope": "diagnostic_only_T004_K0",
        "edge_id": diagnostic.edge.edge_index,
        "node_pair": diagnostic.edge.node_pair,
        "owner_face_id": diagnostic.edge.owner_face_id,
        "outward_normal": [diagnostic.edge.normal_x, diagnostic.edge.normal_y],
        "requested_q_m2_s": float(diagnostic.q_requested_m2_s),
        "requested_group_discharge_signed_m3_s": float(diagnostic.group_discharge_requested_signed_m3_s),
        "actual_group_discharge_signed_m3_s": float(diagnostic.group_discharge_actual_signed_m3_s),
        "root_proof": diagnostic.root.proof_type,
        "c_g_m_s_interval_approx": [format(float(root_lo), ".17g"), format(float(root_hi), ".17g")],
        "root_width_m_s_approx": format(float(root_hi - root_lo), ".6e"),
        "half_ulp_target_m_s_approx": format(float(diagnostic.root.target_half_ulp_m_s), ".6e"),
        "ghost_depth_m": diagnostic.ghost_state.h_m,
        "ghost_velocity_m_s": [diagnostic.ghost_state.u_x_m_s, diagnostic.ghost_state.u_y_m_s],
        "actual_hll_flux": {
            "mass_m2_s": diagnostic.hll_flux.mass_m2_s,
            "momentum_x_m3_s2": diagnostic.hll_flux.momentum_x_m3_s2,
            "momentum_y_m3_s2": diagnostic.hll_flux.momentum_y_m3_s2,
        },
        "absolute_q_residual_m2_s": format(float(diagnostic.uncertainty.actual_abs_q_residual_m2_s), ".6e"),
        "tau_edge_m2_s": format(float(diagnostic.uncertainty.tau_edge_m2_s), ".6e"),
        "group_residual_m3_s": format(float(diagnostic.uncertainty.actual_abs_group_residual_m3_s), ".6e"),
        "tau_group_m3_s": format(float(diagnostic.uncertainty.tau_group_m3_s), ".6e"),
        "input_fingerprint": diagnostic.input_fingerprint,
        "calculation_fingerprint": diagnostic.calculation_fingerprint,
        "boundary_applied": diagnostic.boundary_applied,
        "solver_completed": diagnostic.solver_completed,
        "result_artifacts_written": diagnostic.result_artifacts_written,
        "sources": "none",
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
