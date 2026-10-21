# Triangular boundary request preparation

P4-3A prepares explicit boundary requests against the real P2 mesh and P3-A business package. It does not run a solver, compute a ghost state, call a numerical flux, or change existing closed-wall routes. See the [P4-3A ADR](p4_3a_triangular_boundary_contract_adr.md) for the physical state-closure proposal and its review status.

Status: **P4-3A preparation-only documentation; pending main-chat review. It is not executable open-boundary capability.**

Document pairing: this is the English technical guide. Its Chinese counterpart is [triangular_boundary_preparation_zh.md](triangular_boundary_preparation_zh.md). For the single canonical decision record, see the [Chinese ADR](p4_3a_triangular_boundary_contract_adr.md); the [English ADR](p4_3a_triangular_boundary_contract_adr_en.md) is a reading translation.

## Input and execution

Run P2 mesh preparation and P3-A business preparation first, using the small synthetic annulus example:

```text
hydrodynamics-mesh-prepare examples/triangular_mesh/preparation.yaml
hydrodynamics-business-prepare examples/triangular_mesh/business_preparation.yaml
```

P2 produces `examples/triangular_mesh/prepared_mesh.nc`; P3-A then writes `examples/triangular_mesh/business_preparation.json` and retains its actual outer edges, time series, source labels, absolute water level and declared datum. The boundary config [`boundary_preparation.yaml`](../examples/triangular_boundaries/boundary_preparation.yaml) references that generated P3-A package using a path relative to the config itself:

```text
hydrodynamics-triangular-boundary-prepare examples/triangular_boundaries/boundary_preparation.yaml
```

The paths in this config are relative to that YAML file. `business_file` must resolve to a valid P3-A package, whose own `mesh_artifact` must resolve to a valid P2 UGRID. The config has its own output-time axis; it does not require or modify a solver run config.

| Field | Type and unit | Required/default | Range or rule | Failure behavior |
| --- | --- | --- | --- | --- |
| `format` | string | Required literal | `triangular-boundary-preparation-v1` | Reject other formats |
| `schema_version` | string | Required literal | `1.0` | Reject other versions |
| `case_id` | non-empty string | Required | Identifies this preparation request | Reject empty text |
| `business_file` | relative path | Required | Existing P3-A package; cannot be the output | Reject absolute path, missing package, or identity mismatch |
| `output_file` | relative path | Required | Regular JSON path, distinct from config, P3-A and linked P2 | Reject input/result collision, symlink, hard link, or unapproved existing output |
| `output_times_s` | finite real array, seconds | Required | 2–100,000 points; starts exactly at 0; strictly increasing; each value in `[0, 31,536,000]` | Reject bool, NaN, infinity, duplicate or descending times |
| `time_origin` | timezone-aware ISO-8601 string or null | Required | Null means elapsed seconds only | Reject timestamps without a timezone |
| `unbound_exterior_policy` | string | Required literal | `closed_wall` | Reject implicit free outflow or another policy |
| `boundary_bindings` | array of group choices | Defaults to empty | Must map every P3-A boundary group exactly once | Reject missing, duplicate, unknown, internal, or extra groups |
| `boundary_bindings[].group_name` | non-empty string | Required | Exact existing P3-A group name | Reject unmatched identity |
| `direction` | string | Required for discharge; otherwise absent | `inflow` or `outflow`; fixed for the group | Reject omitted/invalid direction; do not change P3-A Q sign |
| `allocation_policy` | string | Required for discharge; otherwise absent | `uniform_per_unit_length` | Reject other distribution choices |
| `temporal_policy` | string | Required for timeseries; absent for constant | `piecewise_constant` | Reject interpolation, extrapolation or unrecognized temporal rules |
| `overwrite_existing_output` | boolean | Defaults to `false` | If true, may replace only an existing structurally valid boundary-preparation result with the same `case_id` | Reject P2/P3/config, malformed results, different cases and unrelated files |

P3-A discharge values remain non-negative total group flow in m³/s. `direction=inflow` adds the outward-positive sign `-1`; `direction=outflow` adds `+1`. Water levels remain absolute metres with exactly matching, known terrain datum. No conversion, datum inference, clamping or elevation filling occurs.

