"""Large-run (full-size) output bundle exporter -- opt-in, streaming.

Input: ``<bundle>/result_native.nc`` written by :class:`frame_sink.NativeUgridNetcdfSink`.
Output (same bundle directory, no-clobber)::

    result_native.nc                 authoritative native UGRID (from the sink)
    <case_id>.nc                     derived legacy (time,y,x) compatibility NC, 20 legacy variables + 2 coverage variables
    compatibility_grid.json          v2 grid identity/digest + references to binary sidecars
    compatibility_grid_coverage.bin  float32 coverage fraction + float64 source covered area (row-major)
    compatibility_grid_mapping.npz   sparse face/cell overlap table (cell, face index, overlap area m2)
    nc2web/                          legacy nc2web v1 (manifest.json, mesh.bin, frame_NNN.bin) -- existing codec
    stepjson/                        legacy stepjson v1 {"all":["id_depth_speed_direction",...]} -- existing codec
    stepjson_native/                 same record structure keyed by exact native face_id (additional)
    native_web/                      native-triangle-web 2.0: manifest.json, mesh.bin, frame_NNNNNN.bin (one file per frame)
    shp/{full_domain,full_domain_4490,maximum_wet_surface,maximum_wet_surface_4490}/faces.{shp,shx,dbf,prj,cpg}+manifest.json
    viewer.html, run.json, run_report.json, summary.json, parse_summary.json,
    mass_budget/, quality/, provenance/, logs/, checksums.json

Every frame is read from the native NC, written to all per-frame products and
released before the next frame. This module never runs a solver.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import time as _time
from typing import Any

import numpy as np

from .frame_sink import FrameSinkError, LargeNativeResult, display_grid_extent, estimate_bundle_bytes
from .large_run_profile import LargeRunOutputProfile, load_profile


BUNDLE_FORMAT = "large-run-output-bundle-v1"
GRID_FORMAT = "hydrodynamics-legacy-grid-compatibility-v2"
GRID_SCHEMA_VERSION = "2.0"
NATIVE_WEB_FORMAT = "native-triangle-web"
NATIVE_WEB_VERSION = "2.0"
NATIVE_MESH_MAGIC = b"NTW3M01\n"
NATIVE_FRAME_MAGIC_V2 = b"NTW3F02\n"
NATIVE_WEB_VARIABLES = (("water_depth_m", "m"), ("water_level_m", "m"), ("velocity_x_m_s", "m/s"),
                        ("velocity_y_m_s", "m/s"), ("speed_m_s", "m/s"))
SHP_FORMAT = "native-triangular-result-shapefile-large-v1"
DBF_DECIMALS = 12


class LargeRunExportError(RuntimeError):
    pass


# --------------------------------------------------------------------- utils
def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any, *, indent: int | None = 2) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=indent, allow_nan=False) + "\n").encode("utf-8")


def _write_new(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def _write_json(path: Path, value: Any) -> None:
    _write_new(path, _json_bytes(value))


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _face_id_digest(ids: np.ndarray) -> str:
    return hashlib.sha256("\n".join(str(int(v)) for v in ids).encode("ascii")).hexdigest()


def _finite_or_none(value: float) -> float | None:
    return float(value) if math.isfinite(float(value)) else None


# ------------------------------------------------- derived compatibility grid
def _clip_halfplane(poly: np.ndarray, count: np.ndarray, axis: int, bound: np.ndarray, keep_greater: bool) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised Sutherland-Hodgman clip of P convex polygons against one axis-aligned half-plane."""
    P, K, _ = poly.shape
    out = np.zeros((P, K + 1, 2)); out_count = np.zeros(P, dtype=np.int64)
    rows = np.arange(P); safe = np.maximum(count, 1)
    for k in range(K):
        active = k < count
        cur = poly[:, k]; nxt = poly[rows, (k + 1) % safe]
        cin = cur[:, axis] >= bound if keep_greater else cur[:, axis] <= bound
        nin = nxt[:, axis] >= bound if keep_greater else nxt[:, axis] <= bound
        emit = active & cin
        idx = rows[emit]; out[idx, out_count[idx]] = cur[idx]; out_count[idx] += 1
        cross = active & (cin != nin)
        idx = rows[cross]
        if idx.size:
            a, b = cur[idx], nxt[idx]
            t = (bound[idx] - a[:, axis]) / (b[:, axis] - a[:, axis])
            point = a + t[:, None] * (b - a); point[:, axis] = bound[idx]
            out[idx, out_count[idx]] = point; out_count[idx] += 1
    return out, out_count


def _polygon_area(poly: np.ndarray, count: np.ndarray) -> np.ndarray:
    P, K, _ = poly.shape
    rows = np.arange(P); safe = np.maximum(count, 1); total = np.zeros(P)
    for k in range(K):
        active = k < count
        cur = poly[:, k]; nxt = poly[rows, (k + 1) % safe]
        total += np.where(active, cur[:, 0] * nxt[:, 1] - nxt[:, 0] * cur[:, 1], 0.0)
    return 0.5 * np.abs(total)


class SparseCompatibilityGrid:
    """Exact planar triangle-cell overlap table for the anchored derived display grid."""

    def __init__(self, xy: np.ndarray, faces: np.ndarray, areas: np.ndarray, bed: np.ndarray,
                 coordinate_system: str, profile: LargeRunOutputProfile):
        ext = display_grid_extent(xy, profile)
        self.extent = ext
        nx, ny, dx, dy, x0, y0 = ext["nx"], ext["ny"], ext["dx"], ext["dy"], ext["x0"], ext["y0"]
        if nx * ny > profile.max_display_grid_cells:
            raise LargeRunExportError(f"display grid {nx}x{ny} exceeds max_display_grid_cells={profile.max_display_grid_cells}")
        self.nx, self.ny, self.dx, self.dy, self.x0, self.y0 = nx, ny, dx, dy, x0, y0
        tri = xy[faces]  # (F,3,2)
        c0 = np.clip(np.floor((tri[:, :, 0].min(1) - x0) / dx).astype(np.int64), 0, nx - 1)
        c1 = np.clip(np.floor((tri[:, :, 0].max(1) - x0) / dx).astype(np.int64), 0, nx - 1)
        r0 = np.clip(np.floor((tri[:, :, 1].min(1) - y0) / dy).astype(np.int64), 0, ny - 1)
        r1 = np.clip(np.floor((tri[:, :, 1].max(1) - y0) / dy).astype(np.int64), 0, ny - 1)
        width = c1 - c0 + 1; counts = width * (r1 - r0 + 1)
        cells_out, faces_out, area_out = [], [], []
        face_index = np.arange(faces.shape[0])
        chunk = 200_000
        starts = np.concatenate([[0], np.cumsum(counts)])
        total_pairs = int(starts[-1])
        # Pair (face, cell) candidates in bounded chunks of faces.
        face_start = 0
        while face_start < faces.shape[0]:
            face_end = int(np.searchsorted(starts, starts[face_start] + chunk, side="right")) - 1
            face_end = max(face_end, face_start + 1)
            sel = face_index[face_start:face_end]
            pair_face = np.repeat(sel, counts[sel])
            local = np.arange(pair_face.size) - np.repeat(starts[sel] - starts[face_start], counts[sel])
            col = c0[pair_face] + local % width[pair_face]
            row = r0[pair_face] + local // width[pair_face]
            ox = x0 + col * dx; oy = y0 + row * dy
            poly = np.zeros((pair_face.size, 3, 2))
            poly[:, :, 0] = tri[pair_face, :, 0] - ox[:, None]
            poly[:, :, 1] = tri[pair_face, :, 1] - oy[:, None]
            count = np.full(pair_face.size, 3, dtype=np.int64)
            zeros = np.zeros(pair_face.size); xs = np.full(pair_face.size, dx); ys = np.full(pair_face.size, dy)
            poly, count = _clip_halfplane(poly, count, 0, zeros, True)
            poly, count = _clip_halfplane(poly, count, 0, xs, False)
            poly, count = _clip_halfplane(poly, count, 1, zeros, True)
            poly, count = _clip_halfplane(poly, count, 1, ys, False)
            area = np.where(count >= 3, _polygon_area(poly, count), 0.0)
            keep = area > dx * dy * 1e-14
            cells_out.append((row * nx + col)[keep]); faces_out.append(pair_face[keep]); area_out.append(area[keep])
            face_start = face_end
        cell = np.concatenate(cells_out); face = np.concatenate(faces_out); area = np.concatenate(area_out)
        order = np.lexsort((face, cell))
        self.pair_cell, self.pair_face, self.pair_area = cell[order], face[order], area[order]
        self.candidate_pairs = total_pairs
        ncell = nx * ny
        covered = np.bincount(self.pair_cell, weights=self.pair_area, minlength=ncell)
        if np.any(covered > dx * dy * (1 + 1e-9)):
            raise LargeRunExportError("a display cell overlaps native faces beyond its own area (overlapping mesh faces?)")
        native_total = float(np.sum(areas)); covered_total = float(covered.sum())
        if not math.isclose(covered_total, native_total, rel_tol=1e-9, abs_tol=1e-6):
            raise LargeRunExportError(f"overlap area {covered_total} differs from native face area {native_total}")
        per_face = np.bincount(self.pair_face, weights=self.pair_area, minlength=faces.shape[0])
        if not np.allclose(per_face, areas, rtol=1e-8, atol=1e-6):
            raise LargeRunExportError("per-face overlap area does not reproduce native face_area")
        self.covered = covered
        self.coverage = np.minimum(1.0, covered / (dx * dy))
        self.active = covered > 0
        with np.errstate(invalid="ignore", divide="ignore"):
            self.elevation = np.where(self.active, np.bincount(self.pair_cell, weights=self.pair_area * bed[self.pair_face], minlength=ncell) / covered, np.nan)
        self.native_area_m2 = native_total
        self.coordinate_system = coordinate_system
        self.identity = {"grid_schema_version": GRID_SCHEMA_VERSION, "horizontal_crs": coordinate_system,
                         "axis_order": "x_east_y_north", "anchor_edge_m": [ext["anchor"][0], ext["anchor"][1]],
                         "origin_edge_m": [x0, y0], "dx_m": dx, "dy_m": dy,
                         "i0": ext["i0"], "j0": ext["j0"], "i1": ext["i1"], "j1": ext["j1"], "nx": nx, "ny": ny,
                         "cell_center_formula": "(x0+(col+0.5)*dx, y0+(row+0.5)*dy)",
                         "row_order": "south_to_north", "col_order": "west_to_east", "id_rule": "1-based row-major: row*nx+col+1"}
        self.grid_id_digest = _canonical_digest(self.identity)

    def project(self, h: np.ndarray, mx: np.ndarray, my: np.ndarray) -> dict[str, np.ndarray]:
        n = self.nx * self.ny
        w = self.pair_area; f = self.pair_face; c = self.pair_cell
        with np.errstate(invalid="ignore", divide="ignore"):
            depth = np.bincount(c, weights=w * h[f], minlength=n) / self.covered
            qx = np.bincount(c, weights=w * mx[f], minlength=n) / self.covered
            qy = np.bincount(c, weights=w * my[f], minlength=n) / self.covered
        depth[~self.active] = np.nan; qx[~self.active] = np.nan; qy[~self.active] = np.nan
        wet = self.active & (depth > 0)
        u = np.where(self.active, 0.0, np.nan); v = u.copy()
        u[wet] = qx[wet] / depth[wet]; v[wet] = qy[wet] / depth[wet]
        speed = np.hypot(u, v)
        shape = (self.ny, self.nx)
        return {"water_depth": depth.reshape(shape), "water_level": (self.elevation + depth).reshape(shape),
                "velocity_x": u.reshape(shape), "velocity_y": v.reshape(shape),
                "velocity_magnitude": speed.reshape(shape), "discharge": (depth * u * self.dy).reshape(shape)}


