"""Bounded native-triangle/DEM overlap mapping candidate for M2-A.

This route maps already-prepared triangles against selected raw raster windows.
It does not reproject, resample, fill NoData, invoke a solver, or publish a
production-ready terrain artifact. Engineering coverage remains diagnostic
until a source-approved coverage policy is supplied.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
import os
from pathlib import Path
from pathlib import PureWindowsPath
import re
import tempfile
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from .dem_contract import DEMDataset, DEMMetadata, DEMReaderError, DEMWindow
from .mesh_preparation import (
    MeshPreparationError,
    _clip_convex,
    _pixel_polygon,
    _signed_polygon_area,
    _validate_projected_metric_crs,
)
from .triangular_mesh import FaceTerrainField, TriangularMesh, TriangularMeshError


class WindowedTerrainMappingError(ValueError):
    """A bounded mapping failure with a small diagnostic payload when available."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def _json_integer(value: Any) -> int:
    """Match JSON Schema integer semantics while rejecting bool/string coercion."""
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError("value must be a whole JSON number; booleans, strings, and fractions are invalid")


_JSON_INTEGER = Annotated[int, BeforeValidator(_json_integer)]


class WindowedTriangleMappingConfig(BaseModel):
    """Strict independent resource and coverage-policy contract for M2-A/B."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True, validate_assignment=True, allow_inf_nan=False)

    format: Literal["triangle-terrain-windowed-v1"] = "triangle-terrain-windowed-v1"
    max_source_pixels: _JSON_INTEGER = Field(ge=1, le=1_000_000_000, description="Maximum source pixels in the declared raster, count")
    tile_rows: _JSON_INTEGER = Field(default=256, ge=1, le=256, description="Raw source window rows, pixels")
    tile_cols: _JSON_INTEGER = Field(default=256, ge=1, le=256, description="Raw source window columns, pixels")
    max_inflight_tiles: _JSON_INTEGER = Field(default=1, ge=1, le=1, description="This route holds exactly one raw tile at a time")
    gdal_cache_mib: _JSON_INTEGER = Field(default=64, ge=64, le=64, description="GDAL block cache budget, MiB")
    max_faces: _JSON_INTEGER = Field(default=500_000, ge=1, le=500_000, description="Maximum active native faces, count")
    max_nodes: _JSON_INTEGER = Field(default=2_000_000, ge=3, le=2_000_000, description="Maximum native nodes, count")
    max_candidate_overlap_checks: _JSON_INTEGER = Field(ge=1, le=1_000_000_000, description="Maximum actual candidate face/pixel overlap checks, count")
    coverage_policy: Literal["diagnostic_only", "minimum_valid_coverage"]
    minimum_valid_coverage: float | None = Field(default=None, ge=0.0, le=1.0, description="Source-approved valid area / face area threshold, unitless; required only for the strict policy")
    coverage_policy_evidence_path: str | None = Field(default=None, min_length=1, description="Relative path to source-approved coverage-policy evidence")
    coverage_policy_evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", description="SHA-256 of coverage-policy evidence")
    source_vertical_unit_declaration: Literal["m"] | None = Field(default=None, description="Explicit source-attested vertical unit when raster metadata omits it")
    source_vertical_unit_evidence_path: str | None = Field(default=None, min_length=1, description="Relative path to source-unit evidence")
    source_vertical_unit_evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", description="SHA-256 of source-unit evidence")
    source_vertical_datum_declaration: str | None = Field(default=None, min_length=1, description="Source-attested vertical datum; no datum conversion is performed")
    source_vertical_datum_evidence_path: str | None = Field(default=None, min_length=1, description="Relative path to source-datum evidence")
    source_vertical_datum_evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", description="SHA-256 of source-datum evidence")
    area_abs_tolerance_m2: float = Field(default=1e-8, gt=0.0, le=1e-3, description="Absolute area comparison tolerance, m2")
    area_rel_tolerance: float = Field(default=1e-10, gt=0.0, le=1e-6, description="Relative area comparison tolerance, unitless")
    elevation_abs_tolerance_m: float = Field(default=1e-8, gt=0.0, le=1e-3, description="Absolute elevation comparison tolerance, m")
    elevation_rel_tolerance: float = Field(default=1e-10, gt=0.0, le=1e-6, description="Relative elevation comparison tolerance, unitless")

    @model_validator(mode="after")
    def validate_budget_and_policy(self) -> "WindowedTriangleMappingConfig":
        if self.tile_rows * self.tile_cols > 65_536:
            raise ValueError("tile_rows * tile_cols must not exceed 65536 source pixels")
        if self.coverage_policy == "diagnostic_only":
            if self.minimum_valid_coverage is not None or self.coverage_policy_evidence_path is not None or self.coverage_policy_evidence_sha256 is not None:
                raise ValueError("diagnostic_only coverage must not include a threshold or approval evidence")
        elif (self.minimum_valid_coverage is None or self.coverage_policy_evidence_path is None
              or self.coverage_policy_evidence_sha256 is None):
            raise ValueError("minimum_valid_coverage requires a threshold and source-approved evidence path/SHA-256")
        for name in ("coverage_policy_evidence_path", "source_vertical_unit_evidence_path", "source_vertical_datum_evidence_path"):
            value = getattr(self, name)
            windows_path = PureWindowsPath(value) if value is not None else None
            if value is not None and (Path(value).is_absolute() or windows_path.is_absolute()
                    or windows_path.drive or windows_path.root or ".." in Path(value).parts
                    or ".." in windows_path.parts):
                raise ValueError(f"{name} must be a normalized relative path under the preparation config")
        if (self.source_vertical_unit_declaration is None) != (self.source_vertical_unit_evidence_path is None
                and self.source_vertical_unit_evidence_sha256 is None):
            raise ValueError("source_vertical_unit_declaration requires a relative evidence path and SHA-256")
        if (self.source_vertical_unit_evidence_path is None) != (self.source_vertical_unit_evidence_sha256 is None):
            raise ValueError("source vertical-unit evidence path and SHA-256 must be provided together")
        if (self.source_vertical_datum_declaration is None) != (self.source_vertical_datum_evidence_path is None
                and self.source_vertical_datum_evidence_sha256 is None):
            raise ValueError("source_vertical_datum_declaration requires a relative evidence path and SHA-256")
        if (self.source_vertical_datum_evidence_path is None) != (self.source_vertical_datum_evidence_sha256 is None):
            raise ValueError("source vertical-datum evidence path and SHA-256 must be provided together")
        return self


def _require_sha256(value: str, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise WindowedTerrainMappingError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _source_stat_identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size_bytes": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns),
            "ctime_ns": int(stat.st_ctime_ns), "device": int(stat.st_dev), "file_id": int(stat.st_ino)}


def _verify_evidence(path_value: str, digest_value: str, base_directory: str | Path,
                    name: str) -> tuple[str, str]:
    base = Path(base_directory).expanduser().resolve()
    if not base.is_dir():
        raise WindowedTerrainMappingError("config_base must be an existing directory when source evidence is referenced")
    candidate = base / path_value
    if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 4 * 1024 * 1024:
        raise WindowedTerrainMappingError(f"{name} evidence must be a regular non-symlink file no larger than 4 MiB")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        raise WindowedTerrainMappingError(f"{name} evidence path escapes the config directory")
    digest = sha256()
    with resolved.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != digest_value:
        raise WindowedTerrainMappingError(f"{name} evidence SHA-256 differs from the configured identity")
    return str(resolved), actual


def mesh_fingerprint_sha256(mesh: TriangularMesh) -> str:
    """Hash the ordered native geometry/topology inputs without altering them."""
    payload = {
        "mesh_id": mesh.mesh_id,
        "coordinate_system": mesh.coordinate_system,
        "geometry_tolerance_m": float(mesh.geometry_tolerance_m),
        "minimum_face_area_m2": float(mesh.minimum_face_area_m2),
        "node_xy": [[float(x), float(y)] for x, y in mesh.node_xy],
        "face_nodes": [[int(i) for i in face] for face in mesh.face_nodes],
        "face_id": [int(value) for value in mesh.face_id],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256(raw).hexdigest()


def _affine(metadata: DEMMetadata) -> tuple[float, float, float, float, float, float]:
    transform = tuple(float(value) for value in metadata.transform)
    if len(transform) == 9:
        if not all(math.isclose(a, b, rel_tol=0.0, abs_tol=1e-12) for a, b in zip(transform[6:], (0.0, 0.0, 1.0))):
            raise WindowedTerrainMappingError("windowed triangle mapping requires a 2-D affine transform")
        transform = transform[:6]
    a, b, c, d, e, f = transform
    if not (math.isclose(b, 0.0, rel_tol=0.0, abs_tol=1e-12)
            and math.isclose(d, 0.0, rel_tol=0.0, abs_tol=1e-12)
            and a > 0.0 and e < 0.0):
        raise WindowedTerrainMappingError("M2-A supports only north-up axis-aligned source pixels")
    if not (math.isclose(a, metadata.pixel_size_x, rel_tol=0.0, abs_tol=1e-10)
            and math.isclose(-e, metadata.pixel_size_y, rel_tol=0.0, abs_tol=1e-10)):
        raise WindowedTerrainMappingError("source affine pixel size differs from DEM metadata")
    expected = (c, f + e * metadata.height, c + a * metadata.width, f)
    if any(not math.isclose(actual, declared, rel_tol=0.0, abs_tol=1e-6)
           for actual, declared in zip(expected, metadata.bounds)):
        raise WindowedTerrainMappingError("source affine bounds differ from DEM metadata")
    return a, b, c, d, e, f


class _Kahan:
    __slots__ = ("value", "correction")

    def __init__(self) -> None:
        self.value = 0.0
        self.correction = 0.0

    def add(self, value: float) -> None:
        adjusted = float(value) - self.correction
        next_value = self.value + adjusted
        self.correction = (next_value - self.value) - adjusted
        self.value = next_value


def _geometry_candidates(tree: Any, polygons: list[Any], by_wkb: dict[bytes, list[int]], shape: Any) -> list[int]:
    hits = tree.query(shape)
    result: set[int] = set()
    for item in hits:
        if isinstance(item, (int, np.integer)):
            index = int(item)
        else:
            matches = by_wkb.get(bytes(item.wkb), ())
            for index in matches:
                if polygons[index].intersection(shape).area > 0.0:
                    result.add(index)
            continue
        if polygons[index].intersection(shape).area > 0.0:
            result.add(index)
    return sorted(result)


def _window_polygon(metadata: DEMMetadata, window: DEMWindow, box: Any) -> Any:
    a, _, c, _, e, f = _affine(metadata)
    left = c + window.col_off * a
    right = left + window.width * a
    top = f + window.row_off * e
    bottom = top + window.height * e
    return box(left, bottom, right, top)


def _face_pixel_bounds(face_bounds: tuple[float, float, float, float], metadata: DEMMetadata,
                       window: DEMWindow) -> tuple[int, int, int, int]:
    a, _, c, _, e, f = _affine(metadata)
    xmin, ymin, xmax, ymax = face_bounds
    col0 = max(window.col_off, 0, math.floor((xmin - c) / a))
    col1 = min(window.col_off + window.width, metadata.width, math.ceil((xmax - c) / a))
    row0 = max(window.row_off, 0, math.floor((f - ymax) / -e))
    row1 = min(window.row_off + window.height, metadata.height, math.ceil((f - ymin) / -e))
    return row0, row1, col0, col1


def _vertical_provenance(metadata: DEMMetadata, declared_unit: str | None,
                         declared_datum: str | None) -> tuple[str, str, str | None, str | None, str | None, str | None]:
    source_unit = metadata.vertical_unit
    meter_aliases = {"m", "meter", "meters", "metre", "metres"}
    if source_unit is not None:
        if source_unit.strip().casefold() not in meter_aliases:
            raise WindowedTerrainMappingError(f"DEM vertical unit '{source_unit}' is not an attested metre unit")
        if declared_unit not in (None, "m"):
            raise WindowedTerrainMappingError("declared vertical_unit must be 'm'; conversions are not implemented")
        unit_source = "dem_metadata"
    else:
        if declared_unit != "m":
            raise WindowedTerrainMappingError("source vertical unit is missing; an explicit source-attested 'm' declaration is required")
        unit_source = "config_declaration"
    source_datum = metadata.vertical_datum
    if source_datum is not None and declared_datum is not None and source_datum.strip() != declared_datum.strip():
        raise WindowedTerrainMappingError("declared vertical datum conflicts with source metadata; conversion is not implemented")
    if source_datum is not None:
        datum, datum_source = source_datum.strip(), "dem_metadata"
    elif declared_datum is not None:
        datum, datum_source = declared_datum.strip(), "config_declaration"
    else:
        datum, datum_source = None, None
    return "m", unit_source, datum, datum_source, source_unit, source_datum


def _check_chunk(window: DEMWindow, chunk: DEMDataset, source: DEMMetadata) -> None:
    if chunk.shape != window.shape or chunk.metadata.width != window.width or chunk.metadata.height != window.height:
        raise WindowedTerrainMappingError("reader returned a raw tile whose shape differs from its DEMWindow")
    for name in ("dtype", "crs", "horizontal_unit", "nodata_value", "vertical_unit", "vertical_datum", "area_or_point"):
        if getattr(chunk.metadata, name) != getattr(source, name):
            raise WindowedTerrainMappingError(f"source {name} metadata changed while streaming windows")
    a, b, c, d, e, f = _affine(source)
    expected = (a, b, c + a * window.col_off + b * window.row_off,
                d, e, f + d * window.col_off + e * window.row_off)
    if len(chunk.metadata.transform) < 6 or any(
            not math.isclose(float(actual), float(want), rel_tol=0.0, abs_tol=1e-8)
            for actual, want in zip(chunk.metadata.transform[:6], expected)):
        raise WindowedTerrainMappingError("reader window affine is not the source affine plus its global offsets")


def map_triangular_mesh_from_windows(
    reader: Any,
    source: str | Path,
    mesh: TriangularMesh,
    *,
    source_sha256: str,
    config: WindowedTriangleMappingConfig,
    config_base: str | Path | None = None,
) -> tuple[FaceTerrainField, dict[str, Any]]:
    """Map a triangle mesh using selected row-major raw windows and exact clips.

    The reader must provide ``read_metadata`` and ``iter_windows_selected``.
    The reader calls a metadata/offset filter before reading raw values. The
    mapper advances manually and drops each yielded tile and its scratch arrays
    before requesting the next one. CRS strings must match exactly in this M2-A route; no
    authority alias or coordinate transformation is inferred.
    """
    if not isinstance(config, WindowedTriangleMappingConfig):
        raise TypeError("config must be a WindowedTriangleMappingConfig")
    source_digest = _require_sha256(source_sha256, "source_sha256")
    evidence_paths: dict[str, dict[str, str]] = {}
    evidence_refs = (
        ("coverage_policy", config.coverage_policy_evidence_path, config.coverage_policy_evidence_sha256),
        ("source_vertical_unit", config.source_vertical_unit_evidence_path, config.source_vertical_unit_evidence_sha256),
        ("source_vertical_datum", config.source_vertical_datum_evidence_path, config.source_vertical_datum_evidence_sha256),
    )
    for name, evidence_path, evidence_hash in evidence_refs:
        if evidence_path is None:
            continue
        if config_base is None or evidence_hash is None:
            raise WindowedTerrainMappingError(f"{name} evidence requires config_base and a configured SHA-256")
        resolved, actual = _verify_evidence(evidence_path, evidence_hash, config_base, name)
        evidence_paths[name] = {"path": resolved, "sha256": actual}
    if not isinstance(mesh, TriangularMesh):
        raise TypeError("mesh must be a validated TriangularMesh")
    if mesh.n_face > config.max_faces:
        raise WindowedTerrainMappingError(f"mesh faces {mesh.n_face} exceed max_faces={config.max_faces}")
    if mesh.n_node > config.max_nodes:
        raise WindowedTerrainMappingError(f"mesh nodes {mesh.n_node} exceed max_nodes={config.max_nodes}")
    if not hasattr(reader, "read_metadata") or not hasattr(reader, "iter_windows_selected"):
        raise TypeError("reader must implement read_metadata and iter_windows_selected")
    try:
        _validate_projected_metric_crs(mesh.coordinate_system)
    except MeshPreparationError as exc:
        raise WindowedTerrainMappingError(str(exc)) from exc
    source_candidate = Path(source).expanduser()
    if source_candidate.is_symlink():
        raise WindowedTerrainMappingError(f"source raster must not be a symlink: {source_candidate}")
    source_file = source_candidate.resolve()
    if not source_file.is_file():
        raise WindowedTerrainMappingError(f"source raster must be a regular file: {source_file}")
    source_identity_before = _source_stat_identity(source_file)
    metadata = reader.read_metadata(source_file)
    if _source_stat_identity(source_file) != source_identity_before:
        raise WindowedTerrainMappingError("source file changed while reading metadata; no mapping result is valid")
    if not isinstance(metadata, DEMMetadata) or metadata.band_count != 1:
        raise WindowedTerrainMappingError("windowed triangle mapping requires DEMMetadata for exactly one band")
    if metadata.pixel_count > config.max_source_pixels:
        raise WindowedTerrainMappingError(
            f"source pixels {metadata.pixel_count} exceed max_source_pixels={config.max_source_pixels} before reading raster values"
        )
    if not metadata.crs:
        raise WindowedTerrainMappingError("DEM CRS is missing; CRS cannot be guessed")
    horizontal_aliases = {"m", "meter", "meters", "metre", "metres"}
    if metadata.horizontal_unit is None or metadata.horizontal_unit.strip().casefold() not in horizontal_aliases:
        raise WindowedTerrainMappingError("DEM horizontal units must be explicitly attested as metres")
    if metadata.crs.strip() != mesh.coordinate_system.strip():
        raise WindowedTerrainMappingError(
            "source CRS text differs from mesh CRS; M2-A does not infer CRS aliases or reproject coordinates"
        )
    transform = _affine(metadata)
    vertical_unit, unit_source, vertical_datum, datum_source, source_unit, source_datum = _vertical_provenance(
        metadata, config.source_vertical_unit_declaration, config.source_vertical_datum_declaration
    )
    try:
        from shapely.geometry import Polygon, box
        from shapely.strtree import STRtree
    except ImportError as exc:
        raise WindowedTerrainMappingError("M2-A triangle-window mapping requires the triangular GIS extra") from exc

    polygons: list[Any] = []
    by_wkb: dict[bytes, list[int]] = {}
    for index, nodes in enumerate(mesh.face_nodes):
        polygon = Polygon([mesh.node_xy[node] for node in nodes])
        if not polygon.is_valid or polygon.area <= 0.0:
            raise WindowedTerrainMappingError(f"face_id={mesh.face_id[index]} geometry is not a valid positive-area triangle")
        if not math.isclose(float(polygon.area), float(mesh.face_area[index]), rel_tol=1e-9, abs_tol=1e-8):
            raise WindowedTerrainMappingError(f"face_id={mesh.face_id[index]} polygon area differs from TriangularMesh.face_area")
        polygons.append(polygon)
        by_wkb.setdefault(bytes(polygon.wkb), []).append(index)
    tree = STRtree(polygons)

    n_face = mesh.n_face
    accumulators = {
        name: [_Kahan() for _ in range(n_face)]
        for name in ("covered", "valid", "nodata", "nonfinite", "masked_other", "ignored_tiny",
                     "ignored_valid", "ignored_weighted_z", "weighted_z")
    }
    stats: dict[str, int] = {
        "tiles_enumerated": 0, "tiles_selected": 0, "tiles_skipped_no_face_overlap": 0,
        "source_pixels_read": 0, "candidate_face_tile_pairs": 0,
        "candidate_overlap_checks": 0, "clip_calls": 0, "ignored_tiny_overlaps": 0,
        "valid_source_pixels": 0, "nodata_source_pixels": 0, "nonfinite_source_pixels": 0,
        "max_window_pixels": 0, "max_raw_tile_payload_bytes": 0,
    }
    candidates_by_window: dict[tuple[int, int, int, int], list[int]] = {}
    previous_window: tuple[int, int] | None = None
    expected_tile_cols = math.ceil(metadata.width / config.tile_cols)
    expected_tile_rows = math.ceil(metadata.height / config.tile_rows)
    expected_tile_count = expected_tile_cols * expected_tile_rows
    source_metadata_signature = (
        metadata.width, metadata.height, metadata.band_count, metadata.dtype, metadata.bounds,
        tuple(metadata.transform), metadata.crs, metadata.horizontal_unit, metadata.nodata_value,
        metadata.vertical_unit, metadata.vertical_datum, metadata.area_or_point,
    )

    def include_window(window_metadata: DEMMetadata, window: DEMWindow) -> bool:
        nonlocal previous_window
        if (window.height > config.tile_rows or window.width > config.tile_cols
                or window.width * window.height > config.tile_rows * config.tile_cols):
            raise WindowedTerrainMappingError("reader window exceeds configured tile dimensions before raw read")
        if window.row_off + window.height > metadata.height or window.col_off + window.width > metadata.width:
            raise WindowedTerrainMappingError("reader window exceeds source bounds before raw read")
        tile_index = stats["tiles_enumerated"]
        if tile_index >= expected_tile_count:
            raise WindowedTerrainMappingError("reader enumerated more source tiles than the configured row-major grid")
        expected_tile_row, expected_tile_col = divmod(tile_index, expected_tile_cols)
        expected_window = DEMWindow(
            expected_tile_row * config.tile_rows,
            expected_tile_col * config.tile_cols,
            min(config.tile_rows, metadata.height - expected_tile_row * config.tile_rows),
            min(config.tile_cols, metadata.width - expected_tile_col * config.tile_cols),
        )
        if window != expected_window:
            raise WindowedTerrainMappingError(
                "selected-window provider must enumerate the complete nonoverlapping row-major source tile grid"
            )
        stats["tiles_enumerated"] += 1
        window_metadata_signature = (
            window_metadata.width, window_metadata.height, window_metadata.band_count, window_metadata.dtype,
            window_metadata.bounds, tuple(window_metadata.transform), window_metadata.crs,
            window_metadata.horizontal_unit, window_metadata.nodata_value, window_metadata.vertical_unit,
            window_metadata.vertical_datum, window_metadata.area_or_point,
        )
        if window_metadata_signature != source_metadata_signature:
            raise WindowedTerrainMappingError("source metadata changed between metadata probe and tile enumeration")
        order = (window.row_off, window.col_off)
        if previous_window is not None and order <= previous_window:
            raise WindowedTerrainMappingError("selected raw windows must be strictly row-major without duplicates")
        previous_window = order
        tile_shape = _window_polygon(metadata, window, box)
        candidates = _geometry_candidates(tree, polygons, by_wkb, tile_shape)
        key = (window.row_off, window.col_off, window.height, window.width)
        if not candidates:
            stats["tiles_skipped_no_face_overlap"] += 1
            return False
        candidates_by_window[key] = candidates
        stats["tiles_selected"] += 1
        stats["candidate_face_tile_pairs"] += len(candidates)
        return True

    source_path = str(source_file)
    seen_windows: set[tuple[int, int, int, int]] = set()
    def consume_window(window: DEMWindow, chunk: DEMDataset) -> None:
        key = (window.row_off, window.col_off, window.height, window.width)
        if key in seen_windows or key not in candidates_by_window:
            raise WindowedTerrainMappingError("reader yielded a duplicate or unselected source window")
        seen_windows.add(key)
        if (window.height > config.tile_rows or window.width > config.tile_cols
                or window.width * window.height > config.tile_rows * config.tile_cols):
            raise WindowedTerrainMappingError("reader returned a tile larger than the configured source-pixel limit")
        if window.row_off + window.height > metadata.height or window.col_off + window.width > metadata.width:
            raise WindowedTerrainMappingError("reader yielded a source window outside the raster bounds")
        _check_chunk(window, chunk, metadata)
        raw = np.asarray(chunk.elevation, dtype=np.float64)
        valid_mask = np.asarray(chunk.valid_mask, dtype=bool)
        nodata_mask = np.asarray(chunk.nodata_mask, dtype=bool)
        nonfinite_mask = np.asarray(chunk.nonfinite_mask, dtype=bool)
        if raw.shape != window.shape or valid_mask.shape != window.shape or nodata_mask.shape != window.shape or nonfinite_mask.shape != window.shape:
            raise WindowedTerrainMappingError("raw window values and masks have inconsistent shapes")
        if np.any(valid_mask & (nodata_mask | nonfinite_mask | ~np.isfinite(raw))):
            raise WindowedTerrainMappingError("reader marked a NoData/nonfinite raw value as valid")
        if metadata.nodata_value is not None:
            sentinel = np.isfinite(raw) & (raw == float(metadata.nodata_value))
            if np.any(valid_mask & sentinel):
                raise WindowedTerrainMappingError("reader marked a source NoData sentinel as valid")
        stats["source_pixels_read"] += int(raw.size)
        stats["max_window_pixels"] = max(stats["max_window_pixels"], int(raw.size))
        try:
            source_itemsize=int(np.dtype(metadata.dtype).itemsize)
        except (TypeError, ValueError) as exc:
            raise WindowedTerrainMappingError(f"unsupported DEM dtype '{metadata.dtype}'") from exc
        stats["max_raw_tile_payload_bytes"] = max(stats["max_raw_tile_payload_bytes"], int(raw.size * source_itemsize))
        stats["valid_source_pixels"] += int(valid_mask.sum())
        stats["nodata_source_pixels"] += int((nodata_mask & ~nonfinite_mask).sum())
        stats["nonfinite_source_pixels"] += int(nonfinite_mask.sum())
        for face_index in candidates_by_window[key]:
            polygon = polygons[face_index]
            row0, row1, col0, col1 = _face_pixel_bounds(polygon.bounds, metadata, window)
            face_id = mesh.face_id[face_index]
            for source_row in range(row0, row1):
                local_row = source_row - window.row_off
                for source_col in range(col0, col1):
                    local_col = source_col - window.col_off
                    stats["candidate_overlap_checks"] += 1
                    if stats["candidate_overlap_checks"] > config.max_candidate_overlap_checks:
                        diagnostics = {"stats": dict(stats), "limit": config.max_candidate_overlap_checks,
                                       "first_over_budget_face_id": int(face_id),
                                       "source_pixel_row": source_row, "source_pixel_col": source_col}
                        raise WindowedTerrainMappingError(
                            f"candidate overlap checks exceed max_candidate_overlap_checks={config.max_candidate_overlap_checks}",
                            diagnostics=diagnostics,
                        )
                    pixel = _pixel_polygon(metadata.transform, source_row, source_col)
                    clipped = _clip_convex(tuple(polygon.exterior.coords)[:-1], pixel)
                    stats["clip_calls"] += 1
                    overlap = abs(_signed_polygon_area(clipped)) if len(clipped) >= 3 else 0.0
                    tiny_limit = max(mesh.geometry_tolerance_m ** 2, mesh.face_area[face_index] * 1e-14)
                    if overlap <= tiny_limit:
                        if overlap > 0.0:
                            stats["ignored_tiny_overlaps"] += 1
                            accumulators["ignored_tiny"][face_index].add(overlap)
                            if valid_mask[local_row, local_col]:
                                accumulators["ignored_valid"][face_index].add(overlap)
                                accumulators["ignored_weighted_z"][face_index].add(overlap * float(raw[local_row, local_col]))
                        continue
                    accumulators["covered"][face_index].add(overlap)
                    if nonfinite_mask[local_row, local_col]:
                        accumulators["nonfinite"][face_index].add(overlap)
                    elif nodata_mask[local_row, local_col]:
                        accumulators["nodata"][face_index].add(overlap)
                    elif valid_mask[local_row, local_col]:
                        accumulators["valid"][face_index].add(overlap)
                        accumulators["weighted_z"][face_index].add(overlap * float(raw[local_row, local_col]))
                    else:
                        accumulators["masked_other"][face_index].add(overlap)

    def process_item(item: tuple[DEMWindow, DEMDataset]) -> None:
        window, chunk = item
        consume_window(window, chunk)

    window_iterator = iter(reader.iter_windows_selected(
        source, window_width=config.tile_cols, window_height=config.tile_rows,
        gdal_cache_mib=config.gdal_cache_mib, include_window=include_window,
    ))
    try:
        while True:
            try:
                item = next(window_iterator)
            except StopIteration:
                break
            try:
                process_item(item)
            finally:
                # Clearing both references before next() allows a provider that
                # uses yield to release its suspended return value before the
                # next raw tile is created.
                del item
    except WindowedTerrainMappingError:
        raise
    except DEMReaderError as exc:
        raise WindowedTerrainMappingError(f"raw window provider failed: {exc}") from exc
    finally:
        close = getattr(window_iterator, "close", None)
        if callable(close):
            close()

    if stats["tiles_enumerated"] != expected_tile_count:
        raise WindowedTerrainMappingError(
            f"selected-window stream ended early: enumerated {stats['tiles_enumerated']} of {expected_tile_count} source tiles"
        )
    if len(seen_windows) != stats["tiles_selected"]:
        raise WindowedTerrainMappingError("reader did not yield every selected candidate window")
    source_identity_after = _source_stat_identity(source_file)
    if source_identity_after != source_identity_before:
        raise WindowedTerrainMappingError("source file changed during window mapping; no mapping result is valid",
            diagnostics={"source_identity_before":source_identity_before,"source_identity_after":source_identity_after})

    face_rows: list[dict[str, Any]] = []
    elevation: list[float | None] = []
    valid: list[bool] = []
    covered: list[float] = []
    valid_area: list[float] = []
    coverage: list[float] = []
    failures: list[str] = []
    area_abs_tol = config.area_abs_tolerance_m2
    for index, face_id in enumerate(mesh.face_id):
        face_area = float(mesh.face_area[index])
        area = {name: accumulators[name][index].value for name in ("covered", "valid", "nodata", "nonfinite", "masked_other")}
        ignored_area = accumulators["ignored_tiny"][index].value
        area_tolerance = max(area_abs_tol, face_area * config.area_rel_tolerance)
        explained_area = area["covered"] + ignored_area
        if explained_area > face_area + area_tolerance:
            failures.append(f"face_id={face_id} covered plus ignored area {explained_area:.12g} exceeds face area {face_area:.12g}")
        if ignored_area > area_tolerance:
            failures.append(f"face_id={face_id} ignored in-raster overlap {ignored_area:.12g} exceeds frozen area tolerance {area_tolerance:.12g}")
        if area["valid"] > area["covered"] + max(area_abs_tol, face_area * config.area_rel_tolerance):
            failures.append(f"face_id={face_id} valid area exceeds covered source area")
        area["covered"] = min(face_area, max(0.0, area["covered"]))
        area["valid"] = min(area["covered"], max(0.0, area["valid"]))
        ignored_valid_area = accumulators["ignored_valid"][index].value
        ignored_weighted_z = accumulators["ignored_weighted_z"][index].value
        classified = area["valid"] + area["nodata"] + area["nonfinite"] + area["masked_other"]
        if abs(area["covered"] - classified) > max(area_abs_tol, face_area * config.area_rel_tolerance):
            failures.append(f"face_id={face_id} covered area does not partition into valid/NoData/nonfinite/masked categories")
        ratio = min(1.0, area["valid"] / face_area)
        weighted_z = accumulators["weighted_z"][index].value
        value = weighted_z / area["valid"] if area["valid"] > 0.0 else None
        elevation_with_ignored = ((weighted_z + ignored_weighted_z) / (area["valid"] + ignored_valid_area)
                                  if area["valid"] + ignored_valid_area > 0.0 else None)
        ignored_elevation_delta = (abs(elevation_with_ignored - value)
                                   if elevation_with_ignored is not None and value is not None else 0.0)
        ignored_elevation_tolerance = max(config.elevation_abs_tolerance_m,
                                          abs(elevation_with_ignored or 0.0) * config.elevation_rel_tolerance)
        if ignored_elevation_delta > ignored_elevation_tolerance:
            failures.append(f"face_id={face_id} ignored overlap elevation effect {ignored_elevation_delta:.12g} m exceeds frozen tolerance {ignored_elevation_tolerance:.12g} m")
        if area["valid"] <= 0.0:
            failures.append(f"face_id={face_id} has zero valid DEM overlap area")
        if (config.coverage_policy == "minimum_valid_coverage" and config.minimum_valid_coverage is not None
                and ratio + config.area_rel_tolerance < config.minimum_valid_coverage):
            failures.append(f"face_id={face_id} coverage {ratio:.12g} is below the configured minimum {config.minimum_valid_coverage:.12g}")
        outside = max(0.0, face_area - explained_area)
        face_rows.append({"face_id": int(face_id), "face_area_m2": face_area,
            "covered_area_m2": area["covered"], "valid_area_m2": area["valid"],
            "nodata_area_m2": area["nodata"], "nonfinite_area_m2": area["nonfinite"],
            "masked_other_area_m2": area["masked_other"], "ignored_in_raster_area_m2": ignored_area,
            "ignored_valid_area_m2": ignored_valid_area,
            "ignored_valid_weighted_sum_m3": ignored_weighted_z,
            "ignored_elevation_delta_m": ignored_elevation_delta,
            "ignored_elevation_tolerance_m": ignored_elevation_tolerance,
            "outside_raster_area_m2": outside,
            "coverage_ratio": ratio, "elevation_m": value})
        elevation.append(value);valid.append(value is not None)
        covered.append(area["covered"]);valid_area.append(area["valid"]);coverage.append(ratio)
    report = {
        "format": "triangle-terrain-windowed-report-v1",
        "status": "diagnostic_only" if config.coverage_policy == "diagnostic_only" else "coverage_policy_passed_preparation_only",
        "preparation_only": True, "simulation_result": False, "production_approved": False,
        "source_path": source_path, "source_sha256": source_digest,
        "source_file_identity": {"before":source_identity_before,"after":source_identity_after,
                                  "identity_policy":"Caller-supplied SHA-256 is from the verified manifest; file stat/metadata are rechecked during mapping, hash bytes are not reread here."},
        "source_shape_yx": [metadata.height, metadata.width], "source_pixel_count": metadata.pixel_count,
        "source_crs": metadata.crs, "source_horizontal_unit": metadata.horizontal_unit,
        "source_transform": [float(value) for value in metadata.transform],
        "source_nodata_value": metadata.nodata_value, "source_area_or_point": metadata.area_or_point,
        "source_vertical_unit": metadata.vertical_unit, "source_vertical_datum": metadata.vertical_datum,
        "vertical_unit": vertical_unit, "vertical_unit_source": unit_source,
        "vertical_datum": vertical_datum, "vertical_datum_source": datum_source,
        "source_evidence": evidence_paths,
        "mesh_id": mesh.mesh_id, "mesh_fingerprint_sha256": mesh_fingerprint_sha256(mesh),
        "mesh_mapping_parameters": {"geometry_tolerance_m": float(mesh.geometry_tolerance_m),
                                    "minimum_face_area_m2": float(mesh.minimum_face_area_m2)},
        "face_count": mesh.n_face, "node_count": mesh.n_node,
        "coverage_policy": config.coverage_policy, "minimum_valid_coverage": config.minimum_valid_coverage,
        "tolerances": {"area_abs_m2": config.area_abs_tolerance_m2, "area_rel": config.area_rel_tolerance,
                       "elevation_abs_m": config.elevation_abs_tolerance_m, "elevation_rel": config.elevation_rel_tolerance},
        "resource_limits": config.model_dump(mode="json"),
        "resource_memory_gate": {
            "raw_tile_payload_upper_bound_bytes": int(config.tile_rows * config.tile_cols * np.dtype(metadata.dtype).itemsize),
            "max_inflight_tiles": 1,
            "gdal_cache_bytes": config.gdal_cache_mib * 1024 * 1024,
            "total_process_memory_limit_mib": None,
            "m2b_requirement": "Measure combined Python mesh arrays, STRtree/GEOS, raw tile tuple/masks, GDAL cache, and writer buffers; enforce a process-level stop before actual D-S preparation.",
        },
        "stats": dict(stats),
        "native_face_area_total_m2": float(sum(mesh.face_area)),
        "covered_area_total_m2": float(sum(row["covered_area_m2"] for row in face_rows)),
        "valid_area_total_m2": float(sum(row["valid_area_m2"] for row in face_rows)),
        "faces": face_rows,
        "unassigned_engineering_boundary_groups": True,
    }
    if failures:
        report["status"] = "failed"
        report["failures"] = failures
        raise WindowedTerrainMappingError("; ".join(failures[:5]), diagnostics=report)
    terrain = FaceTerrainField(
        mesh.mesh_id or "", tuple(elevation), tuple(valid), tuple(covered), tuple(valid_area), tuple(coverage),
        mesh.coordinate_system, vertical_datum, vertical_unit, unit_source, datum_source,
        source_unit, source_datum,
    )
    try:
        terrain.validate_for(mesh)
    except TriangularMeshError as exc:
        raise WindowedTerrainMappingError(f"windowed FaceTerrainField failed mesh validation: {exc}") from exc
    return terrain, report


def write_windowed_mapping_report(path: str | Path, report: dict[str, Any]) -> Path:
    """Write a no-clobber diagnostic report; never marks a simulation complete."""
    target = Path(path)
    if target.exists():
        raise WindowedTerrainMappingError(f"diagnostic report target already exists: {target}")
    if not target.parent.is_dir():
        raise WindowedTerrainMappingError(f"diagnostic report parent directory is missing: {target.parent}")
    payload = dict(report)
    payload["preparation_only"] = True
    payload["simulation_result"] = False
    payload["production_approved"] = False
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        temporary.unlink()
    except BaseException as exc:
        temporary.unlink(missing_ok=True)
        if target.exists():
            raise WindowedTerrainMappingError(f"diagnostic report target already exists: {target}") from exc
        raise WindowedTerrainMappingError(f"could not atomically publish diagnostic report '{target}': {exc}") from exc
    return target


__all__ = [
    "WindowedTerrainMappingError",
    "WindowedTriangleMappingConfig",
    "map_triangular_mesh_from_windows",
    "mesh_fingerprint_sha256",
    "write_windowed_mapping_report",
]
