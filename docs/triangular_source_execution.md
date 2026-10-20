# Experimental triangular rainfall and combined-source execution

P4-2C adds one strict execution request, `triangular-source-run-v1`, for
`rainfall` or `manning_and_rainfall`. It reuses a complete P4-1
`sources: none` config only for its time axis, parameters, P2 mesh, P3-A package,
and initial state. The base none result route is not run. Source preparation is
re-read and rebound to the actual inputs before every run; its applied flags
remain false.

The run config requires `format`, `schema_version`, `case_id`, `sources`,
`base_run_config_file`, `source_config_file`, and `result_file`.
`overwrite_existing_result` defaults to false and only permits replacing a
previous source result bound to the same request. The source mode must exactly
match the enabled Manning and rainfall preparation sections. The older
`triangular-solver-run-v1` and `triangular-manning-run-v1` routes retain their
original behavior; the Manning-only route still rejects enabled rainfall,
including an all-zero schedule.

| Field | Type / unit | Required or default | Validation |
|---|---|---|---|
| `format`, `schema_version` | literal strings | defaulted to the v1 values | any other version is rejected |
| `case_id` | non-empty string | required | must match the base and source-preparation configs |
| `sources` | literal enum | required | `rainfall` or `manning_and_rainfall`; must match enabled preparation sections |
| `base_run_config_file` | relative path | required | complete valid P4-1 config with `sources: none` |
| `source_config_file` | relative path | required | its linked base config must resolve to the same file, and its preparation artifact must revalidate |
| `result_file` | relative path | required | distinct from base/source configs, preparation, P2/P3-A inputs, and base result |
| `overwrite_existing_result` | strict boolean | `false` | `true` only replaces a prior source result bound to the same request |

The bound `source_cfl_fraction` remains dimensionless, defaults to `0.5`, and
is limited to `(0,1]`. Rainfall input units are `mm/h` or `m/s`; intensities
must be finite and non-negative. Preparation requires explicit intervals that
match base output times, cover the full run without gaps/overlaps, and have
positive duration. Disabled Manning has no SI interpretation payload; combined
mode requires the unchanged `unit: 1` values, explicit SI policy, rationale,
and one valid `0 < n <= 1` value per face.

## Rainfall adaptation and timing

The preparation adapter stores the original rainfall values/unit and the
matched output indices. It converts `mm/h` to SI exactly once using
`intensity / 3,600,000`; the numerical core receives only finite `m/s` rates.
Each interval is spatially uniform, non-negative and constant. A full schedule
must cover the run, with each boundary matched to a base output time. Execution
does not insert or move output times.

Intervals are left-closed/right-open; the final endpoint only terminates the
schedule. A step selects its interval at the step start and holds that rate for
both SSPRK2 stages. The step ending at a breakpoint uses the old rate in stage
2; the next step uses the new rate. The saved schedule records the raw input
boundaries and the effective output-time boundaries separately.

The source terms are

```text
S_h = R_interval
S_q = (0, 0)
```

When Manning is enabled, the same stage uses

```text
gamma = g n^2 hypot(hu, hv) / h^(7/3)
S_q = -gamma (hu, hv)
```

The two stage updates remain explicit SSPRK2. Rain adds depth without directly
adding horizontal momentum. Manning bounds are recomputed after the first
stage and use the bound-scaled tolerance frozen in P4-2B-R2; rainfall does not
disable or reset that limit.

## Mass ledger and applied flags

For each accepted step and face, rainfall contributes the SSPRK2 weighted
volume `0.5*(R0+R1)*dt*face_area`. The current piecewise-constant schedule uses
the same rate at both stages. Rejected steps do not advance rainfall totals,
interval accounting, accepted source diagnostics, state, or correction
budgets. The ledger records cumulative source volume at each output, and the
result separately records interval rainfall rates and accepted interval
volumes. Those totals are checked against an independent integral over the
effective output intervals.

The correction reference scale and allowance remain based on the initial
state. Rainfall volume never increases the negative-depth correction budget.
Boundary net volume stays zero for the closed-wall route; `source_net_volume_m3`
contains only accepted rainfall volume. Manning changes momentum only and adds
no mass.

The new native output format is `triangular-source-result-v1` with product
`experimental_triangular_source_shallow_water_result`. Rainfall is always
enabled and marked applied for a completed source run, even when every rate is
zero. `rainfall_nonzero_effect_applied` is true only if an accepted stage used
a positive rate. The Manning flags and fields are present only for the combined
mode; a zero-momentum case may report Manning applied with no nonzero Manning
effect. The reader binds both run configs, preparation, P2/P3-A inputs, schedule
and digests, and treats on-disk arrays as authoritative.

## Examples and test scope

[`rainfall_only_run.yaml`](../examples/triangular_sources/rainfall_only_run.yaml)
and
[`manning_and_rainfall_run.yaml`](../examples/triangular_sources/manning_and_rainfall_run.yaml)
use the synthetic still-water base case. Their source-preparation files show
the required unit and interval schedule. The dynamic combined-source regression
uses a separate synthetic wet-bed triangular case with zero initial momentum.

The tests also use a controlled spatial-residual injection to reach a rare
second-stage rejection while positive rainfall is active. That fixture isolates
rollback and source-accounting behavior; it is not an unmodified hydraulic
scenario. This release does not cover spatial rainfall, infiltration,
evaporation, open boundaries, gates, or dynamic dry-front validation.
# Security-aware rainfall reporting

For a security-enabled P2/P3 mesh, rainfall is integrated over active face area. Native source results separately report the excluded-area potential as `security_rainfall_accounting.security_excluded_unmodeled_rainfall_volume_m3` and by interval. This is unmodeled rainfall, not an accepted-budget loss and not transferred to active faces. P2/P3 identity drift is rejected by the shared P4 source-run guard. See [security_exclusion.md](security_exclusion.md).