def _flux_source(ledger_keys: tuple[str, ...], kind: str) -> str | None:
    if f"{kind}_m3_s" in ledger_keys:
        return f"{kind}_m3_s"
    if f"{kind}_volume_m3" in ledger_keys:
        return f"{kind}_volume_m3"
    return None


def _compat_mapping(meta: Any, ledger_keys: tuple[str, ...], cell_size: float) -> dict[str, dict[str, Any]]:
    forcing = meta.forcing
    closed = forcing.boundary_policy == "closed_wall"
    river = forcing.river_inflow_outflow_included
    has_rain = "rain_depth_mm" in ledger_keys
    flux = {}
    for kind in ("inflow", "outflow"):
        src = _flux_source(ledger_keys, kind)
        if closed and not river:
            flux[kind] = {"availability": "ZERO_CLOSED_BOUNDARY_RIVER_INFLOW_OUTFLOW_NOT_INCLUDED", "source": "run forcing scope",
                          "mapping": f"0 because boundaries are closed; river {kind} is NOT included in this run"}
        elif src is None:
            flux[kind] = {"availability": "NOT_PROVIDED_BY_SOLVER_LEDGER", "source": None, "mapping": "NaN; never zero-filled"}
        else:
            flux[kind] = {"availability": "AVAILABLE_SOLVER_LEDGER" + ("_ASSUMED_SECTIONS_RECONSTRUCTED_Q" if forcing.river_section_status.startswith("ASSUMED") else ""),
                          "source": f"solver per-frame ledger {src} (total over {kind} boundary groups)",
                          "mapping": ("rate at the saved time, m3/s" if src.endswith("_m3_s") else
                                      "preceding-interval volume / interval length, m3/s; frame 0 is 0") + f"; {forcing.notice}"}
    if has_rain:
        rain = {"availability": "AVAILABLE_SOLVER_LEDGER", "source": "solver per-frame ledger rain_depth_mm",
                "mapping": "rain depth over the preceding saved interval (mm); frame 0 is 0"}
    elif not forcing.rainfall_included:
        rain = {"availability": "ZERO_NO_RAINFALL_IN_RUN", "source": "run forcing scope", "mapping": "0 because this run applies no rainfall"}
    else:
        rain = {"availability": "NOT_PROVIDED_BY_SOLVER_LEDGER", "source": None, "mapping": "NaN; never inferred from depth change"}
    level_avail = "AVAILABLE_DERIVED" if meta.vertical_datum else "NOT_AVAILABLE_DATUM_UNSPECIFIED"
    return {
        "time": {"availability": "AVAILABLE", "source": "native saved output times", "mapping": "float64 seconds since time_origin"},
        "x": {"availability": "AVAILABLE", "source": "derived display grid", "mapping": f"{cell_size:g} m display-cell centres in source CRS"},
        "y": {"availability": "AVAILABLE", "source": "derived display grid", "mapping": f"{cell_size:g} m display-cell centres in source CRS"},
        "water_depth": {"availability": "AVAILABLE_DERIVED", "source": "native water_depth", "mapping": "exact triangle∩cell overlap-area mean over covered native area"},
        "water_level": {"availability": level_avail, "source": "native bed_elevation + water_depth", "mapping": "area-mean bed + area-mean depth; no datum conversion"},
        "velocity_x": {"availability": "AVAILABLE_DERIVED", "source": "native momentum_x and depth", "mapping": "area-mean momentum_x / area-mean depth where mapped depth > 0, else 0"},
        "velocity_y": {"availability": "AVAILABLE_DERIVED", "source": "native momentum_y and depth", "mapping": "area-mean momentum_y / area-mean depth where mapped depth > 0, else 0"},
        "velocity_magnitude": {"availability": "AVAILABLE_DERIVED", "source": "mapped velocity_x/y", "mapping": "hypot of mapped components"},
        "discharge": {"availability": "AVAILABLE_LEGACY_DIAGNOSTIC", "source": "mapped h and u_x", "mapping": "legacy display diagnostic h*u_x*dy; not a native edge flux"},
        "elevation": {"availability": "AVAILABLE_DERIVED", "source": "native bed_elevation", "mapping": "overlap-area mean over covered native area"},
        "active_mask": {"availability": "AVAILABLE", "source": "native face coverage", "mapping": "1 iff native overlap area > 0"},
        "maximum_depth": {"availability": "AVAILABLE_DERIVED", "source": "mapped saved-frame water_depth", "mapping": "maximum over saved output frames only"},
        "maximum_velocity": {"availability": "AVAILABLE_DERIVED", "source": "mapped saved-frame velocity_magnitude", "mapping": "maximum over saved output frames only"},
        "arrival_time": {"availability": "AVAILABLE_DERIVED", "source": "mapped saved-frame water_depth", "mapping": f"first saved frame with mapped depth > {meta.dry_depth_threshold_m:g} m; NaN never"},
        "inflow": flux["inflow"],
        "outflow": flux["outflow"],
        "rainfall": rain,
        "eta": {"availability": "NOT_AVAILABLE_LEGACY_HYDROSTATIC_FILL", "source": None, "mapping": "no equivalent native SWE field; NaN, never zero-filled"},
        "volume": {"availability": "AVAILABLE_DERIVED", "source": "native water_depth and face_area", "mapping": "sum(native depth × native face area)"},
        "wet_area": {"availability": "AVAILABLE_DERIVED", "source": "native water_depth, face_area, dry threshold", "mapping": "sum(native face area where depth > native dry threshold)"},
        "active_coverage_fraction": {"availability": "AVAILABLE_DERIVED", "source": "native face polygons", "mapping": "overlap area / display-cell area"},
        "source_covered_area_m2": {"availability": "AVAILABLE_DERIVED", "source": "native face polygons", "mapping": "exact native triangle∩display-cell overlap area in m2"},
    }


