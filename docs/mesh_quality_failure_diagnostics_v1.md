# Mesh-quality failure diagnostic v1

This report is emitted only after the restricted mesh-only worker has generated a TriangularMesh and the existing quality check finds at least one face with q strictly below the configured minimum. It is evidence about that failed attempt; it does not make the mesh usable.

## Required fields

The diagnostic JSON is a strict object with no unknown fields. It has no defaults: every row below is required in a successfully written diagnostic. The report is emitted only for a mesh-quality failure.

| Field | Type | Unit, value, or bound |
|---|---|---|
| format / schema_version | string | triangle-mesh-quality-failure-diagnostic-v1 / 1.0 |
| diagnostic_only / preparation_only | boolean | true |
| mesh_ready / ready / latest_published / simulation_result / production_approved / engineering_ready | boolean | false |
| attempt_id / run_id | string | safe ID, 1–64 characters |
| config_sha256 / source_manifest_sha256 / route_code_sha256 | string | lowercase SHA-256; route digest binds the canonical installed-code hash map |
| failure_code / worker_exit_code | string / integer | MINIMUM_FACE_QUALITY_FAILED / 53 |
| failure_message | string | original quality rejection message, at most 512 characters |
| metric | object | fixed metric ID/version, formula, area/edge sources, unit 1, threshold in [0,1], comparison strict_lt |
| mesh.mesh_id | string | 1–128 characters |
| mesh.mesh_sha256 / mesh.canonical_mesh_bytes | lowercase SHA-256 / integer | digest and exact canonical payload byte length present in the worker |
| mesh.node_count / mesh.face_count | integer | 3–2,000,000 nodes; 1–500,000 faces |
| quality_summary.minimum_quality_exact / maximum_quality_exact | number | dimensionless q |
| quality_summary.bad_face_count_exact | integer | 1–face_count; counts exactly q < threshold |
| quality_summary.histogram.counts | array of integers | exactly 1,024 non-negative bin counts |
| quality_summary.histogram.underflow_count / overflow_count | integer | count of q values outside [0,1] |
| quality_summary.quantile_intervals | array | fixed six nearest-rank percentiles, each with 1-based rank and lower/upper q bounds |
| worst_face_selection | object | exact top K, K=16, all faces ordered by q, numeric face ID, then row |
| worst_face_selection.faces | array | at most 16 bounded face summaries |

Each worst-face summary contains its zero-based face row and node/edge indices; external face_id_decimal is a string so IDs above 2⁵³ are exact. Centroid and bounds use target CRS metres; area uses m²; edge lengths use m; minimum_angle_deg uses degrees; quality is dimensionless. Boundary and constraint associations are the group names linked through those mesh edges. The report never includes the full node/face arrays.

The parent failed-bundle quality_failure uses the primary-record shape in the schema and binds attempt/run ID, config/source/code hashes, threshold, counts, and canonical mesh identity. failure_diagnostic is either null or a reference containing a relative path, format, SHA-256, byte count, and diagnostic_only=true. failure_diagnostic_error is null when the report was committed, or contains one to four bounded phase/code/type/message errors when report creation, writing, budget checking, interruption, or parent validation failed.

## Fixed metric and threshold

For each validated counter-clockwise triangle:

    q = 4 sqrt(3) A / (a² + b² + c²)

Here A is positive triangle area in m², a, b, and c are edge lengths in m, and q is dimensionless. The report binds metric ID triangle-face-quality-q4sqrt3-area-over-edge-square-sum-v1, threshold, and strict comparison q < threshold. A face exactly equal to the threshold is not counted as failing. The existing TriangularMesh.quality_report remains the source of the exact minimum and bad-face count; no quality gate or configuration value changes.

## Bounded distribution

The report has a fixed 64 KiB byte cap and contains no full mesh arrays. It records the canonical in-memory mesh payload SHA-256 and exact byte length, node and face counts, exact minimum and maximum q, and exact number of faces below the threshold.

The distribution uses 1,024 equal-width bins over q in [0, 1], with underflow and overflow counts recorded separately. Quantile percentiles are fixed at 10, 25, 50, 75, 90, and 99. Each uses the nearest-rank rule rank = ceil(p*N), one-based. The report returns the lower and upper edges of the containing bin, not an exact quantile value. For in-range q, each interval is at most 1/1,024 wide; underflow and overflow intervals use observed extrema and may be wider. Equal q values map to the same bin and are not ordered for quantiles. Histogram counts are retained so the approximate intervals can be checked.

The worst-face sample is the exact first K=16 faces ordered by q ascending, then numeric face ID ascending, then zero-based face row ascending. Each face ID is a decimal string to preserve int64 identifiers beyond 2⁵³. A sampled row includes its zero-based row, node and edge indices, centroid and bounding box in target CRS metres, area in m², edge lengths in m, minimum angle in degrees, and boundary/constraint group names associated through its mesh edges. It reports only this bounded sample, never all face records.

## Binding and failed-bundle behavior

The report binds attempt/run ID, frozen config SHA-256, source-manifest SHA-256, aggregate route-code SHA-256, metric version, canonical mesh SHA-256, and canonical mesh byte length. The parent failed-bundle status.json and manifest.json reference the diagnostic path, exact SHA-256, byte count, and diagnostic_only=true. A small primary quality-failure record, capped at 4 KiB in private scratch, lets the parent preserve the quality cause if cancellation interrupts diagnostic creation.

The diagnostic is written inside the same Job with no-clobber semantics and counts against the existing output and scratch budgets. If building or writing it fails, exceeds 64 KiB, exceeds the existing budgets, encounters an existing path, or is interrupted, the worker retains the original minimum-quality failure and exit 53. The failed bundle adds failure_diagnostic_error with its phase, code, exception type, and bounded message; the failure does not become success. If the supervisor stops a worker after the primary failure record exists, status retains that quality cause and reports the actual supervisor exit plus the diagnostic interruption.

Every diagnostic and failed status remain diagnostic-only: mesh_ready, ready, latest_published, simulation_result, production_approved, and engineering_ready are false. No MESH_READY marker or normal mesh/audit JSON is written for this failure. Normal mesh readers consume only the regular mesh-only artifact format, not the diagnostic format.

The JSON Schema is hydrodynamics/schemas/domain_mesh_quality_failure_diagnostic_v1.schema.json. Its defs also specify parent status references and diagnostic errors. No input config, source-manifest, mesh, CRS, quality threshold, or resource parameter was added.

Known diagnostic error codes include DIAGNOSTIC_BYTE_CAP_EXCEEDED, DIAGNOSTIC_BUDGET_REJECTED, DIAGNOSTIC_NO_CLOBBER_CONFLICT, DIAGNOSTIC_INTERRUPTED, DIAGNOSTIC_NOT_COMMITTED, DIAGNOSTIC_REFERENCE_INVALID, QUALITY_FAILURE_PRIMARY_RECEIPT_CAP_EXCEEDED, QUALITY_FAILURE_PRIMARY_UNREADABLE, QUALITY_FAILURE_PRIMARY_UNBOUND, and QUALITY_FAILURE_PRIMARY_MISMATCH. These errors never replace the primary MINIMUM_FACE_QUALITY_FAILED cause or change the worker's original failure exit code.

examples/triangular_mesh/domain_mesh_quality_failure.synthetic.example.json is a schema-valid synthetic fixture with dummy hashes; it is not associated with a real attempt, source, or mesh.
