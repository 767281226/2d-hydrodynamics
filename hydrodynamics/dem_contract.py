"""Dependency-free DEM metadata and validation contracts.

This module deliberately does not open raster files.  A future concrete DEM
reader (for example one backed by Rasterio) can produce :class:`DEMMetadata`
and pass it to :class:`DEMValidator` before a mapper is invoked.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isclose, isfinite
from numbers import Integral, Real
from pathlib import Path
from typing import Protocol, Sequence


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field_name} must be a number")
    converted = float(value)
    if not isfinite(converted):
        raise ValueError(f"{field_name} must be finite")
    return converted


def _positive_integer(value: object, field_name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")
    return int(value)


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string or None")
    return value.strip()


@dataclass(frozen=True)
class DEMMetadata:
    """Descriptive metadata for one DEM band or dataset.

    Counts and ``valid_coverage_ratio`` describe the selected band and are
    optional because a metadata reader may inspect geometry before scanning
    values. ``vertical_datum`` and ``elevation_type`` intentionally default to
    ``None`` when the source does not provide them.
    """

    width: int
    height: int
    band_count: int
    dtype: str
    xmin: float
    xmax: float
    ymin: float
    ymax: float
    pixel_size_x: float
    pixel_size_y: float
    transform: Sequence[float]
    crs: str | None = None
    horizontal_unit: str | None = None
    nodata_value: float | int | None = None
    vertical_unit: str | None = None
    vertical_datum: str | None = None
    elevation_type: str | None = None
    area_or_point: str | None = None
    valid_pixel_count: int | None = None
    nodata_pixel_count: int | None = None
    valid_coverage_ratio: float | None = None

    def __post_init__(self) -> None:
        _positive_integer(self.width, "width")
        _positive_integer(self.height, "height")
        _positive_integer(self.band_count, "band_count")
        if not isinstance(self.dtype, str) or not self.dtype.strip():
            raise ValueError("dtype must be a non-empty string")

        xmin = _finite_number(self.xmin, "xmin")
        xmax = _finite_number(self.xmax, "xmax")
        ymin = _finite_number(self.ymin, "ymin")
        ymax = _finite_number(self.ymax, "ymax")
        if xmax <= xmin:
            raise ValueError("xmax must be greater than xmin")
        if ymax <= ymin:
            raise ValueError("ymax must be greater than ymin")
        _finite_number(self.pixel_size_x, "pixel_size_x")
        _finite_number(self.pixel_size_y, "pixel_size_y")
        if self.pixel_size_x <= 0 or self.pixel_size_y <= 0:
            raise ValueError("pixel_size_x and pixel_size_y must be greater than 0")

        try:
            transform = tuple(_finite_number(value, "transform") for value in self.transform)
        except TypeError as exc:
            raise ValueError("transform must be a sequence of finite numbers") from exc
        if len(transform) not in (6, 9):
            raise ValueError("transform must contain 6 affine coefficients or 9 matrix values")
        object.__setattr__(self, "transform", transform)

        for field_name in (
            "crs",
            "horizontal_unit",
            "vertical_unit",
            "vertical_datum",
            "elevation_type",
            "area_or_point",
        ):
            object.__setattr__(self, field_name, _optional_text(getattr(self, field_name), field_name))

        total = self.width * self.height
        for field_name in ("valid_pixel_count", "nodata_pixel_count"):
            value = getattr(self, field_name)
            if value is not None:
                _positive_integer(value, field_name, minimum=0)
                if value > total:
                    raise ValueError(f"{field_name} cannot exceed width * height ({total})")
        if self.valid_pixel_count is not None and self.nodata_pixel_count is not None:
            if self.valid_pixel_count + self.nodata_pixel_count > total:
                raise ValueError("valid_pixel_count + nodata_pixel_count cannot exceed width * height")

        if self.nodata_value is not None:
            _finite_number(self.nodata_value, "nodata_value")
        if self.valid_coverage_ratio is not None:
            ratio = _finite_number(self.valid_coverage_ratio, "valid_coverage_ratio")
            if not 0.0 <= ratio <= 1.0:
                raise ValueError("valid_coverage_ratio must be between 0 and 1")
            if self.valid_pixel_count is not None and not isclose(
                ratio, self.valid_pixel_count / total, rel_tol=1e-6, abs_tol=1e-6
            ):
                raise ValueError("valid_coverage_ratio does not match valid_pixel_count / (width * height)")

    @property
    def pixel_count(self) -> int:
        return self.width * self.height

    @property
    def valid_ratio(self) -> float | None:
        """Dataset-level valid pixel ratio, computed when counts are available."""

        if self.valid_coverage_ratio is not None:
            return self.valid_coverage_ratio
        if self.valid_pixel_count is not None:
            return self.valid_pixel_count / self.pixel_count
        return None

    @property
    def dataset_valid_ratio(self) -> float | None:
        """Explicitly named alias distinguishing dataset coverage from cell coverage."""

        return self.valid_ratio


class DEMValidationError(ValueError):
    """Raised when DEM metadata fails the data contract."""

    def __init__(self, message: str, *, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class DEMValidator:
    """Validate metadata without reading a file or changing any value."""

    def validate(
        self,
        metadata: DEMMetadata,
        *,
        require_projected_crs: bool = False,
        require_crs: bool | None = None,
        require_nodata: bool = True,
        require_statistics: bool = False,
        require_vertical_datum: bool = False,
    ) -> DEMMetadata:
        if not isinstance(metadata, DEMMetadata):
            raise TypeError("metadata must be a DEMMetadata instance")
        errors: list[str] = []
        if require_crs is not None:
            if not isinstance(require_crs, bool):
                raise ValueError("require_crs must be a boolean when provided")
            require_projected_crs = require_crs
        if require_projected_crs and metadata.crs is None:
            errors.append("crs is required when a projected model CRS is required")
        if require_nodata and metadata.nodata_value is None:
            errors.append("nodata_value must be explicitly provided; no default is assumed")
        if require_vertical_datum and metadata.vertical_datum is None:
            errors.append("vertical_datum is required before terrain can be compared with water levels")
        if require_statistics:
            if metadata.valid_pixel_count is None:
                errors.append("valid_pixel_count is required for statistics validation")
            if metadata.nodata_pixel_count is None:
                errors.append("nodata_pixel_count is required for statistics validation")
            if metadata.valid_ratio is None:
                errors.append("valid_pixel_count or valid_coverage_ratio is required for statistics validation")
        if errors:
            raise DEMValidationError("Invalid DEM metadata:\n- " + "\n- ".join(errors), errors=errors)
        return metadata

    def validate_for_model(
        self,
        metadata: DEMMetadata,
        *,
        require_projected_crs: bool = True,
        require_crs: bool | None = None,
        require_vertical_datum: bool = False,
    ) -> DEMMetadata:
        """Validate the minimum contract needed before model mapping.

        ``require_vertical_datum`` is opt-in because the current sample DEM
        reports its vertical unit but not its datum.
        """

        return self.validate(
            metadata,
            require_projected_crs=require_projected_crs,
            require_crs=require_crs,
            require_nodata=True,
            require_vertical_datum=require_vertical_datum,
        )


class DEMReader(Protocol):
    """Interface implemented later by a concrete raster reader."""

    def read_metadata(self, source: str | Path) -> DEMMetadata:
        """Read geometry/CRS/NoData metadata from ``source``."""

    def read_elevation(self, source: str | Path) -> Sequence[Sequence[float]]:
        """Read selected-band elevation values without mapping them."""


class PlaceholderDEMReader:
    """Explicit non-implementation used to document the Reader boundary."""

    def read_metadata(self, source: str | Path) -> DEMMetadata:
        raise NotImplementedError(
            "DEM reading is not implemented; provide a concrete DEMReader in a later phase"
        )

    def read_elevation(self, source: str | Path) -> Sequence[Sequence[float]]:
        raise NotImplementedError(
            "DEM elevation reading is not implemented; provide a concrete DEMReader in a later phase"
        )


__all__ = [
    "DEMMetadata",
    "DEMReader",
    "DEMValidationError",
    "DEMValidator",
    "PlaceholderDEMReader",
]
