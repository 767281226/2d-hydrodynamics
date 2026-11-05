"""Assumed river-section selection on exterior mesh edges by a DEM main-channel rule.

This module turns a small list of *assumed* river crossing points (one per open
boundary section) into contiguous runs of exterior ``TriangularMesh`` edges and
writes an ``assumed-river-sections-audit-v1`` JSON document.  The selection rule
is a terrain heuristic only:

1. The exterior edges of the mesh (edges with one face) are chained into loops;
   the longest loop is the outer boundary.  It is oriented consecutively and an
   arclength ``s`` is assigned to every edge start.
2. Each edge receives a mean (and minimum) elevation from raw DEM samples taken
   at fixed fractions along the edge (NoData -> NaN, ``nanmean``).
3. For each requested section the nearest edge midpoint anchors a window of
   ``window_m`` along the loop (wrap-around).  The thalweg is the lowest edge
   within ``thalweg_search_m``; the bank level is a percentile of the window
   elevations; the threshold is ``max(bank - bank_margin_m, thalweg +
   thalweg_margin_m)``.  The run expands from the thalweg in both directions,
   accepting edges below the threshold and tolerating above-threshold runs whose
   cumulative length is at most ``gap_tolerance_m`` when a below-threshold edge
   follows.  Expansion stops at the window end.

The sections are assumed and non-authoritative; the audit records that status
explicitly.  The DEM is read as raw values at the sampled pixels: there is no
CRS conversion, resampling, or NoData filling.  No solver work happens here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import warnings
from dataclasses import dataclass, field
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .dem_contract import DEMReaderError, GeoTIFFDEMReader
from .triangular_mesh import TriangularMesh, TriangularMeshError

REQUEST_FORMAT = "assumed-river-sections-request-v1"
AUDIT_FORMAT = "assumed-river-sections-audit-v1"
SCHEMA_VERSION = "1.0"
SECTION_STATUS = "ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE"
MESH_ARTIFACT_FORMAT = "triangle-domain-mesh-only-artifact-v1"
SECTION_ROLES = ("inflow", "outflow")

# name -> (default, low, high, low_inclusive, high_inclusive, integer)
RULE_PARAMETER_LIMITS: dict[str, tuple[float, float, float, bool, bool, bool]] = {
    "window_m": (1500.0, 50.0, 50000.0, True, True, False),
    "thalweg_search_m": (300.0, 0.0, 50000.0, False, True, False),
    "bank_percentile": (75, 50.0, 100.0, True, True, False),
    "bank_margin_m": (2.0, 0.0, 50.0, True, True, False),
    "thalweg_margin_m": (1.5, 0.0, 50.0, True, True, False),
    "gap_tolerance_m": (60.0, 0.0, 50000.0, True, True, False),
    "dem_samples_per_edge": (5, 1, 25, True, True, True),
}
MAX_POINT_DISTANCE_LIMITS = (100.0, 0.0, 10000.0)  # default, exclusive low, inclusive high


class RiverSectionSelectionError(ValueError):
    """Validation or selection failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _is_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def _bounded_number(value: Any, name: str, low: float, high: float, *, low_inclusive: bool = True,
                    high_inclusive: bool = True, integer: bool = False) -> float | int:
    if integer:
        if isinstance(value, bool) or not isinstance(value, Integral):
            raise RiverSectionSelectionError("parameter_type", f"{name} must be an integer")
    elif not _is_number(value):
        raise RiverSectionSelectionError("parameter_type", f"{name} must be a finite number")
    number = int(value) if integer else float(value)
    low_ok = number >= low if low_inclusive else number > low
    high_ok = number <= high if high_inclusive else number < high
    if not (low_ok and high_ok):
        lo = "[" if low_inclusive else "("
        hi = "]" if high_inclusive else ")"
        raise RiverSectionSelectionError("parameter_range", f"{name}={number!r} is outside the legal range {lo}{low:g}, {high:g}{hi}")
    return number


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RiverSectionSelectionError("field_type", f"{name} must be a non-empty string")
    return value.strip()


# --------------------------------------------------------------------------- request