For each group, edge lengths come from P2 geometry. The artifact stores each edge's outward unit normal, its length weight `L_e/ΣL`, signed request share `Q_e`, and request unit-width discharge `q_n,e=Q_e/L_e`. Inflow therefore has negative outward `Q_e` and `q_n`; outflow has positive values. These are requested allocations, not accepted HLL fluxes.

## Security-exclusion closed walls

When linked P2/P3-A carries authenticated `excluded_closed_wall` evidence, preparation revalidates it against the active P2 mesh and derives protected edge IDs from the actual security geometry. P3-A package preparation and P4-3A boundary preparation reject any binding whose physical edge IDs intersect that set, including partial overlaps and aliases with different group names. Unbound protected edges remain closed walls; requests are not redistributed.

## Time-series behavior

Constant values cover the full `[0,T]` output axis. A timeseries must start at 0, end at `T`, and every source breakpoint must match exactly one output time using the shared `max(1e-12 s, 8 ULP)` tolerance. The preparation records each raw source time and matched effective output time without rewriting the P3-A input.

Segments are left-closed/right-open. The final sample is retained as a terminal sample and contributes no extra duration. There is no interpolation, extrapolation, gap filling or zero fill. A breakpoint at the end of a future accepted SSPRK2 step is a next-step transition; the second stage of the just-finished step uses the previous segment.

The preparation reports request integrals using the effective matched interval durations. They are analytic request summaries only and are not the future solver's `boundary_net_volume_m3` ledger.

## Output checks

The JSON format is `triangular-boundary-preparation-result-v1`. It is always marked `preparation_only=true`, `simulation_result=false`, `solver_completed=false`, `production_approved=false`, `execution_available=false` and `boundary_applied=false`. It identifies all actual P2 exterior edge IDs, the assigned groups and the unbound edges retained as closed walls. It also stores config, P3-A and P2 SHA-256 digests plus topology and terrain fingerprints.

`read_triangular_boundary_preparation(path, config_path)` reads the linked inputs again, rebuilds the complete expected artifact, then compares all fields. A correct-looking hash inside a changed artifact is not accepted as proof. Output publication uses a temporary JSON file and readback before commit. With overwrite disabled, commit is an atomic no-replace rename. With overwrite enabled, the existing target must validate as a boundary-preparation result for the same `case_id`; its file identity and SHA-256 are captured and checked again after readback under a per-path publisher mutex before atomic replacement. If no target existed initially, overwrite enabled still uses no-replace publication, preserving a late unrelated file. P2/P3-A packages, configs and simulation results remain protected. The mutex serializes publishers using this module; it is not filesystem compare-and-swap for arbitrary external processes. A non-cooperating writer can still race after the final identity check, so callers must keep overwrite targets exclusively controlled while publishing.

## Synthetic example and machine-readable references

The example config maps the P3-A `outer_boundary` discharge series to fixed inflow with length-weighted request allocation and piecewise-constant time behavior. It maps `hole_0` as the existing constant absolute-water-level group; its matching datum remains the one declared by the P3-A terrain/business inputs. Other unbound P2 exterior edges remain explicit closed walls. The example demonstrates input preparation only and is not a runnable open-boundary case.

[`boundary_preparation_references.json`](../examples/triangular_boundaries/boundary_preparation_references.json) is an independent, machine-readable analytic reference set marked `pending_main_review`. It describes four preparation references: the 1 m/3 m length allocation, a `[0,10,20,30] s` piecewise-constant inflow/outflow integral, invariance under rigid rotation and identity/order changes, and the static absolute-stage expectation over a flat bed. Each entry gives inputs, expected values, a derivation or scope note, tolerances where applicable, and primary-source links. The water-level item is only an analytic hydrostatic expectation; the preparation product does not compute depth or claim a zero applied flux. These references do not validate HLL flux, a boundary RHS, or an engineering simulation.

## Current limits

This API does not determine wet/dry state, characteristic regime, normal velocity from stage, tangential velocity, ghost state or HLL flux. It does not claim that requested Q equals the eventual actual boundary mass exchange. The current P4-1 closed-wall run contract still rejects a P3-A package that contains open/group boundary bindings; the presence of a preparation artifact does not change that validator.

P4-3B must review and freeze the full exterior state closure, wet/dry and reversal rules, boundary wave-speed limits, SSPRK2 stage handling, retry rollback and actual boundary/source ledgers before any RHS implementation.
