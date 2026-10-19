# Triangular mesh preparation (P0–P2)

This is a standalone input-preparation path. It imports or generates a real
constrained triangular mesh, maps raw DEM samples to faces by overlap area,
and writes a UGRID NetCDF preparation artifact. It does **not** start either
existing solver and does not produce water depth, flow, or simulation output.
The regular-grid `DomainConfig`, hourly inundation replay, minimal structured
shallow-water solver, and their structured outputs keep their current paths.

## Install and run

The core input contract uses the repository dependencies. The UGRID writer and
projected-CRS checks are optional extras; Gmsh is separately optional:

```text
pip install ".[triangular]"
pip install ".[triangular,gmsh]"  # only when using mesh.type: gmsh
python -m hydrodynamics.mesh_preparation examples/triangular_mesh/preparation.yaml
```

The `hydrodynamics-mesh-prepare` console entry point accepts the same one YAML
path. Paths in the YAML are resolved against the YAML file. The import example
is self-contained and uses only tiny synthetic JSON data. Its `mesh.json`
has a non-rectangular outer ring, an unfilled central hole, outer/hole boundary
groups, a named internal constraint, and stable face IDs. `dem.json` is a tiny
synthetic 5-by-4 raster. The separate `gmsh_hole_refinement.yaml` demonstrates
the optional generator with a non-rectangular boundary, hole, embedded line,
and a local point size field.

## Strict YAML configuration

Unknown keys, implicit string/boolean-to-number conversions, unsupported
versions, CRS errors, bad topology, and paths that do not exist produce
actionable validation errors. The typed runtime schema is
`MeshPreparationConfig`; `load_mesh_preparation_config` is its strict YAML
loader. Pydantic's JSON Schema is available from
`MeshPreparationConfig.model_json_schema()`.

| Input | Type | Unit | Default / required | Legal range or validation |
| --- | --- | --- | --- | --- |
| `version` | integer | — | default `1` | exactly `1`; unsupported config versions fail |
| `coordinate_system` | string | CRS identifier | required | recognized projected CRS; first two axes must be metres |
| `mesh.type` | `json` or `gmsh` | — | required | selects exactly one mesh source |
| `mesh.file` (`json`) | string path | — | required | relative to YAML; file must be version 1 mesh JSON |
| `mesh.outer_boundary` (`gmsh`) | list of 2-number points | m | required | at least 3 distinct vertices; finite, non-self-intersecting ring |
| `mesh.holes` (`gmsh`) | list of rings | m | default `[]` | each ring has at least 3 distinct vertices strictly within the outer ring; holes may not intersect or overlap |
| `mesh.boundary_groups` (`gmsh`) | list of `{name, points, closed}` paths | m | default `[]`; `closed` defaults to `false` | names unique; open paths connect consecutive points only; `closed: true` adds the final-to-first edge; every segment must match a configured boundary edge |
| `mesh.constraints` (`gmsh`) | list of open `{name, points}` paths | m | default `[]`; `closed` must be `false` | names unique; finite line paths within the region; crossings, T-junctions, self-touches and collinear foldbacks are rejected |
| `mesh.default_size_m` (`gmsh`) | number | m | required | `0 < value <= 10,000` |
| `mesh.minimum_size_m` (`gmsh`) | number | m | default `1.0` | `0 < value <= 10,000`, no greater than `default_size_m`, and greater than `geometry_tolerance_m` |
| `mesh.refinement_points` (`gmsh`) | list of `{point, size_m, radius_m}` | m | default `[]` | point has two finite coordinates; `minimum_size_m <= size_m <= default_size_m`; `0 < radius_m <= 100,000` |
| `mesh.maximum_nodes` (`gmsh`) | integer | nodes | default `2,000,000` | `3..10,000,000`; generation fails if exceeded |
| `terrain.type` | `json` or `geotiff` | — | required | `geotiff` uses the raw-value GeoTIFF reader |
| `terrain.file` | string path | — | required | relative to YAML; missing/invalid source fails |
| `terrain.minimum_valid_coverage` | number | fraction | default `0.0` | `[0,1]`; zero-valid-area faces always fail; partial coverage uses valid samples only |
| `terrain.vertical_unit` | literal `m` or null | — | default `null` | may declare metres only when DEM metadata lacks a unit; non-metre source units fail because conversion is not implemented |
| `terrain.vertical_datum` | string or null | — | default `null` | supplements missing DEM datum metadata; conflicting source metadata fails; no conversion is performed |
| `output_file` | string path | — | required | relative to YAML; parent directory is created |
| `minimum_face_quality` | number | fraction | default `0.0` | `[0,1]`; rejects generated/imported faces below the threshold |
| `maximum_faces` | integer | faces | default `500,000` | `1..10,000,000`; imported/generated meshes above the limit fail |
| `maximum_dem_pixels` | integer | pixels | default `1,000,000` | `1..100,000,000`; GeoTIFF metadata is checked before raw values are read |
| `maximum_overlap_checks` | integer | face-pixel pairs | default `20,000,000` | `1..1,000,000,000`; face count times DEM pixel count above the limit fails before mapping |
| `geometry_tolerance_m` | number | m | default `1e-8` | `(0,1]`; nodes/segments at or within tolerance are treated as coincident and rejected where distinct geometry is required |
| `minimum_face_area_m2` | number | m2 | default `1e-12` | `(0,100]`; faces at or below this area fail |

