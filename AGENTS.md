# Contributor guidance

This repository is currently in the input interface and engineering
architecture phase for a future 2-D hydrodynamics engine.

- Keep schema, loader, docs, examples, and tests independent from solver code.
- Do not implement equations, discretization, numerical fluxes, dry/wet
  handling, time integration details, GUI, web services, or GPU optimization
  without an explicit later design decision.
- Every run-affecting input needs a type, unit, default or required status,
  legal range, and clear validation error.
- Update schema, docs, examples, and tests together.

- Never stage or commit large/local DEM inputs (for example `data/dem/*.tif`);
  use them only for explicitly requested read-only compatibility checks.
- Do not commit temporary analysis scripts or generated raster sidecars.
