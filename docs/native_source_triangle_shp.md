# Native source-result triangle SHP v2

The existing v1 request, manifest, Python API, default `schema` command, and `sources=none` scope remain unchanged. v1 continues to reject Manning, rainfall, and combined-source results. Source results use an independent v2 request and manifest.

## Request and CLI

```yaml
format: native-triangle-result-shp-export-request-v2
schema_version: '2.0'
result_kind: rainfall # manning | rainfall | manning_and_rainfall
run_config_file: source_run.yaml
output_directory: source_result_shp_v2
selector:
  aggregation: FRAME
  frame_index: 2
  time_s: 0.1
  window_start_s: null
  window_end_s: null
scope_selection: FULL_DOMAIN
wet_depth_threshold_m: 0.0
target_crs: NATIVE # or EPSG:4490
```

`result_kind` is required and must match the top-level P4-2 config family and its `sources` value. Paths are normalized and relative to the request. Selector, scope, threshold, CRS, saved-time matching, exact int64 face-ID text, projected-area semantics, and geometry checks use the existing v1 rules.

The default `schema` command still prints v1. Use `schema-v2` for the independent source request/manifest schema. The existing `export` and `validate` commands dispatch by request `format`:

```powershell
python -m hydrodynamics.triangular_shp_export schema
python -m hydrodynamics.triangular_shp_export schema-v2
python -m hydrodynamics.triangular_shp_export export --request examples/security_exclusion/export_combined_v2.yaml
python -m hydrodynamics.triangular_shp_export validate --request examples/security_exclusion/export_combined_v2.yaml
```

The wheel ships the separate v2 schema at `hydrodynamics/shapefile_schemas/triangular_source_result_shp_v2.schema.json`; v1 remains at `triangular_result_shp.schema.json`.

## Source identity and budget checks

The adapter calls `read_triangular_manning_result` for Manning and `read_triangular_source_result` for rainfall or combined mode, with the configured-result-path check enabled. It does not call a runner or read a base-none result. Before export, during stage validation, and after publication, it re-reads the current source graph: request, top-level run config, base config, source config, prepared source artifact, P3 business package, P2 mesh, native result, and all security evidence dependencies. Each role records its path, byte length, and SHA-256. The manifest is `LOCAL_SOURCE_GRAPH`; it is not a portable or self-authenticating execution proof.

The v2 manifest records the native result format, product type, solver version, source family, and requested/enabled/applied/nonzero-effect flags. Manning provenance includes the unchanged `unit: 1` declaration, SI interpretation policy, physical unit and rationale. The writer independently integrates every saved depth frame using native face areas and compares the result with each native mass-ledger row. It records the maximum synchronous volume as the largest saved-frame integral; it never sums per-face maxima across different times. Window summaries use only saved frames selected by the exact inclusive window.

Rainfall interval acceptance is checked against `rate × active area × interval duration` and the native ledger. With security exclusion, accepted active-area rainfall and excluded-area potential rainfall remain separate. Excluded rainfall is explicitly unmodeled, is not accepted-budget loss, and is not transferred. `manning` zero-effect and all-zero rainfall still retain their requested/applied family, while their `nonzero_effect_applied` flags remain false.

The v2 writer preserves the existing no-overwrite, reparse-point, component-hash, fresh-validation, stage ownership, marker-retirement, and exception-propagation rules. Source-enabled results remain unsupported by the v1 writer. Risk, population, and loss outputs remain unsupported and are not inferred as zero.

## Synthetic examples

`examples/security_exclusion/` includes v2 requests for all three families, security-enabled rainfall, zero-effect cases, and a dynamic Manning/combined case. The CLI example stays within four faces and 0.1 s; it does not require an existing base-none result.
