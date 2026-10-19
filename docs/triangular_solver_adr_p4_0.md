# ADR P4-0: experimental triangular shallow-water route

**Status:** Independently accepted on 2026-10-17 after R3 contract review;
frozen for P4-1 implementation. The user selected route B on 2026-10-16.
This ADR makes that selection concrete; it does not state that a solver
exists or has passed the numerical tests. See `p4_0_g1_acceptance_2026-10-17.md`
and `p4_1_closed_core_work_order.md` for acceptance scope and the next batch.

## Decision and scope

The future experimental solver uses the depth-averaged two-dimensional
Saint-Venant equations on the validated P2 triangular mesh. A face stores the
area average of the conserved vector

`U = (h, hu, hv)`, with units `m`, `m2/s`, and `m2/s`. The fixed bed elevation
`z_b` uses metres, gravity `g` uses `m/s2`, projected face area uses `m2`, and
edge length uses `m`. The first solver increment is restricted to complete
P2 terrain, complete P3-A initial depth or same-datum absolute water level,
zero initial momentum, an explicit closed-wall policy on every exterior edge,
and `sources=none`.

This increment has no discharge or water-level boundary bindings, gate
hydraulics, Manning friction, rainfall, or spatially partial terrain/initial
fields. The capability checker rejects a linked P3-A package with boundary
bindings; it does not discard them or reinterpret them as walls. A run request
must explicitly declare `boundary_policy: closed_wall`, `initial_momentum:
zero`, and `sources: none`. Roughness values remain in linked-input provenance
but are not applied.

A P3-A depth field is copied face-for-face as `h`. A P3-A absolute-level field
is converted to depth by `h_i=max(eta_i-z_{b,i},0)` after the existing same-datum
validation; cells below terrain are explicitly dry. No spatial interpolation,
missing-value fill, or velocity inference is performed. Since the business
package has no initial momentum field in this increment, nonzero initial
momentum cannot be supplied through a side channel.

The P4-0 code supplies only strict run/state/result contracts and an input
capability check. It has no time-advance function, flux implementation, or
result writer. The result contract requires an experimental marker and
`production_approved: false`. P4-1 must implement and validate the numerical
core before any result can be claimed.

## Frozen spatial route

The cell-centred first-order finite-volume state is stored in P2 `face_id`
order. Terrain is represented as a piecewise-constant bed elevation at each
face. The fixed-bed, source-free equations are

```text
∂t h  + ∇·(h u) = 0
∂t hu + ∇·(hu u + 1/2 g h² I) = -g h ∇z_b
```

The two momentum components are advanced together. There is one numerical
flux per unique mesh edge. For an internal edge, the owner is the incident
face with the smaller stable `face_id`; the unit normal points from that owner
to its neighbor. Boundary normals point out of the incident face. The normal
is derived from the edge coordinates and oriented using face centroids; face
array order and the stored endpoint order do not define its direction. The
owner receives `-L/A_owner` times the integrated flux and the neighbor receives
the exact opposite mass contribution, `+L/A_neighbor`. This single-edge
accounting is the conservation invariant.

For a face state, `u=(hu/h,hv/h)` when wet. At an edge with face bed values
`z_L,z_R`, set `z* = max(z_L,z_R)` and

```text
h*_L = max(0, h_L + z_L - z*)
h*_R = max(0, h_R + z_R - z*)
```

Preserve each wet-side velocity while forming the reconstructed conserved
states `U*_L,U*_R`. The homogeneous normal flux is

```text
F_n(U) = (h u_n,
          h u_n u + 1/2 g h² n_x,
          h u_n v + 1/2 g h² n_y)
```

Use HLL signal estimates

```text
s- = min(0, u_n,L - sqrt(g h*_L), u_n,R - sqrt(g h*_R))
s+ = max(0, u_n,L + sqrt(g h*_L), u_n,R + sqrt(g h*_R))
```

and

```text
F_HLL = (s+ F_n(U*_L) - s- F_n(U*_R)
         + s- s+ (U*_R - U*_L)) / (s+ - s-)
```

with the upwind one-sided flux when both wave speeds have the same sign and a
zero flux when both reconstructed states are completely dry. Hydrostatic
momentum corrections are applied separately to each face side:

```text
F_L = F_HLL + (0, 1/2 g (h_L² - h*_L²) n_x,
                  1/2 g (h_L² - h*_L²) n_y)
```

The neighbor uses the reversed normal and its own correction. The correction
has no mass component. Lake-at-rest preservation is an acceptance property of
this combined implementation, not an assumption that may replace testing.

At a closed wall, the exterior state reflects the normal momentum and retains
the tangential momentum and water depth. The resulting wall mass flux is zero.
The normal pressure momentum flux `1/2 g h² n` remains present; a wall is not a
zero-flux condition for all three conserved components.

