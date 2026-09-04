"""Pure-Python DEM dataset to model-grid mapping algorithms.

The functions in this module operate only on an already-read ``DEMDataset``
and a validated ``DomainConfig``.  They intentionally do not open files,
reproject coordinates, or invoke a hydrodynamics solver.
"""

from __future__ import annotations

from math import floor, isclose, isfinite
from numbers import Real
import re
from typing import Any

from .config import (
    DomainConfig,
    NoDataStrategy,
    ResamplingStrategy,
    TerrainField,
    _coerce_enum,
    resolve_auto_resampling_strategy,
)
from .dem_contract import DEMDataset


class TerrainMappingError(ValueError):
    """Raised when a DEM cannot be mapped to the requested model grid."""


_ABS_TOL = 1e-8
_REL_TOL = 1e-9


def _close(left: float, right: float) -> bool:
    return isclose(left, right, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)


def _affine_components(dataset: DEMDataset) -> tuple[float, float, float, float, float, float]:
    values = tuple(float(value) for value in dataset.metadata.transform)
    if len(values) == 6:
        return values  # a, b, c, d, e, f
    if len(values) == 9:
        a, b, c, d, e, f, g, h, i = values
        if not (_close(g, 0.0) and _close(h, 0.0) and _close(i, 1.0)):
            raise TerrainMappingError("DEM transform is not a 2-D affine homogeneous matrix")
        return a, b, c, d, e, f
    raise TerrainMappingError("DEM transform must contain 6 or 9 values")


def _validate_geometry(dataset: DEMDataset) -> tuple[float, float, float, float, float, float]:
    metadata = dataset.metadata
    if metadata.band_count != 1:
        raise TerrainMappingError(
            f"DEM mapping requires exactly one band, got {metadata.band_count}"
        )
    if dataset.shape != (metadata.height, metadata.width):
        raise TerrainMappingError(
            "DEMDataset shape does not match DEMMetadata height/width"
        )
    a, b, c, d, e, f = _affine_components(dataset)
    if not (_close(b, 0.0) and _close(d, 0.0)):
        raise TerrainMappingError(
            "DEM transform has rotation/shear; V1 mapping requires north-up axis-aligned pixels"
        )
    if a <= 0 or e >= 0:
        raise TerrainMappingError(
            "DEM transform is not north-up (expected positive x scale and negative y scale)"
        )
    pixel_x = abs(a)
    pixel_y = abs(e)
    if not (_close(pixel_x, metadata.pixel_size_x) and _close(pixel_y, metadata.pixel_size_y)):
        raise TerrainMappingError(
            "DEM transform pixel size does not match DEMMetadata pixel_size_x/pixel_size_y"
        )
    calculated_bounds = (
        c,
        f + e * metadata.height,
        c + a * metadata.width,
        f,
    )
    declared_bounds = (metadata.xmin, metadata.ymin, metadata.xmax, metadata.ymax)
    if any(not _close(actual, declared) for actual, declared in zip(calculated_bounds, declared_bounds)):
        raise TerrainMappingError(
            "DEM transform-derived bounds do not match DEMMetadata xmin/ymin/xmax/ymax"
        )
    return a, b, c, d, e, f


