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

from .output import OutputError, OutputField, SimulationOutput, ResultDataset
from .netcdf_writer import NetCDFWriter, OutputDependencyError
from .hourly_inundation import (
    HourlyInundationSolver, classify_dongdian_mask, classify_qingbei_mask,
    fill_volume, write_hourly_netcdf,
)
from .gate_dispatch import (
    GATE_RULES, GateDispatchInput, GateDispatchLog, GateDispatchOptions,
    GateDispatchSnapshot, GateOperation, GateWaterLevel, build_gate_dispatch_log,
    export_gate_dispatch_for_netcdf, gate_dispatch_paths, write_gate_dispatch_log,
)

__all__ += [
    "OutputError", "OutputField", "SimulationOutput", "ResultDataset",
    "NetCDFWriter", "OutputDependencyError", "HourlyInundationSolver",
    "classify_dongdian_mask", "classify_qingbei_mask", "fill_volume", "write_hourly_netcdf",
    "GATE_RULES", "GateDispatchInput", "GateDispatchLog", "GateDispatchOptions",
    "GateDispatchSnapshot", "GateOperation", "GateWaterLevel", "build_gate_dispatch_log",
    "export_gate_dispatch_for_netcdf", "gate_dispatch_paths", "write_gate_dispatch_log",
]