@dataclass(frozen=True)
class ChannelRuleParameters:
    """DEM main-channel rule parameters (SI units; see RULE_PARAMETER_LIMITS)."""

    window_m: float = 1500.0
    thalweg_search_m: float = 300.0
    bank_percentile: float = 75
    bank_margin_m: float = 2.0
    thalweg_margin_m: float = 1.5
    gap_tolerance_m: float = 60.0
    dem_samples_per_edge: int = 5

    def __post_init__(self) -> None:
        for name, (_default, low, high, low_inc, high_inc, integer) in RULE_PARAMETER_LIMITS.items():
            value = _bounded_number(getattr(self, name), f"rule.{name}", low, high, low_inclusive=low_inc,
                                    high_inclusive=high_inc, integer=integer)
            object.__setattr__(self, name, value)
        if self.thalweg_search_m > self.window_m:
            raise RiverSectionSelectionError("parameter_range", "rule.thalweg_search_m must not exceed rule.window_m")
        if self.gap_tolerance_m > self.window_m:
            raise RiverSectionSelectionError("parameter_range", "rule.gap_tolerance_m must not exceed rule.window_m")

    @classmethod
    def from_mapping(cls, payload: Any) -> "ChannelRuleParameters":
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            raise RiverSectionSelectionError("field_type", "rule must be an object")
        unknown = sorted(set(payload) - set(RULE_PARAMETER_LIMITS))
        if unknown:
            raise RiverSectionSelectionError("unknown_field", f"rule contains unknown parameters {unknown}")
        return cls(**dict(payload))

    @property
    def sample_fractions(self) -> tuple[float, ...]:
        k = int(self.dem_samples_per_edge)
        return tuple((2 * j + 1) / (2 * k) for j in range(k))

    def audit_record(self) -> dict[str, Any]:
        fractions = self.sample_fractions
        record = {name: getattr(self, name) for name in RULE_PARAMETER_LIMITS if name != "dem_samples_per_edge"}
        record["dem_samples_per_edge"] = int(self.dem_samples_per_edge)
        record["edge_elevation"] = (f"mean of {self.dem_samples_per_edge} DEM samples at "
                                    f"{fractions[0]:g}..{fractions[-1]:g} of the edge")
        return record


@dataclass(frozen=True)
class SectionRequest:
    name: str
    role: str
    xy: tuple[float, float]
    discharge_series_column: str
    name_zh: str

    @classmethod
    def from_mapping(cls, payload: Any, index: int) -> "SectionRequest":
        label = f"sections[{index}]"
        if not isinstance(payload, Mapping):
            raise RiverSectionSelectionError("field_type", f"{label} must be an object")
        allowed = {"name", "role", "xy", "discharge_series_column", "name_zh"}
        missing = sorted(allowed - set(payload))
        if missing:
            raise RiverSectionSelectionError("missing_field", f"{label} is missing required fields {missing}")
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise RiverSectionSelectionError("unknown_field", f"{label} contains unknown fields {unknown}")
        name = _text(payload["name"], f"{label}.name")
        if len(name) > 32 or not all(ch.isalnum() or ch in "_-" for ch in name):
            raise RiverSectionSelectionError("field_type", f"{label}.name must match [A-Za-z0-9_-]{{1,32}}")
        role = payload["role"]
        if role not in SECTION_ROLES:
            raise RiverSectionSelectionError("section_role", f"{label}.role must be one of {list(SECTION_ROLES)}, got {role!r}")
        xy = payload["xy"]
        if not isinstance(xy, Sequence) or isinstance(xy, str) or len(xy) != 2 or not all(_is_number(v) for v in xy):
            raise RiverSectionSelectionError("field_type", f"{label}.xy must be [x, y] finite numbers in the mesh CRS (m)")
        column = _text(payload["discharge_series_column"], f"{label}.discharge_series_column")
        if not (column[0].isalpha() or column[0] == "_") or not all(ch.isalnum() or ch == "_" for ch in column):
            raise RiverSectionSelectionError("field_type", f"{label}.discharge_series_column must be an identifier [A-Za-z_][A-Za-z0-9_]*")
        return cls(name, role, (float(xy[0]), float(xy[1])), column, _text(payload["name_zh"], f"{label}.name_zh"))


