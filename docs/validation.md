# Configuration validation

Validation is performed before any future solver is called.

## Layers

1. YAML syntax is parsed with `yaml.safe_load`.
2. Pydantic models enforce types, enums, required fields, ranges, and rejection
   of unknown keys (`extra="forbid"`).
3. Model validators enforce cross-field rules such as selected source fields,
   unique boundary IDs, time ordering, and matching output intervals.
4. Raster/CSV existence and metadata checks, DEM coverage/alignment checks, and
   actual mapping are intentionally deferred to a future case-preparation layer.
5. `TerrainField` validates `(ny, nx)` elevation/mask shape; `TerrainMapper`
   validates its interface and then raises an explicit not-implemented error.

Use `ConfigLoadError` for missing/unreadable/invalid-YAML files and
`ConfigValidationError` for a syntactically valid YAML document that violates
the schema. The latter includes a list of Pydantic error dictionaries in
`.errors` and a readable message such as:

```text
Invalid simulation configuration:
- terrain.file: file is required when terrain.type is 'raster'
```

No numerical or physical validation is claimed by this package yet.
