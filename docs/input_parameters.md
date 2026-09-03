# Input parameter contract

`hydrodynamics.config.SimulationConfig` is the authoritative typed schema.
Unknown keys are rejected. Relative file paths are strings at this stage and
are not opened by `load_config`.

## Top-level sections

All ten sections are required: `model`, `domain`, `terrain`,
`initial_condition`, `roughness`, `boundary`, `source`, `numerics`, `output`,
and `validation`.

## Parameter summary

| Path | Type / enum | Unit | Default | Required | Range / rule |
|---|---|---:|---:|:---:|---|
| `model.name` | string | — | — | yes | non-empty |
| `model.description` | string or null | — | null | no | non-empty when present |
| `model.gravity` | number | m/s² | 9.81 | no | > 0 |
| `model.start_time` | number | s | 0 | no | ≥ 0 |
| `model.end_time` | number | s | — | yes | > `start_time` |
| `model.output_interval` | number or null | s | null | no | > 0; if set, equals `output.interval` |
| `model.coordinate_system` | string | CRS identifier | — | yes | non-empty; syntax not yet restricted |
| `model.units` | `SI` | — | `SI` | no | only `SI` currently |
| `domain.type` | `structured` | — | `structured` | no | only structured currently |
| `domain.dx`, `domain.dy` | number | m | — | yes | > 0 |
| `domain.nx`, `domain.ny` | integer or null | cells | null | no | > 0 when present |
| `terrain.type` | `raster` / `constant` | — | — | yes | selects required fields |
| `terrain.file` | string or null | path | null | conditional | required for raster |
| `terrain.nodata` | number or null | dataset value | null | no | metadata only |
| `terrain.elevation` | number or null | m | null | conditional | required for constant |
| `initial_condition.type` | `constant_depth` / `constant_level` / `raster` | — | — | yes | selects required fields |
| `initial_condition.depth` | number or null | m | null | conditional | ≥ 0 for constant depth |
| `initial_condition.level` | number or null | m | null | conditional | required for constant level |
| `initial_condition.depth_file` | string or null | path | null | conditional | one raster field for raster type |
| `initial_condition.level_file` | string or null | path | null | conditional | mutually exclusive with depth file |
| `initial_condition.velocity` | object or null | m/s | null | no | reserved; constant or raster |
| `roughness.type` | `constant` / `raster` | — | — | yes | selects required fields |
| `roughness.manning_n` | number or null | dimensionless | null | conditional | > 0 for constant |
| `roughness.file` | string or null | path | null | conditional | required for raster |
| `boundary` | list of objects | — | — | yes | at least one; unique IDs |
| `boundary[].id` | string | — | — | yes | non-empty and unique |
| `boundary[].location` | `west` / `east` / `north` / `south` | — | — | yes | structured-grid sides |
| `boundary[].type` | `discharge` / `water_level` / `wall` / `timeseries` | — | — | yes | type-specific rules |
| `boundary[].value` | number or null | m³/s or m | null | conditional | ≥ 0; discharge or water level |
| `boundary[].timeseries` | object or null | context-dependent | null | conditional | file reference |
| `boundary[].quantity` | `discharge` / `water_level` or null | — | null | generic timeseries | required for type `timeseries` |
| `source.rainfall.enabled` | boolean | — | false | no | if false, omit type/file |
| `source.rainfall.type` | `timeseries` or null | — | null | conditional | required when enabled |
| `source.rainfall.file` | string or null | path | null | conditional | required when enabled |
| `source.rainfall.time_unit` | `s` | — | `s` | no | only seconds currently |
| `source.rainfall.intensity_unit` | string | mm/h by default | `mm/h` | no | non-empty; physical conversion TBD |
| `numerics.scheme` | string or null | — | null | no | placeholder, not an algorithm choice |
| `numerics.flux` | string or null | — | null | no | placeholder |
| `numerics.time_integrator` | string or null | — | null | no | placeholder |
| `numerics.cfl` | number | dimensionless | 0.5 | no | 0 < CFL ≤ 1 |
| `numerics.min_depth` | number | m | 0.001 | no | ≥ 0 and ≥ `dry_depth` |
| `numerics.dry_depth` | number | m | 0.0001 | no | ≥ 0 |
| `output.directory` | string | path | `results` | no | non-empty |
| `output.interval` | number | s | 60 | no | > 0 |
| `output.variables` | list of enum values | — | `[water_depth]` | no | non-empty, no duplicates |
| `output.format` | `netcdf` / `geotiff` / `csv` | — | `netcdf` | no | interface values only |
| `validation.enabled` | boolean | — | false | no | — |
| `validation.mass_balance_check` | boolean | — | false | no | requires enabled |
| `validation.report` | boolean | — | true | no | — |

The reserved velocity object uses `type: constant` with `x`/`y` in m/s, or
`type: raster` with `x_file`/`y_file`. It is a schema extension point only.
