"""DEM metadata, reader, and validation contracts.

The core package is dependency-free with respect to raster libraries.  The
optional :class:`GeoTIFFDEMReader` imports Rasterio only when a caller asks it
to read a file.  It does not map a DEM to the model grid or run a solver.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from math import isclose, isfinite
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Protocol


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


class _Grid2D(tuple):
    """Immutable nested rows with both ``[j][i]`` and ``[j, i]`` access."""

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
class DEMMetadata:
    """Descriptive metadata for one DEM dataset/band.

    ``vertical_datum`` and ``elevation_type`` intentionally remain ``None``
    when the source does not explicitly provide them.  Statistics are optional:
    a metadata-only read need not scan all pixels.
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
    nonfinite_pixel_count: int | None = None
    valid_coverage_ratio: float | None = None

    def __post_init__(self) -> None:
        width = _positive_integer(self.width, "width")
        height = _positive_integer(self.height, "height")
        _positive_integer(self.band_count, "band_count")
        if not isinstance(self.dtype, str) or not self.dtype.strip():
            raise ValueError("dtype must be a non-empty string")
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)
        object.__setattr__(self, "band_count", int(self.band_count))
        object.__setattr__(self, "dtype", self.dtype.strip())

        xmin = _finite_number(self.xmin, "xmin")
        xmax = _finite_number(self.xmax, "xmax")
        ymin = _finite_number(self.ymin, "ymin")
        ymax = _finite_number(self.ymax, "ymax")
        if xmax <= xmin:
            raise ValueError("xmax must be greater than xmin")
        if ymax <= ymin:
            raise ValueError("ymax must be greater than ymin")
        object.__setattr__(self, "xmin", xmin)
        object.__setattr__(self, "xmax", xmax)
        object.__setattr__(self, "ymin", ymin)
        object.__setattr__(self, "ymax", ymax)

        pixel_x = _finite_number(self.pixel_size_x, "pixel_size_x")
        pixel_y = _finite_number(self.pixel_size_y, "pixel_size_y")
        if pixel_x <= 0 or pixel_y <= 0:
            raise ValueError("pixel_size_x and pixel_size_y must be greater than 0")
        object.__setattr__(self, "pixel_size_x", pixel_x)
        object.__setattr__(self, "pixel_size_y", pixel_y)

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

        total = width * height
        counts: list[int] = []
        for field_name in ("valid_pixel_count", "nodata_pixel_count", "nonfinite_pixel_count"):
            value = getattr(self, field_name)
            if value is not None:
                normalized = _positive_integer(value, field_name, minimum=0)
                if normalized > total:
                    raise ValueError(f"{field_name} cannot exceed width * height ({total})")
                object.__setattr__(self, field_name, normalized)
                counts.append(normalized)
        if counts and sum(counts) > total:
            raise ValueError("pixel counts cannot exceed width * height when combined")

        if self.nodata_value is not None:
            object.__setattr__(self, "nodata_value", _finite_number(self.nodata_value, "nodata_value"))
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
    def bounds(self) -> tuple[float, float, float, float]:
        """Bounds in ``(xmin, ymin, xmax, ymax)`` order."""

        return self.xmin, self.ymin, self.xmax, self.ymax

    @property
    def pixel_size(self) -> tuple[float, float]:
        """Pixel size in x/y order, in source coordinate units."""

        return self.pixel_size_x, self.pixel_size_y

    @property
    def valid_ratio(self) -> float | None:
        """Dataset-level valid pixel ratio; ``None`` if pixels were not scanned."""

        if self.valid_coverage_ratio is not None:
            return self.valid_coverage_ratio
        if self.valid_pixel_count is not None:
            return self.valid_pixel_count / self.pixel_count
        return None

    @property
    def dataset_valid_ratio(self) -> float | None:
        """Explicit alias distinguishing dataset ratio from cell coverage."""

        return self.valid_ratio


