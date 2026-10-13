"""Hourly DEM-filling inundation for detention-basin runs.

The 1 h step is a volume-conserving hydrostatic fill on the mapped terrain.
It is not a CFL-limited HLL time step: a 50 m shallow-water CFL step is a few
seconds, so 288 h of HLL is not a practical engineering run in this engine.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from .netcdf_writer import OutputDependencyError
from .gate_dispatch import (
    GateDispatchInput, build_gate_dispatch_log, check_gate_dispatch_paths,
    gate_dispatch_paths, write_gate_dispatch_log,
)


def fill_volume(
    elevation: np.ndarray,
    active_mask: np.ndarray,
    volume: float,
    cell_area: float,
) -> tuple[np.ndarray, float, float]:
    """Fill active cells from the lowest elevation until ``volume`` is stored."""
    z = np.asarray(elevation, dtype=float)
    active = np.asarray(active_mask, dtype=bool)
    depth = np.zeros_like(z, dtype=float)
    if volume <= 0.0 or not np.any(active) or cell_area <= 0.0:
        return depth, float(z[active].min()) if np.any(active) else 0.0, 0.0
    z_a = z[active]
    lo = float(np.min(z_a))
    hi = float(np.max(z_a) + volume / (float(np.count_nonzero(active)) * cell_area) + 1.0)
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        stored = float(np.maximum(mid - z_a, 0.0).sum() * cell_area)
        if stored < volume:
            lo = mid
        else:
            hi = mid
    eta = 0.5 * (lo + hi)
    depth[active] = np.maximum(eta - z_a, 0.0)
    stored = float(depth[active].sum() * cell_area)
    return depth, float(eta), stored


def dilate_mask(seed: np.ndarray, passable: np.ndarray, iterations: int) -> np.ndarray:
    out = np.asarray(seed, dtype=bool) & np.asarray(passable, dtype=bool)
    if iterations <= 0:
        return out
    for _ in range(int(iterations)):
        up = np.zeros_like(out); up[1:, :] = out[:-1, :]
        down = np.zeros_like(out); down[:-1, :] = out[1:, :]
        left = np.zeros_like(out); left[:, 1:] = out[:, :-1]
        right = np.zeros_like(out); right[:, :-1] = out[:, 1:]
        ul = np.zeros_like(out); ul[1:, 1:] = out[:-1, :-1]
        ur = np.zeros_like(out); ur[1:, :-1] = out[:-1, 1:]
        dl = np.zeros_like(out); dl[:-1, 1:] = out[1:, :-1]
        dr = np.zeros_like(out); dr[:-1, :-1] = out[1:, 1:]
        out = (out | up | down | left | right | ul | ur | dl | dr) & passable
    return out


def polyline_buffer_mask(
    x: np.ndarray,
    y: np.ndarray,
    lines: Sequence[Sequence[tuple[float, float]]],
    radius_m: float,
) -> np.ndarray:
    xx, yy = np.meshgrid(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    mask = np.zeros(xx.shape, dtype=bool)
    r2 = float(radius_m) ** 2
    for line in lines:
        pts = np.asarray(list(line), dtype=float)
        if pts.ndim != 2 or pts.shape[0] == 0:
            continue
        for i in range(pts.shape[0] - 1):
            x0, y0 = float(pts[i, 0]), float(pts[i, 1])
            x1, y1 = float(pts[i + 1, 0]), float(pts[i + 1, 1])
            vx, vy = x1 - x0, y1 - y0
            len2 = vx * vx + vy * vy
            if len2 <= 1.0e-6:
                mask |= (xx - x0) ** 2 + (yy - y0) ** 2 <= r2
                continue
            tpar = np.clip(((xx - x0) * vx + (yy - y0) * vy) / len2, 0.0, 1.0)
            px = x0 + tpar * vx
            py = y0 + tpar * vy
            mask |= (xx - px) ** 2 + (yy - py) ** 2 <= r2
    return mask


def hydrologic_fill_order(
    elevation: np.ndarray,
    seed: np.ndarray,
    passable: np.ndarray,
    active_fill: np.ndarray,
    conveyance: np.ndarray | None = None,
) -> np.ndarray:
    z = np.asarray(elevation, dtype=float)
    seed_m = np.asarray(seed, dtype=bool)
    walk = np.asarray(passable, dtype=bool) | seed_m
    fill = np.asarray(active_fill, dtype=bool)
    conv = np.zeros(z.shape, dtype=bool) if conveyance is None else np.asarray(conveyance, dtype=bool)
    ny, nx = z.shape
    visited = np.zeros(z.shape, dtype=bool)
    heap: list[tuple[float, int, int]] = []
    for r, c in zip(*np.nonzero(seed_m)):
        visited[int(r), int(c)] = True
        heapq.heappush(heap, (float(z[int(r), int(c)]), int(r), int(c)))
    rows: list[int] = []
    cols: list[int] = []
    nbrs = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))
    while heap:
        level, r, c = heapq.heappop(heap)
        if fill[r, c]:
            rows.append(r)
            cols.append(c)
        for dr, dc in nbrs:
            rr, cc = r + dr, c + dc
            if rr < 0 or rr >= ny or cc < 0 or cc >= nx:
                continue
            if visited[rr, cc] or not walk[rr, cc]:
                continue
            visited[rr, cc] = True
            nxt = level if conv[rr, cc] else max(level, float(z[rr, cc]))
            heapq.heappush(heap, (nxt, rr, cc))
    if not rows:
        return np.zeros((0, 2), dtype=int)
    return np.column_stack((rows, cols))


def classify_dongdian_mask(
    active_mask: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    dividing_line: Sequence[tuple[float, float]],
) -> tuple[np.ndarray, np.ndarray]:
    """Split an active mask by the 文安洼/东淀 dividing line.

    The line is extended across the grid. The east/north side (cross product < 0
    for the digitized westbound line) is 东淀; the opposite side is 文安洼.
    """
    active = np.asarray(active_mask, dtype=bool)
    pts = tuple(dividing_line)
    if len(pts) < 2:
        raise ValueError("dividing_line must contain at least two points")
    x0, y0 = float(pts[0][0]), float(pts[0][1])
    x1, y1 = float(pts[-1][0]), float(pts[-1][1])
    dx = x1 - x0
    dy = y1 - y0
    xx, yy = np.meshgrid(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    cross = dx * (yy - y0) - dy * (xx - x0)
    dongdian = active & (cross < 0.0)
    wenanwa = active & ~dongdian
    return dongdian, wenanwa


def _river_northing(
    lines: Sequence[Sequence[tuple[float, float]]],
    x: np.ndarray,
) -> np.ndarray:
    pts: list[tuple[float, float]] = []
    for line in lines:
        pts.extend((float(px), float(py)) for px, py in line)
    if len(pts) < 2:
        raise ValueError("daqing river needs at least two vertices")
    xp = np.asarray([item[0] for item in pts], dtype=float)
    fp = np.asarray([item[1] for item in pts], dtype=float)
    order = np.argsort(xp, kind="mergesort")
    xp = xp[order]
    fp = fp[order]
    ux, inv = np.unique(np.round(xp, 3), return_inverse=True)
    uy = np.zeros(ux.shape, dtype=float)
    cnt = np.zeros(ux.shape, dtype=float)
    for i, yv in zip(inv, fp):
        uy[i] += yv
        cnt[i] += 1
    uy /= np.maximum(cnt, 1.0)
    x = np.asarray(x, dtype=float)
    northing = np.interp(x, ux, uy)
    if ux.size >= 2:
        i1 = 1
        while i1 < ux.size and abs(ux[i1] - ux[0]) < 1.0:
            i1 += 1
        if i1 < ux.size:
            slope_l = (uy[i1] - uy[0]) / (ux[i1] - ux[0])
            northing = np.where(x < ux[0], uy[0] + slope_l * (x - ux[0]), northing)
        i0 = int(ux.size) - 2
        while i0 >= 0 and abs(ux[-1] - ux[i0]) < 1.0:
            i0 -= 1
        if i0 >= 0:
            slope_r = (uy[-1] - uy[i0]) / (ux[-1] - ux[i0])
            northing = np.where(x > ux[-1], uy[-1] + slope_r * (x - ux[-1]), northing)
    return northing


def classify_qingbei_mask(
    active_mask: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    dividing_line: Sequence[tuple[float, float]],
    elevation: np.ndarray,
    daqing_river: Sequence[Sequence[tuple[float, float]]] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Keep Dongdian cells north of the Daqing River as Qingbei.

    Wenanwa is removed with the Dongdian/Wenanwa dividing line. When a Daqing
    river polyline is supplied, Qingbei is strictly north of that track and
    Qingnan stays inactive. Taitou high ground remains in the mask and is
    left dry by water level. Without a river track the older elevation-ridge
    fallback is used.
    """
    dongdian, _wenanwa = classify_dongdian_mask(active_mask, x, y, dividing_line)
    z = np.asarray(elevation, dtype=float)
    if z.shape != dongdian.shape:
        raise ValueError("elevation shape must match active_mask")
    xx = np.asarray(x, dtype=float)
    yy = np.asarray(y, dtype=float)
    _, grid_y = np.meshgrid(xx, yy)
    if not np.any(dongdian):
        return dongdian, np.asarray(active_mask, dtype=bool) & ~dongdian
    if daqing_river:
        river_y = _river_northing(daqing_river, xx)
        qingbei = dongdian & (grid_y > river_y[None, :])
        excluded = np.asarray(active_mask, dtype=bool) & ~qingbei
        return qingbei, excluded
    ys = grid_y[dongdian]
    y_min = float(np.min(ys))
    y_max = float(np.max(ys))
    span = max(y_max - y_min, 1.0)
    y_lo = y_min + 0.38 * span
    y_hi = y_min + 0.52 * span
    band = dongdian & (grid_y >= y_lo) & (grid_y <= y_hi)
    ridge = np.full(xx.size, np.nan)
    for i in range(xx.size):
        col = band[:, i]
        if int(col.sum()) >= 1:
            ridge[i] = yy[col][int(np.argmax(z[col, i]))]
    valid = np.isfinite(ridge)
    if not np.any(valid):
        cut = float(np.median(ys))
        qingbei = dongdian & (grid_y > cut)
    else:
        idx = np.flatnonzero(valid)
        ridge = np.interp(np.arange(xx.size), idx, ridge[idx])
        kernel = min(15, max(3, (int(idx.size) // 8) * 2 + 1))
        pad = np.pad(ridge, (kernel // 2, kernel // 2), mode="edge")
        ridge = np.convolve(pad, np.ones(kernel) / kernel, mode="valid")
        med = float(np.median(ridge))
        ridge = np.maximum(ridge, med)
        col_ymax = np.max(np.where(dongdian, grid_y, -np.inf), axis=0)
        western_basin = np.any(dongdian, axis=0) & (col_ymax <= y_hi)
        qingbei = dongdian & (
            western_basin[None, :] | (grid_y > ridge[None, :])
        )
    excluded = np.asarray(active_mask, dtype=bool) & ~qingbei
    return qingbei, excluded


@dataclass(frozen=True)
class HourlyState:
    time: float
    depth: np.ndarray
    eta: float
    volume: float
    wet_area: float
    inflow_m3s: float
    outflow_m3s: float
    overflow_m3s: float
    rain_mm: float
    velocity_x: np.ndarray
    velocity_y: np.ndarray
    discharge: np.ndarray


class HourlyInundationSolver:
    def __init__(
        self,
        elevation: np.ndarray,
        active_mask: np.ndarray,
        *,
        dx: float,
        dy: float,
        xmin: float,
        ymin: float,
        manning_n: float = 0.035,
        eta_crest: float | None = None,
        min_depth: float = 0.001,
        gravity: float = 9.81,
        inlet_mask: np.ndarray | None = None,
        water_surface_slope: float | None = None,
        conveyance_mask: np.ndarray | None = None,
        max_spread_m_s: float | None = None,
        area_schedule: Sequence[tuple[float, float]] | None = None,
    ) -> None:
        self.elevation = np.asarray(elevation, dtype=float)
        self.active_mask = np.asarray(active_mask, dtype=bool)
        if self.elevation.shape != self.active_mask.shape:
            raise ValueError("elevation and active_mask shapes must match")
        self.ny, self.nx = self.elevation.shape
        self.dx = float(dx)
        self.dy = float(dy)
        self.xmin = float(xmin)
        self.ymin = float(ymin)
        self.cell_area = self.dx * self.dy
        self.manning_n = float(manning_n)
        self.min_depth = float(min_depth)
        self.gravity = float(gravity)
        self.eta_crest = eta_crest
        self.volume = 0.0
        self.time = 0.0
        self.depth = np.zeros_like(self.elevation)
        self.eta = float(self.elevation[self.active_mask].min()) if np.any(self.active_mask) else 0.0
        self.maximum_depth = np.zeros_like(self.elevation)
        self.maximum_velocity = np.zeros_like(self.elevation)
        self.arrival_time = np.full_like(self.elevation, -1.0)
        cols = np.where(self.active_mask)[1]
        self._inlet_col = int(cols.min()) if cols.size else 0
        self._outlet_col = int(cols.max()) if cols.size else self.nx - 1
        if inlet_mask is None:
            self.inlet_mask = None
        else:
            self.inlet_mask = np.asarray(inlet_mask, dtype=bool)
            if self.inlet_mask.shape != self.active_mask.shape:
                raise ValueError("inlet_mask shape must match active_mask")
            if not np.any(self.inlet_mask):
                raise ValueError("inlet_mask is empty")
        if water_surface_slope is not None and float(water_surface_slope) <= 0.0:
            raise ValueError("water_surface_slope must be positive")
        self.water_surface_slope = None if water_surface_slope is None else float(water_surface_slope)
        if conveyance_mask is None:
            self.conveyance_mask = None
        else:
            self.conveyance_mask = np.asarray(conveyance_mask, dtype=bool)
            if self.conveyance_mask.shape != self.active_mask.shape:
                raise ValueError("conveyance_mask shape must match active_mask")
        if max_spread_m_s is not None and float(max_spread_m_s) <= 0.0:
            raise ValueError("max_spread_m_s must be positive")
        self.max_spread_m_s = None if max_spread_m_s is None else float(max_spread_m_s)
        self._reachable = None if self.inlet_mask is None else self.inlet_mask.copy()
        self._fill_order = None
        if area_schedule is None:
            self._area_hours = None
            self._area_fracs = None
        else:
            hrs = [float(item[0]) for item in area_schedule]
            frs = [float(item[1]) for item in area_schedule]
            if len(hrs) < 2:
                raise ValueError("area_schedule needs at least two points")
            self._area_hours = np.asarray(hrs, dtype=float)
            self._area_fracs = np.clip(np.asarray(frs, dtype=float), 0.0, 1.0)


    def _crest_capacity(self, mask: np.ndarray) -> float:
        if self.eta_crest is None or not np.any(mask):
            return float("inf")
        return float(np.maximum(self.eta_crest - self.elevation[mask], 0.0).sum() * self.cell_area)

    def _apply_volume(self, volume: float, fill_mask: np.ndarray | None = None) -> tuple[float, float]:
        mask = self.active_mask if fill_mask is None else (self.active_mask & np.asarray(fill_mask, dtype=bool))
        total = max(0.0, volume)
        overflow = 0.0
        full_cap = self._crest_capacity(self.active_mask)
        if total > full_cap:
            overflow = total - full_cap
            total = full_cap
        reach_cap = self._crest_capacity(mask)
        stored = min(total, reach_cap)
        self.depth, self.eta, in_cell = fill_volume(
            self.elevation, mask, stored, self.cell_area
        )
        self.depth[~mask] = 0.0
        if self.eta_crest is not None and in_cell >= reach_cap and np.any(mask):
            self.eta = float(self.eta_crest)
            self.depth[mask] = np.maximum(self.eta_crest - self.elevation[mask], 0.0)
            in_cell = float(self.depth[mask].sum() * self.cell_area)
        self.volume = total
        self.depth_volume = in_cell
        return in_cell, overflow

    def _velocity(self, inflow_m3s: float, outflow_m3s: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        h = np.maximum(self.depth, 0.0)
        wet = self.active_mask & (h >= self.min_depth)
        u = np.zeros_like(h)
        v = np.zeros_like(h)
        if not np.any(wet):
            return u, v, np.zeros_like(h)
        eta = np.where(self.active_mask, self.elevation + h, np.nan)
        deta_dx = np.zeros_like(h)
        deta_dy = np.zeros_like(h)
        if self.nx > 1:
            deta_dx[:, 1:-1] = (eta[:, 2:] - eta[:, :-2]) / (2.0 * self.dx)
            deta_dx[:, 0] = (eta[:, 1] - eta[:, 0]) / self.dx
            deta_dx[:, -1] = (eta[:, -1] - eta[:, -2]) / self.dx
        if self.ny > 1:
            deta_dy[1:-1, :] = (eta[2:, :] - eta[:-2, :]) / (2.0 * self.dy)
            deta_dy[0, :] = (eta[1, :] - eta[0, :]) / self.dy
            deta_dy[-1, :] = (eta[-1, :] - eta[-2, :]) / self.dy
        deta_dx = np.nan_to_num(deta_dx, nan=0.0)
        deta_dy = np.nan_to_num(deta_dy, nan=0.0)
        slope = np.hypot(deta_dx, deta_dy)
        speed = np.zeros_like(h)
        speed[wet] = (1.0 / self.manning_n) * np.power(h[wet], 2.0 / 3.0) * np.sqrt(np.maximum(slope[wet], 0.0))
        nonzero = wet & (slope > 0.0)
        u[nonzero] = -deta_dx[nonzero] / slope[nonzero] * speed[nonzero]
        v[nonzero] = -deta_dy[nonzero] / slope[nonzero] * speed[nonzero]
        inlet = wet & (np.arange(self.nx)[None, :] <= self._inlet_col + 2)
        outlet = wet & (np.arange(self.nx)[None, :] >= self._outlet_col - 2)
        inlet_width = max(float(np.count_nonzero(inlet)) * self.dy, self.dy)
        outlet_width = max(float(np.count_nonzero(outlet)) * self.dy, self.dy)
        if inflow_m3s > 0.0 and np.any(inlet):
            u[inlet] = np.maximum(u[inlet], inflow_m3s / (inlet_width * np.maximum(h[inlet], self.min_depth)))
        if outflow_m3s > 0.0 and np.any(outlet):
            u[outlet] = np.maximum(u[outlet], outflow_m3s / (outlet_width * np.maximum(h[outlet], self.min_depth)))
        u = np.clip(u, -5.0, 5.0)
        v = np.clip(v, -5.0, 5.0)
        u[~wet] = 0.0
        v[~wet] = 0.0
        discharge = h * u * self.dy
        discharge[~wet] = 0.0
        return u, v, discharge

    def step(self, *, inflow_m3s: float, outflow_m3s: float, rain_mm: float, dt_s: float) -> HourlyState:
        if dt_s <= 0.0:
            raise ValueError("dt_s must be positive")
        rain_m = max(float(rain_mm), 0.0) / 1000.0
        rain_volume = rain_m * float(np.count_nonzero(self.active_mask)) * self.cell_area
        added = float(inflow_m3s) * dt_s + rain_volume - float(outflow_m3s) * dt_s
        target = max(0.0, self.volume + added)
        fill_mask = None
        if self.inlet_mask is not None and self._area_hours is not None:
            if self._fill_order is None:
                if self.eta_crest is None:
                    eta_limit = float(np.max(self.elevation[self.active_mask]))
                else:
                    eta_limit = float(self.eta_crest)
                walk = self.active_mask & (self.elevation < eta_limit)
                walk |= self.inlet_mask
                if self.conveyance_mask is not None:
                    walk |= self.conveyance_mask
                self._fill_order = hydrologic_fill_order(
                    self.elevation, self.inlet_mask, walk, self.active_mask, self.conveyance_mask
                )
            hour = (self.time + dt_s) / 3600.0
            frac = float(np.interp(hour, self._area_hours, self._area_fracs, left=0.0, right=1.0))
            nkeep = int(round(frac * int(self._fill_order.shape[0])))
            fill_mask = np.zeros_like(self.active_mask)
            if nkeep > 0:
                pick = self._fill_order[:nkeep]
                fill_mask[pick[:, 0], pick[:, 1]] = True
            self._reachable = fill_mask
        elif self.inlet_mask is not None:
            wet = self.active_mask & (self.depth >= self.min_depth)
            seed = self.inlet_mask | wet
            if self._reachable is not None:
                seed |= self._reachable
            if np.any(wet):
                h_char = float(np.mean(self.depth[wet]))
            else:
                h_char = 0.3
            h_char = max(h_char, 0.3)
            slope = self.water_surface_slope if self.water_surface_slope is not None else 1.0e-5
            speed = (1.0 / self.manning_n) * (h_char ** (2.0 / 3.0)) * (slope ** 0.5)
            if self.max_spread_m_s is not None:
                speed = min(speed, self.max_spread_m_s)
            cell = min(self.dx, self.dy)
            n_iter = max(1, int(np.ceil(speed * dt_s / cell)))
            if self.eta_crest is None:
                eta_limit = float(np.max(self.elevation[self.active_mask]))
            else:
                eta_limit = float(self.eta_crest)
            passable = self.active_mask & (self.elevation < eta_limit)
            passable |= self.inlet_mask
            if self.conveyance_mask is not None:
                passable |= self.conveyance_mask
            self._reachable = dilate_mask(seed, passable, n_iter)
            fill_mask = self._reachable & self.active_mask
        stored, overflow_volume = self._apply_volume(target, fill_mask)
        overflow_m3s = overflow_volume / dt_s
        self.time += dt_s
        u, v, discharge = self._velocity(inflow_m3s, outflow_m3s)
        mag = np.hypot(u, v)
        self.maximum_depth = np.maximum(self.maximum_depth, self.depth)
        self.maximum_velocity = np.maximum(self.maximum_velocity, mag)
        newly = (self.arrival_time < 0.0) & (self.depth >= self.min_depth)
        self.arrival_time[newly] = self.time
        wet_area = float(np.count_nonzero(self.active_mask & (self.depth >= self.min_depth)) * self.cell_area)
        return HourlyState(
            time=self.time,
            depth=self.depth.copy(),
            eta=self.eta,
            volume=self.volume,
            wet_area=wet_area,
            inflow_m3s=float(inflow_m3s),
            outflow_m3s=float(outflow_m3s),
            overflow_m3s=overflow_m3s,
            rain_mm=float(rain_mm),
            velocity_x=u,
            velocity_y=v,
            discharge=discharge,
        )


def write_hourly_netcdf(
    path: str | Path,
    solver: HourlyInundationSolver,
    *,
    times_s: Sequence[float],
    inflow: Sequence[float],
    outflow: Sequence[float],
    rain_mm: Sequence[float],
    coordinate_system: str,
    vertical_datum: str,
    overwrite: bool = True,
    extra_attrs: dict | None = None,
    gate_dispatch: GateDispatchInput | None = None,
) -> Path:
    if not (len(times_s) == len(inflow) == len(outflow) == len(rain_mm)):
        raise ValueError("times, inflow, outflow and rain series must have the same length")
    if len(times_s) < 2:
        raise ValueError("at least two output times are required")
    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(target)
    origin = (extra_attrs or {}).get("event_start") if times_s[0] == 0 else None
    dispatch_log = build_gate_dispatch_log(times_s, gate_dispatch, time_origin=origin)
    check_gate_dispatch_paths(target, overwrite=overwrite)
    try:
        import netCDF4
    except ImportError as exc:
        raise OutputDependencyError("hourly NetCDF writing requires optional dependency 'netCDF4'") from exc

    times = [float(t) for t in times_s]
    target.parent.mkdir(parents=True, exist_ok=True)
    ny, nx = solver.ny, solver.nx
    y = solver.ymin + (np.arange(ny) + 0.5) * solver.dy
    x = solver.xmin + (np.arange(nx) + 0.5) * solver.dx

    with netCDF4.Dataset(str(target), "w") as ds:
        ds.createDimension("time", len(times))
        ds.createDimension("y", ny)
        ds.createDimension("x", nx)
        tv = ds.createVariable("time", "f8", ("time",))
        yv = ds.createVariable("y", "f8", ("y",))
        xv = ds.createVariable("x", "f8", ("x",))
        tv[:] = times
        tv.units = "s"
        yv[:] = y
        xv[:] = x
        chunks = (1, ny, nx)
        fields = {
            "water_depth": ("m", None),
            "water_level": ("m", vertical_datum),
            "velocity_x": ("m/s", None),
            "velocity_y": ("m/s", None),
            "velocity_magnitude": ("m/s", None),
            "discharge": ("m3/s", None),
        }
        vars_ = {}
        for name, (units, datum) in fields.items():
            var = ds.createVariable(name, "f4", ("time", "y", "x"), zlib=True, complevel=4, chunksizes=chunks, fill_value=0.0)
            var.units = units
            if datum:
                var.vertical_datum = datum
            vars_[name] = var
        elev = ds.createVariable("elevation", "f4", ("y", "x"), zlib=True, complevel=4)
        elev.units = "m"
        elev[:] = solver.elevation
        am = ds.createVariable("active_mask", "i1", ("y", "x"), zlib=True)
        am[:] = solver.active_mask.astype(np.int8)
        max_h = ds.createVariable("maximum_depth", "f4", ("y", "x"), zlib=True, complevel=4)
        max_h.units = "m"
        max_v = ds.createVariable("maximum_velocity", "f4", ("y", "x"), zlib=True, complevel=4)
        max_v.units = "m/s"
        arr = ds.createVariable("arrival_time", "f4", ("y", "x"), zlib=True, complevel=4)
        arr.units = "s"
        qin = ds.createVariable("inflow", "f8", ("time",))
        qin.units = "m3/s"
        qout = ds.createVariable("outflow", "f8", ("time",))
        qout.units = "m3/s"
        rain = ds.createVariable("rainfall", "f8", ("time",))
        rain.units = "mm"
        eta = ds.createVariable("eta", "f8", ("time",))
        eta.units = "m"
        vol = ds.createVariable("volume", "f8", ("time",))
        vol.units = "m3"
        area = ds.createVariable("wet_area", "f8", ("time",))
        area.units = "m2"

        depth0 = np.zeros((ny, nx), dtype=np.float32)
        vars_["water_depth"][0] = depth0
        vars_["water_level"][0] = np.where(solver.active_mask, solver.elevation, 0.0).astype(np.float32)
        zeros = np.zeros((ny, nx), dtype=np.float32)
        vars_["velocity_x"][0] = zeros
        vars_["velocity_y"][0] = zeros
        vars_["velocity_magnitude"][0] = zeros
        vars_["discharge"][0] = zeros
        qin[0] = float(inflow[0])
        qout[0] = float(outflow[0])
        rain[0] = float(rain_mm[0])
        eta[0] = solver.eta
        vol[0] = solver.volume
        area[0] = 0.0

        for i in range(1, len(times)):
            dt = times[i] - times[i - 1]
            state = solver.step(
                inflow_m3s=float(inflow[i]),
                outflow_m3s=float(outflow[i]),
                rain_mm=float(rain_mm[i]),
                dt_s=dt,
            )
            vars_["water_depth"][i] = state.depth.astype(np.float32)
            level = np.where(solver.active_mask, solver.elevation + state.depth, 0.0)
            vars_["water_level"][i] = level.astype(np.float32)
            vars_["velocity_x"][i] = state.velocity_x.astype(np.float32)
            vars_["velocity_y"][i] = state.velocity_y.astype(np.float32)
            vars_["velocity_magnitude"][i] = np.hypot(state.velocity_x, state.velocity_y).astype(np.float32)
            vars_["discharge"][i] = state.discharge.astype(np.float32)
            qin[i] = state.inflow_m3s
            qout[i] = state.outflow_m3s + state.overflow_m3s
            rain[i] = state.rain_mm
            eta[i] = state.eta
            vol[i] = state.volume
            area[i] = state.wet_area

        max_h[:] = solver.maximum_depth.astype(np.float32)
        max_v[:] = solver.maximum_velocity.astype(np.float32)
        arr[:] = solver.arrival_time.astype(np.float32)
        ds.format = "2d-hydrodynamics-netcdf"
        ds.version = "1.0"
        ds.grid_spacing = f"{solver.dx},{solver.dy}"
        ds.origin = f"{solver.xmin},{solver.ymin}"
        ds.coordinate_system = coordinate_system
        ds.vertical_datum = vertical_datum
        ds.time_step_s = float(times[1] - times[0])
        ds.note = (
            "Hourly hydrostatic DEM fill for detention-basin replay. "
            "Not a CFL-limited HLL shallow-water integration."
        )
        if extra_attrs:
            for key, value in extra_attrs.items():
                setattr(ds, key, value)
        csv_path, json_path = gate_dispatch_paths(target)
        ds.gate_dispatch_csv = csv_path.name
        ds.gate_dispatch_json = json_path.name
        ds.gate_dispatch_reported_operation_count = len(dispatch_log.inputs.operations)
        if dispatch_log.inputs.time_origin:
            ds.gate_dispatch_time_origin = dispatch_log.inputs.time_origin
    write_gate_dispatch_log(target, dispatch_log, overwrite=overwrite)
    return target


__all__ = [
    "HourlyInundationSolver",
    "HourlyState",
    "classify_dongdian_mask",
    "classify_qingbei_mask",
    "dilate_mask",
    "hydrologic_fill_order",
    "polyline_buffer_mask",
    "fill_volume",
    "write_hourly_netcdf",
]
