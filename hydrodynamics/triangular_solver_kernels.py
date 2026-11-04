"""Vectorised kernels for the experimental first-order triangular SWE scheme.

The scheme is unchanged from the original per-edge Python loop in
:mod:`triangular_solver`: cell-centred first-order HLL fluxes with Audusse
hydrostatic reconstruction, impermeable reflection on closed walls, and the
frozen per-face wave-CFL bound ``cfl * area / sum(L_e * a_e)``.

Every kernel reproduces the scalar loop's binary64 operation order:

* per-edge arithmetic is written with the same association as the scalar code;
* face accumulations use :func:`numpy.bincount` over an interleaved
  ``[left_0, right_0, left_1, right_1, ...]`` index stream, which adds in the
  same sequential edge order as the scalar ``derivative[i] += ...`` loop.

An optional numba kernel (used only if numba imports) is a compiled copy of the
scalar loop; numpy is always the fallback, numba is never a hard dependency.

Open boundaries are not handled here: every exterior edge is a reflecting
wall in these kernels.  Prescribed-discharge boundaries are added by the caller
as additive per-face terms (see :mod:`triangular_long_run`).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Sequence

import numpy as np


@dataclass(frozen=True, eq=False)
class EdgeGeometry:
    """Precomputed unique-edge arrays for one mesh (all SI units)."""

    n_face: int
    left: np.ndarray          # int64 (n_edge,), face on the normal's tail side
    right: np.ndarray         # int64 (n_edge,), -1 for exterior edges
    right_safe: np.ndarray    # int64, exterior edges point back to ``left``
    interior: np.ndarray      # bool (n_edge,)
    nx: np.ndarray            # float64 unit normal, oriented left -> right / outward
    ny: np.ndarray
    length: np.ndarray        # float64 edge length, m
    area: np.ndarray          # float64 (n_face,), m2
    left_factor: np.ndarray   # length / area[left], 1/m
    right_factor: np.ndarray  # length / area[right_safe], 1/m
    scatter_index: np.ndarray  # int64 interleaved left/right accumulation stream
    scatter_mask: np.ndarray   # bool (2*n_edge,), used slots of interleaved stream

    @property
    def n_edge(self) -> int:
        return int(self.left.size)


def edge_geometry_from_edges(edges: Sequence[Any], face_area: Sequence[float]) -> EdgeGeometry:
    """Build arrays from ``_Edge`` records (index/left/right/nx/ny/length)."""
    count = len(edges)
    left = np.fromiter((edge.left for edge in edges), dtype=np.int64, count=count)
    right = np.fromiter((-1 if edge.right is None else edge.right for edge in edges), dtype=np.int64, count=count)
    nx = np.fromiter((edge.nx for edge in edges), dtype=np.float64, count=count)
    ny = np.fromiter((edge.ny for edge in edges), dtype=np.float64, count=count)
    length = np.fromiter((edge.length for edge in edges), dtype=np.float64, count=count)
    area = np.asarray(face_area, dtype=np.float64).copy()
    interior = right >= 0
    right_safe = np.where(interior, right, left)
    interleaved = np.empty(2 * count, dtype=np.int64)
    interleaved[0::2] = left
    interleaved[1::2] = right_safe
    mask = np.ones(2 * count, dtype=bool)
    mask[1::2] = interior
    for array in (left, right, right_safe, interior, nx, ny, length, area):
        array.setflags(write=False)
    geometry = EdgeGeometry(
        n_face=int(area.size), left=left, right=right, right_safe=right_safe, interior=interior,
        nx=nx, ny=ny, length=length, area=area,
        left_factor=length / area[left], right_factor=length / area[right_safe],
        scatter_index=interleaved[mask], scatter_mask=mask,
    )
    return geometry


def _scatter(geometry: EdgeGeometry, left_values: np.ndarray, right_values: np.ndarray) -> np.ndarray:
    buffer = np.empty(2 * geometry.n_edge, dtype=np.float64)
    buffer[0::2] = left_values
    buffer[1::2] = right_values
    return np.bincount(geometry.scatter_index, weights=buffer[geometry.scatter_mask], minlength=geometry.n_face)


@dataclass
class RhsResult:
    derivative: np.ndarray       # (n_face, 3) d/dt of (h, hu, hv)
    cfl_limit_s: float           # cfl * min(area / wave_sums) over faces with waves
    maximum_pair_residual_m3_s: float
    wave_sums: np.ndarray        # (n_face,) sum_e L_e a_e, m2/s


def rhs_numpy(
    state: np.ndarray,
    bed_m: np.ndarray,
    geometry: EdgeGeometry,
    *,
    gravity: float,
    cfl: float,
    dry_depth_m: float,
) -> RhsResult:
    """Vectorised unique-edge residual identical to the scalar reference loop."""
    h = state[:, 0]
    wet = h > dry_depth_m
    u_face = np.zeros(geometry.n_face, dtype=np.float64)
    v_face = np.zeros(geometry.n_face, dtype=np.float64)
    np.divide(state[:, 1], h, out=u_face, where=wet)
    np.divide(state[:, 2], h, out=v_face, where=wet)
    c_face = np.sqrt(gravity * np.maximum(h, 0.0))
    left, right, interior = geometry.left, geometry.right_safe, geometry.interior
    nx, ny = geometry.nx, geometry.ny

    h_l = h[left]
    u_l = u_face[left]
    v_l = v_face[left]
    un_l = u_l * nx + v_l * ny
    c_l = c_face[left]
    z_l = bed_m[left]
    h_r = h[right]
    u_r = np.where(interior, u_face[right], u_l - 2.0 * un_l * nx)
    v_r = np.where(interior, v_face[right], v_l - 2.0 * un_l * ny)
    un_r = np.where(interior, u_r * nx + v_r * ny, -un_l)
    c_r = c_face[right]
    z_r = bed_m[right]

    a_edge = np.maximum(np.abs(un_l) + c_l, np.abs(un_r) + c_r)
    weighted = geometry.length * a_edge
    wave_sums = _scatter(geometry, weighted, weighted)

    bed_star = np.maximum(z_l, z_r)
    h_l_star = np.maximum(0.0, h_l + z_l - bed_star)
    h_r_star = np.maximum(0.0, h_r + z_r - bed_star)
    qx_l, qy_l = h_l_star * u_l, h_l_star * v_l
    qx_r, qy_r = h_r_star * u_r, h_r_star * v_r

    with np.errstate(divide="ignore", invalid="ignore"):
        pos_l = h_l_star > 0.0
        pos_r = h_r_star > 0.0
        us_l = np.where(pos_l, qx_l / h_l_star, 0.0)
        vs_l = np.where(pos_l, qy_l / h_l_star, 0.0)
        us_r = np.where(pos_r, qx_r / h_r_star, 0.0)
        vs_r = np.where(pos_r, qy_r / h_r_star, 0.0)
        uns_l = np.where(pos_l, us_l * nx + vs_l * ny, 0.0)
        uns_r = np.where(pos_r, us_r * nx + vs_r * ny, 0.0)
        cs_l = np.where(pos_l, np.sqrt(gravity * h_l_star), 0.0)
        cs_r = np.where(pos_r, np.sqrt(gravity * h_r_star), 0.0)
        pressure_l = 0.5 * gravity * h_l_star * h_l_star
        pressure_r = 0.5 * gravity * h_r_star * h_r_star
        fl0 = np.where(pos_l, h_l_star * uns_l, 0.0)
        fl1 = np.where(pos_l, qx_l * uns_l + pressure_l * nx, 0.0)
        fl2 = np.where(pos_l, qy_l * uns_l + pressure_l * ny, 0.0)
        fr0 = np.where(pos_r, h_r_star * uns_r, 0.0)
        fr1 = np.where(pos_r, qx_r * uns_r + pressure_r * nx, 0.0)
        fr2 = np.where(pos_r, qy_r * uns_r + pressure_r * ny, 0.0)
        speed_minus = np.minimum(np.minimum(0.0, uns_l - cs_l), uns_r - cs_r)
        speed_plus = np.maximum(np.maximum(0.0, uns_l + cs_l), uns_r + cs_r)
        denominator = speed_plus - speed_minus
        product = speed_minus * speed_plus
        hll0 = (speed_plus * fl0 - speed_minus * fr0 + product * (h_r_star - h_l_star)) / denominator
        hll1 = (speed_plus * fl1 - speed_minus * fr1 + product * (qx_r - qx_l)) / denominator
        hll2 = (speed_plus * fl2 - speed_minus * fr2 + product * (qy_r - qy_l)) / denominator
    both_dry = (h_l_star <= 0.0) & (h_r_star <= 0.0)
    use_left = ~both_dry & (speed_minus >= 0.0)
    use_right = ~both_dry & ~use_left & (speed_plus <= 0.0)
    use_hll = ~both_dry & ~use_left & ~use_right & (denominator > 0.0)
    flux0 = np.where(use_hll, hll0, 0.0)
    flux1 = np.where(use_hll, hll1, 0.0)
    flux2 = np.where(use_hll, hll2, 0.0)
    flux0 = np.where(use_left, fl0, np.where(use_right, fr0, flux0))
    flux1 = np.where(use_left, fl1, np.where(use_right, fr1, flux1))
    flux2 = np.where(use_left, fl2, np.where(use_right, fr2, flux2))
    # Reflection is exactly impermeable, including in binary64.
    flux0 = np.where(interior, flux0, 0.0)

    correction_l = 0.5 * gravity * (h_l * h_l - h_l_star * h_l_star)
    flux_lx = flux1 + correction_l * nx
    flux_ly = flux2 + correction_l * ny
    left_factor = geometry.left_factor
    left_mass_increment = -left_factor * flux0
    correction_r = 0.5 * gravity * (h_r * h_r - h_r_star * h_r_star)
    flux_rx = flux1 + correction_r * nx
    flux_ry = flux2 + correction_r * ny
    right_factor = geometry.right_factor
    right_mass_increment = right_factor * flux0
    derivative = np.empty_like(state)
    derivative[:, 0] = _scatter(geometry, left_mass_increment, right_mass_increment)
    derivative[:, 1] = _scatter(geometry, -(left_factor * flux_lx), right_factor * flux_rx)
    derivative[:, 2] = _scatter(geometry, -(left_factor * flux_ly), right_factor * flux_ry)

    area = geometry.area
    pair = np.abs(area[left] * left_mass_increment + area[right] * right_mass_increment)[interior]
    maximum_pair_residual = max(0.0, float(pair.max())) if pair.size else 0.0
    positive = wave_sums > 0.0
    cfl_limit = cfl * float(np.min(area[positive] / wave_sums[positive])) if positive.any() else math.inf
    return RhsResult(derivative, cfl_limit, maximum_pair_residual, wave_sums)


# --------------------------------------------------------------------------
# Optional numba kernels.
#
# Pass 1 (parallel over contiguous edge chunks) evaluates exactly the scalar
# per-edge arithmetic and stores each edge's left/right increments.  Pass 2
# (parallel over contiguous face chunks) adds a face's increments in ascending
# edge index, which is the order in which the sequential loop (for edge:
# derivative[left] += ...; derivative[right] += ...) visits that face.  The
# result is therefore bitwise identical to the scalar loop and independent of
# the thread count.  The only cross-chunk reductions are minima and maxima,
# which are exact in any order.
#
# The stage kernel fuses the Euler stage (or SSPRK2 average), the sanitation
# rules and the optional point-implicit Manning update into one parallel pass
# over faces.  Sanitation diagnostics that depend on face order (the first
# failing face and the sequential tiny-negative correction sum) are finished
# by a short serial scan over per-face flags, so they match the serial rules
# exactly.  All work arrays live in a :class:`NumbaWorkspace` so a step makes
# no large allocations.
# --------------------------------------------------------------------------
_NUMBA_KERNELS: Any = None
_NUMBA_STATUS: str | None = None


def _build_numba_kernel() -> Any:
    import numba  # noqa: PLC0415 - optional accelerator
    from numba import prange  # noqa: PLC0415

    @numba.njit(cache=True, fastmath=False, error_model="numpy", parallel=True)
    def edge_pass(state, bed, left, right, nx_all, ny_all, length_all, left_factor_all, right_factor_all,
                  area, gravity, dry_depth_m, out, chunks, pair_partial):
        # out columns: 0 left mass, 1 left x, 2 left y (both subtracted), 3 right
        # mass, 4 right x, 5 right y, 6 L*a, 7 internal-edge pair residual
        n_chunk = chunks.shape[0] - 1
        for c in prange(n_chunk):
            pair_max = 0.0
            for e in range(chunks[c], chunks[c + 1]):
                li = left[e]
                ri = right[e]
                nx = nx_all[e]
                ny = ny_all[e]
                h_l = state[li, 0]
                u_l = state[li, 1] / h_l if h_l > dry_depth_m else 0.0
                v_l = state[li, 2] / h_l if h_l > dry_depth_m else 0.0
                un_l = u_l * nx + v_l * ny
                c_l = math.sqrt(gravity * max(h_l, 0.0))
                if ri < 0:
                    h_r = h_l
                    u_r = u_l - 2.0 * un_l * nx
                    v_r = v_l - 2.0 * un_l * ny
                    un_r = -un_l
                    c_r = c_l
                    z_r = bed[li]
                else:
                    h_r = state[ri, 0]
                    u_r = state[ri, 1] / h_r if h_r > dry_depth_m else 0.0
                    v_r = state[ri, 2] / h_r if h_r > dry_depth_m else 0.0
                    un_r = u_r * nx + v_r * ny
                    c_r = math.sqrt(gravity * max(h_r, 0.0))
                    z_r = bed[ri]
                a_edge = max(abs(un_l) + c_l, abs(un_r) + c_r)
                out[e, 6] = length_all[e] * a_edge
                z_l = bed[li]
                bed_star = max(z_l, z_r)
                hls = max(0.0, h_l + z_l - bed_star)
                hrs = max(0.0, h_r + z_r - bed_star)
                qxl = hls * u_l
                qyl = hls * v_l
                qxr = hrs * u_r
                qyr = hrs * v_r
                if hls <= 0.0:
                    fl0 = 0.0
                    fl1 = 0.0
                    fl2 = 0.0
                    unls = 0.0
                    cls = 0.0
                else:
                    unls = (qxl / hls) * nx + (qyl / hls) * ny
                    cls = math.sqrt(gravity * hls)
                    fl0 = hls * unls
                    fl1 = qxl * unls + 0.5 * gravity * hls * hls * nx
                    fl2 = qyl * unls + 0.5 * gravity * hls * hls * ny
                if hrs <= 0.0:
                    fr0 = 0.0
                    fr1 = 0.0
                    fr2 = 0.0
                    unrs = 0.0
                    crs = 0.0
                else:
                    unrs = (qxr / hrs) * nx + (qyr / hrs) * ny
                    crs = math.sqrt(gravity * hrs)
                    fr0 = hrs * unrs
                    fr1 = qxr * unrs + 0.5 * gravity * hrs * hrs * nx
                    fr2 = qyr * unrs + 0.5 * gravity * hrs * hrs * ny
                if hls <= 0.0 and hrs <= 0.0:
                    f0 = 0.0
                    f1 = 0.0
                    f2 = 0.0
                else:
                    sm = min(0.0, unls - cls, unrs - crs)
                    sp = max(0.0, unls + cls, unrs + crs)
                    if sm >= 0.0:
                        f0 = fl0
                        f1 = fl1
                        f2 = fl2
                    elif sp <= 0.0:
                        f0 = fr0
                        f1 = fr1
                        f2 = fr2
                    else:
                        den = sp - sm
                        if den <= 0.0:
                            f0 = 0.0
                            f1 = 0.0
                            f2 = 0.0
                        else:
                            f0 = (sp * fl0 - sm * fr0 + sm * sp * (hrs - hls)) / den
                            f1 = (sp * fl1 - sm * fr1 + sm * sp * (qxr - qxl)) / den
                            f2 = (sp * fl2 - sm * fr2 + sm * sp * (qyr - qyl)) / den
                if ri < 0:
                    f0 = 0.0
                correction_l = 0.5 * gravity * (h_l * h_l - hls * hls)
                flx = f1 + correction_l * nx
                fly = f2 + correction_l * ny
                lf = left_factor_all[e]
                left_mass = -lf * f0
                out[e, 0] = left_mass
                out[e, 1] = lf * flx
                out[e, 2] = lf * fly
                if ri >= 0:
                    correction_r = 0.5 * gravity * (h_r * h_r - hrs * hrs)
                    frx = f1 + correction_r * nx
                    fry = f2 + correction_r * ny
                    rf = right_factor_all[e]
                    right_mass = rf * f0
                    out[e, 3] = right_mass
                    out[e, 4] = rf * frx
                    out[e, 5] = rf * fry
                    pair = abs(area[li] * left_mass + area[ri] * right_mass)
                    out[e, 7] = pair
                    # NaN-propagating maximum, as numpy.max.
                    if not (pair <= pair_max):
                        pair_max = pair
                else:
                    out[e, 3] = 0.0
                    out[e, 4] = 0.0
                    out[e, 5] = 0.0
                    out[e, 7] = 0.0
            pair_partial[c] = pair_max

    @numba.njit(cache=True, fastmath=False, error_model="numpy", parallel=True)
    def face_pass(face_edge, face_side, edge_values, area, derivative, wave_sums, chunks, ratio_partial):
        n_chunk = chunks.shape[0] - 1
        width = face_edge.shape[1]
        for c in prange(n_chunk):
            ratio_min = math.inf
            for f in range(chunks[c], chunks[c + 1]):
                d0 = 0.0
                d1 = 0.0
                d2 = 0.0
                w = 0.0
                for k in range(width):
                    e = face_edge[f, k]
                    if e < 0:
                        continue
                    w += edge_values[e, 6]
                    if face_side[f, k] == 0:
                        d0 += edge_values[e, 0]
                        d1 -= edge_values[e, 1]
                        d2 -= edge_values[e, 2]
                    else:
                        d0 += edge_values[e, 3]
                        d1 += edge_values[e, 4]
                        d2 += edge_values[e, 5]
                derivative[f, 0] = d0
                derivative[f, 1] = d1
                derivative[f, 2] = d2
                wave_sums[f] = w
                if w > 0.0:
                    ratio = area[f] / w
                    if not (ratio >= ratio_min):
                        ratio_min = ratio
            ratio_partial[c] = ratio_min

    @numba.njit(cache=True, fastmath=False, error_model="numpy", parallel=True)
    def face_rhs(state, bed, slot_face, slot_side, slot_geom, area, gravity, dry_depth_m, derivative, wave_sums,
                 chunks, ratio_partial, pair_partial):
        # Single-pass variant: every face evaluates its own edges (interior
        # edges are evaluated once from each side) with exactly the edge_pass
        # arithmetic, so no per-edge intermediate is stored.  slot_face: stored
        # neighbour index, -1 for a wall edge, -2 for an unused slot; slot_side
        # 0 if this face is the edge's left face; slot_geom [nx, ny, L, lf, rf].
        n_chunk = chunks.shape[0] - 1
        width = slot_face.shape[1]
        for c in prange(n_chunk):
            ratio_min = math.inf
            pair_max = 0.0
            for f in range(chunks[c], chunks[c + 1]):
                h_f = state[f, 0]
                u_f = state[f, 1] / h_f if h_f > dry_depth_m else 0.0
                v_f = state[f, 2] / h_f if h_f > dry_depth_m else 0.0
                c_f = math.sqrt(gravity * max(h_f, 0.0))
                z_f = bed[f]
                d0 = 0.0
                d1 = 0.0
                d2 = 0.0
                w = 0.0
                for k in range(width):
                    nb = slot_face[f, k]
                    if nb == -2:
                        continue
                    nx = slot_geom[f, k, 0]
                    ny = slot_geom[f, k, 1]
                    side = slot_side[f, k]
                    if side == 0:
                        h_l = h_f
                        u_l = u_f
                        v_l = v_f
                        c_l = c_f
                        z_l = z_f
                        un_l = u_l * nx + v_l * ny
                        if nb < 0:
                            h_r = h_l
                            u_r = u_l - 2.0 * un_l * nx
                            v_r = v_l - 2.0 * un_l * ny
                            un_r = -un_l
                            c_r = c_l
                            z_r = z_l
                        else:
                            h_r = state[nb, 0]
                            u_r = state[nb, 1] / h_r if h_r > dry_depth_m else 0.0
                            v_r = state[nb, 2] / h_r if h_r > dry_depth_m else 0.0
                            un_r = u_r * nx + v_r * ny
                            c_r = math.sqrt(gravity * max(h_r, 0.0))
                            z_r = bed[nb]
                    else:
                        h_l = state[nb, 0]
                        u_l = state[nb, 1] / h_l if h_l > dry_depth_m else 0.0
                        v_l = state[nb, 2] / h_l if h_l > dry_depth_m else 0.0
                        un_l = u_l * nx + v_l * ny
                        c_l = math.sqrt(gravity * max(h_l, 0.0))
                        z_l = bed[nb]
                        h_r = h_f
                        u_r = u_f
                        v_r = v_f
                        un_r = u_r * nx + v_r * ny
                        c_r = c_f
                        z_r = z_f
                    a_edge = max(abs(un_l) + c_l, abs(un_r) + c_r)
                    w += slot_geom[f, k, 2] * a_edge
                    bed_star = max(z_l, z_r)
                    hls = max(0.0, h_l + z_l - bed_star)
                    hrs = max(0.0, h_r + z_r - bed_star)
                    qxl = hls * u_l
                    qyl = hls * v_l
                    qxr = hrs * u_r
                    qyr = hrs * v_r
                    if hls <= 0.0:
                        fl0 = 0.0
                        fl1 = 0.0
                        fl2 = 0.0
                        unls = 0.0
                        cls = 0.0
                    else:
                        unls = (qxl / hls) * nx + (qyl / hls) * ny
                        cls = math.sqrt(gravity * hls)
                        fl0 = hls * unls
                        fl1 = qxl * unls + 0.5 * gravity * hls * hls * nx
                        fl2 = qyl * unls + 0.5 * gravity * hls * hls * ny
                    if hrs <= 0.0:
                        fr0 = 0.0
                        fr1 = 0.0
                        fr2 = 0.0
                        unrs = 0.0
                        crs = 0.0
                    else:
                        unrs = (qxr / hrs) * nx + (qyr / hrs) * ny
                        crs = math.sqrt(gravity * hrs)
                        fr0 = hrs * unrs
                        fr1 = qxr * unrs + 0.5 * gravity * hrs * hrs * nx
                        fr2 = qyr * unrs + 0.5 * gravity * hrs * hrs * ny
                    if hls <= 0.0 and hrs <= 0.0:
                        f0 = 0.0
                        f1 = 0.0
                        f2 = 0.0
                    else:
                        sm = min(0.0, unls - cls, unrs - crs)
                        sp = max(0.0, unls + cls, unrs + crs)
                        if sm >= 0.0:
                            f0 = fl0
                            f1 = fl1
                            f2 = fl2
                        elif sp <= 0.0:
                            f0 = fr0
                            f1 = fr1
                            f2 = fr2
                        else:
                            den = sp - sm
                            if den <= 0.0:
                                f0 = 0.0
                                f1 = 0.0
                                f2 = 0.0
                            else:
                                f0 = (sp * fl0 - sm * fr0 + sm * sp * (hrs - hls)) / den
                                f1 = (sp * fl1 - sm * fr1 + sm * sp * (qxr - qxl)) / den
                                f2 = (sp * fl2 - sm * fr2 + sm * sp * (qyr - qyl)) / den
                    if side == 0 and nb < 0:
                        f0 = 0.0
                    if side == 0:
                        correction_l = 0.5 * gravity * (h_l * h_l - hls * hls)
                        flx = f1 + correction_l * nx
                        fly = f2 + correction_l * ny
                        lf = slot_geom[f, k, 3]
                        left_mass = -lf * f0
                        d0 += left_mass
                        d1 -= lf * flx
                        d2 -= lf * fly
                        if nb >= 0:
                            rf = slot_geom[f, k, 4]
                            right_mass = rf * f0
                            pair = abs(area[f] * left_mass + area[nb] * right_mass)
                            if not (pair <= pair_max):
                                pair_max = pair
                    else:
                        correction_r = 0.5 * gravity * (h_r * h_r - hrs * hrs)
                        frx = f1 + correction_r * nx
                        fry = f2 + correction_r * ny
                        rf = slot_geom[f, k, 4]
                        d0 += rf * f0
                        d1 += rf * frx
                        d2 += rf * fry
                derivative[f, 0] = d0
                derivative[f, 1] = d1
                derivative[f, 2] = d2
                wave_sums[f] = w
                if w > 0.0:
                    ratio = area[f] / w
                    if not (ratio >= ratio_min):
                        ratio_min = ratio
            ratio_partial[c] = ratio_min
            pair_partial[c] = pair_max

    @numba.njit(cache=True, fastmath=False, error_model="numpy", parallel=True)
    def stage_fused(first, second, flux, source, boundary, rain, dt, mode, area, dry_depth_m, negative_tolerance_m,
                    manning_n, gravity, apply_friction, out, flags, chunks, count_partial, gamma_partial,
                    stage_flags, stage_depth, stage_count_partial,
                    do_maxima, time_s, max_depth, time_of_max, max_speed):
        # mode 0: out = first + dt * (((flux [+ source]) [+ boundary]) [+ rain on h])
        # mode 1: out = 0.5 * (first + second)
        # mode 2: s = sanitize(first + dt * (...)) with friction (the second
        #         SSPRK2 stage, flags in stage_flags, its tiny negative depths in
        #         stage_depth), then out = 0.5 * (second + s)  [second = step start]
        # Each stage applies the _sanitize_stage rules and, if apply_friction,
        # the point-implicit Manning update q <- q / (1 + dt g n^2 |q| / h^(7/3))
        # on wet faces (never on the final average).
        # flags: 0 ok, 1 tiny negative depth (left in out[f, 0] for the serial
        # correction scan), 2 significant negative depth, 3 non-finite value.
        # do_maxima: also fold ``first`` (an accepted state at time_s) into the
        # per-face maxima; idempotent, so repeating it for a retried step is safe.
        n_face = first.shape[0]
        n_chunk = chunks.shape[0] - 1
        has_source = source.shape[0] == n_face
        has_boundary = boundary.shape[0] == n_face
        for c in prange(n_chunk):
            n_nonfinite = 0
            n_negative = 0
            n_tiny = 0
            s_nonfinite = 0
            s_negative = 0
            s_tiny = 0
            gamma_max = 0.0
            for f in range(chunks[c], chunks[c + 1]):
                if do_maxima:
                    h0 = first[f, 0]
                    if h0 > max_depth[f]:
                        max_depth[f] = h0
                        time_of_max[f] = time_s
                    if h0 > dry_depth_m:
                        speed = math.hypot(first[f, 1] / h0, first[f, 2] / h0)
                        if speed > max_speed[f]:
                            max_speed[f] = speed
                flag = 0
                s0 = 0.0
                s1 = 0.0
                s2 = 0.0
                if mode == 0 or mode == 2:
                    sflag = 0
                    for comp in range(3):
                        total = flux[f, comp]
                        if has_source:
                            total = total + source[f, comp]
                        if has_boundary:
                            total = total + boundary[f, comp]
                        if comp == 0 and rain >= 0.0:
                            total = total + rain
                        value = first[f, comp] + dt * total
                        if not math.isfinite(value):
                            sflag = 3
                        if comp == 0:
                            s0 = value
                        elif comp == 1:
                            s1 = value
                        else:
                            s2 = value
                    if sflag == 3:
                        s_nonfinite += 1
                    else:
                        h = s0
                        if h < -negative_tolerance_m:
                            sflag = 2
                            s_negative += 1
                        else:
                            if h < 0.0:
                                sflag = 1
                                s_tiny += 1
                                if mode == 2:
                                    stage_depth[f] = h
                                    s0 = 0.0
                                h = 0.0
                            if h <= dry_depth_m:
                                s1 = 0.0
                                s2 = 0.0
                            elif apply_friction:
                                n = manning_n[f]
                                gamma = gravity * n * n * math.hypot(s1, s2) / (h * h * np.cbrt(h))
                                factor = 1.0 + dt * gamma
                                s1 = s1 / factor
                                s2 = s2 / factor
                                if math.isfinite(gamma) and gamma > gamma_max:
                                    gamma_max = gamma
                    if mode == 0:
                        out[f, 0] = s0
                        out[f, 1] = s1
                        out[f, 2] = s2
                        flags[f] = sflag
                        if sflag == 3:
                            n_nonfinite += 1
                        elif sflag == 2:
                            n_negative += 1
                        elif sflag == 1:
                            n_tiny += 1
                        continue
                    stage_flags[f] = sflag
                    v0 = 0.5 * (second[f, 0] + s0)
                    v1 = 0.5 * (second[f, 1] + s1)
                    v2 = 0.5 * (second[f, 2] + s2)
                else:
                    v0 = 0.5 * (first[f, 0] + second[f, 0])
                    v1 = 0.5 * (first[f, 1] + second[f, 1])
                    v2 = 0.5 * (first[f, 2] + second[f, 2])
                if not (math.isfinite(v0) and math.isfinite(v1) and math.isfinite(v2)):
                    flag = 3
                out[f, 0] = v0
                out[f, 1] = v1
                out[f, 2] = v2
                if flag == 3:
                    n_nonfinite += 1
                else:
                    h = v0
                    if h < -negative_tolerance_m:
                        flag = 2
                        n_negative += 1
                    else:
                        if h < 0.0:
                            flag = 1
                            n_tiny += 1
                            h = 0.0
                        if h <= dry_depth_m:
                            out[f, 1] = 0.0
                            out[f, 2] = 0.0
                flags[f] = flag
            count_partial[c, 0] = n_nonfinite
            count_partial[c, 1] = n_negative
            count_partial[c, 2] = n_tiny
            stage_count_partial[c, 0] = s_nonfinite
            stage_count_partial[c, 1] = s_negative
            stage_count_partial[c, 2] = s_tiny
            gamma_partial[c] = gamma_max

    @numba.njit(cache=True, fastmath=False, error_model="numpy")
    def sanitize_finish(depth, flags, area, scan_order, any_nonfinite, any_negative, clip):
        # Serial finish in mesh face order (scan_order[mesh face] = storage
        # index): first failing mesh face, or the sequential tiny-negative
        # correction sum exactly as the scalar rules.  ``depth`` holds the
        # unclipped tiny negative depths (clipped to 0 here when ``clip``).
        n_face = flags.shape[0]
        if any_nonfinite:
            for f in range(n_face):
                if flags[scan_order[f]] == 3:
                    return 1, f, 0.0, 0
        if any_negative:
            for f in range(n_face):
                if flags[scan_order[f]] == 2:
                    return 2, f, 0.0, 0
        correction = 0.0
        count = 0
        for f in range(n_face):
            i = scan_order[f]
            if flags[i] == 1:
                correction += -depth[i] * area[i]
                count += 1
                if clip:
                    depth[i] = 0.0
        return 0, -1, correction, count

    @numba.njit(cache=True, fastmath=False, error_model="numpy", parallel=True)
    def implicit_friction(state, manning_n, dt, gravity, dry_depth_m, chunks, gamma_partial):
        n_chunk = chunks.shape[0] - 1
        for c in prange(n_chunk):
            gamma_max = 0.0
            for f in range(chunks[c], chunks[c + 1]):
                h = state[f, 0]
                if h > dry_depth_m:
                    hx = state[f, 1]
                    hy = state[f, 2]
                    n = manning_n[f]
                    gamma = gravity * n * n * math.hypot(hx, hy) / (h * h * np.cbrt(h))
                    factor = 1.0 + dt * gamma
                    state[f, 1] = hx / factor
                    state[f, 2] = hy / factor
                    if math.isfinite(gamma) and gamma > gamma_max:
                        gamma_max = gamma
            gamma_partial[c] = gamma_max

    @numba.njit(cache=True, fastmath=False, error_model="numpy", parallel=True)
    def update_maxima(state, time_s, dry_depth_m, max_depth, time_of_max, max_speed, chunks):
        n_chunk = chunks.shape[0] - 1
        for c in prange(n_chunk):
            for f in range(chunks[c], chunks[c + 1]):
                h = state[f, 0]
                if h > max_depth[f]:
                    max_depth[f] = h
                    time_of_max[f] = time_s
                if h > dry_depth_m:
                    speed = math.hypot(state[f, 1] / h, state[f, 2] / h)
                    if speed > max_speed[f]:
                        max_speed[f] = speed

    return {
        "edge_pass": edge_pass, "face_pass": face_pass, "face_rhs": face_rhs, "stage_fused": stage_fused,
        "sanitize_finish": sanitize_finish, "implicit_friction": implicit_friction, "update_maxima": update_maxima,
        "threads": int(numba.get_num_threads()),
    }


_EMPTY = np.zeros((0, 3), dtype=np.float64)
_EMPTY_1D = np.zeros(0, dtype=np.float64)
_EMPTY_I8 = np.zeros(0, dtype=np.int8)


def numba_status() -> str:
    """Return ``"available"`` or a short reason why the numba kernel is unused."""
    global _NUMBA_KERNELS, _NUMBA_STATUS
    if _NUMBA_STATUS is None:
        try:
            _NUMBA_KERNELS = _build_numba_kernel()
            _NUMBA_STATUS = "available"
        except Exception as exc:  # pragma: no cover - depends on the runtime
            _NUMBA_KERNELS = None
            _NUMBA_STATUS = f"unavailable: {type(exc).__name__}: {exc}"
    return _NUMBA_STATUS


def _chunk_bounds(count: int, n_chunk: int) -> np.ndarray:
    bounds = np.linspace(0, count, n_chunk + 1).astype(np.int64)
    bounds[0], bounds[-1] = 0, count
    return bounds


def _default_chunk_count() -> int:
    return max(1, int(_NUMBA_KERNELS["threads"]) * 4)


def _spread_bits_21(values: np.ndarray) -> np.ndarray:
    """Spread the low 21 bits of each uint64 so bit k moves to bit 3k (Morton helper)."""
    x = values.astype(np.uint64) & np.uint64(0x1FFFFF)
    x = (x | (x << np.uint64(32))) & np.uint64(0x1F00000000FFFF)
    x = (x | (x << np.uint64(16))) & np.uint64(0x1F0000FF0000FF)
    x = (x | (x << np.uint64(8))) & np.uint64(0x100F00F00F00F00F)
    x = (x | (x << np.uint64(4))) & np.uint64(0x10C30C30C30C30C3)
    x = (x | (x << np.uint64(2))) & np.uint64(0x1249249249249249)
    return x


def spatial_face_order(centroid_xy: Sequence[Sequence[float]]) -> np.ndarray:
    """Morton (Z-order) permutation of faces by centroid, for cache-friendly storage.

    Returns ``order`` with ``order[i]`` = mesh face stored at position ``i``.
    This only changes memory layout inside the numba kernels; every result is
    still accumulated in the documented mesh order.
    """
    xy = np.asarray(centroid_xy, dtype=np.float64).reshape(-1, 2)
    if xy.shape[0] == 0:
        return np.zeros(0, dtype=np.int64)
    low = xy.min(axis=0)
    span = float((xy.max(axis=0) - low).max())
    scale = (2.0 ** 21 - 1.0) / span if span > 0.0 else 0.0
    ix = np.floor((xy[:, 0] - low[0]) * scale).astype(np.uint64)
    iy = np.floor((xy[:, 1] - low[1]) * scale).astype(np.uint64)
    code = _spread_bits_21(ix) | (_spread_bits_21(iy) << np.uint64(1))
    return np.argsort(code, kind="stable").astype(np.int64)


class NumbaWorkspace:
    """Preallocated per-mesh work arrays and storage order for the numba kernels.

    ``face_order`` (optional, e.g. :func:`spatial_face_order`) is the storage
    permutation: position ``i`` of every per-face array holds mesh face
    ``face_order[i]``, and edges are stored sorted by their (smaller, larger)
    stored face index.  Without it the storage order is the mesh order.  The
    arithmetic is unchanged by the layout: a face's edge terms are added in
    ascending *mesh* edge index, cross-chunk reductions are exact minima,
    maxima and counts, and the sanitation finish scans in mesh face order.
    ``to_internal`` / ``to_mesh`` convert per-face arrays (copies).

    ``chunks_per_thread`` contiguous chunks per numba thread are used for the
    parallel loops.  ``wave_sums`` returned by :func:`rhs_numba` is the
    workspace buffer and is overwritten by the next call.
    """

    def __init__(self, geometry: EdgeGeometry, *, face_order: np.ndarray | None = None,
                 chunks_per_thread: int = 4) -> None:
        if numba_status() != "available":
            raise RuntimeError(f"numba kernel {numba_status()}")
        threads = max(1, int(_NUMBA_KERNELS["threads"]))
        n_chunk = max(1, threads * int(chunks_per_thread))
        n_face, n_edge = geometry.n_face, geometry.n_edge
        self.geometry = geometry
        self.n_chunk = n_chunk
        self.reordered = face_order is not None
        if face_order is None:
            perm = np.arange(n_face, dtype=np.int64)
        else:
            perm = np.asarray(face_order, dtype=np.int64)
            if perm.shape != (n_face,) or not np.array_equal(np.sort(perm), np.arange(n_face)):
                raise ValueError("face_order must be a permutation of range(n_face)")
        inverse = np.empty(n_face, dtype=np.int64)
        inverse[perm] = np.arange(n_face, dtype=np.int64)
        self.face_perm = perm            # storage position -> mesh face
        self.face_inverse = inverse      # mesh face -> storage position
        left_stored = inverse[geometry.left]
        right_stored = np.where(geometry.interior, inverse[geometry.right_safe], -1)
        if face_order is None:
            edge_perm = np.arange(n_edge, dtype=np.int64)
        else:
            pair_low = np.minimum(left_stored, np.where(geometry.interior, right_stored, left_stored))
            pair_high = np.maximum(left_stored, right_stored)
            edge_perm = np.lexsort((pair_high, pair_low)).astype(np.int64)
        edge_inverse = np.empty(n_edge, dtype=np.int64)
        edge_inverse[edge_perm] = np.arange(n_edge, dtype=np.int64)
        self.edge_perm = edge_perm       # storage position -> mesh edge
        self.left = np.ascontiguousarray(left_stored[edge_perm])
        self.right = np.ascontiguousarray(right_stored[edge_perm])
        self.nx = np.ascontiguousarray(geometry.nx[edge_perm])
        self.ny = np.ascontiguousarray(geometry.ny[edge_perm])
        self.length = np.ascontiguousarray(geometry.length[edge_perm])
        self.left_factor = np.ascontiguousarray(geometry.left_factor[edge_perm])
        self.right_factor = np.ascontiguousarray(geometry.right_factor[edge_perm])
        self.area = np.ascontiguousarray(geometry.area[perm])
        table, sides = _face_edge_table(geometry)
        stored_table = np.where(table >= 0, edge_inverse[np.maximum(table, 0)], -1)[perm]
        self.face_edge = np.ascontiguousarray(stored_table)
        self.face_side = np.ascontiguousarray(sides[perm])
        # Per-face edge slots for the single-pass kernel: neighbour (-1 wall,
        # -2 unused slot), side, and [nx, ny, L, lf, rf] of the slot's edge.
        valid = self.face_edge >= 0
        safe_edge = np.maximum(self.face_edge, 0)
        other = np.where(self.face_side == 0, self.right[safe_edge], self.left[safe_edge])
        self.slot_face = np.ascontiguousarray(np.where(valid, other, -2).astype(np.int64))
        self.slot_side = np.ascontiguousarray(self.face_side)
        slot_geom = np.zeros(self.face_edge.shape + (5,), dtype=np.float64)
        for column, values in enumerate((self.nx, self.ny, self.length, self.left_factor, self.right_factor)):
            slot_geom[..., column] = np.where(valid, values[safe_edge], 0.0)
        self.slot_geom = np.ascontiguousarray(slot_geom)
        self.single_pass = False  # face_rhs: faster on dry meshes, slower when wet (measured)
        self.scan_order = inverse        # mesh face -> storage position (serial finish order)
        self.edge_chunks = _chunk_bounds(n_edge, n_chunk)
        self.face_chunks = _chunk_bounds(n_face, n_chunk)
        self.edge_values = np.empty((n_edge, 8), dtype=np.float64)
        self.wave_sums = np.empty(n_face, dtype=np.float64)
        self.pair_partial = np.empty(n_chunk, dtype=np.float64)
        self.ratio_partial = np.empty(n_chunk, dtype=np.float64)
        self.gamma_partial = np.empty(n_chunk, dtype=np.float64)
        self.count_partial = np.empty((n_chunk, 3), dtype=np.int64)
        self.stage_count_partial = np.empty((n_chunk, 3), dtype=np.int64)
        self.flags = np.empty(n_face, dtype=np.int8)

    def to_internal(self, values: np.ndarray) -> np.ndarray:
        """Mesh-ordered per-face array -> storage order (always a new contiguous array)."""
        return np.ascontiguousarray(np.asarray(values)[self.face_perm])

    def to_mesh(self, values: np.ndarray) -> np.ndarray:
        """Storage-ordered per-face array -> mesh order (always a new contiguous array)."""
        return np.ascontiguousarray(np.asarray(values)[self.face_inverse])


def numba_workspace(geometry: EdgeGeometry) -> NumbaWorkspace:
    """Return the mesh-ordered workspace cached on ``geometry`` (created on first use)."""
    cached = getattr(geometry, "_numba_workspace", None)
    if cached is None:
        cached = NumbaWorkspace(geometry)
        object.__setattr__(geometry, "_numba_workspace", cached)
    return cached


def _nan_propagating_max(values: np.ndarray, floor: float) -> float:
    result = floor
    for value in values.tolist():
        if not (value <= result):
            result = value
    return result


def _nan_propagating_min(values: np.ndarray) -> float:
    result = math.inf
    for value in values.tolist():
        if not (value >= result):
            result = value
    return result


def rhs_numba(
    state: np.ndarray,
    bed_m: np.ndarray,
    geometry: EdgeGeometry,
    *,
    gravity: float,
    cfl: float,
    dry_depth_m: float,
    workspace: NumbaWorkspace | None = None,
    out: np.ndarray | None = None,
) -> RhsResult:
    """Compiled residual, bitwise identical to :func:`rhs_numpy` (see module notes).

    ``state``, ``bed_m`` and the results are in the storage order of
    ``workspace`` (mesh order for the default workspace).  ``out`` receives the
    derivative when given (shape ``(n_face, 3)``); the returned ``wave_sums``
    is the workspace buffer.
    """
    work = workspace if workspace is not None else numba_workspace(geometry)
    kernels = _NUMBA_KERNELS
    derivative = out if out is not None else np.empty((geometry.n_face, 3), dtype=np.float64)
    state = np.ascontiguousarray(state, dtype=np.float64)
    bed_m = np.ascontiguousarray(bed_m, dtype=np.float64)
    if work.single_pass:
        kernels["face_rhs"](state, bed_m, work.slot_face, work.slot_side, work.slot_geom, work.area, float(gravity),
                            float(dry_depth_m), derivative, work.wave_sums, work.face_chunks, work.ratio_partial,
                            work.pair_partial)
    else:
        kernels["edge_pass"](
            state, bed_m, work.left, work.right, work.nx, work.ny, work.length, work.left_factor, work.right_factor,
            work.area, float(gravity), float(dry_depth_m), work.edge_values, work.edge_chunks, work.pair_partial,
        )
        kernels["face_pass"](work.face_edge, work.face_side, work.edge_values, work.area, derivative, work.wave_sums,
                             work.face_chunks, work.ratio_partial)
    maximum_pair = max(0.0, _nan_propagating_max(work.pair_partial, 0.0))
    ratio = _nan_propagating_min(work.ratio_partial)
    cfl_limit = cfl * ratio if math.isfinite(ratio) else math.inf
    return RhsResult(derivative, cfl_limit, maximum_pair, work.wave_sums)


def stage_sanitize_numba(
    first: np.ndarray,
    *,
    out: np.ndarray,
    workspace: NumbaWorkspace,
    area: np.ndarray,
    dry_depth_m: float,
    negative_tolerance_m: float,
    dt_s: float = 0.0,
    flux: np.ndarray | None = None,
    source: np.ndarray | None = None,
    boundary: np.ndarray | None = None,
    rain_m_s: float | None = None,
    second: np.ndarray | None = None,
    manning_n: np.ndarray | None = None,
    gravity: float = 9.81,
) -> tuple[int, int, float, int, float]:
    """Fused stage/average + sanitation (+ point-implicit Manning if ``manning_n``) into ``out``.

    All per-face arrays are in the storage order of ``workspace``.  Returns
    ``(code, mesh_face_index, correction_m3, count, maximum_gamma_per_s)`` with
    the same codes as :func:`stage_and_sanitize`; ``out`` is only meaningful
    for code 0.  Identical results to the unfused kernels.
    """
    kernels = _NUMBA_KERNELS
    friction = manning_n is not None
    if second is None:
        kernels["stage_fused"](
            first, _EMPTY, flux, _EMPTY if source is None else source, _EMPTY if boundary is None else boundary,
            -1.0 if rain_m_s is None else float(rain_m_s), float(dt_s), 0, area, float(dry_depth_m),
            float(negative_tolerance_m), manning_n if friction else _EMPTY_1D, float(gravity), friction,
            out, workspace.flags, workspace.face_chunks, workspace.count_partial, workspace.gamma_partial,
            _EMPTY_I8, _EMPTY_1D, workspace.stage_count_partial, False, 0.0, _EMPTY_1D, _EMPTY_1D, _EMPTY_1D,
        )
    else:
        kernels["stage_fused"](
            first, second, _EMPTY, _EMPTY, _EMPTY, -1.0, 0.0, 1, area, float(dry_depth_m),
            float(negative_tolerance_m), manning_n if friction else _EMPTY_1D, float(gravity), friction,
            out, workspace.flags, workspace.face_chunks, workspace.count_partial, workspace.gamma_partial,
            _EMPTY_I8, _EMPTY_1D, workspace.stage_count_partial, False, 0.0, _EMPTY_1D, _EMPTY_1D, _EMPTY_1D,
        )
    counts = workspace.count_partial.sum(axis=0)
    gamma = float(workspace.gamma_partial.max()) if friction else 0.0
    if counts[0] == 0 and counts[1] == 0 and counts[2] == 0:
        return 0, -1, 0.0, 0, gamma
    code, index, correction, count = kernels["sanitize_finish"](out[:, 0], workspace.flags, area, workspace.scan_order,
                                                                counts[0] > 0, counts[1] > 0, True)
    return int(code), int(index), float(correction), int(count), gamma


def _stage_only_workspace(n_face: int) -> NumbaWorkspace:
    workspace = NumbaWorkspace.__new__(NumbaWorkspace)
    n_chunk = _default_chunk_count()
    workspace.n_chunk = n_chunk
    workspace.face_chunks = _chunk_bounds(n_face, n_chunk)
    workspace.scan_order = np.arange(n_face, dtype=np.int64)
    workspace.flags = np.empty(n_face, dtype=np.int8)
    workspace.count_partial = np.empty((n_chunk, 3), dtype=np.int64)
    workspace.stage_count_partial = np.empty((n_chunk, 3), dtype=np.int64)
    workspace.gamma_partial = np.empty(n_chunk, dtype=np.float64)
    return workspace


def stage_and_sanitize(
    first: np.ndarray,
    *,
    dt_s: float = 0.0,
    flux: np.ndarray | None = None,
    source: np.ndarray | None = None,
    boundary: np.ndarray | None = None,
    rain_m_s: float | None = None,
    second: np.ndarray | None = None,
    area: np.ndarray,
    dry_depth_m: float,
    negative_tolerance_m: float,
    use_numba: bool,
) -> tuple[np.ndarray | None, int, int, float, int]:
    """Euler stage (``second is None``) or SSPRK2 average, then positivity/dry sanitation.

    Same arithmetic order and sanitation rules as the P4-1 path
    (``state + dt * rhs`` with ``rhs = ((flux + source) + boundary)``, rain added
    to the h component; tiny negatives clipped and summed in face order; dry
    faces lose momentum).  Returns ``(state, code, index, correction_m3, count)``
    with code 0 ok, 1 non-finite, 2 significant negative depth at ``index``.
    """
    if use_numba and numba_status() == "available":
        out = np.empty_like(first)
        code, index, correction, count, _ = stage_sanitize_numba(
            first, out=out, workspace=_stage_only_workspace(first.shape[0]), area=area, dry_depth_m=dry_depth_m,
            negative_tolerance_m=negative_tolerance_m, dt_s=dt_s, flux=flux, source=source, boundary=boundary,
            rain_m_s=rain_m_s, second=second,
        )
        return (out if code == 0 else None), code, index, correction, count
    if second is None:
        total = flux.copy()
        if source is not None:
            total += source
        if boundary is not None:
            total += boundary
        if rain_m_s is not None:
            total[:, 0] += rain_m_s
        raw = first + dt_s * total
    else:
        raw = 0.5 * (first + second)
    if not np.isfinite(raw).all():
        bad = np.flatnonzero(~np.isfinite(raw).all(axis=1))
        return None, 1, int(bad[0]), 0.0, 0
    depth = raw[:, 0]
    significant = np.flatnonzero(depth < -negative_tolerance_m)
    if significant.size:
        return None, 2, int(significant[0]), 0.0, 0
    tiny = np.flatnonzero(depth < 0.0)
    correction = 0.0
    if tiny.size:
        for value in (-depth[tiny] * area[tiny]).tolist():
            correction += value
        raw[tiny, 0] = 0.0
    raw[raw[:, 0] <= dry_depth_m, 1:] = 0.0
    return raw, 0, -1, correction, int(tiny.size)


def update_face_maxima(state: np.ndarray, time_s: float, dry_depth_m: float, max_depth: np.ndarray,
                       time_of_max: np.ndarray, max_speed: np.ndarray, *, use_numba: bool,
                       workspace: NumbaWorkspace | None = None) -> None:
    """Fold one accepted state into per-face max depth (and its time) and max speed."""
    if use_numba and numba_status() == "available":
        chunks = workspace.face_chunks if workspace is not None else _chunk_bounds(state.shape[0], _default_chunk_count())
        _NUMBA_KERNELS["update_maxima"](state, float(time_s), float(dry_depth_m), max_depth, time_of_max, max_speed, chunks)
        return
    depth = state[:, 0]
    deeper = depth > max_depth
    max_depth[deeper] = depth[deeper]
    time_of_max[deeper] = time_s
    wet = depth > dry_depth_m
    if wet.any():
        speed = np.hypot(state[wet, 1] / depth[wet], state[wet, 2] / depth[wet])
        max_speed[wet] = np.maximum(max_speed[wet], speed)


def _face_edge_table(geometry: EdgeGeometry) -> tuple[np.ndarray, np.ndarray]:
    """(n_face, k) edge indices per face in ascending order and 0=left / 1=right side."""
    cached = getattr(geometry, "_face_edge_table", None)
    if cached is not None:
        return cached
    edges = np.arange(geometry.n_edge, dtype=np.int64)
    interior = geometry.interior
    faces = np.concatenate([geometry.left, geometry.right[interior]])
    edge_ids = np.concatenate([edges, edges[interior]])
    sides = np.concatenate([np.zeros(geometry.n_edge, dtype=np.int8), np.ones(int(interior.sum()), dtype=np.int8)])
    order = np.lexsort((edge_ids, faces))
    faces, edge_ids, sides = faces[order], edge_ids[order], sides[order]
    counts = np.bincount(faces, minlength=geometry.n_face)
    width = max(1, int(counts.max(initial=0)))
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    slot = np.arange(faces.size) - starts[faces]
    table = np.full((geometry.n_face, width), -1, dtype=np.int64)
    side_table = np.zeros((geometry.n_face, width), dtype=np.int8)
    table[faces, slot] = edge_ids
    side_table[faces, slot] = sides
    object.__setattr__(geometry, "_face_edge_table", (table, side_table))
    return table, side_table


def select_rhs_kernel(name: str) -> Callable[..., RhsResult]:
    """Resolve ``"numpy"``, ``"numba"`` or ``"auto"`` (numba if importable)."""
    if name == "numpy":
        return rhs_numpy
    if name == "numba":
        if numba_status() != "available":
            raise RuntimeError(f"kernel 'numba' requested but numba is {numba_status()}")
        return rhs_numba
    if name == "auto":
        return rhs_numba if numba_status() == "available" else rhs_numpy
    raise ValueError(f"unknown kernel {name!r}; expected 'auto', 'numpy' or 'numba'")


# --------------------------------------------------------------------------
# Manning friction
# --------------------------------------------------------------------------
@dataclass
class ManningExplicitResult:
    source: np.ndarray
    source_limit_s: float
    diagnostics: dict[str, Any]
    error: str | None


def empty_manning_diagnostics() -> dict[str, Any]:
    return {
        "maximum_manning_gamma_per_s": 0.0,
        "maximum_manning_source_magnitude_m2_s2": 0.0,
        "manning_nonzero_face_stage_evaluation_count": 0,
        "source_cfl_limit_evaluation_count": 0,
        "minimum_source_cfl_limit_s": None,
        "maximum_source_cfl_limit_s": None,
        "minimum_wave_cfl_limit_s": None,
    }


def manning_explicit(
    state: np.ndarray,
    manning_n: np.ndarray,
    *,
    gravity: float,
    source_cfl_fraction: float,
    dry_depth_m: float,
    scalar_hypot: bool = False,
) -> ManningExplicitResult:
    """Explicit vector Manning drag ``S = -gamma q`` with ``gamma = g n^2 |q| / h^(7/3)``.

    Per-face failures are reported for the lowest face index, as in the scalar loop.
    ``scalar_hypot=True`` evaluates magnitudes with :func:`math.hypot` (the
    original implementation's rounding; ``numpy.hypot`` can differ by one ULP).
    """
    source = np.zeros_like(state)
    diagnostics = empty_manning_diagnostics()
    h, hx, hy = state[:, 0], state[:, 1], state[:, 2]
    hypot = _scalar_hypot if scalar_hypot else np.hypot
    candidates = np.flatnonzero(h > dry_depth_m)
    q_candidates = hypot(hx[candidates], hy[candidates])
    nonzero = q_candidates != 0.0
    active = candidates[nonzero]
    if active.size == 0:
        return ManningExplicitResult(source, math.inf, diagnostics, None)
    ha, hxa, hya, qa, na = h[active], hx[active], hy[active], q_candidates[nonzero], manning_n[active]
    with np.errstate(over="ignore", divide="ignore", invalid="ignore", under="ignore"):
        depth_power = ha ** (7.0 / 3.0)
        gamma = gravity * na * na * qa / depth_power
        source_x = -gamma * hxa
        source_y = -gamma * hya
        magnitude = hypot(source_x, source_y)
        limit = source_cfl_fraction / gamma
    finite = (np.isfinite(depth_power) & np.isfinite(gamma) & np.isfinite(source_x)
              & np.isfinite(source_y) & np.isfinite(magnitude) & (gamma >= 0.0) & (magnitude >= 0.0))
    positive = finite & (gamma > 0.0)
    code = np.zeros(active.size, dtype=np.int8)
    code[~finite] = 1
    code[positive & (limit == 0.0)] = 2
    code[positive & ~np.isfinite(limit)] = 3
    code[positive & (limit < 0.0)] = 4
    bad = np.flatnonzero(code)
    if bad.size:
        # Mirror the scalar loop: faces before the failing face (and, for a
        # limit failure, the failing face itself) were already recorded.
        first = int(bad[0])
        reason = {1: "non_finite_source", 2: "source_cfl_limit_underflow", 3: "source_cfl_limit_overflow", 4: "invalid_source_cfl_limit"}[int(code[first])]
        stop = first if code[first] == 1 else first + 1
        if stop:
            recorded = slice(0, stop)
            source[active[recorded], 1] = source_x[recorded]
            source[active[recorded], 2] = source_y[recorded]
            diagnostics["maximum_manning_gamma_per_s"] = max(0.0, float(gamma[recorded].max()))
            diagnostics["maximum_manning_source_magnitude_m2_s2"] = max(0.0, float(magnitude[recorded].max()))
            diagnostics["manning_nonzero_face_stage_evaluation_count"] = int(np.count_nonzero((positive & (magnitude > 0.0))[recorded]))
        return ManningExplicitResult(source, 0.0, diagnostics, reason)
    source[active, 1] = source_x
    source[active, 2] = source_y
    diagnostics["maximum_manning_gamma_per_s"] = max(0.0, float(gamma.max()))
    diagnostics["maximum_manning_source_magnitude_m2_s2"] = max(0.0, float(magnitude.max()))
    diagnostics["manning_nonzero_face_stage_evaluation_count"] = int(np.count_nonzero(positive & (magnitude > 0.0)))
    limits = limit[positive]
    if limits.size:
        diagnostics["source_cfl_limit_evaluation_count"] = int(limits.size)
        diagnostics["minimum_source_cfl_limit_s"] = float(limits.min())
        diagnostics["maximum_source_cfl_limit_s"] = float(limits.max())
        source_limit = float(limits.min())
    else:
        source_limit = math.inf
    return ManningExplicitResult(source, source_limit, diagnostics, None)


def _scalar_hypot(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return np.fromiter(map(math.hypot, x.tolist(), y.tolist()), dtype=np.float64, count=x.size)


def manning_point_implicit(
    state: np.ndarray,
    manning_n: np.ndarray,
    dt_s: float,
    *,
    gravity: float,
    dry_depth_m: float,
    use_numba: bool = False,
) -> tuple[np.ndarray, float]:
    """Point-implicit Manning drag on a stage state, in place.

    ``q_new = q / (1 + dt * g n^2 |q| / h^(7/3))`` (equivalently
    ``1 + dt g n^2 |u| / h^(4/3)``), using the stage depth and the stage
    unit discharge after the flux/rain update.  The factor is >= 1, so friction
    can only reduce |q|, never reverse it, for any dt.  Returns the state and the
    maximum friction coefficient gamma (1/s) seen.
    """
    if use_numba and numba_status() == "available":
        n_chunk = _default_chunk_count()
        gamma_partial = np.empty(n_chunk, dtype=np.float64)
        _NUMBA_KERNELS["implicit_friction"](state, np.ascontiguousarray(manning_n, dtype=np.float64), float(dt_s), float(gravity),
                                            float(dry_depth_m), _chunk_bounds(state.shape[0], n_chunk), gamma_partial)
        return state, float(gamma_partial.max(initial=0.0))
    h = state[:, 0]
    active = np.flatnonzero(h > dry_depth_m)
    if active.size == 0:
        return state, 0.0
    ha = h[active]
    hx = state[active, 1]
    hy = state[active, 2]
    na = manning_n[active]
    with np.errstate(over="ignore", divide="ignore", invalid="ignore", under="ignore"):
        # h^(7/3) as h^2 * cbrt(h): same value to rounding, ~5x cheaper than pow.
        gamma = gravity * na * na * np.hypot(hx, hy) / (ha * ha * np.cbrt(ha))
        factor = 1.0 + dt_s * gamma
    if not np.isfinite(gamma).all():
        # Infinite drag means the face momentum is fully damped.
        factor = np.where(np.isfinite(factor), factor, np.inf)
    state[active, 1] = hx / factor
    state[active, 2] = hy / factor
    finite_gamma = gamma[np.isfinite(gamma)]
    return state, float(finite_gamma.max()) if finite_gamma.size else 0.0


__all__ = [
    "EdgeGeometry", "ManningExplicitResult", "NumbaWorkspace", "RhsResult",
    "edge_geometry_from_edges", "empty_manning_diagnostics", "manning_explicit",
    "manning_point_implicit", "numba_status", "numba_workspace", "rhs_numba", "rhs_numpy", "select_rhs_kernel",
    "spatial_face_order", "stage_and_sanitize", "stage_sanitize_numba", "update_face_maxima",
]
