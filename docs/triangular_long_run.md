# Experimental triangular long-run driver

`hydrodynamics.triangular_long_run` runs the experimental triangular
shallow-water scheme for large meshes (about 10^5 faces) and long simulations
(about 10^6 steps). It streams results instead of building the in-memory P4
result contract. It does not change the legacy runners
(`hydrodynamics-triangular-run`, `-manning-run`, `-source-run`): those keep
their explicit Manning treatment, their correction budget, and their
whole-result NetCDF output.

## Scheme (unchanged)

The scheme is the P4-1/P4-2 scheme: cell-centred first-order HLL fluxes, Audusse
hydrostatic reconstruction (well-balanced), closed-wall reflection, the frozen
wave CFL bound `cfl * area / sum(L_e a_e)`, SSPRK2, the dry-depth threshold,
tiny-negative clipping inside the correction budget, and uniform rainfall as an
h source. The only differences are the opt-in options listed below.

Both the legacy path and the long-run path use the kernels in
`hydrodynamics/triangular_solver_kernels.py`:

* The numpy kernel does the same edge arithmetic as the original per-edge loop.
  Face sums use `numpy.bincount` over an interleaved left/right index stream,
  in the same order as the loop.
* An optional numba kernel is used when `numba` imports and `kernel` is `auto`
  or `numba`. It runs a parallel edge pass and then a parallel face pass. The
  face pass adds each face's edge terms in ascending edge index, which is the
  order of the sequential loop. numba is never a required dependency.

Tolerance: on the accepted P4-2C rainfall/Manning cases and on random wet/dry
states over a non-flat bed, the vectorised legacy kernels (`_rhs_and_cfl`,
`_manning_source`, `_sanitize_stage`, `_volume`, `_speed_max`) give
**binary64-identical** results to the frozen scalar copy in
`tests/triangular_scalar_reference.py`. So do the numba RHS and the long-run
`explicit_source_cfl` mode with `step_cfl_fraction: 1.0`. The long-run rain
ledger matches the legacy ledger to 1e-12 relative. The legacy ledger takes an
`fsum` of per-face products, while the long-run ledger uses
`rate * dt * total_area` with compensated accumulation.

## Options contract: `triangular-long-run-options-v1`

The JSON Schema is
[`long_run_options.schema.json`](../examples/triangular_long_run/long_run_options.schema.json).
An example is in
[`long_run_options.example.json`](../examples/triangular_long_run/long_run_options.example.json).
Unknown fields are rejected, and validation errors raise `ValueError` or
`TriangularLongRunError`.

| Field | Type / unit | Default | Legal values / validation |
|---|---|---|---|
| `gravity_m_s2` | number, m/s2 | 9.81 | (0, 20] |
| `cfl_number` | number, - | 0.25 | (0, 0.5] |
| `step_cfl_fraction` | number, - | 0.9 | (0, 1]; proposed dt = fraction x CFL bound. 1.0 is the P4-1 rule. This option only changes step selection. |
| `minimum_time_step_s` / `maximum_time_step_s` | number, s | 1e-6 / 60 | [1e-9, 1] / (0, 86400]; min <= max |
| `maximum_step_retries` | integer | 8 | [0, 20]; each retry halves dt (or uses the fresh stage-1 bound) |
| `dry_depth_threshold_m` | number, m | 1e-6 | (0, 0.1]; must exceed `negative_depth_tolerance_m` |
| `negative_depth_tolerance_m` | number, m | 1e-12 | [0, 1e-4] |
| `volume_correction_budget_fraction` | number, - | 1e-10 | [0, 1e-6] |
| `volume_correction_reference` | enum | `initial_state_plus_cumulative_inflow` | `initial_state`: P4-1 `max(V0, A*max(h0), 1 m3)`. `initial_state_plus_cumulative_inflow`: `max(V0 + rain so far + boundary inflow so far, A*max(h0), 1 m3)`, evaluated before each attempt. |
| `manning_friction` | enum | `point_implicit` | `none` (manning_n must be absent), `explicit_source_cfl` (P4-2 explicit drag plus the `source_cfl_fraction/gamma` step bound), `point_implicit` |
| `source_cfl_fraction` | number, - | null | required in (0, 1] for `explicit_source_cfl`, rejected otherwise |
| `outflow_stage_volume_fraction` | number, - | 0.5 | (0, 1]; the largest share of a face's water above the dry threshold that prescribed outflow can remove in one RK stage |
| `maximum_accepted_step_count` | integer | 1e9 | [1, 1e12]; exceeding it fails with `step_safety_limit` |
| `checkpoint_interval_s` | number, s or null | null | (0, 31536000]; needs a `checkpoint_directory` |
| `checkpoint_keep_count` | integer | 2 | [1, 1000] newest checkpoints kept |
| `kernel` | enum | `auto` | `auto`, `numpy`, or `numba` (`numba` fails if it is not installed) |
| `progress_interval_steps` | integer | 1000 | [1, 1e9] accepted steps between progress callbacks |

