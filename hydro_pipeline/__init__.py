"""hydro_pipeline: reusable 2-D triangular flood pipeline (any projected DEM -> mesh, terrain, sections, run, bundle, viewer).

Extracted from the 2d-hydrodynamics project. Entry point: ``python -m hydro_pipeline.case_pipeline case.yaml``.
"""

__version__ = "0.1.0"

# Import order: config <-> initial_condition / roughness / terrain_mapping_algorithms import each other;
# loading config first (as the original package initializer does) makes every module importable on its own.
from . import config as _config  # noqa: E402,F401