Every coordinate is already in `coordinate_system`. This path rejects a DEM
whose CRS differs from the mesh and never reprojects it. The CRS must be
projected with metre axes. The generator sizes are target edge lengths; they do
not reuse the structured `dx` or `dy` fields. A refinement point creates a
Gmsh distance/threshold size field that returns to the default size at
`radius_m`.

## Mesh JSON v1

`format` must be `triangular-mesh-json-v1`; required arrays are `node_xy` and
`face_nodes`. `face_nodes` uses zero-based integer indices and positive
counter-clockwise orientation. An optional `coordinate_system` must be
equivalent to the config CRS. `face_id` may contain stable non-negative int64
IDs; otherwise IDs are derived deterministically from the mesh identity and
face node indices. `mesh_id` is kept on round-trip; if omitted it is derived
from geometry. `boundary_groups` and `constraint_groups` map names to lists of
two-node mesh edges. Boundary groups may reference only edges with one face;
constraint groups may contain boundary or internal mesh edges. Group labels
have no implicit boundary physics.

The loader rejects nodes closer than `geometry_tolerance_m`, repeated faces, non-finite coordinates, non-integer
indices, out-of-range indices, zero-area/reversed faces, duplicate coordinates,
non-manifold edges, same-direction face incidence, positive-area overlaps,
and group references that do not match actual edges. It then derives unique
edges, edge-to-face and face-to-edge connectivity (`-1` for an exterior
neighbor), edge lengths, face areas and centroids, normalized quality, and
minimum angles. Quality reports distinguish `measured_minimum_face_area_m2`
from `minimum_face_area_threshold_m2`, and `measured_minimum_quality` from
`minimum_quality_threshold`; UGRID globals use the same names.
Stable external IDs are distinct from zero-based topology indices.

## Terrain mapping and NoData

The mapper consumes an already-read `DEMDataset`; it is outside the GeoTIFF
reader. It intersects each triangle with source affine pixel polygons and
computes a valid-area-weighted mean. Elevations are emitted as metres only if
DEM metadata declares metres or the config explicitly declares metres when
metadata omitted the unit. Non-metre units and conflicting vertical-datum
declarations fail. The face field retains whether unit and datum came from DEM
metadata or an explicit config declaration; no unit or datum conversion is
performed. `terrain_covered_area` counts all raster
overlap; `terrain_valid_area` and `terrain_coverage_ratio` exclude masked,
NoData, and non-finite pixels. NoData values are never replaced with zero or
interpolated. A face with no valid overlap fails. Partial overlap is accepted
only when its valid-area fraction meets `minimum_valid_coverage`. The initial
implementation targets small preparation rasters; large-window mapping and
engineering DEM performance are deferred.

`terrain.type: json` is a tiny synthetic fixture format (`synthetic-dem-json-v1`)
with a 6-value affine transform `[a,b,c,d,e,f]`, values, optional boolean
`valid_mask`, CRS, and optional vertical metadata. `terrain.type: geotiff`
preserves the existing reader behavior: raw source values and masks only.

## UGRID output

The output includes a `mesh2d` topology variable, node coordinates, zero-based
face and edge connectivity, edge/face adjacency, stable `face_id`, edge length,
face area/quality/minimum angle and coordinates, terrain elevation, valid mask,
covered and valid areas, and coverage. Boundary and constraint groups are stored as JSON mappings from
names to node-pair edges. Global attributes identify the file as
`mesh_and_terrain_preparation`, set `preparation_only=1` and
`simulation_result=0`. The file also carries the area and quality thresholds
separately from measured minimum values. Static values are on `(face)`; there are no time
dimensions or fabricated simulation variables. `read_ugrid_preparation`
checks the marker, connectivity index base, stored adjacency, and derived
length/area/quality/centroid values, integer connectivity and IDs, terrain
flags, `m` elevation units, face locations, and parseable CF CRS WKT before
reconstructing the same topology and face data. Vertical globals retain the
resolved datum/unit, raw source metadata values, and whether each value came
from DEM metadata or an explicit config declaration.

Connectivity, face IDs, and the validity mask are stored as raw integers.
The reader rejects `scale_factor` and `add_offset` on these variables before
decoding them, including identity values: topology and flags cannot use packed
numeric storage in this preparation format. Floating-point terrain fields are
not affected by this restriction.

## Future interfaces (P3/P4)

P3-A now has independent preparation contracts for constant and face-ID
roughness, depth/absolute-level initial fields, and time-varying boundary-group
bindings. It retains the P2 mesh fingerprint, stable face IDs, masks, unit and
vertical-datum provenance. See
[triangular_business_preparation.md](triangular_business_preparation.md).

P3-B still needs source-face/target-face intersection records, area fractions,
aggregation rules for total versus density fields, duplicate/partial coverage
policies, approval/provenance handling, and native triangular GIS output.
Copying a population total to each overlapping face would double-count it.

P4 requires separate design decisions for the hourly inundation replay and the
structured shallow-water solver. Future adapters will need mesh/field identity
and topology checks, face-area-weighted volume accounting, edge-neighbor
traversal, and edge-based boundary association. The shallow-water path also
needs explicit face-normal flux, source-term, CFL/time-step, dry/wet, and
conservation/convergence decisions. The existing structured HLL/SSPRK2 choice
does not define those decisions. Until those decisions and their validations
exist, the default workflow and either solver must not accept a mesh-preparation
artifact as a calculation grid.
