# TODO and decisions to confirm

The following items are intentionally not decided in this phase. They should
be confirmed before implementing a solver or data readers.

## Contract decisions

- Resolve the duplicate output interval fields: `model.output_interval` and
  `output.interval`. The current schema permits both only when equal and treats
  `output.interval` as canonical.
- Decide whether future non-structured grids need an additional extent contract.
- Define the accepted coordinate-system syntax and whether reprojection is
  performed by the engine.
- Decide whether time-series value units must be repeated in each file
  reference or remain fixed by the owning field (currently documented by
  context).
- Define path resolution, allowed path roots, and whether missing referenced
  files are checked during configuration loading or during case preparation.
- Define the behavior of the reserved `nearest` and `interpolate` NoData strategies.
- Confirm whether `vertical_datum_required` should be a hard run precondition and
  how a confirmed datum value is supplied by the caller.
- Confirm DEM metadata tolerances for `auto` (resolution, CRS, extent, and pixel
  alignment) and the handling of mixed-resolution axes; V1 currently rejects
  undefined cases rather than guessing.
- Confirm the initial-condition representation for a water-level raster and
  the exact velocity-field metadata.
- Confirm output formats and variable-to-format compatibility. The schema lists
  `netcdf`, `geotiff`, and `csv` as interface values only; exporters do not yet
  exist.

## Numerical and physical decisions

- Choose the governing equations and numerical discretization.
- Choose concrete values for `numerics.scheme`, `numerics.flux`, and
  `numerics.time_integrator`.
- Define dry/wet-cell semantics for `min_depth` and `dry_depth`.
- Define boundary placement and orientation conventions.
- Define rainfall, infiltration, evaporation, pump, gate, and weir physics.
- Define validation algorithms, tolerances, and report format.

## Engineering decisions

- Set the supported Python versions and dependency lock strategy.
- Add raster and time-series readers after their contracts are approved.
- Implement TerrainMapper only after DEM coverage, CRS, and resampling contracts
  are approved; the current class is an explicit placeholder. Constant terrain
  mapping behavior is intentionally outside the DEM mapper boundary.
- Add solver-facing interfaces and result writers without coupling them to the
  YAML parser.