"""Frozen scalar reference copy of the original per-edge triangular solver kernels.

Copied verbatim from ``hydrodynamics/triangular_solver.py`` at commit 4844107
before vectorisation.  Tests monkeypatch these into the solver to prove that
the vectorised kernels reproduce the original implementation.  Not solver code.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

from hydrodynamics.triangular_solver import _Sanitized, _empty_manning_diagnostics


def _sanitize_stage(
    state: np.ndarray,
    mesh: Any,
    *,
    dry_depth_m: float,
    negative_tolerance_m: float,
) -> _Sanitized:
    if not np.isfinite(state).all():
        return _Sanitized(state, 0.0, 0, "non_finite_state", "stage contains NaN or infinity")
    clean = state.copy()
    depth = clean[:, 0]
    significant = np.flatnonzero(depth < -negative_tolerance_m)
    if significant.size:
        index = int(significant[0])
        return _Sanitized(state, 0.0, 0, "negative_depth", f"face_id {mesh.face_id[index]} reached h={depth[index]:.17g} m")
    tiny = np.flatnonzero(depth < 0.0)
    correction = 0.0
    for index in tiny:
        correction += -float(depth[index]) * float(mesh.face_area[index])
        clean[index, 0] = 0.0
    count = int(tiny.size)
    dry = clean[:, 0] <= dry_depth_m
    clean[dry, 1:] = 0.0
    if not np.isfinite(clean).all():
        return _Sanitized(state, 0.0, 0, "non_finite_state", "stage sanitation produced NaN or infinity")
    return _Sanitized(clean, correction, count)


def _physical_flux(state: np.ndarray, nx: float, ny: float, gravity: float) -> tuple[np.ndarray, float, float]:
    h, hx, hy = (float(value) for value in state)
    if h <= 0.0:
        return np.zeros(3, dtype=np.float64), 0.0, 0.0
    u, v = hx / h, hy / h
    un = u * nx + v * ny
    c = math.sqrt(gravity * h)
    flux = np.array(
        [h * un, hx * un + 0.5 * gravity * h * h * nx, hy * un + 0.5 * gravity * h * h * ny],
        dtype=np.float64,
    )
    return flux, un, c


def _hll_flux(left: np.ndarray, right: np.ndarray, nx: float, ny: float, gravity: float) -> np.ndarray:
    flux_left, un_left, c_left = _physical_flux(left, nx, ny, gravity)
    flux_right, un_right, c_right = _physical_flux(right, nx, ny, gravity)
    if left[0] <= 0.0 and right[0] <= 0.0:
        return np.zeros(3, dtype=np.float64)
    speed_minus = min(0.0, un_left - c_left, un_right - c_right)
    speed_plus = max(0.0, un_left + c_left, un_right + c_right)
    if speed_minus >= 0.0:
        return flux_left
    if speed_plus <= 0.0:
        return flux_right
    denominator = speed_plus - speed_minus
    if denominator <= 0.0:
        return np.zeros(3, dtype=np.float64)
    return (
        speed_plus * flux_left - speed_minus * flux_right
        + speed_minus * speed_plus * (right - left)
    ) / denominator


def _rhs_and_cfl(
    state: np.ndarray,
    mesh: Any,
    bed_m: Sequence[float],
    edges: Sequence[Any],
    *,
    gravity: float,
    cfl: float,
    dry_depth_m: float,
) -> tuple[np.ndarray, float, float]:
    """Calculate one unique-edge residual and the frozen geometric CFL bound."""
    derivative = np.zeros_like(state)
    wave_sums = np.zeros(mesh.n_face, dtype=np.float64)
    maximum_pair_residual = 0.0
    for edge in edges:
        li, ri = edge.left, edge.right
        h_l = float(state[li, 0])
        mx_l, my_l = float(state[li, 1]), float(state[li, 2])
        u_l = mx_l / h_l if h_l > dry_depth_m else 0.0
        v_l = my_l / h_l if h_l > dry_depth_m else 0.0
        un_l = u_l * edge.nx + v_l * edge.ny
        c_l = math.sqrt(gravity * max(h_l, 0.0))
        if ri is None:
            h_r = h_l
            u_r = u_l - 2.0 * un_l * edge.nx
            v_r = v_l - 2.0 * un_l * edge.ny
            un_r = -un_l
            c_r = c_l
            z_r = float(bed_m[li])
        else:
            h_r = float(state[ri, 0])
            mx_r, my_r = float(state[ri, 1]), float(state[ri, 2])
            u_r = mx_r / h_r if h_r > dry_depth_m else 0.0
            v_r = my_r / h_r if h_r > dry_depth_m else 0.0
            un_r = u_r * edge.nx + v_r * edge.ny
            c_r = math.sqrt(gravity * max(h_r, 0.0))
            z_r = float(bed_m[ri])
        a_edge = max(abs(un_l) + c_l, abs(un_r) + c_r)
        wave_sums[li] += edge.length * a_edge
        if ri is not None:
            wave_sums[ri] += edge.length * a_edge

        bed_star = max(float(bed_m[li]), z_r)
        h_l_star = max(0.0, h_l + float(bed_m[li]) - bed_star)
        h_r_star = max(0.0, h_r + z_r - bed_star)
        left_star = np.array([h_l_star, h_l_star * u_l, h_l_star * v_l], dtype=np.float64)
        right_star = np.array([h_r_star, h_r_star * u_r, h_r_star * v_r], dtype=np.float64)
        flux = _hll_flux(left_star, right_star, edge.nx, edge.ny, gravity)
        if ri is None:
            # Reflection is exactly impermeable, including in binary64.
            flux[0] = 0.0
        correction_l = 0.5 * gravity * (h_l * h_l - h_l_star * h_l_star)
        flux_l = flux.copy()
        flux_l[1] += correction_l * edge.nx
        flux_l[2] += correction_l * edge.ny
        left_factor = edge.length / mesh.face_area[li]
        left_mass_increment = -left_factor * flux[0]
        derivative[li, 0] += left_mass_increment
        derivative[li, 1] -= left_factor * flux_l[1]
        derivative[li, 2] -= left_factor * flux_l[2]
        if ri is not None:
            correction_r = 0.5 * gravity * (h_r * h_r - h_r_star * h_r_star)
            flux_r_x = flux[1] + correction_r * edge.nx
            flux_r_y = flux[2] + correction_r * edge.ny
            right_factor = edge.length / mesh.face_area[ri]
            right_mass_increment = right_factor * flux[0]
            derivative[ri, 0] += right_mass_increment
            derivative[ri, 1] += right_factor * flux_r_x
            derivative[ri, 2] += right_factor * flux_r_y
            # Measure the area-integrated increments actually accumulated into
            # each face derivative; this captures division/multiplication
            # roundoff and is not an algebraic pair+(-pair) identity.
            pair_residual = abs(
                mesh.face_area[li] * left_mass_increment
                + mesh.face_area[ri] * right_mass_increment
            )
            maximum_pair_residual = max(maximum_pair_residual, pair_residual)

    ratios = [mesh.face_area[i] / wave_sums[i] for i in range(mesh.n_face) if wave_sums[i] > 0.0]
    cfl_limit = cfl * min(ratios) if ratios else math.inf
    return derivative, cfl_limit, maximum_pair_residual


def _manning_source(
    state: np.ndarray,
    manning_n_m: Sequence[float] | None,
    *,
    gravity: float,
    source_cfl_fraction: float | None,
    dry_depth_m: float,
) -> tuple[np.ndarray, float, dict[str, Any], str | None]:
    """Evaluate explicit vector Manning drag and its per-stage source bound."""
    source = np.zeros_like(state)
    diagnostics = _empty_manning_diagnostics()
    if manning_n_m is None:
        return source, math.inf, diagnostics, None
    if (source_cfl_fraction is None or not math.isfinite(float(source_cfl_fraction))
            or not 0.0 < float(source_cfl_fraction) <= 1.0 or len(manning_n_m) != len(state)):
        return source, 0.0, diagnostics, "invalid_manning_source_binding"

    finite_limits: list[float] = []
    for index, (h_raw, hx_raw, hy_raw, n_raw) in enumerate(zip(state[:, 0], state[:, 1], state[:, 2], manning_n_m)):
        h, hx, hy, n = float(h_raw), float(hx_raw), float(hy_raw), float(n_raw)
        if h <= dry_depth_m:
            continue
        q_magnitude = math.hypot(hx, hy)
        if q_magnitude == 0.0:
            continue
        try:
            depth_power = h ** (7.0 / 3.0)
            gamma = gravity * n * n * q_magnitude / depth_power
            source_x = -gamma * hx
            source_y = -gamma * hy
            source_magnitude = math.hypot(source_x, source_y)
        except (OverflowError, ZeroDivisionError):
            return source, 0.0, diagnostics, "non_finite_source"
        if not all(math.isfinite(value) for value in (depth_power, gamma, source_x, source_y, source_magnitude)):
            return source, 0.0, diagnostics, "non_finite_source"
        if gamma < 0.0 or source_magnitude < 0.0:
            return source, 0.0, diagnostics, "non_finite_source"
        source[index, 1] = source_x
        source[index, 2] = source_y
        diagnostics["maximum_manning_gamma_per_s"] = max(diagnostics["maximum_manning_gamma_per_s"], gamma)
        diagnostics["maximum_manning_source_magnitude_m2_s2"] = max(
            diagnostics["maximum_manning_source_magnitude_m2_s2"], source_magnitude,
        )
        if gamma > 0.0 and source_magnitude > 0.0:
            diagnostics["manning_nonzero_face_stage_evaluation_count"] += 1
        if gamma > 0.0:
            limit = source_cfl_fraction / gamma
            if limit == 0.0:
                return source, 0.0, diagnostics, "source_cfl_limit_underflow"
            if not math.isfinite(limit):
                return source, 0.0, diagnostics, "source_cfl_limit_overflow"
            if limit < 0.0:
                return source, 0.0, diagnostics, "invalid_source_cfl_limit"
            finite_limits.append(limit)
    if finite_limits:
        diagnostics["source_cfl_limit_evaluation_count"] = len(finite_limits)
        diagnostics["minimum_source_cfl_limit_s"] = min(finite_limits)
        diagnostics["maximum_source_cfl_limit_s"] = max(finite_limits)
        source_limit = min(finite_limits)
    else:
        source_limit = math.inf
    return source, source_limit, diagnostics, None


def _speed_max(state: np.ndarray, dry_depth_m: float) -> float:
    speeds = [math.hypot(float(mx) / float(h), float(my) / float(h)) for h, mx, my in state if h > dry_depth_m]
    return max(speeds, default=0.0)


def _volume(state: np.ndarray, mesh: Any) -> float:
    return math.fsum(float(h) * float(area) for h, area in zip(state[:, 0], mesh.face_area))

