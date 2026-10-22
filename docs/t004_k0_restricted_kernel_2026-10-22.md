# T004-K0 restricted single-edge diagnostic

This document specifies the isolated typed Python API in `hydrodynamics.t004_k0`. K0 certifies one physical exterior edge's fixed-direction discharge ghost, reports its actual binary64 HLL vector flux, and checks supplied triangle states against a per-edge CFL limit. It does not route a production RHS, advance a state, write a run result, or expose a solver command. A JSON run schema is not added because this release has a Python API only.

## Frozen physical scope

- Water is fully wet. Owner depth is **0.8 to 1.0 m inclusive**.
- Gravity is the fixed binary64 value **9.81 m/s²**. It is not an API parameter.
- The owner outward normal velocity must be nonzero, strictly match the declared direction, satisfy **|u_n| ≤ 1.2 m/s**, and satisfy **|u_n| < sqrt(g h)**. The adapter and diagnostic entry both enforce this condition; the trace, rounded ghost and actual HLL flux also have independent strict direction checks.
- This closure batch requires zero owner tangential velocity and explicit zero inflow tangential velocity. General tangential closure needs a later design decision; the synthetic HLL diagnostic can still exercise all vector components.
- The signed edge request must be nonzero and satisfy **|q_n| ≤ 1.2 m²/s**. Inflow uses negative outward q; outflow uses positive outward q.
- Bed elevation is read from the validated terrain sample at the owner face. The boundary ghost uses that same bed value. Source terms are absent (`sources=none`).
- The outgoing characteristic is `J+ = u_n + 2 sqrt(g h)`. The ghost follows `u_n,g = J+ - 2 c_g` and `h_g = c_g²/g`.
- The root search uses exact rational interval bounds around the actual binary64 input bits. A normal root needs strict opposite endpoint residual signs and a strictly negative HLL-Q derivative on each branch. An equal-state root uses the exact HLL equal-state identity and the same derivative proof; it is not mislabeled as an endpoint sign change.
- Acceptance requires a root bracket no wider than **0.5 ulp(c_g)** and no more than the requested bisection cap. A root that exists but misses this bound is rejected with `D0_HLL_Q_ROOT_NOT_CERTIFIED` and no accepted certificate.

Each tuple is certified independently. The owner envelope alone does not imply that every requested q has a root. Any input or proof failure closes the request.

## Typed API

All numeric API inputs accept Python `int` or `float` except `bool`, then normalize to binary64. Non-finite values are rejected. Integer controls and IDs require `type(value) is int`; `bool` and integral floats are rejected.

### `K0State`

| Field | Type | Unit | Required/default | Range |
|---|---|---|---|---|
| `h_m` | finite int/float | m | required | `> 0`; when used as owner, K0 additionally requires `[0.8, 1.0]` |
| `u_x_m_s` | finite int/float | m/s | required | finite |
| `u_y_m_s` | finite int/float | m/s | required | finite |

Neighbor and ghost states used only for CFL require positive depth and finite velocity. Owner states are checked against the stricter K0 envelope and subcritical condition.

### `prepare_p2_edge_request`

Required positional inputs are a validated `TriangularBusinessPackage`, the exact `TriangularBoundaryBinding` object registered in that package, a local `edge_index`, and an explicit `K0State` owner. Keyword inputs:

| Field | Type | Unit | Required/default | Range or rule |
|---|---|---|---|---|
| `stage_index` | strict int | 1 | required | exactly `0` or `1`; stage 0 depth bits must match the linked initial condition |
| `direction` | literal string | — | required | `inflow` or `outflow`; determines the sign of q |
| `inflow_tangential_velocity_m_s` | finite int/float or `None` | m/s | required for inflow; otherwise `None` | exactly `0.0` for this batch; outflow takes the owner's zero tangent and rejects an override |
| `max_bisections` | strict int | subdivisions | default `128` | `[1, 512]` |

The binding must be a constant, nonzero total-group discharge in `m3/s`, contain exactly one actual exterior edge, and be present in the validated package. The adapter derives exact signed `q_n = ±Q/L` from the binding's binary64 Q and the mesh edge length. Multi-edge groups are outside this single-edge release. It rechecks the mesh ID, topology fingerprint, group membership, edge ID, sorted node pair, owner face/local stable face ID, outward normal, length, and authenticated security-exclusion edge IDs. Physical security-exclusion edges stay walls even when another boundary-group name aliases their node pair. Unbound exterior edges also stay closed walls.

The adapter rejects internal/constraint edges, unknown or aliased security edges, partial protected-edge overlap, unmatched package bindings, missing/invalid terrain, invalid owner depth/velocity, a zero q, a wrong q sign, and unsupported boundary modes.

