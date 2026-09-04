"""Typed configuration schema and YAML loader.

This module is deliberately limited to the input contract.  It does not read
rasters, execute time series, or call a hydrodynamics solver.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isclose, isfinite
from numbers import Real
from pathlib import Path
from typing import Annotated, Any, Sequence

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

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)


def _number(value: Any) -> float:
    """Accept YAML integers/floats, but reject booleans and strings."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be a number")
    converted = float(value)
    if not isfinite(converted):
        raise ValueError("must be finite")
    return converted


def _text(value: Any) -> str:
    """Accept only strings so paths and identifiers are not silently coerced."""

    if not isinstance(value, str):
        raise ValueError("must be a string")
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


class ResamplingStrategy(str, Enum):
    AUTO = "auto"
    AREA_WEIGHTED_MEAN = "area_weighted_mean"
    BILINEAR = "bilinear"
    DIRECT = "direct"


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
    # A future preflight must require a confirmed vertical datum before
    # comparing terrain elevations with water levels. The datum value itself
    # remains intentionally unknown until supplied by the data provider.
    vertical_datum_required: StrictBool = True

    @model_validator(mode="after")
    def validate_time_window(self) -> "ModelConfig":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be greater than start_time")
        return self


def _cell_count(lower: float, upper: float, step: float, axis: str) -> int:
    span = upper - lower
    count = round(span / step)
    if count <= 0 or not isclose(span, count * step, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(
            f"{axis} length ({span:g} m) must be evenly divisible by {axis} step ({step:g} m)"
        )
    return count


class DomainConfig(_SchemaModel):
    type: DomainType = DomainType.STRUCTURED
    xmin: Number = Field(description="m")
    xmax: Number = Field(description="m")
    ymin: Number = Field(description="m")
    ymax: Number = Field(description="m")
    dx: Number = Field(ge=30, le=100, description="m")
    dy: Number = Field(ge=30, le=100, description="m")

    @model_validator(mode="after")
    def validate_geometry(self) -> "DomainConfig":
        if self.xmax <= self.xmin:
            raise ValueError("xmax must be greater than xmin")
        if self.ymax <= self.ymin:
            raise ValueError("ymax must be greater than ymin")
        _cell_count(self.xmin, self.xmax, self.dx, "x")
        _cell_count(self.ymin, self.ymax, self.dy, "y")
        return self

    @property
    def nx(self) -> int:
        """Internal x-direction cell count; never accepted as user input."""

        return _cell_count(self.xmin, self.xmax, self.dx, "x")

    @property
    def ny(self) -> int:
        """Internal y-direction cell count; never accepted as user input."""

        return _cell_count(self.ymin, self.ymax, self.dy, "y")


class NoDataStrategy(str, Enum):
    ERROR = "error"
    NEAREST = "nearest"
    INTERPOLATE = "interpolate"


class TerrainResamplingConfig(_SchemaModel):
    strategy: ResamplingStrategy = ResamplingStrategy.AUTO


class TerrainConfig(_SchemaModel):
    type: TerrainType
    file: Text | None = None
    nodata: Number | None = Field(default=None, description="dataset nodata value")
    nodata_strategy: NoDataStrategy = NoDataStrategy.ERROR
    resampling: TerrainResamplingConfig = Field(default_factory=TerrainResamplingConfig)
    # None keeps the threshold undefined until a later validation phase.
    min_valid_coverage: Number | None = Field(default=None, ge=0, le=1)
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
            if self.nodata is not None or self.nodata_strategy is not NoDataStrategy.ERROR:
                raise ValueError("nodata and non-error nodata_strategy are only valid for raster terrain")
            if self.resampling.strategy is not ResamplingStrategy.AUTO:
                raise ValueError("non-auto resampling is only valid for raster terrain")
            if self.min_valid_coverage is not None:
                raise ValueError("min_valid_coverage is only valid for raster terrain")
        return self


class _Grid2D(tuple):
    """Small dependency-free 2-D container supporting ``[j][i]`` and ``[j, i]``."""

    def __new__(cls, rows: Any) -> "_Grid2D":
        return super().__new__(cls, tuple(tuple(row) for row in rows))

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, tuple):
            if len(index) != 2:
                raise IndexError("a 2-D grid requires exactly two indices")
            row_index, column_index = index
            return super().__getitem__(row_index)[column_index]
        return super().__getitem__(index)


