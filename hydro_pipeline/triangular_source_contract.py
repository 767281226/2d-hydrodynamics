"""Versioned run/result contracts for rainfall and combined source execution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Mapping

from pydantic import Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator
import yaml

from .config import Number, Text
from .triangular_solver_contract import (
    TriangularResultContract,
    TriangularRunCapability,
    TriangularSolverContractError,
    _StrictContractModel,
    _source_cfl_bound_exceeded,
    validate_triangular_result_for_run,
    validate_triangular_run_request,
)
from .triangular_sources import (
    TriangularSourcePreparationConfig,
    TriangularSourcePreparationResult,
    load_triangular_source_preparation_config,
    read_triangular_source_preparation,
)


SOURCE_SOLVER_VERSION = "triangular-source-1.0.0"
SOURCE_RESULT_FORMAT = "triangular-source-result-v1"
SOURCE_RESULT_PRODUCT = "experimental_triangular_source_shallow_water_result"


class TriangularSourceContractError(ValueError):
    """Raised for an invalid rainfall/combined-source run or result binding."""


class TriangularSourceRunConfig(_StrictContractModel):
    format: Literal["triangular-source-run-v1"] = "triangular-source-run-v1"
    schema_version: Literal["1.0"] = "1.0"
    case_id: Text
    sources: Literal["rainfall", "manning_and_rainfall"]
    base_run_config_file: Text
    source_config_file: Text
    result_file: Text
    overwrite_existing_result: StrictBool = False

    @field_validator("base_run_config_file", "source_config_file", "result_file")
    @classmethod
    def relative_artifact_paths(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("path must be relative to the source run config")
        return value


class AppliedRainfallInterval(_StrictContractModel):
    """Original prepared interval and the exact base-output interval it applies to."""

    input_start_time_s: Number = Field(ge=0.0)
    input_end_time_s: Number = Field(gt=0.0)
    effective_start_time_s: Number = Field(ge=0.0)
    effective_end_time_s: Number = Field(gt=0.0)
    start_output_index: StrictInt = Field(ge=0)
    end_output_index: StrictInt = Field(ge=1)
    input_intensity: Number = Field(ge=0.0)
    input_unit: Literal["mm/h", "m/s"]
    rainfall_rate_m_s: Number = Field(ge=0.0)
    input_interval_rainfall_depth_m: Number = Field(ge=0.0)
    input_interval_rainfall_volume_m3: Number = Field(ge=0.0)
    applied_interval_rainfall_depth_m: Number = Field(ge=0.0)
    applied_interval_rainfall_volume_m3: Number = Field(ge=0.0)


class SecurityRainfallAccounting(_StrictContractModel):
    """Rainfall split by active/excluded geometry; excluded rain is not a loss or transferred flow."""

    active_area_m2: Number = Field(gt=0.0)
    security_excluded_area_m2: Number = Field(ge=0.0)
    accepted_active_rainfall_volume_m3_by_interval: list[Number] = Field(min_length=1)
    security_excluded_unmodeled_rainfall_volume_m3_by_interval: list[Number] = Field(min_length=1)
    accepted_active_rainfall_volume_m3: Number = Field(ge=0.0)
    security_excluded_unmodeled_rainfall_volume_m3: Number = Field(ge=0.0)
    excluded_rain_is_accepted_budget_loss: Literal[False] = False
    automatic_flow_transfer_from_excluded_area: Literal[False] = False


class TriangularSourceMassBudget(_StrictContractModel):
    """Closed-wall source-aware water-volume ledger in m3."""

    initial_volume_m3: Number = Field(ge=0.0)
    boundary_net_volume_m3: Literal[0.0] = 0.0
    source_net_volume_m3: Number = Field(ge=0.0)
    correction_volume_m3: Number = Field(ge=0.0)
    final_volume_m3: Number = Field(ge=0.0)
    residual_m3: Number

    @model_validator(mode="after")
    def source_ledger_closes(self) -> "TriangularSourceMassBudget":
        expected = self.final_volume_m3 - self.initial_volume_m3 - self.source_net_volume_m3 - self.correction_volume_m3
        if not math.isclose(self.residual_m3, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("residual_m3 must equal final - initial - source - correction volume")
        return self


class TriangularSourceResultContract(TriangularResultContract):
    """Native result contract for applied rainfall, optionally with Manning."""

    format: Literal["triangular-source-result-v1"]
    product_type: Literal["experimental_triangular_source_shallow_water_result"]
    sources_applied: Literal["rainfall", "manning_and_rainfall"]
    manning_enabled: StrictBool
    manning_applied: StrictBool
    rainfall_enabled: Literal[True]
    rainfall_applied: Literal[True]
    mass_budget: list[TriangularSourceMassBudget] = Field(min_length=2)

    base_run_config_artifact: Text
    source_run_config_artifact: Text
    source_config_artifact: Text
    source_preparation_artifact: Text
    source_run_config_sha256: Text
    source_config_sha256: Text
    source_preparation_sha256: Text

    rainfall_input_unit: Literal["mm/h", "m/s"]
    rainfall_schedule: list[AppliedRainfallInterval] = Field(min_length=1)
    rainfall_rate_m_s_by_interval: list[Number] = Field(min_length=1)
    rainfall_volume_m3_by_interval: list[Number] = Field(min_length=1)
    cumulative_rainfall_volume_m3_by_time: list[Number] = Field(min_length=2)
    rainfall_total_depth_m: Number = Field(ge=0.0)
    rainfall_total_volume_m3: Number = Field(ge=0.0)
    rainfall_stage_evaluation_count: StrictInt = Field(ge=0)
    rainfall_nonzero_source_stage_evaluation_count: StrictInt = Field(ge=0)
    rainfall_nonzero_effect_applied: StrictBool
    security_rainfall_accounting: SecurityRainfallAccounting | None = None

    manning_source_unit: Literal["1"] | None
    manning_physical_unit: Literal["s/m^(1/3)"] | None
    manning_interpretation_policy: Literal["legacy_unit1_as_si_manning"] | None
    manning_interpretation_rationale: Text | None
    manning_face_id: list[StrictInt] = Field(default_factory=list)
    manning_n: list[Number] = Field(default_factory=list)
    source_cfl_fraction: Number | None = Field(default=None, gt=0.0, le=1.0)
    maximum_manning_gamma_per_s: Number = Field(ge=0.0)
    maximum_manning_source_magnitude_m2_s2: Number = Field(ge=0.0)
    maximum_manning_gamma_per_s_by_time: list[Number] = Field(min_length=2)
    maximum_manning_source_magnitude_m2_s2_by_time: list[Number] = Field(min_length=2)
    manning_nonzero_face_stage_evaluation_count: StrictInt = Field(ge=0)
    source_cfl_limit_evaluation_count: StrictInt = Field(ge=0)
    minimum_source_cfl_limit_s: Number | None = Field(default=None, gt=0.0)
    maximum_source_cfl_limit_s: Number | None = Field(default=None, gt=0.0)
    manning_source_cfl_limited_step_count: StrictInt = Field(ge=0)
    manning_source_cfl_rejected_step_count: StrictInt = Field(ge=0)
    nonzero_manning_effect_applied: StrictBool

    @field_validator("source_run_config_sha256", "source_config_sha256", "source_preparation_sha256")
    @classmethod
    def digest_shape(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("must be a lowercase SHA-256 digest")
        return value

    @field_validator("base_run_config_artifact", "source_run_config_artifact", "source_config_artifact", "source_preparation_artifact")
    @classmethod
    def relative_result_references(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("source result artifact references must be relative")
        return value

    @model_validator(mode="after")
    def source_contract_is_consistent(self) -> "TriangularSourceResultContract":
        face_count = len(self.face_id)
        output_count = len(self.time_s)
        interval_count = output_count - 1
        expected_mode = "manning_and_rainfall" if self.manning_enabled else "rainfall"
        if self.sources_applied != expected_mode or self.manning_applied != self.manning_enabled:
            raise ValueError("source applied flags must match the requested rainfall/Manning mode")
        if len(self.mass_budget) != output_count:
            raise ValueError("source mass budget must contain one entry per output time")
        if (len(self.rainfall_rate_m_s_by_interval) != interval_count
                or len(self.rainfall_volume_m3_by_interval) != interval_count
                or len(self.cumulative_rainfall_volume_m3_by_time) != output_count):
            raise ValueError("rainfall interval/time arrays must align with the base output time axis")
        if self.rainfall_stage_evaluation_count != 2 * self.accepted_step_count:
            raise ValueError("rainfall must be evaluated at both SSPRK2 stages of every accepted step")
        if self.rainfall_nonzero_source_stage_evaluation_count > self.rainfall_stage_evaluation_count:
            raise ValueError("nonzero rainfall stage count may not exceed total rainfall stage evaluations")
        if self.rainfall_nonzero_effect_applied != (self.rainfall_nonzero_source_stage_evaluation_count > 0):
            raise ValueError("rainfall_nonzero_effect_applied must reflect nonzero accepted rainfall stages")
        has_positive_rain_rate = any(rate > 0.0 for rate in self.rainfall_rate_m_s_by_interval)
        if not has_positive_rain_rate and (
            self.rainfall_nonzero_source_stage_evaluation_count != 0 or self.rainfall_nonzero_effect_applied
        ):
            raise ValueError("all-zero rainfall input must have zero nonzero-stage count and a false nonzero-effect flag")
        if has_positive_rain_rate and (
            self.rainfall_nonzero_source_stage_evaluation_count == 0 or not self.rainfall_nonzero_effect_applied
        ):
            raise ValueError("positive rainfall input must be applied at an accepted nonzero stage")
        if len(self.cumulative_rainfall_volume_m3_by_time) != len(self.mass_budget):
            raise ValueError("cumulative rainfall volume must align with mass_budget")
        if self.cumulative_rainfall_volume_m3_by_time[0] != 0.0:
            raise ValueError("cumulative rainfall volume must start at zero")
        cumulative = 0.0
        for index, (increment, total, budget) in enumerate(zip(
            self.rainfall_volume_m3_by_interval,
            self.cumulative_rainfall_volume_m3_by_time[1:],
            self.mass_budget[1:],
        )):
            if increment < 0.0:
                raise ValueError("rainfall interval volumes must be non-negative")
            cumulative = math.fsum((cumulative, increment))
            if not math.isclose(total, cumulative, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(f"cumulative rainfall volume does not match interval volumes at output {index + 1}")
            if not math.isclose(budget.source_net_volume_m3, total, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("mass_budget source volume must equal cumulative rainfall volume")
        if not math.isclose(self.rainfall_total_volume_m3, cumulative, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("rainfall_total_volume_m3 must equal the accepted interval total")
        if self.mass_budget[0].source_net_volume_m3 != 0.0:
            raise ValueError("initial-frame source volume must be zero")
        area = math.fsum(self.face_area_m2)
        expected_cumulative = 0.0
        for index, (rate, actual_volume, start, end, recorded_cumulative) in enumerate(zip(
            self.rainfall_rate_m_s_by_interval,
            self.rainfall_volume_m3_by_interval,
            self.time_s,
            self.time_s[1:],
            self.cumulative_rainfall_volume_m3_by_time[1:],
        )):
            effective_duration = math.fsum((end, -start))
            expected_volume = rate * effective_duration * area
            if not math.isclose(actual_volume, expected_volume, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"rainfall output interval {index} volume must equal SI rate times effective duration and total area")
            expected_cumulative = math.fsum((expected_cumulative, expected_volume))
            if not math.isclose(recorded_cumulative, expected_cumulative, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"cumulative rainfall volume at output {index + 1} must equal the independent output-time integral")
        for interval, recorded_rate in enumerate(self.rainfall_rate_m_s_by_interval):
            if recorded_rate < 0.0:
                raise ValueError("rainfall rates must be non-negative SI values")

        if self.manning_enabled:
            if (self.manning_source_unit != "1" or self.manning_physical_unit != "s/m^(1/3)"
                    or self.manning_interpretation_policy != "legacy_unit1_as_si_manning"
                    or self.manning_interpretation_rationale is None or not self.manning_interpretation_rationale.strip()):
                raise ValueError("combined execution requires the explicit unchanged-unit Manning interpretation")
            if (len(self.manning_face_id) != face_count or len(self.manning_n) != face_count
                    or tuple(self.manning_face_id) != tuple(self.face_id)):
                raise ValueError("combined Manning values and face IDs must cover result faces in P2 order")
            if any(not 0.0 < value <= 1.0 for value in self.manning_n):
                raise ValueError("SI Manning n must satisfy 0 < n <= 1 on every face")
            if self.source_cfl_fraction is None:
                raise ValueError("combined execution requires its source_cfl_fraction")
            if (len(self.maximum_manning_gamma_per_s_by_time) != output_count
                    or len(self.maximum_manning_source_magnitude_m2_s2_by_time) != output_count):
                raise ValueError("per-time Manning diagnostics must align with outputs")
            for h_row, hx_row, hy_row, expected_gamma, expected_source in zip(
                self.water_depth_m, self.momentum_x_m2_s, self.momentum_y_m2_s,
                self.maximum_manning_gamma_per_s_by_time,
                self.maximum_manning_source_magnitude_m2_s2_by_time,
            ):
                gamma_values = []
                source_values = []
                for h, hx, hy, n in zip(h_row, hx_row, hy_row, self.manning_n):
                    if h <= self.dry_depth_threshold_m:
                        gamma_values.append(0.0)
                        source_values.append(0.0)
                    else:
                        gamma = self.gravity_m_s2 * n * n * math.hypot(hx, hy) / h ** (7.0 / 3.0)
                        gamma_values.append(gamma)
                        source_values.append(gamma * math.hypot(hx, hy))
                if not math.isclose(expected_gamma, max(gamma_values, default=0.0), rel_tol=1e-12, abs_tol=1e-18):
                    raise ValueError("saved-time Manning gamma must match saved state, n, and depth")
                if not math.isclose(expected_source, max(source_values, default=0.0), rel_tol=1e-12, abs_tol=1e-18):
                    raise ValueError("saved-time Manning source magnitude must match saved state")
            if self.maximum_manning_gamma_per_s + 1e-18 < max(self.maximum_manning_gamma_per_s_by_time, default=0.0):
                raise ValueError("global maximum Manning gamma must include saved-time diagnostics")
            if self.maximum_manning_source_magnitude_m2_s2 + 1e-18 < max(self.maximum_manning_source_magnitude_m2_s2_by_time, default=0.0):
                raise ValueError("global maximum Manning source magnitude must include saved-time diagnostics")
            if (self.source_cfl_limit_evaluation_count == 0) != (
                self.minimum_source_cfl_limit_s is None and self.maximum_source_cfl_limit_s is None
            ):
                raise ValueError("source-CFL limit extrema must be present exactly when finite limits were evaluated")
            if self.source_cfl_limit_evaluation_count > 0:
                if self.minimum_source_cfl_limit_s is None or self.maximum_source_cfl_limit_s is None:
                    raise ValueError("source-CFL limit extrema are required when finite limits were evaluated")
                if self.minimum_source_cfl_limit_s > self.maximum_source_cfl_limit_s:
                    raise ValueError("minimum source-CFL limit may not exceed maximum source-CFL limit")
            if self.manning_source_cfl_limited_step_count > self.accepted_cfl_step_count:
                raise ValueError("Manning source-limited count may not exceed accepted CFL steps")
            source_rejections = (
                self.rejected_step_reasons.get("second_stage_source_cfl", 0)
                + self.rejected_step_reasons.get("source_cfl_alignment", 0)
            )
            if self.manning_source_cfl_rejected_step_count != source_rejections:
                raise ValueError("Manning source rejection count must match structured source-CFL reasons")
            if self.nonzero_manning_effect_applied != (
                self.manning_nonzero_face_stage_evaluation_count > 0
                and self.maximum_manning_source_magnitude_m2_s2 > 0.0
            ):
                raise ValueError("nonzero_manning_effect_applied must match accepted nonzero Manning stages")
            if self.nonzero_manning_effect_applied != (
                self.manning_nonzero_face_stage_evaluation_count > 0
                and self.maximum_manning_source_magnitude_m2_s2 > 0.0
            ):
                raise ValueError("nonzero_manning_effect_applied must reflect accepted nonzero Manning source evaluations")
        else:
            if (self.manning_applied or self.manning_source_unit is not None or self.manning_physical_unit is not None
                    or self.manning_interpretation_policy is not None or self.manning_interpretation_rationale is not None
                    or self.manning_face_id or self.manning_n or self.source_cfl_fraction is not None):
                raise ValueError("rainfall-only execution may not claim Manning inputs or application")
            if (self.maximum_manning_gamma_per_s != 0.0 or self.maximum_manning_source_magnitude_m2_s2 != 0.0
                    or any(self.maximum_manning_gamma_per_s_by_time)
                    or any(self.maximum_manning_source_magnitude_m2_s2_by_time)
                    or self.manning_nonzero_face_stage_evaluation_count != 0
                    or self.source_cfl_limit_evaluation_count != 0
                    or self.minimum_source_cfl_limit_s is not None or self.maximum_source_cfl_limit_s is not None
                    or self.manning_source_cfl_limited_step_count != 0 or self.manning_source_cfl_rejected_step_count != 0
                    or self.nonzero_manning_effect_applied or self.manning_source_unit is not None
                    or self.manning_physical_unit is not None or self.manning_interpretation_policy is not None
                    or self.manning_interpretation_rationale is not None or self.source_cfl_fraction is not None):
                raise ValueError("rainfall-only execution must not publish Manning diagnostics")

        if len(self.rainfall_schedule) == 0:
            raise ValueError("rainfall source results must retain the prepared interval schedule")
        next_index = 0
        expected_rates: list[float | None] = [None] * interval_count
        area = math.fsum(self.face_area_m2)
        for item in self.rainfall_schedule:
            if item.start_output_index != next_index or item.end_output_index <= item.start_output_index:
                raise ValueError("rainfall schedule output indices must be contiguous and cover the run")
            if item.end_output_index >= output_count:
                raise ValueError("rainfall schedule end_output_index must refer to an existing output time")
            if item.input_unit != self.rainfall_input_unit:
                raise ValueError("rainfall schedule units must match rainfall_input_unit")
            if item.input_end_time_s <= item.input_start_time_s:
                raise ValueError("raw rainfall interval must span positive input time")
            effective_start = self.time_s[item.start_output_index]
            effective_end = self.time_s[item.end_output_index]
            if item.effective_start_time_s != effective_start or item.effective_end_time_s != effective_end:
                raise ValueError("effective rainfall interval must use exact base output times")
            if item.input_unit == "mm/h":
                expected_rate = item.input_intensity / 3_600_000.0
            else:
                expected_rate = item.input_intensity
            if item.rainfall_rate_m_s != expected_rate:
                raise ValueError("rainfall rate must equal the independently converted input value")
            input_duration = math.fsum((item.input_end_time_s, -item.input_start_time_s))
            effective_duration = math.fsum((effective_end, -effective_start))
            if not math.isclose(item.input_interval_rainfall_depth_m, item.rainfall_rate_m_s * input_duration, rel_tol=1e-12, abs_tol=1e-15):
                raise ValueError("raw-interval rainfall depth must use the original input interval")
            if not math.isclose(item.input_interval_rainfall_volume_m3, item.input_interval_rainfall_depth_m * area, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("raw-interval rainfall volume must use linked face area")
            applied_depth = item.rainfall_rate_m_s * effective_duration
            applied_volume = applied_depth * area
            if not math.isclose(item.applied_interval_rainfall_depth_m, applied_depth, rel_tol=1e-12, abs_tol=1e-15):
                raise ValueError("applied rainfall depth must use effective output times")
            if not math.isclose(item.applied_interval_rainfall_volume_m3, applied_volume, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("applied rainfall volume must use effective output times and linked face area")
            for output_interval in range(item.start_output_index, item.end_output_index):
                if expected_rates[output_interval] is not None:
                    raise ValueError("rainfall schedules may not overlap output intervals")
                expected_rates[output_interval] = item.rainfall_rate_m_s
            next_index = item.end_output_index
        if next_index != interval_count or any(value is None for value in expected_rates):
            raise ValueError("rainfall schedule must cover every base output interval")
        if any(not math.isclose(actual, float(expected), rel_tol=0.0, abs_tol=0.0)
               for actual, expected in zip(self.rainfall_rate_m_s_by_interval, expected_rates)):
            raise ValueError("applied SI rainfall rates must match prepared interval/output mappings")
        for index, item in enumerate(self.rainfall_schedule):
            applied_volume = math.fsum(self.rainfall_volume_m3_by_interval[item.start_output_index:item.end_output_index])
            if not math.isclose(applied_volume, item.applied_interval_rainfall_volume_m3, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(f"accepted rainfall volume differs from prepared interval {index}")
        if not math.isclose(self.rainfall_total_depth_m * area, self.rainfall_total_volume_m3, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("rainfall total depth and volume must agree with linked total face area")
        if not math.isclose(self.rainfall_total_volume_m3, math.fsum(item.applied_interval_rainfall_volume_m3 for item in self.rainfall_schedule), rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("rainfall total volume must equal the effective prepared schedule integral")
        accounting = self.security_rainfall_accounting
        if self.security_exclusion is None:
            if accounting is not None:
                raise ValueError("security_rainfall_accounting requires linked security-exclusion evidence")
        else:
            if accounting is None:
                raise ValueError("security-enabled rainfall results must separately report excluded unmodeled rainfall")
            active_area = math.fsum(float(value) for value in self.face_area_m2)
            excluded_area = float(self.security_exclusion.security_inside_area_m2)
            if (not math.isclose(accounting.active_area_m2, active_area, rel_tol=1e-10, abs_tol=1e-8)
                    or not math.isclose(accounting.security_excluded_area_m2, excluded_area, rel_tol=1e-10, abs_tol=1e-8)):
                raise ValueError("security rainfall areas must match the bound active mesh and excluded domain area")
            if (len(accounting.accepted_active_rainfall_volume_m3_by_interval) != interval_count
                    or len(accounting.security_excluded_unmodeled_rainfall_volume_m3_by_interval) != interval_count):
                raise ValueError("security rainfall interval arrays must align with the output time axis")
            expected_active: list[float] = []
            expected_excluded: list[float] = []
            for index, (start, end, rate) in enumerate(zip(self.time_s, self.time_s[1:], self.rainfall_rate_m_s_by_interval)):
                duration = math.fsum((float(end), -float(start)))
                active_volume = float(rate) * duration * active_area
                excluded_volume = float(rate) * duration * excluded_area
                expected_active.append(active_volume)
                expected_excluded.append(excluded_volume)
                if not math.isclose(accounting.accepted_active_rainfall_volume_m3_by_interval[index], active_volume, rel_tol=1e-10, abs_tol=1e-12):
                    raise ValueError("accepted security rainfall must be rate times active area and output-interval duration")
                if not math.isclose(accounting.accepted_active_rainfall_volume_m3_by_interval[index], self.rainfall_volume_m3_by_interval[index], rel_tol=1e-10, abs_tol=1e-12):
                    raise ValueError("active security rainfall must match the source mass ledger")
                if not math.isclose(accounting.security_excluded_unmodeled_rainfall_volume_m3_by_interval[index], excluded_volume, rel_tol=1e-10, abs_tol=1e-12):
                    raise ValueError("excluded rainfall must be reported as unmodeled rate times excluded area and duration")
            if (not math.isclose(accounting.accepted_active_rainfall_volume_m3, math.fsum(expected_active), rel_tol=1e-10, abs_tol=1e-12)
                    or not math.isclose(accounting.security_excluded_unmodeled_rainfall_volume_m3, math.fsum(expected_excluded), rel_tol=1e-10, abs_tol=1e-12)):
                raise ValueError("security rainfall totals must equal their per-interval accounting")
        for alignment_step, cfl_limit in zip(self.subminimum_alignment_step_s, self.subminimum_alignment_cfl_limit_s):
            if alignment_step > 0.0 and (cfl_limit <= 0.0 or _source_cfl_bound_exceeded(alignment_step, cfl_limit)):
                raise ValueError("source-result alignment step may not exceed its recorded source-aware CFL limit")
        return self


@dataclass(frozen=True)
class TriangularSourceRunCapability:
    config_path: Path
    base_run_config_path: Path
    source_config_path: Path
    source_preparation_path: Path
    result_path: Path
    config: TriangularSourceRunConfig
    source_config: TriangularSourcePreparationConfig
    base_run: TriangularRunCapability
    preparation: TriangularSourcePreparationResult
    rainfall_rate_m_s_by_interval: tuple[float, ...]
    applied_rainfall_schedule: tuple[AppliedRainfallInterval, ...]
    report: Mapping[str, Any]


def load_triangular_source_run_config(path: str | Path) -> TriangularSourceRunConfig:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TriangularSourceContractError(f"source run config not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise TriangularSourceContractError(f"invalid source run YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TriangularSourceContractError("source run config root must be a YAML mapping")
    try:
        return TriangularSourceRunConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}" for issue in exc.errors())
        raise TriangularSourceContractError(f"invalid triangular source run config: {details}") from exc


def _resolve_relative(base: Path, relative: str, label: str) -> Path:
    if Path(relative).is_absolute() or PureWindowsPath(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise TriangularSourceContractError(f"{label} must be relative to the source run config")
    return (base / relative).resolve()


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise TriangularSourceContractError(f"cannot hash linked source artifact '{path}': {exc}") from exc


def _applied_rainfall_schedule(
    preparation: TriangularSourcePreparationResult,
    output_times_s: tuple[float, ...],
    face_areas_m2: tuple[float, ...],
) -> tuple[AppliedRainfallInterval, ...]:
    total_area = math.fsum(face_areas_m2)
    schedule: list[AppliedRainfallInterval] = []
    for item in preparation.rainfall.intervals:
        effective_start = output_times_s[item.start_output_index]
        effective_end = output_times_s[item.end_output_index]
        effective_duration = math.fsum((effective_end, -effective_start))
        applied_depth = float(item.rainfall_rate_m_s) * effective_duration
        applied_volume = applied_depth * total_area
        schedule.append(AppliedRainfallInterval(
            input_start_time_s=item.start_time_s,
            input_end_time_s=item.end_time_s,
            effective_start_time_s=effective_start,
            effective_end_time_s=effective_end,
            start_output_index=item.start_output_index,
            end_output_index=item.end_output_index,
            input_intensity=item.input_intensity,
            input_unit=item.input_unit,
            rainfall_rate_m_s=item.rainfall_rate_m_s,
            input_interval_rainfall_depth_m=item.rainfall_depth_m,
            input_interval_rainfall_volume_m3=item.rainfall_volume_m3,
            applied_interval_rainfall_depth_m=applied_depth,
            applied_interval_rainfall_volume_m3=applied_volume,
        ))
    return tuple(schedule)


def validate_triangular_source_run_request(
    path: str | Path,
    *,
    allow_existing_result: bool = False,
) -> TriangularSourceRunCapability:
    config_path = Path(path).resolve()
    config = load_triangular_source_run_config(config_path)
    base_path = _resolve_relative(config_path.parent, config.base_run_config_file, "base_run_config_file")
    source_path = _resolve_relative(config_path.parent, config.source_config_file, "source_config_file")
    lexical_result = config_path.parent / config.result_file
    if lexical_result.is_symlink():
        raise TriangularSourceContractError("source result_file must not be a symbolic link")
    result_path = _resolve_relative(config_path.parent, config.result_file, "result_file")
    try:
        source_config = load_triangular_source_preparation_config(source_path)
        linked_base = _resolve_relative(source_path.parent, source_config.run_config_file, "source run_config_file")
        if linked_base != base_path:
            raise TriangularSourceContractError("source-preparation run_config_file must resolve to the same base run config")
        if not source_config.rainfall.enabled:
            raise TriangularSourceContractError("P4-2C execution requires rainfall enabled in source preparation")
        base_run = validate_triangular_run_request(base_path, allow_existing_result=True)
        if base_run.config.sources != "none":
            raise TriangularSourceContractError("base_run_config_file must retain sources=none and is not executed")
        preparation_path = _resolve_relative(source_path.parent, source_config.output_file, "source-preparation output_file")
        preparation = read_triangular_source_preparation(preparation_path, source_path)
    except TriangularSourceContractError:
        raise
    except (TriangularSolverContractError, TriangularSourceContractError, OSError, ValueError) as exc:
        raise TriangularSourceContractError(f"linked base/source-preparation inputs are invalid: {exc}") from exc
    if config.case_id != base_run.config.case_id or config.case_id != source_config.case_id:
        raise TriangularSourceContractError("source run, base run and source-preparation case_id values must match")
    if preparation.sources_applied != "none" or preparation.manning.applied or preparation.rainfall.applied or preparation.execution_available:
        raise TriangularSourceContractError("source preparation must remain preparation-only and unapplied")
    if not preparation.rainfall.requested_enabled or not preparation.rainfall.intervals:
        raise TriangularSourceContractError("source run requires a complete prepared rainfall schedule")
    if config.sources == "rainfall":
        if source_config.manning.enabled or preparation.manning.requested_enabled:
            raise TriangularSourceContractError("sources: rainfall requires Manning disabled in source preparation")
    elif not source_config.manning.enabled or not preparation.manning.requested_enabled:
        raise TriangularSourceContractError("sources: manning_and_rainfall requires Manning enabled in source preparation")
    if preparation.rainfall.input_unit != source_config.rainfall.unit:
        raise TriangularSourceContractError("prepared rainfall unit does not match source preparation config")
    if preparation.rainfall.total_rain_volume_m3 < 0.0:
        raise TriangularSourceContractError("prepared rainfall volume must be non-negative")
    if not math.isclose(preparation.volume_correction_budget_m3, base_run.report["volume_correction_budget_m3"], rel_tol=1e-12, abs_tol=1e-15):
        raise TriangularSourceContractError("source-preparation correction budget differs from the base initial-state budget")

    rate_by_interval: list[float | None] = [None] * (len(base_run.config.output_times_s) - 1)
    for interval in preparation.rainfall.intervals:
        for output_interval in range(interval.start_output_index, interval.end_output_index):
            if rate_by_interval[output_interval] is not None:
                raise TriangularSourceContractError("prepared rainfall intervals overlap an output interval")
            rate_by_interval[output_interval] = float(interval.rainfall_rate_m_s)
    if any(rate is None for rate in rate_by_interval):
        raise TriangularSourceContractError("prepared rainfall intervals must cover every base output interval")
    output_times = tuple(float(value) for value in base_run.config.output_times_s)
    schedule = _applied_rainfall_schedule(preparation, output_times, tuple(float(value) for value in base_run.business.mesh.face_area))

    protected = {
        config_path, base_path, source_path, preparation_path,
        base_run.result_path, base_run.business_path, base_run.mesh_path,
    }
    if result_path in protected:
        raise TriangularSourceContractError("source result_file must not overwrite a config, base result, preparation artifact, P2 mesh or P3-A package")
    if result_path.exists() and not result_path.is_file():
        raise TriangularSourceContractError("source result_file must be a regular file path")
    if result_path.exists() and not config.overwrite_existing_result and not allow_existing_result:
        raise TriangularSourceContractError(f"source result_file already exists; set overwrite_existing_result: true to replace it: {result_path}")
    if result_path.exists():
        try:
            if any(item.exists() and os.path.samefile(result_path, item) for item in protected):
                raise TriangularSourceContractError("source result_file must not be a hard link to a protected input or result")
        except OSError as exc:
            raise TriangularSourceContractError(f"cannot safely inspect source result_file identity: {exc}") from exc
    report = {
        "status": "ready_for_experimental_source_run",
        "preflight_only": True,
        "execution_available": True,
        "solver_completed": False,
        "experimental": True,
        "production_approved": False,
        "case_id": config.case_id,
        "base_sources_applied": "none",
        "requested_sources": config.sources,
        "manning_enabled": config.sources == "manning_and_rainfall",
        "manning_applied": False,
        "rainfall_enabled": True,
        "rainfall_applied": False,
        "rainfall_unit": preparation.rainfall.input_unit,
        "prepared_rainfall_volume_m3": preparation.rainfall.total_rain_volume_m3,
        "volume_correction_reference_scale_m3": base_run.report["volume_correction_reference_scale_m3"],
        "volume_correction_budget_m3": base_run.report["volume_correction_budget_m3"],
    }
    return TriangularSourceRunCapability(
        config_path, base_path, source_path, preparation_path, result_path,
        config, source_config, base_run, preparation,
        tuple(float(rate) for rate in rate_by_interval), schedule, report,
    )


def validate_triangular_source_result_for_run(
    result: TriangularSourceResultContract | Mapping[str, Any],
    source_run_config_path: str | Path,
    *,
    allow_overwrite_flag_change: bool = False,
) -> TriangularSourceResultContract:
    capability = validate_triangular_source_run_request(source_run_config_path, allow_existing_result=True)
    try:
        data = result.model_dump(mode="python") if isinstance(result, TriangularSourceResultContract) else result
        contract = TriangularSourceResultContract.model_validate(data)
    except ValidationError as exc:
        raise TriangularSourceContractError(f"source result contract is invalid: {exc}") from exc
    try:
        validate_triangular_result_for_run(
            contract,
            capability.base_run_config_path,
            result_model=TriangularSourceResultContract,
            expected_sources=capability.config.sources,
            artifact_result_path=capability.result_path,
        )
    except (ValidationError, TriangularSolverContractError) as exc:
        raise TriangularSourceContractError(f"embedded source result is not bound to current P2/P3-A inputs: {exc}") from exc

    expected_refs = {
        "base_run_config_artifact": capability.base_run_config_path,
        "source_run_config_artifact": capability.config_path,
        "source_config_artifact": capability.source_config_path,
        "source_preparation_artifact": capability.source_preparation_path,
    }
    for name, source in expected_refs.items():
        expected = Path(os.path.relpath(source, capability.result_path.parent)).as_posix()
        if getattr(contract, name) != expected:
            raise TriangularSourceContractError(f"source result {name} does not match configured artifact")
    expected_digests = {
        "source_run_config_sha256": _sha256(capability.config_path),
        "source_config_sha256": _sha256(capability.source_config_path),
        "source_preparation_sha256": _sha256(capability.source_preparation_path),
    }
    for name, expected in expected_digests.items():
        if name == "source_run_config_sha256" and allow_overwrite_flag_change:
            continue
        if getattr(contract, name) != expected:
            raise TriangularSourceContractError(f"source result {name} does not match current input contents")
    if contract.case_id != capability.config.case_id:
        raise TriangularSourceContractError("source result case_id does not match source run config")
    if contract.sources_applied != capability.config.sources:
        raise TriangularSourceContractError("source result applied mode does not match the versioned source run config")
    if contract.sources_applied == "manning_and_rainfall":
        if contract.source_cfl_fraction != capability.source_config.source_cfl_fraction:
            raise TriangularSourceContractError("source result source_cfl_fraction does not match the current preparation config")
        prepared = capability.preparation.manning
        if (tuple(contract.manning_face_id) != tuple(prepared.face_id)
                or tuple(contract.manning_n) != tuple(float(value) for value in prepared.interpreted_values)):
            raise TriangularSourceContractError("source result Manning values do not match current P3-A preparation")
        if contract.manning_interpretation_rationale != prepared.interpretation_rationale:
            raise TriangularSourceContractError("source result Manning interpretation does not match preparation")
    if not math.isclose(contract.volume_correction_reference_scale_m3, capability.base_run.report["volume_correction_reference_scale_m3"], rel_tol=1e-12, abs_tol=1e-12):
        raise TriangularSourceContractError("source result correction reference scale must stay bound to initial state")
    if not math.isclose(contract.volume_correction_budget_m3, capability.base_run.report["volume_correction_budget_m3"], rel_tol=1e-12, abs_tol=1e-15):
        raise TriangularSourceContractError("source result correction budget may not expand with rainfall")
    if tuple(contract.rainfall_rate_m_s_by_interval) != capability.rainfall_rate_m_s_by_interval:
        raise TriangularSourceContractError("source result SI rainfall rates do not match matched preparation intervals")
    if [item.model_dump(mode="json") for item in contract.rainfall_schedule] != [item.model_dump(mode="json") for item in capability.applied_rainfall_schedule]:
        raise TriangularSourceContractError("source result raw/effective rainfall schedule does not match preparation and base output times")
    return contract


__all__ = [
    "AppliedRainfallInterval", "SecurityRainfallAccounting", "SOURCE_RESULT_FORMAT", "SOURCE_RESULT_PRODUCT", "SOURCE_SOLVER_VERSION",
    "TriangularSourceContractError", "TriangularSourceMassBudget", "TriangularSourceResultContract",
    "TriangularSourceRunCapability", "TriangularSourceRunConfig", "load_triangular_source_run_config",
    "validate_triangular_source_result_for_run", "validate_triangular_source_run_request",
]
