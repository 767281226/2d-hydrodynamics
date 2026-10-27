# Synthetic P0/G0 terrain preparation

This route measures small synthetic window mapping and runs synthetic Gmsh generation inside the restricted Windows worker. It produces preparation diagnostics. It does not authorize real D−S inputs, full DEM mapping, a solver run, or engineering use.

Install the project with its declared optional dependencies from a reviewed checkout:

```powershell
python -m pip install ".[test,raster,triangular,gmsh]"
```

The worker requires the reviewed project checkout as `--project-root` for route-code identities and the tracked evidence document. Installing a wheel supplies the Python package and dependencies; a data-only directory does not replace that checkout. Keep package and checkout code from the same reviewed revision.

The schemas and examples are in `examples/triangular_mesh/`: `b1_synthetic_pilot_config.schema.json`, `b1_synthetic_gmsh_recipe.schema.json`, and the P0/G0 config and recipe examples. Every config field is explicit; no missing resource value silently falls back. G0 pins the installed Gmsh version and native DLL SHA before dispatch and rechecks them inside the Job.

To create and run a new synthetic experiment, choose an unused project-relative work directory:

```powershell
python examples/triangular_mesh/run_b1_synthetic_pilot.py --project-root . --experiment-dir work/pilot-example-001 --p0-repeats 2
```

The helper creates only synthetic TIFF/mesh/recipe inputs and launches each case through the worker CLI. It refuses an existing experiment directory. Alternatively run one already generated, reviewed config:

```powershell
python -m hydrodynamics.restricted_worker --run-config work/pilot-example-001/configs/g0-gmsh-synthetic-normal.json --project-root .
```

The separate `triangle-b1-synthetic-pilot-config-v1` format supports `p0_windowed_mapping` and `g0_gmsh_windowed_mapping`. The published B0 v1 format continues to reject unknown pilot fields.

| Required setting | Fixed value or legal range |
| --- | --- |
| worker / Job commit / attempt wall | 1 worker / 2048 MiB / 900 seconds |
| output / private scratch budgets | 1 GiB / 16 MiB |
| source / tile / GDAL cache | 512×512 at 1m / 256×256, one raw tile / 64 MiB |
| mapper caps | 500000 faces, 2000000 nodes, 1000000 candidate checks |
| P0 case identifier | 1–64 characters, required |
| P0 seed / faces / size / repeat | unsigned 32-bit integer / 2, 8 or 32 / exactly 50m / 1 or 2 |
| G0 generated caps | 1–500000 faces, 3–2000000 nodes |
| G0 minimum normalized quality | finite number in [0,1] |
| G0 absolute / relative area tolerances | [1e-12,1] m² / [1e-12,1e-3] |
| Gmsh version pin | `4.minor.patch`, minor≥11, decimal components without leading zeros or suffixes |
| semantic roles | nonblank strings; hole names follow `hole_N`; all recipe groups must be represented |

The schema describes the remaining required source identities, SHA values, paths, CRS alias and mapping parameters. Config validation errors identify the rejected field. Budgets cannot be automatically increased. Job memory limits constrain committed memory in the Job; they are not a limit on all host physical RAM.

## Read back with external trusted context

`read_prepared_terrain(bundle)` performs generic stored-identity/terrain checks. **For a pilot bundle, this context-free call does not certify recipe geometry, line groups, semantic roles or G0 area audits.** It can accept consistently rehashed files with incorrect G0 semantics. Do not use that call alone as G0 acceptance.

For complete pilot readback, supply both the config retained by the dispatcher and its reviewed project root. Bind the original config bytes independently; do not reconstruct trusted context from fields supplied by the bundle:

```python
import hashlib
import json
from pathlib import Path
from hydrodynamics.prepared_terrain import read_prepared_terrain

project_root = Path("/path/to/reviewed/checkout")
config_path = project_root / "work/pilot-example-001/configs/g0-gmsh-synthetic-normal.json"
config_bytes = config_path.read_bytes()  # dispatcher-retained input, outside the bundle
reader = read_prepared_terrain(
    bundle_path,
    expected_bindings={"config_sha256": hashlib.sha256(config_bytes).hexdigest()},
    pilot_config=json.loads(config_bytes),
    project_root=project_root,
)
faces = reader.iter_faces()
```

Supplying only one of `pilot_config` and `project_root` fails. With both, the standard reader additionally checks the saved mesh against the trusted recipe/config: roles, boundary/constraint segment coverage, outer-minus-holes domain gap/outside/overlap, quality, area difference and configured tolerance. A semantic mismatch raises `PreparedTerrainError`. The supervisor uses this contextual path before publication. Readers do not open the DEM or rerun terrain mapping; retain the bound source/recipe files for identity checks.

Successful attempts publish a completed diagnostic directory with READY; failures keep a failed diagnostic without consumable READY. A diagnostic READY does not confer engineering readiness. Coverage remains pending/diagnostic_only, the scoped vertical relation remains unapplied, and simulation/production/engineering flags stay false.

P0 reports actual candidate/clip counts, mapper component timings, writer/readback times and resource peaks. Independent repetitions do not guarantee cold or warm OS caches. A 512²/32-face result establishes that measured workload only. Before real mapping, separately review an actual D−S mesh, candidate volume, output size and total/per-stage budget; stop for a new plan when the estimate exceeds the frozen budget.
