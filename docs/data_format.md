# Data file format contract

The current loader validates references but does not open external data. The
following is the contract that future readers should implement.

## Paths

Paths in `config.yaml` are normally relative to the case directory. The future
case-preparation layer must resolve them and report the full path when a file
is missing. The loader itself only validates that a path value is a non-empty
string.

## Raster inputs

`terrain.file`, `roughness.file`, `initial_condition.depth_file`, and
`initial_condition.level_file` refer to raster grids (the example uses
GeoTIFF). Readers must eventually verify dimensions, cell size, extent,
coordinate system, and nodata compatibility with `domain`.

Expected quantities and units:

- DEM/terrain elevation: metres (`m`).
- Initial depth: metres (`m`).
- Initial water level: metres (`m`) in the configured vertical datum.
- Manning roughness: dimensionless Manning coefficient.
- Initial velocity `x_file`/`y_file`: metres per second (`m/s`).

## Time-series CSV inputs

The default columns are `time,value`; `TimeSeriesRef` permits overriding their
names. Files should have a header row and numeric rows sorted by increasing
time. The default time unit is seconds from `model.start_time`.

Value units are defined by the owning field until the open unit-metadata
decision is resolved:

- discharge boundary: cubic metres per second (`m³/s`);
- water-level boundary: metres (`m`);
- rainfall: millimetres per hour (`mm/h`) by default.

Interpolation, extrapolation, duplicate timestamps, and missing-value policy
are intentionally deferred to the data-reader design.