## Time integration and failure policy

Advance with SSPRK2:

```text
U(1)   = U(n) + dt L(U(n))
U(n+1) = 1/2 U(n) + 1/2 [U(1) + dt L(U(1))]
```

The frozen global geometric time-step bound is

```text
a_e = max(|u_n,L| + sqrt(g h_L), |u_n,R| + sqrt(g h_R))
dt_CFL = C_CFL min_i [ A_i / sum_e (L_e a_e) ]
```

The same bound uses the reflected state at a wall. A zero denominator in a
completely stationary dry domain means there is no wave-speed restriction;
the selected step is then capped by `maximum_time_step_s`, never set to zero.
The candidate step uses the smaller CFL bound at `U(n)` and the SSPRK2 first
stage `U(1)`. After calculating `U(1)`, P4-1 must recompute `dt_CFL(U(1))`
before evaluating the second residual. If the candidate exceeds this bound,
reject it, record a `second_stage_cfl` rejection, and retry with
`min(dt/2, dt_CFL(U(1)))`; rerun both stages from the last accepted state.
Retries stop at `maximum_step_retries`, or with a structured failure if no
legal candidate remains. This second-stage check is part of the frozen route.

Output times must start at 0 s, increase strictly, and have gaps at least
`minimum_time_step_s`. A step is capped to land exactly at the next output.
If the remaining gap is below `minimum_time_step_s`, P4-1 may take one
explicit output-alignment-only step only when its length is no greater than
the freshly recomputed CFL bound. It records that duration in
`subminimum_alignment_step_s` and the checked bound in
`subminimum_alignment_cfl_limit_s` for the interval. A remainder below the minimum
that exceeds the CFL bound fails; it is never silently accepted. The result
records total accepted count, CFL-limited accepted count, rejected count,
CFL-step extrema, all-step extrema and alignment-only remainders. Their sums
must reconcile with the total duration. The all-step minimum may be below the
configured minimum only when the corresponding output-alignment remainder is
explicitly recorded, along with its recomputed CFL upper bound. Let `T_cfl`
be total duration minus all alignment remainders and `D_j` be output interval
`j` minus its alignment remainder. The single comparison tolerance is
`tol_s(x)=max(1e-12 s, 8*ulp(float(x)))`.

For global CFL step count `N`, observed extrema must be realizable: for `N=1`,
`minimum_cfl_step_s = maximum_cfl_step_s = T_cfl` within `tol_s(T_cfl)`; for
`N>=2`,
`(N-1)*min_cfl + max_cfl <= T_cfl <= min_cfl + (N-1)*max_cfl`, with the same
tolerance. For every output interval, compute
`tau_j=tol_s(time_s[j+1]-time_s[j])` from the original output interval,
`n_min_j=max(1, ceil((D_j-tau_j)/max_cfl))` and
`n_max_j=floor((D_j+tau_j)/min_cfl)`. Require
`n_min_j<=n_max_j` for each interval and
`sum(n_min_j)<=N<=sum(n_max_j)`. This rejects step extrema that cannot land
on every requested output time. Capacity and total duration alone still do
not prove that the declared global minimum and maximum actually occur. Unless
`m==M`, the checker reserves each extremum. For interval `i`, let `L_i,H_i`
be its base counts and `SL=sum(L_i), SH=sum(H_i)`:

```text
minimum reservation: Lm_i=max(L_i, 1+ceil((D_i-m-tau_i)/M)), Hm_i=H_i
maximum reservation: LM_i=L_i, HM_i=min(H_i, 1+floor((D_i-M+tau_i)/m))
same-interval pair: Lb_i=max(L_i, 2, 2+ceil((D_i-m-M-tau_i)/M))
                    Hb_i=min(H_i, 2+floor((D_i-m-M+tau_i)/m))
```

A same-interval pair is feasible when `Lb_i<=Hb_i` and
`SL-L_i+Lb_i<=N<=SH-H_i+Hb_i`. Different-interval extrema require distinct
indices `i!=j`, feasible local reservations, and
`SL-L_i+Lm_i<=N<=SH-H_j+HM_j`. The final pair check uses at most the first two
feasible candidate indices in each class, so the joint check is O(k), not a
scan over an unbounded number of internal-step sequences. When `m==M`, the
base interval counts and total duration already determine every CFL step.
The default `C_CFL=0.25` is a conservative project choice
for this first-order route, not a universal stability constant; the maximum
configured value is `0.5`.

The dry-depth threshold only classifies a face and invokes the configured
momentum recovery rule. At `h <= dry_depth_threshold_m`, set `hu=hv=0` and keep
`h` unchanged; this rule does not silently delete water. For negative depth,
`h < -negative_depth_tolerance_m` rejects the stage. A value in
`[-negative_depth_tolerance_m,0)` may be set to zero only when its
area-weighted correction remains within the run's volume-correction budget.
That correction is accumulated in the mass ledger, including the SSPRK2 stage
weight. It is never hidden by unconditional clipping.

