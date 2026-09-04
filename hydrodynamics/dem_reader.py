"""Public import surface for the optional DEM readers.

The implementation remains in :mod:`hydrodynamics.dem_contract`; this module
exists so callers can depend on a reader-focused path without coupling to the
metadata model's file layout.
"""

from .dem_contract import (
    DEMBandCountError,
    DEMDataset,
    DEMFormatError,
    DEMReader,
    DEMReaderDependencyError,
    DEMReaderError,
    GeoTIFFDEMReader,
    GeoTIFFReader,
    GeoTiffDEMReader,
    GeoTiffReader,
    PlaceholderDEMReader,
    RasterioDEMReader,
)

__all__ = [
    "DEMBandCountError",
    "DEMDataset",
    "DEMFormatError",
    "DEMReader",
    "DEMReaderDependencyError",
    "DEMReaderError",
    "GeoTIFFDEMReader",
    "GeoTIFFReader",
    "GeoTiffDEMReader",
    "GeoTiffReader",
    "PlaceholderDEMReader",
    "RasterioDEMReader",
]
