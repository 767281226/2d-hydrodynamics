# P4-3A Triangular Open-Boundary Contract ADR

Batch: `P4-3A-BOUNDARY-CONTRACT-20261018`
Status: **Preparation contract and analytic references are implemented and pending main-chat review. No RHS or time integration is connected.**
Scope: Preserve P3-A source semantics and define a strict, reviewable preparation layer for future open boundaries. This ADR does not claim that any open boundary has been validated in the solver.

Document role: This is an **English reading translation**, not an independent or authoritative ADR. The sole canonical decision record is the [Chinese ADR](p4_3a_triangular_boundary_contract_adr.md). Both versions are in preparation/candidate status pending main-chat review.

## Decision summary

P3-A boundary values remain source requests. `discharge` is a non-negative total group discharge `Q` in m³/s and contains no direction. `water_level` is an absolute elevation in metres and its vertical datum must exactly match the known datum on the P2 terrain. The new `triangular-boundary-preparation-v1` contract requires an explicit mapping for every bound group, a fixed direction for discharge only, a uniform-per-unit-length allocation policy for discharge only, and a piecewise-constant temporal policy for every time series. Unbound exterior edges are explicitly recorded as `closed_wall`.

The preparation layer derives outward normals, edge lengths, length weights, request allocations, source breakpoints and matched output times from P2/P3-A inputs. The sign convention is outward-positive: inflow requests are negative and outflow requests are positive. Every output is marked `preparation_only=true`, `simulation_result=false`, `solver_completed=false`, `execution_available=false`, and `boundary_applied=false`. Prepared requests do not enter HLL, change a conserved state, or represent the mass flux accepted by a solver.

P4-3B must not feed a P3-A Q or stage request by itself into HLL until a complete exterior-state closure contract and tests exist. The recommended first execution scope is a fully defined, fully wet, fixed-direction boundary. Wet/dry transitions, reversal, supercritical inflow, and stage below bed must be explicitly rejected until later work defines and validates separate physical rules for those branches.

## Existing inputs and sign convention

Each P2 edge `e` is identified from its nodes, adjacent face and exterior-boundary identity. For a true exterior edge, let `m_e` be the edge midpoint, `c_e` the centroid of its unique adjacent face, and `n_e*` either unit normal candidate derived from an endpoint ordering. If `n_e* · (m_e-c_e) < 0`, reverse the candidate. The normal therefore follows actual P2 geometry and adjacency instead of stored node-pair order. This check applies only when `edge_faces` contains one valid face and the other entry is the boundary marker `-1`.

For group length `L = Σ_e L_e` and non-negative request `Q(t)`, the frozen **candidate preparation allocation** is:

```text
w_e = L_e / L
Q_e(t) = s · Q(t) · w_e
q_n,e(t) = Q_e(t) / L_e = s · Q(t) / L
s = -1 for direction=inflow; +1 for direction=outflow
```

`Q_e` is the signed outward group-request share in m³/s. `q_n,e` is the requested normal discharge per unit width in m²/s. Tests check geometric length weights and this algebraic allocation; they do not check or assume an HLL numerical flux.

## Evidence and project choices

### Conclusions directly supported by sources

