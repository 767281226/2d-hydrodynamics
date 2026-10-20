"""Preparation-only source contracts for the future triangular solver routes.

This module validates and adapts source inputs against real P2/P3-A artifacts.
It does not modify the P3-A roughness package, advance a state, or apply a
source term. P4-1 continues to execute only ``sources=none``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import tempfile
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator
import yaml

from .config import Number, Text
from .triangular_solver_contract import (
    TriangularRunCapability,
    TriangularSolverContractError,
    _step_tolerance_s,
    validate_triangular_run_request,
)


class TriangularSourceContractError(ValueError):
    """Raised when a source request or preparation artifact is invalid."""


class _SourceStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, allow_inf_nan=False)


class ManningSourceRequest(_SourceStrictModel):
    """Request to prepare P3-A roughness for a future SI Manning route."""

    enabled: StrictBool = False
    legacy_unit1_policy: Literal["legacy_unit1_as_si_manning"] | None = None
    interpretation_rationale: Text | None = None

    @model_validator(mode="after")
    def policy_matches_enabled_state(self) -> "ManningSourceRequest":
        if self.enabled:
            if self.legacy_unit1_policy != "legacy_unit1_as_si_manning":
                raise ValueError("enabled Manning requires legacy_unit1_policy: legacy_unit1_as_si_manning")
            if self.interpretation_rationale is None or not self.interpretation_rationale.strip():
                raise ValueError("enabled Manning requires a non-empty interpretation_rationale")
        elif self.legacy_unit1_policy is not None or self.interpretation_rationale is not None:
            raise ValueError("disabled Manning must not carry a legacy-unit interpretation or rationale")
        return self


class RainfallIntervalRequest(_SourceStrictModel):
    """One explicit constant-intensity interval; intensity unit is on its parent."""

    start_time_s: Number = Field(ge=0.0, le=31_536_000.0, description="Left boundary, seconds relative to the run start")
    end_time_s: Number = Field(gt=0.0, le=31_536_000.0, description="Right boundary, seconds relative to the run start")
    intensity: Number = Field(ge=0.0, description="Finite non-negative uniform rainfall intensity in the parent unit")

    @model_validator(mode="after")
    def positive_interval(self) -> "RainfallIntervalRequest":
        if self.end_time_s <= self.start_time_s:
            raise ValueError("rainfall interval end_time_s must be greater than start_time_s")
        return self


class RainfallSourceRequest(_SourceStrictModel):
    """Uniform rainfall request as explicit, contiguous constant intervals."""

    enabled: StrictBool = False
    unit: Literal["mm/h", "m/s"] | None = None
    intervals: list[RainfallIntervalRequest] | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def unit_and_payload_match_enabled_state(self) -> "RainfallSourceRequest":
        if self.enabled:
            if self.unit is None:
                raise ValueError("enabled rainfall requires unit: mm/h or m/s")
            if not self.intervals:
                raise ValueError("enabled rainfall requires at least one explicit interval")
        elif self.unit is not None or self.intervals is not None:
            raise ValueError("disabled rainfall must not carry a unit or interval payload")
        return self


class TriangularSourcePreparationConfig(_SourceStrictModel):
    """Versioned preparation request, independent of a solver run config."""

    format: Literal["triangular-source-preparation-v1"] = "triangular-source-preparation-v1"
    schema_version: Literal["1.0"] = "1.0"
    case_id: Text
    run_config_file: Text
    output_file: Text
    source_cfl_fraction: Number = Field(default=0.5, gt=0.0, le=1.0, description="Future explicit source-step fraction, dimensionless")
    manning: ManningSourceRequest = Field(default_factory=ManningSourceRequest)
    rainfall: RainfallSourceRequest = Field(default_factory=RainfallSourceRequest)
    overwrite_existing_output: StrictBool = False

    @field_validator("run_config_file", "output_file")
    @classmethod
    def relative_paths(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("path must be relative to the source-preparation config")
        return value

    @model_validator(mode="after")
    def distinct_artifact_paths(self) -> "TriangularSourcePreparationConfig":
        if self.run_config_file == self.output_file:
            raise ValueError("output_file must be distinct from run_config_file and must not overwrite the run config")
        return self


class PreparedManningField(_SourceStrictModel):
    requested_enabled: StrictBool
    applied: Literal[False] = False
    source_quantity: Literal["manning_n"] = "manning_n"
    source_unit: Literal["1"] = "1"
    physical_unit: Literal["s/m^(1/3)"] | None
    legacy_unit1_policy: Literal["legacy_unit1_as_si_manning"] | None
    interpretation_rationale: Text | None
    numeric_values_changed: Literal[False] = False
    source: Text
    assignment_mode: Literal["constant", "face_values"]
    face_id: list[StrictInt] = Field(min_length=1)
    source_values: list[Number | None] = Field(min_length=1)
    valid_mask: list[StrictBool] = Field(min_length=1)
    interpreted_values: list[Number | None] = Field(min_length=1)

    @model_validator(mode="after")
    def source_field_is_preserved(self) -> "PreparedManningField":
        count = len(self.face_id)
        if any(len(values) != count for values in (self.source_values, self.valid_mask, self.interpreted_values)):
            raise ValueError("prepared Manning IDs, values, masks and interpreted values must have identical face counts")
        if len(set(self.face_id)) != count or any(value < 0 or value > (1 << 63) - 1 for value in self.face_id):
            raise ValueError("prepared Manning face_id must contain unique non-negative int64 identifiers")
        for index, (value, valid, interpreted) in enumerate(zip(self.source_values, self.valid_mask, self.interpreted_values)):
            if valid != (value is not None):
                raise ValueError(f"prepared Manning value/mask mismatch at face index {index}")
            if self.requested_enabled:
                if not valid or interpreted is None:
                    raise ValueError("enabled Manning preparation requires a complete interpreted value on every face")
                if not 0.0 < value <= 1.0:
                    raise ValueError(f"P3-A Manning value at face index {index} must satisfy 0 < n <= 1")
                if interpreted != value:
                    raise ValueError("legacy unit-1 interpretation must preserve the source numeric value exactly")
            elif interpreted is not None:
                raise ValueError("disabled Manning must not publish interpreted SI values")
        if self.requested_enabled:
            if self.physical_unit != "s/m^(1/3)" or self.legacy_unit1_policy != "legacy_unit1_as_si_manning":
                raise ValueError("enabled Manning must identify the legacy interpretation and SI physical unit")
            if self.interpretation_rationale is None or not self.interpretation_rationale.strip():
                raise ValueError("enabled Manning preparation requires its non-empty interpretation rationale")
        elif self.physical_unit is not None or self.legacy_unit1_policy is not None or self.interpretation_rationale is not None:
            raise ValueError("disabled Manning may not claim an SI interpretation")
        return self


class PreparedRainfallInterval(_SourceStrictModel):
    start_time_s: Number = Field(ge=0.0, le=31_536_000.0)
    end_time_s: Number = Field(gt=0.0, le=31_536_000.0)
    start_output_index: StrictInt = Field(ge=0)
    end_output_index: StrictInt = Field(ge=1)
    input_intensity: Number = Field(ge=0.0)
    input_unit: Literal["mm/h", "m/s"]
    rainfall_rate_m_s: Number = Field(ge=0.0)
    rainfall_depth_m: Number = Field(ge=0.0)
    rainfall_volume_m3: Number = Field(ge=0.0)

    @model_validator(mode="after")
    def positive_output_span(self) -> "PreparedRainfallInterval":
        if self.end_time_s <= self.start_time_s or self.end_output_index <= self.start_output_index:
            raise ValueError("prepared rainfall interval must span positive time and at least one output interval")
        return self


class PreparedRainfallField(_SourceStrictModel):
    requested_enabled: StrictBool
    applied: Literal[False] = False
    spatial_policy: Literal["uniform_over_all_faces"] = "uniform_over_all_faces"
    boundary_convention: Literal["left_closed_right_open_final_endpoint_terminal"] = "left_closed_right_open_final_endpoint_terminal"
    input_unit: Literal["mm/h", "m/s"] | None
    intervals: list[PreparedRainfallInterval] = Field(default_factory=list)
    total_rain_depth_m: Number = Field(ge=0.0)
    total_rain_volume_m3: Number = Field(ge=0.0)

    @model_validator(mode="after")
    def enabled_state_matches_intervals(self) -> "PreparedRainfallField":
        if self.requested_enabled:
            if self.input_unit is None or not self.intervals:
                raise ValueError("enabled prepared rainfall requires a unit and intervals")
            if any(interval.input_unit != self.input_unit for interval in self.intervals):
                raise ValueError("all prepared rainfall intervals must retain the request's single input unit")
            if not math.isclose(self.total_rain_depth_m, math.fsum(item.rainfall_depth_m for item in self.intervals), rel_tol=1e-12, abs_tol=1e-15):
                raise ValueError("total_rain_depth_m must equal the independently summed interval depths")
            if not math.isclose(self.total_rain_volume_m3, math.fsum(item.rainfall_volume_m3 for item in self.intervals), rel_tol=1e-12, abs_tol=1e-15):
                raise ValueError("total_rain_volume_m3 must equal the independently summed interval volumes")
        elif self.input_unit is not None or self.intervals or self.total_rain_depth_m != 0.0 or self.total_rain_volume_m3 != 0.0:
            raise ValueError("disabled rainfall must have no unit, intervals or derived rain totals")
        return self


class TriangularSourcePreparationResult(_SourceStrictModel):
    format: Literal["triangular-source-preparation-result-v1"]
    product_type: Literal["triangular_source_preparation"]
    preparation_only: Literal[True]
    simulation_result: Literal[False]
    solver_completed: Literal[False]
    production_approved: Literal[False]
    sources_applied: Literal["none"]
    manning_applied: Literal[False]
    rainfall_applied: Literal[False]
    status: Literal["prepared_no_source_request", "prepared_not_executable"]
    execution_available: StrictBool
    requested_sources: list[Literal["manning", "rainfall"]]
    case_id: Text
    time_origin: Text | None
    coordinate_system: Text
    mesh_id: Text
    node_xy_m: list[list[Number]] = Field(min_length=3)
    face_nodes: list[list[StrictInt]] = Field(min_length=1)
    face_id: list[StrictInt] = Field(min_length=1)
    face_area_m2: list[Number] = Field(min_length=1)
    output_times_s: list[Number] = Field(min_length=2)
    topology_fingerprint: Text
    terrain_fingerprint: Text
    source_config_sha256: Text
    run_config_sha256: Text
    business_package_sha256: Text
    mesh_sha256: Text
    source_config_artifact: Text
    run_config_artifact: Text
    business_package_artifact: Text
    mesh_artifact: Text
    source_cfl_fraction: Number = Field(gt=0.0, le=1.0)
    initial_water_volume_m3: Number = Field(ge=0.0)
    volume_correction_reference_scale_m3: Number = Field(gt=0.0)
    volume_correction_budget_m3: Number = Field(ge=0.0)
    manning: PreparedManningField
    rainfall: PreparedRainfallField

    @model_validator(mode="after")
    def prep_only_flags_and_requests(self) -> "TriangularSourcePreparationResult":
        expected = []
        if self.manning.requested_enabled:
            expected.append("manning")
        if self.rainfall.requested_enabled:
            expected.append("rainfall")
        if self.requested_sources != expected:
            raise ValueError("requested_sources must match the enabled preparation sections")
        if bool(expected):
            if self.status != "prepared_not_executable" or self.execution_available:
                raise ValueError("requested source preparation must remain unavailable for P4-1 execution")
        elif self.status != "prepared_no_source_request" or not self.execution_available:
            raise ValueError("sources=none preparation must reflect the existing P4-1 capability")
        if len(self.face_id) != len(self.face_area_m2) or len(self.face_nodes) != len(self.face_id):
            raise ValueError("prepared face IDs, connectivity and areas must have matching face counts")
        if len(set(self.face_id)) != len(self.face_id) or any(face < 0 or face > (1 << 63) - 1 for face in self.face_id):
            raise ValueError("prepared face_id must contain unique non-negative int64 identifiers")
        if any(len(face) != 3 for face in self.face_nodes) or any(len(point) != 2 for point in self.node_xy_m):
            raise ValueError("prepared topology must retain triangular faces and 2D projected node coordinates")
        if any(node < 0 or node >= len(self.node_xy_m) for face in self.face_nodes for node in face):
            raise ValueError("prepared face_nodes contains an index outside node_xy_m")
        if any(area <= 0.0 for area in self.face_area_m2):
            raise ValueError("prepared face_area_m2 values must be positive")
        if self.output_times_s[0] != 0.0 or any(right <= left for left, right in zip(self.output_times_s, self.output_times_s[1:])):
            raise ValueError("prepared output_times_s must retain a strictly increasing axis beginning at 0 s")
        if tuple(self.manning.face_id) != tuple(self.face_id):
            raise ValueError("prepared Manning face_id order must match the linked P2 mesh")
        if self.rainfall.requested_enabled:
            intervals = self.rainfall.intervals
            if intervals[0].start_output_index != 0 or intervals[-1].end_output_index != len(self.output_times_s) - 1:
                raise ValueError("prepared rainfall must cover the complete run output-time axis")
            for previous, current in zip(intervals, intervals[1:]):
                if previous.end_output_index != current.start_output_index:
                    raise ValueError("prepared rainfall output-index intervals must be contiguous without gaps or overlaps")
            for interval in intervals:
                if interval.end_output_index >= len(self.output_times_s):
                    raise ValueError("prepared rainfall output index is outside output_times_s")
                if abs(interval.start_time_s - self.output_times_s[interval.start_output_index]) > _step_tolerance_s(max(abs(interval.start_time_s), abs(self.output_times_s[interval.start_output_index]))):
                    raise ValueError("prepared rainfall start boundary no longer matches its output index")
                if abs(interval.end_time_s - self.output_times_s[interval.end_output_index]) > _step_tolerance_s(max(abs(interval.end_time_s), abs(self.output_times_s[interval.end_output_index]))):
                    raise ValueError("prepared rainfall end boundary no longer matches its output index")
        for name in ("source_config_sha256", "run_config_sha256", "business_package_sha256", "mesh_sha256", "topology_fingerprint", "terrain_fingerprint"):
            digest = getattr(self, name)
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        return self


@dataclass(frozen=True)
class _SourcePreparationCapability:
    config_path: Path
    run_config_path: Path
    output_path: Path
    config: TriangularSourcePreparationConfig
    run_capability: TriangularRunCapability


def _resolve_relative(base: Path, relative: str, label: str) -> Path:
    if Path(relative).is_absolute() or PureWindowsPath(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise TriangularSourceContractError(f"{label} must be relative to the source-preparation config")
    return (base / relative).resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_triangular_source_preparation_config(path: str | Path) -> TriangularSourcePreparationConfig:
    source = Path(path).resolve()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TriangularSourceContractError(f"source-preparation config not found: {source}") from exc
    except yaml.YAMLError as exc:
        raise TriangularSourceContractError(f"invalid source-preparation YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TriangularSourceContractError("source-preparation config root must be a YAML mapping")
    try:
        return TriangularSourcePreparationConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}" for issue in exc.errors())
        raise TriangularSourceContractError(f"invalid triangular source-preparation config: {details}") from exc


def _load_capability(path: str | Path, *, allow_existing_output: bool = False) -> _SourcePreparationCapability:
    config_path = Path(path).resolve()
    config = load_triangular_source_preparation_config(config_path)
    run_config_path = _resolve_relative(config_path.parent, config.run_config_file, "run_config_file")
    lexical_output = config_path.parent / config.output_file
    if lexical_output.is_symlink():
        raise TriangularSourceContractError("source-preparation output_file must not be a symbolic link")
    output_path = _resolve_relative(config_path.parent, config.output_file, "output_file")
    try:
        run_capability = validate_triangular_run_request(run_config_path, allow_existing_result=True)
    except TriangularSolverContractError as exc:
        raise TriangularSourceContractError(f"linked P4-1 run request is invalid: {exc}") from exc
    if config.case_id != run_capability.config.case_id:
        raise TriangularSourceContractError("source-preparation case_id must match linked run config case_id")
    protected = {
        config_path, run_config_path, run_capability.business_path,
        run_capability.mesh_path, run_capability.result_path,
    }
    if output_path in protected:
        raise TriangularSourceContractError("output_file must not overwrite the source config, run config, P3-A package, P2 mesh or run result")
    if output_path.exists() and not output_path.is_file():
        raise TriangularSourceContractError("source-preparation output_file must be a regular file path")
    if output_path.exists() and not config.overwrite_existing_output and not allow_existing_output:
        raise TriangularSourceContractError(f"source-preparation output_file already exists: {output_path}")
    if output_path.exists():
        try:
            if any(item.exists() and os.path.samefile(output_path, item) for item in protected):
                raise TriangularSourceContractError("output_file must not be a hard link to a protected input/result")
        except OSError as exc:
            raise TriangularSourceContractError(f"cannot safely inspect existing source-preparation output identity: {exc}") from exc
    return _SourcePreparationCapability(config_path, run_config_path, output_path, config, run_capability)


def _matching_output_index(time_s: float, output_times_s: Sequence[float], label: str) -> int:
    matches = [
        index for index, output_time in enumerate(output_times_s)
        if abs(float(time_s) - float(output_time)) <= _step_tolerance_s(max(abs(float(time_s)), abs(float(output_time))))
    ]
    if len(matches) != 1:
        if not matches:
            raise TriangularSourceContractError(f"{label}={time_s:.17g} s must match exactly one requested output time within the shared tolerance")
        raise TriangularSourceContractError(f"{label}={time_s:.17g} s ambiguously matches multiple requested output times")
    return matches[0]


def _prepared_manning(capability: _SourcePreparationCapability) -> PreparedManningField:
    roughness = capability.run_capability.business.roughness
    mesh = capability.run_capability.business.mesh
    roughness.validate_for(mesh)
    if roughness.unit != "1":
        raise TriangularSourceContractError("P3-A roughness must retain its original unit=1 convention")
    if tuple(roughness.face_id) != tuple(mesh.face_id):
        raise TriangularSourceContractError("P3-A roughness face_id order does not match the linked P2 mesh")
    if capability.config.manning.enabled:
        missing = [str(face_id) for face_id, valid in zip(roughness.face_id, roughness.valid_mask) if not valid]
        if missing:
            raise TriangularSourceContractError(f"Manning preparation requires roughness on every face; invalid face_id values include {missing[:8]}")
        for index, value in enumerate(roughness.manning_n):
            if value is None or not math.isfinite(float(value)) or not 0.0 < float(value) <= 1.0:
                raise TriangularSourceContractError(f"P3-A manning_n at face_id {roughness.face_id[index]} must be finite and satisfy 0 < n <= 1")
    interpreted = list(roughness.manning_n) if capability.config.manning.enabled else [None] * mesh.n_face
    return PreparedManningField(
        requested_enabled=capability.config.manning.enabled,
        applied=False,
        source_unit="1",
        physical_unit="s/m^(1/3)" if capability.config.manning.enabled else None,
        legacy_unit1_policy=capability.config.manning.legacy_unit1_policy,
        interpretation_rationale=capability.config.manning.interpretation_rationale,
        numeric_values_changed=False,
        source=roughness.source,
        assignment_mode=roughness.assignment_mode,
        face_id=list(roughness.face_id),
        source_values=list(roughness.manning_n),
        valid_mask=list(roughness.valid_mask),
        interpreted_values=interpreted,
    )


def _prepared_rainfall(capability: _SourcePreparationCapability) -> PreparedRainfallField:
    request = capability.config.rainfall
    if not request.enabled:
        return PreparedRainfallField(
            requested_enabled=False, applied=False, input_unit=None, intervals=[],
            total_rain_depth_m=0.0, total_rain_volume_m3=0.0,
        )
    assert request.unit is not None and request.intervals is not None
    run_times = tuple(float(value) for value in capability.run_capability.config.output_times_s)
    total_time = run_times[-1]
    total_area = math.fsum(capability.run_capability.business.mesh.face_area)
    prepared: list[PreparedRainfallInterval] = []
    previous_end_index: int | None = None
    previous_end_time: float | None = None
    for index, interval in enumerate(request.intervals):
        start = float(interval.start_time_s)
        end = float(interval.end_time_s)
        if end > total_time + _step_tolerance_s(max(abs(end), abs(total_time))):
            raise TriangularSourceContractError(f"rainfall intervals[{index}].end_time_s exceeds run duration {total_time:g} s")
        start_index = _matching_output_index(start, run_times, f"rainfall intervals[{index}].start_time_s")
        end_index = _matching_output_index(end, run_times, f"rainfall intervals[{index}].end_time_s")
        if end_index <= start_index:
            raise TriangularSourceContractError(f"rainfall intervals[{index}] must span at least one requested output interval")
        if index == 0:
            if start_index != 0 or abs(start) > _step_tolerance_s(abs(start)):
                raise TriangularSourceContractError("rainfall schedule must explicitly cover the run start at 0 s")
        else:
            tolerance = _step_tolerance_s(max(abs(start), abs(float(previous_end_time))))
            if start_index != previous_end_index or abs(start - float(previous_end_time)) > tolerance:
                relation = "gap" if start_index > int(previous_end_index) or start > float(previous_end_time) else "overlap"
                raise TriangularSourceContractError(f"rainfall intervals contain a {relation} before intervals[{index}]")
        if request.unit == "mm/h":
            rate = float(interval.intensity) / 3_600_000.0
        else:
            rate = float(interval.intensity)
        duration = math.fsum((end, -start))
        rain_depth = rate * duration
        rain_volume = rain_depth * total_area
        if not all(math.isfinite(value) for value in (rate, duration, rain_depth, rain_volume)):
            raise TriangularSourceContractError(f"rainfall intervals[{index}] produces a non-finite converted rate, depth or volume")
        prepared.append(PreparedRainfallInterval(
            start_time_s=start,
            end_time_s=end,
            start_output_index=start_index,
            end_output_index=end_index,
            input_intensity=float(interval.intensity),
            input_unit=request.unit,
            rainfall_rate_m_s=rate,
            rainfall_depth_m=rain_depth,
            rainfall_volume_m3=rain_volume,
        ))
        previous_end_time = end
        previous_end_index = end_index
    if prepared[0].start_output_index != 0:
        raise TriangularSourceContractError("rainfall schedule must start at output_times_s[0]")
    if prepared[-1].end_output_index != len(run_times) - 1 or abs(prepared[-1].end_time_s - total_time) > _step_tolerance_s(max(abs(prepared[-1].end_time_s), abs(total_time))):
        raise TriangularSourceContractError("rainfall intervals must explicitly cover the final run time without extrapolation")
    try:
        total_depth = math.fsum(item.rainfall_depth_m for item in prepared)
        total_volume = math.fsum(item.rainfall_volume_m3 for item in prepared)
    except OverflowError as exc:
        raise TriangularSourceContractError("rainfall interval totals overflow finite numeric range") from exc
    if not math.isfinite(total_depth) or not math.isfinite(total_volume):
        raise TriangularSourceContractError("rainfall interval totals overflow finite numeric range")
    return PreparedRainfallField(
        requested_enabled=True,
        applied=False,
        input_unit=request.unit,
        intervals=prepared,
        total_rain_depth_m=total_depth,
        total_rain_volume_m3=total_volume,
    )


def _build_preparation_result(capability: _SourcePreparationCapability) -> TriangularSourcePreparationResult:
    run_capability = capability.run_capability
    mesh = run_capability.business.mesh
    requested = []
    if capability.config.manning.enabled:
        requested.append("manning")
    if capability.config.rainfall.enabled:
        requested.append("rainfall")
    result_path = capability.output_path
    refs = {
        "source_config_artifact": Path(os.path.relpath(capability.config_path, result_path.parent)).as_posix(),
        "run_config_artifact": Path(os.path.relpath(capability.run_config_path, result_path.parent)).as_posix(),
        "business_package_artifact": Path(os.path.relpath(run_capability.business_path, result_path.parent)).as_posix(),
        "mesh_artifact": Path(os.path.relpath(run_capability.mesh_path, result_path.parent)).as_posix(),
    }
    return TriangularSourcePreparationResult(
        format="triangular-source-preparation-result-v1",
        product_type="triangular_source_preparation",
        preparation_only=True,
        simulation_result=False,
        solver_completed=False,
        production_approved=False,
        sources_applied="none",
        manning_applied=False,
        rainfall_applied=False,
        status="prepared_not_executable" if requested else "prepared_no_source_request",
        execution_available=not requested,
        requested_sources=requested,
        case_id=capability.config.case_id,
        time_origin=run_capability.config.time_origin,
        coordinate_system=mesh.coordinate_system,
        mesh_id=mesh.mesh_id,
        node_xy_m=[list(point) for point in mesh.node_xy],
        face_nodes=[list(face) for face in mesh.face_nodes],
        face_id=list(mesh.face_id),
        face_area_m2=list(mesh.face_area),
        output_times_s=list(run_capability.config.output_times_s),
        topology_fingerprint=run_capability.report["topology_fingerprint"],
        terrain_fingerprint=run_capability.report["terrain_fingerprint"],
        source_config_sha256=_sha256(capability.config_path),
        run_config_sha256=_sha256(capability.run_config_path),
        business_package_sha256=_sha256(run_capability.business_path),
        mesh_sha256=_sha256(run_capability.mesh_path),
        **refs,
        source_cfl_fraction=capability.config.source_cfl_fraction,
        initial_water_volume_m3=run_capability.report["initial_water_volume_m3"],
        volume_correction_reference_scale_m3=run_capability.report["volume_correction_reference_scale_m3"],
        volume_correction_budget_m3=run_capability.report["volume_correction_budget_m3"],
        manning=_prepared_manning(capability),
        rainfall=_prepared_rainfall(capability),
    )


def _atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    overwrite: bool,
    verify: Callable[[Path], None],
) -> None:
    if not path.parent.is_dir():
        raise TriangularSourceContractError(f"source-preparation output directory does not exist: {path.parent}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp.json", dir=path.parent)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        verify(temporary)
        if overwrite:
            os.replace(temporary, path)
        elif os.name == "nt":
            try:
                os.rename(temporary, path)
            except OSError as exc:
                if path.exists():
                    raise TriangularSourceContractError(f"source-preparation output appeared before publication; refusing to overwrite: {path}") from exc
                raise TriangularSourceContractError(f"cannot atomically publish source-preparation output: {exc}") from exc
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise TriangularSourceContractError(f"source-preparation output appeared before publication; refusing to overwrite: {path}") from exc
            temporary.unlink()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def read_triangular_source_preparation(
    path: str | Path,
    source_config_path: str | Path,
    *,
    enforce_configured_output_path: bool = True,
) -> TriangularSourcePreparationResult:
    """Read and bind preparation data to freshly re-read source/P2/P3-A inputs."""
    artifact_path = Path(path).resolve()
    capability = _load_capability(source_config_path, allow_existing_output=True)
    if enforce_configured_output_path and artifact_path != capability.output_path:
        raise TriangularSourceContractError("source-preparation artifact path does not match configured output_file")
    try:
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
        actual = TriangularSourcePreparationResult.model_validate(raw)
    except FileNotFoundError as exc:
        raise TriangularSourceContractError(f"source-preparation artifact not found: {artifact_path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise TriangularSourceContractError(f"source-preparation artifact is invalid: {exc}") from exc
    expected = _build_preparation_result(capability)
    if actual.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise TriangularSourceContractError("source-preparation artifact does not match re-read config, P2 mesh, terrain or P3-A source fields")
    return actual


def prepare_triangular_sources(source_config_path: str | Path) -> TriangularSourcePreparationResult:
    """Validate and serialize source adaptation data without applying sources."""
    capability = _load_capability(source_config_path)
    result = _build_preparation_result(capability)
    _atomic_write_json(
        capability.output_path,
        result.model_dump(mode="json"),
        overwrite=capability.config.overwrite_existing_output,
        verify=lambda candidate: read_triangular_source_preparation(
            candidate, capability.config_path, enforce_configured_output_path=False,
        ),
    )
    return read_triangular_source_preparation(capability.output_path, capability.config_path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare triangular Manning/rainfall source contracts; does not run or apply a solver source.")
    parser.add_argument("config", help="YAML triangular source-preparation config")
    args = parser.parse_args(argv)
    try:
        result = prepare_triangular_sources(args.config)
    except (TriangularSourceContractError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "ManningSourceRequest", "PreparedManningField", "PreparedRainfallField", "PreparedRainfallInterval",
    "RainfallIntervalRequest", "RainfallSourceRequest", "TriangularSourceContractError",
    "TriangularSourcePreparationConfig", "TriangularSourcePreparationResult",
    "load_triangular_source_preparation_config", "main", "prepare_triangular_sources",
    "read_triangular_source_preparation",
]


if __name__ == "__main__":  # pragma: no cover - module CLI smoke tested separately
    raise SystemExit(main())
