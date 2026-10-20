# Synthetic security-exclusion example

This four-face projected mesh covers a 4 m by 2 m domain. A 1 m wide corridor through the center is excluded, leaving two active rooms with 6 m² total area. The excluded corridor has 2 m². The example uses only synthetic geometry and elevation values.

From this directory, prepare the P2 mesh, P3 fields, and native closed-wall result:

```powershell
python -m hydrodynamics.mesh_preparation mesh_preparation.yaml
python -m hydrodynamics.triangular_business business_preparation.yaml
python -m hydrodynamics.triangular_solver run.yaml
python -m hydrodynamics.triangular_shp_export export --request export_request.yaml
```

To prepare and run the uniform rainfall variant after the native none run:

```powershell
python -m hydrodynamics.triangular_sources source_preparation.yaml
python -m hydrodynamics.triangular_source source_run.yaml
```

The initial depth is 0.2 m on active faces, for 1.2 m³. Rain at 0.0001 m/s for 0.1 s adds 0.00006 m³ over the 6 m² active mesh. Potential rain over the excluded 2 m² is reported separately as 0.00002 m³ of unmodeled rain.

The strict input schema is in `security_exclusion_input.schema.json`; the workflow schema also embeds this input under `security_exclusion`.

## Source-only SHP v2

The source runners below read P2/P3 and their preparation artifacts directly. They do not require or create `base_none_dynamic.nc`.

```powershell
python -m hydrodynamics.triangular_business business_dry_preparation.yaml
python -m hydrodynamics.triangular_sources manning_zero_source_preparation.yaml
python -m hydrodynamics.triangular_manning manning_zero_run.yaml
python -m hydrodynamics.triangular_shp_export export --request export_manning_zero_v2.yaml

python -m hydrodynamics.triangular_business business_dynamic_preparation.yaml
python -m hydrodynamics.triangular_sources manning_source_preparation.yaml
python -m hydrodynamics.triangular_manning manning_run.yaml
python -m hydrodynamics.triangular_shp_export export --request export_manning_v2.yaml

python -m hydrodynamics.triangular_sources combined_source_preparation.yaml
python -m hydrodynamics.triangular_source combined_run.yaml
python -m hydrodynamics.triangular_shp_export export --request export_combined_v2.yaml

python -m hydrodynamics.triangular_sources rainfall_zero_source_preparation.yaml
python -m hydrodynamics.triangular_source rainfall_zero_run.yaml
python -m hydrodynamics.triangular_shp_export export --request export_rainfall_zero_v2.yaml
```

`export_rainfall_v2.yaml` exports the security-enabled nonzero-rain case. `export_rainfall_zero_v2.yaml` exports the all-zero rainfall case; both remain `result_kind: rainfall`, with `rainfall_nonzero_effect_applied` reflecting the actual source effect.
