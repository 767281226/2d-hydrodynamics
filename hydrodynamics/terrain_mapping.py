"""Public DEM-to-grid mapping interface.

Round 5 provides pure-Python mapping for an already-read DEMDataset. The
reader remains separate; this module is a focused public import path for
mapping types and functions. No file I/O, CRS reprojection, or Solver work
is performed here.
"""

from .config import (
    ResamplingStrategy,
    TerrainField,
    TerrainMapper,
    resolve_auto_resampling_strategy,
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
from .terrain_mapping_algorithms import TerrainMappingError, map_dataset_to_field

__all__ = [
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
    "TerrainMappingError",
    "map_dataset_to_field",
    "ResamplingStrategy",
    "TerrainField",
    "TerrainMapper",
    "resolve_auto_resampling_strategy",
]
