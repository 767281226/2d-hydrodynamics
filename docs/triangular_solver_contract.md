# P4-1 triangular run, state and result contracts

`hydrodynamics.triangular_solver_contract` remains independent from the
numerical implementation. Its capability check reads a P3-A v2 package,
validates its linked P2 mesh/terrain identity, and reports that the limited
experimental closed-wall route is available. The check is preflight only: it
does not advance the model or write the configured result.

## Prepare the synthetic input and validate its capability

```text
pip install -e ".[triangular]"
python -m hydrodynamics.mesh_preparation examples/triangular_solver/closed_wall_preparation.yaml
python -m hydrodynamics.triangular_business examples/triangular_solver/closed_wall_business.yaml
```

Then inspect the request without launching a solver:

```python
from hydrodynamics.triangular_solver_contract import validate_triangular_run_request

capability = validate_triangular_run_request(
    "examples/triangular_solver/closed_wall_run.yaml"
)
print(capability.report)
```

The expected report status is `ready_for_experimental_closed_wall_run`, with
`preflight_only: true`, `execution_available: true`, and
`solver_completed: false`.
The example uses only synthetic data. It demonstrates complete terrain and
initial depth, an explicit closed-wall declaration, zero initial momentum,
and `sources=none`. The included roughness remains provenance and is not
applied. The capability check rejects partial terrain, partial initial fields,
or any P3-A boundary binding; it never fills missing values or turns an open
binding into a wall.

Run the synthetic example with:

```text
python -m hydrodynamics.triangular_solver examples/triangular_solver/closed_wall_run.yaml
```

This executes the experimental first-order HLL/hydrostatic/SSPRK2 route and
publishes `closed_wall_result.nc` only after a temporary NetCDF file has been
read back and rebound to the current run config and P2/P3-A inputs. The result
is explicitly not production approved; Manning, rainfall, gate hydraulics, and
other sources remain disabled.

When `overwrite_existing_result` is false, publication uses an atomic
no-replace operation; a destination created after preflight causes a failure
and keeps that file intact. With overwrite explicitly enabled, replacement
still occurs only after read-back validation. Either failure path cleans its
temporary file and preserves a pre-existing result.

The P4-1 result reader/writer must also call
`validate_triangular_result_for_run(result_contract, run_config_path)`. That
entry point re-reads the P2 and P3-A links and binds embedded nodes,
connectivity, geometry-computed face areas, boundary groups, terrain/topology
fingerprints, input file hashes, initial state and output-time axis to those
actual inputs. A structurally valid result with a string-shaped but incorrect
SHA, or a different initial field, is rejected. Legacy-shaped result payloads
may be structurally inspected with inferred metadata for migration diagnostics;
the run-bound entry point rejects inferred geometry or step statistics.

## Contract models

- `TriangularSolverRunConfig` is the strict YAML schema. Its JSON Schema is
  available through `TriangularSolverRunConfig.model_json_schema()`.
- `TriangularConservedState` validates one face-aligned `(h, hu, hv)` state in
  `face_id` order. It checks finite values, int64 face IDs and nonnegative
  water depth; it does not change a state.
- `TriangularMassBudget` checks a signed volume ledger in m³. In this first
  route, `boundary_net_volume_m3` and `source_net_volume_m3` must both be zero.
- `TriangularResultContract` describes a `triangular-solver-result-v1`
  `(time, face)` artifact with original mesh connectivity, stable IDs, input
  hashes, solver/config versions and numerical parameters, states, time origin,
  accepted CFL/total/rejected step counts and extrema, per-interval
  subminimum output-alignment remainders and their checked CFL bounds, and
  per-output mass budgets. The
  alignment exception is accepted only under the ADR's explicit CFL check and
  must be recorded. The P4-1 solver calculates and stores per-time minimum
  depth/maximum speed, internal-edge mass-pair residual, and rejection counts
  by reason. Step statistics use the actual `dt` passed to both Runge–Kutta
  stages; output-time snapping and configured step-bound comparisons use the
  ADR's shared `max(1e-12 s, 8 ULP)` tolerance without enlarging a step. The
  configured and result `output_times_s` interval lower bounds use the same
  duration-scale tolerance; strict ordering/uniqueness remain exact, and an
  interval materially shorter than `minimum_time_step_s` is still rejected. The
  result records the correction reference scale/budget and correction count and requires explicit
  experimental/non-production flags. The execution API is
  `hydrodynamics.triangular_solver.run_triangular_solver`; the independent
  native reader/writer is `hydrodynamics.triangular_solver_netcdf`.

The reader rejects masked/missing samples and rejects `scale_factor` or
`add_offset` on connectivity and face IDs. Face IDs remain signed int64 values
through read-back. It verifies the complete version-1 variable dimensions,
types, units, UGRID references, grid/location associations and projected CRS
metadata; equivalent CRS encodings are compared structurally, while conflicting
WKT/CF metadata is rejected. Internal edge mass-pair diagnostics are computed
from the two area-weighted face increments actually applied, with the frozen
`1e-12 m3/s` acceptance tolerance rather than an algebraic zero expression.

Failed runs raise `TriangularSolverExecutionError`. Its machine-readable
`stage` identifies `stage1`, `stage2`, or `final` when an RK state fails;
`reason` identifies the terminal outcome, `last_reason` retains the final
rejected-step cause when retries are exhausted, and `step_kind` records whether
the attempted advance was CFL-limited or output-alignment-only.

The field types, units, defaults, ranges, failure policy and frozen synthetic
references are specified in the [P4-0 ADR](triangular_solver_adr_p4_0.md).
Reference data are in
[`examples/triangular_solver/benchmark_references.json`](../examples/triangular_solver/benchmark_references.json).
`tests/test_triangular_step_feasibility.py` compares the O(k) joint-extrema
checker to an independent bounded integer-step enumerator, including multiple
output intervals. These are contract/method acceptance cases, not
engineering-accuracy certification.
