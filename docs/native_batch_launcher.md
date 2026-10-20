# Native triangle batch launcher

`run_qingbei_nc.bat` routes to `hydrodynamics.native_batch_launcher`. Its default mode runs the approved synthetic security-exclusion combined-source example and labels the run `SYNTHETIC_ONLY`. It does not start the legacy structured/hourly workflow.

## IDs and output paths

The launcher accepts `--id` and its alias `--script-id`. Explicit IDs pass through the existing `resolve_script_id`/`validate_run_id` contract unchanged. Without either option, it generates the existing Beijing `YYYYMMDD-<Unix nanoseconds>` ID. The default output is `work/results/<script_id>`. Relative `--out-dir` paths are resolved from the project root; absolute paths are accepted. Existing directories, files, links, or junctions are rejected before writing.

`script_id` is the outer batch identity recorded in the output directory, every stage, `run_report.json`, and `summary.json`. The generated native `combined.nc` keeps its existing `case_id` and P4 source graph unchanged; no extra native result identity field is invented.

`run_qingbei_nc.bat` uses the project `.venv\Scripts\python.exe`, `-X utf8`, and `pushd`/`popd` around the script directory. Missing interpreter or dependencies produce an installation command but do not auto-install. Unknown legacy options are rejected rather than silently routed to the structured workflow.

## Synthetic execution

The strict `native-batch-launch-config-v1` selects only `examples/security_exclusion/`, the four-face, three-frame, 0.1 s synthetic case. The launcher copies a fixed JSON/GeoJSON/YAML allowlist into the new run directory and runs P2 mesh preparation, P3 dynamic business preparation, combined source preparation, the existing P4 source runner, and v2 MAX/EPSG:4490 plus FRAME/native SHP export and validation. It never runs or requires `base_none_dynamic.nc`. `summary.json` and `run_report.json` record case/script identity, source flags, native volumes and ledgers, active/excluded area, accepted/unmodeled rainfall, module source and every command's exit status. The optional preview is a standard-library SVG based on native face-node geometry and the validated security geometry.

Failures retain a non-complete report with the failing stage and actual exit code. The batch does not start database, HTTP, viewer-server, or GUI services.

## Engineering preflight

`--engineering` selects the strict `native-engineering-preflight-v1` template. Its 20 evidence fields cover original domain D and CRS, native mesh CRS, DEM source CRS/vertical metadata/NoData/coverage, active-face initial state, Manning source and SI interpretation, rainfall source and intervals, boundary groups/direction/timing/units/datum/provenance, native preparation/run/validation references, and the enabled security-area declaration.

The preflight checks paths and presence only; it does not open DEM, NPZ, CSV, DBF, or vector geometry, generate a mesh, or invoke a solver. It reports `preflight_only=true`, `execution_available=false`, and `UNSUPPORTED_OPEN_BOUNDARY_EXECUTION` for the case's inflow/outflow requirement. Filling the template does not enable engineering execution in this batch.

The strict launch, engineering-template, and report schemas are available with `--schema` and are copied as `examples/native_batch/native_batch_launcher.schema.json`. The Python module and launcher are project-local: installed-wheel use requires the example input tree to remain available in the project checkout.

## Windows Unicode NetCDF paths

Some Windows netCDF4/HDF5 builds reject Unicode file paths even though Python's filesystem APIs can read and write them. The P2 UGRID and native source NetCDF read/write boundaries therefore use short-lived ASCII temporary files for non-ASCII paths. File copies stream through a 1 MiB buffer; the implementation does not load an entire NetCDF file into memory. Reads preserve the original bytes for the existing strict validators.

`clobber=False` publishes with an atomic no-replace rename, so a destination that exists initially or appears during staging is preserved. `clobber=True` publishes with atomic replacement. `r+` prepares and syncs its complete staged copy before checking the original file identity. A per-destination Windows mutex serializes publishers using this adapter from that recheck through replacement. This coordinates the batch's adapter-managed writers; it is not a compare-and-swap primitive for arbitrary external processes. A non-cooperating external writer can still race in the narrow interval between the last identity check and replacement, so `r+` callers must keep the target exclusively controlled outside this adapter. The launcher uses `r+` only on a unique per-run temporary result before the existing strict readback and no-clobber final publication. If a changed identity is observed, publication fails and preserves the current target. Temporary files are cleaned on success and failure. If a temporary workspace or publication cannot be completed, the report keeps the actual underlying I/O error and the run cannot be marked complete. This path adapter does not change NetCDF formats, source graphs, or numerical behavior.
