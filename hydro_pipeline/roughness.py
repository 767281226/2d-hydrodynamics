from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Any, Sequence

from .config import DomainConfig, FieldType, NoDataStrategy, ResamplingStrategy, RoughnessConfig, TerrainField
from .dem_contract import DEMDataset
from .terrain_mapping_algorithms import map_dataset_to_field


class RoughnessError(ValueError):
    """Raised when a Manning roughness field violates its data contract."""


class _Grid2D(tuple):
    def __new__(cls, rows: Any) -> "_Grid2D":
        return super().__new__(cls, tuple(tuple(row) for row in rows))

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, tuple):
            if len(index) != 2:
                raise IndexError("a 2-D grid requires exactly two indices")
            row_index, column_index = index
            return super().__getitem__(row_index)[column_index]
        return super().__getitem__(index)


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise RoughnessError(f"{name} must be a number")
    converted = float(value)
    if not isfinite(converted) or converted <= 0.0:
        raise RoughnessError(f"{name} must be a finite number greater than 0")
    return converted


def _align(field: TerrainField, domain: DomainConfig) -> None:
    if field.shape != (domain.ny, domain.nx) or field.nx != domain.nx or field.ny != domain.ny:
        raise RoughnessError("terrain_field shape does not match domain")
    if (field.dx, field.dy, field.xmin, field.ymin) != (domain.dx, domain.dy, domain.xmin, domain.ymin):
        raise RoughnessError("terrain_field grid metadata does not match domain")


def _bool_grid(value: Sequence[Sequence[bool]], name: str, nx: int, ny: int) -> _Grid2D:
    rows = tuple(tuple(row) for row in value)
    if len(rows) != ny or any(len(row) != nx for row in rows):
        raise RoughnessError(f"{name} shape must be ({ny}, {nx})")
    if any(not isinstance(item, bool) for row in rows for item in row):
        raise RoughnessError(f"{name} values must be boolean")
    return _Grid2D(rows)


def _coverage_grid(value: Sequence[Sequence[float]], nx: int, ny: int) -> _Grid2D:
    rows = tuple(tuple(row) for row in value)
    if len(rows) != ny or any(len(row) != nx for row in rows):
        raise RoughnessError(f"coverage_ratio shape must be ({ny}, {nx})")
    out = []
    for row in rows:
        normalized = []
        for item in row:
            if isinstance(item, bool) or not isinstance(item, Real) or not isfinite(float(item)) or not 0 <= float(item) <= 1:
                raise RoughnessError("coverage_ratio values must be finite numbers between 0 and 1")
            normalized.append(float(item))
        out.append(tuple(normalized))
    return _Grid2D(out)


def _value_grid(value: Sequence[Sequence[float]], nx: int, ny: int) -> _Grid2D:
    rows = tuple(tuple(row) for row in value)
    if len(rows) != ny or any(len(row) != nx for row in rows):
        raise RoughnessError(f"values shape must be ({ny}, {nx})")
    out = []
    for row_index, row in enumerate(rows):
        normalized = []
        for column_index, item in enumerate(row):
            normalized.append(_positive(item, f"values[{row_index}][{column_index}]"))
        out.append(tuple(normalized))
    return _Grid2D(out)


