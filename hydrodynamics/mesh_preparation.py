"""Independent triangular-mesh input, terrain mapping and UGRID preparation.

This module prepares mesh and terrain artifacts only. It does not construct
solver inputs or produce a simulation result.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Literal, Mapping, Sequence

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator
import yaml

from .dem_contract import DEMDataset, DEMMetadata, GeoTIFFDEMReader
from .security_exclusion import (
    SecurityExclusionError,
    SecurityExclusionEvidence,
    SecurityExclusionInput,
    assess_security_domain,
    build_security_evidence,
    check_mesh_against_security,
    domain_from_gmsh_config,
    gmsh_config_for_active_domain,
    load_domain_geojson,
    load_security_area_geometry,
    read_security_exclusion_evidence,
    revalidate_security_exclusion_evidence,
    security_evidence_sha256,
)
from .triangular_mesh import FaceTerrainField, TriangularMesh, TriangularMeshError


class MeshPreparationError(ValueError):
    """Raised for invalid mesh preparation inputs or outputs."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True, validate_assignment=True, allow_inf_nan=False)


class MeshImportConfig(_StrictModel):
    type: Literal["json"]
    file: str = Field(min_length=1, description="Path to version 1 triangular mesh JSON, relative to this config")

    @field_validator("file")
    @classmethod
    def relative_mesh_path(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("mesh.file must be relative to the YAML config")
        return value


def _point(value: Any) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("point must contain exactly two coordinates in metres")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) for item in value):
        raise ValueError("point coordinates must be finite numbers in metres")
    return float(value[0]), float(value[1])


Point = Annotated[tuple[float, float], BeforeValidator(_point)]


class GmshLineGroup(_StrictModel):
    name: str = Field(min_length=1)
    points: list[Point] = Field(min_length=2)
    closed: StrictBool = Field(default=False, description="Whether this path includes a closing segment")


class GmshRefinementPoint(_StrictModel):
    point: Point
    size_m: float = Field(gt=0, le=10000, description="Target element size near this point, m")
    radius_m: float = Field(gt=0, le=100000, description="Distance over which target size returns to default, m")


class GmshMeshConfig(_StrictModel):
    type: Literal["gmsh"]
    outer_boundary: list[Point] = Field(min_length=3)
    holes: list[list[Point]] = Field(default_factory=list)
    boundary_groups: list[GmshLineGroup] = Field(default_factory=list)
    constraints: list[GmshLineGroup] = Field(default_factory=list)
    default_size_m: float = Field(gt=0, le=10000, description="Default target edge size, m")
    minimum_size_m: float = Field(default=1.0, gt=0, le=10000, description="Smallest permitted target edge size, m")
    refinement_points: list[GmshRefinementPoint] = Field(default_factory=list)
    maximum_nodes: StrictInt = Field(default=2_000_000, ge=3, le=10_000_000, description="Maximum generated node count, count")

    @model_validator(mode="after")
    def validate_sizes_and_names(self) -> "GmshMeshConfig":
        if self.minimum_size_m > self.default_size_m:
            raise ValueError("minimum_size_m must be less than or equal to default_size_m (m)")
        names = [group.name for group in [*self.boundary_groups, *self.constraints]]
        if len(names) != len(set(names)):
            raise ValueError("boundary and constraint group names must be unique")
        return self


MeshSourceConfig = MeshImportConfig | GmshMeshConfig