### `diagnose_single_edge`

Accepts one `K0BoundaryRequest` returned by the P2 adapter. It returns a `K0BoundaryDiagnostic` containing:

- actual edge/owner identity, owner normal velocity and discharge, and an outward interval for `J+`;
- the continuous characteristic-trace diagnostic, kept separate from the HLL-Q ghost;
- an accepted `K0RootCertificate` with proof type, celerity interval, derivative bounds `m_F`/`M_F`, half-ulp target and actual bisection count;
- the binary64 ghost used for the HLL call and actual outward HLL mass, normal momentum, tangent momentum, x/y momentum, and wave speeds;
- exact rational absolute `E_root`, `E_ghost`, `E_eval`, and edge tolerance `tau_edge = E_root + E_ghost + E_eval`. `E_input` and `E_q` are zero here because the typed API has no decimal-to-binary conversion and retains the exact binary64 total Q. It reports both signed total Q and actual group discharge, plus `tau_group = L * tau_edge`; cancellation is impossible in the single-edge K0 group. The actual edge and group residuals must be within their budgets, and the HLL mass-flux sign must strictly match the declared direction;
- input and calculation fingerprints. Root, uncertainty, flux, and CFL consumers carry the same calculation fingerprint, which includes the same owner/q input fingerprint, certified root interval, binary64 celerity, constructed ghost and actual HLL values.

The vector HLL flux is computed from `[h, h u_n, h u_t]` and rotated back to global x/y momentum components using the actual edge basis. The requested q remains separate from the actual HLL flux; K0 never replaces the computed flux with q. A continuous trace root, if certified, is labeled separately and its HLL mass flux is reported as a diagnostic.

The result flags are always `boundary_applied=False`, `solver_completed=False`, and `result_artifacts_written=False`.

`root_precision_gate` is also callable for admission diagnostics. Its required `root_exists_bracketed` and `strict_endpoint_signs` flags, and optional `equal_state_identity=False`, require strict bool values. Required `bracket_width_m_s` and `target_width_m_s` are exact `Fraction` values in m/s, respectively non-negative and positive. Required `steps_used` is a strict int in `[0, 512]`; required `max_bisections` is a strict int in `[1, 512]`. Admission requires a proven bracket, an explicit sign or identity proof, width within target, and steps within cap. Invalid types raise `D0_POLICY_INPUT_INVALID`; failed admission returns `D0_HLL_Q_ROOT_NOT_CERTIFIED` with no certificate. This gate checks proof metadata; it does not independently establish the mathematical proof supplied to it.

### `diagnose_synthetic_hll_interface`

Inputs are two `K0State` values, finite normal x/y components, and `direction` (`inflow` or `outflow`). The normal must be unit length within `1e-12`. It returns the full vector HLL flux and whether its sign matches the declaration, with provenance `SYNTHETIC_INTERFACE`. It does not certify a P2 edge or a Q root and never applies a boundary. It exists for local Riemann sign regressions outside the K0 owner envelope.

### `check_two_stage_domain_cfl`

Required inputs are the validated package, complete `K0StageDomainInput` records for stage 0 and stage 1, and positive finite `dt_s`. `cfl_number` defaults to `0.25`, dimensionless, and K0 freezes it at exactly `0.25`.

| Record field | Type | Unit | Required/default | Range or rule |
|---|---|---|---|---|
| `K0StageDomainInput.stage_index` | strict int | 1 | required | exactly `0` for the first record and `1` for the second |
| `K0StageDomainInput.cells` | sequence of `K0CellStageInput` | — | required | exactly one item for every actual mesh face ID; no duplicates |
| `K0CellStageInput.face_id` | strict int | dimensionless | required | actual stable mesh face ID |
| `K0CellStageInput.owner_state` | `K0State` | state units above | required | this cell's state at this stage |
| `K0CellStageInput.edges` | sequence of `K0CflEdgeInput` | — | required | exactly three distinct actual edges belonging to `face_id` |
| `K0CflEdgeInput.edge_index` | strict int | dimensionless | required | actual local mesh edge index belonging to the cell |
| `K0CflEdgeInput.right_state` | `K0State` | state units above | required | actual neighbor state or explicit ghost for this edge |
| `K0CflEdgeInput.neighbor_face_id` | strict int or `None` | dimensionless | default `None` | required for internal edges and must be the actual neighbor; forbidden for exterior edges |
| `K0CflEdgeInput.ghost_state_id` | non-empty string or `None` | — | default `None` | required for exterior edges; forbidden for internal edges |
| `K0CflEdgeInput.boundary_request` | `K0BoundaryRequest` or `None` | — | default `None` | required on bound discharge edges; it must come from the current package/binding and match stage, owner, direction, Q and derived q; forbidden on internal and unbound wall edges |
| `dt_s` | finite int/float | s | required | strictly greater than zero |
| `remaining_to_breakpoint_s` | finite int/float or `None` | s | default `None` (no breakpoint declared) | when supplied, strictly positive and at least `dt_s` |
| `cfl_number` | finite int/float | 1 | default `0.25` | exactly `0.25` in this release |