@dataclass(frozen=True)
class RoughnessField:
    """Manning roughness coefficient n aligned to the model grid."""

    quantity: str
    values: Sequence[Sequence[float]]
    nx: int
    ny: int
    dx: float
    dy: float
    xmin: float
    ymin: float
    valid_mask: Sequence[Sequence[bool]] | None = None
    nodata_mask: Sequence[Sequence[bool]] | None = None
    coverage_ratio: Sequence[Sequence[float]] | None = None
    coordinate_system: str | None = None

    def __post_init__(self) -> None:
        if self.quantity not in {"roughness", "manning_n"}:
            raise RoughnessError("quantity must be 'roughness' or 'manning_n'")
        object.__setattr__(self, "quantity", "manning_n")
        if not isinstance(self.nx, int) or self.nx <= 0 or not isinstance(self.ny, int) or self.ny <= 0:
            raise RoughnessError("nx and ny must be positive integers")
        object.__setattr__(self, "values", _value_grid(self.values, self.nx, self.ny))
        valid = _bool_grid(self.valid_mask, "valid_mask", self.nx, self.ny) if self.valid_mask is not None else None
        nodata = _bool_grid(self.nodata_mask, "nodata_mask", self.nx, self.ny) if self.nodata_mask is not None else None
        if valid is not None and nodata is not None and any(v and n for row_v, row_n in zip(valid, nodata) for v, n in zip(row_v, row_n)):
            raise RoughnessError("valid_mask and nodata_mask cannot overlap")
        if valid is not None:
            object.__setattr__(self, "valid_mask", valid)
        if nodata is not None:
            object.__setattr__(self, "nodata_mask", nodata)
        if self.coverage_ratio is not None:
            object.__setattr__(self, "coverage_ratio", _coverage_grid(self.coverage_ratio, self.nx, self.ny))
        if self.coordinate_system is not None:
            if not isinstance(self.coordinate_system, str) or not self.coordinate_system.strip():
                raise RoughnessError("coordinate_system must be a non-empty string or None")
            object.__setattr__(self, "coordinate_system", self.coordinate_system.strip())

    @property
    def shape(self) -> tuple[int, int]:
        return self.ny, self.nx

    @property
    def data(self) -> Sequence[Sequence[float]]:
        return self.values

    @property
    def manning_n(self) -> Sequence[Sequence[float]]:
        return self.values

    @property
    def crs(self) -> str | None:
        return self.coordinate_system

    @classmethod
    def from_constant(cls, reference: DomainConfig | TerrainField, manning_n: Real, *, coordinate_system: str | None = None) -> "RoughnessField":
        value = _positive(manning_n, "manning_n")
        if isinstance(reference, TerrainField):
            domain = DomainConfig(xmin=reference.xmin, xmax=reference.xmin + reference.nx * reference.dx, ymin=reference.ymin, ymax=reference.ymin + reference.ny * reference.dy, dx=reference.dx, dy=reference.dy)
            _align(reference, domain)
            effective_crs = coordinate_system if coordinate_system is not None else reference.coordinate_system
        elif isinstance(reference, DomainConfig):
            domain = reference
            effective_crs = coordinate_system
        else:
            raise TypeError("reference must be a DomainConfig or TerrainField")
        valid = tuple(tuple(True for _ in range(domain.nx)) for _ in range(domain.ny))
        nodata = tuple(tuple(False for _ in range(domain.nx)) for _ in range(domain.ny))
        coverage = tuple(tuple(1.0 for _ in range(domain.nx)) for _ in range(domain.ny))
        return cls(quantity="manning_n", values=tuple(tuple(value for _ in range(domain.nx)) for _ in range(domain.ny)), nx=domain.nx, ny=domain.ny, dx=domain.dx, dy=domain.dy, xmin=domain.xmin, ymin=domain.ymin, valid_mask=valid, nodata_mask=nodata, coverage_ratio=coverage, coordinate_system=effective_crs)

    @classmethod
    def from_dataset(cls, dataset: DEMDataset, domain: DomainConfig, *, terrain_field: TerrainField | None = None, coordinate_system: str | None = None, strategy: ResamplingStrategy | str = ResamplingStrategy.AUTO, nodata_strategy: NoDataStrategy | str = NoDataStrategy.ERROR, min_valid_coverage: float | None = None) -> "RoughnessField":
        if not isinstance(dataset, DEMDataset):
            raise TypeError("dataset must be a DEMDataset")
        if terrain_field is not None:
            _align(terrain_field, domain)
            if coordinate_system is None:
                coordinate_system = terrain_field.coordinate_system
            elif terrain_field.coordinate_system is not None and terrain_field.coordinate_system.strip().upper() != coordinate_system.strip().upper():
                raise RoughnessError("terrain_field coordinate_system does not match requested model CRS")
        for row_index, row in enumerate(dataset.elevation):
            for column_index, value in enumerate(row):
                if dataset.valid_mask[row_index, column_index]:
                    _positive(value, f"roughness source values[{row_index}][{column_index}]")
        mapped = map_dataset_to_field(dataset, domain, coordinate_system=coordinate_system, strategy=strategy, nodata_strategy=nodata_strategy, min_valid_coverage=min_valid_coverage)
        return cls(quantity="manning_n", values=mapped.elevation, nx=mapped.nx, ny=mapped.ny, dx=mapped.dx, dy=mapped.dy, xmin=mapped.xmin, ymin=mapped.ymin, valid_mask=mapped.valid_mask, nodata_mask=mapped.nodata_mask, coverage_ratio=mapped.coverage_ratio, coordinate_system=coordinate_system)

    @classmethod
    def from_config(cls, config: RoughnessConfig, domain: DomainConfig, *, dataset: DEMDataset | None = None, terrain_field: TerrainField | None = None, coordinate_system: str | None = None, strategy: ResamplingStrategy | str = ResamplingStrategy.AUTO, nodata_strategy: NoDataStrategy | str = NoDataStrategy.ERROR, min_valid_coverage: float | None = None) -> "RoughnessField":
        if not isinstance(config, RoughnessConfig):
            raise TypeError("config must be a RoughnessConfig")
        if config.type is FieldType.CONSTANT:
            return cls.from_constant(terrain_field or domain, config.manning_n, coordinate_system=coordinate_system)
        if dataset is None:
            raise RoughnessError("dataset is required for raster roughness; read the configured file first")
        return cls.from_dataset(dataset, domain, terrain_field=terrain_field, coordinate_system=coordinate_system, strategy=strategy, nodata_strategy=nodata_strategy, min_valid_coverage=min_valid_coverage)


ResistanceField = RoughnessField

__all__ = ["RoughnessError", "RoughnessField", "ResistanceField"]
