from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isclose, isfinite
from numbers import Real
from typing import TYPE_CHECKING, Any, Sequence

from .config import (
    DomainConfig,
    InitialConditionQuantity,
    NoDataStrategy,
    ResamplingStrategy,
    TerrainField,
)
from .dem_contract import DEMDataset
from .terrain_mapping_algorithms import (
    TerrainMappingError,
    _primary_epsg_code,
    map_dataset_to_field,
)

if TYPE_CHECKING:
    from .config import InitialConditionConfig


class InitialConditionError(ValueError):
    """Raised when an initial depth or water-level field is unsafe to build."""


_ABS_TOL = 1e-8
_REL_TOL = 1e-9


class _Grid2D(tuple):
    """Immutable nested rows supporting both [j][i] and [j, i]."""

    def __new__(cls, rows: Any) -> _Grid2D:
        return super().__new__(cls, tuple(tuple(row) for row in rows))

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, tuple):
            if len(index) != 2:
                raise IndexError("a 2-D grid requires exactly two indices")
            row_index, column_index = index
            return super().__getitem__(row_index)[column_index]
        return super().__getitem__(index)


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise InitialConditionError(f"{name} must be a number")
    converted = float(value)
    if not isfinite(converted):
        raise InitialConditionError(f"{name} must be finite")
    return converted


