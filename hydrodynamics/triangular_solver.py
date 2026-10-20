"""Experimental first-order finite-volume solver for closed triangular domains.

This module is intentionally independent from the input schema and P2/P3-A
loaders.  The public runner composes those validated inputs with the numerical
core, then delegates result serialization to :mod:`triangular_solver_netcdf`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .triangular_mesh import TriangularMesh
from .triangular_solver_contract import (
    TriangularMassBudget,
    TriangularResultContract,
    TriangularRunCapability,
    TriangularSolverContractError,
    _step_tolerance_s,
    _source_cfl_bound_exceeded,
    _source_cfl_bound_tolerance_s,
    validate_triangular_result_for_run,
    validate_triangular_run_request,
)
from .triangular_business import terrain_fingerprint, topology_fingerprint


SOLVER_VERSION = "triangular-closed-wall-1.0.0"


class TriangularSolverExecutionError(RuntimeError):
    """Structured failure from a run that did not produce a result artifact."""

    def __init__(
        self,
        message: str,
        *,
        time_s: float,
        stage: str,
        reason: str,
        last_reason: str | None = None,
        step_kind: str | None = None,
        accepted_step_count: int,
        rejected_step_count: int,
        rejected_step_reasons: Mapping[str, int],
    ) -> None:
        super().__init__(message)
        self.time_s = float(time_s)
        self.stage = stage
        self.reason = reason
        self.last_reason = last_reason or reason
        self.step_kind = step_kind
        self.accepted_step_count = int(accepted_step_count)
        self.rejected_step_count = int(rejected_step_count)
        self.rejected_step_reasons = dict(rejected_step_reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "time_s": self.time_s,
            "stage": self.stage,
            "reason": self.reason,
            "last_reason": self.last_reason,
            "step_kind": self.step_kind,
            "accepted_step_count": self.accepted_step_count,
            "rejected_step_count": self.rejected_step_count,
            "rejected_step_reasons": dict(self.rejected_step_reasons),
            "result_published": False,
            "message": str(self),
        }


@dataclass(frozen=True)
class _Edge:
    index: int
    left: int
    right: int | None
    nx: float
    ny: float
    length: float


@dataclass(frozen=True)
class _Sanitized:
    state: np.ndarray
    correction_volume_m3: float
    correction_count: int
    reason: str | None = None
    detail: str | None = None


def _edges(mesh: TriangularMesh) -> tuple[_Edge, ...]:
    """Return edges with normals oriented by geometry and stable face IDs."""
    answer: list[_Edge] = []
    for edge_index, (node_a, node_b) in enumerate(mesh.edge_nodes):
        incident_a, incident_b = mesh.edge_faces[edge_index]
        if incident_b == -1:
            left = incident_a
            right = None
        elif mesh.face_id[incident_a] <= mesh.face_id[incident_b]:
            left, right = incident_a, incident_b
        else:
            left, right = incident_b, incident_a
        ax, ay = mesh.node_xy[node_a]
        bx, by = mesh.node_xy[node_b]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        nx, ny = dy / length, -dx / length
        mx, my = (ax + bx) * 0.5, (ay + by) * 0.5
        if right is None:
            cx, cy = mesh.face_centroid[left]
            toward_x, toward_y = mx - cx, my - cy
        else:
            lx, ly = mesh.face_centroid[left]
            rx, ry = mesh.face_centroid[right]
            toward_x, toward_y = rx - lx, ry - ly
        if nx * toward_x + ny * toward_y < 0.0:
            nx, ny = -nx, -ny
        answer.append(_Edge(edge_index, left, right, nx, ny, length))
    return tuple(answer)


def _sanitize_stage(
    state: np.ndarray,
    mesh: TriangularMesh,
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
    mesh: TriangularMesh,
    bed_m: Sequence[float],
    edges: Sequence[_Edge],
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


def _empty_manning_diagnostics() -> dict[str, Any]:
    return {
        "maximum_manning_gamma_per_s": 0.0,
        "maximum_manning_source_magnitude_m2_s2": 0.0,
        "manning_nonzero_face_stage_evaluation_count": 0,
        "source_cfl_limit_evaluation_count": 0,
        "minimum_source_cfl_limit_s": None,
        "maximum_source_cfl_limit_s": None,
        "minimum_wave_cfl_limit_s": None,
    }


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


def _merge_manning_diagnostics(*items: Mapping[str, Any]) -> dict[str, Any]:
    merged = _empty_manning_diagnostics()
    for item in items:
        merged["maximum_manning_gamma_per_s"] = max(merged["maximum_manning_gamma_per_s"], float(item.get("maximum_manning_gamma_per_s", 0.0)))
        merged["maximum_manning_source_magnitude_m2_s2"] = max(
            merged["maximum_manning_source_magnitude_m2_s2"], float(item.get("maximum_manning_source_magnitude_m2_s2", 0.0)),
        )
        merged["manning_nonzero_face_stage_evaluation_count"] += int(item.get("manning_nonzero_face_stage_evaluation_count", 0))
        merged["source_cfl_limit_evaluation_count"] += int(item.get("source_cfl_limit_evaluation_count", 0))
        for name in (
            "minimum_source_cfl_limit_s", "maximum_source_cfl_limit_s", "minimum_wave_cfl_limit_s",
        ):
            value = item.get(name)
            if value is None or not math.isfinite(float(value)):
                continue
            current = merged[name]
            if current is None:
                merged[name] = float(value)
            elif name == "maximum_source_cfl_limit_s":
                merged[name] = max(current, float(value))
            else:
                merged[name] = min(current, float(value))
    return merged


def _attempt_step(
    state: np.ndarray,
    dt_s: float,
    mesh: TriangularMesh,
    bed_m: Sequence[float],
    edges: Sequence[_Edge],
    *,
    gravity: float,
    cfl: float,
    dry_depth_m: float,
    negative_tolerance_m: float,
    correction_budget_remaining_m3: float,
    manning_n_m: Sequence[float] | None = None,
    source_cfl_fraction: float | None = None,
    rainfall_rate_m_s: float | None = None,
) -> tuple[np.ndarray | None, float, int, float, str | None, str | None, float, str | None, dict[str, Any]]:
    rhs_0, wave_cfl_0, pair_0 = _rhs_and_cfl(state, mesh, bed_m, edges, gravity=gravity, cfl=cfl, dry_depth_m=dry_depth_m)
    source_0, source_cfl_0, source_diag_0, source_error_0 = _manning_source(
        state, manning_n_m, gravity=gravity, source_cfl_fraction=source_cfl_fraction, dry_depth_m=dry_depth_m,
    )
    source_diag_0["minimum_wave_cfl_limit_s"] = wave_cfl_0
    cfl_0 = min(wave_cfl_0, source_cfl_0)
    rhs_0 += source_0
    if rainfall_rate_m_s is not None:
        rhs_0[:, 0] += rainfall_rate_m_s
    if source_error_0:
        return None, 0.0, 0, cfl_0, source_error_0, f"stage 1 Manning source evaluation failed: {source_error_0}", pair_0, "stage1", _merge_manning_diagnostics(source_diag_0)
    stage_1_raw = state + dt_s * rhs_0
    stage_1 = _sanitize_stage(stage_1_raw, mesh, dry_depth_m=dry_depth_m, negative_tolerance_m=negative_tolerance_m)
    if stage_1.reason:
        return None, 0.0, 0, cfl_0, stage_1.reason, f"stage 1: {stage_1.detail}", pair_0, "stage1", _merge_manning_diagnostics(source_diag_0)
    if 0.5 * stage_1.correction_volume_m3 > correction_budget_remaining_m3:
        return None, 0.0, 0, cfl_0, "correction_budget", "stage-1 weighted correction exceeds the remaining volume-correction budget", pair_0, "stage1", _merge_manning_diagnostics(source_diag_0)

    _, wave_cfl_1, pair_1 = _rhs_and_cfl(stage_1.state, mesh, bed_m, edges, gravity=gravity, cfl=cfl, dry_depth_m=dry_depth_m)
    source_1, source_cfl_1, source_diag_1, source_error_1 = _manning_source(
        stage_1.state, manning_n_m, gravity=gravity, source_cfl_fraction=source_cfl_fraction, dry_depth_m=dry_depth_m,
    )
    source_diag_1["minimum_wave_cfl_limit_s"] = wave_cfl_1
    cfl_1 = min(wave_cfl_1, source_cfl_1)
    attempt_diagnostics = _merge_manning_diagnostics(source_diag_0, source_diag_1)
    if source_error_1:
        return None, 0.0, 0, cfl_1, source_error_1, f"stage 2 Manning source evaluation failed: {source_error_1}", max(pair_0, pair_1), "stage2", attempt_diagnostics
    wave_tolerance = _step_tolerance_s(max(dt_s, wave_cfl_1 if math.isfinite(wave_cfl_1) else dt_s))
    wave_violated = math.isfinite(wave_cfl_1) and dt_s > wave_cfl_1 + wave_tolerance
    source_violated = _source_cfl_bound_exceeded(dt_s, source_cfl_1)
    if source_violated and (not wave_violated or source_cfl_1 <= wave_cfl_1):
        return None, 0.0, 0, cfl_1, "second_stage_source_cfl", "candidate step exceeds freshly recomputed stage-1 Manning source CFL bound", max(pair_0, pair_1), "stage2", attempt_diagnostics
    if wave_violated:
        return None, 0.0, 0, cfl_1, "second_stage_cfl", "candidate step exceeds freshly recomputed stage-1 wave CFL bound", max(pair_0, pair_1), "stage2", attempt_diagnostics
    rhs_1, _, pair_1 = _rhs_and_cfl(stage_1.state, mesh, bed_m, edges, gravity=gravity, cfl=cfl, dry_depth_m=dry_depth_m)
    rhs_1 += source_1
    if rainfall_rate_m_s is not None:
        rhs_1[:, 0] += rainfall_rate_m_s
    stage_2_raw = stage_1.state + dt_s * rhs_1
    stage_2 = _sanitize_stage(stage_2_raw, mesh, dry_depth_m=dry_depth_m, negative_tolerance_m=negative_tolerance_m)
    if stage_2.reason:
        return None, 0.0, 0, cfl_1, stage_2.reason, f"stage 2: {stage_2.detail}", max(pair_0, pair_1), "stage2", attempt_diagnostics
    correction = 0.5 * (stage_1.correction_volume_m3 + stage_2.correction_volume_m3)
    if correction > correction_budget_remaining_m3:
        return None, 0.0, 0, cfl_1, "correction_budget", "SSPRK2 weighted correction exceeds the remaining volume-correction budget", max(pair_0, pair_1), "stage2", attempt_diagnostics
    candidate = 0.5 * (state + stage_2.state)
    if not np.isfinite(candidate).all():
        return None, 0.0, 0, cfl_1, "non_finite_state", "SSPRK2 convex combination contains NaN or infinity", max(pair_0, pair_1), "final", attempt_diagnostics
    final = _sanitize_stage(candidate, mesh, dry_depth_m=dry_depth_m, negative_tolerance_m=negative_tolerance_m)
    if final.reason:
        return None, 0.0, 0, cfl_1, final.reason, f"final state: {final.detail}", max(pair_0, pair_1), "final", attempt_diagnostics
    # Convex combination is non-negative if both stages were non-negative.
    # If rounding creates a new tiny negative, account for its full volume.
    correction += final.correction_volume_m3
    if correction > correction_budget_remaining_m3:
        return None, 0.0, 0, cfl_1, "correction_budget", "final-state correction exceeds the remaining volume-correction budget", max(pair_0, pair_1), "final", attempt_diagnostics
    count = stage_1.correction_count + stage_2.correction_count + final.correction_count
    return final.state, correction, count, cfl_1, None, None, max(pair_0, pair_1), None, attempt_diagnostics


def _initial_depth(capability: TriangularRunCapability) -> np.ndarray:
    initial = capability.business.initial_condition
    terrain = capability.business.terrain
    if initial.quantity.value == "depth":
        return np.asarray(initial.values_m, dtype=np.float64)
    return np.asarray([max(float(level) - float(bed), 0.0) for level, bed in zip(initial.values_m, terrain.elevation_m)], dtype=np.float64)


def _speed_max(state: np.ndarray, dry_depth_m: float) -> float:
    speeds = [math.hypot(float(mx) / float(h), float(my) / float(h)) for h, mx, my in state if h > dry_depth_m]
    return max(speeds, default=0.0)


def _volume(state: np.ndarray, mesh: TriangularMesh) -> float:
    return math.fsum(float(h) * float(area) for h, area in zip(state[:, 0], mesh.face_area))


def _compensated_add(total: float, correction: float, value: float) -> tuple[float, float]:
    """Neumaier compensated addition for interval elapsed time."""
    updated = total + value
    if abs(total) >= abs(value):
        correction += (total - updated) + value
    else:
        correction += (value - updated) + total
    return updated, correction


def _make_result_data(
    capability: TriangularRunCapability,
    rows: list[np.ndarray],
    stats: Mapping[str, Any],
) -> dict[str, Any]:
    config, business, mesh = capability.config, capability.business, capability.business.mesh
    bed = [float(value) for value in business.terrain.elevation_m]
    initial_volume = _volume(rows[0], mesh)
    reference_depth = max((float(value) for value in rows[0][:, 0]), default=0.0)
    scale = max(initial_volume, math.fsum(mesh.face_area) * reference_depth, 1.0)
    correction_budget = config.volume_correction_budget_fraction * scale
    cumulative_corrections: list[float] = []
    correction = 0.0
    budget_rows = []
    source_volume_by_output = stats.get("source_volume_by_output", [0.0] * len(rows))
    if len(source_volume_by_output) != len(rows):
        raise ValueError("source_volume_by_output must contain one cumulative ledger value per output time")
    for row, increment in zip(rows, stats["correction_by_output"]):
        correction += float(increment)
        cumulative_corrections.append(correction)
        final_volume = _volume(row, mesh)
        source_volume = float(source_volume_by_output[len(budget_rows)])
        residual = final_volume - initial_volume - source_volume - correction
        budget_rows.append({
            "initial_volume_m3": initial_volume,
            "boundary_net_volume_m3": 0.0,
            "source_net_volume_m3": source_volume,
            "correction_volume_m3": correction,
            "final_volume_m3": final_volume,
            "residual_m3": residual,
        })
    result_path = capability.result_path
    mesh_ref = Path(__import__("os").path.relpath(capability.mesh_path, result_path.parent)).as_posix()
    business_ref = Path(__import__("os").path.relpath(capability.business_path, result_path.parent)).as_posix()
    data = {
        "format": "triangular-solver-result-v1",
        "product_type": "experimental_triangular_shallow_water_result",
        "simulation_result": True,
        "experimental": True,
        "production_approved": False,
        "gate_hydraulics_enabled": False,
        "manning_enabled": False,
        "rainfall_enabled": False,
        "sources_applied": "none",
        "boundary_policy": "closed_wall",
        "case_id": config.case_id,
        "time_origin": config.time_origin,
        "coordinate_system": mesh.coordinate_system,
        "mesh_id": mesh.mesh_id,
        "mesh_artifact": mesh_ref,
        "business_package": business_ref,
        "solver_version": SOLVER_VERSION,
        "run_config_sha256": stats["config_sha256"],
        "topology_fingerprint": topology_fingerprint(mesh),
        "terrain_fingerprint": terrain_fingerprint(mesh, business.terrain),
        "mesh_sha256": stats["mesh_sha256"],
        "business_package_sha256": stats["business_sha256"],
        "security_exclusion": (
            None if business.security_exclusion is None
            else business.security_exclusion.model_dump(mode="json")
        ),
        "gravity_m_s2": config.gravity_m_s2,
        "cfl_number": config.cfl_number,
        "minimum_time_step_s": config.minimum_time_step_s,
        "maximum_time_step_s": config.maximum_time_step_s,
        "maximum_step_retries": config.maximum_step_retries,
        "dry_depth_threshold_m": config.dry_depth_threshold_m,
        "negative_depth_tolerance_m": config.negative_depth_tolerance_m,
        "volume_correction_budget_fraction": config.volume_correction_budget_fraction,
        "volume_correction_reference_scale_m3": scale,
        "volume_correction_budget_m3": correction_budget,
        "mesh_geometry_tolerance_m": mesh.geometry_tolerance_m,
        "minimum_face_area_m2": mesh.minimum_face_area_m2,
        "node_xy_m": [list(point) for point in mesh.node_xy],
        "face_nodes": [list(face) for face in mesh.face_nodes],
        "face_id": list(mesh.face_id),
        "face_area_m2": list(mesh.face_area),
        "boundary_groups": {name: [list(mesh.edge_nodes[i]) for i in indices] for name, indices in mesh.boundary_groups.items()},
        "constraint_groups": {name: [list(mesh.edge_nodes[i]) for i in indices] for name, indices in mesh.constraint_groups.items()},
        "time_s": list(config.output_times_s),
        "water_depth_m": [row[:, 0].tolist() for row in rows],
        "momentum_x_m2_s": [row[:, 1].tolist() for row in rows],
        "momentum_y_m2_s": [row[:, 2].tolist() for row in rows],
        "minimum_water_depth_m_by_time": [float(np.min(row[:, 0])) for row in rows],
        "maximum_speed_m_s_by_time": [_speed_max(row, config.dry_depth_threshold_m) for row in rows],
        "maximum_internal_edge_mass_pair_residual_m3_s": float(stats["maximum_pair_residual"]),
        "rejected_step_reasons": dict(stats["rejected_reasons"]),
        "mass_budget": budget_rows,
        "solver_scheme": "cell_centered_first_order_hll_hydrostatic_ssprk2",
        "accepted_step_count": stats["accepted_step_count"],
        "accepted_cfl_step_count": stats["accepted_cfl_step_count"],
        "rejected_step_count": stats["rejected_step_count"],
        "mass_correction_count": stats["mass_correction_count"],
        "minimum_cfl_step_s": stats["minimum_cfl_step_s"],
        "maximum_cfl_step_s": stats["maximum_cfl_step_s"],
        "minimum_accepted_step_s": stats["minimum_accepted_step_s"],
        "maximum_accepted_step_s": stats["maximum_accepted_step_s"],
        "subminimum_alignment_step_s": list(stats["alignment_steps"]),
        "subminimum_alignment_cfl_limit_s": list(stats["alignment_limits"]),
        "geometry_metadata_inferred": False,
        "step_statistics_inferred": False,
        "diagnostics_inferred": False,
    }
    return data


def _make_result(capability: TriangularRunCapability, rows: list[np.ndarray], stats: Mapping[str, Any]) -> TriangularResultContract:
    return TriangularResultContract.model_validate(_make_result_data(capability, rows, stats))


def _integrate(
    capability: TriangularRunCapability,
    *,
    manning_n_m: Sequence[float] | None = None,
    source_cfl_fraction: float | None = None,
    rainfall_rate_m_s_by_interval: Sequence[float] | None = None,
) -> tuple[TriangularResultContract | dict[str, Any], dict[str, Any]]:
    config, business, mesh = capability.config, capability.business, capability.business.mesh
    if (manning_n_m is None) != (source_cfl_fraction is None):
        raise TriangularSolverExecutionError(
            "Manning n values and source_cfl_fraction must be supplied together",
            time_s=0.0, stage="source_preflight", reason="invalid_manning_source_binding",
            accepted_step_count=0, rejected_step_count=0, rejected_step_reasons={},
        )
    if manning_n_m is not None:
        if len(manning_n_m) != mesh.n_face or any(not math.isfinite(float(n)) or not 0.0 < float(n) <= 1.0 for n in manning_n_m):
            raise TriangularSolverExecutionError(
                "Manning n must contain one finite SI value in (0,1] per mesh face",
                time_s=0.0, stage="source_preflight", reason="invalid_manning_source_binding",
                accepted_step_count=0, rejected_step_count=0, rejected_step_reasons={},
            )
        manning_n_m = tuple(float(value) for value in manning_n_m)
    interval_count = len(config.output_times_s) - 1
    if rainfall_rate_m_s_by_interval is not None:
        if (len(rainfall_rate_m_s_by_interval) != interval_count
                or any(not math.isfinite(float(rate)) or float(rate) < 0.0 for rate in rainfall_rate_m_s_by_interval)):
            raise TriangularSolverExecutionError(
                "rainfall_rate_m_s_by_interval must contain one finite non-negative SI rate per output interval",
                time_s=0.0, stage="source_preflight", reason="invalid_rainfall_source_binding",
                accepted_step_count=0, rejected_step_count=0, rejected_step_reasons={},
            )
        rainfall_rate_m_s_by_interval = tuple(float(rate) for rate in rainfall_rate_m_s_by_interval)
    rainfall_enabled = rainfall_rate_m_s_by_interval is not None
    total_area_m2 = math.fsum(mesh.face_area)
    manning_enabled = manning_n_m is not None
    bed = tuple(float(value) for value in business.terrain.elevation_m)
    edges = _edges(mesh)
    initial_h = _initial_depth(capability)
    state = np.zeros((mesh.n_face, 3), dtype=np.float64)
    state[:, 0] = initial_h
    initial = _sanitize_stage(state, mesh, dry_depth_m=config.dry_depth_threshold_m, negative_tolerance_m=config.negative_depth_tolerance_m)
    if initial.reason:
        raise TriangularSolverExecutionError(initial.detail or "initial state is invalid", time_s=0.0, stage="initial", reason=initial.reason, accepted_step_count=0, rejected_step_count=0, rejected_step_reasons={})
    state = initial.state
    rows = [state.copy()]
    maximum_gamma_by_output: list[float] = []
    maximum_source_magnitude_by_output: list[float] = []
    correction_by_output = [0.0]
    rejected_reasons: dict[str, int] = {}
    accepted_steps: list[float] = []
    cfl_steps: list[float] = []
    alignment_steps_by_interval = [0.0] * (len(config.output_times_s) - 1)
    alignment_limits_by_interval = [0.0] * (len(config.output_times_s) - 1)
    max_pair = 0.0
    correction_total = 0.0
    correction_count = 0
    accepted_source_diagnostics = _empty_manning_diagnostics()
    source_cfl_limited_step_count = 0
    rainfall_volume_by_interval = [0.0] * interval_count
    cumulative_rainfall_volume_by_output = [0.0]
    cumulative_rainfall_volume = 0.0
    rainfall_stage_evaluation_count = 0
    rainfall_nonzero_source_stage_evaluation_count = 0

    def record_output_manning_diagnostics(output_state: np.ndarray, output_time: float) -> None:
        if manning_n_m is None:
            maximum_gamma_by_output.append(0.0)
            maximum_source_magnitude_by_output.append(0.0)
            return
        _, _, diagnostics, source_error = _manning_source(
            output_state, manning_n_m, gravity=config.gravity_m_s2,
            source_cfl_fraction=source_cfl_fraction, dry_depth_m=config.dry_depth_threshold_m,
        )
        if source_error:
            raise TriangularSolverExecutionError(
                f"saved-time Manning diagnostic failed: {source_error}",
                time_s=output_time, stage="output_diagnostic", reason=source_error,
                accepted_step_count=len(accepted_steps), rejected_step_count=sum(rejected_reasons.values()),
                rejected_step_reasons=rejected_reasons,
            )
        maximum_gamma_by_output.append(diagnostics["maximum_manning_gamma_per_s"])
        maximum_source_magnitude_by_output.append(diagnostics["maximum_manning_source_magnitude_m2_s2"])

    record_output_manning_diagnostics(state, 0.0)
    time_s = 0.0
    for interval_index, end_time in enumerate(config.output_times_s[1:]):
        rainfall_rate_m_s = (
            rainfall_rate_m_s_by_interval[interval_index]
            if rainfall_rate_m_s_by_interval is not None else None
        )
        output_correction_start = correction_total
        interval_start = time_s
        interval_duration = math.fsum((float(end_time), -interval_start))
        elapsed = 0.0
        elapsed_correction = 0.0
        while True:
            remaining = math.fsum((interval_duration, -elapsed, -elapsed_correction))
            if remaining <= 0.0:
                if remaining >= -_step_tolerance_s(interval_duration):
                    time_s = float(end_time)
                    break
                raise TriangularSolverExecutionError(
                    f"accepted step durations overshot output interval by {-remaining:.17g} s",
                    time_s=time_s, stage="time_accumulation", reason="time_overshoot",
                    accepted_step_count=len(accepted_steps), rejected_step_count=sum(rejected_reasons.values()), rejected_step_reasons=rejected_reasons,
                )
            _, wave_cfl_now, pair_now = _rhs_and_cfl(state, mesh, bed, edges, gravity=config.gravity_m_s2, cfl=config.cfl_number, dry_depth_m=config.dry_depth_threshold_m)
            max_pair = max(max_pair, pair_now)
            if manning_n_m is not None:
                _, source_cfl_now, _, source_error = _manning_source(
                    state, manning_n_m, gravity=config.gravity_m_s2,
                    source_cfl_fraction=source_cfl_fraction, dry_depth_m=config.dry_depth_threshold_m,
                )
                if source_error:
                    raise TriangularSolverExecutionError(
                        f"Manning source limit evaluation failed: {source_error}", time_s=time_s,
                        stage="step_selection", reason=source_error, last_reason=source_error,
                        step_kind="manning_source_cfl",
                        accepted_step_count=len(accepted_steps), rejected_step_count=sum(rejected_reasons.values()), rejected_step_reasons=rejected_reasons,
                    )
            else:
                source_cfl_now = math.inf
            cfl_now = min(wave_cfl_now, source_cfl_now)
            minimum_step_tolerance = _step_tolerance_s(config.minimum_time_step_s)
            alignment_only = remaining < config.minimum_time_step_s - minimum_step_tolerance
            if alignment_only:
                if manning_n_m is None:
                    if not math.isfinite(cfl_now):
                        cfl_now = config.maximum_time_step_s
                    alignment_reason = (
                        "alignment_cfl"
                        if remaining > cfl_now + _step_tolerance_s(max(remaining, cfl_now))
                        else None
                    )
                    failed_bound = cfl_now
                else:
                    source_bound_failed = (
                        math.isfinite(source_cfl_now)
                        and _source_cfl_bound_exceeded(remaining, source_cfl_now)
                    )
                    wave_bound_failed = (
                        math.isfinite(wave_cfl_now)
                        and remaining > wave_cfl_now + _step_tolerance_s(max(remaining, wave_cfl_now))
                    )
                    if source_bound_failed and (not wave_bound_failed or source_cfl_now <= wave_cfl_now):
                        alignment_reason = "source_cfl_alignment"
                        failed_bound = source_cfl_now
                    elif wave_bound_failed:
                        alignment_reason = "alignment_cfl"
                        failed_bound = wave_cfl_now
                    else:
                        alignment_reason = None
                        failed_bound = cfl_now
                if alignment_reason is not None:
                    raise TriangularSolverExecutionError(
                        f"output-alignment remainder {remaining:.17g} s exceeds current CFL bound {failed_bound:.17g} s",
                        time_s=time_s, stage="output_alignment", reason=alignment_reason, last_reason=alignment_reason,
                        step_kind="output_alignment", accepted_step_count=len(accepted_steps),
                        rejected_step_count=sum(rejected_reasons.values()), rejected_step_reasons=rejected_reasons,
                    )
                dt = remaining
                stage_label = "output_alignment"
            else:
                dt = min(remaining, config.maximum_time_step_s, cfl_now)
                if not math.isfinite(dt):
                    dt = min(remaining, config.maximum_time_step_s)
                if dt + minimum_step_tolerance < config.minimum_time_step_s:
                    source_limited = manning_n_m is not None and source_cfl_now <= wave_cfl_now and source_cfl_now <= config.maximum_time_step_s and source_cfl_now <= remaining
                    failure_reason = "source_cfl_below_minimum_time_step" if source_limited else "minimum_time_step"
                    raise TriangularSolverExecutionError(
                        f"no legal time step remains: {'Manning source' if source_limited else 'CFL'} bound {dt:.17g} s is below minimum_time_step_s",
                        time_s=time_s, stage="step_selection", reason=failure_reason,
                        last_reason="source_cfl" if source_limited else "wave_cfl", step_kind="cfl_step",
                        accepted_step_count=len(accepted_steps), rejected_step_count=sum(rejected_reasons.values()), rejected_step_reasons=rejected_reasons,
                    )
                stage_label = "cfl_step"
            source_cfl_constrained_step = (
                manning_n_m is not None
                and math.isfinite(source_cfl_now)
                and not alignment_only
                and source_cfl_now <= wave_cfl_now + _source_cfl_bound_tolerance_s(source_cfl_now)
                and source_cfl_now <= config.maximum_time_step_s + _source_cfl_bound_tolerance_s(source_cfl_now)
                and source_cfl_now <= remaining + _source_cfl_bound_tolerance_s(source_cfl_now)
                and abs(dt - source_cfl_now) <= _source_cfl_bound_tolerance_s(source_cfl_now)
            )
            retry_count = 0
            while True:
                budget_remaining = max(0.0, config.volume_correction_budget_fraction * capability.report["volume_correction_reference_scale_m3"] - correction_total)
                candidate, correction, corrections, cfl_after_stage, reason, detail, pair, failure_stage, attempt_source_diagnostics = _attempt_step(
                    state, dt, mesh, bed, edges,
                    gravity=config.gravity_m_s2, cfl=config.cfl_number,
                    dry_depth_m=config.dry_depth_threshold_m,
                    negative_tolerance_m=config.negative_depth_tolerance_m,
                    correction_budget_remaining_m3=budget_remaining,
                    manning_n_m=manning_n_m,
                    source_cfl_fraction=source_cfl_fraction,
                    rainfall_rate_m_s=rainfall_rate_m_s,
                )
                if reason is None and candidate is not None:
                    alignment_failure: str | None = None
                    if alignment_only and manning_n_m is None and math.isfinite(cfl_after_stage):
                        if dt > cfl_after_stage + _step_tolerance_s(max(dt, cfl_after_stage)):
                            alignment_failure = "alignment_cfl"
                    elif alignment_only:
                        source_bound = attempt_source_diagnostics["minimum_source_cfl_limit_s"]
                        wave_bound = attempt_source_diagnostics["minimum_wave_cfl_limit_s"]
                        source_failed = (
                            source_bound is not None
                            and _source_cfl_bound_exceeded(dt, source_bound)
                        )
                        wave_failed = (
                            wave_bound is not None and math.isfinite(float(wave_bound))
                            and dt > wave_bound + _step_tolerance_s(max(dt, float(wave_bound)))
                        )
                        if source_failed and (not wave_failed or source_bound <= wave_bound):
                            alignment_failure = "source_cfl_alignment"
                        elif wave_failed:
                            alignment_failure = "alignment_cfl"
                    if alignment_failure is not None:
                        reason = alignment_failure
                        detail = (
                            "alignment step exceeds the freshly recomputed Manning source CFL limit"
                            if alignment_failure == "source_cfl_alignment"
                            else "alignment step exceeds the freshly recomputed wave CFL limit"
                        )
                        failure_stage = "stage2"
                    else:
                        rain_volume_increment = 0.0
                        next_rainfall_cumulative = cumulative_rainfall_volume
                        next_rainfall_interval_volume = rainfall_volume_by_interval[interval_index]
                        if rainfall_rate_m_s is not None:
                            try:
                                rain_volume_increment = math.fsum(
                                    0.5 * (rainfall_rate_m_s + rainfall_rate_m_s) * dt * float(area)
                                    for area in mesh.face_area
                                )
                                next_rainfall_cumulative = math.fsum((cumulative_rainfall_volume, rain_volume_increment))
                                next_rainfall_interval_volume = math.fsum((rainfall_volume_by_interval[interval_index], rain_volume_increment))
                            except OverflowError as exc:
                                raise TriangularSolverExecutionError(
                                    "accepted rainfall stage produced an unrepresentable mass increment",
                                    time_s=time_s, stage="source_accounting", reason="non_finite_rainfall_volume",
                                    accepted_step_count=len(accepted_steps), rejected_step_count=sum(rejected_reasons.values()),
                                    rejected_step_reasons=rejected_reasons,
                                ) from exc
                            if not all(math.isfinite(value) for value in (rain_volume_increment, next_rainfall_cumulative, next_rainfall_interval_volume)):
                                raise TriangularSolverExecutionError(
                                    "accepted rainfall stage produced a non-finite mass increment",
                                    time_s=time_s, stage="source_accounting", reason="non_finite_rainfall_volume",
                                    accepted_step_count=len(accepted_steps), rejected_step_count=sum(rejected_reasons.values()),
                                    rejected_step_reasons=rejected_reasons,
                                )
                        reaches_output = abs(dt - remaining) <= _step_tolerance_s(max(dt, remaining))
                        elapsed, elapsed_correction = _compensated_add(elapsed, elapsed_correction, dt)
                        # Advance the state by the exact dt used by both RK
                        # stages.  Snap only the clock to the requested output
                        # when the residual is within the shared ULP tolerance;
                        # never enlarge dt to make the statistics look tidy.
                        time_s = float(end_time) if reaches_output else math.fsum((interval_start, elapsed, elapsed_correction))
                        state = candidate
                        max_pair = max(max_pair, pair)
                        if manning_n_m is not None:
                            accepted_source_diagnostics = _merge_manning_diagnostics(accepted_source_diagnostics, attempt_source_diagnostics)
                            source_cfl_limited_step_count += int(source_cfl_constrained_step)
                        if rainfall_rate_m_s is not None:
                            cumulative_rainfall_volume = next_rainfall_cumulative
                            rainfall_volume_by_interval[interval_index] = next_rainfall_interval_volume
                            rainfall_stage_evaluation_count += 2
                            if rainfall_rate_m_s > 0.0:
                                rainfall_nonzero_source_stage_evaluation_count += 2
                        accepted_steps.append(dt)
                        if alignment_only:
                            alignment_steps_by_interval[interval_index] = dt
                            if manning_n_m is None:
                                alignment_limits_by_interval[interval_index] = float(
                                    cfl_after_stage if math.isfinite(cfl_after_stage) else config.maximum_time_step_s
                                )
                            else:
                                alignment_bounds = [
                                    float(value) for value in (
                                        attempt_source_diagnostics["minimum_wave_cfl_limit_s"],
                                        attempt_source_diagnostics["minimum_source_cfl_limit_s"],
                                    )
                                    if value is not None and math.isfinite(float(value))
                                ]
                                alignment_limits_by_interval[interval_index] = min(alignment_bounds) if alignment_bounds else config.maximum_time_step_s
                        else:
                            cfl_steps.append(dt)
                        correction_total += correction
                        correction_count += corrections
                        break
                if reason is None:
                    reason, detail = "non_finite_state", "step attempt returned no candidate state"
                if reason == "second_stage_source_cfl":
                    source_cfl_constrained_step = True
                    if alignment_only:
                        reason = "source_cfl_alignment"
                rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                if reason in {
                    "non_finite_state", "non_finite_source", "invalid_manning_source_binding",
                    "invalid_rainfall_source_binding",
                    "source_cfl_limit_underflow", "source_cfl_limit_overflow", "invalid_source_cfl_limit",
                }:
                    raise TriangularSolverExecutionError(
                        detail or "a non-finite stage state was found", time_s=time_s, stage=failure_stage or stage_label, reason=reason,
                        last_reason=reason, step_kind=stage_label,
                        accepted_step_count=len(accepted_steps), rejected_step_count=sum(rejected_reasons.values()), rejected_step_reasons=rejected_reasons,
                    )
                retry_count += 1
                if alignment_only:
                    raise TriangularSolverExecutionError(
                        detail or f"output-alignment attempt failed: {reason}", time_s=time_s, stage=failure_stage or "output_alignment", reason=reason,
                        last_reason="second_stage_source_cfl" if reason == "source_cfl_alignment" else reason,
                        step_kind="output_alignment",
                        accepted_step_count=len(accepted_steps), rejected_step_count=sum(rejected_reasons.values()), rejected_step_reasons=rejected_reasons,
                    )
                if retry_count > config.maximum_step_retries:
                    raise TriangularSolverExecutionError(
                        detail or f"step retry limit exhausted after {retry_count - 1} retries", time_s=time_s, stage=failure_stage or stage_label,
                        reason="retry_exhausted", accepted_step_count=len(accepted_steps), rejected_step_count=sum(rejected_reasons.values()), rejected_step_reasons=rejected_reasons,
                        last_reason=reason, step_kind=stage_label,
                    )
                if reason in {"second_stage_cfl", "second_stage_source_cfl"} and math.isfinite(cfl_after_stage):
                    next_dt = min(dt * 0.5, cfl_after_stage)
                else:
                    next_dt = dt * 0.5
                if next_dt + minimum_step_tolerance < config.minimum_time_step_s:
                    minimum_failure_reason = (
                        "source_cfl_below_minimum_time_step"
                        if reason == "second_stage_source_cfl"
                        else "minimum_time_step"
                    )
                    raise TriangularSolverExecutionError(
                        f"retry candidate {next_dt:.17g} s is below minimum_time_step_s",
                        time_s=time_s, stage=failure_stage or stage_label, reason=minimum_failure_reason,
                        accepted_step_count=len(accepted_steps), rejected_step_count=sum(rejected_reasons.values()), rejected_step_reasons=rejected_reasons,
                        last_reason=reason, step_kind=stage_label,
                    )
                dt = next_dt
            # Prevent a last-bit roundoff loop without moving an accepted state.
            if time_s == end_time:
                break
            if len(accepted_steps) > 10_000_000:
                raise TriangularSolverExecutionError("accepted-step safety limit exceeded", time_s=time_s, stage="integration", reason="step_safety_limit", accepted_step_count=len(accepted_steps), rejected_step_count=sum(rejected_reasons.values()), rejected_step_reasons=rejected_reasons)
        rows.append(state.copy())
        correction_by_output.append(correction_total - output_correction_start)
        cumulative_rainfall_volume_by_output.append(cumulative_rainfall_volume)
        record_output_manning_diagnostics(state, float(end_time))

    if not accepted_steps:
        raise TriangularSolverExecutionError("positive-duration run completed without an accepted step", time_s=time_s, stage="integration", reason="no_accepted_steps", accepted_step_count=0, rejected_step_count=sum(rejected_reasons.values()), rejected_step_reasons=rejected_reasons)
    stats = {
        "accepted_step_count": len(accepted_steps),
        "accepted_cfl_step_count": len(cfl_steps),
        "rejected_step_count": sum(rejected_reasons.values()),
        "rejected_reasons": rejected_reasons,
        "mass_correction_count": correction_count,
        "minimum_cfl_step_s": min(cfl_steps),
        "maximum_cfl_step_s": max(cfl_steps),
        "minimum_accepted_step_s": min(accepted_steps),
        "maximum_accepted_step_s": max(accepted_steps),
        "alignment_steps": alignment_steps_by_interval,
        "alignment_limits": alignment_limits_by_interval,
        "correction_by_output": correction_by_output,
        "maximum_pair_residual": max_pair,
        "config_sha256": _sha256_file(capability.config_path),
        "mesh_sha256": _sha256_file(capability.mesh_path),
        "business_sha256": _sha256_file(capability.business_path),
    }
    if manning_enabled:
        stats.update({
            "manning_enabled": True,
            "manning_n_m": list(manning_n_m),
            "source_cfl_fraction": float(source_cfl_fraction),
            "maximum_manning_gamma_per_s": max(
                accepted_source_diagnostics["maximum_manning_gamma_per_s"],
                max(maximum_gamma_by_output, default=0.0),
            ),
            "maximum_manning_source_magnitude_m2_s2": max(
                accepted_source_diagnostics["maximum_manning_source_magnitude_m2_s2"],
                max(maximum_source_magnitude_by_output, default=0.0),
            ),
            "maximum_manning_gamma_per_s_by_time": maximum_gamma_by_output,
            "maximum_manning_source_magnitude_m2_s2_by_time": maximum_source_magnitude_by_output,
            "manning_nonzero_face_stage_evaluation_count": accepted_source_diagnostics["manning_nonzero_face_stage_evaluation_count"],
            "source_cfl_limit_evaluation_count": accepted_source_diagnostics["source_cfl_limit_evaluation_count"],
            "minimum_source_cfl_limit_s": accepted_source_diagnostics["minimum_source_cfl_limit_s"],
            "maximum_source_cfl_limit_s": accepted_source_diagnostics["maximum_source_cfl_limit_s"],
            "manning_source_cfl_limited_step_count": source_cfl_limited_step_count,
            "manning_source_cfl_rejected_step_count": (
                rejected_reasons.get("second_stage_source_cfl", 0)
                + rejected_reasons.get("source_cfl_alignment", 0)
            ),
            "nonzero_manning_effect_applied": (
                accepted_source_diagnostics["manning_nonzero_face_stage_evaluation_count"] > 0
                and accepted_source_diagnostics["maximum_manning_source_magnitude_m2_s2"] > 0.0
            ),
        })
    if rainfall_enabled:
        stats.update({
            "rainfall_enabled": True,
            "rainfall_rate_m_s_by_interval": list(rainfall_rate_m_s_by_interval),
            "rainfall_volume_m3_by_interval": rainfall_volume_by_interval,
            "cumulative_rainfall_volume_m3_by_time": cumulative_rainfall_volume_by_output,
            "rainfall_total_volume_m3": cumulative_rainfall_volume,
            "rainfall_total_depth_m": cumulative_rainfall_volume / total_area_m2,
            "rainfall_stage_evaluation_count": rainfall_stage_evaluation_count,
            "rainfall_nonzero_source_stage_evaluation_count": rainfall_nonzero_source_stage_evaluation_count,
            "rainfall_nonzero_effect_applied": rainfall_nonzero_source_stage_evaluation_count > 0,
            "source_volume_by_output": cumulative_rainfall_volume_by_output,
        })
    result_data = _make_result_data(capability, rows, stats)
    result: TriangularResultContract | dict[str, Any]
    if rainfall_enabled:
        result = result_data
    else:
        result = TriangularResultContract.model_validate(result_data)
    return result, stats


def _sha256_file(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_triangular_solver(run_config_path: str | Path) -> dict[str, Any]:
    """Validate, execute and atomically publish one experimental closed-wall run."""
    capability = validate_triangular_run_request(run_config_path)
    result, _ = _integrate(capability)
    result = validate_triangular_result_for_run(result, run_config_path)
    from .triangular_solver_netcdf import write_triangular_result_atomic
    output_path = write_triangular_result_atomic(result, run_config_path)
    return {
        "status": "completed",
        "experimental": True,
        "production_approved": False,
        "case_id": result.case_id,
        "result_file": str(output_path),
        "security_exclusion": capability.report["security_exclusion"],
        "accepted_step_count": result.accepted_step_count,
        "accepted_cfl_step_count": result.accepted_cfl_step_count,
        "rejected_step_count": result.rejected_step_count,
        "rejected_step_reasons": result.rejected_step_reasons,
        "minimum_water_depth_m_by_time": result.minimum_water_depth_m_by_time,
        "maximum_speed_m_s_by_time": result.maximum_speed_m_s_by_time,
        "maximum_internal_edge_mass_pair_residual_m3_s": result.maximum_internal_edge_mass_pair_residual_m3_s,
        "mass_correction_count": result.mass_correction_count,
        "mass_correction_total_m3": result.mass_budget[-1].correction_volume_m3,
        "mass_budget": [item.model_dump(mode="json") for item in result.mass_budget],
        "minimum_cfl_step_s": result.minimum_cfl_step_s,
        "maximum_cfl_step_s": result.maximum_cfl_step_s,
        "minimum_accepted_step_s": result.minimum_accepted_step_s,
        "maximum_accepted_step_s": result.maximum_accepted_step_s,
        "subminimum_alignment_step_s": result.subminimum_alignment_step_s,
        "subminimum_alignment_cfl_limit_s": result.subminimum_alignment_cfl_limit_s,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the experimental closed-wall triangular shallow-water solver.")
    parser.add_argument("run_config", help="P4-1 triangular solver YAML run config")
    args = parser.parse_args(argv)
    try:
        report = run_triangular_solver(args.run_config)
    except (TriangularSolverContractError, TriangularSolverExecutionError, OSError, ValueError) as exc:
        if isinstance(exc, TriangularSolverExecutionError):
            print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2))
            return 2
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a module entry point
    raise SystemExit(main())


__all__ = ["SOLVER_VERSION", "TriangularSolverExecutionError", "main", "run_triangular_solver"]