@dataclass(frozen=True)
class TerrainField:
    """Terrain values and per-cell quality information aligned to the grid.

    ``elevation[j][i]`` (and the conceptual ``elevation[j, i]``) is the
    representative elevation for a cell.  ``valid_mask`` uses True for a
    reliable cell; the legacy ``nodata_mask`` uses True for NoData.
    ``coverage_ratio[j][i]`` is the valid DEM area divided by that cell area.
    This container never reads or resamples a DEM.
    """

    elevation: Sequence[Sequence[float]]
    nx: int
    ny: int
    dx: float
    dy: float
    xmin: float
    ymin: float
    nodata_mask: Sequence[Sequence[bool]] | None = None
    valid_mask: Sequence[Sequence[bool]] | None = None
    coverage_ratio: Sequence[Sequence[float]] | None = None

    @staticmethod
    def _bool_grid(
        value: Sequence[Sequence[bool]], name: str, nx: int, ny: int
    ) -> tuple[tuple[bool, ...], ...]:
        try:
            rows = tuple(tuple(row) for row in value)
        except TypeError as exc:
            raise ValueError(f"TerrainField.{name} must be a 2-D boolean sequence") from exc
        if len(rows) != ny:
            raise ValueError(f"TerrainField.{name} must have {ny} rows, got {len(rows)}")
        for row_index, row in enumerate(rows):
            if len(row) != nx:
                raise ValueError(
                    f"TerrainField.{name} row {row_index} must have {nx} values, got {len(row)}"
                )
            if any(not isinstance(item, bool) for item in row):
                raise ValueError(f"TerrainField.{name} values must be boolean")
        return rows

    @staticmethod
    def _coverage_grid(
        value: Sequence[Sequence[float]], nx: int, ny: int
    ) -> tuple[tuple[float, ...], ...]:
        try:
            rows = tuple(tuple(row) for row in value)
        except TypeError as exc:
            raise ValueError("TerrainField.coverage_ratio must be a 2-D numeric sequence") from exc
        if len(rows) != ny:
            raise ValueError(f"TerrainField.coverage_ratio must have {ny} rows, got {len(rows)}")
        normalized: list[tuple[float, ...]] = []
        for row_index, row in enumerate(rows):
            if len(row) != nx:
                raise ValueError(
                    f"TerrainField.coverage_ratio row {row_index} must have {nx} values, got {len(row)}"
                )
            normalized_row: list[float] = []
            for column_index, item in enumerate(row):
                if isinstance(item, bool) or not isinstance(item, Real):
                    raise ValueError(
                        f"TerrainField.coverage_ratio[{row_index}][{column_index}] must be a number"
                    )
                converted = float(item)
                if not isfinite(converted) or not 0.0 <= converted <= 1.0:
                    raise ValueError(
                        f"TerrainField.coverage_ratio[{row_index}][{column_index}] must be between 0 and 1"
                    )
                normalized_row.append(converted)
            normalized.append(tuple(normalized_row))
        return tuple(normalized)

    def __post_init__(self) -> None:
        if isinstance(self.nx, bool) or not isinstance(self.nx, int) or self.nx <= 0:
            raise ValueError("TerrainField.nx must be a positive integer")
        if isinstance(self.ny, bool) or not isinstance(self.ny, int) or self.ny <= 0:
            raise ValueError("TerrainField.ny must be a positive integer")
        for name, value in (("dx", self.dx), ("dy", self.dy)):
            if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(float(value)) or value <= 0:
                raise ValueError(f"TerrainField.{name} must be a positive finite number")
        for name, value in (("xmin", self.xmin), ("ymin", self.ymin)):
            if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(float(value)):
                raise ValueError(f"TerrainField.{name} must be a finite number")

        try:
            rows = tuple(tuple(row) for row in self.elevation)
        except TypeError as exc:
            raise ValueError("TerrainField.elevation must be a 2-D sequence") from exc
        if len(rows) != self.ny:
            raise ValueError(f"TerrainField.elevation must have {self.ny} rows, got {len(rows)}")
        for row_index, row in enumerate(rows):
            if len(row) != self.nx:
                raise ValueError(
                    f"TerrainField.elevation row {row_index} must have {self.nx} values, got {len(row)}"
                )
            for column_index, value in enumerate(row):
                if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(float(value)):
                    raise ValueError(
                        f"TerrainField.elevation[{row_index}][{column_index}] must be a finite number"
                    )
        object.__setattr__(self, "elevation", _Grid2D(rows))

        nodata_rows = (
            self._bool_grid(self.nodata_mask, "nodata_mask", self.nx, self.ny)
            if self.nodata_mask is not None
            else None
        )
        valid_rows = (
            self._bool_grid(self.valid_mask, "valid_mask", self.nx, self.ny)
            if self.valid_mask is not None
            else None
        )
        # If only one mask is supplied, derive the other for compatibility. If
        # both are supplied, keep their independent meanings: a mapper may
        # carry a filled but still low-confidence cell in a later phase.
        if nodata_rows is not None and valid_rows is None:
            valid_rows = tuple(tuple(not item for item in row) for row in nodata_rows)
        elif valid_rows is not None and nodata_rows is None:
            nodata_rows = tuple(tuple(not item for item in row) for row in valid_rows)
        if nodata_rows is not None:
            object.__setattr__(self, "nodata_mask", _Grid2D(nodata_rows))
        if valid_rows is not None:
            object.__setattr__(self, "valid_mask", _Grid2D(valid_rows))

        if self.coverage_ratio is not None:
            object.__setattr__(
                self, "coverage_ratio", _Grid2D(self._coverage_grid(self.coverage_ratio, self.nx, self.ny))
            )

    @classmethod
    def from_domain(
        cls,
        domain: DomainConfig,
        elevation: Sequence[Sequence[float]],
        nodata_mask: Sequence[Sequence[bool]] | None = None,
        valid_mask: Sequence[Sequence[bool]] | None = None,
        coverage_ratio: Sequence[Sequence[float]] | None = None,
    ) -> "TerrainField":
        """Create a field whose dimensions and metadata come from ``domain``."""

        if not isinstance(domain, DomainConfig):
            raise TypeError("domain must be a DomainConfig")
        return cls(
            elevation=elevation,
            nx=domain.nx,
            ny=domain.ny,
            dx=domain.dx,
            dy=domain.dy,
            xmin=domain.xmin,
            ymin=domain.ymin,
            nodata_mask=nodata_mask,
            valid_mask=valid_mask,
            coverage_ratio=coverage_ratio,
        )

    @property
    def terrain_elevation(self) -> Sequence[Sequence[float]]:
        """Read-only alias emphasizing the solver-facing array semantics."""

        return self.elevation

    @property
    def shape(self) -> tuple[int, int]:
        """Array shape in ``(ny, nx)`` order."""

        return self.ny, self.nx

    @property
    def valid_area(self) -> Sequence[Sequence[float]] | None:
        """Derived valid DEM area per cell in square metres, when available."""

        if self.coverage_ratio is None:
            return None
        cell_area = self.dx * self.dy
        return _Grid2D(
            tuple(tuple(ratio * cell_area for ratio in row) for row in self.coverage_ratio)
        )


