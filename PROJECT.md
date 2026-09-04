# 2-D Hydrodynamics

## Current phase

This project is in the **input parameter and configuration interface design
phase**. It is not a hydrodynamics solver yet.

## Target

Provide a reusable calculation engine that another Python program, a CLI, or a
future software integration can call with a case directory and a validated
configuration. Different input data and parameters should describe different
simulation scenarios without changing engine code.

## Architecture boundary

The current package contains a typed configuration contract, YAML loader, and a
non-computational DEM mapping boundary. It does not read GeoTIFF/CSV contents,
perform CRS conversion or resampling, build a solver grid, run a time step, or
produce results. Those responsibilities will consume this contract later.

## Case convention

```text
case/
├── config.yaml
├── data/
│   ├── dem.tif
│   ├── manning.tif
│   ├── initial_depth.tif
│   ├── initial_velocity.tif
│   ├── inflow.csv
│   ├── water_level.csv
│   └── rainfall.csv
└── results/
```

## Design principles

- Configuration and solver implementation are separate.
- Unknown keys and invalid combinations fail before a run starts.
- Relative paths are interpreted relative to the case directory by the future
  engine; the current loader intentionally does not open referenced data.
- Numerical scheme, flux, and time-integrator names remain placeholders.
- Terrain mapping strategies and NoData policies are declared, but actual DEM
  mapping remains unimplemented.
- DEMMetadata/DEMValidator define a dependency-free metadata contract; the real DEM
  is used only for compatibility validation and is not part of source control.
- V1.0 grid resolution is 30–100 m; nx/ny are computed internally from the
  explicit domain extent and dx/dy.

See [TODO.md](TODO.md) for decisions intentionally left open.
