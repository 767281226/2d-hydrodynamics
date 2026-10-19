# Rehearsal R1 contract and catalog

Batch: `REHEARSAL-R1-CONTRACT-CATALOG-20261019`. This is an independent Python input/catalog contract only. It does not create a Java/Vue copy, write to a database, start an engine, or publish a result.

## Commands

```text
hydrodynamics-rehearsal schema --model scenario --output examples/rehearsal/rehearsal_scenario_v1.schema.json
hydrodynamics-rehearsal catalog list --kind sources
hydrodynamics-rehearsal catalog list --kind events
hydrodynamics-rehearsal catalog list --kind templates
hydrodynamics-rehearsal catalog show events TJ23-DONGDIAN-ACTIVATE
hydrodynamics-rehearsal template clone TJ_2023_DONGDIAN_PUBLIC_HISTORY_V1 --output examples/rehearsal/dongdian_history_draft.yaml
hydrodynamics-rehearsal validate examples/rehearsal/scenario_historical_clone_draft.yaml --mode preflight
hydrodynamics-rehearsal validate examples/rehearsal/scenario_synthetic_closed_box.yaml --mode preflight --require-compute
```

`validate` returns 0 when it successfully produced a validation report. Use `--require-compute` when a script must fail if `can_compute` is false; that condition returns 3. Invalid documents and catalog/output errors return 2. No subcommand launches a solver or contacts external services.

## Scenario fields and normalized time axis

`ScenarioDraft` is strict (`extra=forbid`, finite numeric values, numeric strings and booleans cannot stand in for numbers). The public `normalize_output_times` function applies the same numeric-string/boolean rejection. It records the display name, stable region IDs, event reference, business-local start and IANA timezone, duration, output interval, engine mode, profile references, upper-boundary source choices, rule reference, initial/rainfall status, downstream policy, requested products, and metric-policy version. `tenant_id`, platform support flags, solver `dt`, actual result fields, and lower-boundary values are not user inputs in this contract.

The local `time_origin_local` field must be present; use null when no absolute origin is known. Absolute timestamps use `yyyy-MM-dd HH:mm:ss` with a separate IANA timezone. Nonexistent and ambiguous local times are rejected instead of guessed.

`duration_hours` and `output_step_minutes` normalize to `output_times_s` in seconds. The calculation uses decimal arithmetic, starts at 0, retains the exact terminal duration, and adds that endpoint when the requested interval does not divide the duration. A 120-hour duration with 60-minute output spacing yields 121 frames. The interval is a save/output spacing only; it never configures the solver integration `dt`. The R1 contract bounds duration at 31,536,000 seconds, frame count at 100,000, and distinct output spacing at no less than 1e-6 seconds. A terminal remainder below the minimum is rejected; source breakpoints produce a proposed-axis diagnostic, not a silent edit.

## Upper-boundary source contract

Each section can choose one discriminated source mode:

- `FORECAST`: forecast dataset/version, issue time and timezone, and section series reference.
- `DESIGN`: approved design dataset/version, method, return period, approval reference, and section series reference.
- `HISTORICAL`: event/observation dataset/version, observation kind, station/section reference, and series reference.
- `MANUAL`: non-empty rationale, optional source reference, and explicit series reference.

Every series declares quantity, unit, temporal policy, and scenario-relative sample points if provided. Discharge remains non-negative `m3/s`, with an explicit direction stored separately. Absolute water level uses `m` and requires a vertical datum; it does not supply velocity or flux. The currently packaged parser resolves only the pinned synthetic profile. Other forecast/design/historical dataset references remain `NOT_ASSESSED` until a trusted parser binds their contents and digest; a user-supplied `supported=true` is rejected.

`rainfall.mode=UNKNOWN` differs from `EXPLICIT_NONE`. The first is a saved but non-computable draft; the second is an explicit no-rain choice. Initial state follows the same distinction between `UNKNOWN` and a versioned profile reference.

`downstream_policy` defaults to `DEFERRED_UNKNOWN` with `user_input_required=false` and `applied=false`. This is not zero flow, zero stage, free outflow, or a closed wall. Only the pinned `synthetic_closed_box_v1` profile may select `SYNTHETIC_CLOSED_DOMAIN`; user-authored closed-wall defaults and real open-boundary computation are rejected.

## Built-in sources and templates

The packaged catalog enables only reviewed source IDs S1–S4 from the R1 handoff. Candidate sources with access/review gaps are absent. It retains the 2023-08-01 02:00 Dongdian decision-effective event, 2023 Yongding observations/actions, 2025 Xihe ecological report with unknown event time, and 2025 joint-dispatch theme. Each event preserves source publication time separately from event time, local-time precision, the assumed timezone policy, source URL, measurements and missing evidence. All events have `can_drive_model=false` and `gate_commands_sent=0`.

The Xiqing/Jinghai transfer report retains its administrative scope alias and `mapped_region_id=null`, even when referenced by the Dongdian history template. It cannot become a Dongdian region result or a per-person evacuation record. Every catalog template is read-only, `runnable=false`, and `engineering_validated=false`; cloning creates an incomplete draft with unknown inputs and deferred downstream policy.

## Save, compute, publish and capability state

`evaluate_scenario` reports `can_save`, `can_compute`, `can_publish`, `execution_readiness`, blockers, warnings, normalized times, capability flags, and verified-profile hashes. `can_save` means structural draft readiness only; the business service must still enforce tenant/project authorization and resolve stable region IDs. Computation additionally requires a trusted resolved profile, compatible registered engine, complete required profiles, supported source/product policies, and consistent scopes. The current positive case is a small pinned synthetic triangular closed-wall profile. Its provenance is `SYNTHETIC_MODEL`, usage is `TEST_ONLY`, and `can_publish` remains false because R1 adds no result adapter or publisher.

Capability fields are reported as four separate facts: raw variable availability, adapter readiness, quality pass, and publishability. Existing raw variables alone never make a product publishable. In particular, open-boundary RHS, formal risk/loss, wet duration, and automatic evacuation remain unavailable until separate implementation and review.

`ExecutionEnvelope` keeps platform run/attempt IDs separate from the legacy `script_id`/`local_run_id`, which must equal each other. It also binds tenant/project and scenario/version against trusted service context, model `case_id`, engine mode, and the SHA-256 of verified resolved inputs. `platform_run_id` cannot be reused as the local script identity. This module validates the envelope only; it does not mint platform identities or perform G1/database writes.

## Product selector

The closed product aggregation allowlist is product-specific: `FRAME`, `SERIES`, `MAX_OVER_WINDOW`, `PEAK_AREA_OVER_WINDOW`, `UNION_FOOTPRINT`, `FIRST_ARRIVAL`, `WET_DURATION`, and `EVENT_TIMELINE`. Frame selectors require a frame index and time; window/series selectors require an explicit time window; spatial selectors require `scope_id` and a SHA-256 `scope_hash`. G1 audit uses only `AUDIT_RUN_GLOBAL` with no spatial hash.

`validate_selector_against_manifest` requires exact result set, product, scope identity, aggregation, metric policy, frame/window match, and `COMPLETE` coverage. Frame time must match the manifest's actual time coordinate. It does not trust a client-echoed selector or infer coverage from a display bounding box.
