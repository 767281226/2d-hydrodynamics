"""Public input, calculation, result-export, and workflow interfaces."""

from .config import (
    ConfigLoadError,
    ConfigValidationError,
    InitialConditionConfig,
    InitialConditionError,
    InitialConditionField,
    InitialConditionQuantity,
    InitialConditionType,
    InitialWaterField,
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

from .roughness import RoughnessError, RoughnessField, ResistanceField

__all__ = [
    "ConfigLoadError",
    "ConfigValidationError",
    "InitialConditionConfig",
    "InitialConditionError",
    "InitialConditionField",
    "InitialConditionQuantity",
    "InitialConditionType",
    "InitialWaterField",
    "RoughnessError",
    "RoughnessField",
    "ResistanceField",
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

from .nc2web import (
    FRAME_MAGIC, MESH_MAGIC, Nc2WebError, Nc2WebManifest, Nc2WebStepJsonReport,
    TARGET_WEB_EPSG, convert_netcdf_to_web, convert_netcdf_to_step_json, parse_netcdf_for_web,
)
from .workflow_config import WorkflowConfig, WorkflowError, load_workflow_config
from .workflow import run_workflow, find_latest_completed_bundle
__all__ += [
    "FRAME_MAGIC", "MESH_MAGIC", "Nc2WebError", "Nc2WebManifest", "Nc2WebStepJsonReport",
    "TARGET_WEB_EPSG", "convert_netcdf_to_web", "convert_netcdf_to_step_json", "parse_netcdf_for_web",
    "WorkflowConfig", "WorkflowError", "load_workflow_config", "run_workflow", "find_latest_completed_bundle",
]

# G1 evidence required by the offline result-package adapter.
from .gate_dispatch import GateDatumConversionEvidence, check_time_origin_consistency, validate_gate_dispatch_exports
__all__.extend(["GateDatumConversionEvidence", "check_time_origin_consistency", "validate_gate_dispatch_exports"])

from .external_evidence import validate_external_evidence, VerticalDatumAttestation, DispatchApprovalEvidence, LeveeCrestEvidence, BenchmarkEvidence
__all__.extend(["validate_external_evidence", "VerticalDatumAttestation", "DispatchApprovalEvidence", "LeveeCrestEvidence", "BenchmarkEvidence"])
