"""P4-1 run, state and result contracts for an experimental triangular SWE route.

This module validates inputs and result-shaped data only. It deliberately does
not advance time, evaluate fluxes, or write simulation results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator
import yaml

from .config import Number, Text
from .triangular_business import (
    TriangularBusinessError,
    TriangularBusinessPackage,
    read_triangular_business_package,
    terrain_fingerprint,
    topology_fingerprint,
)
from .security_exclusion import SecurityExclusionEvidence, security_evidence_sha256
from .triangular_mesh import TriangularMesh, TriangularMeshError


class TriangularSolverContractError(ValueError):
    """Raised when a P4-0 run request or triangular result contract is invalid."""


class _StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, allow_inf_nan=False)


_STEP_ABSOLUTE_TOLERANCE_S = 1e-12


def _step_tolerance_s(value: float) -> float:
    """Shared step-sum tolerance: max(1e-12 s, eight binary64 ULPs)."""
    return max(_STEP_ABSOLUTE_TOLERANCE_S, 8.0 * math.ulp(float(value)))


def _source_cfl_bound_tolerance_s(bound_s: float) -> float:
    """Roundoff allowance for a positive physical source-CFL bound, with no clock floor."""
    bound = float(bound_s)
    if not math.isfinite(bound) or bound <= 0.0:
        return 0.0
    return min(8.0 * math.ulp(bound), 1e-12 * bound)


def _source_cfl_bound_exceeded(step_s: float, bound_s: float) -> bool:
    """Compare a step with a source-CFL bound without applying the clock-time floor."""
    bound = float(bound_s)
    if math.isinf(bound) and bound > 0.0:
        return False
    if not math.isfinite(bound) or bound <= 0.0:
        return True
    return float(step_s) > bound + _source_cfl_bound_tolerance_s(bound)


def _validate_realizable_step_statistics(
    *,
    time_s: list[float],
    minimum_time_step_s: float,
    maximum_time_step_s: float,
    accepted_step_count: int,
    accepted_cfl_step_count: int,
    minimum_cfl_step_s: float,
    maximum_cfl_step_s: float,
    minimum_accepted_step_s: float,
    maximum_accepted_step_s: float,
    subminimum_alignment_step_s: list[float],
    subminimum_alignment_cfl_limit_s: list[float],
) -> None:
    """Check global extrema plus per-output interval step-count feasibility in O(k)."""
    interval_durations = [math.fsum((right, -left)) for left, right in zip(time_s, time_s[1:])]
    interval_tolerances = [_step_tolerance_s(duration) for duration in interval_durations]
    if len(subminimum_alignment_step_s) != len(interval_durations) or len(subminimum_alignment_cfl_limit_s) != len(interval_durations):
        raise ValueError("alignment step and CFL-limit arrays must have one value per output interval")
    if any(duration + tolerance < minimum_time_step_s for duration, tolerance in zip(interval_durations, interval_tolerances)):
        raise ValueError("each result time_s interval must be at least minimum_time_step_s")
    if any(step < 0.0 or (step > 0.0 and step >= minimum_time_step_s) for step in subminimum_alignment_step_s):
        raise ValueError("subminimum alignment remainders must be 0 or strictly less than minimum_time_step_s")
    for index, (duration, tolerance, alignment, cfl_limit) in enumerate(zip(
        interval_durations, interval_tolerances, subminimum_alignment_step_s, subminimum_alignment_cfl_limit_s,
    )):
        if cfl_limit < 0.0:
            raise ValueError("subminimum alignment CFL limits must be non-negative")
        if alignment == 0.0 and cfl_limit != 0.0:
            raise ValueError("alignment CFL limit must be 0 when no subminimum alignment step is recorded")
        if alignment > 0.0 and alignment > cfl_limit + tolerance:
            raise ValueError("subminimum alignment step may not exceed its recorded CFL limit")
        if alignment > duration + tolerance:
            raise ValueError(f"output interval {index} has an invalid alignment remainder")

    if minimum_cfl_step_s + _step_tolerance_s(minimum_time_step_s) < minimum_time_step_s:
        raise ValueError("minimum_cfl_step_s may not fall below minimum_time_step_s")
    if maximum_cfl_step_s > maximum_time_step_s + _step_tolerance_s(maximum_time_step_s):
        raise ValueError("maximum_cfl_step_s may not exceed maximum_time_step_s")
    if minimum_cfl_step_s > maximum_cfl_step_s:
        raise ValueError("minimum_cfl_step_s must be less than or equal to maximum_cfl_step_s")
    alignment_count = sum(step > 0.0 for step in subminimum_alignment_step_s)
    if accepted_step_count != accepted_cfl_step_count + alignment_count:
        raise ValueError("accepted_step_count must equal accepted_cfl_step_count plus subminimum alignment steps")
    if accepted_cfl_step_count < len(interval_durations):
        raise ValueError("each requested output interval must contain at least one CFL-limited accepted step")

    interval_cfl_durations = [math.fsum((duration, -alignment)) for duration, alignment in zip(interval_durations, subminimum_alignment_step_s)]
    total_duration = math.fsum(interval_durations)
    total_cfl_duration = math.fsum(interval_cfl_durations)
    total_tolerance = _step_tolerance_s(total_duration)
    count = accepted_cfl_step_count
    if count == 1:
        if (abs(total_cfl_duration - minimum_cfl_step_s) > total_tolerance
                or abs(total_cfl_duration - maximum_cfl_step_s) > total_tolerance):
            raise ValueError("a single CFL step must equal both minimum and maximum CFL step and remaining duration")
    else:
        shortest_extrema_sum = math.fsum(((count - 1) * minimum_cfl_step_s, maximum_cfl_step_s))
        longest_extrema_sum = math.fsum((minimum_cfl_step_s, (count - 1) * maximum_cfl_step_s))
        if total_cfl_duration + total_tolerance < shortest_extrema_sum or total_cfl_duration - total_tolerance > longest_extrema_sum:
            raise ValueError("CFL step extrema/count cannot realize the remaining total duration")

    lower_counts: list[int] = []
    upper_counts: list[int] = []
    for duration, tolerance in zip(interval_cfl_durations, interval_tolerances):
        if duration <= 0.0:
            raise ValueError("each output interval must contain positive CFL-limited duration")
        lower = max(1, math.ceil(math.fsum((duration, -tolerance)) / maximum_cfl_step_s))
        upper = math.floor(math.fsum((duration, tolerance)) / minimum_cfl_step_s)
        if lower > upper:
            index = len(lower_counts)
            raise ValueError(f"output interval {index} cannot be covered by steps within configured minimum/maximum bounds")
        lower_counts.append(lower)
        upper_counts.append(upper)
    sum_lower = sum(lower_counts)
    sum_upper = sum(upper_counts)
    if not sum_lower <= count <= sum_upper:
        raise ValueError("accepted_cfl_step_count cannot cover every output interval within configured step bounds")

    if minimum_cfl_step_s != maximum_cfl_step_s:
        minimum_candidates: list[tuple[int, int]] = []
        maximum_candidates: list[tuple[int, int]] = []
        same_interval_extrema_possible = False
        for index, (duration, tolerance, lower, upper) in enumerate(zip(interval_cfl_durations, interval_tolerances, lower_counts, upper_counts)):
            reserve_min_lower = max(
                lower,
                1 + math.ceil(math.fsum((duration, -minimum_cfl_step_s, -tolerance)) / maximum_cfl_step_s),
            )
            if reserve_min_lower <= upper:
                total_lower = sum_lower - lower + reserve_min_lower
                if total_lower <= count and len(minimum_candidates) < 2:
                    minimum_candidates.append((index, total_lower))

            reserve_max_upper = min(
                upper,
                1 + math.floor(math.fsum((duration, -maximum_cfl_step_s, tolerance)) / minimum_cfl_step_s),
            )
            if lower <= reserve_max_upper:
                total_upper = sum_upper - upper + reserve_max_upper
                if count <= total_upper and len(maximum_candidates) < 2:
                    maximum_candidates.append((index, total_upper))

            both_lower = max(
                lower,
                2,
                2 + math.ceil(math.fsum((duration, -minimum_cfl_step_s, -maximum_cfl_step_s, -tolerance)) / maximum_cfl_step_s),
            )
            both_upper = min(
                upper,
                2 + math.floor(math.fsum((duration, -minimum_cfl_step_s, -maximum_cfl_step_s, tolerance)) / minimum_cfl_step_s),
            )
            both_total_lower = sum_lower - lower + both_lower
            both_total_upper = sum_upper - upper + both_upper
            if both_lower <= both_upper and both_total_lower <= count <= both_total_upper:
                same_interval_extrema_possible = True

        distinct_interval_extrema_possible = any(
            minimum_index != maximum_index and minimum_total_lower <= count <= maximum_total_upper
            for minimum_index, minimum_total_lower in minimum_candidates[:2]
            for maximum_index, maximum_total_upper in maximum_candidates[:2]
        )
        if not same_interval_extrema_possible and not distinct_interval_extrema_possible:
            raise ValueError("minimum and maximum CFL step extrema cannot both occur in any feasible interval allocation")

    accepted_extrema = [minimum_cfl_step_s, maximum_cfl_step_s]
    accepted_extrema.extend(step for step in subminimum_alignment_step_s if step > 0.0)
    if not math.isclose(minimum_accepted_step_s, min(accepted_extrema), rel_tol=1e-12, abs_tol=1e-15):
        raise ValueError("minimum_accepted_step_s does not match CFL/alignment step extrema")
    if not math.isclose(maximum_accepted_step_s, max(accepted_extrema), rel_tol=1e-12, abs_tol=1e-15):
        raise ValueError("maximum_accepted_step_s does not match CFL/alignment step extrema")


class TriangularSolverRunConfig(_StrictContractModel):
    """Input contract for the P4-1 experimental closed-wall run capability."""

    format: Literal["triangular-solver-run-v1"] = "triangular-solver-run-v1"
    schema_version: Literal["1.0"] = "1.0"
    case_id: Text
    business_file: Text
    result_file: Text
    time_origin: Text | None = None
    output_times_s: list[Number] = Field(min_length=2, max_length=100_000)

    # These three are required on purpose. A missing open-boundary or source
    # configuration may not silently become a wall or zero source.
    boundary_policy: Literal["closed_wall"]
    initial_momentum: Literal["zero"]
    sources: Literal["none"]

    # The numerical route is fixed in P4-0; callers cannot select an
    # unimplemented solver variant through this contract.
    spatial_order: Literal[1] = 1
    numerical_flux: Literal["HLL"] = "HLL"
    bed_balance: Literal["hydrostatic_reconstruction"] = "hydrostatic_reconstruction"
    time_integrator: Literal["SSPRK2"] = "SSPRK2"

    gravity_m_s2: Number = Field(default=9.81, gt=0.0, le=20.0)
    cfl_number: Number = Field(default=0.25, gt=0.0, le=0.5)
    minimum_time_step_s: Number = Field(default=1e-6, ge=1e-9, le=1.0)
    maximum_time_step_s: Number = Field(default=60.0, gt=0.0, le=86_400.0)
    maximum_step_retries: StrictInt = Field(default=8, ge=0, le=20)
    dry_depth_threshold_m: Number = Field(default=1e-6, gt=0.0, le=0.1)
    negative_depth_tolerance_m: Number = Field(default=1e-12, ge=0.0, le=1e-4)
    volume_correction_budget_fraction: Number = Field(default=1e-10, ge=0.0, le=1e-6)
    maximum_face_count: StrictInt = Field(default=500_000, ge=1, le=10_000_000)
    overwrite_existing_result: StrictBool = False

    @field_validator("business_file", "result_file")
    @classmethod
    def relative_paths(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("path must be relative to the run config")
        return value

    @field_validator("time_origin")
    @classmethod
    def timezone_aware_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("time_origin must be an ISO8601 timestamp with timezone") from exc
        if parsed.utcoffset() is None:
            raise ValueError("time_origin must include a timezone")
        return value

    @model_validator(mode="after")
    def valid_time_and_retry_ranges(self) -> "TriangularSolverRunConfig":
        if self.business_file == self.result_file:
            raise ValueError("result_file must be distinct from business_file")
        if self.minimum_time_step_s > self.maximum_time_step_s:
            raise ValueError("minimum_time_step_s must be less than or equal to maximum_time_step_s")
        if self.negative_depth_tolerance_m >= self.dry_depth_threshold_m:
            raise ValueError("negative_depth_tolerance_m must be less than dry_depth_threshold_m")
        if not math.isclose(self.output_times_s[0], 0.0, rel_tol=0.0, abs_tol=0.0):
            raise ValueError("output_times_s must start at 0 s")
        if any(value < 0.0 or value > 31_536_000.0 for value in self.output_times_s):
            raise ValueError("output_times_s must be within [0, 31,536,000] s")
        if any(right <= left for left, right in zip(self.output_times_s, self.output_times_s[1:])):
            raise ValueError("output_times_s must be strictly increasing and unique")
        interval_durations = [math.fsum((right, -left)) for left, right in zip(self.output_times_s, self.output_times_s[1:])]
        if any(duration + _step_tolerance_s(duration) < self.minimum_time_step_s for duration in interval_durations):
            raise ValueError("each output_times_s interval must be at least minimum_time_step_s")
        return self


class TriangularConservedState(_StrictContractModel):
    """One face-aligned `(h, hu, hv)` state; it does not evolve the state."""

    face_id: list[StrictInt] = Field(min_length=1)
    water_depth_m: list[Number] = Field(min_length=1)
    momentum_x_m2_s: list[Number] = Field(min_length=1)
    momentum_y_m2_s: list[Number] = Field(min_length=1)

    @model_validator(mode="after")
    def aligned_nonnegative_state(self) -> "TriangularConservedState":
        lengths = {len(self.face_id), len(self.water_depth_m), len(self.momentum_x_m2_s), len(self.momentum_y_m2_s)}
        if len(lengths) != 1:
            raise ValueError("face_id, water_depth_m, momentum_x_m2_s and momentum_y_m2_s must have the same face count")
        if len(set(self.face_id)) != len(self.face_id) or any(face_id < 0 for face_id in self.face_id):
            raise ValueError("face_id must contain unique non-negative int64 identifiers")
        if any(face_id > (1 << 63) - 1 for face_id in self.face_id):
            raise ValueError("face_id values must fit in signed int64")
        if any(depth < 0.0 for depth in self.water_depth_m):
            raise ValueError("water_depth_m must be non-negative; significant negative depth is a failed state")
        return self

    def validate_for(self, mesh) -> "TriangularConservedState":
        if tuple(self.face_id) != tuple(mesh.face_id):
            raise TriangularSolverContractError("state face_id order does not match linked triangular mesh")
        if len(self.face_id) != mesh.n_face:
            raise TriangularSolverContractError("state must contain one (h, hu, hv) row per mesh face")
        return self


class TriangularMassBudget(_StrictContractModel):
    """Signed volume ledger for one saved output time, in m3."""

    initial_volume_m3: Number = Field(ge=0.0)
    boundary_net_volume_m3: Number
    source_net_volume_m3: Number
    correction_volume_m3: Number = Field(ge=0.0)
    final_volume_m3: Number = Field(ge=0.0)
    residual_m3: Number

    @model_validator(mode="after")
    def closed_wall_source_free_ledger(self) -> "TriangularMassBudget":
        if self.boundary_net_volume_m3 != 0.0:
            raise ValueError("closed_wall mass budget boundary_net_volume_m3 must equal 0 m3")
        if self.source_net_volume_m3 != 0.0:
            raise ValueError("sources=none mass budget source_net_volume_m3 must equal 0 m3")
        expected = self.final_volume_m3 - self.initial_volume_m3 - self.boundary_net_volume_m3 - self.source_net_volume_m3 - self.correction_volume_m3
        if not math.isclose(self.residual_m3, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("residual_m3 must equal final - initial - boundary - source - correction volume")
        return self


class TriangularResultContract(_StrictContractModel):
    """Schema for an experimental `(time, face)` simulation result.

    Constructing or validating this model does not run the solver or establish
    engineering approval. The P4-1 runner produces and verifies actual values.
    """

    format: Literal["triangular-solver-result-v1"]
    product_type: Literal["experimental_triangular_shallow_water_result"]
    simulation_result: Literal[True]
    experimental: Literal[True]
    production_approved: Literal[False]
    gate_hydraulics_enabled: Literal[False]
    manning_enabled: Literal[False]
    rainfall_enabled: Literal[False]
    sources_applied: Literal["none"]
    boundary_policy: Literal["closed_wall"]
    case_id: Text
    time_origin: Text | None
    coordinate_system: Text
    mesh_id: Text
    mesh_artifact: Text
    business_package: Text
    solver_version: Text
    run_config_sha256: Text
    topology_fingerprint: Text
    terrain_fingerprint: Text
    mesh_sha256: Text
    business_package_sha256: Text
    security_exclusion: SecurityExclusionEvidence | None = None
    gravity_m_s2: Number = Field(gt=0.0, le=20.0)
    cfl_number: Number = Field(gt=0.0, le=0.5)
    minimum_time_step_s: Number = Field(ge=1e-9, le=1.0)
    maximum_time_step_s: Number = Field(gt=0.0, le=86_400.0)
    maximum_step_retries: StrictInt = Field(ge=0, le=20)
    dry_depth_threshold_m: Number = Field(gt=0.0, le=0.1)
    negative_depth_tolerance_m: Number = Field(ge=0.0, le=1e-4)
    volume_correction_budget_fraction: Number = Field(ge=0.0, le=1e-6)
    volume_correction_reference_scale_m3: Number = Field(gt=0.0)
    volume_correction_budget_m3: Number = Field(ge=0.0)
    mesh_geometry_tolerance_m: Number = Field(default=1e-8, gt=0.0, le=1.0)
    minimum_face_area_m2: Number = Field(default=1e-12, gt=0.0, le=100.0)
    node_xy_m: list[list[Number]] = Field(min_length=3)
    face_nodes: list[list[StrictInt]] = Field(min_length=1)
    face_id: list[StrictInt] = Field(min_length=1)
    face_area_m2: list[Number] = Field(min_length=1)
    boundary_groups: dict[str, list[list[StrictInt]]] = Field(default_factory=dict)
    constraint_groups: dict[str, list[list[StrictInt]]] = Field(default_factory=dict)
    time_s: list[Number] = Field(min_length=2)
    water_depth_m: list[list[Number]] = Field(min_length=2)
    momentum_x_m2_s: list[list[Number]] = Field(min_length=2)
    momentum_y_m2_s: list[list[Number]] = Field(min_length=2)
    minimum_water_depth_m_by_time: list[Number] = Field(min_length=2)
    maximum_speed_m_s_by_time: list[Number] = Field(min_length=2)
    maximum_internal_edge_mass_pair_residual_m3_s: Number = Field(ge=0.0)
    rejected_step_reasons: dict[str, StrictInt] = Field(default_factory=dict)
    mass_budget: list[TriangularMassBudget] = Field(min_length=2)
    solver_scheme: Literal["cell_centered_first_order_hll_hydrostatic_ssprk2"]
    accepted_step_count: StrictInt = Field(ge=0)
    accepted_cfl_step_count: StrictInt = Field(ge=0)
    rejected_step_count: StrictInt = Field(ge=0)
    mass_correction_count: StrictInt = Field(ge=0)
    minimum_cfl_step_s: Number = Field(gt=0.0)
    maximum_cfl_step_s: Number = Field(gt=0.0)
    minimum_accepted_step_s: Number = Field(gt=0.0)
    maximum_accepted_step_s: Number = Field(gt=0.0)
    subminimum_alignment_step_s: list[Number] = Field(min_length=1)
    subminimum_alignment_cfl_limit_s: list[Number] = Field(min_length=1)
    geometry_metadata_inferred: StrictBool = False
    step_statistics_inferred: StrictBool = False
    diagnostics_inferred: StrictBool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_structural_compatibility(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        data = dict(raw)
        geometry_fields = ("mesh_geometry_tolerance_m", "minimum_face_area_m2", "boundary_groups", "constraint_groups")
        if any(name not in data for name in geometry_fields):
            data["geometry_metadata_inferred"] = True
        else:
            data.setdefault("geometry_metadata_inferred", False)
        data.setdefault("mesh_geometry_tolerance_m", 1e-8)
        data.setdefault("minimum_face_area_m2", 1e-12)
        data.setdefault("boundary_groups", {})
        data.setdefault("constraint_groups", {})

        step_fields = ("accepted_cfl_step_count", "minimum_cfl_step_s", "maximum_cfl_step_s", "subminimum_alignment_step_s", "subminimum_alignment_cfl_limit_s")
        missing = [name for name in step_fields if name not in data]
        if len(missing) == len(step_fields):
            # Legacy-shaped result payloads can still be structurally checked
            # by inferring that all accepted steps were CFL-limited. The
            # execution-bound validator below rejects inferred metadata.
            data["accepted_cfl_step_count"] = data.get("accepted_step_count")
            data["minimum_cfl_step_s"] = data.get("minimum_accepted_step_s")
            data["maximum_cfl_step_s"] = data.get("maximum_accepted_step_s")
            output_times = data.get("time_s", [])
            data["subminimum_alignment_step_s"] = [0.0] * max(0, len(output_times) - 1)
            data["subminimum_alignment_cfl_limit_s"] = [0.0] * max(0, len(output_times) - 1)
            data["step_statistics_inferred"] = True
        else:
            data.setdefault("step_statistics_inferred", False)
        diagnostic_fields = (
            "minimum_water_depth_m_by_time", "maximum_speed_m_s_by_time",
            "maximum_internal_edge_mass_pair_residual_m3_s",
        )
        if any(name not in data for name in diagnostic_fields):
            data["diagnostics_inferred"] = True
            try:
                depth_rows = data["water_depth_m"]
                x_rows = data["momentum_x_m2_s"]
                y_rows = data["momentum_y_m2_s"]
                dry_depth = float(data["dry_depth_threshold_m"])
                data.setdefault("minimum_water_depth_m_by_time", [min(row) for row in depth_rows])
                data.setdefault("maximum_speed_m_s_by_time", [
                    max((math.hypot(float(hx) / float(h), float(hy) / float(h)) if float(h) > dry_depth else 0.0)
                        for h, hx, hy in zip(hrow, xrow, yrow))
                    for hrow, xrow, yrow in zip(depth_rows, x_rows, y_rows)
                ])
                data.setdefault("maximum_internal_edge_mass_pair_residual_m3_s", 0.0)
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
        else:
            data.setdefault("diagnostics_inferred", False)
        return data

    @field_validator("mesh_artifact", "business_package")
    @classmethod
    def linked_paths_are_relative(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("result input artifact paths must be relative")
        return value

    @field_validator("run_config_sha256", "topology_fingerprint", "terrain_fingerprint", "mesh_sha256", "business_package_sha256")
    @classmethod
    def sha256_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("time_origin")
    @classmethod
    def validate_result_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("time_origin must be an ISO8601 timestamp with timezone") from exc
        if parsed.utcoffset() is None:
            raise ValueError("time_origin must include a timezone")
        return value

    @model_validator(mode="after")
    def time_face_arrays_match(self) -> "TriangularResultContract":
        try:
            embedded_mesh = TriangularMesh(
                self.node_xy_m,
                self.face_nodes,
                self.coordinate_system,
                mesh_id=self.mesh_id,
                face_id=self.face_id,
                boundary_groups=self.boundary_groups,
                constraint_groups=self.constraint_groups,
                geometry_tolerance_m=self.mesh_geometry_tolerance_m,
                minimum_face_area_m2=self.minimum_face_area_m2,
            )
        except (TriangularMeshError, TypeError, ValueError) as exc:
            raise ValueError(f"embedded triangular geometry/topology is invalid: {exc}") from exc
        if topology_fingerprint(embedded_mesh) != self.topology_fingerprint:
            raise ValueError("topology_fingerprint does not match embedded node/face/group geometry")
        if any(not math.isclose(actual, declared, rel_tol=1e-12, abs_tol=1e-12)
               for actual, declared in zip(embedded_mesh.face_area, self.face_area_m2)):
            raise ValueError("face_area_m2 does not match areas computed from embedded mesh geometry")
        if len(set(self.face_id)) != len(self.face_id) or any(face < 0 or face > (1 << 63) - 1 for face in self.face_id):
            raise ValueError("face_id must contain unique non-negative int64 identifiers")
        if len(self.face_nodes) != len(self.face_id) or len(self.face_area_m2) != len(self.face_id):
            raise ValueError("face_nodes, face_area_m2 and face_id must have the same face count")
        if any(len(face) != 3 for face in self.face_nodes):
            raise ValueError("face_nodes must preserve exactly three node indices per triangle")
        if any(len(set(face)) != 3 for face in self.face_nodes):
            raise ValueError("face_nodes must contain three distinct node indices per triangle")
        if any(len(point) != 2 for point in self.node_xy_m):
            raise ValueError("node_xy_m must contain exactly two projected coordinates per node")
        if any(node < 0 or node >= len(self.node_xy_m) for face in self.face_nodes for node in face):
            raise ValueError("face_nodes contains a node index outside node_xy_m")
        if any(area <= 0.0 for area in self.face_area_m2):
            raise ValueError("face_area_m2 must contain positive projected areas")
        if not math.isclose(self.time_s[0], 0.0, rel_tol=0.0, abs_tol=0.0):
            raise ValueError("time_s must start at 0 s")
        if any(value > 31_536_000.0 for value in self.time_s):
            raise ValueError("time_s must be within [0, 31,536,000] s")
        if any(right <= left for left, right in zip(self.time_s, self.time_s[1:])):
            raise ValueError("time_s must be strictly increasing and unique")
        for name in ("water_depth_m", "momentum_x_m2_s", "momentum_y_m2_s"):
            values = getattr(self, name)
            if len(values) != len(self.time_s) or any(len(row) != len(self.face_id) for row in values):
                raise ValueError(f"{name} must have shape (time, face) matching time_s and face_id")
        if any(value < 0.0 for row in self.water_depth_m for value in row):
            raise ValueError("water_depth_m must be non-negative in every saved state")
        if len(self.minimum_water_depth_m_by_time) != len(self.time_s) or len(self.maximum_speed_m_s_by_time) != len(self.time_s):
            raise ValueError("saved depth/velocity diagnostics must contain one value per output time")
        for index, (depths, x_momentum, y_momentum) in enumerate(zip(self.water_depth_m, self.momentum_x_m2_s, self.momentum_y_m2_s)):
            expected_minimum = min(depths)
            expected_speed = max(
                (math.hypot(hx / h, hy / h) if h > self.dry_depth_threshold_m else 0.0)
                for h, hx, hy in zip(depths, x_momentum, y_momentum)
            )
            if not math.isclose(self.minimum_water_depth_m_by_time[index], expected_minimum, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("minimum_water_depth_m_by_time must be computed from saved face depths")
            if not math.isclose(self.maximum_speed_m_s_by_time[index], expected_speed, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("maximum_speed_m_s_by_time must be computed from saved momenta and depths")
            if any(h <= self.dry_depth_threshold_m and (hx != 0.0 or hy != 0.0) for h, hx, hy in zip(depths, x_momentum, y_momentum)):
                raise ValueError("faces at/below dry_depth_threshold_m must have zero stored momentum")
        if self.maximum_internal_edge_mass_pair_residual_m3_s > 1e-12:
            raise ValueError("maximum internal-edge mass-pair cancellation residual exceeds 1e-12 m3/s")
        if any(not name or count < 0 for name, count in self.rejected_step_reasons.items()):
            raise ValueError("rejected_step_reasons must contain named non-negative counts")
        if sum(self.rejected_step_reasons.values()) != self.rejected_step_count:
            raise ValueError("rejected_step_reasons counts must sum to rejected_step_count")
        if len(self.mass_budget) != len(self.time_s):
            raise ValueError("mass_budget must contain one entry per saved time")
        _validate_realizable_step_statistics(
            time_s=self.time_s,
            minimum_time_step_s=self.minimum_time_step_s,
            maximum_time_step_s=self.maximum_time_step_s,
            accepted_step_count=self.accepted_step_count,
            accepted_cfl_step_count=self.accepted_cfl_step_count,
            minimum_cfl_step_s=self.minimum_cfl_step_s,
            maximum_cfl_step_s=self.maximum_cfl_step_s,
            minimum_accepted_step_s=self.minimum_accepted_step_s,
            maximum_accepted_step_s=self.maximum_accepted_step_s,
            subminimum_alignment_step_s=self.subminimum_alignment_step_s,
            subminimum_alignment_cfl_limit_s=self.subminimum_alignment_cfl_limit_s,
        )
        volumes = [sum(depth * area for depth, area in zip(row, self.face_area_m2)) for row in self.water_depth_m]
        if any(not math.isclose(budget.final_volume_m3, volume, rel_tol=1e-10, abs_tol=1e-12) for budget, volume in zip(self.mass_budget, volumes)):
            raise ValueError("mass_budget final_volume_m3 must equal the saved face-depth integral")
        expected_initial_volume = volumes[0]
        if not math.isclose(self.mass_budget[0].initial_volume_m3, expected_initial_volume, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("first-frame initial_volume_m3 must equal the water volume computed from initial h and embedded face areas")
        if self.mass_budget[0].correction_volume_m3 != 0.0:
            raise ValueError("first-frame cumulative correction_volume_m3 must be 0 before any accepted step")
        reference_depth = max(self.water_depth_m[0], default=0.0)
        actual_volume_scale = max(expected_initial_volume, sum(self.face_area_m2) * reference_depth, 1.0)
        if not math.isclose(self.volume_correction_reference_scale_m3, actual_volume_scale, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("volume_correction_reference_scale_m3 must be computed from the first-frame state and embedded face areas")
        initial_volumes = [budget.initial_volume_m3 for budget in self.mass_budget]
        if any(not math.isclose(value, initial_volumes[0], rel_tol=1e-12, abs_tol=1e-12) for value in initial_volumes[1:]):
            raise ValueError("mass_budget initial_volume_m3 must remain the same reference volume at every output")
        corrections = [budget.correction_volume_m3 for budget in self.mass_budget]
        if any(right < left for left, right in zip(corrections, corrections[1:])):
            raise ValueError("mass_budget correction_volume_m3 must be cumulative and non-decreasing")
        if (self.mass_correction_count == 0) != (not any(value > 0.0 for value in corrections)):
            raise ValueError("mass_correction_count must reflect whether any positive volume correction was recorded")
        if self.minimum_accepted_step_s > self.maximum_accepted_step_s:
            raise ValueError("minimum_accepted_step_s must be less than or equal to maximum_accepted_step_s")
        if self.minimum_time_step_s > self.maximum_time_step_s:
            raise ValueError("minimum_time_step_s must be less than or equal to maximum_time_step_s")
        if self.negative_depth_tolerance_m >= self.dry_depth_threshold_m:
            raise ValueError("negative_depth_tolerance_m must be less than dry_depth_threshold_m")
        expected_budget = self.volume_correction_budget_fraction * self.volume_correction_reference_scale_m3
        if not math.isclose(self.volume_correction_budget_m3, expected_budget, rel_tol=1e-12, abs_tol=1e-15):
            raise ValueError("volume_correction_budget_m3 must equal fraction times reference scale")
        if any(item.correction_volume_m3 > self.volume_correction_budget_m3 for item in self.mass_budget):
            raise ValueError("cumulative correction volume may not exceed volume_correction_budget_m3")
        if self.accepted_step_count <= 0:
            raise ValueError("a completed positive-duration result must report at least one accepted step")
        return self


@dataclass(frozen=True)
class TriangularRunCapability:
    """Validated P4-0 request and linked P3-A inputs; no run has been executed."""

    config_path: Path
    business_path: Path
    mesh_path: Path
    result_path: Path
    config: TriangularSolverRunConfig
    business: TriangularBusinessPackage
    report: Mapping[str, Any]


def load_triangular_solver_run_config(path: str | Path) -> TriangularSolverRunConfig:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TriangularSolverContractError(f"triangular solver run config not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise TriangularSolverContractError(f"invalid triangular solver run YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TriangularSolverContractError("triangular solver run config root must be a YAML mapping")
    try:
        return TriangularSolverRunConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}" for issue in exc.errors())
        raise TriangularSolverContractError(f"invalid triangular solver run config: {details}") from exc


def _resolve_relative(base: Path, relative: str, label: str) -> Path:
    if Path(relative).is_absolute() or PureWindowsPath(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise TriangularSolverContractError(f"{label} must be relative to the run config/business package")
    return (base / relative).resolve()


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise TriangularSolverContractError(f"cannot read linked artifact '{path}': {exc}") from exc


def validate_triangular_run_request(path: str | Path, *, allow_existing_result: bool = False) -> TriangularRunCapability:
    """Validate input capability without advancing or writing a solver run."""
    config_path = Path(path).resolve()
    config = load_triangular_solver_run_config(config_path)
    business_path = _resolve_relative(config_path.parent, config.business_file, "business_file")
    lexical_result_path = config_path.parent / config.result_file
    if lexical_result_path.is_symlink():
        raise TriangularSolverContractError("result_file must not be a symbolic link")
    result_path = _resolve_relative(config_path.parent, config.result_file, "result_file")
    try:
        raw_business = json.loads(business_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TriangularSolverContractError(f"linked P3-A business package not found: {business_path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TriangularSolverContractError(f"cannot read linked P3-A business package: {exc}") from exc
    if not isinstance(raw_business, dict) or not isinstance(raw_business.get("mesh_artifact"), str):
        raise TriangularSolverContractError("linked P3-A package must contain a relative mesh_artifact path")
    mesh_path = _resolve_relative(business_path.parent, raw_business["mesh_artifact"], "mesh_artifact")
    protected_inputs = {config_path, business_path, mesh_path}
    if result_path in protected_inputs:
        raise TriangularSolverContractError("result_file must not overwrite the run config, P3-A package, or its linked P2 mesh")
    if result_path.exists():
        try:
            if any(os.path.samefile(result_path, source) for source in protected_inputs):
                raise TriangularSolverContractError("result_file must not overwrite a hard-linked run config, P3-A package, or P2 mesh")
        except OSError as exc:
            raise TriangularSolverContractError(f"cannot safely inspect existing result_file identity: {exc}") from exc
    if result_path.exists() and not result_path.is_file():
        raise TriangularSolverContractError(f"result_file must be a regular file path: {result_path}")
    if result_path.exists() and not config.overwrite_existing_result and not allow_existing_result:
        raise TriangularSolverContractError(f"result_file already exists; set overwrite_existing_result: true to authorize replacement: {result_path}")
    try:
        business = read_triangular_business_package(business_path)
    except (TriangularBusinessError, TriangularMeshError, OSError) as exc:
        raise TriangularSolverContractError(f"linked P3-A package failed identity/contract validation: {exc}") from exc
    mesh = business.mesh
    terrain = business.terrain
    initial = business.initial_condition
    if mesh.n_face > config.maximum_face_count:
        raise TriangularSolverContractError(f"mesh has {mesh.n_face} faces, above maximum_face_count={config.maximum_face_count}")
    if not all(terrain.valid_mask):
        missing = [str(mesh.face_id[index]) for index, valid in enumerate(terrain.valid_mask) if not valid]
        raise TriangularSolverContractError(f"P4-1 closed-wall capability requires complete terrain; invalid face_id values include {missing[:8]}")
    for index, (covered, ratio, area) in enumerate(zip(terrain.covered_area_m2, terrain.coverage_ratio, mesh.face_area)):
        if ratio < 1.0 - 1e-9 or not math.isclose(covered, area, rel_tol=1e-9, abs_tol=1e-8):
            raise TriangularSolverContractError(f"P4-1 closed-wall capability requires full terrain area coverage at face_id {mesh.face_id[index]}")
    if not all(initial.valid_mask):
        missing = [str(mesh.face_id[index]) for index, valid in enumerate(initial.valid_mask) if not valid]
        raise TriangularSolverContractError(f"P4-1 closed-wall capability requires a complete initial field; invalid face_id values include {missing[:8]}")
    if tuple(initial.face_id) != tuple(mesh.face_id):
        raise TriangularSolverContractError("P3-A initial-condition face_id order does not match mesh")
    if business.boundary_bindings:
        groups = [binding.group_name for binding in business.boundary_bindings]
        raise TriangularSolverContractError(f"P4-1 closed_wall scope does not accept P3-A open/group boundary bindings: {groups}; no binding is discarded or converted to a wall")
    checks = {
        "mesh_and_terrain_identity": "pass",
        "complete_terrain": "pass",
        "complete_initial_condition": "pass",
        "zero_initial_momentum": "declared_by_run_config",
        "all_exterior_edges_closed_wall": "declared_by_run_config",
        "open_boundary_bindings": "none",
        "source_terms": "none",
        "roughness": "retained_as_input_provenance_not_applied",
    }
    if initial.quantity.value == "depth":
        initial_depths = tuple(float(value) for value in initial.values_m)
    else:
        initial_depths = tuple(max(float(level) - float(elevation), 0.0) for level, elevation in zip(initial.values_m, terrain.elevation_m))
    initial_volume = sum(depth * area for depth, area in zip(initial_depths, mesh.face_area))
    total_area = sum(mesh.face_area)
    reference_depth = max(initial_depths, default=0.0)
    volume_scale = max(initial_volume, total_area * reference_depth, 1.0)
    correction_budget = config.volume_correction_budget_fraction * volume_scale
    security = business.security_exclusion
    security_summary = None if security is None else {
        "enabled": True,
        "evidence_sha256": security_evidence_sha256(security),
        "source_union_geometry_sha256": security.source_union_geometry_sha256,
        "active_face_id_sha256": security.active_face_id_sha256,
        "original_domain_area_m2": security.original_domain_area_m2,
        "security_inside_area_m2": security.security_inside_area_m2,
        "active_domain_area_m2": security.active_domain_area_m2,
        "active_mesh_area_m2": security.active_mesh_area_m2,
        "security_boundary_edge_count": security.security_boundary_edge_count,
    }
    report = {
        "status": "ready_for_experimental_closed_wall_run",
        "contract_only": False,
        "preflight_only": True,
        "execution_available": True,
        "solver_completed": False,
        "experimental": True,
        "production_approved": False,
        "case_id": config.case_id,
        "mesh_id": mesh.mesh_id,
        "topology_fingerprint": topology_fingerprint(mesh),
        "terrain_fingerprint": terrain_fingerprint(mesh, terrain),
        "security_exclusion": security_summary,
        "coordinate_system": mesh.coordinate_system,
        "face_count": mesh.n_face,
        "boundary_policy": config.boundary_policy,
        "initial_momentum": config.initial_momentum,
        "sources": config.sources,
        "manning_enabled": False,
        "rainfall_enabled": False,
        "gate_hydraulics_enabled": False,
        "volume_correction_budget_fraction": config.volume_correction_budget_fraction,
        "reference_depth_m": reference_depth,
        "initial_water_volume_m3": initial_volume,
        "volume_correction_reference_scale_m3": volume_scale,
        "volume_correction_budget_m3": correction_budget,
        "checks": checks,
        "artifacts": {
            "business_file": {"path": business_path.name, "sha256": _sha256(business_path)},
            "mesh_file": {"path": mesh_path.name, "sha256": _sha256(mesh_path)},
            "result_file": result_path.name,
        },
    }
    return TriangularRunCapability(config_path, business_path, mesh_path, result_path, config, business, report)


def validate_triangular_result_for_run(
    result: TriangularResultContract | Mapping[str, Any],
    run_config_path: str | Path,
    *,
    result_model: type[TriangularResultContract] = TriangularResultContract,
    expected_sources: str | None = None,
    artifact_result_path: str | Path | None = None,
) -> TriangularResultContract:
    """Bind a result contract to freshly re-read P2/P3-A inputs and run config.

    P4-1 writers/readers must call this after constructing/loading the result;
    SHA string shape alone is not an input identity check.
    """
    capability = validate_triangular_run_request(run_config_path, allow_existing_result=True)
    try:
        current_data = result.model_dump(mode="python") if isinstance(result, TriangularResultContract) else result
        # Pydantic's default instance policy may return a model unchanged. Dump
        # to a mapping first so mutable nested lists/budgets are always checked.
        contract = result_model.model_validate(current_data)
    except ValidationError as exc:
        raise TriangularSolverContractError(f"triangular result contract is invalid: {exc}") from exc
    if contract.geometry_metadata_inferred or contract.step_statistics_inferred or contract.diagnostics_inferred:
        raise TriangularSolverContractError("result must explicitly embed P2 geometry metadata, accepted-step statistics, and calculated diagnostics")
    mesh = capability.business.mesh
    terrain = capability.business.terrain
    if contract.case_id != capability.config.case_id:
        raise TriangularSolverContractError("result case_id does not match run config")
    if contract.mesh_id != mesh.mesh_id or contract.coordinate_system != mesh.coordinate_system:
        raise TriangularSolverContractError("result mesh_id/coordinate_system does not match freshly read P2 mesh")
    if contract.topology_fingerprint != topology_fingerprint(mesh):
        raise TriangularSolverContractError("result topology_fingerprint does not match freshly read P2 topology")
    if contract.terrain_fingerprint != terrain_fingerprint(mesh, terrain):
        raise TriangularSolverContractError("result terrain_fingerprint does not match freshly read P2 terrain")
    if tuple(tuple(point) for point in contract.node_xy_m) != tuple(tuple(point) for point in mesh.node_xy):
        raise TriangularSolverContractError("result node_xy_m does not match the linked P2 mesh")
    if tuple(tuple(face) for face in contract.face_nodes) != tuple(tuple(face) for face in mesh.face_nodes):
        raise TriangularSolverContractError("result face_nodes does not match the linked P2 connectivity")
    if tuple(contract.face_id) != tuple(mesh.face_id):
        raise TriangularSolverContractError("result face_id order does not match the linked P2 mesh")
    if any(not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12) for actual, expected in zip(contract.face_area_m2, mesh.face_area)):
        raise TriangularSolverContractError("result face_area_m2 does not match the linked P2 geometry")
    expected_boundaries = {
        name: [list(mesh.edge_nodes[edge_index]) for edge_index in edge_indices]
        for name, edge_indices in mesh.boundary_groups.items()
    }
    expected_constraints = {
        name: [list(mesh.edge_nodes[edge_index]) for edge_index in edge_indices]
        for name, edge_indices in mesh.constraint_groups.items()
    }
    if contract.boundary_groups != expected_boundaries or contract.constraint_groups != expected_constraints:
        raise TriangularSolverContractError("result boundary/constraint groups do not match linked P2 topology")
    if (not math.isclose(contract.mesh_geometry_tolerance_m, mesh.geometry_tolerance_m, rel_tol=0.0, abs_tol=0.0)
            or not math.isclose(contract.minimum_face_area_m2, mesh.minimum_face_area_m2, rel_tol=0.0, abs_tol=0.0)):
        raise TriangularSolverContractError("result mesh geometry tolerances do not match linked P2 artifact")
    expected_security = capability.business.security_exclusion
    if (contract.security_exclusion is None) != (expected_security is None):
        raise TriangularSolverContractError("result security-exclusion evidence presence does not match linked P2/P3 inputs")
    if contract.security_exclusion is not None and expected_security is not None:
        if security_evidence_sha256(contract.security_exclusion) != security_evidence_sha256(expected_security):
            raise TriangularSolverContractError("result security-exclusion evidence does not match freshly revalidated P2/P3 inputs")

    initial = capability.business.initial_condition
    if initial.quantity.value == "depth":
        expected_initial_depth = tuple(float(value) for value in initial.values_m)
    else:
        expected_initial_depth = tuple(max(float(level) - float(elevation), 0.0) for level, elevation in zip(initial.values_m, terrain.elevation_m))
    if any(not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12) for actual, expected in zip(contract.water_depth_m[0], expected_initial_depth)):
        raise TriangularSolverContractError("result first-frame water_depth_m does not match complete linked P3-A initial condition")
    if any(value != 0.0 for value in (*contract.momentum_x_m2_s[0], *contract.momentum_y_m2_s[0])):
        raise TriangularSolverContractError("result first-frame momentum must match initial_momentum: zero")
    if tuple(contract.time_s) != tuple(capability.config.output_times_s):
        raise TriangularSolverContractError("result time_s must exactly match run config output_times_s")
    source_mode = capability.config.sources if expected_sources is None else expected_sources
    if expected_sources is not None and (capability.config.sources != "none" or expected_sources not in {"rainfall", "manning_and_rainfall"}):
        raise TriangularSolverContractError("a source-enabled result may only be bound to a sources=none base config")
    if contract.sources_applied != source_mode or contract.boundary_policy != capability.config.boundary_policy:
        raise TriangularSolverContractError("result boundary/source capabilities do not match run config")
    if contract.time_origin != capability.config.time_origin:
        if contract.time_origin is None or capability.config.time_origin is None:
            raise TriangularSolverContractError("result time_origin must exactly preserve the run-config origin or null")
        result_origin = datetime.fromisoformat(contract.time_origin.replace("Z", "+00:00"))
        config_origin = datetime.fromisoformat(capability.config.time_origin.replace("Z", "+00:00"))
        if result_origin.astimezone(timezone.utc) != config_origin.astimezone(timezone.utc):
            raise TriangularSolverContractError("result time_origin does not represent the same instant as run config")

    config = capability.config
    for name in (
        "gravity_m_s2", "cfl_number", "minimum_time_step_s", "maximum_time_step_s",
        "maximum_step_retries", "dry_depth_threshold_m", "negative_depth_tolerance_m",
        "volume_correction_budget_fraction",
    ):
        actual = getattr(contract, name)
        expected = getattr(config, name)
        if isinstance(expected, float):
            equal = math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-15)
        else:
            equal = actual == expected
        if not equal:
            raise TriangularSolverContractError(f"result {name} does not match run config")

    expected_scale = capability.report["volume_correction_reference_scale_m3"]
    expected_budget = capability.report["volume_correction_budget_m3"]
    if (not math.isclose(contract.volume_correction_reference_scale_m3, expected_scale, rel_tol=1e-12, abs_tol=1e-12)
            or not math.isclose(contract.volume_correction_budget_m3, expected_budget, rel_tol=1e-12, abs_tol=1e-15)):
        raise TriangularSolverContractError("result correction budget does not match actual initial state and linked face areas")
    actual_initial_volume = capability.report["initial_water_volume_m3"]
    if not math.isclose(contract.mass_budget[0].initial_volume_m3, actual_initial_volume, rel_tol=1e-12, abs_tol=1e-12):
        raise TriangularSolverContractError("result initial mass budget does not match linked P3-A initial state")

    artifact_path = capability.result_path if artifact_result_path is None else Path(artifact_result_path).resolve()
    expected_mesh_ref = Path(os.path.relpath(capability.mesh_path, artifact_path.parent)).as_posix()
    expected_business_ref = Path(os.path.relpath(capability.business_path, artifact_path.parent)).as_posix()
    if contract.mesh_artifact != expected_mesh_ref or contract.business_package != expected_business_ref:
        raise TriangularSolverContractError("result input artifact references do not match the configured P2/P3-A files")
    if contract.run_config_sha256 != _sha256(capability.config_path):
        raise TriangularSolverContractError("result run_config_sha256 does not match the run config contents")
    if contract.mesh_sha256 != _sha256(capability.mesh_path):
        raise TriangularSolverContractError("result mesh_sha256 does not match the linked P2 artifact")
    if contract.business_package_sha256 != _sha256(capability.business_path):
        raise TriangularSolverContractError("result business_package_sha256 does not match the linked P3-A package")
    return contract


__all__ = [
    "TriangularConservedState", "TriangularMassBudget", "TriangularResultContract",
    "TriangularRunCapability", "TriangularSolverContractError", "TriangularSolverRunConfig",
    "load_triangular_solver_run_config", "validate_triangular_result_for_run", "validate_triangular_run_request",
]
