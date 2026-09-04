"""Public DEM-to-grid mapping interface.

The V1.0 implementation deliberately stops at the interface boundary.  The
configuration module remains the canonical implementation; this module provides
a focused public import path for terrain mapping types.
"""

from .config import (
    ResamplingStrategy,
    TerrainField,
    TerrainMapper,
    resolve_auto_resampling_strategy,
)
from .dem_contract import (
    DEMMetadata,
    DEMReader,
    DEMValidationError,
    DEMValidator,
    PlaceholderDEMReader,
)

__all__ = [
    "DEMMetadata",
    "DEMReader",
    "DEMValidationError",
    "DEMValidator",
    "PlaceholderDEMReader",
    "ResamplingStrategy",
    "TerrainField",
    "TerrainMapper",
    "resolve_auto_resampling_strategy",
]
