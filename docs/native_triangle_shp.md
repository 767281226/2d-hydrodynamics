# Native triangular P4 result Shapefile

`hydrodynamics-triangle-shp` exports one polygon feature per face from an existing native P4-1 NetCDF result. The command does not run a solver. Before export or validation it rechecks the run config, current P3-A business package, linked P2 UGRID mesh, actual native arrays, case identity, topology and terrain by calling the existing P4-1 reader. Only `closed_wall`, `initial_momentum=zero`, `sources=none` is supported in this first adapter. Preparation-only UGRID, structured `time,y,x` NetCDF, source-enabled results, open boundaries and arrays that merely resemble P4 data are rejected.

Install and run the small example:

```powershell
python -m pip install -e ".[triangular,shp]"
hydrodynamics-mesh-prepare examples/native_triangle_shp/mesh_preparation.yaml
hydrodynamics-business-prepare examples/native_triangle_shp/business_preparation.yaml
hydrodynamics-triangular-run examples/native_triangle_shp/run.yaml
hydrodynamics-triangle-shp export --request examples/native_triangle_shp/export_frame.yaml
hydrodynamics-triangle-shp validate --request examples/native_triangle_shp/export_frame.yaml
```

Use `hydrodynamics-triangle-shp schema` for the installed request/manifest contracts. Request paths are relative to the request file; output must be a new child directory without path aliases. Existing outputs are never replaced. `validate` accepts `--package-directory` when the package was copied to another location, but still requires the original run config, P3-A package, P2 mesh and native result.

The request supports two selectors. `FRAME` requires an exact `frame_index` and `time_s` pair from the native saved-time axis. `MAX_OVER_WINDOW` requires saved-time endpoints and includes both; no interpolation is performed. Per-face equal maxima use the earliest native time. Scope is either `FULL_DOMAIN` or `MAXIMUM_WET_SURFACE`; the latter includes a face when its selected depth is strictly greater than `wet_depth_threshold_m` (finite, non-negative, default 0 m). An empty selection still produces a valid zero-feature Polygon layer.

## Native-web manifest schema

The native-web wire `version` remains `1.0`. Its current JSON Schema revision is 1.0.1 at [`native_web_manifest_v1_0_1.schema.json`](../examples/native_triangle_shp/native_web_manifest_v1_0_1.schema.json), with `$id` `native-triangle-web-manifest-v1.0.1`. The previous [`native_web_manifest.schema.json`](../examples/native_triangle_shp/native_web_manifest.schema.json) keeps its original v1 identity for historical consumers.

`data_origin` is optional for legacy v1 manifests. When present, its only declared value is `SYNTHETIC_SMALL_NATIVE_FIXTURE`, and the manifest must also say `qa_fixture=true`, `live_solver_ran=false`, and `status=qa_fixture_ready`. An absent field remains undeclared; the schema supplies no default and does not infer live or production status.

Every output feature preserves the native `face_id` as exact decimal text in DBF, avoiding numeric precision loss above `2**53`. Polygon vertices come directly from that face's three `face_nodes`; there is no rectangular conversion, remeshing, subdivision, merge or resampling. `FRAME` fields are `time_s`, `depth_m`, `water_lvl`, `ux_m_s`, `uy_m_s` and `area_m2`; `MAX_OVER_WINDOW` fields are `max_depth`, `max_time` and `area_m2`. The versioned manifest contains the full semantic field mapping and units. Water level requires the current P3-A vertical datum. Velocity components are momentum divided by depth and remain in the original projected CRS axes.

Native projected CRS geometry is the default. The optional `EPSG:4490` output transforms only the exported coordinates. `area_m2` always comes from native P4 `face_area` and is checked against triangle geometry in the source projected CRS; longitude/latitude area is never reported as square metres. The package manifest records the request/config/P2/P3-A/result digests, case/mesh/topology/terrain identities, selector/time axis, CRS, datum, exact face ID set digest, component hashes and byte counts. P4-1 has no platform attempt/script IDs; the manifest says so and does not invent them. Outputs remain `experimental=true`, `engineering_validated=false`, `production_approved=false`, and `can_publish=false`.

Publication is staged in a new directory and freshly validated after rename. If that final validation fails, an unchanged success manifest is retired only when its directory parent chain and file identity still match this export. A concurrently modified manifest is copied byte-for-byte to a unique `manifest.recovered.<id>.json` file before marker removal. A replaced output root or manifest inode is preserved untouched and its path is reported; the exporter does not recursively delete the package directory.

The four legacy structured grid Shapefile writer APIs remain importable and raise an explicit migration error before touching their target paths. The hourly structured workflow continues to emit its NetCDF, journal, nc2web, stepjson and viewer outputs; its SHP stage is `not_applicable` with reason `native_triangular_result_required`. Historical schema 1.0 structured bundles remain discoverable as `legacy_structured_rectangular_shp`; they are not classified as native triangle results.
