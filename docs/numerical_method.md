# Numerical method interface (reserved)

This phase does not select governing equations, spatial discretization,
numerical flux, limiter, dry/wet-cell algorithm, or time integrator.

`numerics.scheme`, `numerics.flux`, and `numerics.time_integrator` are optional
non-empty strings. They are placeholders that allow a future engine to expose
method selection without changing the case shape. `null` in the example means
“not selected yet”. No value is interpreted by the current Python package.

The general controls have schema-level sanity checks only:

- `cfl`: dimensionless number in `(0, 1]`;
- `min_depth`: non-negative metres;
- `dry_depth`: non-negative metres and no larger than `min_depth`.

These checks do not define a physical dry-state algorithm. The later numerical
design must document which methods accept these controls and whether their
legal ranges differ.
