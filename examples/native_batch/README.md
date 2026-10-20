# Native triangle batch launcher

`run_qingbei_nc.bat` now starts the native triangle batch. With no arguments it runs only the approved four-face `SYNTHETIC_ONLY` combined Manning/rainfall plus security-exclusion example. It creates a new `work/results/<script_id>` directory and refuses to overwrite an existing path.

```powershell
run_qingbei_nc.bat
run_qingbei_nc.bat --id demo-run-01
run_qingbei_nc.bat --script-id demo-run-02 --out-dir "D:/out/native demo"
run_qingbei_nc.bat --engineering --id case001-preflight
```

Relative `--out-dir` paths are anchored at the project root; absolute paths are accepted. `--id` and `--script-id` preserve an explicit valid ID. If omitted, the launcher uses `resolve_script_id` (Beijing date plus Unix nanoseconds). Supplying both with different values is an error. Unknown options such as `--workflow` and `--show-latest` are rejected.

`--engineering` reads the strict template at `examples/case_001/native_engineering_preflight.yaml` and writes a `preflight_only` blocked report. It checks declared paths only, does not read DEM/NPZ/CSV/DBF contents, does not prepare a mesh or run a solver, and always reports that native open-boundary execution is unsupported in this batch. `--engineering-config` selects another preflight template. `--config` is only for the strict synthetic launcher profile.

The `script_id` is the outer batch identity in the output path, stage records, `run_report.json`, and `summary.json`. The native `combined.nc` retains its existing strict `case_id` and source graph; the launcher does not add an unsupported native-contract field.

The launcher requires `.venv\Scripts\python.exe` and the project's local dependencies. It never installs packages or accesses the network. To inspect the versioned launch/preflight/report schemas, run:

```powershell
.venv\Scripts\python.exe -X utf8 -m hydrodynamics.native_batch_launcher --schema
```

Static schemas are in `native_batch_launcher.schema.json`. The default chain copies only the JSON/GeoJSON/YAML allowlist into the new run directory, then runs P2, P3, combined source preparation, the source-only runner, v2 MAX/EPSG:4490 SHP export/validation and v2 FRAME/native SHP export/validation. It does not run or require the base-none result. The copied profile and limits are fixed in `synthetic_demo.yaml` (64 faces, 8 saved frames, 1 s maximum).