If a stage is rejected, apply the retry rule above. Stop with a structured
failure when retries are exhausted, a non-alignment step falls below
`minimum_time_step_s`, the output-alignment rule cannot satisfy both bounds,
or a non-finite state is found. A failed attempt does not enter accepted
history or the successful output budget. The future writer must not publish a
successful result after such a failure.

With walls and `sources=none`, the mass ledger records zero net boundary and
source volume. For each output time it records initial/final water volume,
boundary volume, source volume, positive mass corrections and residual:

```text
residual = final - initial - boundary - sources - corrections
```

## Frozen P4-1 defaults and legal ranges

| Input | Type | Unit | Default / required | Legal range and error |
|---|---|---|---|---|
| `format` | literal string | — | `triangular-solver-run-v1` | other format identifiers are rejected |
| `schema_version` | literal string | — | `1.0` | only `1.0` |
| `case_id` | non-empty string | — | required | blank or non-string is rejected |
| `business_file` | relative path | — | required | P3-A v2 package; output may not overwrite it or its linked mesh |
| `result_file` | relative path | — | required | may not overwrite config, business package or linked mesh |
| `time_origin` | ISO8601 string or null | — | default null | if present, a timezone is required; current time is never inferred |
| `output_times_s` | finite numeric array | s | required | 2–100,000 values, first exactly 0, strictly increasing, each ≤31,536,000 |
| `boundary_policy` | enum | — | required | only `closed_wall`; omission is an error |
| `initial_momentum` | enum | — | required | only `zero`; omission is an error |
| `sources` | enum | — | required | only `none`; omission is an error |
| `spatial_order` | literal integer | — | default 1 | only 1 |
| `numerical_flux` | literal string | — | default `HLL` | other fluxes are rejected |
| `bed_balance` | literal string | — | default `hydrostatic_reconstruction` | other methods are rejected |
| `time_integrator` | literal string | — | default `SSPRK2` | other integrators are rejected |
| `gravity_m_s2` | finite number | m/s² | default 9.81 | greater than 0 and at most 20 |
| `cfl_number` | finite number | 1 | default 0.25 | greater than 0 and at most 0.5 |
| `minimum_time_step_s` | finite number | s | default 1e-6 | 1e-9 through 1; must not exceed maximum step |
| `maximum_time_step_s` | finite number | s | default 60 | greater than 0 and at most 86,400 |
| `maximum_step_retries` | integer | attempts | default 8 | 0 through 20 |
| `dry_depth_threshold_m` | finite number | m | default 1e-6 | greater than 0 and at most 0.1 |
| `negative_depth_tolerance_m` | finite number | m | default 1e-12 | 0 through 1e-4 and less than dry threshold |
| `volume_correction_budget_fraction` | finite number | 1 | default 1e-10 | 0 through 1e-6; applied to `max(V0, A_total h_ref, 1 m3)` |
| `maximum_face_count` | integer | faces | default 500,000 | 1 through 10,000,000 |
| `overwrite_existing_result` | strict boolean | — | default false | existing result path is rejected unless explicitly true |

All numbers reject booleans, strings, NaN and infinity. The schema/model error
names the failed field or range. `volume_correction_budget_fraction` is
dimensionless; the derived budget and every recorded correction use m³.

## Frozen synthetic benchmarks and reference tolerances

The machine-readable manifest is
[`examples/triangular_solver/benchmark_references.json`](../examples/triangular_solver/benchmark_references.json).
It contains independent reference states; no value is derived from the future
solver.

1. **Flat lake at rest:** 10 m × 10 m, `z_b=0`, `h=1 m`, zero momentum, closed
   walls, `t=100 s`. Maximum absolute depth error ≤`1e-10 m`; maximum absolute
   velocity error ≤`1e-10 m/s`; absolute volume residual ≤`1e-8 m³` (the frozen
   scale is 100 m³ and the relative threshold is 1e-10). No mass correction is
   allowed for this equilibrium case.
2. **Stepped bed lake at rest:** `eta=2 m`, per-face bed values include wet,
   shallow and dry faces; the exact reference is `h=max(eta-z_b,0)`, `hu=hv=0`.
   The same state tolerances apply. No cumulative correction may be used to
   cancel a water-surface error.
3. **Completely dry closed box:** `h=hu=hv=0` for 100 s. The saved state stays
   exactly zero, output times are reached, and no zero-step loop or correction
   occurs.