Point-implicit Manning: after each SSPRK2 Euler stage (flux, rain, and boundary
update, then sanitation), every wet face gets
`q <- q / (1 + dt * g * n^2 * |q| / h^(7/3))`. This is the same as
`1 + dt g n^2 |u| / h^(4/3)`, with the stage depth and stage discharge. The
factor is at least 1, so for any dt friction only reduces |q| and never reverses
it. There is no friction step bound, so thin rain films are no longer limited to
0.01-0.1 s steps.

## Open boundary groups: `triangular-open-boundary-group-v1`

| Field | Type / unit | Default | Validation |
|---|---|---|---|
| `name` | non-empty string | required | unique among groups |
| `role` | enum | required | `inflow` (water enters) or `outflow` (water leaves) |
| `edge_indices` | list of int | required | exterior P2 mesh edge indices (`mesh.edge_nodes` order), unique, at most one group per edge |
| `time_s` | list of number, s | required | strictly increasing; must cover `[0, last output time]` |
| `discharge_m3_s` | list of number, m3/s | required | same length as `time_s`, every value >= 0 (direction comes from `role`) |
| `distribution` | enum | `length` | `length` or `conveyance_depth_5_3` (weights `L_e h^(5/3)` over wet faces; an all-dry inflow section falls back to length weights) |

Q(t) is linearly interpolated between samples. Asking for a value outside the
samples fails with `discharge_series_out_of_range`. Use
`open_boundary_edges_for_mesh_group(mesh, name)` to take edge indices from a
named P2 boundary group.

Candidate `edge_indices` for assumed river sections (DEM main-channel rule): see `docs/river_section_selection.md`.

Formulation, documented here as an experimental choice:

* Open edges keep the closed-wall Riemann flux, which gives hydrostatic balance
  and reflects normal momentum. The prescribed discharge is added as an extra
  flux on the interior face. With Q = 0 the result is exactly a wall, and a lake
  at rest stays at rest.
* Inflow adds mass flux `q_e` per unit edge length, with `sum q_e L_e = Q(t)`.
  It also adds momentum flux `q_e^2 / h_g` along the inward normal, where the
  ghost depth is `h_g = max(h_face, (q_e^2/g)^(1/3))`. On a dry bed this enters
  at critical depth, so the first water can arrive through the boundary. The
  inflow ghost wave speed `q_e/h_g + sqrt(g h_g)` is added to the face CFL sum.
* Outflow removes mass `q_e` and momentum `q_e * u_face`. The discharge is shared
  over wet edges by the chosen weights. Each face's withdrawal in one stage of
  length dt is capped at `outflow_stage_volume_fraction * (h - dry) * A`.
  Withdrawal from dry faces is not spread to other faces. The ledger records the
  requested and delivered volumes, and the shortfall is requested minus
  delivered. A negative depth still triggers the normal dt-halving retry.

Volumes use the SSPRK2 stage times t and t+dt, so the step volume is
`0.5 dt (Q(t) + Q(t+dt))`. This is exact for piecewise-linear Q when the
output times are aligned with the samples.

## Python API

For a complete real case (mesh artifact + per-face terrain + assumed sections +
discharge CSV, streamed into the large-run native sink and exported), use the
glue driver `hydrodynamics.large_run_driver` (`docs/large_run_driver.md`).

