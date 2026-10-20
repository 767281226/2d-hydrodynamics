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
