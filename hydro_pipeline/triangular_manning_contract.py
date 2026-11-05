"""Versioned Manning execution contract layered over the P4-1 `none` route."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import hashlib
import math
import os
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
    TriangularSourceContractError,
    TriangularSourcePreparationConfig,
    TriangularSourcePreparationResult,
    load_triangular_source_preparation_config,
    read_triangular_source_preparation,
)


class TriangularManningContractError(ValueError):
    """Raised for an invalid versioned Manning run or result binding."""


class TriangularManningRunConfig(_StrictContractModel):
    """Independent execution selector; all hydrodynamic values come from base_run_config_file."""

    format: Literal["triangular-manning-run-v1"] = "triangular-manning-run-v1"
    schema_version: Literal["1.0"] = "1.0"
    case_id: Text
    sources: Literal["manning"]
    base_run_config_file: Text
    source_config_file: Text
    result_file: Text
    overwrite_existing_result: StrictBool = False

    @field_validator("base_run_config_file", "source_config_file", "result_file")
    @classmethod
    def relative_artifact_paths(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("path must be relative to the Manning run config")
        return value


class TriangularManningResultContract(TriangularResultContract):
    """A Manning execution result with a distinct format and applied-source markers."""

    format: Literal["triangular-manning-result-v1"]
    product_type: Literal["experimental_triangular_manning_shallow_water_result"]
    sources_applied: Literal["manning"]
    manning_enabled: Literal[True]
    manning_applied: Literal[True]
    rainfall_enabled: Literal[False]
    rainfall_applied: Literal[False]
    base_run_config_artifact: Text
    manning_run_config_artifact: Text
    source_config_artifact: Text
    source_preparation_artifact: Text
    manning_run_config_sha256: Text
    source_config_sha256: Text
    source_preparation_sha256: Text
    manning_source_unit: Literal["1"]
    manning_physical_unit: Literal["s/m^(1/3)"]
    manning_interpretation_policy: Literal["legacy_unit1_as_si_manning"]
    manning_interpretation_rationale: Text
    source_cfl_fraction: Number = Field(gt=0.0, le=1.0)
    manning_face_id: list[StrictInt] = Field(min_length=1)
    manning_n: list[Number] = Field(min_length=1)
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

    @field_validator(
        "manning_run_config_sha256", "source_config_sha256", "source_preparation_sha256",
    )
    @classmethod
    def digest_shape(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("must be a lowercase SHA-256 digest")
        return value

    @field_validator("base_run_config_artifact", "manning_run_config_artifact", "source_config_artifact", "source_preparation_artifact")
    @classmethod
    def relative_result_references(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("Manning result artifact references must be relative")
        return value

    @model_validator(mode="after")
    def source_data_and_diagnostics_are_aligned(self) -> "TriangularManningResultContract":
        face_count = len(self.face_id)
        if len(self.manning_face_id) != face_count or len(self.manning_n) != face_count:
            raise ValueError("manning_face_id and manning_n must have one value per result face")
        if tuple(self.manning_face_id) != tuple(self.face_id):
            raise ValueError("manning_face_id order must match result face_id")
        if len(set(self.manning_face_id)) != face_count or any(face_id < 0 or face_id > (1 << 63) - 1 for face_id in self.manning_face_id):
            raise ValueError("manning_face_id must contain unique non-negative int64 identifiers")
        if any(not 0.0 < value <= 1.0 for value in self.manning_n):
            raise ValueError("each SI-interpreted Manning n must be finite and satisfy 0 < n <= 1")
        if len(self.maximum_manning_gamma_per_s_by_time) != len(self.time_s) or len(self.maximum_manning_source_magnitude_m2_s2_by_time) != len(self.time_s):
            raise ValueError("per-time Manning diagnostics must align with time_s")
        for alignment_step, cfl_limit in zip(
            self.subminimum_alignment_step_s, self.subminimum_alignment_cfl_limit_s,
        ):
            if alignment_step > 0.0:
                if cfl_limit <= 0.0:
                    raise ValueError("a Manning alignment step requires a positive recorded CFL limit")
                if _source_cfl_bound_exceeded(alignment_step, cfl_limit):
                    raise ValueError("Manning alignment step may not exceed its recorded CFL limit beyond source-bound roundoff")
        if self.manning_source_cfl_limited_step_count > self.accepted_cfl_step_count:
            raise ValueError("Manning source-limited step count may not exceed accepted CFL-step count")
        source_reject_count = (
            self.rejected_step_reasons.get("second_stage_source_cfl", 0)
            + self.rejected_step_reasons.get("source_cfl_alignment", 0)
        )
        if self.manning_source_cfl_rejected_step_count != source_reject_count:
            raise ValueError("Manning source-CFL rejected count must match source-CFL rejection reasons")
        if (self.source_cfl_limit_evaluation_count == 0) != (self.minimum_source_cfl_limit_s is None and self.maximum_source_cfl_limit_s is None):
            raise ValueError("source-CFL limit extrema must be present exactly when finite limits were evaluated")
        if self.source_cfl_limit_evaluation_count > 0:
            if self.minimum_source_cfl_limit_s is None or self.maximum_source_cfl_limit_s is None:
                raise ValueError("source-CFL limit extrema are required when finite limits were evaluated")
            if self.minimum_source_cfl_limit_s > self.maximum_source_cfl_limit_s:
                raise ValueError("minimum source-CFL limit must not exceed maximum source-CFL limit")
        if (self.manning_nonzero_face_stage_evaluation_count > 0) != self.nonzero_manning_effect_applied:
            raise ValueError("nonzero_manning_effect_applied must reflect accepted nonzero face-stage source evaluations")
        if self.maximum_manning_gamma_per_s + 1e-18 < max(self.maximum_manning_gamma_per_s_by_time, default=0.0):
            raise ValueError("global maximum Manning gamma must include saved-time diagnostics")
        if self.maximum_manning_source_magnitude_m2_s2 + 1e-18 < max(self.maximum_manning_source_magnitude_m2_s2_by_time, default=0.0):
            raise ValueError("global Manning source maximum must include saved-time diagnostics")
        for row_h, row_hx, row_hy, expected_gamma, expected_source in zip(
            self.water_depth_m, self.momentum_x_m2_s, self.momentum_y_m2_s,
            self.maximum_manning_gamma_per_s_by_time,
            self.maximum_manning_source_magnitude_m2_s2_by_time,
        ):
            gamma_values = []
            source_values = []
            for h, hx, hy, n in zip(row_h, row_hx, row_hy, self.manning_n):
                if h <= self.dry_depth_threshold_m:
                    gamma_values.append(0.0)
                    source_values.append(0.0)
                else:
                    gamma = self.gravity_m_s2 * n * n * math.hypot(hx, hy) / h ** (7.0 / 3.0)
                    gamma_values.append(gamma)
                    source_values.append(gamma * math.hypot(hx, hy))
            if not math.isclose(expected_gamma, max(gamma_values, default=0.0), rel_tol=1e-12, abs_tol=1e-18):
                raise ValueError("per-time maximum_manning_gamma_per_s must be recomputed from saved state and n")
            if not math.isclose(expected_source, max(source_values, default=0.0), rel_tol=1e-12, abs_tol=1e-18):
                raise ValueError("per-time Manning source magnitude must be recomputed from saved state and n")
        return self


@dataclass(frozen=True)
class TriangularManningRunCapability:
    config_path: Path
    base_run_config_path: Path
    source_config_path: Path
    source_preparation_path: Path
    result_path: Path
    config: TriangularManningRunConfig
    source_config: TriangularSourcePreparationConfig
    base_run: TriangularRunCapability
    preparation: TriangularSourcePreparationResult
    report: Mapping[str, Any]


def load_triangular_manning_run_config(path: str | Path) -> TriangularManningRunConfig:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TriangularManningContractError(f"Manning run config not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise TriangularManningContractError(f"invalid Manning run YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TriangularManningContractError("Manning run config root must be a YAML mapping")
    try:
        return TriangularManningRunConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}" for issue in exc.errors())
        raise TriangularManningContractError(f"invalid triangular Manning run config: {details}") from exc


def _resolve_relative(base: Path, relative: str, label: str) -> Path:
    if Path(relative).is_absolute() or PureWindowsPath(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise TriangularManningContractError(f"{label} must be relative to the Manning run config")
    return (base / relative).resolve()


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise TriangularManningContractError(f"cannot hash linked Manning artifact '{path}': {exc}") from exc


def validate_triangular_manning_run_request(
    path: str | Path,
    *,
    allow_existing_result: bool = False,
) -> TriangularManningRunCapability:
    config_path = Path(path).resolve()
    config = load_triangular_manning_run_config(config_path)
    base_run_config_path = _resolve_relative(config_path.parent, config.base_run_config_file, "base_run_config_file")
    source_config_path = _resolve_relative(config_path.parent, config.source_config_file, "source_config_file")
    lexical_result_path = config_path.parent / config.result_file
    if lexical_result_path.is_symlink():
        raise TriangularManningContractError("Manning result_file must not be a symbolic link")
    result_path = _resolve_relative(config_path.parent, config.result_file, "result_file")
    try:
        source_config = load_triangular_source_preparation_config(source_config_path)
        source_run_path = _resolve_relative(source_config_path.parent, source_config.run_config_file, "source run_config_file")
        if source_run_path != base_run_config_path:
            raise TriangularManningContractError("source-preparation run_config_file must resolve to the same base run config")
        if source_config.rainfall.enabled:
            raise TriangularManningContractError("P4-2B does not support rainfall; rainfall enabled is rejected even when all intensities are zero")
        if not source_config.manning.enabled:
            raise TriangularManningContractError("Manning execution requires a source-preparation config with Manning enabled")
        base_run = validate_triangular_run_request(base_run_config_path, allow_existing_result=True)
        if base_run.config.sources != "none":
            raise TriangularManningContractError("base_run_config_file must retain sources=none and is never executed as a Manning run")
        preparation_path = _resolve_relative(source_config_path.parent, source_config.output_file, "source-preparation output_file")
        preparation = read_triangular_source_preparation(preparation_path, source_config_path)
    except TriangularManningContractError:
        raise
    except (TriangularSolverContractError, TriangularSourceContractError, OSError, ValueError) as exc:
        raise TriangularManningContractError(f"linked base/source-preparation inputs are invalid: {exc}") from exc
    if config.case_id != base_run.config.case_id or config.case_id != source_config.case_id:
        raise TriangularManningContractError("Manning run, base run and source-preparation case_id values must match")
    if not source_config.manning.enabled or not preparation.manning.requested_enabled:
        raise TriangularManningContractError("Manning execution requires a source-preparation artifact with Manning enabled")
    if preparation.rainfall.requested_enabled or source_config.rainfall.enabled:
        raise TriangularManningContractError("P4-2B does not support rainfall; rainfall enabled is rejected even when all intensities are zero")
    if preparation.manning.applied or preparation.sources_applied != "none" or preparation.execution_available:
        raise TriangularManningContractError("source-preparation artifact must remain preparation_only and unapplied")
    if preparation.manning.legacy_unit1_policy != "legacy_unit1_as_si_manning" or not preparation.manning.interpretation_rationale:
        raise TriangularManningContractError("Manning preparation is missing the explicit legacy unit-1 SI interpretation evidence")
    if tuple(preparation.manning.face_id) != tuple(base_run.business.mesh.face_id):
        raise TriangularManningContractError("prepared Manning face_id order does not match the linked P2 mesh")
    if len(preparation.manning.interpreted_values) != base_run.business.mesh.n_face or any(value is None for value in preparation.manning.interpreted_values):
        raise TriangularManningContractError("Manning execution requires one valid prepared n value for every mesh face")

    protected = {
        config_path, base_run_config_path, source_config_path, preparation_path,
        base_run.result_path, base_run.business_path, base_run.mesh_path,
    }
    if result_path in protected:
        raise TriangularManningContractError("Manning result_file must not overwrite any base config, result, source config/preparation or P2/P3-A input")
    if result_path.exists() and not result_path.is_file():
        raise TriangularManningContractError("Manning result_file must be a regular file path")
    if result_path.exists() and not config.overwrite_existing_result and not allow_existing_result:
        raise TriangularManningContractError(f"Manning result_file already exists; set overwrite_existing_result: true to authorize replacement: {result_path}")
    if result_path.exists():
        try:
            if any(item.exists() and os.path.samefile(result_path, item) for item in protected):
                raise TriangularManningContractError("Manning result_file must not be a hard link to a protected input or result")
        except OSError as exc:
            raise TriangularManningContractError(f"cannot safely inspect Manning result_file identity: {exc}") from exc
    report = {
        "status": "ready_for_experimental_manning_run",
        "preflight_only": True,
        "execution_available": True,
        "solver_completed": False,
        "experimental": True,
        "production_approved": False,
        "case_id": config.case_id,
        "base_sources": "none",
        "base_sources_applied": "none",
        "requested_sources": ["manning"],
        "manning_enabled": True,
        "manning_applied": False,
        "rainfall_enabled": False,
        "rainfall_applied": False,
        "source_cfl_fraction": source_config.source_cfl_fraction,
        "face_count": base_run.business.mesh.n_face,
        "manning_face_count": len(preparation.manning.interpreted_values),
        "volume_correction_reference_scale_m3": base_run.report["volume_correction_reference_scale_m3"],
        "volume_correction_budget_m3": base_run.report["volume_correction_budget_m3"],
        "artifacts": {
            "base_run_config": {"path": base_run_config_path.name, "sha256": _sha256(base_run_config_path)},
            "source_config": {"path": source_config_path.name, "sha256": _sha256(source_config_path)},
            "source_preparation": {"path": preparation_path.name, "sha256": _sha256(preparation_path)},
            "business_file": {"path": base_run.business_path.name, "sha256": _sha256(base_run.business_path)},
            "mesh_file": {"path": base_run.mesh_path.name, "sha256": _sha256(base_run.mesh_path)},
            "result_file": result_path.name,
        },
    }
    return TriangularManningRunCapability(
        config_path, base_run_config_path, source_config_path, preparation_path,
        result_path, config, source_config, base_run, preparation, report,
    )


def _base_result_contract(result: TriangularManningResultContract) -> TriangularResultContract:
    data = result.model_dump(mode="python")
    source_fields = {
        "base_run_config_artifact", "manning_run_config_artifact", "source_config_artifact", "source_preparation_artifact",
        "manning_run_config_sha256", "source_config_sha256", "source_preparation_sha256",
        "manning_source_unit", "manning_physical_unit", "manning_interpretation_policy", "manning_interpretation_rationale",
        "source_cfl_fraction", "manning_face_id", "manning_n", "maximum_manning_gamma_per_s",
        "maximum_manning_source_magnitude_m2_s2", "maximum_manning_gamma_per_s_by_time",
        "maximum_manning_source_magnitude_m2_s2_by_time", "manning_nonzero_face_stage_evaluation_count",
        "source_cfl_limit_evaluation_count", "minimum_source_cfl_limit_s", "maximum_source_cfl_limit_s",
        "manning_source_cfl_limited_step_count", "manning_source_cfl_rejected_step_count", "nonzero_manning_effect_applied",
        "manning_applied", "rainfall_applied",
    }
    for name in source_fields:
        data.pop(name, None)
    data.update({
        "format": "triangular-solver-result-v1",
        "product_type": "experimental_triangular_shallow_water_result",
        "manning_enabled": False,
        "sources_applied": "none",
    })
    return TriangularResultContract.model_validate(data)


def validate_triangular_manning_result_for_run(
    result: TriangularManningResultContract | Mapping[str, Any],
    manning_run_config_path: str | Path,
) -> TriangularManningResultContract:
    capability = validate_triangular_manning_run_request(manning_run_config_path, allow_existing_result=True)
    try:
        raw = result.model_dump(mode="python") if isinstance(result, TriangularManningResultContract) else result
        contract = TriangularManningResultContract.model_validate(raw)
    except ValidationError as exc:
        raise TriangularManningContractError(f"Manning result contract is invalid: {exc}") from exc
    try:
        base = _base_result_contract(contract)
        validate_triangular_result_for_run(base, capability.base_run_config_path)
    except (ValidationError, TriangularSolverContractError) as exc:
        raise TriangularManningContractError(f"embedded Manning base result is not bound to the current P2/P3-A inputs: {exc}") from exc

    result_path = capability.result_path
    expected_refs = {
        "base_run_config_artifact": (capability.base_run_config_path, result_path.parent),
        "manning_run_config_artifact": (capability.config_path, result_path.parent),
        "source_config_artifact": (capability.source_config_path, result_path.parent),
        "source_preparation_artifact": (capability.source_preparation_path, result_path.parent),
    }
    for name, (source_path, base_path) in expected_refs.items():
        expected = Path(os.path.relpath(source_path, base_path)).as_posix()
        if getattr(contract, name) != expected:
            raise TriangularManningContractError(f"Manning result {name} does not match the configured artifact")
    digest_checks = {
        "manning_run_config_sha256": _sha256(capability.config_path),
        "source_config_sha256": _sha256(capability.source_config_path),
        "source_preparation_sha256": _sha256(capability.source_preparation_path),
    }
    for name, expected in digest_checks.items():
        if getattr(contract, name) != expected:
            raise TriangularManningContractError(f"Manning result {name} does not match current input contents")
    prep = capability.preparation.manning
    if contract.case_id != capability.config.case_id:
        raise TriangularManningContractError("Manning result case_id does not match the execution request")
    if (contract.manning_source_unit != prep.source_unit or contract.manning_physical_unit != "s/m^(1/3)"
            or contract.manning_interpretation_policy != prep.legacy_unit1_policy
            or contract.manning_interpretation_rationale != prep.interpretation_rationale):
        raise TriangularManningContractError("Manning result unit interpretation does not match the current source-preparation artifact")
    if tuple(contract.manning_face_id) != tuple(prep.face_id) or tuple(contract.manning_n) != tuple(float(value) for value in prep.interpreted_values):
        raise TriangularManningContractError("Manning result face IDs or n values do not match the current P3-A preparation")
    if not math.isclose(contract.source_cfl_fraction, capability.source_config.source_cfl_fraction, rel_tol=0.0, abs_tol=1e-15):
        raise TriangularManningContractError("Manning result source_cfl_fraction does not match the source config")
    source_reject_count = (
        contract.rejected_step_reasons.get("second_stage_source_cfl", 0)
        + contract.rejected_step_reasons.get("source_cfl_alignment", 0)
    )
    if contract.manning_source_cfl_rejected_step_count != source_reject_count:
        raise TriangularManningContractError("Manning source-CFL rejection diagnostics do not match the rejection reason map")
    if not math.isclose(contract.volume_correction_reference_scale_m3, capability.base_run.report["volume_correction_reference_scale_m3"], rel_tol=1e-12, abs_tol=1e-12):
        raise TriangularManningContractError("Manning result correction reference scale must remain bound to the base initial state")
    if not math.isclose(contract.volume_correction_budget_m3, capability.base_run.report["volume_correction_budget_m3"], rel_tol=1e-12, abs_tol=1e-15):
        raise TriangularManningContractError("Manning result correction budget may not expand with source terms")
    return contract


__all__ = [
    "TriangularManningContractError", "TriangularManningResultContract", "TriangularManningRunConfig",
    "TriangularManningRunCapability", "load_triangular_manning_run_config",
    "validate_triangular_manning_run_request", "validate_triangular_manning_result_for_run",
]
