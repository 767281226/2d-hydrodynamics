"""Streaming long-run driver for the experimental triangular shallow-water scheme.

This is the large-run path (O(10^5) faces, O(10^6) steps).  It uses exactly the
same spatial scheme as :mod:`triangular_solver` (first-order HLL, Audusse
hydrostatic reconstruction, closed-wall reflection, frozen wave CFL, SSPRK2,
dry threshold, uniform rainfall source) through the vectorised kernels in
:mod:`triangular_solver_kernels`, and adds, as explicit opt-in options:

* point-implicit Manning friction (no friction step limit);
* a correction-budget reference that grows with cumulative rainfall and
  boundary inflow volume;
* prescribed-discharge open boundary groups (inflow / capped outflow) and
  stage-dependent free-outflow groups (critical-flow weir law, capped);
* a per-output frame sink (frames are handed off, never retained);
* bounded per-face maxima and a compact per-interval mass ledger;
* atomic checkpoints with exact restart.

The legacy in-memory result path (:func:`triangular_solver.run_triangular_solver`
and the source/Manning runners) is unchanged and does not use this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

import numpy as np
from pydantic import Field, StrictInt, ValidationError, field_validator, model_validator

from .config import Number, Text
from .triangular_solver import (
    TriangularSolverExecutionError,
    _Sanitized,
    _edges,
    _sanitize_stage,
)
from .triangular_solver_contract import (
    _StrictContractModel,
    _source_cfl_bound_exceeded,
    _step_tolerance_s,
)
from .triangular_solver_kernels import (
    EdgeGeometry,
    NumbaWorkspace,
    manning_explicit,
    manning_point_implicit,
    numba_status,
    select_rhs_kernel,
    spatial_face_order,
    stage_and_sanitize,
    stage_sanitize_numba,
    update_face_maxima,
)


LONG_RUN_OPTIONS_FORMAT = "triangular-long-run-options-v1"
OPEN_BOUNDARY_FORMAT = "triangular-open-boundary-group-v1"
CHECKPOINT_FORMAT = "triangular-long-run-checkpoint-v1"
LONG_RUN_SOLVER_VERSION = "triangular-long-run-1.0.0"
# Critical depth over a broad crest: Q/L = (2/3)^(3/2) sqrt(g) H^(3/2).
FREE_OUTFLOW_DEFAULT_DISCHARGE_COEFFICIENT = (2.0 / 3.0) ** 1.5


class TriangularLongRunError(ValueError):
    """Invalid long-run options, inputs, boundary groups or checkpoint."""


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------
class TriangularLongRunOptions(_StrictContractModel):
    """Numerical and run-control options for the streaming long-run path.

    Units are SI.  Numerical defaults equal the P4-1 run config defaults; the
    three new treatment options default to the large-run choices.
    """

    format: Literal["triangular-long-run-options-v1"] = LONG_RUN_OPTIONS_FORMAT
    schema_version: Literal["1.0"] = "1.0"
    gravity_m_s2: Number = Field(default=9.81, gt=0.0, le=20.0)
    cfl_number: Number = Field(default=0.25, gt=0.0, le=0.5)
    # Proposed step = step_cfl_fraction * current CFL bound.  1.0 reproduces the
    # P4-1 selection rule exactly; the 0.9 default avoids the near-certain
    # second-stage CFL rejection (and its wasted RHS evaluation) while depths
    # are rising.  It changes only step selection, never the scheme.
    step_cfl_fraction: Number = Field(default=0.9, gt=0.0, le=1.0)
    minimum_time_step_s: Number = Field(default=1e-6, ge=1e-9, le=1.0)
    maximum_time_step_s: Number = Field(default=60.0, gt=0.0, le=86_400.0)
    maximum_step_retries: StrictInt = Field(default=8, ge=0, le=20)
    dry_depth_threshold_m: Number = Field(default=1e-6, gt=0.0, le=0.1)
    negative_depth_tolerance_m: Number = Field(default=1e-12, ge=0.0, le=1e-4)
    volume_correction_budget_fraction: Number = Field(default=1e-10, ge=0.0, le=1e-6)
    # "initial_state" = P4-1 rule max(V0, A*max(h0), 1 m3).
    # "initial_state_plus_cumulative_inflow" = max(V0 + rain + boundary inflow so far, A*max(h0), 1 m3).
    volume_correction_reference: Literal["initial_state", "initial_state_plus_cumulative_inflow"] = (
        "initial_state_plus_cumulative_inflow"
    )
    # "none": no friction (manning_n must be absent).  "explicit_source_cfl":
    # P4-2 explicit drag with step bound source_cfl_fraction/gamma.
    # "point_implicit": q <- q / (1 + dt g n^2 |q| / h^(7/3)) per RK stage.
    manning_friction: Literal["none", "explicit_source_cfl", "point_implicit"] = "point_implicit"
    source_cfl_fraction: Number | None = Field(default=None, gt=0.0, le=1.0)
    # Fraction of a face's water above the dry threshold that prescribed or free
    # outflow may remove in one RK stage.
    outflow_stage_volume_fraction: Number = Field(default=0.5, gt=0.0, le=1.0)
    maximum_accepted_step_count: StrictInt = Field(default=1_000_000_000, ge=1, le=1_000_000_000_000)
    checkpoint_interval_s: Number | None = Field(default=None, gt=0.0, le=31_536_000.0)
    checkpoint_keep_count: StrictInt = Field(default=2, ge=1, le=1000)
    kernel: Literal["auto", "numpy", "numba"] = "auto"
    progress_interval_steps: StrictInt = Field(default=1000, ge=1, le=1_000_000_000)

    @model_validator(mode="after")
    def consistent(self) -> "TriangularLongRunOptions":
        if self.minimum_time_step_s > self.maximum_time_step_s:
            raise ValueError("minimum_time_step_s must be less than or equal to maximum_time_step_s")
        if self.negative_depth_tolerance_m >= self.dry_depth_threshold_m:
            raise ValueError("negative_depth_tolerance_m must be less than dry_depth_threshold_m")
        if self.manning_friction == "explicit_source_cfl" and self.source_cfl_fraction is None:
            raise ValueError("source_cfl_fraction in (0, 1] is required when manning_friction is explicit_source_cfl")
        if self.manning_friction != "explicit_source_cfl" and self.source_cfl_fraction is not None:
            raise ValueError("source_cfl_fraction is only valid with manning_friction explicit_source_cfl")
        return self

    def numerics_fingerprint(self) -> str:
        """SHA-256 of the options that change results (excludes run-control fields)."""
        data = self.model_dump(mode="json")
        for name in ("checkpoint_interval_s", "checkpoint_keep_count", "kernel", "progress_interval_steps",
                     "maximum_accepted_step_count"):
            data.pop(name)
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def _check_series(times: list[float], values: list[float], time_name: str, value_name: str) -> None:
    if len(times) != len(values):
        raise ValueError(f"{time_name} and {value_name} must have the same length")
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError(f"{time_name} must be strictly increasing")
    if any(value < 0.0 for value in values):
        raise ValueError(f"{value_name} values are magnitudes and must be >= 0; use role to set direction")


_FREE_OUTFLOW_ONLY_FIELDS = ("discharge_coefficient", "crest_elevation_m", "reference_time_s", "reference_discharge_m3_s")


class OpenBoundaryGroupSpec(_StrictContractModel):
    """Open boundary group on exterior mesh edges.

    ``inflow`` / ``outflow``: prescribed discharge.  ``discharge_m3_s`` are
    non-negative magnitudes; ``role`` gives the direction.  Q(t) is linearly
    interpolated between samples; evaluating outside [time_s[0], time_s[-1]] is
    an error, and the samples must cover the whole run.

    ``free_outflow``: stage-dependent outflow (experimental critical-flow weir
    law q_e = C_d sqrt(g) H^(3/2), H = max(0, min(h, z + h - z_crest)), outward only).
    It takes no prescribed series; ``reference_time_s`` /
    ``reference_discharge_m3_s`` (both or neither) are only integrated into the
    ledger for comparison and never applied.
    """

    format: Literal["triangular-open-boundary-group-v1"] = OPEN_BOUNDARY_FORMAT
    name: Text
    role: Literal["inflow", "outflow", "free_outflow"]
    edge_indices: list[StrictInt] = Field(min_length=1)
    # s / m3/s.  Required for inflow and outflow, forbidden for free_outflow.
    time_s: list[Number] | None = Field(default=None, min_length=2, max_length=10_000_000)
    discharge_m3_s: list[Number] | None = Field(default=None, min_length=2, max_length=10_000_000)
    distribution: Literal["length", "conveyance_depth_5_3"] = "length"
    # free_outflow only.  Dimensionless, (0, 1.5]; null = (2/3)^(3/2) ~= 0.5443.
    discharge_coefficient: Number | None = Field(default=None, gt=0.0, le=1.5)
    # free_outflow only.  m, bed datum, [-1000, 10000]; null = bed of the adjacent face (H = h).
    crest_elevation_m: Number | None = Field(default=None, ge=-1000.0, le=10000.0)
    # free_outflow only.  Comparison series (s / m3/s), recorded in the ledger, never applied.
    reference_time_s: list[Number] | None = Field(default=None, min_length=2, max_length=10_000_000)
    reference_discharge_m3_s: list[Number] | None = Field(default=None, min_length=2, max_length=10_000_000)

    @property
    def effective_discharge_coefficient(self) -> float:
        return FREE_OUTFLOW_DEFAULT_DISCHARGE_COEFFICIENT if self.discharge_coefficient is None else float(self.discharge_coefficient)

    @property
    def series_time_s(self) -> list[float] | None:
        """Time samples that must cover the run (prescribed series, or the free-outflow reference)."""
        return self.reference_time_s if self.role == "free_outflow" else self.time_s

    @field_validator("name")
    @classmethod
    def plain_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be non-empty")
        return value

    @model_validator(mode="after")
    def series(self) -> "OpenBoundaryGroupSpec":
        if self.role == "free_outflow":
            if self.time_s is not None or self.discharge_m3_s is not None:
                raise ValueError("role free_outflow computes Q from the local water level; time_s/discharge_m3_s are not allowed "
                                 "(use reference_time_s/reference_discharge_m3_s for a comparison series)")
            if self.distribution != "length":
                raise ValueError("distribution does not apply to role free_outflow (every edge uses its own head)")
            if (self.reference_time_s is None) != (self.reference_discharge_m3_s is None):
                raise ValueError("reference_time_s and reference_discharge_m3_s must be given together or not at all")
            if self.reference_time_s is not None:
                _check_series(self.reference_time_s, self.reference_discharge_m3_s, "reference_time_s", "reference_discharge_m3_s")
        else:
            if self.time_s is None or self.discharge_m3_s is None:
                raise ValueError(f"role {self.role} requires time_s and discharge_m3_s")
            given = [name for name in _FREE_OUTFLOW_ONLY_FIELDS if getattr(self, name) is not None]
            if given:
                raise ValueError(f"{', '.join(given)} only valid for role free_outflow")
            _check_series(self.time_s, self.discharge_m3_s, "time_s", "discharge_m3_s")
        if any(index < 0 for index in self.edge_indices):
            raise ValueError("edge_indices must be non-negative mesh edge indices")
        if len(set(self.edge_indices)) != len(self.edge_indices):
            raise ValueError("edge_indices must be unique")
        return self


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class TriangularLongRunInputs:
    """Face-aligned case arrays for one long run (SI units, mesh face order)."""

    mesh: Any
    bed_m: np.ndarray
    initial_depth_m: np.ndarray
    output_times_s: np.ndarray
    manning_n: np.ndarray | None = None
    rainfall_rate_m_s_by_interval: np.ndarray | None = None
    open_boundaries: tuple[OpenBoundaryGroupSpec, ...] = ()

    @classmethod
    def build(
        cls,
        mesh: Any,
        *,
        bed_m: Sequence[float],
        initial_depth_m: Sequence[float],
        output_times_s: Sequence[float],
        manning_n: Sequence[float] | None = None,
        rainfall_rate_m_s_by_interval: Sequence[float] | None = None,
        open_boundaries: Sequence[OpenBoundaryGroupSpec | Mapping[str, Any]] = (),
    ) -> "TriangularLongRunInputs":
        def array(values: Sequence[float], name: str) -> np.ndarray:
            result = np.array(values, dtype=np.float64)
            if result.ndim != 1 or not np.isfinite(result).all():
                raise TriangularLongRunError(f"{name} must be a finite 1-D array")
            result.setflags(write=False)
            return result

        n_face = mesh.n_face
        bed = array(bed_m, "bed_m")
        depth = array(initial_depth_m, "initial_depth_m")
        times = array(output_times_s, "output_times_s")
        if bed.size != n_face or depth.size != n_face:
            raise TriangularLongRunError("bed_m and initial_depth_m must contain one value per mesh face")
        if (depth < 0.0).any():
            raise TriangularLongRunError("initial_depth_m must be non-negative")
        if times.size < 2 or times[0] != 0.0 or (np.diff(times) <= 0.0).any() or times[-1] > 31_536_000.0:
            raise TriangularLongRunError("output_times_s must start at 0 s, be strictly increasing, and end <= 31,536,000 s")
        n_array = None
        if manning_n is not None:
            n_array = array(manning_n, "manning_n")
            if n_array.size != n_face or ((n_array <= 0.0) | (n_array > 1.0)).any():
                raise TriangularLongRunError("manning_n must contain one SI value in (0, 1] per mesh face")
        rain = None
        if rainfall_rate_m_s_by_interval is not None:
            rain = array(rainfall_rate_m_s_by_interval, "rainfall_rate_m_s_by_interval")
            if rain.size != times.size - 1 or (rain < 0.0).any():
                raise TriangularLongRunError("rainfall_rate_m_s_by_interval must contain one non-negative m/s rate per output interval")
        groups: list[OpenBoundaryGroupSpec] = []
        for item in open_boundaries:
            try:
                groups.append(item if isinstance(item, OpenBoundaryGroupSpec) else OpenBoundaryGroupSpec.model_validate(item))
            except ValidationError as exc:
                raise TriangularLongRunError(f"invalid open boundary group: {exc}") from exc
        names = [group.name for group in groups]
        if len(set(names)) != len(names):
            raise TriangularLongRunError("open boundary group names must be unique")
        used: set[int] = set()
        n_edge = len(mesh.edge_nodes)
        for group in groups:
            for edge in group.edge_indices:
                if edge >= n_edge:
                    raise TriangularLongRunError(f"open boundary group {group.name!r}: edge index {edge} is outside the mesh ({n_edge} edges)")
                if mesh.edge_faces[edge][1] != -1:
                    raise TriangularLongRunError(f"open boundary group {group.name!r}: edge index {edge} is an interior edge, not an exterior edge")
                if edge in used:
                    raise TriangularLongRunError(f"open boundary group {group.name!r}: edge index {edge} belongs to more than one group")
                used.add(edge)
            samples = group.series_time_s
            if samples is not None and (samples[0] > 0.0 or samples[-1] < float(times[-1])):
                label = "reference discharge samples" if group.role == "free_outflow" else "discharge samples"
                raise TriangularLongRunError(
                    f"open boundary group {group.name!r}: {label} cover [{samples[0]}, {samples[-1]}] s "
                    f"but the run needs [0, {float(times[-1])}] s"
                )
        return cls(mesh, bed, depth, times, n_array, rain, tuple(groups))

    @classmethod
    def from_capability(
        cls,
        capability: Any,
        *,
        manning_n: Sequence[float] | None = None,
        rainfall_rate_m_s_by_interval: Sequence[float] | None = None,
        open_boundaries: Sequence[OpenBoundaryGroupSpec | Mapping[str, Any]] = (),
    ) -> "TriangularLongRunInputs":
        """Build inputs from a validated :class:`TriangularRunCapability`."""
        business = capability.business
        terrain = business.terrain
        initial = business.initial_condition
        if initial.quantity.value == "depth":
            depth = [float(value) for value in initial.values_m]
        else:
            depth = [max(float(level) - float(bed), 0.0) for level, bed in zip(initial.values_m, terrain.elevation_m)]
        return cls.build(
            business.mesh, bed_m=terrain.elevation_m, initial_depth_m=depth,
            output_times_s=capability.config.output_times_s, manning_n=manning_n,
            rainfall_rate_m_s_by_interval=rainfall_rate_m_s_by_interval, open_boundaries=open_boundaries,
        )


def open_boundary_edges_for_mesh_group(mesh: Any, group_name: str) -> list[int]:
    """Return exterior edge indices of a named P2 ``boundary_groups`` entry."""
    try:
        return [int(index) for index in mesh.boundary_groups[group_name]]
    except KeyError as exc:
        raise TriangularLongRunError(f"mesh has no boundary group {group_name!r}") from exc


# --------------------------------------------------------------------------
# Frames and sinks
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TriangularFrame:
    """One saved output state.  Arrays are fresh read-only copies (SI units)."""

    output_index: int
    time_s: float
    water_depth_m: np.ndarray
    unit_discharge_x_m2_s: np.ndarray
    unit_discharge_y_m2_s: np.ndarray
    velocity_x_m_s: np.ndarray
    velocity_y_m_s: np.ndarray
    water_level_m: np.ndarray
    ledger: Mapping[str, Any]


class FrameSink(Protocol):
    def __call__(self, frame: TriangularFrame) -> None: ...


class MemoryFrameSink:
    """Small-case sink that keeps every frame (do not use for long runs)."""

    def __init__(self) -> None:
        self.frames: list[TriangularFrame] = []

    def __call__(self, frame: TriangularFrame) -> None:
        self.frames.append(frame)


def _readonly(array: np.ndarray) -> np.ndarray:
    result = np.array(array, dtype=np.float64, copy=True)
    result.setflags(write=False)
    return result


# --------------------------------------------------------------------------
# Open boundaries (prescribed discharge)
# --------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class _BoundaryGroup:
    spec: OpenBoundaryGroupSpec
    edges: np.ndarray        # mesh edge indices
    faces: np.ndarray        # interior face of each edge
    local: np.ndarray        # position of each edge's face in _BoundarySet.faces
    length: np.ndarray       # m
    nx: np.ndarray           # outward unit normal
    ny: np.ndarray
    factor: np.ndarray       # length / area[face], 1/m
    times: np.ndarray        # prescribed series, or the free-outflow reference series (empty if none)
    discharge: np.ndarray
    bed: np.ndarray          # bed elevation of each edge's face, m
    crest: np.ndarray | None  # free_outflow crest elevation per edge, m; None = face bed (head = depth)
    coefficient: float       # free_outflow discharge coefficient C_d

    @property
    def free(self) -> bool:
        return self.spec.role == "free_outflow"

    @property
    def has_reference(self) -> bool:
        return self.free and self.times.size > 0

    def discharge_at(self, time_s: float) -> float:
        tolerance = _step_tolerance_s(max(abs(time_s), 1.0))
        if time_s < self.times[0] - tolerance or time_s > self.times[-1] + tolerance:
            raise TriangularSolverExecutionError(
                f"open boundary group {self.spec.name!r} discharge requested at t={time_s:.17g} s outside its samples",
                time_s=time_s, stage="open_boundary", reason="discharge_series_out_of_range",
                accepted_step_count=0, rejected_step_count=0, rejected_step_reasons={},
            )
        return float(np.interp(time_s, self.times, self.discharge))


@dataclass(frozen=True, eq=False)
class _BoundarySet:
    """All open boundary groups plus the sorted union of their interior faces.

    Boundary terms are evaluated on this compact face set only (a few hundred
    faces) and scattered into a persistent dense ``(n_face, 3)`` buffer whose
    other rows stay exactly zero, so the stage arithmetic is unchanged.
    """

    groups: tuple[_BoundaryGroup, ...]
    faces: np.ndarray        # sorted unique interior faces of all groups

    def __len__(self) -> int:
        return len(self.groups)

    def __iter__(self):
        return iter(self.groups)


def _prepare_boundary_groups(inputs: TriangularLongRunInputs, geometry: EdgeGeometry,
                             face_map: np.ndarray | None = None) -> _BoundarySet:
    """Boundary groups; ``face_map`` (mesh face -> storage position) selects the per-face array layout."""
    edge_lists = [np.asarray(spec.edge_indices, dtype=np.int64) for spec in inputs.open_boundaries]
    stored = (lambda faces: faces) if face_map is None else (lambda faces: face_map[faces])
    union = np.unique(np.concatenate([stored(geometry.left[edges]) for edges in edge_lists])) if edge_lists else np.zeros(0, dtype=np.int64)
    groups = []
    bed = np.asarray(inputs.bed_m, dtype=np.float64)
    for spec, edges in zip(inputs.open_boundaries, edge_lists):
        faces = stored(geometry.left[edges])
        free = spec.role == "free_outflow"
        series_t = spec.reference_time_s if free else spec.time_s
        series_q = spec.reference_discharge_m3_s if free else spec.discharge_m3_s
        crest = np.full(edges.size, float(spec.crest_elevation_m)) if free and spec.crest_elevation_m is not None else None
        groups.append(_BoundaryGroup(
            spec=spec, edges=edges, faces=faces, local=np.searchsorted(union, faces), length=geometry.length[edges],
            nx=geometry.nx[edges], ny=geometry.ny[edges], factor=geometry.left_factor[edges],
            times=np.asarray(series_t if series_t is not None else [], dtype=np.float64),
            discharge=np.asarray(series_q if series_q is not None else [], dtype=np.float64),
            bed=bed[geometry.left[edges]], crest=crest, coefficient=spec.effective_discharge_coefficient,
        ))
    return _BoundarySet(tuple(groups), union)


@dataclass
class _BoundaryStage:
    derivative: np.ndarray | None          # additive (n_face, 3) term, or None
    inflow_m3_s: dict[str, float]
    outflow_requested_m3_s: dict[str, float]
    outflow_delivered_m3_s: dict[str, float]
    reference_outflow_m3_s: dict[str, float] = field(default_factory=dict)  # free_outflow comparison series only


def _edge_weights(group: _BoundaryGroup, depth: np.ndarray, dry_depth_m: float, *, wet_only: bool) -> np.ndarray:
    h = depth[group.faces]
    wet = h > dry_depth_m
    if group.spec.distribution == "conveyance_depth_5_3":
        weights = group.length * np.where(wet, np.maximum(h, 0.0), 0.0) ** (5.0 / 3.0)
    else:
        weights = np.where(wet, group.length, 0.0) if wet_only else group.length.copy()
    if not wet_only and not (weights > 0.0).any():
        weights = group.length.copy()  # all-dry inflow section: length-weighted
    return weights


def _inflow_unit_discharge(group: _BoundaryGroup, depth: np.ndarray, time_s: float, dry_depth_m: float) -> np.ndarray:
    discharge = group.discharge_at(time_s)
    if discharge == 0.0:
        return np.zeros(group.edges.size, dtype=np.float64)
    weights = _edge_weights(group, depth, dry_depth_m, wet_only=False)
    # q_e [m2/s] with sum(q_e L_e) == Q: q_e = Q w_e / (L_e sum(w)).
    return discharge * (weights / math.fsum(weights.tolist())) / group.length


def _inflow_ghost_depth(q: np.ndarray, h_interior: np.ndarray, gravity: float) -> np.ndarray:
    critical = np.cbrt(q * q / gravity)
    return np.maximum(h_interior, critical)


def _free_outflow_unit_discharge(group: _BoundaryGroup, depth: np.ndarray, gravity: float, dry_depth_m: float) -> np.ndarray:
    """Critical-flow weir law per edge, q_e = C_d sqrt(g) H^(3/2) in m2/s, outward only.

    H = h on a wet face when the crest is the face bed, otherwise
    H = max(0, min(h, (z + h) - z_crest)).  The head never exceeds the water
    depth on the face: a crest below the face bed cannot draw more than critical
    flow from depth h, which keeps q/h <= C_d sqrt(g h) and the CFL bound finite
    for thin films.  Dry faces (h <= dry threshold) give 0, so a pool at or below
    the crest is exactly a closed wall.
    """
    h = depth[group.faces]
    wet = h > dry_depth_m
    if group.crest is None:
        head = np.where(wet, h, 0.0)
    else:
        head = np.where(wet, np.maximum(np.minimum(h, (group.bed + h) - group.crest), 0.0), 0.0)
    if not head.any():
        return head  # all zeros: exactly the result below, without the extra array work
    return (group.coefficient * math.sqrt(gravity)) * (head * np.sqrt(head))


def _boundary_wave_sums(boundaries: _BoundarySet, state: np.ndarray, time_s: float,
                        *, gravity: float, dry_depth_m: float) -> np.ndarray | None:
    """Extra sum_e L_e a_e on ``boundaries.faces`` (added to the wall wave sums).

    Inflow adds its ghost wave speed ``q/h_g + sqrt(g h_g)``; free outflow the
    critical outflow speed ``q/h + sqrt(g h)`` of the interior face; prescribed
    outflow adds nothing.  Each group's contribution is accumulated per face in
    edge order and the groups are added in order, exactly as a dense
    ``bincount`` sum would.
    """
    extra = None
    n_local = boundaries.faces.size
    for group in boundaries:
        if group.spec.role == "outflow":
            continue
        if group.free:
            q = _free_outflow_unit_discharge(group, state[:, 0], gravity, dry_depth_m)
            positive = q > 0.0
            if not positive.any():
                continue
            h = state[group.faces, 0]
            speed = np.zeros_like(q)
            speed[positive] = q[positive] / h[positive] + np.sqrt(gravity * h[positive])
            contribution = np.bincount(group.local, weights=group.length * speed, minlength=n_local)
            extra = contribution if extra is None else extra + contribution
            continue
        q = _inflow_unit_discharge(group, state[:, 0], time_s, dry_depth_m)
        positive = q > 0.0
        if not positive.any():
            continue
        ghost = _inflow_ghost_depth(q, state[group.faces, 0], gravity)
        speed = np.zeros_like(q)
        speed[positive] = q[positive] / ghost[positive] + np.sqrt(gravity * ghost[positive])
        contribution = np.bincount(group.local, weights=group.length * speed, minlength=n_local)
        extra = contribution if extra is None else extra + contribution
    return extra


def _boundary_wave_cfl(boundaries: _BoundarySet, wall_cfl: float, wave_sums: np.ndarray, extra: np.ndarray | None,
                       area: np.ndarray, cfl: float) -> float:
    """``cfl * min(area / (wave_sums + extra))`` given the wall-only bound ``wall_cfl``.

    Only boundary faces change; a face's bound can only shrink when ``extra``
    is added, so the minimum over all faces is ``min(wall_cfl, boundary minimum)``
    in binary64 (min and the positive scaling by ``cfl`` are exact and monotone).
    """
    if extra is None:
        return wall_cfl
    faces = boundaries.faces
    sums = wave_sums[faces] + extra
    positive = sums > 0.0
    if not positive.any():
        return wall_cfl
    return min(wall_cfl, cfl * float(np.min(area[faces][positive] / sums[positive])))


def _boundary_stage(boundaries: _BoundarySet, state: np.ndarray, time_s: float, dt_s: float, area: np.ndarray,
                    *, gravity: float, dry_depth_m: float, outflow_fraction: float,
                    dense: np.ndarray | None = None) -> _BoundaryStage:
    """Additive face derivative of all open boundary groups for one RK stage.

    Open edges keep the closed-wall Riemann flux (hydrostatic balance and
    normal-momentum reflection).  On top of it:

    * inflow adds mass flux q_e (into the face) and momentum flux
      (q_e^2 / h_g) along the inward normal, h_g = max(h_face, (q_e^2/g)^(1/3));
    * outflow removes mass q_e and momentum q_e * u_face, with q_e capped so a
      stage of length dt removes at most ``outflow_fraction`` of the water above
      the dry threshold in each adjacent face;
    * free outflow does the same with q_e = C_d sqrt(g) H^(3/2) evaluated on the
      stage state instead of a prescribed Q (its "requested" discharge is the
      uncapped weir discharge).

    The terms are computed on ``boundaries.faces`` and written into ``dense``
    (an ``(n_face, 3)`` buffer that is zero on every other row; allocated when
    not given).
    """
    stage = _BoundaryStage(None, {}, {}, {})
    if not len(boundaries):
        return stage
    n_local = boundaries.faces.size
    h = state[:, 0]
    d0 = np.zeros(n_local)
    d1 = np.zeros(n_local)
    d2 = np.zeros(n_local)
    for group in boundaries:
        name = group.spec.name
        local = group.local
        if group.spec.role == "inflow":
            q = _inflow_unit_discharge(group, h, time_s, dry_depth_m)
            ghost = _inflow_ghost_depth(q, h[group.faces], gravity)
            with np.errstate(divide="ignore", invalid="ignore"):
                momentum = np.where(q > 0.0, q * q / ghost, 0.0)
            d0 += np.bincount(local, weights=group.factor * q, minlength=n_local)
            d1 -= np.bincount(local, weights=group.factor * momentum * group.nx, minlength=n_local)
            d2 -= np.bincount(local, weights=group.factor * momentum * group.ny, minlength=n_local)
            stage.inflow_m3_s[name] = math.fsum((q * group.length).tolist())
        else:
            q = None  # unit discharge per edge before the stage cap, or None for no withdrawal
            if group.free:
                weir = _free_outflow_unit_discharge(group, h, gravity, dry_depth_m)
                if group.has_reference:
                    stage.reference_outflow_m3_s[name] = group.discharge_at(time_s)
                if weir.any():
                    q = weir
                    stage.outflow_requested_m3_s[name] = math.fsum((weir * group.length).tolist())
                else:
                    stage.outflow_requested_m3_s[name] = 0.0
            else:
                requested = group.discharge_at(time_s)
                stage.outflow_requested_m3_s[name] = requested
                if requested > 0.0:
                    weights = _edge_weights(group, h, dry_depth_m, wet_only=True)
                    total = math.fsum(weights.tolist())
                    if total > 0.0:
                        q = requested * (weights / total) / group.length
            if q is None:
                # No withdrawal: the face terms below would subtract exact +0.0, so skip them.
                stage.outflow_delivered_m3_s[name] = 0.0
                continue
            wanted = np.bincount(local, weights=q * group.length * dt_s, minlength=n_local)[local]
            available = outflow_fraction * np.maximum(h[group.faces] - dry_depth_m, 0.0) * area[group.faces]
            with np.errstate(divide="ignore", invalid="ignore"):
                scale = np.where(wanted > available, available / wanted, 1.0)
            delivered_q = q * np.where(wanted > 0.0, scale, 0.0)
            hf = h[group.faces]
            wet = hf > dry_depth_m
            u = np.zeros_like(hf)
            v = np.zeros_like(hf)
            np.divide(state[group.faces, 1], hf, out=u, where=wet)
            np.divide(state[group.faces, 2], hf, out=v, where=wet)
            d0 -= np.bincount(local, weights=group.factor * delivered_q, minlength=n_local)
            d1 -= np.bincount(local, weights=group.factor * delivered_q * u, minlength=n_local)
            d2 -= np.bincount(local, weights=group.factor * delivered_q * v, minlength=n_local)
            stage.outflow_delivered_m3_s[name] = math.fsum((delivered_q * group.length).tolist())
    if dense is None:
        dense = np.zeros((state.shape[0], 3), dtype=np.float64)
    faces = boundaries.faces
    dense[faces, 0] = d0
    dense[faces, 1] = d1
    dense[faces, 2] = d2
    stage.derivative = dense
    return stage


# --------------------------------------------------------------------------
# Ledger helpers
# --------------------------------------------------------------------------
class _Compensated:
    """Neumaier-compensated running sum for million-step ledgers."""

    __slots__ = ("total", "correction")

    def __init__(self, total: float = 0.0, correction: float = 0.0) -> None:
        self.total = float(total)
        self.correction = float(correction)

    def add(self, value: float) -> None:
        updated = self.total + value
        if abs(self.total) >= abs(value):
            self.correction += (self.total - updated) + value
        else:
            self.correction += (value - updated) + self.total
        self.total = updated

    @property
    def value(self) -> float:
        return self.total + self.correction

    def state(self) -> list[float]:
        return [self.total, self.correction]


_LEDGER_KEYS = ("rainfall", "inflow", "outflow_requested", "outflow_delivered", "correction")


@dataclass
class TriangularLongRunSummary:
    status: str
    final_time_s: float
    water_depth_m: np.ndarray
    unit_discharge_x_m2_s: np.ndarray
    unit_discharge_y_m2_s: np.ndarray
    maximum_depth_m: np.ndarray
    time_of_maximum_depth_s: np.ndarray
    maximum_speed_m_s: np.ndarray
    ledger: list[dict[str, Any]]
    accepted_step_count: int
    rejected_step_count: int
    rejected_step_reasons: dict[str, int]
    mass_correction_count: int
    minimum_accepted_step_s: float
    maximum_accepted_step_s: float
    maximum_internal_edge_mass_pair_residual_m3_s: float
    maximum_manning_gamma_per_s: float
    volume_correction_reference_scale_m3: float
    volume_correction_budget_m3: float
    kernel: str
    wall_clock_s: float
    checkpoints_written: list[str] = field(default_factory=list)
    resumed_from: str | None = None


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def _inputs_fingerprint(inputs: TriangularLongRunInputs, geometry: EdgeGeometry) -> str:
    digest = hashlib.sha256()
    for array in (geometry.area, geometry.left, geometry.right, geometry.length, inputs.bed_m,
                  inputs.initial_depth_m, inputs.output_times_s):
        digest.update(np.ascontiguousarray(array).tobytes())
    digest.update(b"n" if inputs.manning_n is None else inputs.manning_n.tobytes())
    digest.update(b"r" if inputs.rainfall_rate_m_s_by_interval is None else inputs.rainfall_rate_m_s_by_interval.tobytes())
    for group in inputs.open_boundaries:
        # Unset (null) fields are omitted, so prescribed groups hash exactly as
        # before free_outflow existed (older checkpoints stay resumable); free
        # groups always carry their resolved discharge coefficient.
        data = group.model_dump(mode="json", exclude_none=True)
        if group.role == "free_outflow":
            data["discharge_coefficient"] = group.effective_discharge_coefficient
            data.setdefault("crest_elevation_m", None)
        digest.update(json.dumps(data, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def _fail(message: str, *, time_s: float, stage: str, reason: str, counters: Mapping[str, Any],
          last_reason: str | None = None, step_kind: str | None = None) -> TriangularSolverExecutionError:
    return TriangularSolverExecutionError(
        message, time_s=time_s, stage=stage, reason=reason, last_reason=last_reason, step_kind=step_kind,
        accepted_step_count=counters["accepted"], rejected_step_count=sum(counters["rejected"].values()),
        rejected_step_reasons=counters["rejected"],
    )


def _write_checkpoint(directory: Path, payload: Mapping[str, Any], arrays: Mapping[str, np.ndarray], output_index: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"checkpoint_{output_index:08d}.npz"
    temporary = directory / f".checkpoint_{output_index:08d}.npz.tmp"
    with open(temporary, "wb") as handle:
        np.savez(handle, metadata=np.frombuffer(json.dumps(payload, sort_keys=True).encode("utf-8"), dtype=np.uint8), **arrays)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, final)
    return final


def load_long_run_checkpoint(path: str | Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Read a checkpoint written by :func:`run_triangular_long_run`."""
    try:
        with np.load(Path(path), allow_pickle=False) as data:
            metadata = json.loads(bytes(data["metadata"]).decode("utf-8"))
            arrays = {name: np.array(data[name]) for name in data.files if name != "metadata"}
    except (OSError, KeyError, ValueError) as exc:
        raise TriangularLongRunError(f"cannot read long-run checkpoint {path}: {exc}") from exc
    if metadata.get("format") != CHECKPOINT_FORMAT:
        raise TriangularLongRunError(f"{path} is not a {CHECKPOINT_FORMAT} file")
    return metadata, arrays


