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
    DEMBandCountError,
    DEMDataset,
    DEMFormatError,
    DEMMetadata,
    DEMReader,
    DEMReaderDependencyError,
    DEMReaderError,
    DEMValidationError,
    DEMValidator,
    GeoTIFFDEMReader,
    GeoTIFFReader,
    GeoTiffDEMReader,
    GeoTiffReader,
    PlaceholderDEMReader,
    RasterioDEMReader,
)

__all__ = [
    "ConfigLoadError",
    "ConfigValidationError",
    "DEMBandCountError",
    "DEMDataset",
    "DEMFormatError",
    "DEMMetadata",
    "DEMReader",
    "DEMReaderDependencyError",
    "DEMReaderError",
    "DEMValidationError",
    "DEMValidator",
    "GeoTIFFDEMReader",
    "GeoTIFFReader",
    "GeoTiffDEMReader",
    "GeoTiffReader",
    "PlaceholderDEMReader",
    "RasterioDEMReader",
    "NoDataStrategy",
    "ResamplingStrategy",
    "SimulationConfig",
    "TerrainField",
    "TerrainMapper",
    "TerrainMappingError",
    "map_dataset_to_field",
    "resolve_auto_resampling_strategy",
    "load_config",
]

from .terrain_mapping_algorithms import TerrainMappingError, map_dataset_to_field
