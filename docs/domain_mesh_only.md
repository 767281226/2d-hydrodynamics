# Restricted domain mesh-only preparation

`triangle-domain-mesh-only-config-v1` is a separate generate-only route for a trusted dispatcher to create a diagnostic mesh. It does not accept the P0/G0 pilot config format, terrain settings, or solver settings. A successful run produces `domain-mesh.json`, `domain-source-audit.json`, an identity manifest/status, and a `MESH_READY` diagnostic marker. It does not produce terrain artifacts, a B0 `READY`, a `latest` pointer, or an engineering/simulation result.

## Install and run the synthetic example

The existing optional extras cover the geometry stack: PyShp and PyProj through `shp`, Shapely/PyProj through `triangular`, and the Gmsh Python/native package through `gmsh`.

```powershell
python -m pip install ".[test,shp,triangular,gmsh]"
python examples/triangular_mesh/run_domain_mesh_only_synthetic.py --project-root . --experiment-dir work/domain-mesh-only-synthetic-a
```

The helper creates a synthetic D Shapefile, synthetic S GeoJSON, a synthetic boundary line, a constraint line, a source identity manifest, and one route config under the new experiment directory. It refuses an existing directory. A caller may instead dispatch a reviewed config directly:

```powershell
python -m hydrodynamics.restricted_worker --run-config work/domain-mesh-only-synthetic-a/configs/domain-mesh-only-synthetic-normal.json --project-root .
```

All geometry decoding, CRS checks/transforms, D−S union/difference, Gmsh DLL loading/generation, quality and coverage audits, output writing, and trusted-context semantic readback run in the Job-bound worker. The supervisor reads the bounded external config/source manifest and file identities, watches the restricted worker tree, verifies output byte identities, and publishes only `MESH_READY` after successful worker exit. It does not pre-read or union geometry, load Gmsh, or generate a mesh.

## Geometry source manifest

The external `triangle-domain-mesh-source-manifest-v1` is named and SHA/stat-bound by the config. It contains 3–8 components: exactly one `domain_D`, one `security_S`, and at least one source boundary/constraint line component. Every component declares a role, format, explicit source CRS, file SHA-256 values, and file-stat identity. All paths are relative to the manifest directory. Source manifests are capped at 1 MiB, there are at most 40 component files, each file is capped at 4 MiB, and total component bytes are capped at 16 MiB.

- Polygon D and S accept GeoJSON Polygon/MultiPolygon feature sources or Shapefiles. D features are unioned; A accepts D only when the union is one Polygon, including its holes. A MultiPolygon D union is refused in this segment.
- S may contain multiple Polygon/MultiPolygon features and is unioned, so S may be multipart. The resulting D−S geometry must be one Polygon with holes; a MultiPolygon active domain is refused rather than truncated. No component is selected or discarded to force a single active polygon.
- Each line component represents exactly one LineString and one named geometry group. Multiple lines use multiple manifest components. MultiLineString/multipart line records are refused; the adapter does not drop parts.
- Shapefiles require `.shp`, `.shx`, and `.prj`. Any present `.dbf` or `.cpg` must be listed and its SHA/stat checked. The geometry reader opens only SHP/SHX and PRJ; DBF records are never read.
- A Shapefile PRJ must be semantically equivalent to its explicit `source_crs`. A GeoJSON CRS, when present, must be semantically equivalent to its explicit `source_crs`. Comparison ignores axis-order metadata only under the fixed XY coordinate-array policy; all datum and projection parameters remain significant. For WKT1 Shapefile PRJ versus WKT2 declarations, the adapter explicitly compares the geodetic CRS, projection method, every parameter and unit, and projected axis directions and units. An EPSG authority match or low-confidence EPSG guess does not establish equivalence. The explicit target CRS must be projected in metres. Vector coordinates may be transformed from the declared source CRS to that target CRS inside the Job with `always_xy=True`; no DEM transform/resampling/fill path exists in this route.
- Before any projected-CRS equality fast path, the adapter requires the complete EPSG:9807 Transverse Mercator parameter contract: latitude/longitude of natural origin (angular), scale factor at natural origin (scale), and false easting/false northing (linear). Every value must be finite and carry a recognized unit of the expected category; the exact required parameter set is enforced, so missing or extra parameters are refused. No zero/one method defaults are inserted. Geographic CRS comparison uses its own semantic path and is not checked against this projected parameter set. Other projected method codes do not have a complete parameter contract in this A revision and are refused for source CRS admission pending an explicit contract.

Boundary and constraint components become named Gmsh groups. The adapter refuses unsupported geometry, missing/extra groups, constraints crossing S, invalid CRS/geometry, face/node/quality caps, or any source change. The source SHA/stat manifest is checked before and after geometry work and semantic readback. The route config explicitly carries the existing nominal 50 m and minimum 1 m mesh-size decisions, caps Gmsh generation at 500,000 faces and 2,000,000 nodes, and requires quality/area thresholds. The A-stage runner keeps one worker, 2048 MiB Job commit, 900 seconds, 1 GiB output, and 16 MiB scratch.

These input and readback caps are separate. A source manifest is at most 1 MiB; a source set has 3–8 components and at most 40 files; each source file is at most 4 MiB and component files total at most 16 MiB. The trusted semantic reader also hashes `domain-mesh.json` and `domain-source-audit.json` separately with a 16 MiB maximum per artifact. The 1 GiB stage output budget does not raise either readback limit, and the 500,000-face/2,000,000-node caps do not imply that every permitted mesh fits the current 16 MiB readback cap. Meshes or audits above that cap are not supported by this reader revision; no larger valid mesh has been verified.

## Reader contracts

`read_domain_mesh_bundle_identity(...)` is an identity reader. It requires caller-supplied expected config and source-manifest SHA values and checks `MESH_READY`, manifest/status flags, and artifact hashes. It does not certify D/S/line semantics.

`validate_domain_mesh_bundle_semantics(...)` is the trusted-context geometry reader. It requires the dispatcher-retained config bytes, the project root, their expected hashes, and an in-process worker context bound to the current PID/attempt, plus an active Job membership check. The worker creates that context after route admission and invokes the reader before returning success; callers outside the Job cannot construct a valid context from bundle fields. It rechecks source components and the saved mesh against D/S, boundary/constraint source lines, face IDs, mesh quality, area closure, gap/outside/overlap, and security-area exclusion. It does not regenerate Gmsh or read a DEM. A context-free bundle cannot supply or upgrade its own trusted source context.

The accepted P0/G0 usage contract remains in `docs/b1_synthetic_pilot_usage.md`; that document continues to distinguish identity-only reads from trusted-context semantic checks.