@dataclass(frozen=True)
class DEMDataset(Sequence[Sequence[float]]):
    """Raw DEM values plus masks; no value is silently replaced.

    ``elevation`` retains source NoData sentinels and non-finite values exactly
    as read.  ``valid_mask`` is true only for finite, non-NoData values;
    ``nodata_mask`` identifies source mask/sentinel values and
    ``nonfinite_mask`` identifies NaN/Inf.  The concrete reader gives
    non-finite values precedence so the metadata category counts are disjoint;
    the masks remain independent for future adapters.
    """

    metadata: DEMMetadata
    elevation: Sequence[Sequence[float]]
    valid_mask: Sequence[Sequence[bool]]
    nodata_mask: Sequence[Sequence[bool]]
    nonfinite_mask: Sequence[Sequence[bool]]

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, DEMMetadata):
            raise TypeError("metadata must be a DEMMetadata instance")
        rows = tuple(tuple(row) for row in self.elevation)
        if len(rows) != self.metadata.height:
            raise ValueError(
                f"elevation must have {self.metadata.height} rows, got {len(rows)}"
            )
        for row_index, row in enumerate(rows):
            if len(row) != self.metadata.width:
                raise ValueError(
                    f"elevation row {row_index} must have {self.metadata.width} values, got {len(row)}"
                )
            for column_index, value in enumerate(row):
                if isinstance(value, bool) or not isinstance(value, Real):
                    raise ValueError(f"elevation[{row_index}][{column_index}] must be numeric")
        object.__setattr__(self, "elevation", _Grid2D(rows))
        for name in ("valid_mask", "nodata_mask", "nonfinite_mask"):
            object.__setattr__(self, name, self._mask(getattr(self, name), name))
        for row_index in range(self.metadata.height):
            for column_index in range(self.metadata.width):
                if self.valid_mask[row_index, column_index] and (
                    self.nodata_mask[row_index, column_index]
                    or self.nonfinite_mask[row_index, column_index]
                ):
                    raise ValueError(
                        "valid_mask cannot mark a cell valid when nodata_mask or nonfinite_mask is true"
                    )

    def _mask(self, value: Sequence[Sequence[bool]], name: str) -> _Grid2D:
        rows = tuple(tuple(row) for row in value)
        if len(rows) != self.metadata.height:
            raise ValueError(f"{name} must have {self.metadata.height} rows, got {len(rows)}")
        for row_index, row in enumerate(rows):
            if len(row) != self.metadata.width:
                raise ValueError(
                    f"{name} row {row_index} must have {self.metadata.width} values, got {len(row)}"
                )
            if any(not isinstance(item, bool) for item in row):
                raise ValueError(f"{name} values must be boolean")
        return _Grid2D(rows)

    def __len__(self) -> int:
        return self.metadata.height

    def __getitem__(self, index: Any) -> Any:
        return self.elevation[index]

    @property
    def shape(self) -> tuple[int, int]:
        return self.metadata.height, self.metadata.width

    @property
    def terrain_elevation(self) -> Sequence[Sequence[float]]:
        return self.elevation


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
        """Validate the minimum contract needed before model mapping."""

        return self.validate(
            metadata,
            require_projected_crs=require_projected_crs,
            require_crs=require_crs,
            require_nodata=True,
            require_vertical_datum=require_vertical_datum,
        )


class DEMReader(Protocol):
    """Interface implemented by a concrete DEM file reader."""

    def read_metadata(self, source: str | Path) -> DEMMetadata:
        """Read geometry/CRS/NoData metadata from ``source``."""

    def read_elevation(self, source: str | Path) -> Sequence[Sequence[float]]:
        """Read raw elevation values from ``source`` without replacement."""


class DEMReaderError(ValueError):
    """Base error for unsupported or unreadable DEM sources."""


class DEMReaderDependencyError(DEMReaderError, ImportError):
    """Raised when the optional raster reader dependency is unavailable."""


class DEMFormatError(DEMReaderError):
    """Raised when a source is not a supported single-band GeoTIFF."""


class DEMBandCountError(DEMFormatError):
    """Raised when a GeoTIFF has more than one band."""