def _coerce_enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in enum_type)
            raise ValueError(f"{field_name} must be one of: {allowed}") from exc
    raise ValueError(f"{field_name} must be a {enum_type.__name__} value or string")


class TerrainMapper:
    """Configuration and in-memory DEM-to-grid mapping entry points.

    The map method is the legacy configuration-only placeholder. The
    map_dataset method maps an already-read DEMDataset without file I/O
    or reprojection.
    """

    def map(
        self,
        domain: DomainConfig,
        terrain: TerrainConfig,
        *,
        nodata_strategy: NoDataStrategy | None = None,
        resampling_strategy: ResamplingStrategy | None = None,
        coordinate_system: str | None = None,
        vertical_datum_required: bool = True,
        dem_metadata: DEMMetadata | None = None,
    ) -> TerrainField:
        """Validate interface arguments, then explicitly remain unimplemented."""

        if not isinstance(domain, DomainConfig):
            raise TypeError("domain must be a DomainConfig")
        if not isinstance(terrain, TerrainConfig):
            raise TypeError("terrain must be a TerrainConfig")
        if terrain.type is not TerrainType.RASTER:
            raise ValueError("TerrainMapper requires terrain.type to be 'raster'")
        normalized_nodata = (
            _coerce_enum(nodata_strategy, NoDataStrategy, "nodata_strategy")
            if nodata_strategy is not None
            else None
        )
        normalized_resampling = (
            _coerce_enum(resampling_strategy, ResamplingStrategy, "resampling_strategy")
            if resampling_strategy is not None
            else None
        )
        if coordinate_system is not None and (not isinstance(coordinate_system, str) or not coordinate_system.strip()):
            raise ValueError("coordinate_system must be a non-empty string when provided")
        if not isinstance(vertical_datum_required, bool):
            raise ValueError("vertical_datum_required must be a boolean")
        if dem_metadata is not None:
            DEMValidator().validate_for_model(
                dem_metadata,
                require_projected_crs=coordinate_system is not None,
                require_vertical_datum=vertical_datum_required,
            )
        effective_nodata = normalized_nodata or terrain.nodata_strategy
        effective_resampling = normalized_resampling or terrain.resampling.strategy
        if normalized_nodata is not None and normalized_nodata is not terrain.nodata_strategy:
            raise ValueError("nodata_strategy override does not match terrain.nodata_strategy")
        if normalized_resampling is not None and normalized_resampling is not terrain.resampling.strategy:
            raise ValueError("resampling_strategy override does not match terrain.resampling.strategy")
        if effective_nodata is not NoDataStrategy.ERROR:
            raise NotImplementedError(
                f"NoData strategy '{effective_nodata.value}' is declared but not implemented"
            )
        # ``effective_resampling`` is intentionally not executed here.  Its
        # value is validated so a future mapper can consume the same contract.
        _ = effective_resampling
        raise NotImplementedError(
            "TerrainMapper.map is an interface placeholder; DEM reading, CRS transformation, "
            "NoData handling, and resampling are not implemented"
        )

    def map_terrain(self, domain: DomainConfig, terrain: TerrainConfig, **kwargs: Any) -> TerrainField:
        """Descriptive alias for :meth:`map`."""

        return self.map(domain, terrain, **kwargs)

    def map_dataset(
        self,
        dataset: DEMDataset,
        domain: DomainConfig,
        *,
        coordinate_system: str | None = None,
        strategy: ResamplingStrategy | str = ResamplingStrategy.AUTO,
        nodata_strategy: NoDataStrategy | str = NoDataStrategy.ERROR,
        min_valid_coverage: float | None = None,
    ) -> TerrainField:
        """Map an already-read DEMDataset to the model grid.

        This method preserves the existing :meth:`map` configuration API. It
        performs no file I/O, CRS conversion, or Solver work.
        """

        from .terrain_mapping_algorithms import map_dataset_to_field

        return map_dataset_to_field(
            dataset,
            domain,
            coordinate_system=coordinate_system,
            strategy=strategy,
            nodata_strategy=nodata_strategy,
            min_valid_coverage=min_valid_coverage,
        )

    def map_field(self, dataset: DEMDataset, domain: DomainConfig, **kwargs: Any) -> TerrainField:
        """Alias for :meth:`map_dataset` used by future engine adapters."""

        return self.map_dataset(dataset, domain, **kwargs)


