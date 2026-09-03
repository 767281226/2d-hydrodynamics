"""Typed configuration schema and YAML loader.

This module is deliberately limited to the input contract.  It does not read
rasters, execute time series, or call a hydrodynamics solver.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)


def _number(value: Any) -> float:
    """Accept YAML integers/floats, but reject booleans and strings."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("must be a number")
    return float(value)


def _text(value: Any) -> str:
    """Accept only strings so paths and identifiers are not silently coerced."""

    if not isinstance(value, str):
        raise TypeError("must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be empty")
    return stripped


Number = Annotated[float, BeforeValidator(_number)]
Text = Annotated[str, BeforeValidator(_text)]


class _SchemaModel(BaseModel):
    """Common strict model settings for every configuration section."""

    model_config = ConfigDict(
        extra="forbid",
        validate_default=True,
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class Units(str, Enum):
    SI = "SI"


class DomainType(str, Enum):
    STRUCTURED = "structured"


class TerrainType(str, Enum):
    RASTER = "raster"
    CONSTANT = "constant"


class InitialConditionType(str, Enum):
    CONSTANT_DEPTH = "constant_depth"
    CONSTANT_LEVEL = "constant_level"
    RASTER = "raster"


class FieldType(str, Enum):
    CONSTANT = "constant"
    RASTER = "raster"


class BoundaryType(str, Enum):
    DISCHARGE = "discharge"
    WATER_LEVEL = "water_level"
    WALL = "wall"
    TIMESERIES = "timeseries"


class BoundaryLocation(str, Enum):
    WEST = "west"
    EAST = "east"
    NORTH = "north"
    SOUTH = "south"


class BoundaryQuantity(str, Enum):
    DISCHARGE = "discharge"
    WATER_LEVEL = "water_level"


class TimeUnit(str, Enum):
    SECONDS = "s"


class RainfallType(str, Enum):
    TIMESERIES = "timeseries"


class OutputVariable(str, Enum):
    WATER_DEPTH = "water_depth"
    WATER_LEVEL = "water_level"
    VELOCITY_X = "velocity_x"
    VELOCITY_Y = "velocity_y"
    VELOCITY_MAGNITUDE = "velocity_magnitude"
    DISCHARGE = "discharge"
    MAXIMUM_DEPTH = "maximum_depth"
    MAXIMUM_VELOCITY = "maximum_velocity"
    ARRIVAL_TIME = "arrival_time"
    FROUDE_NUMBER = "froude_number"


class OutputFormat(str, Enum):
    NETCDF = "netcdf"
    GEOTIFF = "geotiff"
    CSV = "csv"


class ModelConfig(_SchemaModel):
    name: Text
    description: Text | None = None
    gravity: Number = Field(default=9.81, gt=0, description="m/s^2")
    start_time: Number = Field(default=0.0, ge=0, description="s")
    end_time: Number = Field(description="s")
    # Kept for compatibility with the requested draft.  output.interval is the
    # canonical field until this duplication is resolved by the model design.
    output_interval: Number | None = Field(default=None, gt=0, description="s")
    coordinate_system: Text
    units: Units = Units.SI

    @model_validator(mode="after")
    def validate_time_window(self) -> "ModelConfig":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be greater than start_time")
        return self


class DomainConfig(_SchemaModel):
    type: DomainType = DomainType.STRUCTURED
    dx: Number = Field(gt=0, description="m")
    dy: Number = Field(gt=0, description="m")
    nx: StrictInt | None = Field(default=None, gt=0)
    ny: StrictInt | None = Field(default=None, gt=0)


class TerrainConfig(_SchemaModel):
    type: TerrainType
    file: Text | None = None
    nodata: Number | None = Field(default=None, description="dataset nodata value")
    elevation: Number | None = Field(default=None, description="m")

    @model_validator(mode="after")
    def validate_source(self) -> "TerrainConfig":
        if self.type is TerrainType.RASTER:
            if self.file is None:
                raise ValueError("file is required when terrain.type is 'raster'")
            if self.elevation is not None:
                raise ValueError("elevation is only valid when terrain.type is 'constant'")
        elif self.type is TerrainType.CONSTANT:
            if self.elevation is None:
                raise ValueError("elevation is required when terrain.type is 'constant'")
            if self.file is not None:
                raise ValueError("file is only valid when terrain.type is 'raster'")
        return self


class VelocityFieldConfig(_SchemaModel):
    """Reserved initial velocity interface; no velocity computation occurs here."""

    type: FieldType
    x: Number | None = Field(default=None, description="m/s")
    y: Number | None = Field(default=None, description="m/s")
    x_file: Text | None = None
    y_file: Text | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "VelocityFieldConfig":
        if self.type is FieldType.CONSTANT:
            if self.x is None or self.y is None:
                raise ValueError("x and y are required for a constant velocity field")
            if self.x_file is not None or self.y_file is not None:
                raise ValueError("x_file/y_file are only valid for a raster velocity field")
        else:
            if self.x_file is None or self.y_file is None:
                raise ValueError("x_file and y_file are required for a raster velocity field")
            if self.x is not None or self.y is not None:
                raise ValueError("x/y are only valid for a constant velocity field")
        return self


class InitialConditionConfig(_SchemaModel):
    type: InitialConditionType
    depth: Number | None = Field(default=None, ge=0, description="m")
    level: Number | None = Field(default=None, description="m")
    depth_file: Text | None = None
    level_file: Text | None = None
    velocity: VelocityFieldConfig | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "InitialConditionConfig":
        if self.type is InitialConditionType.CONSTANT_DEPTH:
            if self.depth is None:
                raise ValueError("depth is required when initial_condition.type is 'constant_depth'")
            if any(value is not None for value in (self.level, self.depth_file, self.level_file)):
                raise ValueError("constant_depth accepts only depth (and optional velocity)")
        elif self.type is InitialConditionType.CONSTANT_LEVEL:
            if self.level is None:
                raise ValueError("level is required when initial_condition.type is 'constant_level'")
            if any(value is not None for value in (self.depth, self.depth_file, self.level_file)):
                raise ValueError("constant_level accepts only level (and optional velocity)")
        else:
            if (self.depth_file is None) == (self.level_file is None):
                raise ValueError("exactly one of depth_file or level_file is required for a raster initial condition")
            if self.depth is not None or self.level is not None:
                raise ValueError("depth/level constants are not valid for a raster initial condition")
        return self


class RoughnessConfig(_SchemaModel):
    type: FieldType
    manning_n: Number | None = Field(default=None, gt=0)
    file: Text | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "RoughnessConfig":
        if self.type is FieldType.CONSTANT:
            if self.manning_n is None:
                raise ValueError("manning_n is required when roughness.type is 'constant'")
            if self.file is not None:
                raise ValueError("file is only valid when roughness.type is 'raster'")
        else:
            if self.file is None:
                raise ValueError("file is required when roughness.type is 'raster'")
            if self.manning_n is not None:
                raise ValueError("manning_n is only valid when roughness.type is 'constant'")
        return self


class TimeSeriesRef(_SchemaModel):
    file: Text
    time_column: Text = "time"
    value_column: Text = "value"
    time_unit: TimeUnit = TimeUnit.SECONDS

    @model_validator(mode="after")
    def validate_columns(self) -> "TimeSeriesRef":
        if self.time_column == self.value_column:
            raise ValueError("time_column and value_column must be different")
        return self


class BoundaryConfig(_SchemaModel):
    id: Text
    location: BoundaryLocation
    type: BoundaryType
    value: Number | None = None
    timeseries: TimeSeriesRef | None = None
    quantity: BoundaryQuantity | None = None

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: Number | None) -> Number | None:
        if value is not None and value < 0:
            raise ValueError("must be greater than or equal to 0")
        return value

    @model_validator(mode="after")
    def validate_source(self) -> "BoundaryConfig":
        if self.type is BoundaryType.WALL:
            if self.value is not None or self.timeseries is not None or self.quantity is not None:
                raise ValueError("wall boundary does not accept value, timeseries, or quantity")
        elif self.type is BoundaryType.TIMESERIES:
            if self.timeseries is None:
                raise ValueError("timeseries is required when boundary.type is 'timeseries'")
            if self.quantity is None:
                raise ValueError("quantity is required for a generic timeseries boundary")
            if self.value is not None:
                raise ValueError("value is not valid for a generic timeseries boundary")
        else:
            if (self.value is None) == (self.timeseries is None):
                raise ValueError("exactly one of value or timeseries is required for this boundary type")
            if self.quantity is not None:
                raise ValueError("quantity is only valid when boundary.type is 'timeseries'")
        return self


