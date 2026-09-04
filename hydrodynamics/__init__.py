"""Configuration interface for the 2-D hydrodynamics project.

The package intentionally contains no numerical solver.  It provides typed,
validated configuration objects that can be consumed by a future engine,
command-line tool, or another Python application.
"""

from .config import (
    ConfigLoadError,
    ConfigValidationError,
    NoDataStrategy,
    ResamplingStrategy,
    SimulationConfig,
    TerrainField,
    TerrainMapper,
    resolve_auto_resampling_strategy,
    load_config,
)
from .dem_contract import (
    DEMMetadata,
    DEMReader,
    DEMValidationError,
    DEMValidator,
    PlaceholderDEMReader,
)

__all__ = [
    "ConfigLoadError",
    "ConfigValidationError",
    "DEMMetadata",
    "DEMReader",
    "DEMValidationError",
    "DEMValidator",
    "PlaceholderDEMReader",
    "NoDataStrategy",
    "ResamplingStrategy",
    "SimulationConfig",
    "TerrainField",
    "TerrainMapper",
    "resolve_auto_resampling_strategy",
    "load_config",
]
