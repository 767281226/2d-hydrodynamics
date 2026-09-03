"""Configuration interface for the 2-D hydrodynamics project.

The package intentionally contains no numerical solver.  It provides typed,
validated configuration objects that can be consumed by a future engine,
command-line tool, or another Python application.
"""

from .config import (
    ConfigLoadError,
    ConfigValidationError,
    SimulationConfig,
    load_config,
)

__all__ = [
    "ConfigLoadError",
    "ConfigValidationError",
    "SimulationConfig",
    "load_config",
]
