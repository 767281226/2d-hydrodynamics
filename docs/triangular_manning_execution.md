# Experimental triangular Manning execution

P4-2B adds an optional Manning-only execution route on top of the triangular
`sources: none` input contract. The base run config supplies the hydro model,
time controls, P2 mesh, P3-A business package, and initial state. It is used for
validation and parameter binding; the Manning runner does not execute its
P4-1 result route. The source-preparation artifact is also re-read and checked
against those inputs before each Manning run.

This route is experimental and is not production approved. It supports Manning
drag only. Rainfall is rejected when enabled, including a zero-intensity
rainfall request. No P4-1 file format, reader, or writer is repurposed.

## Manning input interpretation

The P3-A package continues to store its Manning quantity with unit `1` and its
original numeric values. A source-preparation request must explicitly select
`legacy_unit1_as_si_manning` and include a rationale. This execution route then
interprets each value as SI Manning `n` in `s/m^(1/3)`, requires one value per
face in the range `0 < n <= 1`, and does not rewrite the source package. The
choice follows the documented SI convention used by [HEC-RAS 2D](https://www.hec.usace.army.mil/confluence/rasdocs/r2dum/latest/development-of-a-2d-or-combined-1d-2d-model/creating-hydraulic-property-tables-for-2d-flow-areas).

## Applied source and step control

For conserved state `(h, hu, hv)`, the implementation uses

```text
q = (hu, hv)
gamma = g n^2 hypot(hu, hv) / h^(7/3)
S = (0, -gamma hu, -gamma hv)
```

This is the vector Manning momentum source documented by
[Clawpack](https://www.clawpack.org/list/manning.html). The source is added to
both SSPRK2 Euler residuals. Before evaluating the second residual, the source
and its bound are recomputed from the first-stage state. The step bound is the
minimum of the wave CFL bound and `source_cfl_fraction / gamma` over wet faces.
`source_cfl_fraction` is dimensionless, defaults to `0.5`, and must satisfy
`0 < value <= 1`. The dry-depth threshold suppresses the singular source
evaluation at dry faces; it does not add water or alter wet/dry policy.

Rejected candidates leave the conserved state, correction ledger, and accepted
source diagnostics unchanged. A source-bound rejection retries with the smaller
of half the prior step and the freshly evaluated bound. A bound below the
configured minimum step fails with a structured source-CFL reason. Output
alignment tails must satisfy both the wave and source limits. They are not
enlarged or silently accepted.
Positive source bounds use only
`min(8 * ulp(bound), 1e-12 * bound)` as roundoff allowance, with no absolute
time floor. If that allowance underflows to zero, comparison is strict. The
shared clock tolerance remains in use for time matching and statistics.
Manning result validation applies the same bound-scaled check to recorded
alignment steps and their source-aware CFL limit. The regression suite includes
a controlled residual injection to reach the runner's tiny-tail branch; that is
a solver-path test, not an unmodified physical scenario.
When positive drag produces a source limit that underflows to zero or overflows
to a non-finite value, the stage fails with a specific source-limit reason;
only an actual zero drag coefficient has an unbounded source limit.

Rainfall remains unsupported. Manning has no depth source, so the mass ledger's
source volume remains zero and its correction allowance remains based on the
initial state. This separation is consistent with the source treatment
described in [FullSWOF](https://arxiv.org/pdf/1401.4125).

## Configs and result

`examples/triangular_sources/manning_only_source_preparation.yaml` shows the
Manning interpretation and explicitly disables rainfall. After preparing that
artifact, `examples/triangular_sources/manning_run.yaml` selects
`sources: manning`, links the unchanged `sources: none` base config, and writes
a distinct result path. The runner validates the full config and refuses to
overwrite a base config, P2/P3-A input, source-preparation artifact, or base
result. Replacing an existing Manning result requires
`overwrite_existing_result: true`.

Manning output uses `triangular-manning-result-v1` and the product type
`experimental_triangular_manning_shallow_water_result`. It retains the P4-1
state, topology, mass ledger, and time-step diagnostics, with separate flags for
`manning_enabled`, `manning_applied`, `rainfall_enabled`, and
`rainfall_applied`. The output also records the config/preparation digests,
face-ordered SI `manning_n`, interpretation policy, source-CFL fraction and
limits, accepted source-CFL-limited steps, source-CFL rejections, and per-output
maximum `gamma` and source magnitude. A nonzero-effect flag is true only when
the accepted RK stages evaluated a nonzero Manning source.
The limited-step count includes non-tail steps selected by the source bound and
steps accepted after a second-stage source-CFL retry.
The reader accepts only the versioned metadata field set, checks duplicated
base fields against the validated base result, and reads state arrays from the
actual NetCDF variables.

The sample base config is a synthetic still-water case. Its source is applied
but the momentum is zero, so it correctly reports no nonzero Manning effect.
The Manning tests use a separate synthetic wet-bed dam-break package with
zero initial momentum to verify that hydrodynamic motion generates momentum
and Manning reduces it. That test is a source-effect check; its frictionless
Stoker comparison is not treated as a Manning accuracy target.