class _CompatNetcdfWriter:
    def __init__(self, path: Path, native: LargeNativeResult, grid: SparseCompatibilityGrid, profile: LargeRunOutputProfile, sha: str):
        import netCDF4
        meta = native.metadata
        self.path = path; self.grid = grid; self.meta = meta
        self.has_rain = "rain_depth_mm" in native.ledger_keys
        self.flux_source = {kind: _flux_source(native.ledger_keys, kind) for kind in ("inflow", "outflow")}
        self.times = native.time_s
        self.mapping = _compat_mapping(meta, native.ledger_keys, grid.dx)
        self.ds = ds = netCDF4.Dataset(str(path), "w", format="NETCDF4")
        T = native.n_frame
        ds.createDimension("time", T); ds.createDimension("y", grid.ny); ds.createDimension("x", grid.nx)
        attrs = {"format": "legacy-structured-compatible-grid-v1", "result_schema_version": "1.0",
                 "product_type": "derived_structured_compatibility_grid", "artifact_type": "derived_display_compatibility",
                 "large_run_profile": "hydrodynamics-large-run-output-profile-v1.0",
                 "run_id": meta.run_id, "case_id": meta.case_id, "grid_id_digest": grid.grid_id_digest,
                 "native_result_sha256": sha, "source_native_result_path": str(native.path),
                 "origin": f"{grid.x0},{grid.y0}", "grid_spacing": f"{grid.dx},{grid.dy}", "dx": grid.dx, "dy": grid.dy,
                 "coordinate_system": meta.coordinate_system,
                 "vertical_datum": meta.vertical_datum or "UNSPECIFIED_NATIVE_SOURCE_DATUM",
                 "compatibility_profile": "legacy-structured-grid-v1-derived-from-native-triangles",
                 "compatibility_variable_mappings_json": json.dumps(self.mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                 "maximum_semantics": "maximum over saved native output frames only",
                 "time_origin": meta.time_origin, "boundary_policy": meta.forcing.boundary_policy,
                 "forcing_scope": meta.forcing.scope, "forcing_notice": meta.forcing.notice,
                 "river_inflow_outflow_included": np.int8(meta.forcing.river_inflow_outflow_included),
                 "rainfall_included": np.int8(meta.forcing.rainfall_included),
                 "river_section_status": meta.forcing.river_section_status,
                 "discharge_series_status": meta.forcing.discharge_series_status,
                 "boundary_groups_json": json.dumps([g.model_dump(mode="json") for g in meta.forcing.boundary_groups], ensure_ascii=False, separators=(",", ":")),
                 "data_origin": meta.data_origin, "database_status": "DISABLED_NOT_REQUESTED",
                 "social_economy_status": "NOT_READ_NOT_REQUESTED",
                 "display_grid_is_not_computing_mesh": np.int8(1)}
        for key, value in attrs.items():
            ds.setncattr(key, value)
        t = ds.createVariable("time", "f8", ("time",)); t.units = "s"; t.standard_name = "time"; t.calendar = "proleptic_gregorian"
        t[:] = native.time_s
        xv = ds.createVariable("x", "f8", ("x",)); xv.units = "m"; xv.axis = "X"; xv[:] = grid.x0 + (np.arange(grid.nx) + 0.5) * grid.dx
        yv = ds.createVariable("y", "f8", ("y",)); yv.units = "m"; yv.axis = "Y"; yv[:] = grid.y0 + (np.arange(grid.ny) + 0.5) * grid.dy
        level = int(profile.compat_nc_complevel)
        comp = {"zlib": level > 0, "complevel": level or 1, "shuffle": level > 0, "chunksizes": (1, grid.ny, grid.nx)}
        self.fields = {}
        for name, units in (("water_depth", "m"), ("water_level", "m"), ("velocity_x", "m/s"), ("velocity_y", "m/s"),
                            ("velocity_magnitude", "m/s"), ("discharge", "m3/s")):
            var = ds.createVariable(name, "f4", ("time", "y", "x"), fill_value=np.float32(np.nan), **comp)
            var.units = units; var.availability = self.mapping[name]["availability"]; var.mapping = self.mapping[name]["mapping"]
            self.fields[name] = var
        self.static = {}
        for name, units in (("elevation", "m"), ("maximum_depth", "m"), ("maximum_velocity", "m/s"), ("arrival_time", "s")):
            var = ds.createVariable(name, "f4", ("y", "x"), fill_value=np.float32(np.nan), zlib=True)
            var.units = units; var.availability = self.mapping[name]["availability"]; var.mapping = self.mapping[name]["mapping"]
            self.static[name] = var
        shape = (grid.ny, grid.nx)
        active = ds.createVariable("active_mask", "i1", ("y", "x"), zlib=True); active[:] = grid.active.reshape(shape).astype(np.int8)
        active.units = "1"; active.mapping = self.mapping["active_mask"]["mapping"]
        cov = ds.createVariable("active_coverage_fraction", "f4", ("y", "x"), fill_value=np.float32(np.nan), zlib=True)
        cov.units = "1"; cov[:] = grid.coverage.reshape(shape)
        covered = ds.createVariable("source_covered_area_m2", "f8", ("y", "x"), fill_value=np.nan, zlib=True)
        covered.units = "m2"; covered.availability = "AVAILABLE_DERIVED"; covered[:] = grid.covered.reshape(shape)
        self.static["elevation"][:] = grid.elevation.reshape(shape)
        self.scalars = {}
        for name, units in (("inflow", "m3/s"), ("outflow", "m3/s"), ("rainfall", "mm"), ("eta", "m"), ("volume", "m3"), ("wet_area", "m2")):
            var = ds.createVariable(name, "f8", ("time",), fill_value=np.nan)
            var.units = units; var.availability = self.mapping[name]["availability"]; var.mapping = self.mapping[name]["mapping"]
            self.scalars[name] = var
        self.max_depth = np.full(shape, np.nan); self.max_speed = np.full(shape, np.nan); self.arrival = np.full(shape, np.nan)
        self.level_available = bool(meta.vertical_datum)
        self.closed_no_river = meta.forcing.boundary_policy == "closed_wall" and not meta.forcing.river_inflow_outflow_included
        self.rows: dict[str, list[float]] = {"volume_m3": [], "wet_area_m2": [], "display_wet_covered_area_m2": [], "rainfall_mm": []}

    def write(self, frame: Any, mapped: dict[str, np.ndarray], volume: float, wet_area: float) -> None:
        i = frame.index
        for name, var in self.fields.items():
            data = mapped[name]
            if name == "water_level" and not self.level_available:
                data = np.full_like(data, np.nan)
            var[i, :, :] = data.astype(np.float32)
        h = mapped["water_depth"]; speed = mapped["velocity_magnitude"]; active = self.grid.active.reshape(h.shape)
        self.max_depth[active] = np.fmax(self.max_depth[active], h[active])
        self.max_speed[active] = np.fmax(self.max_speed[active], speed[active])
        arrived = active & np.isnan(self.arrival) & (h > self.meta.dry_depth_threshold_m)
        self.arrival[arrived] = frame.time_s
        if self.has_rain:
            rain = 0.0 if i == 0 else frame.ledger["rain_depth_mm"]
        else:
            rain = 0.0 if not self.meta.forcing.rainfall_included else np.nan
        for kind in ("inflow", "outflow"):
            src = self.flux_source[kind]
            if self.closed_no_river:
                value = 0.0
            elif src is None:
                value = np.nan
            elif src.endswith("_m3_s"):
                value = frame.ledger[src]
            else:
                value = 0.0 if i == 0 else frame.ledger[src] / (self.times[i] - self.times[i - 1])
            self.scalars[kind][i] = value
            self.rows.setdefault(f"{kind}_m3_s", []).append(float(value) if math.isfinite(value) else None)
        self.scalars["rainfall"][i] = rain; self.scalars["eta"][i] = np.nan
        self.scalars["volume"][i] = volume; self.scalars["wet_area"][i] = wet_area
        display_wet = float(self.grid.covered.reshape(h.shape)[np.isfinite(h) & (h > 0.0)].sum())
        self.rows["volume_m3"].append(volume); self.rows["wet_area_m2"].append(wet_area)
        self.rows["display_wet_covered_area_m2"].append(display_wet); self.rows["rainfall_mm"].append(float(rain) if math.isfinite(rain) else None)

    def close(self, count: int) -> None:
        ds = self.ds
        self.static["maximum_depth"][:] = self.max_depth; self.static["maximum_velocity"][:] = self.max_speed
        self.static["arrival_time"][:] = self.arrival
        ds.compatibility_frame_count = np.int32(count); ds.compatibility_display_grid_cells = np.int32(self.grid.nx * self.grid.ny)
        ds.rainfall_frame_semantics = "rain depth accumulated over the preceding saved-frame interval; initial frame is zero"
        ds.close()


# ------------------------------------------------------------- native web v2
class _NativeWebV2Writer:
    def __init__(self, out: Path, native: LargeNativeResult, profile: LargeRunOutputProfile, sha: str):
        self.out = out; out.mkdir()
        self.native = native; self.sha = sha
        self.dtype = "<f4" if profile.native_web_value_dtype == "float32" else "<f8"
        self.value_bytes = 4 if self.dtype == "<f4" else 8
        self.frames: list[dict[str, Any]] = []; self.extrema: dict[str, dict[str, float]] = {}
        self.level_available = bool(native.metadata.vertical_datum)
        n_node, n_face = native.node_xy_m.shape[0], native.n_face
        mesh = out / "mesh.bin"
        with mesh.open("xb") as stream:
            stream.write(NATIVE_MESH_MAGIC); stream.write(struct.pack("<II", n_node, n_face))
            stream.write(np.ascontiguousarray(native.node_xy_m, dtype="<f8").tobytes())
            stream.write(np.ascontiguousarray(native.face_nodes, dtype="<u4").tobytes())
            ids = [str(int(v)).encode("ascii") for v in native.face_id]
            if any(len(v) > 32 for v in ids):
                raise LargeRunExportError("face_id does not fit the fixed 32-byte ASCII table")
            stream.write(b"".join(v.ljust(32, b" ") for v in ids))
            stream.write(np.ascontiguousarray(native.face_area_m2, dtype="<f8").tobytes())
            stream.flush(); os.fsync(stream.fileno())
        self.mesh_sha, self.mesh_bytes = _sha_file(mesh), mesh.stat().st_size
        bounds = native.node_xy_m
        self.bounds = [float(bounds[:, 0].min()), float(bounds[:, 1].min()), float(bounds[:, 0].max()), float(bounds[:, 1].max())]

    def write(self, frame: Any, speed: np.ndarray) -> None:
        count = self.native.n_face
        level = frame.water_level_m if self.level_available else np.full(count, np.nan)
        arrays = (frame.water_depth_m, level, frame.velocity_x_m_s, frame.velocity_y_m_s, speed)
        name = f"frame_{frame.index:06d}.bin"; path = self.out / name
        digest = hashlib.sha256()
        header = NATIVE_FRAME_MAGIC_V2 + struct.pack("<IdIII", frame.index, frame.time_s, count, len(arrays), self.value_bytes)
        with path.open("xb") as stream:
            stream.write(header); digest.update(header)
            for (var_name, _), array in zip(NATIVE_WEB_VARIABLES, arrays):
                raw = np.asarray(array, dtype=self.dtype).tobytes(order="C")
                stream.write(raw); digest.update(raw)
                finite = array[np.isfinite(array)]
                if finite.size:
                    bounds = self.extrema.setdefault(var_name, {"min": float(finite.min()), "max": float(finite.max())})
                    bounds["min"] = min(bounds["min"], float(finite.min())); bounds["max"] = max(bounds["max"], float(finite.max()))
            stream.flush()
        self.frames.append({"index": frame.index, "time_s": frame.time_s, "file": name,
                            "sha256": digest.hexdigest(), "bytes": path.stat().st_size})

    def finish(self, run: dict[str, Any]) -> dict[str, Any]:
        native = self.native; meta = native.metadata
        body = {"format": NATIVE_WEB_FORMAT, "version": NATIVE_WEB_VERSION, "status": "complete",
                "run_id": meta.run_id, "case_id": meta.case_id, "data_origin": meta.data_origin,
                "live_solver_ran": meta.live_solver_ran, "production_approved": meta.production_approved,
                "qa_fixture": False, "large_run_profile": True,
                "topology": "native_triangular_mesh", "source_profile": "authoritative_native_faces",
                "source_native_result_sha256": self.sha, "source_native_result_file": "result_native.nc",
                "coordinate_system": meta.coordinate_system, "vertical_datum": meta.vertical_datum,
                "time_origin": meta.time_origin, "times_s": [float(v) for v in native.time_s],
                "dry_depth_threshold_m": float(meta.dry_depth_threshold_m),
                "node_count": int(native.node_xy_m.shape[0]), "face_count": native.n_face,
                "extent_m": self.bounds, "forcing": meta.forcing.model_dump(mode="json"),
                "face_ids_encoding": "decimal ASCII strings, fixed 32-byte records; never parsed as JavaScript Number",
                "face_id_sha256": _face_id_digest(native.face_id),
                "mesh_file": {"name": "mesh.bin", "sha256": self.mesh_sha, "bytes": self.mesh_bytes,
                              "layout": "magic[8], uint32 node_count, uint32 face_count, node_xy float64 interleaved, face_nodes uint32 triples, face_id 32-byte ASCII strings, face_area float64"},
                "value_dtype": "float32-le" if self.value_bytes == 4 else "float64-le",
                "variables": [{"name": name, "dtype": "float32-le" if self.value_bytes == 4 else "float64-le", "units": units,
                               "availability": "available" if name != "water_level_m" or self.level_available else "not_available_datum_unspecified"}
                              for name, units in NATIVE_WEB_VARIABLES],
                "extrema": self.extrema, "frame_magic": NATIVE_FRAME_MAGIC_V2.decode("ascii"),
                "frame_header": "uint32 index, float64 time_s, uint32 face_count, uint32 variable_count, uint32 bytes_per_value; 32-byte header, then each variable as face-order little-endian values",
                "frame_loading": "one file per saved frame; viewer fetches the selected frame and prefetches one neighbour",
                "frames": self.frames,
                "wet_area_rule": f"sum(native face_area_m2 where depth > {meta.dry_depth_threshold_m:g} m)",
                "database_status": "DISABLED_NOT_REQUESTED", "social_economy_status": "NOT_READ_NOT_REQUESTED"}
        raw = _json_bytes(body); _write_new(self.out / "manifest.json", raw)
        return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "frame_count": len(self.frames),
                "face_count": native.n_face, "status": "complete",
                "frame_bytes_total": int(sum(row["bytes"] for row in self.frames))}


