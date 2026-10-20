# Security-area exclusion input

This contract keeps the original computational domain `D` separate from the union of protected areas `S`; the active mesh domain is `D \\ S`. Protected polygons are removed from the face mesh, and their new edges use the existing `closed_wall` behavior. This adds no levee height, overtopping, gate control, dry/wet rule, or source physics.

## Input format

`security_exclusion` may be omitted from P2 or workflow configuration for an unconfigured case. When enabled, provide the complete versioned input:

```yaml
security_exclusion:
  format: security-area-exclusion-v1
  schema_version: '1.0'
  enabled: true
  source_file: security_area.geojson
  source_crs: EPSG:32650
  constraint: excluded_closed_wall
```

`enabled` is a required boolean, `source_file` is a non-empty path relative to the declaring configuration, `source_crs` is an explicit CRS declaration, and `constraint` currently accepts only `excluded_closed_wall`. Areas are measured in m². Source geometry must be a valid, non-empty Polygon/MultiPolygon with finite coordinates. Missing or conflicting CRS, invalid geometry, and self-intersections fail without silent repair. For Shapefiles, the geometry reader opens `.shp`, `.shx`, and `.prj`; it never reads DBF attributes.

If the GeoJSON root declares `crs`, its `name`/`EPSG` declaration must parse and be equivalent to `source_crs`; conflicting, malformed, or unrecognized declarations fail. When the GeoJSON has no embedded CRS, the required `source_crs` is used without inference.

An imported mesh also needs an explicit original domain `D`; the security polygon's bounding box cannot stand in for it:

```yaml
  domain_geometry_file: domain.geojson
  domain_crs: EPSG:32650
```

The GeoJSON uses `security-domain-geometry-v1`; its embedded `coordinate_system` must match the declared `domain_crs`. The Gmsh route constructs `D` from the configured outer boundary and holes before meshing `D \\ S`. Cases the adapter cannot express, including unsupported multiple components or boundary cuts, fail with an explicit `UNSUPPORTED` error. Imported meshes are checked face by face for geometric overlap and exact domain coverage; face-center clipping is not used.

## Identity and execution

P2 stores source and CRS digests, normalized geometry, original/excluded/active areas, the active face-set digest, coverage residual, and the actual security-boundary edge group. P3 must carry evidence identical to P2. Before execution, none, Manning, and rainfall P4 routes reread the sources and verify the active faces. A changed security input or mesh invalidates old P3 inputs, results, and SHP exports.

Constant initial depth is defined over active faces. Face assignments that refer to an excluded face ID fail; values are never silently dropped and redistributed, and old whole-domain volume is never re-averaged. Uniform rain is accepted over actual active area. Potential rain over the excluded area is reported in `security_rainfall_accounting.security_excluded_unmodeled_rainfall_volume_m3`; it is unmodeled rain, not a loss from the accepted water budget, and it is not transferred into the active domain.

Native results contain active faces only. The native SHP manifest binds the security-evidence digest and the same active face-ID set. Structured `nc2web`/stepjson encoders cannot represent native triangular security results and reject them explicitly. Risk, population, and loss statistics that are not implemented remain `UNSUPPORTED`, never zero.

`examples/security_exclusion/` is a small synthetic two-room case (4 faces, 0.1 s) with an imported mesh, original domain, exclusion geometry, P2/P3 configs, none and rainfall configs, and an SHP request. Paths are relative to that directory. `examples/case_001/workflow.yaml` explicitly references `../../map/securityArea/dd_securityArea_shape.shp`; the current structured/hourly workflow rejects it before model execution with `UNSUPPORTED_STRUCTURED_SECURITY_EXCLUSION` to prevent an unprotected run.