```python
from hydrodynamics.triangular_long_run import (
    TriangularLongRunInputs, TriangularLongRunOptions, OpenBoundaryGroupSpec,
    run_triangular_long_run, load_long_run_checkpoint, open_boundary_edges_for_mesh_group,
)

inputs = TriangularLongRunInputs.build(
    mesh,                                # hydrodynamics.triangular_mesh.TriangularMesh
    bed_m=bed, initial_depth_m=h0,       # one value per face, metres
    output_times_s=[0.0, 3600.0, ...],   # start at 0, strictly increasing
    manning_n=n,                         # per face, (0, 1], or None with manning_friction="none"
    rainfall_rate_m_s_by_interval=None,  # one m/s rate per output interval, or None
    open_boundaries=[{"name": "A", "role": "inflow", "edge_indices": [...],
                      "time_s": t, "discharge_m3_s": q}],
)
# or TriangularLongRunInputs.from_capability(validate_triangular_run_request(cfg), manning_n=..., ...)

summary = run_triangular_long_run(
    inputs, TriangularLongRunOptions(checkpoint_interval_s=21600.0),
    frame_sink=my_sink,                  # called with a TriangularFrame for every output time
    checkpoint_directory="run/ckpt",     # needed when checkpoint_interval_s is set
    resume_from=None,                    # path of a checkpoint_XXXXXXXX.npz to continue from
    progress_callback=print,             # optional, every progress_interval_steps steps
    edges=None,                          # optional precomputed triangular_solver._edges(mesh)
)
```

`TriangularFrame` fields: `output_index`, `time_s`, plus the per-face arrays
`water_depth_m`, `unit_discharge_x_m2_s`, `unit_discharge_y_m2_s`,
`velocity_x_m_s`, `velocity_y_m_s` (0 on dry faces), and `water_level_m`
(bed + h). Each array is a new read-only float64 copy in mesh face order. The
frame also has `ledger`, the row for that output. The driver keeps no frames;
`MemoryFrameSink` is only for small cases.

`TriangularLongRunSummary` returns:

* the final state;
* per-face `maximum_depth_m`, `time_of_maximum_depth_s`, and
  `maximum_speed_m_s`, updated after every accepted step;
* the ledger rows;
* step counters, extreme step sizes, and rejection reasons;
* the maximum pair residual and maximum gamma;
* the final correction scale and budget;
* the kernel used, the wall time, and the checkpoint paths.

Ledger row per output time, all in m3 unless noted:

* `stored_volume_m3` and `initial_volume_m3`.
* Interval and cumulative values for rainfall, inflow, requested outflow,
  delivered outflow, outflow shortfall, and correction.
* `residual_m3 = stored - initial - rain - inflow + delivered outflow - correction`.
* Per-group cumulative inflow, requested, delivered, and shortfall volumes.
* `volume_correction_reference_scale_m3`.
* Minimum and maximum depth.
* Interval accepted and rejected step counts, minimum and maximum dt, and wall
  time.

## Checkpoints

A checkpoint is written atomically (temporary file, fsync, `os.replace`) as
`checkpoint_<output index>.npz` in `checkpoint_directory`. It is written at the
first output time at or after each `checkpoint_interval_s` and always at the
final output. It stores the state `(h, hu, hv)`, the per-face maxima, the output
index and time, counters, the compensated ledger totals, the ledger rows, and
fingerprints of the inputs and numerical options. Resuming checks both
fingerprints. Run-control fields (checkpoint settings, kernel, progress, step
cap) can change between runs. A resumed run gives the same final state, maxima,
and ledger as a run without a break. It does not emit frames that were already
emitted.

## Performance (graded experimental mesh, 186,284 faces, 282,760 edges)

These numbers are from a 20-thread Windows workstation that other jobs were
also using:

| Path | Seconds per accepted step |
|---|---|
| Original per-edge Python loop | about 52 |
| Vectorised numpy | about 0.30 |
| numba, dry start with rain and point-implicit friction | about 0.037 |
| numba, wet state | about 0.043 |

The working set is about 0.5 GiB, mostly the `TriangularMesh` Python object.
Building the `TriangularMesh` once takes 50-66 s. Explicit friction is
replaced by point-implicit friction, so the step size follows the wave CFL bound
and the smallest faces control it. Wave-CFL dt with u = 1 m/s:

| Depth | All faces | Faces with area/perimeter >= 1.5 m |
|---|---|---|
| 1 m | 0.0061 s | 0.094 s |
| 2 m | 0.0045 s | 0.071 s |
| 5 m | 0.0030 s | 0.048 s |

## Limitations

* Experimental only. Not production approved.
* No gate hydraulics, infiltration, or spatial rainfall.
* Open-boundary forms are the simple prescribed-flux forms above. There are no
  stage, rating-curve, or Riemann-invariant boundaries.
* Outflow caps are applied per group. A face next to two outflow groups is
  protected only by the negative-depth retry.
* Explicit mode follows the legacy step rules but does not reproduce every
  legacy Manning alignment diagnostic. Use the legacy runners for accepted P4
  result artifacts.
