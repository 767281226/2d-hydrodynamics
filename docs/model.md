# Model and case architecture

One simulation is represented by a case directory. `config.yaml` is the
control plane; `data/` contains external raster/time-series inputs; `results/`
is reserved for generated outputs.

The top-level configuration has ten sections:

1. `model` — case identity, time window, gravity, CRS, and unit system.
2. `domain` — currently a structured rectangular grid spacing.
3. `terrain` — raster or constant bed elevation.
4. `initial_condition` — initial depth/level and a reserved velocity field.
5. `roughness` — constant or raster Manning roughness.
6. `boundary` — an extensible list of boundary objects.
7. `source` — currently only a disabled/enabled rainfall placeholder.
8. `numerics` — method placeholders and general thresholds.
9. `output` — interval, variables, destination, and format.
10. `validation` — validation switches and report preference.

The loader validates structure and values but deliberately does not open the
referenced GeoTIFF/CSV files. A future case-preparation layer should resolve
paths, inspect raster dimensions/CRS, and check cross-file compatibility.