4. **Wet-bed Stoker Riemann dam break:** flat frictionless bed, domain `[0,2]`
   m × `[0,1]` m, dam at `x=1 m`, left state `(h,u)=(2 m,0)`, right state
   `(1 m,0)`, `g=9.81 m/s²`, fixed comparison time `0.1 s`. The reference
   star depth is the positive root of
   `2(sqrt(g h*)-sqrt(g h_L)) + (h*-h_R)sqrt((g/2)(1/h*+1/h_R)) = 0`;
   the star state is `h*=1.453840892374573 m`, `u*=1.3058337531817275 m/s`; the
   fan-head speed is `-sqrt(g h_L)`, fan-tail speed is `u* - sqrt(g h*)`, and
   the right shock speed is `h* u*/(h*-h_R)`. Sample/face-centroid values in
   the manifest are frozen. Relative area-weighted L1 depth errors are capped
   at 0.35, 0.25 and 0.18 for 8, 16 and 32 x-columns respectively; each
   refinement must strictly reduce the error. The 16/32-column momentum L1
   error is capped at 0.30/0.22 and must also decrease. Closed-box volume
   residual is ≤`1e-10 × max(V0, A_total h_ref, 1 m³)`.
5. **Edge identity/orientation variants:** reorder face storage and stable IDs
   without changing physical geometry, and rotate/translate the fixture.
   Internal-edge mass contributions must cancel pairwise; mapped static-state
   errors use the same lake tolerances. A separate unequal-area fixture
   prevents correctness from depending on uniform triangles.

The fixed volume scale is `max(V0, A_total h_ref, 1 m³)`, where `h_ref` is the
maximum initial face depth after the declared P3-A depth/level mapping; it is
not recomputed from a solver result. Static equilibrium, mass conservation, correction
budget, dynamic reference error, grid refinement and time-step sensitivity
are separate acceptance checks. P4-0 freezes definitions and numbers only;
the suite is run against P4-1 after independent review. No performance,
real-event or engineering-accuracy claim follows from these synthetic cases.

## Result and failure contract

The future result is `triangular-solver-result-v1` with dimensions `(time,
face)` and explicit original `node_xy_m`, `face_nodes`, stable `face_id`,
`face_area_m2`, projected CRS, mesh/topology/terrain fingerprints and input
artifact SHA-256 digests. Variables are `water_depth_m(time,face)`,
`momentum_x_m2_s(time,face)` and `momentum_y_m2_s(time,face)`. `time_s` is
relative to the nullable timezone-aware `time_origin`; no wall-clock time is
invented. The result carries the scheme version, total accepted/CFL-limited/
rejected counts, CFL-step extrema, all-step extrema, and per-output-interval
`subminimum_alignment_step_s` plus `subminimum_alignment_cfl_limit_s` arrays. The accepted counts and step extrema must
reconcile with the saved total duration and output times. Actual per-time
minimum depth and maximum speed, maximum internal-edge pair-cancellation
residual, and rejected-step counts by reason are stored as diagnostics. It also records mass-
correction count and the correction-budget scale/budget in m³. Per-time ledgers
retain initial/final volume, boundary/source terms, applied corrections and
residuals. The result also carries explicit false values for
gate hydraulics, Manning, rainfall and production approval. Face IDs are int64 in NetCDF and
decimal strings in JSON/GeoJSON.

An unsuccessful request or failed integration is an error report, not a
successful result artifact. The result contract cannot be used as proof that
the solver is implemented; P4-1 must supply the writer and execution tests.

## Source references

- Audusse et al., *A Fast and Stable Well-Balanced Scheme with Hydrostatic
  Reconstruction for Shallow Water Flows*, SIAM J. Sci. Comput. 25(6), 2004,
  [DOI: 10.1137/S1064827503431090](https://doi.org/10.1137/S1064827503431090).
- Harten, Lax and van Leer, *On Upstream Differencing and Godunov-Type Schemes
  for Hyperbolic Conservation Laws*, SIAM Review 25(1), 1983,
  [DOI: 10.1137/1025002](https://doi.org/10.1137/1025002).
- Gottlieb, Shu and Tadmor, *Strong Stability-Preserving High-Order Time
  Discretization Methods*, Math. Comp. 67, 1998,
  [AMS article](https://www.ams.org/mcom/1998-67-221/S0025-5718-98-00913-2/).
- Wang and Liu, *A comparative study of finite volume methods on unstructured
  meshes for simulation of 2D shallow water wave problems*, Math. Comput.
  Simul. 53, 2000, [DOI: 10.1016/S0378-4754(00)00173-7](https://doi.org/10.1016/S0378-4754(00)00173-7).
- Hydrostatic reconstruction and HLL on triangular shallow-water meshes are
  method references, not validation of this project’s combination. The
  equations, orientation rules, tolerances and capability limits above remain
  project decisions and must pass the frozen tests.