@dataclass(frozen=True)
class RiverSectionsRequest:
    crs: str
    sections: tuple[SectionRequest, ...]
    rule: ChannelRuleParameters = field(default_factory=ChannelRuleParameters)
    max_point_distance_m: float = MAX_POINT_DISTANCE_LIMITS[0]
    dem_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "crs", _text(self.crs, "crs"))
        object.__setattr__(self, "max_point_distance_m", _bounded_number(
            self.max_point_distance_m, "max_point_distance_m", MAX_POINT_DISTANCE_LIMITS[1], MAX_POINT_DISTANCE_LIMITS[2],
            low_inclusive=False))
        if not self.sections:
            raise RiverSectionSelectionError("missing_field", "sections must contain at least one section")
        names = [section.name for section in self.sections]
        if len(set(names)) != len(names):
            raise RiverSectionSelectionError("duplicate_section", f"section names must be unique: {names}")
        if self.dem_sha256 is not None:
            object.__setattr__(self, "dem_sha256", _sha256_text(self.dem_sha256, "dem_sha256"))

    @classmethod
    def from_mapping(cls, payload: Any) -> "RiverSectionsRequest":
        if not isinstance(payload, Mapping):
            raise RiverSectionSelectionError("field_type", "request must be a JSON object")
        allowed = {"format", "schema_version", "crs", "sections", "rule", "max_point_distance_m", "dem_sha256"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise RiverSectionSelectionError("unknown_field", f"request contains unknown fields {unknown}")
        if payload.get("format") != REQUEST_FORMAT:
            raise RiverSectionSelectionError("format", f"request.format must be {REQUEST_FORMAT!r}")
        if payload.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise RiverSectionSelectionError("format", f"request.schema_version must be {SCHEMA_VERSION!r}")
        for name in ("crs", "sections"):
            if name not in payload:
                raise RiverSectionSelectionError("missing_field", f"request is missing required field {name!r}")
        raw_sections = payload["sections"]
        if not isinstance(raw_sections, Sequence) or isinstance(raw_sections, str):
            raise RiverSectionSelectionError("field_type", "sections must be a list")
        sections = tuple(SectionRequest.from_mapping(item, index) for index, item in enumerate(raw_sections))
        return cls(
            crs=payload["crs"],
            sections=sections,
            rule=ChannelRuleParameters.from_mapping(payload.get("rule")),
            max_point_distance_m=payload.get("max_point_distance_m", MAX_POINT_DISTANCE_LIMITS[0]),
            dem_sha256=payload.get("dem_sha256"),
        )


def load_request(path: str | Path) -> RiverSectionsRequest:
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RiverSectionSelectionError("request_unreadable", f"cannot read request JSON {path}: {exc}") from exc
    return RiverSectionsRequest.from_mapping(payload)


def _sha256_text(value: Any, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise RiverSectionSelectionError("field_type", f"{name} must be a 64-character hexadecimal SHA-256")
    return text


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- CRS


def _crs_equivalent_text(left: str, right: str) -> bool:
    """Compare two CRS descriptions without transforming anything.

    With pyproj available both descriptions are parsed, bound CRS wrappers
    (TOWGS84) are unwrapped, and the project's projected-CRS equivalence policy
    is applied.  Without pyproj a normalised string comparison is used.
    """
    try:
        from pyproj import CRS
    except ImportError:  # pragma: no cover - depends on environment
        return left.strip().casefold() == right.strip().casefold()
    from .domain_mesh_only import _crs_equivalent

    def parse(text: str) -> Any:
        try:
            crs = CRS.from_user_input(text)
        except Exception as exc:
            raise RiverSectionSelectionError("crs_invalid", f"cannot parse CRS {text[:80]!r}: {exc}") from exc
        return crs.source_crs if getattr(crs, "is_bound", False) and crs.source_crs is not None else crs

    a, b = parse(left), parse(right)
    return bool(a.equals(b, ignore_axis_order=True) or _crs_equivalent(a, b))


# --------------------------------------------------------------------------- mesh


def load_mesh_artifact(path: str | Path) -> tuple[TriangularMesh, str]:
    """Load a ``triangle-domain-mesh-only-artifact-v1`` JSON file into a TriangularMesh."""
    path = Path(path)
    if not path.is_file():
        raise RiverSectionSelectionError("mesh_artifact_missing", f"mesh artifact not found: {path}")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RiverSectionSelectionError("mesh_artifact_malformed", f"mesh artifact is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("format") != MESH_ARTIFACT_FORMAT:
        raise RiverSectionSelectionError("mesh_artifact_format", f"mesh artifact format must be {MESH_ARTIFACT_FORMAT!r}")
    missing = [key for key in ("node_xy", "face_nodes", "coordinate_system", "mesh_id") if key not in payload]
    if missing:
        raise RiverSectionSelectionError("mesh_artifact_format", f"mesh artifact is missing fields {missing}")
    kwargs: dict[str, Any] = {key: payload[key] for key in ("face_id", "boundary_groups", "constraint_groups",
                                                             "geometry_tolerance_m", "minimum_face_area_m2") if key in payload}
    try:
        mesh = TriangularMesh(node_xy=payload["node_xy"], face_nodes=payload["face_nodes"],
                              coordinate_system=payload["coordinate_system"], mesh_id=payload["mesh_id"], **kwargs)
    except TriangularMeshError as exc:
        raise RiverSectionSelectionError("mesh_invalid", f"mesh artifact topology is invalid: {exc}") from exc
    return mesh, hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class OuterLoop:
    """Longest exterior edge loop of a mesh, oriented consecutively."""

    edge_indices: np.ndarray      # (n,) global mesh edge index (mesh.edge_nodes order)
    oriented_nodes: np.ndarray    # (n, 2) node indices in loop direction
    start_s: np.ndarray           # (n,) arclength at edge start, m
    length: np.ndarray            # (n,) edge length, m
    midpoint: np.ndarray          # (n, 2) edge midpoint xy, m
    loop_lengths: tuple[int, ...]  # edge counts of every exterior loop

    @property
    def perimeter_m(self) -> float:
        return float(self.length.sum())

    def relative_arclength(self, anchor: int) -> np.ndarray:
        """Signed arclength of every edge start relative to ``anchor`` (wrap-around)."""
        perimeter = self.perimeter_m
        rel = self.start_s - self.start_s[anchor]
        rel = np.where(rel > perimeter / 2.0, rel - perimeter, rel)
        rel = np.where(rel < -perimeter / 2.0, rel + perimeter, rel)
        return rel


def outer_boundary_loop(mesh: TriangularMesh) -> OuterLoop:
    """Chain exterior edges (one face) into loops and return the longest one."""
    xy = np.asarray(mesh.node_xy, dtype=float)
    edge_nodes = np.asarray(mesh.edge_nodes, dtype=np.int64)
    edge_faces = np.asarray(mesh.edge_faces, dtype=np.int64)
    exterior = np.nonzero((edge_faces[:, 0] == -1) | (edge_faces[:, 1] == -1))[0]
    if exterior.size == 0:
        raise RiverSectionSelectionError("mesh_no_exterior", "mesh has no exterior edges")
    ext = edge_nodes[exterior]
    adjacency: dict[int, list[tuple[int, int]]] = {}
    for k, (a, b) in enumerate(ext.tolist()):
        adjacency.setdefault(a, []).append((b, k))
        adjacency.setdefault(b, []).append((a, k))
    seen: set[int] = set()
    loops: list[list[int]] = []
    for start in adjacency:
        if start in seen:
            continue
        loop: list[int] = []
        node = start
        previous_edge = -1
        while True:
            seen.add(node)
            candidates = [(n, k) for n, k in adjacency[node] if k != previous_edge]
            if not candidates:
                break
            node, previous_edge = candidates[0]
            loop.append(previous_edge)
            if node == start:
                break
        loops.append(loop)
    outer = max(loops, key=len)
    oriented: list[tuple[int, int]] = []
    node = -1
    for k in outer:
        a, b = int(ext[k, 0]), int(ext[k, 1])
        if node < 0:
            node = a
        if a == node:
            oriented.append((a, b))
            node = b
        else:
            oriented.append((b, a))
            node = a
    oriented_nodes = np.asarray(oriented, dtype=np.int64)
    delta = xy[oriented_nodes[:, 1]] - xy[oriented_nodes[:, 0]]
    length = np.hypot(delta[:, 0], delta[:, 1])
    start_s = np.concatenate([[0.0], np.cumsum(length)])[:-1]
    midpoint = 0.5 * (xy[oriented_nodes[:, 0]] + xy[oriented_nodes[:, 1]])
    return OuterLoop(exterior[np.asarray(outer, dtype=np.int64)], oriented_nodes, start_s, length, midpoint,
                     tuple(len(loop) for loop in loops))


# --------------------------------------------------------------------------- DEM sampling


def sample_edge_elevations(dem_path: str | Path, xy_start: np.ndarray, xy_end: np.ndarray,
                           fractions: Sequence[float]) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Raw DEM values at fractions along each edge; returns (z_mean, z_min, nodata_count, dem_info).

    Pixels equal to the NoData value, non-finite pixels, and points outside the
    raster become NaN.  No resampling or filling is applied.
    """
    reader = GeoTIFFDEMReader()
    try:
        metadata = reader.read_metadata(dem_path)
    except DEMReaderError as exc:
        raise RiverSectionSelectionError("dem_unreadable", str(exc)) from exc
    rasterio = reader._rasterio()
    from rasterio.transform import rowcol

    n = len(xy_start)
    samples = np.full((len(fractions), n), np.nan)
    # GDAL_PAM_ENABLED=NO: never write .aux.xml sidecars next to the source raster.
    with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(Path(dem_path)) as dataset:
        nodata = dataset.nodata
        for row_index, fraction in enumerate(fractions):
            points = xy_start + float(fraction) * (xy_end - xy_start)
            rows, cols = rowcol(dataset.transform, points[:, 0], points[:, 1])
            rows = np.asarray(rows, dtype=np.int64)
            cols = np.asarray(cols, dtype=np.int64)
            inside = (rows >= 0) & (rows < dataset.height) & (cols >= 0) & (cols < dataset.width)
            if inside.any():
                coords = [(float(x), float(y)) for x, y in points[inside]]
                values = np.asarray([v[0] for v in dataset.sample(coords, masked=False)], dtype=float)
                if nodata is not None:
                    values[values == float(nodata)] = np.nan
                values[~np.isfinite(values)] = np.nan
                samples[row_index, inside] = values
    nodata_count = np.isnan(samples).sum(axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN edges stay NaN by design
        z_mean = np.nanmean(samples, axis=0)
        z_min = np.nanmin(samples, axis=0)
    dem_info = {"crs": metadata.crs, "nodata_value": metadata.nodata_value, "pixel_size": metadata.pixel_size}
    return z_mean, z_min, nodata_count, dem_info


# --------------------------------------------------------------------------- pure rule


@dataclass(frozen=True)
class ChannelRunSelection:
    window_indices: np.ndarray
    thalweg_index: int
    thalweg_elevation_m: float
    bank_level_m: float
    threshold_m: float
    lo: int
    hi: int
    run_indices: np.ndarray

    @property
    def edge_count(self) -> int:
        return int(len(self.run_indices))


def select_channel_run(rel_s: np.ndarray, length: np.ndarray, z: np.ndarray,
                       rule: ChannelRuleParameters) -> ChannelRunSelection:
    """Apply the DEM main-channel rule to one closed loop.

    ``rel_s`` is the signed arclength of every edge start relative to the
    anchor edge (wrapped to ``(-P/2, P/2]``), ``length`` the edge lengths and
    ``z`` the per-edge elevations (NaN = no DEM data).  All arrays follow the
    cyclic loop order.  NaN elevations never form the thalweg and count as
    above-threshold edges during expansion.
    """
    rel_s = np.asarray(rel_s, dtype=float)
    length = np.asarray(length, dtype=float)
    z = np.asarray(z, dtype=float)
    n = len(rel_s)
    if not (len(length) == n == len(z)) or n < 3:
        raise RiverSectionSelectionError("profile_shape", "rel_s, length and z must have the same length >= 3")
    window = np.nonzero(np.abs(rel_s) < rule.window_m)[0]
    if window.size == 0:
        raise RiverSectionSelectionError("window_empty", "no boundary edge lies inside window_m of the anchor edge")
    near = window[np.abs(rel_s[window]) < rule.thalweg_search_m]
    if near.size == 0 or not np.isfinite(z[near]).any():
        raise RiverSectionSelectionError("thalweg_undefined", "no edge with valid DEM data inside thalweg_search_m of the anchor edge")
    if not np.isfinite(z[window]).any():
        raise RiverSectionSelectionError("window_no_dem", "no edge inside window_m has valid DEM data")
    thalweg = int(near[np.nanargmin(z[near])])
    z_thalweg = float(z[thalweg])
    z_bank = float(np.nanpercentile(z[window], rule.bank_percentile))
    threshold = max(z_bank - rule.bank_margin_m, z_thalweg + rule.thalweg_margin_m)

    def expand(step: int) -> tuple[int, int]:
        last_ok, offset, gap, i = thalweg, 0, 0.0, thalweg
        for steps in range(1, n):
            i = (i + step) % n
            if abs(rel_s[i]) >= rule.window_m:
                break
            if z[i] < threshold:
                last_ok, offset, gap = i, steps, 0.0
            else:
                gap += length[i]
                if gap > rule.gap_tolerance_m:
                    break
        return last_ok, offset

    lo, offset_lo = expand(-1)
    hi, offset_hi = expand(+1)
    count = offset_lo + offset_hi + 1
    if count > n:
        raise RiverSectionSelectionError("run_covers_loop", "selected run would cover the whole boundary loop; reduce window_m")
    run = np.asarray([(lo + k) % n for k in range(count)], dtype=np.int64)
    return ChannelRunSelection(window, thalweg, z_thalweg, z_bank, float(threshold), int(lo), int(hi), run)


# --------------------------------------------------------------------------- driver


def _json_number(value: float) -> float | None:
    value = float(value)
    return value if math.isfinite(value) else None


def select_river_sections(request: RiverSectionsRequest, mesh: TriangularMesh, dem_path: str | Path, *,
                          mesh_artifact: str | None = None, mesh_artifact_sha256: str | None = None,
                          expected_dem_sha256: str | None = None) -> dict[str, Any]:
    """Select exterior edge runs for every requested section and return the audit document."""
    if not _crs_equivalent_text(request.crs, mesh.coordinate_system):
        raise RiverSectionSelectionError("crs_mismatch", f"request crs {request.crs!r} does not match mesh coordinate_system {mesh.coordinate_system!r}")
    dem_path = Path(dem_path)
    if not dem_path.is_file():
        raise RiverSectionSelectionError("dem_missing", f"DEM file not found: {dem_path}")
    expected = None
    for candidate, label in ((request.dem_sha256, "request.dem_sha256"), (expected_dem_sha256, "expected_dem_sha256")):
        if candidate is None:
            continue
        candidate = _sha256_text(candidate, label)
        if expected is not None and candidate != expected:
            raise RiverSectionSelectionError("dem_sha256_mismatch", "request.dem_sha256 and expected_dem_sha256 disagree")
        expected = candidate
    dem_sha256 = _file_sha256(dem_path)
    if expected is not None and dem_sha256 != expected:
        raise RiverSectionSelectionError("dem_sha256_mismatch", f"DEM sha256 {dem_sha256} differs from expected {expected}")

    loop = outer_boundary_loop(mesh)
    rule = request.rule
    if 2.0 * rule.window_m >= loop.perimeter_m:
        raise RiverSectionSelectionError("parameter_range", f"rule.window_m={rule.window_m:g} must be below half the outer loop perimeter ({loop.perimeter_m:.1f} m)")
    xy = np.asarray(mesh.node_xy, dtype=float)
    n = len(loop.edge_indices)

    anchors: list[tuple[int, float, np.ndarray, np.ndarray]] = []
    needed = np.zeros(n, dtype=bool)
    for section in request.sections:
        distance = np.hypot(loop.midpoint[:, 0] - section.xy[0], loop.midpoint[:, 1] - section.xy[1])
        anchor = int(distance.argmin())
        if distance[anchor] > request.max_point_distance_m:
            raise RiverSectionSelectionError("point_far_from_boundary", f"section {section.name!r} point is {distance[anchor]:.1f} m from the nearest outer boundary edge midpoint (max_point_distance_m={request.max_point_distance_m:g})")
        rel = loop.relative_arclength(anchor)
        window = np.nonzero(np.abs(rel) < rule.window_m)[0]
        needed[window] = True
        anchors.append((anchor, float(distance[anchor]), rel, window))

    needed_idx = np.nonzero(needed)[0]
    z_mean = np.full(n, np.nan)
    z_min = np.full(n, np.nan)
    nodata_count = np.zeros(n, dtype=np.int64)
    start = xy[loop.oriented_nodes[needed_idx, 0]]
    end = xy[loop.oriented_nodes[needed_idx, 1]]
    z_mean[needed_idx], z_min[needed_idx], nodata_count[needed_idx], dem_info = sample_edge_elevations(dem_path, start, end, rule.sample_fractions)
    if dem_info["crs"] is None:
        raise RiverSectionSelectionError("dem_crs_missing", "DEM has no CRS; cannot confirm it matches the mesh CRS")
    if not _crs_equivalent_text(request.crs, dem_info["crs"]):
        raise RiverSectionSelectionError("dem_crs_mismatch", f"DEM CRS does not match request crs {request.crs!r}; no transformation is performed")

    edge_faces = np.asarray(mesh.edge_faces, dtype=np.int64)
    sections: dict[str, Any] = {}
    for section, (anchor, anchor_distance, rel, window) in zip(request.sections, anchors):
        try:
            run = select_channel_run(rel, loop.length, z_mean, rule)
        except RiverSectionSelectionError as exc:
            raise RiverSectionSelectionError(exc.code, f"section {section.name!r}: {exc.message}") from exc
        idx = run.run_indices
        global_edges = loop.edge_indices[idx]
        if not all(edge_faces[e, 0] == -1 or edge_faces[e, 1] == -1 for e in global_edges):
            raise RiverSectionSelectionError("internal", "non-exterior edge selected")
        z_run = z_mean[idx]
        valid = np.isfinite(z_run)
        sections[section.name] = {
            "role": section.role,
            "name_zh": section.name_zh,
            "discharge_series_column": section.discharge_series_column,
            "osm_intersection_xy": [section.xy[0], section.xy[1]],
            "nearest_edge_distance_m": anchor_distance,
            "thalweg_elevation_m": run.thalweg_elevation_m,
            "bank_level_p75_m": run.bank_level_m,
            "threshold_m": run.threshold_m,
            "edge_count": run.edge_count,
            "width_m": float(loop.length[idx].sum()),
            "mean_edge_elevation_m": _json_number(np.average(z_run[valid], weights=loop.length[idx][valid])) if valid.any() else None,
            "min_edge_elevation_m": _json_number(np.nanmin(z_min[idx])) if np.isfinite(z_min[idx]).any() else None,
            "max_edge_elevation_m": _json_number(np.nanmax(z_run)) if valid.any() else None,
            "edges_above_threshold_included": int((~(z_run < run.threshold_m)).sum()),
            "window_edge_count": int(window.size),
            "nodata_edges_in_window": int((~np.isfinite(z_mean[window])).sum()),
            "edge_indices": [int(e) for e in global_edges],
            "edge_nodes": [sorted(int(v) for v in loop.oriented_nodes[k]) for k in idx],
            "profile": [{"s_m": float(rel[k]), "length_m": float(loop.length[k]), "z_mean_m": _json_number(z_mean[k]),
                         "z_min_m": _json_number(z_min[k]), "mid_xy": [float(loop.midpoint[k, 0]), float(loop.midpoint[k, 1])]} for k in idx],
            "window_profile": [{"s_m": float(rel[k]), "length_m": float(loop.length[k]), "z_mean_m": _json_number(z_mean[k])} for k in window],
        }
    return {
        "format": AUDIT_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "mesh_id": mesh.mesh_id,
        "mesh_artifact": mesh_artifact,
        "mesh_artifact_sha256": mesh_artifact_sha256,
        "dem": str(dem_path).replace("\\", "/"),
        "dem_sha256": dem_sha256,
        "dem_crs": dem_info["crs"],
        "crs": request.crs,
        "section_status": SECTION_STATUS,
        "max_point_distance_m": request.max_point_distance_m,
        "rule": rule.audit_record(),
        "outer_loop": {"edge_count": int(n), "perimeter_m": loop.perimeter_m, "exterior_loop_edge_counts": list(loop.loop_lengths)},
        "sections": sections,
    }


def write_audit(audit: Mapping[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(audit, indent=1, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return path


def write_section_figure(audit: Mapping[str, Any], mesh: TriangularMesh, path: str | Path) -> Path:
    """Overlay of window edges, the selected run and the requested point, one panel per section."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RiverSectionSelectionError("figure_dependency", "writing a figure requires optional dependency 'matplotlib'") from exc
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    xy = np.asarray(mesh.node_xy, dtype=float)
    edge_nodes = np.asarray(mesh.edge_nodes, dtype=np.int64)
    loop = outer_boundary_loop(mesh)
    sections = audit["sections"]
    fig, axes = plt.subplots(1, len(sections), figsize=(6 * len(sections), 6), squeeze=False)
    for ax, (name, section) in zip(axes[0], sections.items()):
        px, py = section["osm_intersection_xy"]
        anchor = int(np.hypot(loop.midpoint[:, 0] - px, loop.midpoint[:, 1] - py).argmin())
        rel = loop.relative_arclength(anchor)
        for k in np.nonzero(np.abs(rel) < audit["rule"]["window_m"])[0]:
            a, b = xy[loop.oriented_nodes[k]]
            ax.plot([a[0], b[0]], [a[1], b[1]], color="#9aa0a6", lw=1.0)
        for e in section["edge_indices"]:
            a, b = xy[edge_nodes[e]]
            ax.plot([a[0], b[0]], [a[1], b[1]], color="#1f6feb", lw=3.0, solid_capstyle="round")
        ax.plot(px, py, marker="*", color="#d1242f", ms=14, ls="none")
        ax.set_aspect("equal")
        ax.set_title(f"{name} {section['name_zh']} ({section['role']})\nwidth {section['width_m']:.0f} m, {section['edge_count']} edges, {SECTION_STATUS}", fontsize=9)
        ax.set_xlabel("x (m)")
        ax.set_ylabel("y (m)")
        ax.grid(True, color="#e6e8eb", lw=0.5)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=80, bbox_inches="tight")
    plt.close(fig)
    return path


def run_selection(request_path: str | Path, mesh_path: str | Path, dem_path: str | Path, out_path: str | Path, *,
                  figure_path: str | Path | None = None, expected_dem_sha256: str | None = None) -> dict[str, Any]:
    request = load_request(request_path)
    mesh, mesh_sha = load_mesh_artifact(mesh_path)
    audit = select_river_sections(request, mesh, dem_path, mesh_artifact=str(mesh_path).replace("\\", "/"),
                                  mesh_artifact_sha256=mesh_sha, expected_dem_sha256=expected_dem_sha256)
    write_audit(audit, out_path)
    if figure_path is not None:
        write_section_figure(audit, mesh, figure_path)
    return audit


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hydro_pipeline.river_section_selection",
                                     description="Select exterior mesh edge runs for assumed river sections by a DEM main-channel rule.")
    parser.add_argument("request", help=f"{REQUEST_FORMAT} JSON file")
    parser.add_argument("--mesh", required=True, help=f"{MESH_ARTIFACT_FORMAT} JSON artifact")
    parser.add_argument("--dem", required=True, help="DEM GeoTIFF in the mesh CRS (read as raw values)")
    parser.add_argument("--out", required=True, help=f"output {AUDIT_FORMAT} JSON path")
    parser.add_argument("--figure", default=None, help="optional PNG overlay (requires matplotlib)")
    parser.add_argument("--expected-dem-sha256", default=None, help="optional SHA-256 the DEM file must match")
    args = parser.parse_args(argv)
    try:
        audit = run_selection(args.request, args.mesh, args.dem, args.out, figure_path=args.figure,
                              expected_dem_sha256=args.expected_dem_sha256)
    except RiverSectionSelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for name, section in audit["sections"].items():
        print(f"{name}: {section['role']} thalweg {section['thalweg_elevation_m']:.2f} m bank {section['bank_level_p75_m']:.2f} m "
              f"threshold {section['threshold_m']:.2f} m edges {section['edge_count']} width {section['width_m']:.1f} m")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