* The USACE HEC-RAS 2D manual documents Flow Hydrograph and Stage Hydrograph as different boundary types. Its flow hydrograph uses positive values for inflow and negative values for outflow. Stage hydrographs may cause inflow or outflow per boundary cell according to the relative water levels; a dry cell may not exchange water when the boundary stage is below its minimum ground elevation. The manual also prohibits attaching different external boundary types to the same cell face. [HEC-RAS 2D External Boundary Conditions](https://www.hec.usace.army.mil/confluence/rasdocs/r2dum/latest/boundary-and-initial-conditions-for-2d-flow-areas/external-boundary-conditions)
* The Deltares D-Flow FM technical manual distinguishes water level, normal velocity, group-integrated discharge, Riemann/outflow conditions, and supplementary tangential velocity. It gives the discharge integral `∫ h u·n dΓ = Q_b` and separately gives `u·t = v_b`. Those formulas use Deltares's inward-positive normal convention. This project’s outward-positive convention is an explicit project choice and does not copy Deltares signs. [D-Flow FM Technical Reference Manual, §6.4](https://content.oss.deltares.nl/delft3dfm2d3d/D-Flow_FM_Technical_Reference_Manual.pdf)
* Antonopoulos and Dougalis analyze characteristic boundaries for one-dimensional shallow-water equations, including characteristic speeds `u ± √(g h)` and Riemann-invariant forms. Their paper concerns a one-dimensional nondimensional model under particular assumptions. It explains why incoming information differs by subcritical/supercritical regime, but does not validate a two-dimensional finite-volume HLL boundary algorithm. [Notes on Galerkin-finite element methods for the Shallow Water equations with characteristic boundary conditions](https://arxiv.org/abs/1507.08209)

### Project choices, not validation conclusions from external software

* Do not change P3-A numeric values, units, source labels, quantity meanings, or native boundary-group identities. Never infer discharge direction from a group name, interior state, omitted configuration field, or case documentation.
* Outward-positive signs, `uniform_per_unit_length`, mapping to actual P2 exterior edges, explicit closed walls for unbound edges, strict time matching, and preparation-only flags are project input-contract choices. They are not HEC-RAS or D-Flow FM flux algorithms.
* This batch does not assign velocity, tangential momentum, direction, or mass flux to a `water_level` request, and does not infer wet/dry transitions from stage.

## Why Q and absolute stage do not form a complete HLL exterior state

A two-dimensional shallow-water boundary state needs at least `h`, `h u_n`, and `h u_t`. Given a known boundary bed `z_b`, absolute stage `ζ_b` can at most supply a candidate depth `h_b=ζ_b-z_b`; it does not supply normal or tangential velocity. If `h_b<=0`, the wet state is not defined. Group discharge constrains only an integral over the edge group, `∫ h u_n dΓ`. Even with this batch’s fixed allocation giving `q_n,e`, a depth and tangential velocity are still missing to evaluate `u_n=q_n/h`. One scalar does not complete `(h, hu, hv)`.

Future boundary-state closure must select and record a complete method. Candidates are:

1. **Explicit full exterior state:** supply datum-consistent absolute stage/depth, normal velocity or Q allocation, and tangential velocity for each edge or regularized edge segment. With outward normal `n=(n_x,n_y)` and tangent `t=(-n_y,n_x)`, rotate the momenta back using `hu=h(u_n n_x+u_t t_x)` and `hv=h(u_n n_y+u_t t_y)`.
2. **A validated characteristic/relational closure:** for example, combine stage with one incoming characteristic quantity and state which characteristic is taken from the interior. The method must distinguish two-dimensional behavior, local normal/tangential components, bed source terms, wet/dry conditions, and supercritical direction. A one-dimensional relation cannot be treated as a two-dimensional theorem.

Current P3-A stores only one quantity, discharge or water level, per group. If a complete state is selected later, a separately reviewed, versioned supplemental contract must associate the complementary state data with the original P3-A fields. Historical P3-A packages must not be rewritten, and this batch must not silently fabricate a missing stage or tangential-velocity default.

## Future interface with HLL, hydrostatic reconstruction, and momentum source terms

P4-0 selected triangular finite-volume `HLL`, hydrostatic reconstruction, and `SSPRK2`, but the current route supports only closed walls. A future open edge must use the same conservative edge-normal flux convention: rotate interior and exterior momenta into local normal/tangential coordinates; retain the hydrostatic reconstruction and bed treatment used on internal edges; compute one HLL mass/momentum flux from the reconstructed states; then rotate momentum flux back to global coordinates. Open-edge bed pressure and terrain source terms must be designed together with the existing well-balanced treatment. A closed wall must remain consistent with a reflected normal-momentum state, zero normal mass exchange, and consistent pressure terms. A stage-only input, unconditional zero exterior momentum, post-flux clipping of mass, or bypass of hydrostatic reconstruction is not an implementation of this contract.

Prepared request `Q_e` / `q_n,e` and the HLL exterior flux `F^mass_e` eventually accepted by the solver are different quantities. The result's boundary mass ledger must come from the numerical fluxes actually accepted in both RK stages and their time weights. With positive volume defined into the domain, `boundary_net_volume_m3 = -Σ_e∫ F^mass_e L_e dt`. A report may show the request, actual accepted exchange, and their difference side by side, but must not overwrite the actual flux with requested Q to hide a mismatch.

## Future execution support table: preparation capability and rejection rules

“Preparation” below means the current P4-3A implementation. “P4-3B entry rule” is a proposed execution gate, not an observed solver result.

| Case | P4-3A preparation | Proposed P4-3B entry rule |
| --- | --- | --- |
| Non-negative group Q plus explicit fixed inflow/outflow | Accept and allocate signed requests by edge length | Reject execution until full state closure; initially review only fully wet, declared-direction, subcritical cases |
| `Q=0` stop sample | Accept as a zero request; do not integrate beyond the final endpoint | Zero request alone does not prove zero actual HLL flux; reject or roll back without a matching exterior state/stop rule |
| Direction reversal within one group | P3-A Q is unsigned; preparation direction is fixed per group | No implicit reversal; design an explicit signed series/switch rule and reversal characteristic state in a separate version |
| Reversal against a fixed direction | Do not classify or alter the P3-A request | If either RK stage has actual outward flux against the fixed declaration, reject and roll back until a bidirectional rule is validated |
| Fully wet stage above bed | Preserve absolute stage and source datum; do not derive full exterior state | Stage still requires normal/tangential closure; reject execution without it |
| Stage below any actual edge bed | Preserve input; do not clip to zero or rewrite it | Reject initial scope; separately design a dry boundary, wetting threshold, and mass-ledger tests |
| Partly wet, partly dry, or boundary wet/dry transition | Do not classify or redistribute group Q | Reject initial scope; do not reallocate Q over only wet edges or fill undefined velocities |
| Fully dry outlet / inflow into a dry domain | Preserve request without claiming it produces flow | Reject initial scope until dry-outlet characteristics, wet/dry retry, non-negativity, and conservation behavior are validated |
| Subcritical | No regime classification | Initially allow only fully wet, fixed-direction cases with a complete exterior-state closure; still require two-stage CFL and flux tests |
| Supercritical inflow | No regime classification | Reject unless all incoming characteristic/full-state data are supplied and separately tested |
| Supercritical outflow | No regime classification | Reject initial scope; later permit only with an explicitly justified pure extrapolation/outflow closure |

Characteristic speeds and incoming-information counts in the one-dimensional reference depend on direction and regime. The proposed two-dimensional initial scope is therefore a project safeguard, not an extrapolation of that paper as a two-dimensional proof. HEC-RAS dry-stage behavior and D-Flow FM discharge-depth/drying logic are their respective implementations and are not adopted as defaults here.

## Time discretization, stability, and retry ledger

Every time-series breakpoint must match one unique output time within the shared `max(1e-12 s, 8 ULP)` tolerance. Preserve both source `raw_time_s` and matched `effective_time_s`; do not interpolate, extrapolate, or add breakpoints. Intervals are left-closed/right-open. Store the final value as a terminal sample, with no extra integrated duration. If a future SSPRK2 step ends exactly at an internal breakpoint, its second stage still uses the old interval's left-side value; the new interval starts on the next step.

Future P4-3B must include actual boundary HLL speeds in the stable-step upper bound and check the state and boundary speeds in both SSPRK2 stages. A candidate cell restriction is `Δt <= C_CFL · min_K A_K / Σ_{e∈∂K}(L_e α_e)`, where `α_e` must cover the largest normal wave speed of the two HLL reconstructed states. The Deltares manual also ties time-step estimates to actual fluxes/velocities, but this candidate formula still requires validation against this project's HLL, triangular cell areas, and SSPRK2 implementation. Boundary breakpoints must continue to limit the current `dt`; a step cannot cross a breakpoint.

If a stage state, boundary speed, negative depth, or per-step mass condition fails, the future executor must reject the provisional update and retry with a smaller `dt`. Source ledger, `boundary_net`, and correction volume from trial stages must roll back with the state. Exhausted retries stop through the existing failure path; no half-stage flux may remain and mass correction may not conceal a boundary mismatch.

## C source terms and the mass-correction budget

Keep the fixed initial correction budget based on P4-2C `V0` and its original scale. Do not recalculate it from P4-3A requests, preparation integrals, or future boundary exchange. A future source-aware boundary configuration needs a separately designed version combination: `source_net_volume_m3` continues to represent source terms only, while `boundary_net_volume_m3` continues to represent only accepted boundary flux. Do not add boundary volume to initial `V0`, rainfall totals, or the source ledger to offset boundary error. Existing none/Manning/C source runs do not become open-boundary runs merely because a preparation artifact exists.

## Preparation interface and artifact

Module `hydrodynamics.triangular_boundaries` provides `load_triangular_boundary_preparation_config`, `prepare_triangular_boundaries`, and `read_triangular_boundary_preparation`. The command is `hydrodynamics-triangular-boundary-prepare CONFIG.yaml`.

The artifact references the config, P3-A business package, actual linked P2 UGRID, mesh/terrain fingerprints, and SHA-256 digests. It lists all actual P2 exterior edges, edges assigned to request groups, and unbound edges explicitly retained as walls. Reading the preparation JSON reloads every input, rebuilds the entire expected artifact, and compares all fields; a self-reported hash is not proof. Output publication uses a same-directory temporary file, readback validation, and atomic publication. Replacement is disabled by default; explicit overwrite can replace only an earlier boundary-preparation JSON, never a config, P2/P3-A package, or unrelated existing file.

Every P3-A group must map one-to-one to a boundary choice. Unknown or missing groups, extra groups, internal constraint groups, P3-A/P2 mismatch, missing or mismatched datum, overlapping edge assignment, time gaps, out-of-range or unmatched breakpoints, and invalid output time axes are rejected. Every unbound P2 exterior edge is listed in `unbound_exterior_edges` and remains a wall.

## Independent synthetic references and tolerances

The machine-readable catalog [`boundary_preparation_references.json`](../examples/triangular_boundaries/boundary_preparation_references.json) is marked `pending_main_review`. Its analytic geometry and request-volume cases do not mean open-boundary solver behavior is validated:

* For 1 m and 3 m edges with `Q=0.01 m³/s`, weights are 0.25/0.75, edge shares are 0.0025/0.0075 m³/s, and the magnitude of unit-width outward request is 0.0025 m²/s on both edges. Inflow/outflow signs are opposite.
* On `[0,10,20,30] s`, inflow `Q=[0.01,0,0.02,0.02] m³/s` integrates to 0.1, 0, and 0.2 m³ (0.3 m³ total). Outflow at `0.005 m³/s` integrates to 0.15 m³, giving 0.15 m³ net requested inflow. The final sample is terminal and is not integrated again.
* Rotation of the same geometry and reordering face/node/endpoint identities leave lengths, weights, and Q shares invariant; normals follow the rigid rotation.
* With the same explicit synthetic datum, a flat bed at 0 m and absolute stage 1 m have an analytic hydrostatic expectation `h=1 m, q=0` and zero exchange. P4-3A only stores stage requests; it does not compute or pass this hydraulic state.

Absolute error for the sum of edge requests is bounded by `1e-12*max(1,|Q|) m³/s`; unit-normal norm error is `1e-12`; independent interval/cumulative request-volume error is `1e-12 m³`; field equivalence is `1e-10*max(1,|reference|)`. These are preparation and analytic-arithmetic thresholds only. P4-3B needs separate actual-HLL mass-flux cases and reviewed thresholds.

## Source list

1. USACE, *HEC-RAS 2D User's Manual*, External Boundary Conditions (current online manual). Cited for the distinction between flow/stage, flow sign, per-cell behavior, dry-stage behavior, and no multiple external conditions on one cell face. This describes HEC-RAS behavior, not project validation. [Official manual](https://www.hec.usace.army.mil/confluence/rasdocs/r2dum/latest/boundary-and-initial-conditions-for-2d-flow-areas/external-boundary-conditions)
2. Deltares, *D-Flow Flexible Mesh Technical Reference Manual*, §6.4, especially equations 6.147–6.158. Cited for the discharge integral and the distinction among water-level, normal-velocity, and tangential-velocity inputs. Its inward-positive normal and solver algorithms are not imported as project conventions. [Official PDF](https://content.oss.deltares.nl/delft3dfm2d3d/D-Flow_FM_Technical_Reference_Manual.pdf)
3. Antonopoulos, D. C. & Dougalis, V. A., *Notes on Galerkin-finite element methods for the Shallow Water equations with characteristic boundary conditions* (2015), §§1, 3, and 4. Cited only for one-dimensional characteristic speeds/invariants and regime context, not for two-dimensional FV/HLL boundary validation. [arXiv:1507.08209](https://arxiv.org/abs/1507.08209)