class TerrainSourceConfig(_StrictModel):
    type: Literal["json", "geotiff"]
    file: str = Field(min_length=1, description="DEM source path, relative to this config")
    minimum_valid_coverage: float = Field(default=0.0, ge=0.0, le=1.0, description="Minimum valid overlap fraction per face, unitless")
    vertical_unit: Literal["m"] | None = Field(default=None, description="Explicit source-unit declaration when DEM metadata has no vertical unit; no conversion is performed")
    vertical_datum: str | None = Field(default=None, description="Source-attested vertical datum declaration; supplements missing metadata and must match present metadata")

    @field_validator("file")
    @classmethod
    def relative_dem_path(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("terrain.file must be relative to the YAML config")
        return value

    @field_validator("vertical_datum")
    @classmethod
    def normalize_vertical_datum(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("vertical_datum must be a non-empty source datum declaration or null")
        return value.strip()

    @field_validator("minimum_valid_coverage")
    @classmethod
    def finite_coverage(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("minimum_valid_coverage must be finite and between 0 and 1")
        return value


class MeshPreparationConfig(_StrictModel):
    version: StrictInt = Field(default=1, ge=1, le=1, description="Configuration format version")
    coordinate_system: str = Field(min_length=1, description="Projected CRS identifier; coordinates use metres")
    mesh: MeshSourceConfig = Field(discriminator="type")
    terrain: TerrainSourceConfig
    security_exclusion: SecurityExclusionInput | None = Field(default=None, description="Optional explicit excluded-area closed-wall constraint; null keeps generic P2 preparation unchanged")
    output_file: str = Field(min_length=1, description="UGRID NetCDF preparation artifact path")
    minimum_face_quality: float = Field(default=0.0, ge=0.0, le=1.0, description="Minimum accepted normalized triangle quality, unitless")
    maximum_faces: StrictInt = Field(default=500_000, ge=1, le=10_000_000, description="Maximum imported/generated face count")
    maximum_dem_pixels: StrictInt = Field(default=1_000_000, ge=1, le=100_000_000, description="Maximum DEM pixel count")
    maximum_overlap_checks: StrictInt = Field(default=20_000_000, ge=1, le=1_000_000_000, description="Maximum face-by-pixel overlap checks")
    geometry_tolerance_m: float = Field(default=1e-8, gt=0.0, le=1.0, description="Coordinate/segment tolerance, m")
    minimum_face_area_m2: float = Field(default=1e-12, gt=0.0, le=100.0, description="Minimum accepted triangle area, m2")

    @field_validator("output_file")
    @classmethod
    def relative_output_path(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("output_file must be relative to the YAML config")
        return value

    @field_validator("coordinate_system")
    @classmethod
    def normalize_crs(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("coordinate_system must be a non-empty projected CRS identifier")
        return value.strip()

    @field_validator("minimum_face_quality")
    @classmethod
    def finite_quality(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("minimum_face_quality must be finite and between 0 and 1")
        return value

    @field_validator("geometry_tolerance_m", "minimum_face_area_m2")
    @classmethod
    def finite_geometry_limits(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite and positive")
        return value

    @model_validator(mode="after")
    def validate_geometry_scale(self) -> "MeshPreparationConfig":
        if self.security_exclusion is not None and self.security_exclusion.enabled:
            if isinstance(self.mesh, MeshImportConfig) and self.security_exclusion.domain_geometry_file is None:
                raise ValueError("an imported mesh with security exclusion requires original-domain geometry D and its explicit CRS")
            if isinstance(self.mesh, GmshMeshConfig) and self.security_exclusion.domain_geometry_file is not None:
                raise ValueError("Gmsh security exclusion derives D from mesh.outer_boundary/holes; domain_geometry_file is reserved for imported meshes")
        if isinstance(self.mesh, GmshMeshConfig):
            if self.mesh.minimum_size_m <= self.geometry_tolerance_m:
                raise ValueError("mesh.minimum_size_m must be greater than geometry_tolerance_m")
            if any(not self.mesh.minimum_size_m <= point.size_m <= self.mesh.default_size_m for point in self.mesh.refinement_points):
                raise ValueError("each mesh.refinement_points[].size_m must be between mesh.minimum_size_m and mesh.default_size_m")
        return self


@dataclass(frozen=True)
class MeshPreparationResult:
    mesh: TriangularMesh
    terrain: FaceTerrainField
    artifact_path: Path
    quality_report: Mapping[str, Any]
    preparation_only: bool = True
    security_exclusion: SecurityExclusionEvidence | None = None


def load_mesh_preparation_config(path: str | Path) -> MeshPreparationConfig:
    """Load a strict YAML mesh preparation configuration."""
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MeshPreparationError(f"Mesh preparation config not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise MeshPreparationError(f"Invalid YAML in mesh preparation config '{config_path}': {exc}") from exc
    if not isinstance(raw, dict):
        raise MeshPreparationError("mesh preparation config must be a YAML mapping")
    try:
        config = MeshPreparationConfig.model_validate(raw)
    except ValidationError as exc:
        detail = "\n".join(f"- {'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}" for issue in exc.errors())
        raise MeshPreparationError(f"Invalid mesh preparation config:\n{detail}") from exc
    _validate_projected_metric_crs(config.coordinate_system)
    return config


def _validate_projected_metric_crs(crs: str) -> None:
    try:
        from pyproj import CRS
    except ImportError as exc:  # pragma: no cover - optional dependency environment
        raise MeshPreparationError("CRS verification requires optional dependency 'pyproj'; install the triangular extra (pip install '.[triangular]')") from exc
    try:
        parsed = CRS.from_user_input(crs)
    except Exception as exc:
        raise MeshPreparationError(f"coordinate_system '{crs}' is not a recognized CRS") from exc
    if not parsed.is_projected:
        raise MeshPreparationError(f"coordinate_system '{crs}' must be projected; geographic coordinates are not accepted")
    axes = parsed.axis_info
    if len(axes) < 2 or any(axis.unit_conversion_factor is None or not math.isclose(axis.unit_conversion_factor, 1.0, rel_tol=0.0, abs_tol=1e-12) for axis in axes[:2]):
        raise MeshPreparationError(f"coordinate_system '{crs}' horizontal axes must use metres")


def _same_crs(left: str, right: str) -> bool:
    try:
        from pyproj import CRS
        return bool(CRS.from_user_input(left).equals(CRS.from_user_input(right), ignore_axis_order=True))
    except ImportError as exc:  # pragma: no cover - configuration validation requires pyproj
        raise MeshPreparationError("CRS comparison requires optional dependency 'pyproj'; install the triangular extra") from exc
    except Exception:
        return left.strip().casefold() == right.strip().casefold()


def load_mesh_json(
    path: str | Path,
    *,
    coordinate_system: str,
    geometry_tolerance_m: float = 1e-8,
    minimum_face_area_m2: float = 1e-12,
) -> TriangularMesh:
    """Read the explicit v1 mesh JSON format; input edges refer to node pairs."""
    _validate_projected_metric_crs(coordinate_system)
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MeshPreparationError(f"Mesh JSON not found: {source}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MeshPreparationError(f"Invalid mesh JSON '{source}': {exc}") from exc
    if not isinstance(document, dict):
        raise MeshPreparationError("mesh JSON root must be an object")
    allowed = {"format", "mesh_id", "coordinate_system", "node_xy", "face_nodes", "face_id", "boundary_groups", "constraint_groups"}
    unknown = set(document) - allowed
    missing = {"format", "node_xy", "face_nodes"} - set(document)
    if missing:
        raise MeshPreparationError(f"mesh JSON is missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise MeshPreparationError(f"mesh JSON contains unsupported fields: {', '.join(sorted(unknown))}")
    if document["format"] != "triangular-mesh-json-v1":
        raise MeshPreparationError("mesh JSON format must be 'triangular-mesh-json-v1'")
    file_crs = document.get("coordinate_system", coordinate_system)
    if not isinstance(file_crs, str) or not file_crs.strip():
        raise MeshPreparationError("mesh JSON coordinate_system must be a non-empty CRS string")
    if not _same_crs(file_crs, coordinate_system):
        raise MeshPreparationError(f"mesh JSON CRS '{file_crs}' does not match config coordinate_system '{coordinate_system}'")
    try:
        return TriangularMesh(
            node_xy=document["node_xy"], face_nodes=document["face_nodes"],
            coordinate_system=coordinate_system, mesh_id=document.get("mesh_id"),
            face_id=document.get("face_id"), boundary_groups=document.get("boundary_groups"),
            constraint_groups=document.get("constraint_groups"), geometry_tolerance_m=geometry_tolerance_m,
            minimum_face_area_m2=minimum_face_area_m2,
        )
    except (TriangularMeshError, TypeError) as exc:
        raise MeshPreparationError(f"Invalid mesh topology in '{source}': {exc}") from exc


def read_dem_json(path: str | Path) -> DEMDataset:
    """Read tiny synthetic DEM JSON: values and masks only, with no resampling."""
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MeshPreparationError(f"Synthetic DEM JSON not found: {source}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MeshPreparationError(f"Invalid synthetic DEM JSON '{source}': {exc}") from exc
    allowed = {"format", "coordinate_system", "transform", "elevation", "valid_mask", "nodata_value", "vertical_unit", "vertical_datum"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise MeshPreparationError(f"DEM JSON must be an object containing only: {', '.join(sorted(allowed))}")
    if data.get("format") != "synthetic-dem-json-v1":
        raise MeshPreparationError("DEM JSON format must be 'synthetic-dem-json-v1'")
    try:
        raw_rows = data["elevation"]
        if not isinstance(raw_rows, list) or any(not isinstance(row, list) for row in raw_rows):
            raise ValueError("elevation must be a 2-D numeric array")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for row in raw_rows for value in row):
            raise ValueError("elevation values must be numbers; booleans and strings are invalid")
        rows = tuple(tuple(float(value) for value in row) for row in raw_rows)
        height, width = len(rows), len(rows[0])
        if not width or any(len(row) != width for row in rows):
            raise ValueError("elevation rows must have one non-zero common width")
        raw_transform = data["transform"]
        if not isinstance(raw_transform, list) or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_transform):
            raise ValueError("transform must be a six-number affine array [a,b,c,d,e,f]")
        transform = tuple(float(value) for value in raw_transform)
        if len(transform) != 6 or not all(math.isfinite(value) for value in transform):
            raise ValueError("transform must be six finite affine coefficients [a,b,c,d,e,f]")
        a, b, c, d, e, f = transform
        if abs(a * e - b * d) <= 1e-15:
            raise ValueError("transform must define non-zero pixel area")
        corners = ((c, f), (a * width + c, d * width + f), (a * width + b * height + c, d * width + e * height + f), (b * height + c, e * height + f))
        xs, ys = [point[0] for point in corners], [point[1] for point in corners]
        crs = data.get("coordinate_system")
        if not isinstance(crs, str) or not crs.strip():
            raise ValueError("coordinate_system is required")
        nodata = data.get("nodata_value")
        if isinstance(nodata, bool) or (nodata is not None and not isinstance(nodata, (int, float))):
            raise ValueError("nodata_value must be a number or null")
        nodata = float(nodata) if nodata is not None else None
        supplied_mask = data.get("valid_mask")
        if supplied_mask is not None and (len(supplied_mask) != height or any(len(row) != width for row in supplied_mask)):
            raise ValueError("valid_mask dimensions must match elevation")
        masks_valid: list[tuple[bool, ...]] = []
        masks_nodata: list[tuple[bool, ...]] = []
        masks_nonfinite: list[tuple[bool, ...]] = []
        for j, row in enumerate(rows):
            valid_row, nodata_row, nonfinite_row = [], [], []
            for i, value in enumerate(row):
                nonfinite = not math.isfinite(value)
                is_nodata = not nonfinite and nodata is not None and value == nodata
                given = bool(supplied_mask[j][i]) if supplied_mask is not None else True
                if supplied_mask is not None and not isinstance(supplied_mask[j][i], bool):
                    raise ValueError("valid_mask values must be booleans")
                valid_row.append(given and not nonfinite and not is_nodata)
                nodata_row.append(not given or is_nodata)
                nonfinite_row.append(nonfinite)
            masks_valid.append(tuple(valid_row))
            masks_nodata.append(tuple(nodata_row))
            masks_nonfinite.append(tuple(nonfinite_row))
        metadata = DEMMetadata(
            width=width, height=height, band_count=1, dtype="float64",
            xmin=min(xs), xmax=max(xs), ymin=min(ys), ymax=max(ys),
            pixel_size_x=math.hypot(a, d), pixel_size_y=math.hypot(b, e),
            transform=transform, crs=crs.strip(), horizontal_unit="metre",
            nodata_value=nodata, vertical_unit=data.get("vertical_unit"),
            vertical_datum=data.get("vertical_datum"),
        )
        return DEMDataset(metadata, rows, tuple(masks_valid), tuple(masks_nodata), tuple(masks_nonfinite))
    except (KeyError, TypeError, ValueError) as exc:
        raise MeshPreparationError(f"Invalid synthetic DEM JSON '{source}': {exc}") from exc


def _pixel_polygon(transform: Sequence[float], row: int, column: int) -> tuple[tuple[float, float], ...]:
    a, b, c, d, e, f = transform[:6]
    return tuple((a * col + b * row_ + c, d * col + e * row_ + f) for col, row_ in ((column, row), (column + 1, row), (column + 1, row + 1), (column, row + 1)))


def _signed_polygon_area(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    origin = points[0]
    return 0.5 * sum(
        (points[i][0] - origin[0]) * (points[i + 1][1] - origin[1])
        - (points[i + 1][0] - origin[0]) * (points[i][1] - origin[1])
        for i in range(1, len(points) - 1)
    )


def _clip_convex(
    subject: Sequence[tuple[float, float]],
    clip: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    polygon = list(subject)
    clip_points = tuple(clip if _signed_polygon_area(clip) >= 0 else reversed(clip))
    for edge_index, start in enumerate(clip_points):
        end = clip_points[(edge_index + 1) % len(clip_points)]
        if not polygon:
            break
        output: list[tuple[float, float]] = []
        coordinate_scale = max(1.0, *(abs(value) for point in (*subject, *clip) for value in point))
        coordinate_epsilon = max(1e-12, math.ulp(coordinate_scale) * 4.0)
        cross_tolerance = coordinate_epsilon * math.hypot(end[0] - start[0], end[1] - start[1])
        previous = polygon[-1]
        prev_inside = _cross2(start, end, previous) >= -cross_tolerance
        for current in polygon:
            current_inside = _cross2(start, end, current) >= -cross_tolerance
            if current_inside != prev_inside:
                dx1, dy1 = current[0] - previous[0], current[1] - previous[1]
                dx2, dy2 = end[0] - start[0], end[1] - start[1]
                denominator = dx1 * dy2 - dy1 * dx2
                if abs(denominator) > cross_tolerance:
                    t = ((start[0] - previous[0]) * dy2 - (start[1] - previous[1]) * dx2) / denominator
                    output.append((previous[0] + t * dx1, previous[1] + t * dy1))
            if current_inside:
                output.append(current)
            previous, prev_inside = current, current_inside
        polygon = output
    return tuple(polygon)


def _cross2(a: tuple[float, float], b: tuple[float, float], point: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])


_METRE_UNITS = {"m", "meter", "meters", "metre", "metres"}


def _resolve_vertical_provenance(
    dataset: DEMDataset,
    *,
    declared_unit: str | None,
    declared_datum: str | None,
) -> tuple[str, str, str | None, str | None, str | None, str | None]:
    source_unit = dataset.metadata.vertical_unit
    if source_unit is not None:
        if source_unit.strip().casefold() not in _METRE_UNITS:
            raise MeshPreparationError(
                f"DEM vertical_unit '{source_unit}' is unsupported; this mapper does not convert elevations to metres"
            )
        if declared_unit is not None and declared_unit != "m":
            raise MeshPreparationError("terrain.vertical_unit declaration must be 'm'; unit conversion is not implemented")
        unit_source = "dem_metadata"
    else:
        if declared_unit != "m":
            raise MeshPreparationError(
                "DEM vertical_unit is missing; provide terrain.vertical_unit: m only when the source data is known to be metres"
            )
        unit_source = "config_declaration"

    source_datum = dataset.metadata.vertical_datum
    if source_datum is not None and declared_datum is not None and source_datum.strip() != declared_datum.strip():
        raise MeshPreparationError(
            f"terrain.vertical_datum '{declared_datum}' conflicts with DEM metadata vertical_datum '{source_datum}'; datum conversion is not implemented"
        )
    if source_datum is not None:
        resolved_datum, datum_source = source_datum, "dem_metadata"
    elif declared_datum is not None:
        resolved_datum, datum_source = declared_datum.strip(), "config_declaration"
    else:
        resolved_datum, datum_source = None, None
    return "m", unit_source, resolved_datum, datum_source, source_unit, source_datum


def map_dem_to_triangular_mesh(
    dataset: DEMDataset,
    mesh: TriangularMesh,
    *,
    minimum_valid_coverage: float = 0.0,
    vertical_unit: str | None = None,
    vertical_datum: str | None = None,
    maximum_overlap_checks: int = 20_000_000,
) -> FaceTerrainField:
    """Map an already-read DEM by exact polygon overlap area, excluding NoData."""
    _validate_projected_metric_crs(mesh.coordinate_system)
    if dataset.metadata.crs is None:
        raise MeshPreparationError("DEM CRS is required for triangular terrain mapping")
    if not _same_crs(dataset.metadata.crs, mesh.coordinate_system):
        raise MeshPreparationError(f"DEM CRS '{dataset.metadata.crs}' does not match mesh CRS '{mesh.coordinate_system}'; reprojection is not performed")
    resolved_unit, unit_source, resolved_datum, datum_source, source_unit, source_datum = _resolve_vertical_provenance(
        dataset, declared_unit=vertical_unit, declared_datum=vertical_datum
    )
    if isinstance(minimum_valid_coverage, bool) or not isinstance(minimum_valid_coverage, (int, float)) or not math.isfinite(minimum_valid_coverage) or not 0.0 <= minimum_valid_coverage <= 1.0:
        raise MeshPreparationError("minimum_valid_coverage must be between 0 and 1")
    if isinstance(maximum_overlap_checks, bool) or not isinstance(maximum_overlap_checks, int) or not 1 <= maximum_overlap_checks <= 1_000_000_000:
        raise MeshPreparationError("maximum_overlap_checks must be an integer between 1 and 1000000000")
    overlap_checks = mesh.n_face * dataset.metadata.pixel_count
    if overlap_checks > maximum_overlap_checks:
        raise MeshPreparationError(f"face-by-pixel overlap checks ({overlap_checks}) exceed maximum_overlap_checks={maximum_overlap_checks}")
    transform = dataset.metadata.transform
    if len(transform) == 9:
        transform = transform[:6]
    determinant = transform[0] * transform[4] - transform[1] * transform[3]
    pixel_width = math.hypot(transform[0], transform[3])
    pixel_height = math.hypot(transform[1], transform[4])
    if min(pixel_width, pixel_height) <= mesh.geometry_tolerance_m or abs(determinant) <= mesh.geometry_tolerance_m**2:
        raise MeshPreparationError("DEM affine pixel size/area must be greater than the mesh geometry tolerance")
    nodata_value = dataset.metadata.nodata_value
    for row in range(dataset.metadata.height):
        for column in range(dataset.metadata.width):
            value = float(dataset.elevation[row, column])
            if dataset.valid_mask[row, column] and (
                not math.isfinite(value)
                or dataset.nodata_mask[row, column]
                or dataset.nonfinite_mask[row, column]
                or (nodata_value is not None and value == float(nodata_value))
            ):
                raise MeshPreparationError(f"DEMDataset valid_mask contradicts the source value or invalid mask at row {row}, column {column}")
    elevations: list[float | None] = []
    masks: list[bool] = []
    covered_areas: list[float] = []
    valid_areas: list[float] = []
    coverage: list[float] = []
    for face_index, face in enumerate(mesh.face_nodes):
        triangle = tuple(mesh.node_xy[node] for node in face)
        weighted_sum = valid_area = covered_area = 0.0
        for row in range(dataset.metadata.height):
            for column in range(dataset.metadata.width):
                pixel = _pixel_polygon(transform, row, column)
                clipped = _clip_convex(triangle, pixel)
                overlap = abs(_signed_polygon_area(clipped)) if len(clipped) >= 3 else 0.0
                if overlap <= max(mesh.geometry_tolerance_m**2, mesh.face_area[face_index] * 1e-14):
                    continue
                covered_area += overlap
                if dataset.valid_mask[row, column]:
                    valid_area += overlap
                    weighted_sum += overlap * float(dataset.elevation[row, column])
        area = mesh.face_area[face_index]
        ratio = min(1.0, valid_area / area)
        if valid_area <= area * 1e-14:
            raise MeshPreparationError(f"face {mesh.face_id[face_index]} has zero valid DEM overlap area")
        if ratio + 1e-9 < minimum_valid_coverage:
            raise MeshPreparationError(f"face {mesh.face_id[face_index]} valid DEM coverage {ratio:.6f} is below minimum {minimum_valid_coverage:.6f}")
        elevations.append(weighted_sum / valid_area)
        masks.append(True)
        covered_areas.append(min(area, covered_area))
        valid_areas.append(min(area, valid_area))
        coverage.append(ratio)
    field = FaceTerrainField(
        mesh.mesh_id or "", tuple(elevations), tuple(masks), tuple(covered_areas),
        tuple(valid_areas), tuple(coverage), mesh.coordinate_system, resolved_datum,
        resolved_unit, unit_source, datum_source, source_unit, source_datum,
    )
    try:
        return field.validate_for(mesh)
    except TriangularMeshError as exc:
        raise MeshPreparationError(str(exc)) from exc


def _load_terrain(
    config: TerrainSourceConfig,
    base_dir: Path,
    *,
    maximum_pixels: int,
    face_count: int,
    maximum_overlap_checks: int,
) -> DEMDataset:
    source = (base_dir / config.file).resolve()
    if config.type == "json":
        dataset = read_dem_json(source)
    else:
        reader = GeoTIFFDEMReader()
        metadata = reader.read_metadata(source)
        if metadata.pixel_count > maximum_pixels:
            raise MeshPreparationError(f"DEM pixel count {metadata.pixel_count} exceeds maximum_dem_pixels={maximum_pixels}")
        if face_count * metadata.pixel_count > maximum_overlap_checks:
            raise MeshPreparationError(f"face-by-pixel overlap checks ({face_count * metadata.pixel_count}) exceed maximum_overlap_checks={maximum_overlap_checks}")
        dataset = reader.read_dataset(source)
    if dataset.metadata.pixel_count > maximum_pixels:
        raise MeshPreparationError(f"DEM pixel count {dataset.metadata.pixel_count} exceeds maximum_dem_pixels={maximum_pixels}")
    if face_count * dataset.metadata.pixel_count > maximum_overlap_checks:
        raise MeshPreparationError(f"face-by-pixel overlap checks ({face_count * dataset.metadata.pixel_count}) exceed maximum_overlap_checks={maximum_overlap_checks}")
    return dataset


def _load_mesh(config: MeshPreparationConfig, base_dir: Path, *, mesh_override: GmshMeshConfig | None = None) -> TriangularMesh:
    if isinstance(config.mesh, MeshImportConfig):
        return load_mesh_json(
            (base_dir / config.mesh.file).resolve(), coordinate_system=config.coordinate_system,
            geometry_tolerance_m=config.geometry_tolerance_m,
            minimum_face_area_m2=config.minimum_face_area_m2,
        )
    from .mesh_generation import generate_gmsh_mesh
    source = mesh_override if mesh_override is not None else config.mesh
    return generate_gmsh_mesh(
        source, coordinate_system=config.coordinate_system,
        geometry_tolerance_m=config.geometry_tolerance_m,
        minimum_face_area_m2=config.minimum_face_area_m2,
    )


def prepare_mesh(config_path: str | Path) -> MeshPreparationResult:
    """Run the standalone P0-P2 mesh and terrain preparation workflow."""
    source = Path(config_path).resolve()
    config = load_mesh_preparation_config(source)
    security_evidence = None
    security_assessment = None
    security = config.security_exclusion
    mesh_override = None
    domain_source_path = source
    domain_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    domain_source_kind = "P2_GMSH_CONFIG"
    domain_source_crs = config.coordinate_system
    if security is not None and security.enabled:
        geometry = load_security_area_geometry(security, source, config.coordinate_system)
        if isinstance(config.mesh, GmshMeshConfig):
            domain = domain_from_gmsh_config(config.mesh)
            security_assessment = assess_security_domain(domain, geometry)
            mesh_override = gmsh_config_for_active_domain(
                config.mesh, security_assessment, tolerance_m=config.geometry_tolerance_m,
            )
        else:
            domain_path, domain_crs = config.security_exclusion.domain_geometry_file, config.security_exclusion.domain_crs
            if domain_path is None or domain_crs is None:
                raise MeshPreparationError("an imported mesh with security exclusion requires original-domain GeoJSON D and explicit domain_crs")
            domain, domain_source_path, domain_source_sha256 = load_domain_geojson(
                (source.parent / domain_path).resolve(), domain_crs, config.coordinate_system,
            )
            domain_source_kind = "DOMAIN_GEOJSON"
            domain_source_crs = domain_crs
            security_assessment = assess_security_domain(domain, geometry)
    elif config.security_exclusion is not None and not config.security_exclusion.enabled:
        security = None
    mesh = _load_mesh(config, source.parent, mesh_override=mesh_override)
    if security is not None and security.enabled:
        try:
            checked = check_mesh_against_security(
                mesh, security_assessment, tolerance_m=config.geometry_tolerance_m,
            )
        except SecurityExclusionError as exc:
            raise MeshPreparationError(str(exc)) from exc
        mesh = checked.mesh
        try:
            security_evidence = build_security_evidence(
                load_security_area_geometry(security, source, config.coordinate_system), security_assessment, checked,
                domain_source_kind=domain_source_kind, domain_source_path=domain_source_path,
                domain_source_sha256=domain_source_sha256, domain_source_crs=domain_source_crs,
            )
        except SecurityExclusionError as exc:
            raise MeshPreparationError(str(exc)) from exc
    if mesh.n_face > config.maximum_faces:
        raise MeshPreparationError(f"mesh face count {mesh.n_face} exceeds maximum_faces={config.maximum_faces}")
    report = mesh.quality_report(minimum_quality=config.minimum_face_quality)
    bad = report["faces_below_quality_threshold"]
    if bad:
        raise MeshPreparationError(f"{len(bad)} mesh faces are below minimum_face_quality {config.minimum_face_quality:g}; first face index is {bad[0]}")
    dem = _load_terrain(
        config.terrain, source.parent, maximum_pixels=config.maximum_dem_pixels,
        face_count=mesh.n_face, maximum_overlap_checks=config.maximum_overlap_checks,
    )
    terrain = map_dem_to_triangular_mesh(
        dem, mesh,
        minimum_valid_coverage=config.terrain.minimum_valid_coverage,
        vertical_unit=config.terrain.vertical_unit,
        vertical_datum=config.terrain.vertical_datum,
        maximum_overlap_checks=config.maximum_overlap_checks,
    )
    output = (source.parent / config.output_file).resolve()
    protected_inputs = {source}
    if isinstance(config.mesh, MeshImportConfig):
        protected_inputs.add((source.parent / config.mesh.file).resolve())
    protected_inputs.add((source.parent / config.terrain.file).resolve())
    if config.security_exclusion is not None and config.security_exclusion.enabled:
        security_source = (source.parent / config.security_exclusion.source_file).resolve()
        protected_inputs.add(security_source)
        if security_source.suffix.lower() == ".shp":
            protected_inputs.update(
                security_source.with_suffix(suffix).resolve()
                for suffix in (".shx", ".prj", ".dbf")
                if security_source.with_suffix(suffix).exists()
            )
        if config.security_exclusion.domain_geometry_file is not None:
            protected_inputs.add((source.parent / config.security_exclusion.domain_geometry_file).resolve())
    if output in protected_inputs:
        raise MeshPreparationError("output_file must not refer to the configuration, mesh input, or DEM input file")
    write_ugrid_preparation(output, mesh, terrain, minimum_quality_threshold=config.minimum_face_quality,
                            security_exclusion=security_evidence)
    return MeshPreparationResult(
        mesh, terrain, output, report,
        preparation_only=True,
        security_exclusion=security_evidence,
    )


def write_ugrid_preparation(
    path: str | Path,
    mesh: TriangularMesh,
    terrain: FaceTerrainField,
    *,
    minimum_quality_threshold: float = 0.0,
    security_exclusion: SecurityExclusionEvidence | None = None,
) -> Path:
    """Write a UGRID mesh and static face terrain field, tagged as preparation."""
    _validate_projected_metric_crs(mesh.coordinate_system)
    terrain.validate_for(mesh)
    if security_exclusion is not None:
        try:
            revalidate_security_exclusion_evidence(security_exclusion, mesh)
        except SecurityExclusionError as exc:
            raise MeshPreparationError(f"security exclusion does not match the P2 mesh before write: {exc}") from exc
    if isinstance(minimum_quality_threshold, bool) or not isinstance(minimum_quality_threshold, (int, float)) or not math.isfinite(minimum_quality_threshold) or not 0.0 <= minimum_quality_threshold <= 1.0:
        raise MeshPreparationError("minimum_quality_threshold must be between 0 and 1")
    try:
        import netCDF4
        import numpy as np
    except ImportError as exc:  # pragma: no cover - optional dependency environment
        raise MeshPreparationError("UGRID NetCDF output requires optional dependency 'netCDF4'; install the triangular extra (pip install '.[triangular]')") from exc
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    edge_boundary, edge_constraint = {}, {}
    for name, indices in mesh.boundary_groups.items():
        edge_boundary[name] = [mesh.edge_nodes[index] for index in indices]
    for name, indices in mesh.constraint_groups.items():
        edge_constraint[name] = [mesh.edge_nodes[index] for index in indices]
    try:
        with netCDF4.Dataset(target, "w", format="NETCDF4") as root:
            root.Conventions = "CF-1.10 UGRID-1.0"
            root.title = "Triangular mesh preparation artifact"
            root.product_type = "mesh_and_terrain_preparation"
            root.preparation_only = 1
            root.simulation_result = 0
            root.mesh_id = mesh.mesh_id or ""
            root.coordinate_system = mesh.coordinate_system
            root.mesh_geometry_tolerance_m = mesh.geometry_tolerance_m
            root.minimum_face_area_threshold_m2 = mesh.minimum_face_area_m2
            root.measured_minimum_face_area_m2 = min(mesh.face_area)
            root.minimum_quality_threshold = minimum_quality_threshold
            root.vertical_unit = terrain.vertical_unit
            root.vertical_unit_source = terrain.vertical_unit_source
            root.source_vertical_unit = terrain.source_vertical_unit or ""
            root.vertical_datum = terrain.vertical_datum or ""
            root.vertical_datum_source = terrain.vertical_datum_source or ""
            root.source_vertical_datum = terrain.source_vertical_datum or ""
            if security_exclusion is not None:
                root.security_exclusion_json = json.dumps(
                    security_exclusion.model_dump(mode="json"), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"), allow_nan=False,
                )
                root.security_exclusion_sha256 = security_evidence_sha256(security_exclusion)
            root.boundary_groups_json = json.dumps(edge_boundary, separators=(",", ":"), sort_keys=True)
            root.constraint_groups_json = json.dumps(edge_constraint, separators=(",", ":"), sort_keys=True)
            root.createDimension("node", mesh.n_node)
            root.createDimension("face", mesh.n_face)
            root.createDimension("edge", mesh.n_edge)
            root.createDimension("max_face_nodes", 3)
            root.createDimension("two", 2)
            topology = root.createVariable("mesh2d", "i4")
            topology.assignValue(0)
            topology.cf_role = "mesh_topology"
            topology.topology_dimension = 2
            topology.node_coordinates = "node_x node_y"
            topology.face_coordinates = "face_x face_y"
            topology.face_node_connectivity = "face_nodes"
            topology.edge_node_connectivity = "edge_nodes"
            topology.face_edge_connectivity = "face_edges"
            topology.edge_face_connectivity = "edge_faces"
            topology.face_dimension = "face"
            topology.edge_dimension = "edge"
            x = root.createVariable("node_x", "f8", ("node",))
            y = root.createVariable("node_y", "f8", ("node",))
            x[:] = [point[0] for point in mesh.node_xy]
            y[:] = [point[1] for point in mesh.node_xy]
            x.standard_name, x.units, x.axis = "projection_x_coordinate", "m", "X"
            y.standard_name, y.units, y.axis = "projection_y_coordinate", "m", "Y"
            crs = root.createVariable("crs", "i4")
            crs.assignValue(0)
            from pyproj import CRS
            parsed_crs = CRS.from_user_input(mesh.coordinate_system)
            crs_wkt = parsed_crs.to_wkt(version="WKT2_2019")
            crs.crs_wkt = crs_wkt
            crs.spatial_ref = crs_wkt
            crs.coordinate_system = mesh.coordinate_system
            for attribute, value in parsed_crs.to_cf().items():
                if attribute in {"crs_wkt", "spatial_ref"} or value is None:
                    continue
                if isinstance(value, (list, tuple)):
                    value = np.asarray(value)
                setattr(crs, attribute, value)
            authority = parsed_crs.to_authority()
            if authority:
                crs.epsg_code = f"{authority[0]}:{authority[1]}"
            x.grid_mapping = "crs"
            y.grid_mapping = "crs"
            faces = root.createVariable("face_nodes", "i8", ("face", "max_face_nodes"), fill_value=-1)
            faces[:] = np.asarray(mesh.face_nodes, dtype="int64")
            faces.start_index = 0
            faces.cf_role = "face_node_connectivity"
            edges = root.createVariable("edge_nodes", "i8", ("edge", "two"), fill_value=-1)
            edges[:] = np.asarray(mesh.edge_nodes, dtype="int64")
            edges.start_index = 0
            edges.cf_role = "edge_node_connectivity"
            edge_length = root.createVariable("edge_length", "f8", ("edge",))
            edge_length[:] = mesh.edge_length
            edge_length.units, edge_length.mesh, edge_length.location, edge_length.grid_mapping = "m", "mesh2d", "edge", "crs"
            edge_faces = root.createVariable("edge_faces", "i8", ("edge", "two"), fill_value=-1)
            edge_faces[:] = np.asarray(mesh.edge_faces, dtype="int64")
            edge_faces.start_index = 0
            edge_faces.cf_role = "edge_face_connectivity"
            face_edges = root.createVariable("face_edges", "i8", ("face", "max_face_nodes"), fill_value=-1)
            face_edges[:] = np.asarray(mesh.face_edges, dtype="int64")
            face_edges.start_index = 0
            face_edges.cf_role = "face_edge_connectivity"
            face_id = root.createVariable("face_id", "i8", ("face",))
            face_id[:] = np.asarray(mesh.face_id, dtype="int64")
            face_id.long_name = "stable external face identifier"
            area = root.createVariable("face_area", "f8", ("face",))
            area[:] = mesh.face_area
            area.units, area.mesh, area.location, area.grid_mapping = "m2", "mesh2d", "face", "crs"
            quality = root.createVariable("face_quality", "f8", ("face",))
            quality[:] = mesh.face_quality
            quality.units, quality.mesh, quality.location, quality.grid_mapping = "1", "mesh2d", "face", "crs"
            minimum_angle = root.createVariable("face_minimum_angle", "f8", ("face",))
            minimum_angle[:] = mesh.minimum_angle_deg
            minimum_angle.units, minimum_angle.mesh, minimum_angle.location, minimum_angle.grid_mapping = "degree", "mesh2d", "face", "crs"
            centroid_x = root.createVariable("face_x", "f8", ("face",))
            centroid_y = root.createVariable("face_y", "f8", ("face",))
            centroid_x[:] = [point[0] for point in mesh.face_centroid]
            centroid_y[:] = [point[1] for point in mesh.face_centroid]
            centroid_x.units, centroid_y.units = "m", "m"
            centroid_x.mesh, centroid_x.location, centroid_x.grid_mapping = "mesh2d", "face", "crs"
            centroid_y.mesh, centroid_y.location, centroid_y.grid_mapping = "mesh2d", "face", "crs"
            elevation = root.createVariable("terrain_elevation", "f8", ("face",), fill_value=np.nan)
            elevation[:] = np.asarray([np.nan if value is None else value for value in terrain.elevation_m], dtype="float64")
            elevation.units, elevation.mesh, elevation.location, elevation.grid_mapping = terrain.vertical_unit, "mesh2d", "face", "crs"
            valid = root.createVariable("terrain_valid", "i1", ("face",))
            valid[:] = np.asarray(terrain.valid_mask, dtype="int8")
            valid.flag_values, valid.flag_meanings = np.asarray((0, 1), dtype="int8"), "invalid valid"
            valid.units, valid.mesh, valid.location, valid.grid_mapping = "1", "mesh2d", "face", "crs"
            covered = root.createVariable("terrain_covered_area", "f8", ("face",))
            valid_area = root.createVariable("terrain_valid_area", "f8", ("face",))
            coverage = root.createVariable("terrain_coverage_ratio", "f8", ("face",))
            covered[:] = terrain.covered_area_m2
            valid_area[:] = terrain.valid_area_m2
            coverage[:] = terrain.coverage_ratio
            covered.units, valid_area.units, coverage.units = "m2", "m2", "1"
            for variable in (covered, valid_area, coverage):
                variable.mesh, variable.location, variable.grid_mapping = "mesh2d", "face", "crs"
    except Exception as exc:
        if isinstance(exc, MeshPreparationError):
            raise
        raise MeshPreparationError(f"Could not write UGRID preparation artifact '{target}': {exc}") from exc
    return target


def read_ugrid_security_exclusion(path: str | Path) -> SecurityExclusionEvidence | None:
    """Read and authenticate optional security evidence embedded in a P2 UGRID file."""
    try:
        return read_security_exclusion_evidence(path)
    except SecurityExclusionError as exc:
        raise MeshPreparationError(f"UGRID security-exclusion evidence is invalid: {exc}") from exc


def read_ugrid_preparation(path: str | Path) -> tuple[TriangularMesh, FaceTerrainField]:
    """Read this module's UGRID preparation artifact and validate all arrays."""
    try:
        import netCDF4
    except ImportError as exc:  # pragma: no cover
        raise MeshPreparationError("UGRID NetCDF input requires optional dependency 'netCDF4'; install the triangular extra") from exc
    source = Path(path)
    try:
        with netCDF4.Dataset(source, "r") as root:
            if getattr(root, "product_type", None) != "mesh_and_terrain_preparation" or int(getattr(root, "preparation_only", 0)) != 1 or int(getattr(root, "simulation_result", 1)) != 0:
                raise MeshPreparationError("NetCDF file is not a triangular mesh preparation artifact")
            if "time" in root.dimensions or "time" in root.variables:
                raise MeshPreparationError("mesh preparation artifacts must not contain a simulation time dimension")
            if "mesh2d" not in root.variables or getattr(root.variables["mesh2d"], "cf_role", None) != "mesh_topology":
                raise MeshPreparationError("UGRID mesh2d topology variable is missing")
            required_variables = {
                "node_x", "node_y", "face_nodes", "edge_nodes", "edge_faces", "face_edges",
                "face_id", "edge_length", "face_area", "face_quality", "face_minimum_angle",
                "face_x", "face_y", "terrain_elevation", "terrain_valid",
                "terrain_covered_area", "terrain_valid_area", "terrain_coverage_ratio", "crs",
            }
            missing_variables = required_variables - set(root.variables)
            if missing_variables:
                raise MeshPreparationError(f"UGRID preparation artifact is missing variables: {', '.join(sorted(missing_variables))}")
            expected_dimensions = {
                "node_x": ("node",), "node_y": ("node",),
                "face_nodes": ("face", "max_face_nodes"),
                "edge_nodes": ("edge", "two"), "edge_faces": ("edge", "two"),
                "face_edges": ("face", "max_face_nodes"), "face_id": ("face",),
                "edge_length": ("edge",), "face_area": ("face",),
                "face_quality": ("face",), "face_minimum_angle": ("face",),
                "face_x": ("face",), "face_y": ("face",),
                "terrain_elevation": ("face",), "terrain_valid": ("face",),
                "terrain_covered_area": ("face",), "terrain_valid_area": ("face",),
                "terrain_coverage_ratio": ("face",),
            }
            for name, dimensions in expected_dimensions.items():
                if tuple(root.variables[name].dimensions) != dimensions:
                    raise MeshPreparationError(f"UGRID {name} dimensions must be {dimensions}")
            if any(name not in root.dimensions for name in ("node", "face", "edge", "max_face_nodes", "two")):
                raise MeshPreparationError("UGRID preparation artifact is missing a required topology dimension")
            if len(root.dimensions["max_face_nodes"]) != 3 or len(root.dimensions["two"]) != 2:
                raise MeshPreparationError("UGRID face_nodes and two dimensions must have lengths 3 and 2")
            for name in ("face_nodes", "edge_nodes", "edge_faces", "face_edges", "face_id", "terrain_valid"):
                variable = root.variables[name]
                if variable.dtype.kind not in "iu":
                    raise MeshPreparationError(f"UGRID {name} must use an integer data type")
                for attribute in ("scale_factor", "add_offset"):
                    if attribute in variable.ncattrs():
                        raise MeshPreparationError(f"UGRID {name} must not use packing attribute {attribute}")
            for name in ("node_x", "node_y", "edge_length", "face_area", "face_quality", "face_minimum_angle", "face_x", "face_y", "terrain_elevation", "terrain_covered_area", "terrain_valid_area", "terrain_coverage_ratio"):
                if root.variables[name].dtype.kind not in "if":
                    raise MeshPreparationError(f"UGRID {name} must use a numeric data type")
            for name in ("face_nodes", "edge_nodes", "edge_faces", "face_edges"):
                if int(getattr(root.variables[name], "start_index", -1)) != 0:
                    raise MeshPreparationError(f"UGRID {name} start_index must be 0")
            connectivity_roles = {
                "face_nodes": "face_node_connectivity", "edge_nodes": "edge_node_connectivity",
                "edge_faces": "edge_face_connectivity", "face_edges": "face_edge_connectivity",
            }
            for name, role in connectivity_roles.items():
                if getattr(root.variables[name], "cf_role", None) != role:
                    raise MeshPreparationError(f"UGRID {name}.cf_role must be {role!r}")
            topology = root.variables["mesh2d"]
            for attribute, expected in (
                ("topology_dimension", 2), ("node_coordinates", "node_x node_y"),
                ("face_coordinates", "face_x face_y"),
                ("face_node_connectivity", "face_nodes"), ("edge_node_connectivity", "edge_nodes"),
                ("face_edge_connectivity", "face_edges"), ("edge_face_connectivity", "edge_faces"),
            ):
                if getattr(topology, attribute, None) != expected:
                    raise MeshPreparationError(f"UGRID mesh2d.{attribute} must be {expected!r}")

            def require_attributes(name: str, expected: Mapping[str, str]) -> None:
                variable = root.variables[name]
                for attribute, value in expected.items():
                    if getattr(variable, attribute, None) != value:
                        raise MeshPreparationError(f"UGRID {name}.{attribute} must be {value!r}")

            require_attributes("node_x", {"units": "m", "grid_mapping": "crs"})
            require_attributes("node_y", {"units": "m", "grid_mapping": "crs"})
            require_attributes("face_area", {"units": "m2", "mesh": "mesh2d", "location": "face", "grid_mapping": "crs"})
            require_attributes("edge_length", {"units": "m", "mesh": "mesh2d", "location": "edge", "grid_mapping": "crs"})
            require_attributes("face_quality", {"units": "1", "mesh": "mesh2d", "location": "face", "grid_mapping": "crs"})
            require_attributes("face_minimum_angle", {"units": "degree", "mesh": "mesh2d", "location": "face", "grid_mapping": "crs"})
            require_attributes("face_x", {"units": "m", "mesh": "mesh2d", "location": "face", "grid_mapping": "crs"})
            require_attributes("face_y", {"units": "m", "mesh": "mesh2d", "location": "face", "grid_mapping": "crs"})
            require_attributes("terrain_elevation", {"units": "m", "mesh": "mesh2d", "location": "face", "grid_mapping": "crs"})
            require_attributes("terrain_valid", {"units": "1", "mesh": "mesh2d", "location": "face", "grid_mapping": "crs", "flag_meanings": "invalid valid"})
            if tuple(int(value) for value in root.variables["terrain_valid"].flag_values) != (0, 1):
                raise MeshPreparationError("UGRID terrain_valid.flag_values must be (0, 1)")
            for name, units in (("terrain_covered_area", "m2"), ("terrain_valid_area", "m2"), ("terrain_coverage_ratio", "1")):
                require_attributes(name, {"units": units, "mesh": "mesh2d", "location": "face", "grid_mapping": "crs"})

            if getattr(root.variables["crs"], "grid_mapping_name", None) is None:
                raise MeshPreparationError("UGRID crs.grid_mapping_name is required by this preparation format")
            crs_wkt = getattr(root.variables["crs"], "crs_wkt", None)
            if not isinstance(crs_wkt, str) or not crs_wkt.strip():
                raise MeshPreparationError("UGRID crs.crs_wkt must contain a WKT CRS, not only an authority identifier")
            try:
                from pyproj import CRS
                parsed_wkt_crs = CRS.from_wkt(crs_wkt)
                declared_crs = CRS.from_user_input(getattr(root, "coordinate_system", ""))
            except Exception as exc:
                raise MeshPreparationError(f"UGRID crs.crs_wkt is not parseable WKT: {exc}") from exc
            if not parsed_wkt_crs.is_projected or not parsed_wkt_crs.equals(declared_crs, ignore_axis_order=True):
                raise MeshPreparationError("UGRID crs.crs_wkt must be projected and match global coordinate_system")
            cf_grid_mapping = parsed_wkt_crs.to_cf().get("grid_mapping_name")
            if cf_grid_mapping is None or root.variables["crs"].grid_mapping_name != cf_grid_mapping:
                raise MeshPreparationError("UGRID crs.grid_mapping_name is inconsistent with crs.crs_wkt")
            x, y = root.variables["node_x"][:], root.variables["node_y"][:]
            nodes = tuple((float(a), float(b)) for a, b in zip(x, y))
            faces = root.variables["face_nodes"][:]
            face_fill = getattr(root.variables["face_nodes"], "_FillValue", -1)
            if hasattr(faces, "filled"):
                faces = faces.filled(face_fill)
            if any(int(value) == int(face_fill) for row in faces for value in row):
                raise MeshPreparationError("triangular face connectivity contains fill values")
            if len(nodes) != len(root.dimensions["node"]) or len(faces) != len(root.dimensions["face"]):
                raise MeshPreparationError("UGRID topology dimensions do not match coordinate/connectivity arrays")
            mesh_id = root.mesh_id
            crs = root.coordinate_system
            _validate_projected_metric_crs(crs)
            boundary_json = json.loads(getattr(root, "boundary_groups_json", "{}"))
            constraint_json = json.loads(getattr(root, "constraint_groups_json", "{}"))
            if not hasattr(root, "minimum_face_area_threshold_m2") or not hasattr(root, "mesh_geometry_tolerance_m"):
                raise MeshPreparationError("UGRID mesh geometry tolerance and minimum-area threshold attributes are required")
            if not hasattr(root, "minimum_quality_threshold"):
                raise MeshPreparationError("UGRID minimum_quality_threshold attribute is required")
            quality_threshold = float(root.minimum_quality_threshold)
            if not math.isfinite(quality_threshold) or not 0.0 <= quality_threshold <= 1.0:
                raise MeshPreparationError("UGRID minimum_quality_threshold must be between 0 and 1")
            mesh = TriangularMesh(
                nodes, tuple(tuple(int(value) for value in row) for row in faces), crs,
                mesh_id=mesh_id, face_id=tuple(int(value) for value in root.variables["face_id"][:]),
                boundary_groups=boundary_json, constraint_groups=constraint_json,
                geometry_tolerance_m=float(root.mesh_geometry_tolerance_m),
                minimum_face_area_m2=float(root.minimum_face_area_threshold_m2),
            )
            if any(value < quality_threshold for value in mesh.face_quality):
                raise MeshPreparationError("UGRID face_quality contains faces below minimum_quality_threshold")
            if not hasattr(root, "measured_minimum_face_area_m2") or not math.isclose(float(root.measured_minimum_face_area_m2), min(mesh.face_area), rel_tol=1e-12, abs_tol=1e-12):
                raise MeshPreparationError("UGRID measured_minimum_face_area_m2 is inconsistent with face_area")
            def unmasked_rows(name: str, fill: int = -1) -> tuple[tuple[int, ...], ...]:
                values = root.variables[name][:]
                if hasattr(values, "filled"):
                    values = values.filled(fill)
                return tuple(tuple(value.item() if hasattr(value, "item") else value for value in row) for row in values)
            for variable_name, stored, derived in (
                ("edge_nodes", unmasked_rows("edge_nodes"), mesh.edge_nodes),
                ("edge_faces", unmasked_rows("edge_faces"), mesh.edge_faces),
                ("face_edges", unmasked_rows("face_edges"), mesh.face_edges),
            ):
                if stored != derived:
                    raise MeshPreparationError(f"UGRID {variable_name} is inconsistent with face_nodes topology")
            metric_checks = (
                ("edge_length", root.variables["edge_length"][:], mesh.edge_length),
                ("face_area", root.variables["face_area"][:], mesh.face_area),
                ("face_quality", root.variables["face_quality"][:], mesh.face_quality),
                ("face_minimum_angle", root.variables["face_minimum_angle"][:], mesh.minimum_angle_deg),
                ("face_x", root.variables["face_x"][:], tuple(point[0] for point in mesh.face_centroid)),
                ("face_y", root.variables["face_y"][:], tuple(point[1] for point in mesh.face_centroid)),
            )
            for variable_name, stored, derived in metric_checks:
                if len(stored) != len(derived) or any(not math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12) for left, right in zip(stored, derived)):
                    raise MeshPreparationError(f"UGRID {variable_name} is inconsistent with node coordinates and topology")
            valid_values = tuple(value.item() if hasattr(value, "item") else value for value in root.variables["terrain_valid"][:])
            if any(value not in (0, 1) for value in valid_values):
                raise MeshPreparationError("UGRID terrain_valid values must be exactly 0 or 1")
            valid = tuple(value == 1 for value in valid_values)
            raw_elev = root.variables["terrain_elevation"][:]
            raw_elev_mask = getattr(raw_elev, "mask", False)
            if hasattr(raw_elev_mask, "tolist"):
                raw_elev_mask = raw_elev_mask.tolist()
            if not isinstance(raw_elev_mask, (tuple, list)):
                raw_elev_mask = [bool(raw_elev_mask)] * len(valid)
            elevations_list: list[float | None] = []
            for index, (value, is_valid) in enumerate(zip(raw_elev, valid)):
                masked = bool(raw_elev_mask[index])
                numeric = float(value) if not masked else math.nan
                if is_valid and (masked or not math.isfinite(numeric)):
                    raise MeshPreparationError(f"UGRID terrain_elevation[{index}] must be finite when terrain_valid is 1")
                if not is_valid and not masked and math.isfinite(numeric):
                    raise MeshPreparationError(f"UGRID terrain_elevation[{index}] must be missing when terrain_valid is 0")
                elevations_list.append(numeric if is_valid else None)
            for attribute in ("vertical_unit", "vertical_unit_source", "vertical_datum", "vertical_datum_source", "source_vertical_unit", "source_vertical_datum"):
                if not hasattr(root, attribute):
                    raise MeshPreparationError(f"UGRID global attribute {attribute} is required for vertical provenance")
            vertical_unit = root.vertical_unit
            vertical_unit_source = root.vertical_unit_source
            vertical_datum = getattr(root, "vertical_datum", "") or None
            vertical_datum_source = getattr(root, "vertical_datum_source", "") or None
            source_vertical_unit = getattr(root, "source_vertical_unit", "") or None
            source_vertical_datum = getattr(root, "source_vertical_datum", "") or None
            if vertical_unit != "m":
                raise MeshPreparationError("UGRID vertical_unit metadata must be 'm'")
            terrain = FaceTerrainField(
                mesh_id, tuple(elevations_list), valid,
                tuple(float(value) for value in root.variables["terrain_covered_area"][:]),
                tuple(float(value) for value in root.variables["terrain_valid_area"][:]),
                tuple(float(value) for value in root.variables["terrain_coverage_ratio"][:]),
                crs, vertical_datum, vertical_unit, vertical_unit_source, vertical_datum_source,
                source_vertical_unit, source_vertical_datum,
            )
            terrain.validate_for(mesh)
            security_evidence = read_ugrid_security_exclusion(source)
            if security_evidence is not None:
                revalidate_security_exclusion_evidence(security_evidence, mesh)
            return mesh, terrain
    except MeshPreparationError:
        raise
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise MeshPreparationError(f"Could not read UGRID preparation artifact '{source}': {exc}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a triangular mesh and face terrain artifact (does not run a solver).")
    parser.add_argument("config", help="YAML mesh preparation config")
    args = parser.parse_args(argv)
    try:
        result = prepare_mesh(args.config)
    except (MeshPreparationError, TriangularMeshError) as exc:
        parser.error(str(exc))
    payload: dict[str, Any] = {
        "preparation_only": True, "artifact": str(result.artifact_path), "quality_report": result.quality_report,
        "security_exclusion": None if result.security_exclusion is None else {
            "enabled": True,
            "source_geometry_sha256": result.security_exclusion.source_union_geometry_sha256,
            "domain_geometry_sha256": result.security_exclusion.domain_geometry_sha256,
            "original_domain_area_m2": result.security_exclusion.original_domain_area_m2,
            "security_union_area_m2": result.security_exclusion.security_union_area_m2,
            "security_inside_area_m2": result.security_exclusion.security_inside_area_m2,
            "security_outside_area_m2": result.security_exclusion.security_outside_area_m2,
            "active_domain_area_m2": result.security_exclusion.active_domain_area_m2,
            "active_mesh_area_m2": result.security_exclusion.active_mesh_area_m2,
            "coverage_residual_m2": result.security_exclusion.coverage_residual_m2,
            "active_face_count": result.security_exclusion.mesh_face_count,
            "security_boundary_edge_count": result.security_exclusion.security_boundary_edge_count,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a module entry point
    raise SystemExit(main())


__all__ = [
    "MeshPreparationConfig", "MeshPreparationError", "MeshPreparationResult",
    "map_dem_to_triangular_mesh", "load_mesh_json", "load_mesh_preparation_config",
    "main", "prepare_mesh", "read_dem_json", "read_ugrid_preparation",
    "write_ugrid_preparation",
]
