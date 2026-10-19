# R2A synthetic result package

This example uses the packaged `r2a_flat_bed_closed_v1` fixture: two triangular faces, a 2 m² flat bed, 0.2 m still water, closed walls, no source terms, three saved times over one second, and no more than 64 faces. It exercises the existing triangular runner, then exports its already-written NetCDF result. The package adapter never starts the solver.

From the repository root, with the project environment installed:

```powershell
hydrodynamics-mesh-prepare examples/rehearsal_package/mesh_preparation.yaml
hydrodynamics-business-prepare examples/rehearsal_package/business_preparation.yaml
hydrodynamics-rehearsal-package make-local-envelope --request examples/rehearsal_package/package_request.yaml
hydrodynamics-triangular-run examples/rehearsal_package/run.yaml
hydrodynamics-rehearsal-package export --request examples/rehearsal_package/package_request.yaml
hydrodynamics-rehearsal-package validate --request examples/rehearsal_package/package_request.yaml
```

Without a G1 companion pair, export creates a valid `INCOMPLETE_REVIEW` package. Add the local synthetic G1 evidence and export a second package to see the complete path:

```powershell
python examples/rehearsal_package/add_local_g1_pair.py --run-config examples/rehearsal_package/run.yaml --assignment examples/rehearsal_package/dispatch_assignment.json
hydrodynamics-rehearsal-package export --request examples/rehearsal_package/package_request_with_g1.yaml --require-complete
hydrodynamics-rehearsal-package validate --request examples/rehearsal_package/package_request_with_g1.yaml --require-complete
```

`add_local_g1_pair.py` calls the existing file exporter with empty local inputs. It does not connect to a database or claim a physical gate operation. The G1 JSON keeps its original `result_file` path. Both package requests use the same separately labeled local assignment and execution envelope; result identity authentication remains `NOT_ASSESSED`.

The package stores original native NetCDF bytes plus frame summaries, selectors, quality evidence and identity context. It is bound to the original local request, run config, P3-A business package and P2 mesh. Moving the result package alone does not make it portable: validation needs that source graph. Packages always have `can_publish=false` and `engineering_validated=false`.

To check the incomplete exit code, run export with `--require-complete` before creating the G1 pair. It returns code 3 while keeping the valid incomplete package for review. Invalid/corrupt input returns code 2.