def resolve_auto_resampling_strategy(
    dem_dx: float,
    dem_dy: float,
    grid_dx: float,
    grid_dy: float,
    *,
    aligned: bool = False,
) -> ResamplingStrategy:
    """Resolve the frozen ``auto`` policy from supplied raster metadata.

    This function only classifies resolutions; it never reads or modifies a
    raster. ``aligned`` must be true only when CRS, extent, pixel boundaries,
    and alignment have already been verified by a future data-preparation
    layer. Mixed finer/coarser axes are rejected because V1.0 does not define
    that case.
    """

    if not isinstance(aligned, bool):
        raise ValueError("aligned must be a boolean")
    values = {"dem_dx": dem_dx, "dem_dy": dem_dy, "grid_dx": grid_dx, "grid_dy": grid_dy}
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(float(value)) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")

    same_resolution = isclose(dem_dx, grid_dx, rel_tol=1e-9, abs_tol=1e-9) and isclose(
        dem_dy, grid_dy, rel_tol=1e-9, abs_tol=1e-9
    )
    if same_resolution:
        if not aligned:
            raise ValueError("same-resolution DEM requires verified CRS, extent, and pixel alignment for direct mapping")
        return ResamplingStrategy.DIRECT

    finer = dem_dx <= grid_dx and dem_dy <= grid_dy and (dem_dx < grid_dx or dem_dy < grid_dy)
    coarser = dem_dx >= grid_dx and dem_dy >= grid_dy and (dem_dx > grid_dx or dem_dy > grid_dy)
    if finer:
        return ResamplingStrategy.AREA_WEIGHTED_MEAN
    if coarser:
        return ResamplingStrategy.BILINEAR
    raise ValueError("DEM resolution is finer on one axis and coarser on the other; V1.0 auto policy is undefined")

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


# Imported late to avoid a circular import while the configuration classes load.
from .terrain_mapping_algorithms import TerrainMappingError, map_dataset_to_field

__all__ = [
    "BoundaryConfig",
    "BoundaryLocation",
    "BoundaryQuantity",
    "BoundaryType",
    "ConfigLoadError",
    "ConfigValidationError",
    "DEMBandCountError",
    "DEMDataset",
    "DEMFormatError",
    "DEMMetadata",
    "DEMValidationError",
    "DEMValidator",
    "DEMReader",
    "DEMReaderDependencyError",
    "DEMReaderError",
    "GeoTIFFDEMReader",
    "GeoTIFFReader",
    "GeoTiffDEMReader",
    "GeoTiffReader",
    "PlaceholderDEMReader",
    "RasterioDEMReader",
    "DomainConfig",
    "DomainType",
    "NoDataStrategy",
    "ResamplingStrategy",
    "TerrainField",
    "TerrainMapper",
    "TerrainMappingError",
    "map_dataset_to_field",
    "resolve_auto_resampling_strategy",
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
    "TerrainResamplingConfig",
    "TerrainType",
    "TimeSeriesRef",
    "Units",
    "ValidationConfig",
    "VelocityFieldConfig",
    "load_config",
]