# ------------------------------------------------------------ native stepjson
def _fmt_num_list(values: np.ndarray, digits: int) -> list[str]:
    """Vector form of nc2web._fmt_num (same text rule)."""
    out = []
    for value in values.tolist():
        text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
        if text in ("", "-0"):
            text = "0"
        out.append(text if "." in text else text + ".0")
    return out


def _step_records(ids: list[str], h: np.ndarray, u: np.ndarray, v: np.ndarray, still: float) -> list[str]:
    speed = np.hypot(u, v)
    still_mask = ~np.isfinite(speed) | (speed <= 1e-12)
    direction = np.where(still_mask, still, np.degrees(np.arctan2(u, v)) % 360.0)
    speed = np.where(still_mask, 0.0, speed)
    hs, ss, ds_ = _fmt_num_list(h, 3), _fmt_num_list(speed, 2), _fmt_num_list(direction, 1)
    return [f"{a}_{b}_{c}_{d}" for a, b, c, d in zip(ids, hs, ss, ds_)]


class _NativeStepJsonWriter:
    def __init__(self, out: Path, native: LargeNativeResult, profile: LargeRunOutputProfile):
        self.out = out; out.mkdir()
        self.ids = np.asarray([str(int(v)) for v in native.face_id], dtype=object)
        self.min_depth = float(profile.stepjson_min_depth_m); self.still = float(profile.stepjson_still_direction_deg)
        self.files: list[str] = []; self.wet_counts: list[int] = []; self.bytes = 0
        self.native = native

    def write(self, frame: Any) -> None:
        h, u, v = frame.water_depth_m, frame.velocity_x_m_s, frame.velocity_y_m_s
        wet = np.isfinite(h) & np.isfinite(u) & np.isfinite(v) & (h > self.min_depth)
        records = _step_records(self.ids[wet].tolist(), h[wet], u[wet], v[wet], self.still)
        name = f"step_{frame.index:03d}.json"
        raw = (json.dumps({"all": records}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _write_new(self.out / name, raw)
        self.files.append(name); self.wet_counts.append(len(records)); self.bytes += len(raw)

    def finish(self) -> dict[str, Any]:
        meta = self.native.metadata
        report = {"status": "pass", "format": "stepjson-native-face-v1", "epsg": meta.coordinate_system,
                  "source": "result_native.nc", "output_dir": str(self.out), "face_count": self.native.n_face,
                  "times": [float(t) for t in self.native.time_s], "time_origin": meta.time_origin,
                  "files": self.files, "cell_id_rule": "exact native face_id decimal string (not a grid row-major ID)",
                  "record_format": "face_id_depth_speed_direction joined by underscore",
                  "still_direction_deg": self.still, "min_depth_m": self.min_depth, "warnings": [],
                  "wet_counts": self.wet_counts, "forcing_notice": meta.forcing.notice}
        _write_json(self.out / "index.json", report)
        return report


# ----------------------------------------------------------------------- SHP
def _dbf_values(values: np.ndarray, name: str, *, allow_nan: bool = False) -> list[float | None]:
    out: list[float | None] = []
    for value in np.round(values.astype(np.float64), DBF_DECIMALS).tolist():
        if not math.isfinite(value):
            if allow_nan:
                out.append(None); continue
            raise LargeRunExportError(f"SHP field {name} has a non-finite value")
        if len(f"{value:.{DBF_DECIMALS}f}") > 20:
            raise LargeRunExportError(f"SHP field {name} value {value} does not fit N(20,{DBF_DECIMALS})")
        out.append(value)
    return out


_FULL_FIELDS = (("face_id", "C", 32, 0, "native_face_id", None), ("time_s", "N", 20, DBF_DECIMALS, "frame_time_s", "s"),
                ("depth_m", "N", 20, DBF_DECIMALS, "water_depth_m", "m"), ("water_lvl", "N", 20, DBF_DECIMALS, "water_level_m", "m"),
                ("ux_m_s", "N", 20, DBF_DECIMALS, "velocity_x_m_s", "m/s"), ("uy_m_s", "N", 20, DBF_DECIMALS, "velocity_y_m_s", "m/s"),
                ("area_m2", "N", 20, DBF_DECIMALS, "native_face_area_m2", "m2"),
                ("max_depth", "N", 20, DBF_DECIMALS, "saved_frame_maximum_water_depth_m", "m"),
                ("max_time", "N", 20, DBF_DECIMALS, "earliest_saved_time_of_maximum_s", "s"),
                ("max_speed", "N", 20, DBF_DECIMALS, "saved_frame_maximum_speed_m_s", "m/s"),
                ("arrive_s", "N", 20, DBF_DECIMALS, "saved_frame_arrival_time_s_blank_if_never_wet", "s"),
                ("bed_m", "N", 20, DBF_DECIMALS, "bed_elevation_m", "m"))
_MAX_FIELDS = (("face_id", "C", 32, 0, "native_face_id", None),
               ("max_depth", "N", 20, DBF_DECIMALS, "saved_frame_maximum_water_depth_m", "m"),
               ("max_time", "N", 20, DBF_DECIMALS, "earliest_saved_time_of_maximum_s", "s"),
               ("area_m2", "N", 20, DBF_DECIMALS, "native_face_area_m2", "m2"),
               ("max_speed", "N", 20, DBF_DECIMALS, "saved_frame_maximum_speed_m_s", "m/s"),
               ("arrive_s", "N", 20, DBF_DECIMALS, "saved_frame_arrival_time_s", "s"),
               ("bed_m", "N", 20, DBF_DECIMALS, "bed_elevation_m", "m"))


def _write_shp_package(directory: Path, role: str, scope: str, target: str, native: LargeNativeResult,
                       selection: np.ndarray, columns: dict[str, list[Any]], fields: tuple, sha: str,
                       threshold: float, selector: dict[str, Any]) -> dict[str, Any]:
    import shapefile
    from pyproj import CRS, Transformer
    import pyproj
    directory.mkdir(parents=True)
    source = CRS.from_user_input(native.metadata.coordinate_system)
    target_crs = source if target == "NATIVE" else CRS.from_epsg(4490)
    xy = native.node_xy_m
    if target == "NATIVE":
        out_xy = xy
    else:
        transformer = Transformer.from_crs(source, target_crs, always_xy=True)
        lon, lat = transformer.transform(xy[:, 0], xy[:, 1])
        out_xy = np.column_stack([lon, lat])
        if not np.isfinite(out_xy).all():
            raise LargeRunExportError("EPSG:4490 transform produced non-finite coordinates")
    faces = native.face_nodes[selection]
    tri = out_xy[faces]
    signed = (tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1]) - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1])
    ccw = signed > 0
    tri[ccw] = tri[ccw][:, [0, 2, 1]]  # ESRI outer rings are clockwise; vertices unchanged
    writer = shapefile.Writer(str(directory / "faces"), shapeType=shapefile.POLYGON, encoding="utf-8")
    writer.autoBalance = 1
    try:
        for name, kind, width, decimals, *_ in fields:
            writer.field(name, kind, size=width, decimal=decimals)
        names = [row[0] for row in fields]
        cols = [columns[name] for name in names]
        for k in range(tri.shape[0]):
            ring = tri[k].tolist(); ring.append(ring[0])
            writer.poly([ring])
            writer.record(*[col[k] for col in cols])
    finally:
        writer.close()
    (directory / "faces.cpg").write_text("UTF-8", encoding="ascii")
    (directory / "faces.prj").write_text(target_crs.to_wkt(version="WKT1_ESRI"), encoding="utf-8")
    files = []
    for key in ("faces.shp", "faces.shx", "faces.dbf", "faces.prj", "faces.cpg"):
        path = directory / key
        files.append({"key": key, "sha256": _sha_file(path), "bytes": path.stat().st_size})
    meta = native.metadata
    exported_ids = native.face_id[selection]
    manifest = {"format": SHP_FORMAT, "schema_version": "1.0", "role": role, "scope_selection": scope,
                "target_crs": target, "target_crs_name": target_crs.name,
                "target_crs_wkt_esri": target_crs.to_wkt(version="WKT1_ESRI"),
                "source_crs": meta.coordinate_system,
                "crs_transform": None if target == "NATIVE" else {"from": meta.coordinate_system, "to": "EPSG:4490",
                    "library": "pyproj", "pyproj_version": pyproj.__version__, "proj_version": pyproj.proj_version_str,
                    "always_xy": True, "vertex_transform_only": True},
                "run_id": meta.run_id, "case_id": meta.case_id, "data_origin": meta.data_origin,
                "source_native_result_file": "result_native.nc", "source_native_result_sha256": sha,
                "geometry": "one true native triangle per feature (3 distinct vertices + closing point, clockwise)",
                "feature_count": int(selection.size), "native_face_count": native.n_face,
                "exported_face_id_sha256": _face_id_digest(exported_ids),
                "native_face_id_sha256": _face_id_digest(native.face_id),
                "scope_area_m2": float(math.fsum(native.face_area_m2[selection].tolist())),
                "wet_depth_threshold_m": threshold, "selector": selector,
                "fields": [{"dbf_name": name, "dbf_type": kind, "width": width, "decimals": decimals,
                            "semantic_name": semantic, "unit": unit} for name, kind, width, decimals, semantic, unit in fields],
                "forcing": meta.forcing.model_dump(mode="json"), "files": files}
    _write_json(directory / "manifest.json", manifest)
    return {"role": role, "scope": scope, "target_crs": target, "feature_count": int(selection.size),
            "manifest_sha256": _sha_file(directory / "manifest.json"),
            "components": sorted(p.name for p in directory.iterdir() if p.is_file())}