class GeoTIFFDEMReader:
    """Optional Rasterio-backed reader for single-band ``.tif/.tiff`` files.

    The reader performs no reprojection, resampling, NoData filling, or model
    grid mapping.  ``read_dataset`` returns a ``DEMDataset`` whose raw values
    remain untouched and whose masks explicitly identify invalid values; the
    legacy ``read_elevation`` method returns only those raw values.
    """

    SUPPORTED_EXTENSIONS = frozenset({".tif", ".tiff"})

    @classmethod
    def _source_path(cls, source: str | Path) -> Path:
        try:
            path = Path(source)
        except TypeError as exc:
            raise DEMReaderError("DEM source must be a filesystem path or string") from exc
        if path.suffix.lower() not in cls.SUPPORTED_EXTENSIONS:
            allowed = ", ".join(sorted(cls.SUPPORTED_EXTENSIONS))
            raise DEMFormatError(f"Unsupported DEM extension '{path.suffix or '<none>'}'; expected {allowed}")
        if not path.is_file():
            raise DEMReaderError(f"DEM file not found: {path}")
        return path

    @staticmethod
    def _rasterio() -> Any:
        try:
            import rasterio
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise DEMReaderDependencyError(
                "GeoTIFF reading requires optional dependency 'rasterio'; "
                "install the raster extra (for example: pip install '.[raster]')"
            ) from exc
        return rasterio

    @classmethod
    def _metadata_from_dataset(cls, dataset: Any) -> DEMMetadata:
        if dataset.count != 1:
            raise DEMBandCountError(
                f"V1 DEM reader supports exactly one band; file contains {dataset.count} bands"
            )
        crs = dataset.crs.to_wkt() if dataset.crs is not None else None
        horizontal_unit = getattr(dataset.crs, "linear_units", None) if dataset.crs is not None else None
        # Rasterio exposes this only when the source explicitly records a band
        # unit; no vertical datum or CRS is inferred from the numeric values.
        units = dataset.units or ()
        vertical_unit = units[0] if units and units[0] else None
        tags = dataset.tags()
        return DEMMetadata(
            width=dataset.width,
            height=dataset.height,
            band_count=dataset.count,
            dtype=str(dataset.dtypes[0]),
            xmin=dataset.bounds.left,
            xmax=dataset.bounds.right,
            ymin=dataset.bounds.bottom,
            ymax=dataset.bounds.top,
            pixel_size_x=abs(dataset.res[0]),
            pixel_size_y=abs(dataset.res[1]),
            transform=tuple(dataset.transform),
            crs=crs,
            horizontal_unit=horizontal_unit,
            nodata_value=dataset.nodata,
            vertical_unit=vertical_unit,
            vertical_datum=None,
            elevation_type=None,
            area_or_point=tags.get("AREA_OR_POINT"),
        )

    def read_metadata(self, source: str | Path) -> DEMMetadata:
        path = self._source_path(source)
        try:
            with self._rasterio().open(path) as dataset:
                return self._metadata_from_dataset(dataset)
        except DEMReaderError:
            raise
        except Exception as exc:
            raise DEMReaderError(f"Could not read DEM metadata from {path}: {exc}") from exc

    def read_dataset(self, source: str | Path) -> DEMDataset:
        path = self._source_path(source)
        try:
            rasterio = self._rasterio()
            with rasterio.open(path) as dataset:
                metadata = self._metadata_from_dataset(dataset)
                raw = dataset.read(1, masked=False)
                source_mask = dataset.read_masks(1)
                rows = tuple(tuple(row.tolist()) for row in raw)
                nodata = metadata.nodata_value
                nodata_rows: list[tuple[bool, ...]] = []
                nonfinite_rows: list[tuple[bool, ...]] = []
                valid_rows: list[tuple[bool, ...]] = []
                for row_index, row in enumerate(rows):
                    nodata_row: list[bool] = []
                    nonfinite_row: list[bool] = []
                    valid_row: list[bool] = []
                    for column_index, value in enumerate(row):
                        is_nonfinite = not isfinite(float(value))
                        is_nodata = (
                            not is_nonfinite
                            and (
                                bool(source_mask[row_index, column_index] == 0)
                                or (nodata is not None and float(value) == float(nodata))
                            )
                        )
                        nodata_row.append(is_nodata)
                        nonfinite_row.append(is_nonfinite)
                        valid_row.append(not is_nodata and not is_nonfinite)
                    nodata_rows.append(tuple(nodata_row))
                    nonfinite_rows.append(tuple(nonfinite_row))
                    valid_rows.append(tuple(valid_row))
                valid_count = sum(sum(row) for row in valid_rows)
                nodata_count = sum(sum(row) for row in nodata_rows)
                nonfinite_count = sum(sum(row) for row in nonfinite_rows)
                metadata = replace(
                    metadata,
                    valid_pixel_count=valid_count,
                    nodata_pixel_count=nodata_count,
                    nonfinite_pixel_count=nonfinite_count,
                    valid_coverage_ratio=valid_count / metadata.pixel_count,
                )
                return DEMDataset(
                    metadata=metadata,
                    elevation=rows,
                    valid_mask=valid_rows,
                    nodata_mask=nodata_rows,
                    nonfinite_mask=nonfinite_rows,
                )
        except DEMReaderError:
            raise
        except Exception as exc:
            raise DEMReaderError(f"Could not read DEM elevation from {path}: {exc}") from exc

    def read_elevation(self, source: str | Path) -> Sequence[Sequence[float]]:
        """Return raw values only, preserving NoData and non-finite values."""

        return self.read_dataset(source).elevation

    def read(self, source: str | Path) -> DEMDataset:
        """Convenience alias for :meth:`read_dataset`."""

        return self.read_dataset(source)

    def read_values(self, source: str | Path) -> Sequence[Sequence[float]]:
        """Compatibility alias for :meth:`read_elevation`."""

        return self.read_elevation(source)


# Common spelling aliases for callers and future adapters.
GeoTiffDEMReader = GeoTIFFDEMReader
GeoTIFFReader = GeoTIFFDEMReader
GeoTiffReader = GeoTIFFDEMReader
RasterioDEMReader = GeoTIFFDEMReader


class PlaceholderDEMReader:
    """Explicit non-implementation used when no concrete reader is selected."""

    def read_metadata(self, source: str | Path) -> DEMMetadata:
        raise NotImplementedError(
            "DEM reading is not implemented; provide a concrete DEMReader in a later phase"
        )

    def read_elevation(self, source: str | Path) -> Sequence[Sequence[float]]:
        raise NotImplementedError(
            "DEM elevation reading is not implemented; provide a concrete DEMReader in a later phase"
        )

    def read_dataset(self, source: str | Path) -> DEMDataset:
        raise NotImplementedError(
            "DEM dataset reading is not implemented; provide a concrete DEMReader in a later phase"
        )


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
]