Each stage contains exactly one `K0CellStageInput` for every stable mesh face ID. A cell has exactly three `K0CflEdgeInput` records, one for each actual mesh edge. Internal edges must name their actual neighbor's stable face ID and their right-state bits must equal that neighbor's stage owner state. Exterior edges need an explicit ghost ID: bound discharge edges must use that stage's certified K0 ghost and its original typed request. CFL recomputes request identity from the live package, binding, Q, edge length, owner stage, terrain and linked initial sample. It rejects cached diagnostics when those inputs changed, even when their internal calculation fingerprint remains self-consistent. Every unbound exterior edge, including security edges, must use the reflected closed-wall state. Missing, duplicated, mismatched, non-finite, or wrong-stage identities fail closed.

For each edge, K0 computes and returns an outward upper bound for `alpha = max(|u_n,L| + sqrt(g h_L), |u_n,R| + sqrt(g h_R))`. It then computes `dt_K = CFL * A_K / sum_e(L_e alpha_e)` for every cell and stage, using each edge's own right state and the actual mesh area/length. The result includes per-edge alpha bounds, per-cell limits, stage minima, the limiting stage/face, and the domain minimum over both stages. No state is advanced. `K0CflResult.state_advanced` is always `False`.

## Error codes

| Code | Meaning |
|---|---|
| `D0_POLICY_INPUT_INVALID` | Wrong type, missing identity, invalid scalar, or unsupported API option |
| `D0_POLICY_INPUT_STATE_MISMATCH` | Owner, neighbor, ghost, stage, q, or consumer fingerprint differs |
| `D0_HLL_Q_OWNER_DIRECTION_INVALID` | Owner normal velocity is zero or opposes the fixed requested direction |
| `D0_HLL_Q_TRACE_DIRECTION_INVALID` | Certified continuous trace has zero or wrong-direction normal velocity |
| `D0_HLL_Q_GHOST_DIRECTION_INVALID` | Constructed binary64 ghost has zero or wrong-direction normal velocity |
| `D0_P2_EDGE_IDENTITY_INVALID` | Selected edge/binding/mesh/owner/neighbor identity is not an actual P2 exterior identity |
| `D0_SECURITY_EXCLUSION_EDGE_FORBIDDEN` | Physical edge is authenticated as a security-exclusion closed wall |
| `D0_K0_SINGLE_EDGE_GROUP_REQUIRED` | Discharge binding has more than one actual edge |
| `D0_HLL_Q_INPUT_DIRECTION_INVALID` | Zero q or requested direction does not match the signed q |
| `D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED` | Owner is outside the fixed wet/subcritical envelope or terrain is missing |
| `D0_HLL_Q_ROOT_NOT_CERTIFIED` | No certifiable root, derivative/denominator proof failure, ambiguous signs, resource exhaustion, or precision target unmet |
| `BOUNDARY_ACTUAL_HLL_DIRECTION_MISMATCH` | Actual binary64 HLL mass flux is zero or has the wrong strict sign |
| `BOUNDARY_HLL_Q_RESIDUAL_EXCEEDED` | Actual HLL q residual exceeds the independently accumulated error budget |
| `D0_TRIANGLE_CFL_INVALID` | Non-positive cell CFL denominator |
| `D0_BREAKPOINT_CROSSED` | Requested dt exceeds the supplied remaining time to a breakpoint |

## Example and checks

Run the synthetic example from the repository root with:

```powershell
python examples/triangular_boundaries/t004_k0_single_edge_demo.py
```

It prints a single-edge diagnostic and no run output. Focused tests live in `tests/test_t004_k0.py`; the independent R4 policy acceptance tests remain separate. K0 tests cover strict integer and boolean types, equal and non-equal root proofs, the R3 cap-1 and no-root failure cases, actual-versus-continuous-trace flux, the two legacy synthetic direction counterexamples, physical security-edge aliasing, actual-neighbor CFL identities, and two-stage state immutability.

## Deliberate limits

This is an isolated kernel/API acceptance artifact. It does not implement a production RHS, time integration, dynamic outlet, full D0, CLI, result writer, or solver run. It does not claim domain-wide root existence or hydrodynamic accuracy. No DEM, database, engineering case, historical recovery or restart was used.