def run_triangular_long_run(
    inputs: TriangularLongRunInputs,
    options: TriangularLongRunOptions | Mapping[str, Any] | None = None,
    *,
    frame_sink: FrameSink | None = None,
    checkpoint_directory: str | Path | None = None,
    resume_from: str | Path | None = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    edges: Sequence[Any] | None = None,
) -> TriangularLongRunSummary:
    """Advance one case through all ``output_times_s`` and stream every frame.

    ``frame_sink(frame)`` is called once per saved output time (including t=0
    unless resuming); frames are not retained by the driver.  If
    ``options.checkpoint_interval_s`` is set, a checkpoint is written to
    ``checkpoint_directory`` at the first output time at or after each
    interval; ``resume_from`` continues exactly from such a checkpoint.
    """
    try:
        options = options if isinstance(options, TriangularLongRunOptions) else TriangularLongRunOptions.model_validate(dict(options or {}))
    except ValidationError as exc:
        raise TriangularLongRunError(f"invalid long-run options: {exc}") from exc
    mesh = inputs.mesh
    friction = options.manning_friction
    if friction == "none" and inputs.manning_n is not None:
        raise TriangularLongRunError("manning_n was supplied but manning_friction is 'none'")
    if friction != "none" and inputs.manning_n is None:
        raise TriangularLongRunError(f"manning_friction {friction!r} requires manning_n")
    if options.checkpoint_interval_s is not None and checkpoint_directory is None:
        raise TriangularLongRunError("checkpoint_interval_s requires checkpoint_directory")

    wall_start = time.perf_counter()
    edge_set = edges if edges is not None else _edges(mesh)
    geometry: EdgeGeometry = edge_set.geometry  # type: ignore[attr-defined]
    rhs = select_rhs_kernel(options.kernel)
    kernel_name = "numba" if rhs.__name__ == "rhs_numba" else "numpy"
    use_numba = kernel_name == "numba"
    # The numba kernels keep every per-face array in a cache-friendly storage
    # order (spatial Morton order of the face centroids; see NumbaWorkspace).
    # Inputs are converted once, outputs are converted back on every frame,
    # checkpoint and summary; the arithmetic and its order are unchanged.
    workspace = NumbaWorkspace(geometry, face_order=spatial_face_order(mesh.face_centroid)) if use_numba else None
    to_internal = workspace.to_internal if use_numba else (lambda values: values)
    to_mesh = workspace.to_mesh if use_numba else (lambda values: values)
    groups = _prepare_boundary_groups(inputs, geometry, face_map=workspace.face_inverse if use_numba else None)
    boundary_buffer = np.zeros((mesh.n_face, 3), dtype=np.float64) if len(groups) else None
    fingerprint = _inputs_fingerprint(inputs, geometry)
    numerics = options.numerics_fingerprint()

    g = float(options.gravity_m_s2)
    cfl = float(options.cfl_number)
    dry = float(options.dry_depth_threshold_m)
    negative_tolerance = float(options.negative_depth_tolerance_m)
    min_dt = float(options.minimum_time_step_s)
    max_dt = float(options.maximum_time_step_s)
    area = geometry.area
    total_area = math.fsum(area.tolist())
    bed = np.ascontiguousarray(inputs.bed_m)
    area_k = to_internal(area)                                       # per-face arrays in storage order
    bed_k = to_internal(bed)
    manning_n = None if inputs.manning_n is None else to_internal(inputs.manning_n)
    rain_rates = inputs.rainfall_rate_m_s_by_interval
    times = [float(value) for value in inputs.output_times_s]
    explicit = friction == "explicit_source_cfl"
    implicit = friction == "point_implicit"
    source_fraction = float(options.source_cfl_fraction) if explicit else None
    minimum_step_tolerance = _step_tolerance_s(min_dt)

    initial_state = np.zeros((mesh.n_face, 3), dtype=np.float64)
    initial_state[:, 0] = inputs.initial_depth_m
    initial = _sanitize_stage(initial_state, mesh, dry_depth_m=dry, negative_tolerance_m=negative_tolerance)
    if initial.reason:
        raise TriangularLongRunError(initial.detail or "initial state is invalid")
    initial_volume = math.fsum((initial.state[:, 0] * area).tolist())
    reference_depth = float(initial.state[:, 0].max(initial=0.0))
    base_scale = max(initial_volume, total_area * reference_depth, 1.0)

    counters: dict[str, Any] = {"accepted": 0, "rejected": {}, "corrections": 0}
    ledger_rows: list[dict[str, Any]] = []
    totals = {key: _Compensated() for key in _LEDGER_KEYS}
    group_keys = {group.spec.name: ("inflow", "outflow_requested", "outflow_delivered") + (("reference_outflow",) if group.has_reference else ())
                  for group in groups}
    group_totals = {name: {key: _Compensated() for key in keys} for name, keys in group_keys.items()}
    state = to_internal(initial.state)
    max_depth = state[:, 0].copy()
    time_of_max = np.zeros(mesh.n_face)
    max_speed = np.zeros(mesh.n_face)
    extrema = {"min_dt": math.inf, "max_dt": -math.inf, "pair": 0.0, "gamma": 0.0}
    start_index = 0
    last_checkpoint_time = 0.0
    checkpoints: list[str] = []

    if resume_from is not None:
        metadata, arrays = load_long_run_checkpoint(resume_from)
        if metadata["inputs_fingerprint"] != fingerprint or metadata["numerics_fingerprint"] != numerics:
            raise TriangularLongRunError("checkpoint does not match these inputs and numerical options")
        state = to_internal(arrays["state"].copy())
        max_depth = to_internal(arrays["maximum_depth_m"].copy())
        time_of_max = to_internal(arrays["time_of_maximum_depth_s"].copy())
        max_speed = to_internal(arrays["maximum_speed_m_s"].copy())
        start_index = int(metadata["output_index"])
        counters = {"accepted": int(metadata["accepted"]), "rejected": dict(metadata["rejected"]), "corrections": int(metadata["corrections"])}
        totals = {key: _Compensated(*metadata["totals"][key]) for key in _LEDGER_KEYS}
        group_totals = {name: {key: _Compensated(*value) for key, value in items.items()} for name, items in metadata["group_totals"].items()}
        extrema = dict(metadata["extrema"])
        ledger_rows = list(metadata["ledger"])
        last_checkpoint_time = times[start_index]

    def correction_scale() -> float:
        if options.volume_correction_reference == "initial_state":
            return base_scale
        return max(initial_volume + totals["rainfall"].value + totals["inflow"].value, total_area * reference_depth, 1.0)

    def velocities(current: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = current[:, 0]
        wet = h > dry
        u = np.zeros(mesh.n_face)
        v = np.zeros(mesh.n_face)
        np.divide(current[:, 1], h, out=u, where=wet)
        np.divide(current[:, 2], h, out=v, where=wet)
        return u, v

    def group_ledger(group: _BoundaryGroup, index: int) -> dict[str, float]:
        items = group_totals[group.spec.name]
        entry = {
            "cumulative_inflow_volume_m3": items["inflow"].value,
            "cumulative_outflow_requested_volume_m3": items["outflow_requested"].value,
            "cumulative_outflow_delivered_volume_m3": items["outflow_delivered"].value,
            "cumulative_outflow_shortfall_volume_m3": items["outflow_requested"].value - items["outflow_delivered"].value,
        }
        if group.free:
            # Instantaneous (uncapped) weir discharge of the saved state, m3/s.
            weir = _free_outflow_unit_discharge(group, state[:, 0], g, dry)
            entry["outflow_discharge_m3_s"] = math.fsum((weir * group.length).tolist())
            if group.has_reference:
                entry["reference_outflow_discharge_m3_s"] = group.discharge_at(times[index])
                entry["cumulative_reference_outflow_volume_m3"] = items["reference_outflow"].value
        return entry

    def ledger_row(index: int, interval: Mapping[str, float], wall_s: float, interval_steps: int, interval_rejected: int,
                   interval_dt: tuple[float, float]) -> dict[str, Any]:
        stored = math.fsum((state[:, 0] * area_k).tolist())
        cumulative = {key: totals[key].value for key in _LEDGER_KEYS}
        residual = (stored - initial_volume - cumulative["rainfall"] - cumulative["inflow"]
                    + cumulative["outflow_delivered"] - cumulative["correction"])
        return {
            "output_index": index,
            "time_s": times[index],
            "stored_volume_m3": stored,
            "initial_volume_m3": initial_volume,
            "interval_rainfall_volume_m3": interval.get("rainfall", 0.0),
            "interval_inflow_volume_m3": interval.get("inflow", 0.0),
            "interval_outflow_requested_volume_m3": interval.get("outflow_requested", 0.0),
            "interval_outflow_delivered_volume_m3": interval.get("outflow_delivered", 0.0),
            "interval_outflow_shortfall_volume_m3": interval.get("outflow_requested", 0.0) - interval.get("outflow_delivered", 0.0),
            "interval_correction_volume_m3": interval.get("correction", 0.0),
            "cumulative_rainfall_volume_m3": cumulative["rainfall"],
            "cumulative_inflow_volume_m3": cumulative["inflow"],
            "cumulative_outflow_requested_volume_m3": cumulative["outflow_requested"],
            "cumulative_outflow_delivered_volume_m3": cumulative["outflow_delivered"],
            "cumulative_outflow_shortfall_volume_m3": cumulative["outflow_requested"] - cumulative["outflow_delivered"],
            "cumulative_correction_volume_m3": cumulative["correction"],
            "residual_m3": residual,
            "groups": {group.spec.name: group_ledger(group, index) for group in groups},
            "volume_correction_reference_scale_m3": correction_scale(),
            "minimum_water_depth_m": float(state[:, 0].min()),
            "maximum_water_depth_m": float(state[:, 0].max()),
            "interval_accepted_step_count": interval_steps,
            "interval_rejected_step_count": interval_rejected,
            "interval_minimum_step_s": None if interval_steps == 0 else interval_dt[0],
            "interval_maximum_step_s": None if interval_steps == 0 else interval_dt[1],
            "interval_wall_clock_s": wall_s,
        }

    def emit(index: int, row: Mapping[str, Any]) -> None:
        if frame_sink is None:
            return
        u, v = velocities(state)
        depth = to_mesh(state[:, 0])
        frame_sink(TriangularFrame(
            output_index=index, time_s=times[index], water_depth_m=_readonly(depth),
            unit_discharge_x_m2_s=_readonly(to_mesh(state[:, 1])), unit_discharge_y_m2_s=_readonly(to_mesh(state[:, 2])),
            velocity_x_m_s=_readonly(to_mesh(u)), velocity_y_m_s=_readonly(to_mesh(v)), water_level_m=_readonly(bed + depth),
            ledger=dict(row),
        ))

    if resume_from is None:
        row = ledger_row(0, {}, 0.0, 0, 0, (math.inf, -math.inf))
        ledger_rows.append(row)
        emit(0, row)

    # numba path: all per-step arrays are preallocated.  Two derivative buffers
    # (stage-0 flux is cached across retries while stage-1 flux is recomputed)
    # and three state buffers that rotate with the accepted state.
    flux_buffers = [np.empty((mesh.n_face, 3)) for _ in range(2)] if use_numba else [None, None]
    stage_buffers = [np.empty((mesh.n_face, 3)) for _ in range(3)] if use_numba else [None, None, None]

    def evaluate(current: np.ndarray, stage_time: float, slot: int) -> tuple[np.ndarray, float, float, np.ndarray | None, float, Mapping[str, Any] | None, str | None]:
        """Spatial residual, wave CFL, pair residual, explicit Manning source/limit."""
        if use_numba:
            result = rhs(current, bed_k, geometry, gravity=g, cfl=cfl, dry_depth_m=dry, workspace=workspace, out=flux_buffers[slot])
        else:
            result = rhs(current, bed_k, geometry, gravity=g, cfl=cfl, dry_depth_m=dry)
        wave_cfl = result.cfl_limit_s
        if len(groups):
            extra = _boundary_wave_sums(groups, current, stage_time, gravity=g, dry_depth_m=dry)
            wave_cfl = _boundary_wave_cfl(groups, wave_cfl, result.wave_sums, extra, area_k, cfl)
        if explicit:
            source = manning_explicit(current, manning_n, gravity=g, source_cfl_fraction=source_fraction, dry_depth_m=dry)
            return result.derivative, wave_cfl, result.maximum_pair_residual_m3_s, source.source, source.source_limit_s, source.diagnostics, source.error
        return result.derivative, wave_cfl, result.maximum_pair_residual_m3_s, None, math.inf, None, None

    def sanitized(out: np.ndarray | None, code: int, index: int, correction_volume: float, count: int) -> _Sanitized:
        if code == 1:
            return _Sanitized(None, 0.0, 0, "non_finite_state", "stage contains NaN or infinity")
        if code == 2:
            return _Sanitized(None, 0.0, 0, "negative_depth",
                              f"face_id {mesh.face_id[index]} fell below -negative_depth_tolerance_m")
        return _Sanitized(out, correction_volume, count)

    def euler(current: np.ndarray, flux: np.ndarray, source: np.ndarray | None, boundary: np.ndarray | None,
              rain_m_s: float | None, dt: float, slot: int) -> tuple[_Sanitized, float]:
        """Euler stage + sanitation (+ point-implicit Manning); returns the stage and max gamma."""
        if use_numba:
            out = stage_buffers[slot]
            code, index, correction_volume, count, gamma = stage_sanitize_numba(
                current, out=out, workspace=workspace, area=area_k, dry_depth_m=dry, negative_tolerance_m=negative_tolerance,
                dt_s=dt, flux=flux, source=source, boundary=boundary, rain_m_s=rain_m_s,
                manning_n=manning_n if implicit else None, gravity=g,
            )
            return sanitized(out if code == 0 else None, code, index, correction_volume, count), gamma
        stage = sanitized(*stage_and_sanitize(
            current, dt_s=dt, flux=flux, source=source, boundary=boundary, rain_m_s=rain_m_s, area=area_k,
            dry_depth_m=dry, negative_tolerance_m=negative_tolerance, use_numba=False,
        ))
        gamma = 0.0
        if implicit and stage.state is not None:
            _, gamma = manning_point_implicit(stage.state, manning_n, dt, gravity=g, dry_depth_m=dry, use_numba=False)
        return stage, gamma

    def average(first: np.ndarray, second: np.ndarray) -> _Sanitized:
        if use_numba:
            out = stage_buffers[2]
            code, index, correction_volume, count, _ = stage_sanitize_numba(
                first, out=out, workspace=workspace, area=area_k, dry_depth_m=dry, negative_tolerance_m=negative_tolerance,
                second=second,
            )
            return sanitized(out if code == 0 else None, code, index, correction_volume, count)
        return sanitized(*stage_and_sanitize(
            first, second=second, area=area_k, dry_depth_m=dry, negative_tolerance_m=negative_tolerance, use_numba=False,
        ))

    outflow_fraction = float(options.outflow_stage_volume_fraction)
    step_fraction = float(options.step_cfl_fraction)
    time_s = times[start_index]
    progress_wall = time.perf_counter()
    for interval_index in range(start_index, len(times) - 1):
        end_time = times[interval_index + 1]
        rain = None if rain_rates is None else float(rain_rates[interval_index])
        interval_start = time_s
        interval_duration = math.fsum((end_time, -interval_start))
        elapsed = _Compensated()
        interval = {key: _Compensated() for key in _LEDGER_KEYS}
        interval_steps = 0
        interval_rejected_start = sum(counters["rejected"].values())
        interval_dt = [math.inf, -math.inf]
        interval_wall = time.perf_counter()
        cached: tuple[Any, ...] | None = None
        while True:
            remaining = math.fsum((interval_duration, -elapsed.total, -elapsed.correction))
            if remaining <= 0.0:
                if remaining >= -_step_tolerance_s(interval_duration):
                    time_s = end_time
                    break
                raise _fail(f"accepted step durations overshot output interval by {-remaining:.17g} s", time_s=time_s,
                            stage="time_accumulation", reason="time_overshoot", counters=counters)
            if cached is None:
                cached = evaluate(state, time_s, 0)
            flux_0, wave_cfl_0, pair_0, source_0, source_cfl_0, _, source_error_0 = cached
            if source_error_0:
                raise _fail(f"Manning source limit evaluation failed: {source_error_0}", time_s=time_s, stage="step_selection",
                            reason=source_error_0, counters=counters, step_kind="manning_source_cfl")
            extrema["pair"] = max(extrema["pair"], pair_0)
            cfl_now = min(wave_cfl_0, source_cfl_0)
            if step_fraction != 1.0 and math.isfinite(cfl_now):
                cfl_now *= step_fraction
            alignment_only = remaining < min_dt - minimum_step_tolerance
            if alignment_only:
                bound = cfl_now if math.isfinite(cfl_now) else max_dt
                if remaining > bound + _step_tolerance_s(max(remaining, bound)):
                    raise _fail(f"output-alignment remainder {remaining:.17g} s exceeds current CFL bound {bound:.17g} s",
                                time_s=time_s, stage="output_alignment", reason="alignment_cfl", counters=counters,
                                step_kind="output_alignment")
                dt = remaining
            else:
                dt = min(remaining, max_dt, cfl_now)
                if not math.isfinite(dt):
                    dt = min(remaining, max_dt)
                if dt + minimum_step_tolerance < min_dt:
                    source_limited = explicit and source_cfl_0 <= wave_cfl_0
                    raise _fail(
                        f"no legal time step remains: {'Manning source' if source_limited else 'CFL'} bound {dt:.17g} s is below minimum_time_step_s",
                        time_s=time_s, stage="step_selection",
                        reason="source_cfl_below_minimum_time_step" if source_limited else "minimum_time_step",
                        counters=counters, last_reason="source_cfl" if source_limited else "wave_cfl", step_kind="cfl_step")
            retry_count = 0
            while True:
                budget_remaining = max(0.0, options.volume_correction_budget_fraction * correction_scale() - totals["correction"].value)
                reason, detail = None, None
                boundary_0 = _boundary_stage(groups, state, time_s, dt, area_k, gravity=g, dry_depth_m=dry,
                                             outflow_fraction=outflow_fraction, dense=boundary_buffer)
                # The point-implicit Manning update is fused into the stage; its
                # result is discarded with the stage if the stage is rejected.
                stage_1, gamma_seen = euler(state, flux_0, source_0, boundary_0.derivative, rain, dt, 0)
                cfl_after = math.inf
                if stage_1.reason:
                    reason, detail = stage_1.reason, f"stage 1: {stage_1.detail}"
                elif 0.5 * stage_1.correction_volume_m3 > budget_remaining:
                    reason, detail = "correction_budget", "stage-1 weighted correction exceeds the remaining volume-correction budget"
                else:
                    stage_1_state = stage_1.state
                    evaluated_1 = evaluate(stage_1_state, time_s + dt, 1)
                    flux_1, wave_cfl_1, pair_1, source_1, source_cfl_1, source_diag_1, source_error_1 = evaluated_1
                    cfl_after = min(wave_cfl_1, source_cfl_1)
                    if source_error_1:
                        raise _fail(f"stage 2 Manning source evaluation failed: {source_error_1}", time_s=time_s, stage="stage2",
                                    reason=source_error_1, counters=counters)
                    wave_violated = math.isfinite(wave_cfl_1) and dt > wave_cfl_1 + _step_tolerance_s(max(dt, wave_cfl_1))
                    source_violated = explicit and _source_cfl_bound_exceeded(dt, source_cfl_1)
                    if source_violated and (not wave_violated or source_cfl_1 <= wave_cfl_1):
                        reason, detail = "second_stage_source_cfl", "candidate step exceeds freshly recomputed stage-1 Manning source CFL bound"
                    elif wave_violated:
                        reason, detail = "second_stage_cfl", "candidate step exceeds freshly recomputed stage-1 wave CFL bound"
                    else:
                        boundary_1 = _boundary_stage(groups, stage_1_state, time_s + dt, dt, area_k, gravity=g, dry_depth_m=dry,
                                                     outflow_fraction=outflow_fraction, dense=boundary_buffer)
                        stage_2, gamma_2 = euler(stage_1_state, flux_1, source_1, boundary_1.derivative, rain, dt, 1)
                        if stage_2.reason:
                            reason, detail = stage_2.reason, f"stage 2: {stage_2.detail}"
                        else:
                            correction = 0.5 * (stage_1.correction_volume_m3 + stage_2.correction_volume_m3)
                            stage_2_state = stage_2.state
                            gamma_seen = max(gamma_seen, gamma_2)
                            if correction > budget_remaining:
                                reason, detail = "correction_budget", "SSPRK2 weighted correction exceeds the remaining volume-correction budget"
                            else:
                                final = average(state, stage_2_state)
                                if final.reason:
                                    reason, detail = final.reason, f"final state: {final.detail}"
                                else:
                                    correction += final.correction_volume_m3
                                    if correction > budget_remaining:
                                        reason, detail = "correction_budget", "final-state correction exceeds the remaining volume-correction budget"
                if reason is None:
                    if alignment_only and math.isfinite(cfl_after) and dt > cfl_after + _step_tolerance_s(max(dt, cfl_after)):
                        reason, detail = "alignment_cfl", "alignment step exceeds the freshly recomputed CFL limit"
                if reason is None:
                    break
                counters["rejected"][reason] = counters["rejected"].get(reason, 0) + 1
                if reason == "non_finite_state":
                    raise _fail(detail or "a non-finite stage state was found", time_s=time_s, stage="step", reason=reason, counters=counters)
                retry_count += 1
                if alignment_only:
                    raise _fail(detail or f"output-alignment attempt failed: {reason}", time_s=time_s, stage="output_alignment",
                                reason=reason, counters=counters, step_kind="output_alignment")
                if retry_count > options.maximum_step_retries:
                    raise _fail(detail or f"step retry limit exhausted after {retry_count - 1} retries", time_s=time_s, stage="step",
                                reason="retry_exhausted", counters=counters, last_reason=reason, step_kind="cfl_step")
                if reason in {"second_stage_cfl", "second_stage_source_cfl"} and math.isfinite(cfl_after):
                    next_dt = min(dt * 0.5, cfl_after)
                else:
                    next_dt = dt * 0.5
                if next_dt + minimum_step_tolerance < min_dt:
                    raise _fail(f"retry candidate {next_dt:.17g} s is below minimum_time_step_s", time_s=time_s, stage="step",
                                reason="source_cfl_below_minimum_time_step" if reason == "second_stage_source_cfl" else "minimum_time_step",
                                counters=counters, last_reason=reason, step_kind="cfl_step")
                dt = next_dt

            # Accept the step.
            reaches_output = abs(dt - remaining) <= _step_tolerance_s(max(dt, remaining))
            elapsed.add(dt)
            time_s = end_time if reaches_output else math.fsum((interval_start, elapsed.total, elapsed.correction))
            previous_state = state
            state = final.state
            if use_numba:
                stage_buffers[2] = previous_state  # the old state buffer becomes the next final-state buffer
            cached = None
            extrema["pair"] = max(extrema["pair"], pair_1)
            extrema["gamma"] = max(extrema["gamma"], gamma_seen)
            if explicit and source_diag_1 is not None:
                extrema["gamma"] = max(extrema["gamma"], float(source_diag_1["maximum_manning_gamma_per_s"]))
            increments = {
                "rainfall": 0.0 if rain is None else 0.5 * (rain + rain) * dt * total_area,
                "inflow": 0.5 * dt * (sum(boundary_0.inflow_m3_s.values()) + sum(boundary_1.inflow_m3_s.values())),
                "outflow_requested": 0.5 * dt * (sum(boundary_0.outflow_requested_m3_s.values()) + sum(boundary_1.outflow_requested_m3_s.values())),
                "outflow_delivered": 0.5 * dt * (sum(boundary_0.outflow_delivered_m3_s.values()) + sum(boundary_1.outflow_delivered_m3_s.values())),
                "correction": correction,
            }
            for key, value in increments.items():
                if value:
                    totals[key].add(value)
                    interval[key].add(value)
            for group in groups:
                name = group.spec.name
                for key, attribute in (("inflow", "inflow_m3_s"), ("outflow_requested", "outflow_requested_m3_s"),
                                       ("outflow_delivered", "outflow_delivered_m3_s"), ("reference_outflow", "reference_outflow_m3_s")):
                    if key not in group_totals[name]:
                        continue
                    value = 0.5 * dt * (getattr(boundary_0, attribute).get(name, 0.0) + getattr(boundary_1, attribute).get(name, 0.0))
                    if value:
                        group_totals[name][key].add(value)
            counters["accepted"] += 1
            counters["corrections"] += stage_1.correction_count + stage_2.correction_count + final.correction_count
            interval_steps += 1
            interval_dt[0] = min(interval_dt[0], dt)
            interval_dt[1] = max(interval_dt[1], dt)
            extrema["min_dt"] = min(extrema["min_dt"], dt)
            extrema["max_dt"] = max(extrema["max_dt"], dt)
            update_face_maxima(state, time_s, dry, max_depth, time_of_max, max_speed, use_numba=use_numba, workspace=workspace)
            if counters["accepted"] > options.maximum_accepted_step_count:
                raise _fail("accepted-step safety limit exceeded", time_s=time_s, stage="integration",
                            reason="step_safety_limit", counters=counters)
            if progress_callback is not None and counters["accepted"] % options.progress_interval_steps == 0:
                now = time.perf_counter()
                progress_callback({
                    "time_s": time_s, "dt_s": dt, "accepted_step_count": counters["accepted"],
                    "rejected_step_count": sum(counters["rejected"].values()), "wall_clock_s": now - wall_start,
                    "wall_clock_since_last_progress_s": now - progress_wall, "maximum_depth_m": float(state[:, 0].max()),
                    "kernel": kernel_name,
                })
                progress_wall = now
            if time_s == end_time:
                break

        output_index = interval_index + 1
        interval_values = {key: value.value for key, value in interval.items()}
        row = ledger_row(output_index, interval_values, time.perf_counter() - interval_wall, interval_steps,
                         sum(counters["rejected"].values()) - interval_rejected_start, (interval_dt[0], interval_dt[1]))
        ledger_rows.append(row)
        emit(output_index, row)
        due = (options.checkpoint_interval_s is not None
               and (times[output_index] - last_checkpoint_time >= options.checkpoint_interval_s - _step_tolerance_s(times[output_index])
                    or output_index == len(times) - 1))
        if due:
            payload = {
                "format": CHECKPOINT_FORMAT, "solver_version": LONG_RUN_SOLVER_VERSION,
                "inputs_fingerprint": fingerprint, "numerics_fingerprint": numerics,
                "output_index": output_index, "time_s": times[output_index],
                "accepted": counters["accepted"], "rejected": counters["rejected"], "corrections": counters["corrections"],
                "totals": {key: value.state() for key, value in totals.items()},
                "group_totals": {name: {key: value.state() for key, value in items.items()} for name, items in group_totals.items()},
                "extrema": extrema, "ledger": ledger_rows,
            }
            written = _write_checkpoint(Path(checkpoint_directory), payload, {
                "state": to_mesh(state), "maximum_depth_m": to_mesh(max_depth),
                "time_of_maximum_depth_s": to_mesh(time_of_max), "maximum_speed_m_s": to_mesh(max_speed),
            }, output_index)
            checkpoints.append(str(written))
            last_checkpoint_time = times[output_index]
            existing = sorted(Path(checkpoint_directory).glob("checkpoint_*.npz"))
            for stale in existing[:-options.checkpoint_keep_count]:
                stale.unlink()

    if counters["accepted"] == 0 and len(times) - 1 > start_index:
        raise _fail("positive-duration run completed without an accepted step", time_s=time_s, stage="integration",
                    reason="no_accepted_steps", counters=counters)
    scale = correction_scale()
    return TriangularLongRunSummary(
        status="completed", final_time_s=time_s,
        water_depth_m=to_mesh(state[:, 0]).copy(), unit_discharge_x_m2_s=to_mesh(state[:, 1]).copy(),
        unit_discharge_y_m2_s=to_mesh(state[:, 2]).copy(),
        maximum_depth_m=to_mesh(max_depth), time_of_maximum_depth_s=to_mesh(time_of_max), maximum_speed_m_s=to_mesh(max_speed),
        ledger=ledger_rows, accepted_step_count=counters["accepted"], rejected_step_count=sum(counters["rejected"].values()),
        rejected_step_reasons=dict(counters["rejected"]), mass_correction_count=counters["corrections"],
        minimum_accepted_step_s=extrema["min_dt"], maximum_accepted_step_s=extrema["max_dt"],
        maximum_internal_edge_mass_pair_residual_m3_s=extrema["pair"], maximum_manning_gamma_per_s=extrema["gamma"],
        volume_correction_reference_scale_m3=scale,
        volume_correction_budget_m3=options.volume_correction_budget_fraction * scale,
        kernel=kernel_name, wall_clock_s=time.perf_counter() - wall_start, checkpoints_written=checkpoints,
        resumed_from=None if resume_from is None else str(resume_from),
    )


def long_run_options_json_schema() -> dict[str, Any]:
    """JSON Schema of the long-run options and open-boundary group contracts."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Triangular long-run options and open boundary groups",
        "$defs": {
            "TriangularLongRunOptions": TriangularLongRunOptions.model_json_schema(),
            "OpenBoundaryGroupSpec": OpenBoundaryGroupSpec.model_json_schema(),
        },
    }


__all__ = [
    "CHECKPOINT_FORMAT", "FREE_OUTFLOW_DEFAULT_DISCHARGE_COEFFICIENT", "FrameSink", "LONG_RUN_OPTIONS_FORMAT",
    "LONG_RUN_SOLVER_VERSION", "MemoryFrameSink",
    "OPEN_BOUNDARY_FORMAT", "OpenBoundaryGroupSpec", "TriangularFrame", "TriangularLongRunError",
    "TriangularLongRunInputs", "TriangularLongRunOptions", "TriangularLongRunSummary",
    "load_long_run_checkpoint", "long_run_options_json_schema", "numba_status",
    "open_boundary_edges_for_mesh_group", "run_triangular_long_run",
]