class RainfallConfig(_SchemaModel):
    enabled: StrictBool = False
    type: RainfallType | None = None
    file: Text | None = None
    time_unit: TimeUnit = TimeUnit.SECONDS
    intensity_unit: Text = "mm/h"

    @model_validator(mode="after")
    def validate_source(self) -> "RainfallConfig":
        if not self.enabled:
            if self.type is not None or self.file is not None:
                raise ValueError("type and file must be omitted when rainfall.enabled is false")
        else:
            if self.type is not RainfallType.TIMESERIES:
                raise ValueError("rainfall.type must be 'timeseries' when rainfall is enabled")
            if self.file is None:
                raise ValueError("file is required when rainfall is enabled")
        return self


class SourceConfig(_SchemaModel):
    rainfall: RainfallConfig


class NumericsConfig(_SchemaModel):
    # These are intentionally free-form placeholders until the numerical
    # method is selected in a later design phase.
    scheme: Text | None = None
    flux: Text | None = None
    time_integrator: Text | None = None
    cfl: Number = Field(default=0.5, gt=0, le=1)
    min_depth: Number = Field(default=0.001, ge=0, description="m")
    dry_depth: Number = Field(default=0.0001, ge=0, description="m")

    @model_validator(mode="after")
    def validate_depth_thresholds(self) -> "NumericsConfig":
        if self.min_depth < self.dry_depth:
            raise ValueError("min_depth must be greater than or equal to dry_depth")
        return self