def _primary_epsg_code(value: str) -> str | None:
    """Return the outermost EPSG authority in an EPSG/WKT description.

    WKT commonly contains nested authorities for the base geographic CRS and
    linear units. Only the shallowest (outermost) authority identifies the
    horizontal CRS itself; matching every authority can create false matches.
    """

    simple = re.fullmatch(r"EPSG\s*:\s*(\d+)", value.strip(), flags=re.IGNORECASE)
    if simple:
        return f"EPSG:{simple.group(1)}"
    authority = re.compile(
        r"""(?:ID|AUTHORITY)\s*\[\s*["']EPSG["']\s*,\s*["']?(\d+)""",
        flags=re.IGNORECASE,
    )
    matches: list[tuple[int, str]] = []
    depth = 0
    index = 0
    while index < len(value):
        char = value[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        match = authority.match(value, index)
        if match:
            # The regex consumes the opening bracket; account for it before
            # scanning the remainder so nested authorities get a deeper level.
            depth += 1
            matches.append((depth, f"EPSG:{match.group(1)}"))
            index = match.end()
            continue
        index += 1
    if matches:
        shallowest = min(item[0] for item in matches)
        return next(code for item_depth, code in reversed(matches) if item_depth == shallowest)
    codes = re.findall(r"\bEPSG\s*:\s*(\d+)\b", value, flags=re.IGNORECASE)
    return f"EPSG:{codes[-1]}" if codes else None


_COMMON_GEOGRAPHIC_EPSG = frozenset(
    {"EPSG:4326", "EPSG:4258", "EPSG:4269", "EPSG:4490", "EPSG:4610", "EPSG:4979"}
)


def _is_geographic_crs(value: str, *, horizontal_unit: str | None = None) -> bool:
    """Best-effort, dependency-free rejection of angular geographic CRS."""

    normalized = value.strip().upper()
    root = re.match(r"^(?P<kind>PROJCRS|PROJCS|GEOGCRS|GEOGCS)\s*\[", normalized)
    # A projected WKT may contain a nested geographic base CRS and degree
    # unit, so those nested tokens do not make the horizontal CRS geographic.
    if root is not None:
        return root.group("kind") in {"GEOGCRS", "GEOGCS"}
    if horizontal_unit is not None and re.search(
        r"degree|radian|grad", horizontal_unit, flags=re.IGNORECASE
    ):
        return True
    return _primary_epsg_code(normalized) in _COMMON_GEOGRAPHIC_EPSG


def _same_crs(dataset: DEMDataset, coordinate_system: str | None) -> None:
    source_crs = dataset.metadata.crs
    if coordinate_system is None:
        raise TerrainMappingError("model coordinate_system is required for DEM mapping")
    if not isinstance(coordinate_system, str) or not coordinate_system.strip():
        raise TerrainMappingError("model coordinate_system must be a non-empty string")
    if source_crs is None or not source_crs.strip():
        raise TerrainMappingError("DEM CRS is missing; CRS cannot be guessed")
    source_normalized = " ".join(source_crs.strip().upper().split())
    model_normalized = " ".join(coordinate_system.strip().upper().split())
    if _is_geographic_crs(source_normalized, horizontal_unit=dataset.metadata.horizontal_unit):
        raise TerrainMappingError(
            "DEM CRS must be a planar/projected CRS; angular geographic coordinates are not supported"
        )
    if _is_geographic_crs(model_normalized):
        raise TerrainMappingError(
            "model coordinate_system must be a planar/projected CRS; angular geographic coordinates are not supported"
        )
    if source_normalized == model_normalized:
        return
    source_code = _primary_epsg_code(source_normalized)
    model_code = _primary_epsg_code(model_normalized)
    if source_code is not None and source_code == model_code:
        return
    raise TerrainMappingError(
        "DEM CRS does not match the model coordinate_system; CRS transformation is not performed"
    )


def _domain_inside_dem(dataset: DEMDataset, domain: DomainConfig) -> None:
    metadata = dataset.metadata
    if not (_close(domain.xmin, metadata.xmin) or domain.xmin > metadata.xmin):
        raise TerrainMappingError("model domain extends beyond DEM xmin")
    if not (_close(domain.xmax, metadata.xmax) or domain.xmax < metadata.xmax):
        raise TerrainMappingError("model domain extends beyond DEM xmax")
    if not (_close(domain.ymin, metadata.ymin) or domain.ymin > metadata.ymin):
        raise TerrainMappingError("model domain extends beyond DEM ymin")
    if not (_close(domain.ymax, metadata.ymax) or domain.ymax < metadata.ymax):
        raise TerrainMappingError("model domain extends beyond DEM ymax")

def _valid_source_value(dataset: DEMDataset, row: int, column: int) -> bool:
    value = dataset.elevation[row, column]
    if not dataset.valid_mask[row, column]:
        return False
    if dataset.nodata_mask[row, column] or dataset.nonfinite_mask[row, column]:
        return False
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    numeric = float(value)
    if not isfinite(numeric):
        return False
    nodata = dataset.metadata.nodata_value
    # NoData is an exact source sentinel; a tolerance could discard a valid
    # elevation that merely happens to be close to that sentinel.
    if nodata is not None and numeric == float(nodata):
        return False
    return True


def _normalize_strategy(value: Any) -> ResamplingStrategy:
    try:
        return _coerce_enum(value, ResamplingStrategy, "resampling_strategy")  # type: ignore[return-value]
    except ValueError as exc:
        raise TerrainMappingError(str(exc)) from exc


def _normalize_nodata_strategy(value: Any) -> NoDataStrategy:
    try:
        return _coerce_enum(value, NoDataStrategy, "nodata_strategy")  # type: ignore[return-value]
    except ValueError as exc:
        raise TerrainMappingError(str(exc)) from exc


def _validate_common(
    dataset: DEMDataset,
    domain: DomainConfig,
    *,
    coordinate_system: str | None,
    nodata_strategy: Any,
    min_valid_coverage: float | None,
) -> NoDataStrategy:
    if not isinstance(dataset, DEMDataset):
        raise TypeError("dataset must be a DEMDataset")
    if not isinstance(domain, DomainConfig):
        raise TypeError("domain must be a DomainConfig")
    _same_crs(dataset, coordinate_system)
    _validate_geometry(dataset)
    _domain_inside_dem(dataset, domain)
    normalized_nodata = _normalize_nodata_strategy(nodata_strategy)
    if normalized_nodata is not NoDataStrategy.ERROR:
        raise NotImplementedError(
            f"NoData strategy '{normalized_nodata.value}' is declared but not implemented"
        )
    if min_valid_coverage is not None:
        if isinstance(min_valid_coverage, bool) or not isinstance(min_valid_coverage, Real):
            raise TerrainMappingError("min_valid_coverage must be a number or None")
        if not isfinite(float(min_valid_coverage)) or not 0.0 <= float(min_valid_coverage) <= 1.0:
            raise TerrainMappingError("min_valid_coverage must be between 0 and 1")
    return normalized_nodata


def _aligned_same_grid(
    dataset: DEMDataset,
    domain: DomainConfig,
    affine: tuple[float, float, float, float, float, float],
) -> bool:
    a, _, c, _, e, f = affine
    metadata = dataset.metadata
    return (
        _close(abs(a), domain.dx)
        and _close(abs(e), domain.dy)
        and dataset.metadata.width == domain.nx
        and dataset.metadata.height == domain.ny
        and _close(c, domain.xmin)
        and _close(f, domain.ymax)
        and _close(metadata.xmin, domain.xmin)
        and _close(metadata.xmax, domain.xmax)
        and _close(metadata.ymin, domain.ymin)
        and _close(metadata.ymax, domain.ymax)
    )


def _check_coverage(coverage: float, minimum: float | None, *, cell: str) -> None:
    if coverage <= _ABS_TOL:
        raise TerrainMappingError(f"{cell} has no valid DEM overlap")
    if minimum is not None and coverage + _ABS_TOL < minimum:
        raise TerrainMappingError(
            f"{cell} valid coverage {coverage:.12g} is below min_valid_coverage {minimum:.12g}"
        )


def _new_field(
    domain: DomainConfig,
    elevations: list[list[float]],
    valid: list[list[bool]],
    nodata: list[list[bool]],
    coverage: list[list[float]],
) -> TerrainField:
    return TerrainField.from_domain(
        domain,
        elevations,
        valid_mask=valid,
        nodata_mask=nodata,
        coverage_ratio=coverage,
    )


def _area_weighted_mean(
    dataset: DEMDataset,
    domain: DomainConfig,
    affine: tuple[float, float, float, float, float, float],
    minimum: float | None,
) -> TerrainField:
    _, _, origin_x, _, _, origin_y = affine
    pixel_x = dataset.metadata.pixel_size_x
    pixel_y = dataset.metadata.pixel_size_y
    width = dataset.metadata.width
    height = dataset.metadata.height
    elevations: list[list[float]] = []
    valid_mask: list[list[bool]] = []
    nodata_mask: list[list[bool]] = []
    coverage_rows: list[list[float]] = []
    cell_area = domain.dx * domain.dy

    for j in range(domain.ny):
        y0 = domain.ymin + j * domain.dy
        y1 = y0 + domain.dy
        row_values: list[float] = []
        row_valid: list[bool] = []
        row_nodata: list[bool] = []
        row_coverage: list[float] = []
        col_row_start = max(0, int(floor((origin_y - y1) / pixel_y)) - 1)
        col_row_end = min(height - 1, int((origin_y - y0) / pixel_y) + 1)
        for i in range(domain.nx):
            x0 = domain.xmin + i * domain.dx
            x1 = x0 + domain.dx
            col_start = max(0, int(floor((x0 - origin_x) / pixel_x)) - 1)
            col_end = min(width - 1, int((x1 - origin_x) / pixel_x) + 1)
            weighted_sum = 0.0
            valid_area = 0.0
            for source_row in range(col_row_start, col_row_end + 1):
                source_y1 = origin_y - source_row * pixel_y
                source_y0 = source_y1 - pixel_y
                overlap_y = min(y1, source_y1) - max(y0, source_y0)
                if overlap_y <= _ABS_TOL:
                    continue
                for source_col in range(col_start, col_end + 1):
                    source_x0 = origin_x + source_col * pixel_x
                    source_x1 = source_x0 + pixel_x
                    overlap_x = min(x1, source_x1) - max(x0, source_x0)
                    if overlap_x <= _ABS_TOL or not _valid_source_value(dataset, source_row, source_col):
                        continue
                    overlap_area = overlap_x * overlap_y
                    weighted_sum += overlap_area * float(dataset.elevation[source_row, source_col])
                    valid_area += overlap_area
            ratio = valid_area / cell_area
            _check_coverage(ratio, minimum, cell=f"cell[{j}, {i}]")
            row_values.append(weighted_sum / valid_area)
            row_valid.append(True)
            row_nodata.append(False)
            row_coverage.append(min(1.0, max(0.0, ratio)))
        elevations.append(row_values)
        valid_mask.append(row_valid)
        nodata_mask.append(row_nodata)
        coverage_rows.append(row_coverage)
    return _new_field(domain, elevations, valid_mask, nodata_mask, coverage_rows)


def _direct(
    dataset: DEMDataset,
    domain: DomainConfig,
    affine: tuple[float, float, float, float, float, float],
    minimum: float | None,
) -> TerrainField:
    if not _aligned_same_grid(dataset, domain, affine):
        raise TerrainMappingError(
            "direct mapping requires equal resolution, extent, pixel boundaries, and axis alignment"
        )
    elevations: list[list[float]] = []
    valid_mask: list[list[bool]] = []
    nodata_mask: list[list[bool]] = []
    coverage_rows: list[list[float]] = []
    for j in range(domain.ny):
        source_row = dataset.metadata.height - 1 - j
        row_values: list[float] = []
        row_valid: list[bool] = []
        row_nodata: list[bool] = []
        row_coverage: list[float] = []
        for i in range(domain.nx):
            if not _valid_source_value(dataset, source_row, i):
                _check_coverage(0.0, minimum, cell=f"cell[{j}, {i}]")
            row_values.append(float(dataset.elevation[source_row, i]))
            row_valid.append(True)
            row_nodata.append(False)
            row_coverage.append(1.0)
        elevations.append(row_values)
        valid_mask.append(row_valid)
        nodata_mask.append(row_nodata)
        coverage_rows.append(row_coverage)
    return _new_field(domain, elevations, valid_mask, nodata_mask, coverage_rows)


def _bilinear(
    dataset: DEMDataset,
    domain: DomainConfig,
    affine: tuple[float, float, float, float, float, float],
    minimum: float | None,
) -> TerrainField:
    _, _, origin_x, _, _, origin_y = affine
    pixel_x = dataset.metadata.pixel_size_x
    pixel_y = dataset.metadata.pixel_size_y
    width = dataset.metadata.width
    height = dataset.metadata.height
    elevations: list[list[float]] = []
    valid_mask: list[list[bool]] = []
    nodata_mask: list[list[bool]] = []
    coverage_rows: list[list[float]] = []
    for j in range(domain.ny):
        y = domain.ymin + (j + 0.5) * domain.dy
        row_values: list[float] = []
        row_valid: list[bool] = []
        row_nodata: list[bool] = []
        row_coverage: list[float] = []
        for i in range(domain.nx):
            x = domain.xmin + (i + 0.5) * domain.dx
            source_x = (x - origin_x) / pixel_x - 0.5
            source_y = (origin_y - y) / pixel_y - 0.5
            col0 = floor(source_x)
            row0 = floor(source_y)
            col1 = col0 + 1
            row1 = row0 + 1
            if col0 < 0 or row0 < 0 or col1 >= width or row1 >= height:
                raise TerrainMappingError(
                    f"cell[{j}, {i}] bilinear neighborhood would require extrapolation"
                )
            if not all(
                _valid_source_value(dataset, source_row, source_col)
                for source_row, source_col in (
                    (row0, col0),
                    (row0, col1),
                    (row1, col0),
                    (row1, col1),
                )
            ):
                raise TerrainMappingError(
                    f"cell[{j}, {i}] bilinear neighborhood contains NoData or non-finite values"
                )
            tx = source_x - col0
            ty = source_y - row0
            z00 = float(dataset.elevation[row0, col0])
            z01 = float(dataset.elevation[row0, col1])
            z10 = float(dataset.elevation[row1, col0])
            z11 = float(dataset.elevation[row1, col1])
            value = (
                (1.0 - tx) * (1.0 - ty) * z00
                + tx * (1.0 - ty) * z01
                + (1.0 - tx) * ty * z10
                + tx * ty * z11
            )
            _check_coverage(1.0, minimum, cell=f"cell[{j}, {i}]")
            row_values.append(value)
            row_valid.append(True)
            row_nodata.append(False)
            row_coverage.append(1.0)
        elevations.append(row_values)
        valid_mask.append(row_valid)
        nodata_mask.append(row_nodata)
        coverage_rows.append(row_coverage)
    return _new_field(domain, elevations, valid_mask, nodata_mask, coverage_rows)


def map_dataset_to_field(
    dataset: DEMDataset,
    domain: DomainConfig,
    *,
    coordinate_system: str | None = None,
    strategy: ResamplingStrategy | str = ResamplingStrategy.AUTO,
    nodata_strategy: NoDataStrategy | str = NoDataStrategy.ERROR,
    min_valid_coverage: float | None = None,
) -> TerrainField:
    """Map an already-read DEM dataset to a model grid.

    No files are opened and no CRS conversion is attempted.  ``strategy`` may
    be ``auto``, ``area_weighted_mean``, ``direct``, or ``bilinear``.  The
    ``nearest`` and ``interpolate`` NoData policies remain explicitly
    unimplemented.
    """

    _validate_common(
        dataset,
        domain,
        coordinate_system=coordinate_system,
        nodata_strategy=nodata_strategy,
        min_valid_coverage=min_valid_coverage,
    )
    affine = _affine_components(dataset)
    selected = _normalize_strategy(strategy)
    # Keep this tolerance identical to resolve_auto_resampling_strategy.
    same_resolution = isclose(
        dataset.metadata.pixel_size_x, domain.dx, rel_tol=_REL_TOL, abs_tol=1e-9
    ) and isclose(dataset.metadata.pixel_size_y, domain.dy, rel_tol=_REL_TOL, abs_tol=1e-9)
    aligned = same_resolution and _aligned_same_grid(dataset, domain, affine)
    if selected is ResamplingStrategy.AUTO:
        try:
            selected = resolve_auto_resampling_strategy(
                dataset.metadata.pixel_size_x,
                dataset.metadata.pixel_size_y,
                domain.dx,
                domain.dy,
                aligned=aligned,
            )
        except ValueError as exc:
            raise TerrainMappingError(str(exc)) from exc

    finer_or_equal_x = dataset.metadata.pixel_size_x < domain.dx or _close(
        dataset.metadata.pixel_size_x, domain.dx
    )
    finer_or_equal_y = dataset.metadata.pixel_size_y < domain.dy or _close(
        dataset.metadata.pixel_size_y, domain.dy
    )
    coarser_or_equal_x = dataset.metadata.pixel_size_x > domain.dx or _close(
        dataset.metadata.pixel_size_x, domain.dx
    )
    coarser_or_equal_y = dataset.metadata.pixel_size_y > domain.dy or _close(
        dataset.metadata.pixel_size_y, domain.dy
    )

    if selected is ResamplingStrategy.AREA_WEIGHTED_MEAN:
        if not (finer_or_equal_x and finer_or_equal_y and not same_resolution):
            raise TerrainMappingError("area_weighted_mean requires a finer DEM on both axes")
        return _area_weighted_mean(dataset, domain, affine, min_valid_coverage)
    if selected is ResamplingStrategy.DIRECT:
        return _direct(dataset, domain, affine, min_valid_coverage)
    if selected is ResamplingStrategy.BILINEAR:
        if not (coarser_or_equal_x and coarser_or_equal_y and not same_resolution):
            raise TerrainMappingError("bilinear requires a coarser DEM on both axes")
        return _bilinear(dataset, domain, affine, min_valid_coverage)
    raise TerrainMappingError(f"Unsupported mapping strategy: {selected.value}")


__all__ = ["TerrainMappingError", "map_dataset_to_field"]
