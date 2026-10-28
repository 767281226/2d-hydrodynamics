# Derived D-exterior source contract (manifest 1.1)

This document describes the synthetic, geometry-only D-exterior source branch for the restricted mesh-only adapter. It adds no hydraulic boundary meaning and does not change the solver.

## Compatibility and component sources

Manifest schema 1.0 remains the original file-backed contract. Its component fields, strict unknown-field rejection, three-component minimum, and line-file requirement remain unchanged.

Schema 1.1 keeps the same root format and three-component minimum. Each component selects one explicit source branch:

- A file-backed component uses source_kind=file, the existing polygon or line format, source_crs, files, and a required source_version identifier. Each file identity binds the staged relative path, SHA-256, stat identity, and an origin record with its own relative path, SHA-256, and stat identity. The original bytes and staged copy must match and must have distinct physical (device,file_id) identities. Path aliases and hard links are rejected across all declared original and staged files, including across components; files and bytes are not silently deduplicated for budget accounting. Original and staged files are both checked before and after geometry loading; both count toward the 4 MiB per-file, 16 MiB aggregate, and 40-file limits.
- A derived component uses source_kind=derived and format=derived_domain_D_exterior_line_v1. It has no files, source CRS, coordinates, geometry hash, or caller-provided provenance hash. It must name the actual component_id of the file-backed domain_D parent.

A manifest still requires exactly one D, exactly one S, and at least one source line. A derived line does not make a two-component D/S manifest valid by itself; the declaration is the required third component. The JSON Schema branches use oneOf; runtime validation also checks parent resolution, unique component and group IDs, parent role/source kind, and source identity consistency.

## Derived line fields

The derived component requires:

| Field | Type and accepted value |
|---|---|
| component_id | Required safe ASCII identifier, 1–64 characters |
| source_kind | Required literal string derived |
| role | Required literal string boundary_line |
| format | Required literal string derived_domain_D_exterior_line_v1 |
| parent_component_id | Required safe component ID resolving to the unique file-backed D |
| group_name | Required safe 1–64 character identifier, unique and not outer_boundary, security_exclusion_boundary, or hole_N |
| closed | Required strict boolean true |
| geometry_only | Required strict boolean true |
| q_binding | Required literal string NONE |

NONE is not a zero flow, wall rule, cross-section, or hydraulic boundary. The line is used only as a geometry group.

## Worker derivation

The worker verifies all original and staged parent-D sidecars, including optional DBF and CPG bytes. It does not read DBF records. It validates the raw PRJ/WKT against the explicit source CRS, checks the target and fixed XY coordinate policy, disables PROJ networking, selects a best-available operation with ballpark disallowed, and records the actual CoordinateOperation chain and its explicit ballpark flags. Missing operation evidence fails closed.

For schema 1.1 parent D, the worker inspects the raw Shapefile header and every record shape type before calling the shared geometry reader. The header and records must all be XY Polygon type 5, and record content lengths must match the XY layout. PolygonZ, PolygonM, mixed header/record types, and extra non-XY payload fail before PyShp can expose a flattened GeoJSON interface. A parent-D GeoJSON source receives the same raw position-dimension check; each position must contain exactly two finite numbers. This stricter check is limited to schema 1.1 parent D; schema 1.0 and other security geometry readers retain their existing behavior. The dimension proof is included in the parent audit and derived-line provenance.

After the accepted transform, the worker validates every D feature, performs the existing full-feature union, and requires one valid Polygon. It takes that union's exterior ring in order, retaining XY coordinates and the explicit closing point. It does not use a D interior ring, S, or D−S edge. It does not simplify, densify, reverse, rotate, buffer, offset, repair, or select a multipart member. Empty, invalid, non-finite, 3D, multipart, over-cap, or otherwise unsupported D input fails with no MESH_READY. The existing D−S and line-coverage audit must also accept the result; an S area that cuts the exterior line is rejected.

The maximum exterior sequence is 250,000 XY points including the closing point. This bounds geometry hashing and audit work; it is not a mesh-capacity guarantee.

G0's recipe area check computes shoelace terms after translating each ring to its first vertex. This changes only the area calculation used by the audit; coordinates, ring order, datum, mesh settings, and the existing 1e-12 comparison tolerance remain fixed.

## Hashes and semantic trust

The canonical geometry hash format is triangle-domain-D-exterior-xy-m-f64le-v1:

1. ASCII bytes for the format name followed by one NUL byte.
2. The ordered point count as an unsigned 64-bit little-endian integer.
3. Every ordered X,Y pair as two little-endian IEEE-754 float64 values. Negative zero is normalized to positive zero. The closing point is included.

The geometry digest contains no path, timestamp, source stat, or run identity. A separate canonical provenance record binds the rule/version, actual D component ID and role, source version, every original/staged sidecar SHA/stat/path, raw source CRS, target CRS, actual operation and dependency/database identities, the transformed-all-features-then-union stage, D-union hash, ordered point count, closure, group, role, geometry-only flag, and Q=NONE. Its provenance SHA-256 is computed over canonical UTF-8 JSON with sorted keys, compact separators, and the canonical trailing newline. Neither digest is accepted from the manifest.

The worker writes those records to the source audit. The trusted-context semantic reader reopens the externally bound config and sources, rebuilds D, repeats the derivation, and compares the exact ordered geometry hash, provenance, line role, group, and Q binding. Re-signing a forged audit or changing the derived declaration cannot promote bundle data to trusted source. The identity-only bundle reader remains explicitly non-semantic.

## Resource and failure contract

The existing one-worker Windows Job limits remain: 2 GiB memory, 900 seconds, 1 GiB output, and 16 MiB scratch. The source manifest remains at most 1 MiB; each physical original or staged file is at most 4 MiB; original plus staged source bytes are at most 16 MiB and files at most 40; manifests contain 3–8 components. Mesh and source audit artifacts each remain capped at 16 MiB. No limit is raised automatically.

The parent/staging paths use the same owned-root relative resolver; absolute paths, parent traversal, links, reparse points, duplicate physical files, missing sidecars, origin/copy drift, and path escape fail. Cancellation, process exit, parse/CRS/geometry failure, write failure, and budget failure leave no MESH_READY, READY, or latest marker. Publication remains no-clobber.

## Boundary group coverage

The mesh adapter checks each boundary group against the exact boundary of D−S first. A complete line whose exact difference from that boundary is empty is accepted directly. If the line is not an exact subset, the existing tolerance-buffer check still applies; the tolerance is not increased and the adapter does not shorten lines. A group marked closed includes its last-to-first segment whether or not the repeated closing point is present in the input point list.

This check is limited to boundary groups. The active-domain type, source validation, and constraint/exclusion checks remain in force. Lines that cross S, cross a hole, leave the active boundary, or extend beyond the existing tolerance continue to fail.

The CLI summary is ASCII-safe JSON, so Unicode diagnostics remain recoverable on GBK and UTF-8 consoles and the process returns the worker's original exit code.

## Synthetic usage

Install the bounded mesh-only dependencies with:

    pip install ".[domain-mesh-only]"

Run the synthetic derived-line route with:

    python examples/triangular_mesh/run_domain_mesh_only_synthetic.py --project-root . --experiment-dir work/derived-line-smallcase --derived-domain-exterior

The example uses only tiny synthetic vector files. It does not read real D/S, DEM pixels, perform terrain mapping, or call the solver. The existing default example continues to exercise the schema 1.0 file-backed line route.