class OutputConfig(_SchemaModel):
    directory: Text = "results"
    interval: Number = Field(default=60.0, gt=0, description="s")
    variables: list[OutputVariable] = Field(default_factory=lambda: [OutputVariable.WATER_DEPTH], min_length=1)
    format: OutputFormat = OutputFormat.NETCDF

    @field_validator("variables")
    @classmethod
    def validate_unique_variables(cls, value: list[OutputVariable]) -> list[OutputVariable]:
        if len(set(value)) != len(value):
            raise ValueError("variables must not contain duplicates")
        return value


class ValidationConfig(_SchemaModel):
    enabled: StrictBool = False
    mass_balance_check: StrictBool = False
    report: StrictBool = True

    @model_validator(mode="after")
    def validate_flags(self) -> "ValidationConfig":
        if self.mass_balance_check and not self.enabled:
            raise ValueError("mass_balance_check requires validation.enabled to be true")
        return self


class SimulationConfig(_SchemaModel):
    """Complete configuration for one simulation case."""

    model: ModelConfig
    domain: DomainConfig
    terrain: TerrainConfig
    initial_condition: InitialConditionConfig
    roughness: RoughnessConfig
    boundary: list[BoundaryConfig] = Field(min_length=1)
    source: SourceConfig
    numerics: NumericsConfig
    output: OutputConfig
    validation: ValidationConfig

    @model_validator(mode="after")
    def validate_cross_section_contract(self) -> "SimulationConfig":
        ids = [item.id for item in self.boundary]
        if len(set(ids)) != len(ids):
            raise ValueError("boundary ids must be unique")
        if self.model.output_interval is not None and self.model.output_interval != self.output.interval:
            raise ValueError("model.output_interval and output.interval must match when both are provided")
        return self


class ConfigLoadError(ValueError):
    """Raised when a configuration file cannot be read or parsed as YAML."""


class ConfigValidationError(ValueError):
    """Raised when YAML data does not satisfy :class:`SimulationConfig`."""

    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


def _format_validation_error(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "config"
        lines.append(f"{location}: {item['msg']}")
    return "Invalid simulation configuration:\n- " + "\n- ".join(lines)


def load_config(path: str | Path) -> SimulationConfig:
    """Load and validate a YAML configuration file.

    Paths are not resolved and referenced raster/CSV files are not opened at
    this stage.  The returned object is ready to be passed to a future engine.
    """

    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigLoadError(f"Configuration file not found: {config_path}")

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ConfigLoadError("PyYAML is required to load YAML configuration files") from exc

    try:
        with config_path.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"Invalid YAML in {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigLoadError(f"Could not read configuration file {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigValidationError("Invalid simulation configuration:\n- config: root must be a mapping")

    try:
        return SimulationConfig.model_validate(raw)
    except ValidationError as exc:
        details = exc.errors()
        raise ConfigValidationError(_format_validation_error(exc), errors=details) from exc


__all__ = [
    "BoundaryConfig",
    "BoundaryLocation",
    "BoundaryQuantity",
    "BoundaryType",
    "ConfigLoadError",
    "ConfigValidationError",
    "DomainConfig",
    "DomainType",
    "FieldType",
    "InitialConditionConfig",
    "InitialConditionType",
    "ModelConfig",
    "NumericsConfig",
    "OutputConfig",
    "OutputFormat",
    "OutputVariable",
    "RainfallConfig",
    "RoughnessConfig",
    "SimulationConfig",
    "TerrainConfig",
    "TerrainType",
    "TimeSeriesRef",
    "Units",
    "ValidationConfig",
    "VelocityFieldConfig",
    "load_config",
]
