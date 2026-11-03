"""Streaming frame sink for full-size native triangular runs (large-run profile).

Solver side contract (plain numpy, no project types required)::

    sink = NativeUgridNetcdfSink(bundle_dir / "result_native.nc", profile)
    sink.open(mesh, metadata)             # mesh: SinkMesh or mapping; metadata: LargeRunMetadata or mapping
    for index, time_s, arrays in saved_frames:
        sink.write_frame(index, time_s, arrays, ledger=None)
    report = sink.close(maxima, ledger)   # or sink.abort("reason") on failure

``arrays`` per saved frame (each a 1-D float array of length n_face, face order
of ``mesh.face_id``):

* ``water_depth_m`` (required, >= 0)
* one or both component pairs: ``unit_discharge_x_m2_s``/``unit_discharge_y_m2_s``
  (aliases ``momentum_x_m2_s``/``momentum_y_m2_s``, ``qx``/``qy``) and/or
  ``velocity_x_m_s``/``velocity_y_m_s`` (aliases ``u``/``v``)
* ``water_level_m`` (optional; derived as bed + depth when omitted)

``ledger`` per frame (optional): finite scalars such as ``volume_m3``,
``rain_depth_mm``, ``inflow_m3_s``/``outflow_m3_s`` (totals, rate at the saved
time) or ``inflow_volume_m3``/``outflow_volume_m3`` (preceding interval), and
optionally one nested level per boundary group, e.g.
``{"groups": {"daqinghe_west": {"inflow_volume_m3": 1.2e5}}}`` (flattened to
``groups__daqinghe_west__inflow_volume_m3``). Keys are ``[A-Za-z0-9_]+``.

``close(maxima, ledger)``: ``maxima`` optional per-face arrays
``max_water_depth_m``, ``max_speed_m_s``, ``max_water_level_m``,
``time_of_max_water_depth_s``, ``arrival_time_s`` (NaN = never wet); they may be
internal-step maxima and must be >= the saved-frame maxima the sink tracks
itself. ``ledger`` optional mapping of scalars, strings, per-frame sequences
and nested mappings (e.g. per boundary group inflow/outflow volumes and outflow
shortfall). Series become ``mass_budget_<flat_key>`` (time) variables, numeric
scalars ``ledger_<flat_key>`` global attributes, and the complete ledger is kept
verbatim in the string variable ``mass_ledger_json``.

The sink never holds more than one frame: each frame is appended to an
unlimited ``time`` dimension in a NETCDF4 UGRID file and the running
saved-frame maxima/arrival are updated in place. The file is written as
``<name>.partial`` and atomically linked to its final name only on a
successful ``close``. No solver equations live here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

import numpy as np

from .large_run_profile import LargeRunMetadata, LargeRunOutputProfile, parse_metadata


NATIVE_LARGE_FORMAT = "native-triangle-ugrid-large-result"
NATIVE_LARGE_VERSION = "1.0"
NATIVE_LARGE_PRODUCT = "native_triangular_simulation_result_large"

_ALIASES = {
    "water_depth_m": ("water_depth_m", "h", "water_depth"),
    "unit_discharge_x_m2_s": ("unit_discharge_x_m2_s", "momentum_x_m2_s", "qx"),
    "unit_discharge_y_m2_s": ("unit_discharge_y_m2_s", "momentum_y_m2_s", "qy"),
    "velocity_x_m_s": ("velocity_x_m_s", "u"),
    "velocity_y_m_s": ("velocity_y_m_s", "v"),
    "water_level_m": ("water_level_m", "eta_m", "water_level"),
}
_MAXIMA_KEYS = ("max_water_depth_m", "max_speed_m_s", "max_water_level_m", "time_of_max_water_depth_s", "arrival_time_s")


class FrameSinkError(ValueError):
    """Invalid sink call order, mesh, frame array or ledger."""


@runtime_checkable
class FrameSink(Protocol):
    def open(self, mesh: Any, metadata: Any) -> None: ...
    def write_frame(self, index: int, time_s: float, arrays: Mapping[str, Any], ledger: Mapping[str, float] | None = None) -> None: ...
    def close(self, maxima: Mapping[str, Any] | None = None, ledger: Mapping[str, Any] | None = None) -> dict[str, Any]: ...
    def abort(self, reason: str) -> None: ...


@dataclass(frozen=True)
class SinkMesh:
    """Native triangle mesh in a projected metre CRS. Face order is authoritative."""

    node_xy_m: np.ndarray            # (n_node, 2) float64
    face_nodes: np.ndarray           # (n_face, 3) int, 0-based
    face_id: np.ndarray              # (n_face,) int64, unique, >= 0
    bed_elevation_m: np.ndarray      # (n_face,) float64
    face_area_m2: np.ndarray | None = None  # verified against geometry if given

    @classmethod
    def coerce(cls, value: Any, *, max_faces: int, max_nodes: int) -> "SinkMesh":
        if isinstance(value, Mapping):
            get = value.get
        else:
            get = lambda key, default=None: getattr(value, key, default)
        try:
            xy = np.ascontiguousarray(np.asarray(get("node_xy_m", get("nodes_xy")), dtype=np.float64))
            faces = np.ascontiguousarray(np.asarray(get("face_nodes", get("faces")), dtype=np.int64))
        except (TypeError, ValueError) as exc:
            raise FrameSinkError(f"mesh node_xy_m/face_nodes are not numeric arrays: {exc}") from exc
        if xy.ndim != 2 or xy.shape[1] != 2 or xy.shape[0] < 3 or not np.isfinite(xy).all():
            raise FrameSinkError("mesh node_xy_m must be a finite (n_node>=3, 2) array")
        if faces.ndim != 2 or faces.shape[1] != 3 or faces.shape[0] < 1:
            raise FrameSinkError("mesh face_nodes must be an (n_face>=1, 3) triangle array")
        n_node, n_face = xy.shape[0], faces.shape[0]
        if n_face > max_faces or n_node > max_nodes:
            raise FrameSinkError(f"mesh size {n_face} faces/{n_node} nodes exceeds the profile caps {max_faces}/{max_nodes}")
        if faces.min() < 0 or faces.max() >= n_node:
            raise FrameSinkError("mesh face_nodes reference a node outside 0..n_node-1")
        if np.any((faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2]) | (faces[:, 0] == faces[:, 2])):
            raise FrameSinkError("every mesh face needs three distinct nodes")
        raw_ids = get("face_id")
        ids = np.arange(1, n_face + 1, dtype=np.int64) if raw_ids is None else np.asarray(raw_ids)
        if ids.shape != (n_face,) or not np.issubdtype(ids.dtype, np.integer):
            raise FrameSinkError("mesh face_id must be an integer array of length n_face")
        ids = ids.astype(np.int64)
        if ids.min() < 0 or np.unique(ids).size != n_face:
            raise FrameSinkError("mesh face_id values must be unique non-negative integers")
        bed = np.asarray(get("bed_elevation_m"), dtype=np.float64) if get("bed_elevation_m") is not None else None
        if bed is None or bed.shape != (n_face,) or not np.isfinite(bed).all():
            raise FrameSinkError("mesh bed_elevation_m must be a finite per-face array (m)")
        p0, p1, p2 = xy[faces[:, 0]], xy[faces[:, 1]], xy[faces[:, 2]]
        area = 0.5 * np.abs((p1[:, 0] - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) - (p2[:, 0] - p0[:, 0]) * (p1[:, 1] - p0[:, 1]))
        if not np.all(area > 0):
            raise FrameSinkError(f"{int(np.count_nonzero(~(area > 0)))} mesh faces have zero/invalid planar area")
        given = get("face_area_m2")
        if given is not None:
            given = np.asarray(given, dtype=np.float64)
            if given.shape != (n_face,) or not np.allclose(given, area, rtol=1e-9, atol=1e-9):
                raise FrameSinkError("mesh face_area_m2 differs from the planar triangle geometry")
            area = given
        return cls(xy, faces, ids, bed, area)

    @property
    def n_face(self) -> int:
        return int(self.face_nodes.shape[0])

    @property
    def n_node(self) -> int:
        return int(self.node_xy_m.shape[0])


def _pick(arrays: Mapping[str, Any], canonical: str) -> Any:
    found = [key for key in _ALIASES[canonical] if key in arrays]
    if len(found) > 1:
        raise FrameSinkError(f"frame arrays give {canonical} twice via aliases {found}")
    return arrays[found[0]] if found else None


def _face_array(value: Any, name: str, n_face: int, *, allow_nan: bool = False) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (n_face,):
        raise FrameSinkError(f"{name} must have shape ({n_face},), got {array.shape}")
    if not allow_nan and not np.isfinite(array).all():
        raise FrameSinkError(f"{name} contains non-finite values")
    return array


_KEY_OK = __import__("re").compile(r"^[A-Za-z0-9_]+$")


def _flatten_ledger(value: Mapping[str, Any], *, per_frame: int | None, label: str, prefix: str = "") -> dict[str, Any]:
    """Flatten a (possibly one/two-level nested, e.g. per boundary group) ledger.

    Returns flat_key -> float | np.ndarray(series) | str. Nested keys are
    joined with ``__``. ``per_frame`` given means scalars only (one frame).
    Series must have exactly ``count`` values (checked by the caller).
    """
    out: dict[str, Any] = {}
    if not isinstance(value, Mapping):
        raise FrameSinkError(f"{label} must be a mapping")
    for key, item in value.items():
        if not isinstance(key, str) or not _KEY_OK.match(key) or "__" in key:
            raise FrameSinkError(f"{label} key {key!r} must match [A-Za-z0-9_]+ without '__'")
        flat = f"{prefix}__{key}" if prefix else key
        if flat.count("__") > 2:
            raise FrameSinkError(f"{label} nesting is limited to three levels ({flat})")
        if isinstance(item, Mapping):
            out.update(_flatten_ledger(item, per_frame=per_frame, label=label, prefix=flat))
        elif isinstance(item, str):
            if per_frame is not None:
                raise FrameSinkError(f"per-frame {label} {flat} must be numeric")
            out[flat] = item
        elif isinstance(item, (list, tuple, np.ndarray)):
            if per_frame is not None:
                raise FrameSinkError(f"per-frame {label} {flat} must be a scalar")
            series = np.asarray(item, dtype=np.float64)
            if series.ndim != 1 or not np.isfinite(series).all():
                raise FrameSinkError(f"{label} series {flat} must be a finite 1-D sequence")
            out[flat] = series
        elif isinstance(item, bool) or not isinstance(item, (int, float, np.floating, np.integer)) or not math.isfinite(float(item)):
            raise FrameSinkError(f"{label} {flat} must be a finite number, string, series or mapping")
        else:
            out[flat] = float(item)
    return out


def _ledger_units(key: str) -> str:
    leaf = key.rsplit("__", 1)[-1]
    for suffix, unit in (("_m3_s", "m3 s-1"), ("_m3", "m3"), ("_mm", "mm"), ("_m2", "m2"), ("_s", "s"), ("_m", "m")):
        if leaf.endswith(suffix):
            return unit
    return "1"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_jsonable(v) for v in (value.tolist() if isinstance(value, np.ndarray) else value)]
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    return value


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def estimate_bundle_bytes(n_face: int, n_node: int, n_frame: int, grid_cells: int, profile: LargeRunOutputProfile) -> int:
    """Uncompressed upper-bound estimate for the native NC + all derived outputs (bytes)."""
    value_bytes = 4 if profile.native_web_value_dtype == "float32" else 8
    native_nc = n_face * n_frame * (3 * 8 + 2 * 4) + n_node * 16 + n_face * 64
    native_web = n_face * n_frame * 5 * value_bytes + n_face * 60 + n_node * 16
    compat_nc = grid_cells * n_frame * 6 * 4
    nc2web = grid_cells * n_frame * 4 * 4
    stepjson = grid_cells * n_frame * 28 + (n_face * n_frame * 30 if profile.write_stepjson_native else 0)
    shp = 4 * n_face * 420
    return int(native_nc + native_web + compat_nc + nc2web + stepjson + shp)


def display_grid_extent(xy: np.ndarray, profile: LargeRunOutputProfile) -> dict[str, Any]:
    """Anchored derived display grid covering the mesh bounds (see large_run_output_profile.md)."""
    size = float(profile.display_grid_cell_size_m)
    ax, ay = float(profile.display_grid_anchor_x_m), float(profile.display_grid_anchor_y_m)
    xmin, ymin = float(xy[:, 0].min()), float(xy[:, 1].min())
    xmax, ymax = float(xy[:, 0].max()), float(xy[:, 1].max())
    i0, j0 = math.floor((xmin - ax) / size), math.floor((ymin - ay) / size)
    i1, j1 = math.ceil((xmax - ax) / size), math.ceil((ymax - ay) / size)
    nx, ny = max(1, i1 - i0), max(1, j1 - j0)
    return {"i0": i0, "j0": j0, "i1": i0 + nx, "j1": j0 + ny, "nx": nx, "ny": ny,
            "x0": ax + size * i0, "y0": ay + size * j0, "dx": size, "dy": size,
            "anchor": [ax, ay], "mesh_bounds": [xmin, ymin, xmax, ymax]}


class NativeUgridNetcdfSink:
    """Append-only NETCDF4 UGRID writer; one saved frame in memory at a time."""

    def __init__(self, path: str | Path, profile: LargeRunOutputProfile | None = None):
        self.path = Path(path).resolve()
        self.profile = profile or LargeRunOutputProfile()
        self.partial = self.path.with_name(self.path.name + ".partial")
        self._ds = None
        self._state = "new"
        self.mesh: SinkMesh | None = None
        self.metadata: LargeRunMetadata | None = None
        self._next = 0
        self._last_time = -math.inf
        self._ledger_keys: tuple[str, ...] | None = None

    # ------------------------------------------------------------------ open
    def open(self, mesh: Any, metadata: Any) -> None:
        if self._state != "new":
            raise FrameSinkError("sink.open may be called once")
        profile = self.profile
        meta = parse_metadata(metadata)
        mesh_value = SinkMesh.coerce(mesh, max_faces=profile.max_face_count, max_nodes=profile.max_node_count)
        if self.path.exists() or self.partial.exists():
            raise FrameSinkError(f"native result target exists; no-clobber refused: {self.path}")
        if not self.path.parent.is_dir():
            raise FrameSinkError(f"native result parent directory is missing: {self.path.parent}")
        try:
            from pyproj import CRS
            crs = CRS.from_user_input(meta.coordinate_system)
        except ImportError as exc:
            raise FrameSinkError("large-run sink requires pyproj to verify the projected CRS") from exc
        except Exception as exc:
            raise FrameSinkError(f"coordinate_system cannot be resolved: {exc}") from exc
        if not crs.is_projected or any(abs(float(axis.unit_conversion_factor) - 1.0) > 1e-12 for axis in crs.axis_info[:2]):
            raise FrameSinkError("coordinate_system must be a projected CRS in metres")
        if meta.expected_times_s is not None:
            if len(meta.expected_times_s) > profile.max_frame_count:
                raise FrameSinkError("expected_times_s exceeds max_frame_count")
            if profile.disk_preflight:
                grid = display_grid_extent(mesh_value.node_xy_m, profile)
                need = estimate_bundle_bytes(mesh_value.n_face, mesh_value.n_node, len(meta.expected_times_s),
                                             grid["nx"] * grid["ny"], profile) * float(profile.disk_safety_factor)
                free = shutil.disk_usage(self.path.parent).free
                if free < need:
                    raise FrameSinkError(f"disk preflight failed: need about {int(need)} bytes, {free} free")
        try:
            import netCDF4
        except ImportError as exc:
            raise FrameSinkError("large-run sink requires netCDF4") from exc
        self.mesh, self.metadata = mesh_value, meta
        ds = netCDF4.Dataset(str(self.partial), "w", format="NETCDF4")
        self._ds = ds
        try:
            self._define(ds, crs)
        except BaseException:
            ds.close(); self.partial.unlink(missing_ok=True); raise
        self._state = "open"
        # running saved-frame statistics (n_face vectors only)
        n = mesh_value.n_face
        self._max_h = np.full(n, -np.inf); self._max_h_t = np.full(n, np.nan)
        self._max_speed = np.full(n, -np.inf); self._arrival = np.full(n, np.nan)
        self._max_level = np.full(n, -np.inf)

    def _define(self, ds: Any, crs: Any) -> None:
        mesh, meta, profile = self.mesh, self.metadata, self.profile
        ds.setncattr("format", NATIVE_LARGE_FORMAT)
        ds.setncattr("result_schema_version", NATIVE_LARGE_VERSION)
        ds.setncattr("product_type", NATIVE_LARGE_PRODUCT)
        ds.setncattr("status", "INCOMPLETE")
        ds.setncattr("simulation_result", np.int8(1 if meta.live_solver_ran else 0))
        ds.setncattr("data_origin", meta.data_origin)
        ds.setncattr("live_solver_ran", np.int8(meta.live_solver_ran))
        ds.setncattr("production_approved", np.int8(meta.production_approved))
        ds.setncattr("run_id", meta.run_id); ds.setncattr("case_id", meta.case_id)
        ds.setncattr("coordinate_system", meta.coordinate_system)
        ds.setncattr("vertical_datum", meta.vertical_datum or "UNSPECIFIED_NATIVE_SOURCE_DATUM")
        ds.setncattr("time_origin", meta.time_origin)
        ds.setncattr("dry_depth_threshold_m", float(meta.dry_depth_threshold_m))
        ds.setncattr("forcing_scope", meta.forcing.scope)
        ds.setncattr("boundary_policy", meta.forcing.boundary_policy)
        ds.setncattr("river_inflow_outflow_included", np.int8(meta.forcing.river_inflow_outflow_included))
        ds.setncattr("rainfall_included", np.int8(meta.forcing.rainfall_included))
        ds.setncattr("river_section_status", meta.forcing.river_section_status)
        ds.setncattr("discharge_series_status", meta.forcing.discharge_series_status)
        ds.setncattr("boundary_groups_json", json.dumps([g.model_dump(mode="json") for g in meta.forcing.boundary_groups], ensure_ascii=False, separators=(",", ":")))
        ds.setncattr("forcing_notice", meta.forcing.notice)
        ds.setncattr("run_metadata_json", json.dumps(meta.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        ds.setncattr("output_profile_json", json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
        ds.setncattr("maximum_semantics", "saved_frame_* = maximum over saved output frames only; solver_* = values handed over by the solver at close (may include internal steps)")
        ds.createDimension("time", None)
        ds.createDimension("face", mesh.n_face)
        ds.createDimension("node", mesh.n_node)
        ds.createDimension("max_face_nodes", 3)
        topo = ds.createVariable("mesh2d", "i4"); topo.assignValue(0)
        topo.cf_role = "mesh_topology"; topo.topology_dimension = np.int32(2)
        topo.node_coordinates = "node_x node_y"; topo.face_coordinates = "face_x face_y"
        topo.face_node_connectivity = "face_nodes"; topo.face_dimension = "face"
        cv = ds.createVariable("crs", "i4"); cv.assignValue(0)
        cv.coordinate_system = meta.coordinate_system
        wkt = crs.to_wkt(version="WKT2_2019"); cv.crs_wkt = wkt; cv.spatial_ref = wkt
        try:
            for key, value in crs.to_cf().items():
                if key not in {"crs_wkt", "spatial_ref"} and value is not None:
                    setattr(cv, key, value)
        except Exception:
            pass
        for name, axis, col in (("node_x", "X", 0), ("node_y", "Y", 1)):
            var = ds.createVariable(name, "f8", ("node",)); var[:] = mesh.node_xy_m[:, col]
            var.units = "m"; var.axis = axis; var.grid_mapping = "crs"
            var.standard_name = "projection_x_coordinate" if axis == "X" else "projection_y_coordinate"
        fn = ds.createVariable("face_nodes", "i4", ("face", "max_face_nodes"), zlib=True, complevel=1)
        fn[:] = mesh.face_nodes.astype(np.int32); fn.cf_role = "face_node_connectivity"; fn.start_index = np.int32(0)
        fid = ds.createVariable("face_id", "i8", ("face",)); fid[:] = mesh.face_id; fid.cf_role = "face_id"
        area = ds.createVariable("face_area", "f8", ("face",)); area[:] = mesh.face_area_m2
        area.units = "m2"; area.mesh = "mesh2d"; area.location = "face"; area.grid_mapping = "crs"
        centroid = mesh.node_xy_m[mesh.face_nodes].mean(axis=1)
        for name, axis, col in (("face_x", "X", 0), ("face_y", "Y", 1)):
            var = ds.createVariable(name, "f8", ("face",)); var[:] = centroid[:, col]
            var.units = "m"; var.axis = axis; var.grid_mapping = "crs"; var.mesh = "mesh2d"; var.location = "face"
        bed = ds.createVariable("bed_elevation", "f8", ("face",)); bed[:] = mesh.bed_elevation_m
        bed.units = "m"; bed.mesh = "mesh2d"; bed.location = "face"; bed.grid_mapping = "crs"
        tv = ds.createVariable("time", "f8", ("time",))
        tv.units = f"seconds since {meta.time_origin}"; tv.standard_name = "time"; tv.axis = "T"
        tv.calendar = "proleptic_gregorian"; tv.time_origin = meta.time_origin
        chunk = (1, min(int(profile.native_nc_face_chunk), mesh.n_face))
        level = int(profile.native_nc_complevel)
        comp = {"zlib": level > 0, "complevel": level if level else 1, "shuffle": level > 0, "chunksizes": chunk}
        specs = (
            ("water_depth", "f8", "m", "water depth"),
            ("momentum_x", "f8", "m2 s-1", "x unit discharge (momentum per unit width)"),
            ("momentum_y", "f8", "m2 s-1", "y unit discharge (momentum per unit width)"),
            ("velocity_x", "f4", "m s-1", "x depth-averaged velocity"),
            ("velocity_y", "f4", "m s-1", "y depth-averaged velocity"),
        )
        for name, dtype, units, long_name in specs:
            var = ds.createVariable(name, dtype, ("time", "face"), **comp)
            var.units = units; var.long_name = long_name; var.mesh = "mesh2d"; var.location = "face"; var.grid_mapping = "crs"
        for name, units in (("volume", "m3"), ("wet_area", "m2"), ("maximum_water_depth_by_time", "m"), ("maximum_speed_by_time", "m s-1")):
            var = ds.createVariable(name, "f8", ("time",)); var.units = units
        ds["volume"].long_name = "sum(water_depth * face_area) over all native faces"
        ds["wet_area"].long_name = "sum(face_area where water_depth > dry_depth_threshold_m)"
        self._level_var = None

    # ----------------------------------------------------------- write_frame
    def write_frame(self, index: int, time_s: float, arrays: Mapping[str, Any], ledger: Mapping[str, float] | None = None) -> None:
        if self._state != "open":
            raise FrameSinkError("write_frame requires an open sink")
        if type(index) not in (int, np.int32, np.int64) or int(index) != self._next:
            raise FrameSinkError(f"frame index must be contiguous from 0; expected {self._next}, got {index!r}")
        t = float(time_s)
        if not math.isfinite(t) or t <= self._last_time or (self._next == 0 and t < 0):
            raise FrameSinkError(f"frame time must be finite and strictly increasing; got {time_s!r}")
        meta, mesh = self.metadata, self.mesh
        if self._next >= self.profile.max_frame_count:
            raise FrameSinkError("frame count exceeds the profile max_frame_count")
        if meta.expected_times_s is not None:
            if self._next >= len(meta.expected_times_s) or t != float(meta.expected_times_s[self._next]):
                raise FrameSinkError(f"frame {index} time {t} differs from expected_times_s")
        if not isinstance(arrays, Mapping):
            raise FrameSinkError("frame arrays must be a mapping of name -> numpy array")
        known = {alias for names in _ALIASES.values() for alias in names}
        unknown = sorted(set(arrays) - known)
        if unknown:
            raise FrameSinkError(f"unknown frame array names: {unknown}")
        n = mesh.n_face
        raw_h = _pick(arrays, "water_depth_m")
        if raw_h is None:
            raise FrameSinkError("frame arrays require water_depth_m")
        h = _face_array(raw_h, "water_depth_m", n)
        if np.any(h < 0):
            raise FrameSinkError(f"frame {index} has negative water depth")
        qx, qy = _pick(arrays, "unit_discharge_x_m2_s"), _pick(arrays, "unit_discharge_y_m2_s")
        ux, uy = _pick(arrays, "velocity_x_m_s"), _pick(arrays, "velocity_y_m_s")
        if (qx is None) != (qy is None) or (ux is None) != (uy is None):
            raise FrameSinkError("vector components must be given as complete x/y pairs")
        if qx is None and ux is None:
            raise FrameSinkError("frame arrays require unit discharge and/or velocity components")
        threshold = float(meta.dry_depth_threshold_m)
        wet = h > threshold
        if qx is not None:
            qx = _face_array(qx, "unit_discharge_x_m2_s", n); qy = _face_array(qy, "unit_discharge_y_m2_s", n)
        if ux is not None:
            ux = _face_array(ux, "velocity_x_m_s", n); uy = _face_array(uy, "velocity_y_m_s", n)
        if qx is None:
            qx, qy = h * ux, h * uy
        if ux is None:
            ux = np.zeros(n); uy = np.zeros(n)
            np.divide(qx, h, out=ux, where=wet); np.divide(qy, h, out=uy, where=wet)
        level_raw = _pick(arrays, "water_level_m")
        level = None if level_raw is None else _face_array(level_raw, "water_level_m", n)
        ds = self._ds
        i = self._next
        ds["time"][i] = t
        ds["water_depth"][i, :] = h
        ds["momentum_x"][i, :] = qx
        ds["momentum_y"][i, :] = qy
        ds["velocity_x"][i, :] = ux.astype(np.float32)
        ds["velocity_y"][i, :] = uy.astype(np.float32)
        if level is not None or self._level_var is not None:
            if self._level_var is None:
                if i != 0:
                    raise FrameSinkError("water_level_m must be supplied from frame 0 onward or not at all")
                level_level = int(self.profile.native_nc_complevel)
                self._level_var = ds.createVariable("water_level", "f8", ("time", "face"), zlib=level_level > 0,
                    complevel=level_level or 1, shuffle=level_level > 0, chunksizes=(1, min(int(self.profile.native_nc_face_chunk), n)))
                self._level_var.units = "m"; self._level_var.mesh = "mesh2d"; self._level_var.location = "face"
                self._level_var.long_name = "solver water level"
            if level is None:
                raise FrameSinkError("water_level_m was supplied for earlier frames and must stay present")
            self._level_var[i, :] = level
        level_values = level if level is not None else mesh.bed_elevation_m + h
        speed = np.hypot(ux, uy)
        volume = float(np.dot(h, mesh.face_area_m2))
        ds["volume"][i] = volume
        ds["wet_area"][i] = float(mesh.face_area_m2[wet].sum())
        ds["maximum_water_depth_by_time"][i] = float(h.max())
        ds["maximum_speed_by_time"][i] = float(speed.max())
        ledger = _flatten_ledger(dict(ledger or {}), per_frame=i, label="frame ledger")
        keys = tuple(sorted(ledger))
        if self._ledger_keys is None:
            if i != 0 and keys:
                raise FrameSinkError("per-frame ledger must be supplied from frame 0 onward or not at all")
            self._ledger_keys = keys
            for key in keys:
                var = ds.createVariable(f"mass_budget_{key}", "f8", ("time",))
                var.units = _ledger_units(key); var.long_name = f"solver per-frame ledger {key}"
        elif keys != self._ledger_keys:
            raise FrameSinkError(f"per-frame ledger keys changed from {self._ledger_keys} to {keys}")
        for key in keys:
            ds[f"mass_budget_{key}"][i] = ledger[key]
        if "volume_m3" in ledger:
            reference = float(ledger["volume_m3"])
            if abs(volume - reference) > float(self.profile.volume_check_rel_tol) * max(1.0, abs(reference)):
                raise FrameSinkError(f"frame {index}: sum(h*A)={volume} differs from ledger volume_m3={reference}")
        better = h > self._max_h
        self._max_h_t[better] = t; self._max_h[better] = h[better]
        np.maximum(self._max_speed, speed, out=self._max_speed)
        np.maximum(self._max_level, level_values, out=self._max_level)
        arrived = np.isnan(self._arrival) & wet
        self._arrival[arrived] = t
        ds.sync()
        self._next += 1
        self._last_time = t

    # ----------------------------------------------------------------- close
    def close(self, maxima: Mapping[str, Any] | None = None, ledger: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self._state != "open":
            raise FrameSinkError("close requires an open sink")
        try:
            report = self._finish(maxima, ledger)
        except BaseException:
            self.abort("close validation failed")
            raise
        return report

    def _finish(self, maxima: Mapping[str, Any] | None, ledger: Mapping[str, Any] | None) -> dict[str, Any]:
        meta, mesh, ds = self.metadata, self.mesh, self._ds
        count = self._next
        if count == 0:
            raise FrameSinkError("no saved frame was written")
        if meta.expected_times_s is not None and count != len(meta.expected_times_s):
            raise FrameSinkError(f"only {count} of {len(meta.expected_times_s)} expected frames were written")
        n = mesh.n_face
        for name, values, units in (
            ("saved_frame_max_water_depth", self._max_h, "m"),
            ("saved_frame_max_water_depth_time", self._max_h_t, "s"),
            ("saved_frame_max_speed", self._max_speed, "m s-1"),
            ("saved_frame_max_water_level", self._max_level, "m"),
            ("saved_frame_arrival_time", self._arrival, "s"),
        ):
            var = ds.createVariable(name, "f8", ("face",), fill_value=np.nan); var[:] = values
            var.units = units; var.mesh = "mesh2d"; var.location = "face"
        ds["saved_frame_arrival_time"].long_name = "first saved frame time with depth > dry_depth_threshold_m; NaN never wet"
        provided = []
        for key, value in dict(maxima or {}).items():
            if key not in _MAXIMA_KEYS:
                raise FrameSinkError(f"unknown maxima key {key!r}; allowed {_MAXIMA_KEYS}")
            array = _face_array(value, key, n, allow_nan=key == "arrival_time_s")
            if key == "max_water_depth_m" and np.any(array < self._max_h - 1e-9):
                raise FrameSinkError("solver max_water_depth_m is below a saved-frame depth")
            if key == "max_speed_m_s" and np.any(array < self._max_speed - 1e-6 * np.maximum(1.0, self._max_speed)):
                raise FrameSinkError("solver max_speed_m_s is below a saved-frame speed")
            var = ds.createVariable("solver_" + key, "f8", ("face",), fill_value=np.nan)
            var[:] = array; var.mesh = "mesh2d"; var.location = "face"
            provided.append(key)
        flat = _flatten_ledger(dict(ledger or {}), per_frame=None, label="close ledger")
        scalars: dict[str, Any] = {}
        series_names: list[str] = []
        for key, item in sorted(flat.items()):
            if isinstance(item, np.ndarray):
                if item.shape != (count,):
                    raise FrameSinkError(f"ledger series {key} must have one value per saved frame ({count})")
                name = f"mass_budget_{key}"
                if name in ds.variables:
                    if not np.allclose(np.asarray(ds[name][:], dtype=np.float64), item, rtol=1e-12, atol=0.0):
                        raise FrameSinkError(f"ledger series {key} differs from the per-frame ledger values")
                else:
                    var = ds.createVariable(name, "f8", ("time",)); var[:] = item
                    var.units = _ledger_units(key); var.long_name = f"solver close ledger series {key}"
                series_names.append(name)
            else:
                scalars[key] = item
                if not isinstance(item, str):
                    ds.setncattr(f"ledger_{key}", float(item))
        full = json.dumps(_jsonable(dict(ledger or {})), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        lv = ds.createVariable("mass_ledger_json", str, ()); lv[...] = full
        lv.long_name = "complete solver close ledger (JSON, nested per boundary group as supplied)"
        ds.setncattr("mass_ledger_scalars_json", json.dumps(scalars, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        ds.setncattr("mass_ledger_series_variables", ",".join(series_names) or "none")
        ds.setncattr("solver_maxima_provided", ",".join(provided) or "none")
        ds.setncattr("saved_frame_count", np.int32(count))
        ds.setncattr("status", "COMPLETE")
        ds.close(); self._ds = None
        with self.partial.open("r+b") as stream:
            os.fsync(stream.fileno())
        if self.path.exists():
            raise FrameSinkError("native result target appeared during the run; refusing to overwrite")
        os.replace(self.partial, self.path)
        self._state = "closed"
        return {"path": str(self.path), "bytes": self.path.stat().st_size, "frame_count": count,
                "face_count": n, "node_count": mesh.n_node, "status": "COMPLETE"}

    def abort(self, reason: str) -> None:
        """Close the partial file, mark it FAILED and keep it for diagnostics."""
        if self._ds is not None:
            try:
                self._ds.setncattr("status", "FAILED")
                self._ds.setncattr("failure_reason", str(reason)[:2000])
            finally:
                self._ds.close(); self._ds = None
        if self.partial.exists():
            failed = self.path.with_name(self.path.name + ".failed")
            if not failed.exists():
                os.replace(self.partial, failed)
        self._state = "aborted"

    def __enter__(self) -> "NativeUgridNetcdfSink":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None and self._state == "open":
            self.abort(f"{exc_type.__name__}: {exc}")


@dataclass
class LargeNativeFrame:
    index: int
    time_s: float
    water_depth_m: np.ndarray
    momentum_x_m2_s: np.ndarray
    momentum_y_m2_s: np.ndarray
    velocity_x_m_s: np.ndarray
    velocity_y_m_s: np.ndarray
    water_level_m: np.ndarray
    volume_m3: float
    ledger: dict[str, float]


class LargeNativeResult:
    """Streaming reader for a COMPLETE large native UGRID result (one frame per read)."""

    def __init__(self, path: str | Path):
        import netCDF4
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise FrameSinkError(f"large native result is missing: {self.path}")
        self._ds = netCDF4.Dataset(str(self.path), "r")
        ds = self._ds
        ds.set_auto_mask(False)
        try:
            if getattr(ds, "format", None) != NATIVE_LARGE_FORMAT or getattr(ds, "result_schema_version", None) != NATIVE_LARGE_VERSION:
                raise FrameSinkError("not a native-triangle-ugrid-large-result v1.0 file")
            if getattr(ds, "status", None) != "COMPLETE":
                raise FrameSinkError(f"large native result status is {getattr(ds, 'status', None)!r}, not COMPLETE")
            self.metadata = parse_metadata(json.loads(ds.run_metadata_json))
            self.node_xy_m = np.column_stack([ds["node_x"][:], ds["node_y"][:]]).astype(np.float64)
            self.face_nodes = np.asarray(ds["face_nodes"][:], dtype=np.int64)
            self.face_id = np.asarray(ds["face_id"][:], dtype=np.int64)
            self.face_area_m2 = np.asarray(ds["face_area"][:], dtype=np.float64)
            self.bed_elevation_m = np.asarray(ds["bed_elevation"][:], dtype=np.float64)
            self.time_s = np.asarray(ds["time"][:], dtype=np.float64)
            if self.time_s.size != int(ds.saved_frame_count) or ds["water_depth"].shape != (self.time_s.size, self.face_id.size):
                raise FrameSinkError("large native result time/face dimensions are inconsistent")
            self.ledger_keys = tuple(sorted(name[len("mass_budget_"):] for name in ds.variables if name.startswith("mass_budget_")))
            self.has_level = "water_level" in ds.variables
            self.mass_ledger = json.loads(ds["mass_ledger_json"][...]) if "mass_ledger_json" in ds.variables else {}
        except BaseException:
            ds.close(); raise

    @property
    def n_face(self) -> int:
        return int(self.face_id.size)

    @property
    def n_frame(self) -> int:
        return int(self.time_s.size)

    def static(self, name: str) -> np.ndarray:
        return np.asarray(self._ds[name][:], dtype=np.float64)

    def frame(self, index: int) -> LargeNativeFrame:
        ds = self._ds
        h = np.asarray(ds["water_depth"][index, :], dtype=np.float64)
        level = (np.asarray(ds["water_level"][index, :], dtype=np.float64) if self.has_level else self.bed_elevation_m + h)
        return LargeNativeFrame(
            index, float(self.time_s[index]), h,
            np.asarray(ds["momentum_x"][index, :], dtype=np.float64), np.asarray(ds["momentum_y"][index, :], dtype=np.float64),
            np.asarray(ds["velocity_x"][index, :], dtype=np.float64), np.asarray(ds["velocity_y"][index, :], dtype=np.float64),
            level, float(ds["volume"][index]),
            {key: float(ds[f"mass_budget_{key}"][index]) for key in self.ledger_keys})

    def iter_frames(self) -> Iterator[LargeNativeFrame]:
        for index in range(self.n_frame):
            yield self.frame(index)

    def sha256(self) -> str:
        return _sha_file(self.path)

    def close(self) -> None:
        if self._ds is not None:
            self._ds.close(); self._ds = None

    def __enter__(self) -> "LargeNativeResult":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


__all__ = ["FrameSink", "FrameSinkError", "SinkMesh", "NativeUgridNetcdfSink", "LargeNativeResult", "LargeNativeFrame",
           "NATIVE_LARGE_FORMAT", "NATIVE_LARGE_VERSION", "display_grid_extent", "estimate_bundle_bytes"]