def _walk_ledger(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_ledger(item, f"{prefix}__{key}" if prefix else str(key))
    else:
        yield prefix, value


def _export_shp(out: Path, native: LargeNativeResult, profile: LargeRunOutputProfile, sha: str) -> list[dict[str, Any]]:
    meta = native.metadata
    threshold = float(meta.dry_depth_threshold_m if profile.shp_wet_depth_threshold_m is None else profile.shp_wet_depth_threshold_m)
    last = native.frame(native.n_frame - 1)
    max_h = native.static("saved_frame_max_water_depth"); max_t = native.static("saved_frame_max_water_depth_time")
    max_s = native.static("saved_frame_max_speed"); arrival = native.static("saved_frame_arrival_time")
    ids = [str(int(v)) for v in native.face_id]
    bed = native.bed_elevation_m; area = native.face_area_m2
    level = last.water_level_m
    full_cols = {"face_id": ids, "time_s": _dbf_values(np.full(native.n_face, last.time_s), "time_s"),
                 "depth_m": _dbf_values(last.water_depth_m, "depth_m"), "water_lvl": _dbf_values(level, "water_lvl"),
                 "ux_m_s": _dbf_values(last.velocity_x_m_s, "ux_m_s"), "uy_m_s": _dbf_values(last.velocity_y_m_s, "uy_m_s"),
                 "area_m2": _dbf_values(area, "area_m2"), "max_depth": _dbf_values(max_h, "max_depth"),
                 "max_time": _dbf_values(max_t, "max_time"), "max_speed": _dbf_values(max_s, "max_speed"),
                 "arrive_s": _dbf_values(arrival, "arrive_s", allow_nan=True), "bed_m": _dbf_values(bed, "bed_m")}
    wet = np.flatnonzero(max_h > threshold)
    max_cols = {name: [full_cols[name][k] for k in wet.tolist()] for name in ("face_id", "max_depth", "max_time", "area_m2", "max_speed", "arrive_s", "bed_m")}
    full_sel = np.arange(native.n_face)
    frame_selector = {"aggregation": "FRAME", "frame_index": native.n_frame - 1, "time_s": last.time_s}
    max_selector = {"aggregation": "MAX_OVER_SAVED_FRAMES", "window_start_s": float(native.time_s[0]), "window_end_s": float(native.time_s[-1])}
    rows = []
    for role, scope, target, sel, cols, fields, selector in (
        ("full_domain", "FULL_DOMAIN", "NATIVE", full_sel, full_cols, _FULL_FIELDS, frame_selector),
        ("full_domain_4490", "FULL_DOMAIN", "EPSG:4490", full_sel, full_cols, _FULL_FIELDS, frame_selector),
        ("maximum_wet_surface", "MAXIMUM_WET_SURFACE", "NATIVE", wet, max_cols, _MAX_FIELDS, max_selector),
        ("maximum_wet_surface_4490", "MAXIMUM_WET_SURFACE", "EPSG:4490", wet, max_cols, _MAX_FIELDS, max_selector),
    ):
        rows.append(_write_shp_package(out / role, role, scope, target, native, sel, cols, fields, sha, threshold, selector))
    return rows


# --------------------------------------------------------------- the export
def export_large_run_bundle(bundle_dir: str | Path, profile: LargeRunOutputProfile | None = None,
                            *, progress: Any = None) -> dict[str, Any]:
    """Export every derived deliverable from ``<bundle_dir>/result_native.nc`` (streaming)."""
    from .nc2web import convert_netcdf_to_step_json, convert_netcdf_to_web
    profile = profile or LargeRunOutputProfile()
    out = Path(bundle_dir).resolve()
    native_path = out / "result_native.nc"
    log = progress or (lambda message: None)
    started = _time.perf_counter(); timings: dict[str, float] = {}
    if not native_path.is_file():
        raise LargeRunExportError(f"bundle has no result_native.nc: {out}")
    existing = sorted(p.name for p in out.iterdir() if p.name != "result_native.nc")
    if existing:
        raise LargeRunExportError(f"bundle already contains derived outputs; no-clobber refused: {existing[:5]}")
    with LargeNativeResult(native_path) as native:
        meta = native.metadata
        if native.n_frame > profile.max_frame_count or native.n_face > profile.max_face_count:
            raise LargeRunExportError("native result exceeds the large-run profile caps")
        sha = native.sha256(); timings["hash_native_s"] = _time.perf_counter() - started
        log(f"native {native.n_face} faces, {native.n_frame} frames, sha {sha[:12]}")
        t0 = _time.perf_counter()
        grid = SparseCompatibilityGrid(native.node_xy_m, native.face_nodes, native.face_area_m2, native.bed_elevation_m,
                                       meta.coordinate_system, profile)
        timings["compat_grid_s"] = _time.perf_counter() - t0
        if profile.disk_preflight:
            need = estimate_bundle_bytes(native.n_face, native.node_xy_m.shape[0], native.n_frame, grid.nx * grid.ny, profile)
            free = shutil.disk_usage(out).free
            if free < need * float(profile.disk_safety_factor):
                raise LargeRunExportError(f"disk preflight failed: need about {need} bytes x {profile.disk_safety_factor}, {free} free")
        log(f"compat grid {grid.nx}x{grid.ny} @ {grid.dx:g} m, {grid.pair_cell.size} overlaps")
        comp_path = out / f"{meta.case_id}.nc"
        if comp_path.name == "result_native.nc":
            raise LargeRunExportError("case_id must not collide with result_native")
        compat = _CompatNetcdfWriter(comp_path, native, grid, profile, sha)
        web = _NativeWebV2Writer(out / "native_web", native, profile, sha)
        steps_native = _NativeStepJsonWriter(out / "stepjson_native", native, profile) if profile.write_stepjson_native else None
        frame_stats = []
        tol = float(profile.volume_check_rel_tol)
        t0 = _time.perf_counter()
        try:
            for frame in native.iter_frames():
                h = frame.water_depth_m
                speed = np.hypot(frame.velocity_x_m_s, frame.velocity_y_m_s)
                volume = float(np.dot(h, native.face_area_m2))
                if abs(volume - frame.volume_m3) > tol * max(1.0, abs(frame.volume_m3)):
                    raise LargeRunExportError(f"frame {frame.index} volume differs from the native NC volume series")
                if "volume_m3" in frame.ledger and abs(volume - frame.ledger["volume_m3"]) > tol * max(1.0, abs(frame.ledger["volume_m3"])):
                    raise LargeRunExportError(f"frame {frame.index} volume differs from the solver ledger")
                wet = h > meta.dry_depth_threshold_m
                wet_area = float(native.face_area_m2[wet].sum())
                mapped = grid.project(h, frame.momentum_x_m2_s, frame.momentum_y_m2_s)
                compat.write(frame, mapped, volume, wet_area)
                web.write(frame, speed)
                if steps_native is not None:
                    steps_native.write(frame)
                frame_stats.append({"index": frame.index, "time_s": frame.time_s, "max_depth_m": float(h.max()),
                                    "max_speed_m_s": float(speed.max()), "volume_m3": volume, "wet_area_m2": wet_area,
                                    "wet_face_count": int(np.count_nonzero(wet)), **{f"ledger_{k}": v for k, v in frame.ledger.items()}})
                if frame.index % 24 == 0:
                    log(f"frame {frame.index}/{native.n_frame - 1}")
        finally:
            compat.close(len(frame_stats))
        timings["frame_pass_s"] = _time.perf_counter() - t0
        run_preview = {"run_id": meta.run_id}
        web_report = web.finish(run_preview)
        steps_native_report = steps_native.finish() if steps_native is not None else None
        # Grid sidecars
        coverage_path = out / "compatibility_grid_coverage.bin"
        _write_new(coverage_path, b"CGCOV01\n" + struct.pack("<II", grid.ny, grid.nx)
                   + grid.coverage.astype("<f4").tobytes() + grid.covered.astype("<f8").tobytes())
        mapping_path = out / "compatibility_grid_mapping.npz"
        with mapping_path.open("xb") as stream:
            np.savez_compressed(stream, cell=grid.pair_cell.astype(np.int64), face_index=grid.pair_face.astype(np.int64),
                                overlap_area_m2=grid.pair_area, face_id=native.face_id)
        t0 = _time.perf_counter()
        nc2web = convert_netcdf_to_web(comp_path, out / "nc2web")
        timings["nc2web_s"] = _time.perf_counter() - t0; log("nc2web done")
        t0 = _time.perf_counter()
        stepjson = convert_netcdf_to_step_json(comp_path, out / "stepjson", min_depth=float(profile.stepjson_min_depth_m),
                                               still_direction_deg=float(profile.stepjson_still_direction_deg))
        timings["stepjson_s"] = _time.perf_counter() - t0; log("stepjson done")
        grid_doc = {"format": GRID_FORMAT, "schema_version": GRID_SCHEMA_VERSION, "run_id": meta.run_id,
                    "grid_id_digest": grid.grid_id_digest, "grid_identity": grid.identity,
                    "native_result_sha256": sha, "id_rule": "1-based row-major: row*nx+col+1",
                    "nx": grid.nx, "ny": grid.ny, "origin_lower_left_m": [grid.x0, grid.y0], "dx": grid.dx, "dy": grid.dy,
                    "coordinate_system": meta.coordinate_system, "mesh_bounds_m": grid.extent["mesh_bounds"],
                    "display_is_derived": True, "native_triangles_remain_authoritative": True,
                    "display_grid_is_not_computing_mesh": True,
                    "coverage_area_m2": float(grid.covered.sum()), "native_active_area_m2": grid.native_area_m2,
                    "active_cell_count": int(np.count_nonzero(grid.active)),
                    "partial_cell_count": int(np.count_nonzero((grid.coverage > 0) & (grid.coverage < 1 - 1e-12))),
                    "overlap_pair_count": int(grid.pair_cell.size),
                    "face_overlap_rule": "exact planar native triangle ∩ display cell area in source projected CRS",
                    "field_rule": "area-average h and per-width momentum over covered area; u/v = mean(momentum)/mean(h) where mean(h)>0",
                    "coverage_file": {"name": coverage_path.name, "sha256": _sha_file(coverage_path), "bytes": coverage_path.stat().st_size,
                                      "layout": "magic CGCOV01\\n, uint32 ny, uint32 nx, float32 coverage[ny*nx] row-major (row=south..north), float64 source_covered_area_m2[ny*nx]"},
                    "mapping_file": {"name": mapping_path.name, "sha256": _sha_file(mapping_path), "bytes": mapping_path.stat().st_size,
                                     "layout": "npz: cell (row*nx+col, 0-based), face_index (0-based native order), overlap_area_m2, face_id"},
                    "variables": compat.mapping,
                    "nc2web_frame_files": [{"file": row["file"], "bytes": (out / "nc2web" / row["file"]).stat().st_size,
                                            "sha256": _sha_file(out / "nc2web" / row["file"])} for row in nc2web.frames]}
        _write_json(out / "compatibility_grid.json", grid_doc)
        t0 = _time.perf_counter()
        shp_rows = _export_shp(out / "shp", native, profile, sha)
        timings["shp_s"] = _time.perf_counter() - t0; log("shp done")
        viewer_source = Path(__file__).resolve().parent / "assets" / "viewer.html"
        _write_new(out / "viewer.html", viewer_source.read_bytes())
        maxima_present = [name for name in ("solver_max_water_depth_m", "solver_max_speed_m_s") if name in native._ds.variables]
        solver_max = {name: float(np.nanmax(native.static(name))) for name in maxima_present}
        ledger_doc = native.mass_ledger
        not_applicable = "NOT_APPLICABLE_RAIN_ONLY_CLOSED_BOUNDARY" if not meta.forcing.river_inflow_outflow_included else "NOT_REQUESTED_NO_GATE_DISPATCH_INPUT"
        group_series = {key: [row[f"ledger_{key}"] for row in frame_stats] for key in native.ledger_keys if "__" in key}
        shortfall = {key: value for key, value in _walk_ledger(ledger_doc) if "shortfall" in key.lower()}
        shortfall.update({key: [row[f"ledger_{key}"] for row in frame_stats] for key in native.ledger_keys if "shortfall" in key.lower()})
        times = [float(t) for t in native.time_s]
        status = "COMPLETE" if meta.production_approved else "COMPLETE_NOT_PRODUCTION_APPROVED"
        forcing = meta.forcing.model_dump(mode="json")
        run = {"format": BUNDLE_FORMAT, "schema_version": "1.0", "run_id": meta.run_id, "case_id": meta.case_id,
               "status": status, "data_origin": meta.data_origin, "live_solver_ran": meta.live_solver_ran,
               "simulation_result": meta.live_solver_ran, "production_approved": meta.production_approved,
               "qa_fixture": False, "large_run_profile": profile.model_dump(mode="json"),
               "forcing": forcing, "forcing_notice": meta.forcing.notice,
               "river_inflow_outflow_included": meta.forcing.river_inflow_outflow_included,
               "native_result": {"file": "result_native.nc", "sha256": sha, "bytes": native_path.stat().st_size,
                                 "face_count": native.n_face, "node_count": int(native.node_xy_m.shape[0]),
                                 "saved_frame_count": native.n_frame, "grid_topology": "native_triangular_mesh",
                                 "dry_depth_threshold_m": float(meta.dry_depth_threshold_m),
                                 "face_id_sha256": _face_id_digest(native.face_id)},
               "time_origin": meta.time_origin, "time_s": times,
               "extent_m": web.bounds, "coordinate_system": meta.coordinate_system, "vertical_datum": meta.vertical_datum,
               "viewer": {"default_profile": "native_web", "profiles": {"native_web": "native_web/manifest.json",
                                                                       "compatibility_grid": "nc2web/manifest.json"}},
               "compatibility": {"profile": "legacy-structured-grid-v1-derived-from-native-triangles", "file": comp_path.name,
                                 "sha256": _sha_file(comp_path), "grid_id_digest": grid.grid_id_digest,
                                 "mapping_file": "compatibility_grid.json", "nc2web_v1": "nc2web/manifest.json",
                                 "stepjson_v1": "stepjson/index.json", "stepjson_native": "stepjson_native/index.json" if steps_native else None,
                                 "cell_size_m": grid.dx, "nx": grid.nx, "ny": grid.ny},
               "native_web": {"manifest": "native_web/manifest.json", "sha256": web_report["sha256"], "version": NATIVE_WEB_VERSION},
               "shp_packages": shp_rows, "solver": meta.solver, "source_hashes": meta.source_hashes,
               "legacy_optional_artifacts": {
                   "gate_dispatch.csv": {"status": not_applicable},
                   "gate_dispatch.json": {"status": not_applicable},
                   "dispatch_input.json": {"status": not_applicable},
                   "database_receipt": {"status": "DISABLED_NOT_REQUESTED"},
                   "social_economy_overlay": {"status": "NOT_READ_NOT_REQUESTED"}},
               "database_status": "DISABLED_NOT_REQUESTED", "social_economy_status": "NOT_READ_NOT_REQUESTED",
               "source_graph_file": "provenance/source_graph.json"}
        for name in ("mass_budget", "quality", "provenance", "logs"):
            (out / name).mkdir()
        _write_json(out / "provenance" / "source_graph.json", {"format": "large-run-source-graph-v1", "run_id": meta.run_id,
            "native_result": {"file": "result_native.nc", "sha256": sha}, "source_hashes": meta.source_hashes,
            "solver": meta.solver, "run_metadata": meta.model_dump(mode="json")})
        _write_json(out / "mass_budget" / "summary.json", {"format": "large-run-mass-budget-v1", "run_id": meta.run_id,
            "time_s": times, "volume_m3": compat.rows["volume_m3"], "native_wet_area_m2": compat.rows["wet_area_m2"],
            "compatibility_display_wet_covered_area_m2": compat.rows["display_wet_covered_area_m2"],
            "rainfall_mm": compat.rows["rainfall_mm"],
            "solver_ledger_series": {key: [row[f"ledger_{key}"] for row in frame_stats] for key in native.ledger_keys},
            "solver_ledger_close": ledger_doc, "volume_check_rel_tol": tol,
            "inflow_m3_s": compat.rows.get("inflow_m3_s"), "outflow_m3_s": compat.rows.get("outflow_m3_s"),
            "boundary_groups": [g.model_dump(mode="json") for g in meta.forcing.boundary_groups],
            "boundary_group_ledger_series": group_series, "outflow_shortfall": shortfall,
            "forcing": forcing,
            "inflow_outflow": ("0 / not included: rain-only run with closed boundaries" if not meta.forcing.river_inflow_outflow_included
                               else meta.forcing.notice)})
        _write_json(out / "quality" / "compatibility_mapping.json", {"format": "large-run-compatibility-quality-v1",
            "grid_id_digest": grid.grid_id_digest, "native_active_area_m2": grid.native_area_m2,
            "covered_area_m2": float(grid.covered.sum()), "area_residual_m2": float(grid.covered.sum()) - grid.native_area_m2,
            "partial_display_cell_count": grid_doc["partial_cell_count"],
            "no_coverage_cell_count": int(np.count_nonzero(~grid.active)), "candidate_pairs": grid.candidate_pairs,
            "overlap_pairs": int(grid.pair_cell.size)})
        summary = {"format": "large-run-summary-v1", "run_id": meta.run_id, "case_id": meta.case_id,
                   "data_origin": meta.data_origin, "forcing_notice": meta.forcing.notice,
                   "native_face_count": native.n_face, "native_node_count": int(native.node_xy_m.shape[0]),
                   "saved_frames": native.n_frame, "time_origin": meta.time_origin,
                   "first_time_s": times[0], "last_time_s": times[-1],
                   "compatibility_grid": {"nx": grid.nx, "ny": grid.ny, "dx": grid.dx, "dy": grid.dy, "grid_id_digest": grid.grid_id_digest},
                   "max_depth_m_saved_frames": max(row["max_depth_m"] for row in frame_stats),
                   "max_speed_m_s_saved_frames": max(row["max_speed_m_s"] for row in frame_stats),
                   "solver_maxima": solver_max, "forcing": forcing,
                   "mass_ledger": ledger_doc, "boundary_group_ledger_series": group_series, "outflow_shortfall": shortfall,
                   "maximum_wet_face_count": next(row["feature_count"] for row in shp_rows if row["role"] == "maximum_wet_surface"),
                   "frames": frame_stats, "unmapped_fields": {"eta": "NOT_AVAILABLE_LEGACY_HYDROSTATIC_FILL"},
                   "database": "DISABLED_NOT_REQUESTED", "social_economy": "NOT_READ_NOT_REQUESTED"}
        _write_json(out / "summary.json", summary)
        timings["total_s"] = _time.perf_counter() - started
        _write_json(out / "logs" / "large_run_export.json", {"format": "large-run-export-log-v1", "run_id": meta.run_id,
            "status": "PASS", "timings_s": timings, "finished_utc": datetime.now(timezone.utc).isoformat(),
            "native_frame_reader": "streaming one frame per read", "compat_nc": "PASS", "nc2web_v1": nc2web.status,
            "stepjson_v1": stepjson.status, "stepjson_native": "PASS" if steps_native else "DISABLED",
            "native_web_v2": "PASS", "shp_packages": "PASS"})
        parse_summary = {"format": "large-run-parse-summary-v1", "run_id": meta.run_id,
                         "native_web": {"status": web_report["status"], "frames": web_report["frame_count"], "faces": web_report["face_count"]},
                         "compatibility_nc": {"status": "PASS", "path": comp_path.name},
                         "nc2web": {"status": nc2web.status, "frames": len(nc2web.frames), "variables": list(nc2web.variables)},
                         "stepjson": {"status": stepjson.status, "files": len(stepjson.files), "record_format": stepjson.record_format},
                         "stepjson_native": None if steps_native_report is None else {"status": "pass", "files": len(steps_native_report["files"])},
                         "shp_packages": shp_rows, "forcing_notice": meta.forcing.notice}
        _write_json(out / "parse_summary.json", parse_summary)
        _write_json(out / "run_report.json", {"format": "large-run-report-v1", "run_id": meta.run_id, "status": "complete",
            "complete": True, "can_publish": bool(meta.production_approved), "production_approved": meta.production_approved,
            "data_origin": meta.data_origin, "forcing_notice": meta.forcing.notice,
            "message": "All large-run deliverables were exported from the streamed native result.",
            "outputs": {"native_web": web_report["sha256"], "nc2web_manifest": _sha_file(out / "nc2web" / "manifest.json"),
                        "stepjson_index": _sha_file(out / "stepjson" / "index.json")}})
        _write_json(out / "run.json", run)
    rows = []
    for path in sorted(p for p in out.rglob("*") if p.is_file() and p.name != "checksums.json"):
        rows.append({"path": path.relative_to(out).as_posix(), "bytes": path.stat().st_size, "sha256": _sha_file(path)})
    _write_json(out / "checksums.json", {"format": "large-run-bundle-checksums-v1", "run_id": meta.run_id,
                                          "file_count": len(rows), "total_bytes": int(sum(r["bytes"] for r in rows)), "files": rows})
    timings["total_with_checksums_s"] = _time.perf_counter() - started
    return {"run_id": meta.run_id, "output_dir": str(out), "status": status, "face_count": int(run["native_result"]["face_count"]),
            "frame_count": len(times), "grid": [grid.nx, grid.ny, grid.dx], "file_count": len(rows),
            "total_bytes": int(sum(r["bytes"] for r in rows)), "timings_s": timings}


# --------------------------------------------------------------- verification
def verify_large_run_bundle(bundle_dir: str | Path) -> dict[str, Any]:
    """Independent open/parse check of every deliverable (streaming; used by tests and the synthetic driver)."""
    import netCDF4
    import shapefile
    out = Path(bundle_dir).resolve()
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    if run.get("format") != BUNDLE_FORMAT:
        raise LargeRunExportError("run.json is not a large-run bundle")
    checks: dict[str, Any] = {}
    checksums = json.loads((out / "checksums.json").read_text(encoding="utf-8"))
    listed = {row["path"]: row for row in checksums["files"]}
    actual = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p.name != "checksums.json"}
    if set(listed) != actual or any((out / k).stat().st_size != v["bytes"] for k, v in listed.items()):
        raise LargeRunExportError("checksums.json file list/sizes differ from the bundle")
    checks["checksums"] = {"files": len(listed), "total_bytes": checksums["total_bytes"]}
    T = run["native_result"]["saved_frame_count"]; F = run["native_result"]["face_count"]
    with netCDF4.Dataset(str(out / "result_native.nc")) as ds:
        if ds.status != "COMPLETE" or ds["water_depth"].shape != (T, F) or ds.dimensions["time"].isunlimited() is False:
            raise LargeRunExportError("native NC dimensions/status mismatch")
        ids = np.asarray(ds["face_id"][:]); h_last = np.asarray(ds["water_depth"][T - 1, :])
        checks["native_nc"] = {"time": T, "face": F, "node": len(ds.dimensions["node"]), "forcing_notice": ds.forcing_notice,
                               "river_inflow_outflow_included": int(ds.river_inflow_outflow_included)}
    with netCDF4.Dataset(str(out / run["compatibility"]["file"])) as ds:
        names = {"time", "x", "y", "water_depth", "water_level", "velocity_x", "velocity_y", "velocity_magnitude", "discharge",
                 "elevation", "active_mask", "maximum_depth", "maximum_velocity", "arrival_time", "inflow", "outflow",
                 "rainfall", "eta", "volume", "wet_area"}
        missing = names - set(ds.variables)
        if missing or ds["water_depth"].shape[0] != T:
            raise LargeRunExportError(f"compatibility NC missing {sorted(missing)} or wrong time length")
        checks["compat_nc"] = {"variables": len(ds.variables), "shape": list(ds["water_depth"].shape), "grid_id_digest": ds.grid_id_digest}
    nc2 = json.loads((out / "nc2web" / "manifest.json").read_text(encoding="utf-8"))
    if nc2["format"] != "nc2web" or nc2["version"] != "1.0" or len(nc2["frames"]) != T:
        raise LargeRunExportError("nc2web manifest invalid")
    for row in (nc2["frames"][0], nc2["frames"][-1]):
        raw = (out / "nc2web" / row["file"]).read_bytes()
        ny, nx, nv = struct.unpack("<III", raw[8:20])
        if raw[:8] != b"NC2WF01\n" or len(raw) != 20 + ny * nx * nv * 4:
            raise LargeRunExportError("nc2web frame header/length invalid")
    checks["nc2web"] = {"frames": len(nc2["frames"]), "nx": nc2["nx"], "ny": nc2["ny"], "variables": nc2["variables"]}
    for sub in ("stepjson", "stepjson_native"):
        index_path = out / sub / "index.json"
        if not index_path.is_file():
            continue
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if len(index["files"]) != T:
            raise LargeRunExportError(f"{sub} frame count mismatch")
        records = 0
        for name in index["files"]:
            doc = json.loads((out / sub / name).read_text(encoding="utf-8"))
            if list(doc) != ["all"] or any(type(r) is not str or r.count("_") != 3 for r in doc["all"]):
                raise LargeRunExportError(f"{sub}/{name} is not the legacy {{'all':[...]}} structure")
            records += len(doc["all"])
        checks[sub] = {"files": len(index["files"]), "records": records}
    manifest = json.loads((out / "native_web" / "manifest.json").read_text(encoding="utf-8"))
    if manifest["version"] != NATIVE_WEB_VERSION or len(manifest["frames"]) != T or manifest["face_count"] != F:
        raise LargeRunExportError("native_web manifest invalid")
    mesh_raw = (out / "native_web" / "mesh.bin").read_bytes()
    n_node, n_face = struct.unpack("<II", mesh_raw[8:16])
    id_off = 16 + n_node * 16 + n_face * 12
    web_ids = np.asarray(mesh_raw[id_off:id_off + 32 * n_face].decode("ascii").split(), dtype=np.int64) if n_face else np.zeros(0)
    if n_face != F or not np.array_equal(web_ids, ids):
        raise LargeRunExportError("native_web mesh face IDs differ from the native NC")
    frame_raw = (out / "native_web" / manifest["frames"][-1]["file"]).read_bytes()
    index, t, faces, nvars, vbytes = struct.unpack("<IdIII", frame_raw[8:32])
    depth = np.frombuffer(frame_raw, dtype="<f4" if vbytes == 4 else "<f8", count=faces, offset=32)
    if frame_raw[:8] != NATIVE_FRAME_MAGIC_V2 or len(frame_raw) != 32 + faces * nvars * vbytes or not np.allclose(depth, h_last, rtol=1e-6, atol=1e-6):
        raise LargeRunExportError("native_web frame layout/values invalid")
    checks["native_web"] = {"frames": len(manifest["frames"]), "frame_bytes": len(frame_raw), "mesh_bytes": len(mesh_raw)}
    shp_checks = {}
    for row in run["shp_packages"]:
        directory = out / "shp" / row["role"]
        for key in ("faces.shp", "faces.shx", "faces.dbf", "faces.prj", "faces.cpg", "manifest.json"):
            if not (directory / key).is_file():
                raise LargeRunExportError(f"SHP {row['role']} is missing {key}")
        with shapefile.Reader(str(directory / "faces.shp"), encoding="utf-8") as reader:
            if reader.shapeType != shapefile.POLYGON or len(reader) != row["feature_count"]:
                raise LargeRunExportError(f"SHP {row['role']} type/count invalid")
            bad = 0; dbf_ids = []
            for shape_rec in reader.iterShapeRecords():
                pts = shape_rec.shape.points
                if len(pts) != 4 or tuple(pts[0]) != tuple(pts[-1]) or len({tuple(p) for p in pts[:3]}) != 3:
                    bad += 1
                dbf_ids.append(int(str(shape_rec.record[0]).strip()))
            if bad:
                raise LargeRunExportError(f"SHP {row['role']} has {bad} non-triangle features")
            if row["scope"] == "FULL_DOMAIN" and not np.array_equal(np.asarray(dbf_ids, dtype=np.int64), ids):
                raise LargeRunExportError(f"SHP {row['role']} face IDs differ from native face_id")
        shp_checks[row["role"]] = {"features": row["feature_count"], "prj_head": (directory / "faces.prj").read_text(encoding="utf-8")[:40]}
    checks["shp"] = shp_checks
    if not (out / "viewer.html").is_file():
        raise LargeRunExportError("viewer.html missing")
    checks["viewer"] = {"profiles": run["viewer"]["profiles"], "time_origin": run["time_origin"]}
    return {"status": "PASS", "run_id": run["run_id"], "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hydrodynamics-large-run-export")
    parser.add_argument("bundle_dir", help="directory containing result_native.nc written by NativeUgridNetcdfSink")
    parser.add_argument("--profile", help="large-run output profile YAML/JSON (defaults apply when omitted)")
    parser.add_argument("--verify", action="store_true", help="re-open and parse every deliverable after export")
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile) if args.profile else LargeRunOutputProfile()
        report = export_large_run_bundle(args.bundle_dir, profile, progress=lambda m: print(m, file=sys.stderr, flush=True))
        if args.verify:
            report["verify"] = verify_large_run_bundle(args.bundle_dir)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    except (LargeRunExportError, FrameSinkError, ValueError, OSError) as exc:
        print(json.dumps({"status": "failed", "type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