def _datum(value: Any, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise InitialConditionError(
                "vertical_datum is required and must be a confirmed non-empty string"
            )
        return None
    if not isinstance(value, str) or not value.strip():
        raise InitialConditionError("vertical_datum must be a confirmed non-empty string")
    return value.strip()


def _quantity(value: InitialConditionQuantity | str) -> InitialConditionQuantity:
    if isinstance(value, InitialConditionQuantity):
        return value
    if isinstance(value, str):
        try:
            return InitialConditionQuantity(value)
        except ValueError as exc:
            raise InitialConditionError("quantity must be one of: depth, level") from exc
    raise InitialConditionError("quantity must be an InitialConditionQuantity or string")


def _bool_grid(value: Sequence[Sequence[bool]], name: str, nx: int, ny: int) -> _Grid2D:
    try:
        rows = tuple(tuple(row) for row in value)
    except TypeError as exc:
        raise InitialConditionError(f"{name} must be a 2-D boolean sequence") from exc
    if len(rows) != ny:
        raise InitialConditionError(f"{name} must have {ny} rows, got {len(rows)}")
    for row_index, row in enumerate(rows):
        if len(row) != nx:
            raise InitialConditionError(f"{name} row {row_index} must have {nx} values, got {len(row)}")
        if any(not isinstance(item, bool) for item in row):
            raise InitialConditionError(f"{name} values must be boolean")
    return _Grid2D(rows)


def _coverage_grid(value: Sequence[Sequence[float]], nx: int, ny: int) -> _Grid2D:
    try:
        rows = tuple(tuple(row) for row in value)
    except TypeError as exc:
        raise InitialConditionError("coverage_ratio must be a 2-D numeric sequence") from exc
    if len(rows) != ny:
        raise InitialConditionError(f"coverage_ratio must have {ny} rows, got {len(rows)}")
    result: list[tuple[float, ...]] = []
    for row_index, row in enumerate(rows):
        if len(row) != nx:
            raise InitialConditionError(f"coverage_ratio row {row_index} must have {nx} values, got {len(row)}")
        out_row: list[float] = []
        for column_index, item in enumerate(row):
            number = _number(item, f"coverage_ratio[{row_index}][{column_index}]")
            if not 0.0 <= number <= 1.0:
                raise InitialConditionError(
                    f"coverage_ratio[{row_index}][{column_index}] must be between 0 and 1"
                )
            out_row.append(number)
        result.append(tuple(out_row))
    return _Grid2D(result)


def _value_grid(value: Sequence[Sequence[float]], nx: int, ny: int, quantity: InitialConditionQuantity) -> _Grid2D:
    try:
        rows = tuple(tuple(row) for row in value)
    except TypeError as exc:
        raise InitialConditionError("values must be a 2-D numeric sequence") from exc
    if len(rows) != ny:
        raise InitialConditionError(f"values must have {ny} rows, got {len(rows)}")
    result: list[tuple[float, ...]] = []
    for row_index, row in enumerate(rows):
        if len(row) != nx:
            raise InitialConditionError(f"values row {row_index} must have {nx} values, got {len(row)}")
        out_row: list[float] = []
        for column_index, item in enumerate(row):
            number = _number(item, f"values[{row_index}][{column_index}]")
            if quantity is InitialConditionQuantity.DEPTH and number < 0.0:
                raise InitialConditionError(
                    f"values[{row_index}][{column_index}] must be greater than or equal to 0 m for depth"
                )
            out_row.append(number)
        result.append(tuple(out_row))
    return _Grid2D(result)


def _metadata_from_terrain(field: TerrainField) -> dict[str, Any]:
    return {
        "nx": field.nx,
        "ny": field.ny,
        "dx": field.dx,
        "dy": field.dy,
        "xmin": field.xmin,
        "ymin": field.ymin,
        "coordinate_system": field.coordinate_system,
    }


def _quality_from_terrain(field: TerrainField) -> tuple[Any, Any, Any]:
    valid = field.valid_mask
    nodata = field.nodata_mask
    coverage = field.coverage_ratio
    if valid is None:
        if coverage is None:
            valid = _Grid2D(tuple(tuple(True for _ in range(field.nx)) for _ in range(field.ny)))
        else:
            valid = _Grid2D(tuple(tuple(value > 0.0 for value in row) for value in coverage))
    if nodata is None:
        nodata = _Grid2D(tuple(tuple(not item for item in row) for row in valid))
    if coverage is None:
        coverage = _Grid2D(tuple(tuple(1.0 if valid[row][column] else 0.0 for column in range(field.nx)) for row in range(field.ny)))
    return valid, nodata, coverage


def _check_alignment(field: TerrainField, domain: DomainConfig) -> None:
    if field.shape != (domain.ny, domain.nx) or field.nx != domain.nx or field.ny != domain.ny:
        raise InitialConditionError(
            f"terrain_field shape {field.shape} does not match domain shape {(domain.ny, domain.nx)}"
        )
    if not (
        isclose(field.dx, domain.dx, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
        and isclose(field.dy, domain.dy, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
        and isclose(field.xmin, domain.xmin, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
        and isclose(field.ymin, domain.ymin, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
    ):
        raise InitialConditionError("terrain_field grid metadata does not match domain")


@dataclass(frozen=True)
class InitialConditionField:
    """Immutable depth or absolute water-level values aligned to a model grid.

    ``quantity='depth'`` stores water depth relative to mapped terrain; it is
    not an absolute elevation. ``quantity='level'`` stores absolute water-surface
    elevation. No Solver or time stepping is performed by this class.
    """

    quantity: InitialConditionQuantity | str
    values: Sequence[Sequence[float]]
    nx: int
    ny: int
    dx: float
    dy: float
    xmin: float
    ymin: float
    vertical_datum: str | None = None
    valid_mask: Sequence[Sequence[bool]] | None = None
    nodata_mask: Sequence[Sequence[bool]] | None = None
    coverage_ratio: Sequence[Sequence[float]] | None = None
    coordinate_system: str | None = None
    terrain_elevation: Sequence[Sequence[float]] | None = None

    def __post_init__(self) -> None:
        quantity = _quantity(self.quantity)
        object.__setattr__(self, "quantity", quantity)
        if isinstance(self.nx, bool) or not isinstance(self.nx, int) or self.nx <= 0:
            raise InitialConditionError("nx must be a positive integer")
        if isinstance(self.ny, bool) or not isinstance(self.ny, int) or self.ny <= 0:
            raise InitialConditionError("ny must be a positive integer")
        dx = _number(self.dx, "dx")
        dy = _number(self.dy, "dy")
        if dx <= 0 or dy <= 0:
            raise InitialConditionError("dx and dy must be positive")
        object.__setattr__(self, "dx", dx)
        object.__setattr__(self, "dy", dy)
        object.__setattr__(self, "xmin", _number(self.xmin, "xmin"))
        object.__setattr__(self, "ymin", _number(self.ymin, "ymin"))
        object.__setattr__(self, "vertical_datum", _datum(self.vertical_datum, required=quantity is InitialConditionQuantity.LEVEL))
        if self.coordinate_system is not None:
            if not isinstance(self.coordinate_system, str) or not self.coordinate_system.strip():
                raise InitialConditionError("coordinate_system must be a non-empty string or None")
            object.__setattr__(self, "coordinate_system", self.coordinate_system.strip())
        object.__setattr__(self, "values", _value_grid(self.values, self.nx, self.ny, quantity))
        valid = _bool_grid(self.valid_mask, "valid_mask", self.nx, self.ny) if self.valid_mask is not None else None
        nodata = _bool_grid(self.nodata_mask, "nodata_mask", self.nx, self.ny) if self.nodata_mask is not None else None
        if valid is None and nodata is not None:
            valid = _Grid2D(tuple(tuple(not item for item in row) for row in nodata))
        if nodata is None and valid is not None:
            nodata = _Grid2D(tuple(tuple(not item for item in row) for row in valid))
        if valid is not None and nodata is not None:
            for row_index, row in enumerate(valid):
                for column_index, item in enumerate(row):
                    if item and nodata[row_index, column_index]:
                        raise InitialConditionError(
                            f"valid_mask and nodata_mask overlap at [{row_index}][{column_index}]"
                        )
        if valid is not None:
            object.__setattr__(self, "valid_mask", valid)
        if nodata is not None:
            object.__setattr__(self, "nodata_mask", nodata)
        if self.coverage_ratio is not None:
            object.__setattr__(self, "coverage_ratio", _coverage_grid(self.coverage_ratio, self.nx, self.ny))
        if self.terrain_elevation is not None:
            object.__setattr__(self, "terrain_elevation", _value_grid(self.terrain_elevation, self.nx, self.ny, InitialConditionQuantity.LEVEL))

    def __len__(self) -> int:
        return self.ny

    def __getitem__(self, index: Any) -> Any:
        return self.values[index]

    @property
    def shape(self) -> tuple[int, int]:
        return self.ny, self.nx

    @property
    def data(self) -> Sequence[Sequence[float]]:
        return self.values

    @property
    def array(self) -> Sequence[Sequence[float]]:
        return self.values

    @property
    def kind(self) -> str:
        return self.quantity.value

    @property
    def field(self) -> Sequence[Sequence[float]]:
        return self.values

    @property
    def elevation(self) -> Sequence[Sequence[float]]:
        """Alias for callers that use generic field/elevation terminology."""

        return self.values

    @property
    def depth(self) -> Sequence[Sequence[float]] | None:
        return self.values if self.quantity is InitialConditionQuantity.DEPTH else None

    @property
    def level(self) -> Sequence[Sequence[float]] | None:
        return self.values if self.quantity is InitialConditionQuantity.LEVEL else None

    @property
    def water_depth(self) -> Sequence[Sequence[float]] | None:
        return self.depth

    @property
    def water_level(self) -> Sequence[Sequence[float]] | None:
        return self.level

    @property
    def crs(self) -> str | None:
        return self.coordinate_system

    @property
    def valid_area(self) -> Sequence[Sequence[float]] | None:
        if self.coverage_ratio is None:
            return None
        area = self.dx * self.dy
        return _Grid2D(tuple(tuple(value * area for value in row) for row in self.coverage_ratio))

    @property
    def water_surface_elevation(self) -> Sequence[Sequence[float]]:
        if self.quantity is InitialConditionQuantity.LEVEL:
            return self.values
        if self.terrain_elevation is None:
            raise InitialConditionError(
                "depth field requires terrain_elevation to derive water-surface elevation"
            )
        return _Grid2D(
            tuple(
                tuple(float(depth) + float(terrain) for depth, terrain in zip(depth_row, terrain_row))
                for depth_row, terrain_row in zip(self.values, self.terrain_elevation)
            )
        )

    @classmethod
    def from_constant_depth(
        cls, terrain_field: TerrainField, depth: Real, *, vertical_datum: str | None = None
    ) -> InitialConditionField:
        if not isinstance(terrain_field, TerrainField):
            raise TypeError("terrain_field must be a TerrainField")
        value = _number(depth, "depth")
        if value < 0:
            raise InitialConditionError("depth must be greater than or equal to 0 m")
        datum = _datum(vertical_datum)
        if datum is not None and terrain_field.vertical_datum is not None and datum != terrain_field.vertical_datum:
            raise InitialConditionError("vertical_datum does not match terrain_field.vertical_datum")
        valid, nodata, coverage = _quality_from_terrain(terrain_field)
        return cls(
            quantity=InitialConditionQuantity.DEPTH,
            values=tuple(tuple(value for _ in range(terrain_field.nx)) for _ in range(terrain_field.ny)),
            **_metadata_from_terrain(terrain_field),
            vertical_datum=datum or terrain_field.vertical_datum,
            valid_mask=valid,
            nodata_mask=nodata,
            coverage_ratio=coverage,
            terrain_elevation=terrain_field.elevation,
        )

    @classmethod
    def from_constant_level(
        cls, reference: DomainConfig | TerrainField, level: Real, *, vertical_datum: str | None = None
    ) -> InitialConditionField:
        value = _number(level, "level")
        datum = _datum(vertical_datum)
        if isinstance(reference, TerrainField):
            if reference.vertical_datum is None:
                raise InitialConditionError(
                    "terrain_field.vertical_datum is unknown; absolute level is not safe"
                )
            if datum is None:
                datum = reference.vertical_datum
            elif reference.vertical_datum != datum:
                raise InitialConditionError("vertical_datum does not match terrain_field.vertical_datum")
            valid, nodata, coverage = _quality_from_terrain(reference)
            return cls(
                quantity=InitialConditionQuantity.LEVEL,
                values=tuple(tuple(value for _ in range(reference.nx)) for _ in range(reference.ny)),
                **_metadata_from_terrain(reference),
                vertical_datum=datum,
                valid_mask=valid,
                nodata_mask=nodata,
                coverage_ratio=coverage,
                terrain_elevation=reference.elevation,
            )
        if not isinstance(reference, DomainConfig):
            raise TypeError("reference must be a DomainConfig or TerrainField")
        return cls(
            quantity=InitialConditionQuantity.LEVEL,
            values=tuple(tuple(value for _ in range(reference.nx)) for _ in range(reference.ny)),
            nx=reference.nx,
            ny=reference.ny,
            dx=reference.dx,
            dy=reference.dy,
            xmin=reference.xmin,
            ymin=reference.ymin,
            vertical_datum=datum,
            valid_mask=tuple(tuple(True for _ in range(reference.nx)) for _ in range(reference.ny)),
            nodata_mask=tuple(tuple(False for _ in range(reference.nx)) for _ in range(reference.ny)),
            coverage_ratio=tuple(tuple(1.0 for _ in range(reference.nx)) for _ in range(reference.ny)),
        )

    @classmethod
    def from_dataset(
        cls,
        dataset: DEMDataset,
        domain: DomainConfig,
        *,
        quantity: InitialConditionQuantity | str = InitialConditionQuantity.DEPTH,
        terrain_field: TerrainField | None = None,
        coordinate_system: str | None = None,
        vertical_datum: str | None = None,
        strategy: ResamplingStrategy | str = ResamplingStrategy.AUTO,
        nodata_strategy: NoDataStrategy | str = NoDataStrategy.ERROR,
        min_valid_coverage: float | None = None,
    ) -> InitialConditionField:
        selected = _quantity(quantity)
        if not isinstance(dataset, DEMDataset):
            raise TypeError("dataset must be a DEMDataset")
        if not isinstance(domain, DomainConfig):
            raise TypeError("domain must be a DomainConfig")
        if terrain_field is not None:
            if not isinstance(terrain_field, TerrainField):
                raise TypeError("terrain_field must be a TerrainField or None")
            _check_alignment(terrain_field, domain)
        source_datum = _datum(dataset.metadata.vertical_datum)
        datum = _datum(vertical_datum)
        if selected is InitialConditionQuantity.LEVEL:
            if source_datum is None:
                raise InitialConditionError(
                    "DEM level raster vertical datum is unknown; a confirmed datum is required"
                )
            if datum is None:
                datum = source_datum
            elif source_datum != datum:
                raise InitialConditionError("vertical_datum does not match DEMDataset.metadata.vertical_datum")
            if terrain_field is not None:
                if terrain_field.vertical_datum is None:
                    raise InitialConditionError("terrain_field.vertical_datum is unknown for an absolute level field")
                if terrain_field.vertical_datum != datum:
                    raise InitialConditionError("vertical_datum does not match terrain_field.vertical_datum")
        else:
            if datum is not None and source_datum is not None and datum != source_datum:
                raise InitialConditionError("vertical_datum does not match DEMDataset.metadata.vertical_datum")
            if datum is not None and terrain_field is not None and terrain_field.vertical_datum is not None and datum != terrain_field.vertical_datum:
                raise InitialConditionError("vertical_datum does not match terrain_field.vertical_datum")
            for row_index, row in enumerate(dataset.elevation):
                for column_index, value in enumerate(row):
                    if dataset.valid_mask[row_index, column_index] and isfinite(float(value)) and float(value) < 0.0:
                        raise InitialConditionError(
                            f"initial depth raster values[{row_index}][{column_index}] must be >= 0 m"
                        )
        effective_crs = (
            coordinate_system
            if coordinate_system is not None
            else (terrain_field.coordinate_system if terrain_field is not None else None)
        )
        if effective_crs is not None and (not isinstance(effective_crs, str) or not effective_crs.strip()):
            raise InitialConditionError("coordinate_system must be a non-empty string when provided")
        if terrain_field is not None and terrain_field.coordinate_system is not None and effective_crs is not None:
            left = terrain_field.coordinate_system.strip().upper()
            right = effective_crs.strip().upper()
            if left != right and _primary_epsg_code(left) != _primary_epsg_code(right):
                raise InitialConditionError(
                    "terrain_field coordinate_system does not match the requested model CRS"
                )
        mapped = map_dataset_to_field(
            dataset,
            domain,
            coordinate_system=effective_crs,
            strategy=strategy,
            nodata_strategy=nodata_strategy,
            min_valid_coverage=min_valid_coverage,
        )
        if mapped.shape != (domain.ny, domain.nx):
            raise InitialConditionError("mapped initial-condition field shape does not match domain")
        return cls(
            quantity=selected,
            values=mapped.elevation,
            nx=mapped.nx,
            ny=mapped.ny,
            dx=mapped.dx,
            dy=mapped.dy,
            xmin=mapped.xmin,
            ymin=mapped.ymin,
            vertical_datum=datum or mapped.vertical_datum,
            valid_mask=mapped.valid_mask,
            nodata_mask=mapped.nodata_mask,
            coverage_ratio=mapped.coverage_ratio,
            coordinate_system=effective_crs,
            terrain_elevation=terrain_field.elevation if terrain_field is not None else None,
        )

    @classmethod
    def from_config(
        cls,
        config: InitialConditionConfig,
        domain: DomainConfig,
        *,
        dataset: DEMDataset | None = None,
        terrain_field: TerrainField | None = None,
        coordinate_system: str | None = None,
        strategy: ResamplingStrategy | str = ResamplingStrategy.AUTO,
        nodata_strategy: NoDataStrategy | str = NoDataStrategy.ERROR,
        min_valid_coverage: float | None = None,
    ) -> InitialConditionField:
        """Build a field from ``InitialConditionConfig`` without file I/O.

        A caller supplies ``dataset`` after reading the configured raster. The
        method deliberately does not resolve paths or attach the reserved
        velocity field.
        """

        from .config import InitialConditionConfig, InitialConditionType

        if not isinstance(config, InitialConditionConfig):
            raise TypeError("config must be an InitialConditionConfig")
        if not isinstance(domain, DomainConfig):
            raise TypeError("domain must be a DomainConfig")
        if config.velocity is not None:
            raise NotImplementedError(
                "initial-condition velocity is reserved and not implemented"
            )
        if config.type is InitialConditionType.CONSTANT_DEPTH:
            if terrain_field is None:
                raise InitialConditionError(
                    "terrain_field is required for a relative constant_depth field"
                )
            return cls.from_constant_depth(
                terrain_field, config.depth, vertical_datum=config.vertical_datum
            )
        if config.type is InitialConditionType.CONSTANT_LEVEL:
            return cls.from_constant_level(
                terrain_field or domain, config.level, vertical_datum=config.vertical_datum
            )
        if dataset is None:
            raise InitialConditionError(
                "dataset is required for a raster initial condition; read the configured GeoTIFF first"
            )
        quantity = (
            InitialConditionQuantity.LEVEL
            if config.level_file is not None
            else InitialConditionQuantity.DEPTH
        )
        effective_crs = coordinate_system if coordinate_system is not None else (
            terrain_field.coordinate_system if terrain_field is not None else None
        )
        return cls.from_dataset(
            dataset,
            domain,
            quantity=quantity,
            terrain_field=terrain_field,
            coordinate_system=effective_crs,
            vertical_datum=config.vertical_datum,
            strategy=strategy,
            nodata_strategy=nodata_strategy,
            min_valid_coverage=min_valid_coverage,
        )

    @classmethod
    def from_raster(cls, dataset: DEMDataset, domain: DomainConfig, **kwargs: Any) -> InitialConditionField:
        """Alias for :meth:`from_dataset`."""

        return cls.from_dataset(dataset, domain, **kwargs)


InitialWaterField = InitialConditionField


__all__ = [
    "InitialConditionError",
    "InitialConditionField",
    "InitialConditionQuantity",
    "InitialWaterField",
]
