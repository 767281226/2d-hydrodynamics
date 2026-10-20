"""Versioned P4-2C rainfall-only and Manning-plus-rainfall execution entry point."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .triangular_solver import TriangularSolverExecutionError, _integrate
from .triangular_source_contract import (
    SOURCE_RESULT_FORMAT,
    SOURCE_RESULT_PRODUCT,
    SOURCE_SOLVER_VERSION,
    SecurityRainfallAccounting,
    TriangularSourceContractError,
    TriangularSourceResultContract,
    TriangularSourceRunCapability,
    load_triangular_source_run_config,
    validate_triangular_source_result_for_run,
    validate_triangular_source_run_request,
)
from .triangular_solver_contract import _sha256


def _make_source_result(
    base_result_data: Mapping[str, Any],
    capability: TriangularSourceRunCapability,
    stats: Mapping[str, Any],
) -> TriangularSourceResultContract:
    result_path = capability.result_path
    prep = capability.preparation
    manning_enabled = capability.config.sources == "manning_and_rainfall"
    if manning_enabled:
        n_field = prep.manning
        manning_values = [float(value) for value in n_field.interpreted_values]
        manning_face_ids = list(n_field.face_id)
        manning_source_unit = n_field.source_unit
        manning_physical_unit = n_field.physical_unit
        manning_policy = n_field.legacy_unit1_policy
        manning_rationale = n_field.interpretation_rationale
        source_cfl_fraction = capability.source_config.source_cfl_fraction
        maximum_gamma = stats["maximum_manning_gamma_per_s"]
        maximum_source = stats["maximum_manning_source_magnitude_m2_s2"]
        gamma_by_time = stats["maximum_manning_gamma_per_s_by_time"]
        source_by_time = stats["maximum_manning_source_magnitude_m2_s2_by_time"]
        nonzero_stage_count = stats["manning_nonzero_face_stage_evaluation_count"]
        source_limit_count = stats["source_cfl_limit_evaluation_count"]
        minimum_source_limit = stats["minimum_source_cfl_limit_s"]
        maximum_source_limit = stats["maximum_source_cfl_limit_s"]
        limited_step_count = stats["manning_source_cfl_limited_step_count"]
        rejected_source_step_count = stats["manning_source_cfl_rejected_step_count"]
        nonzero_manning_effect = stats["nonzero_manning_effect_applied"]
    else:
        manning_values = []
        manning_face_ids = []
        manning_source_unit = None
        manning_physical_unit = None
        manning_policy = None
        manning_rationale = None
        source_cfl_fraction = None
        maximum_gamma = 0.0
        maximum_source = 0.0
        gamma_by_time = [0.0] * len(base_result_data["time_s"])
        source_by_time = [0.0] * len(base_result_data["time_s"])
        nonzero_stage_count = 0
        source_limit_count = 0
        minimum_source_limit = None
        maximum_source_limit = None
        limited_step_count = 0
        rejected_source_step_count = 0
        nonzero_manning_effect = False

    security_accounting = None
    security = capability.base_run.business.security_exclusion
    if security is not None:
        mesh = capability.base_run.business.mesh
        active_area = math.fsum(float(value) for value in mesh.face_area)
        excluded_area = float(security.security_inside_area_m2)
        output_times = capability.base_run.config.output_times_s
        durations = [math.fsum((float(end), -float(start))) for start, end in zip(output_times, output_times[1:])]
        rates = [float(value) for value in stats["rainfall_rate_m_s_by_interval"]]
        security_accounting = SecurityRainfallAccounting(
            active_area_m2=active_area,
            security_excluded_area_m2=excluded_area,
            accepted_active_rainfall_volume_m3_by_interval=[rate * duration * active_area for rate, duration in zip(rates, durations)],
            security_excluded_unmodeled_rainfall_volume_m3_by_interval=[rate * duration * excluded_area for rate, duration in zip(rates, durations)],
            accepted_active_rainfall_volume_m3=math.fsum(rate * duration * active_area for rate, duration in zip(rates, durations)),
            security_excluded_unmodeled_rainfall_volume_m3=math.fsum(rate * duration * excluded_area for rate, duration in zip(rates, durations)),
        ).model_dump(mode="python")

    data = {
        **dict(base_result_data),
        "format": SOURCE_RESULT_FORMAT,
        "product_type": SOURCE_RESULT_PRODUCT,
        "sources_applied": capability.config.sources,
        "manning_enabled": manning_enabled,
        "manning_applied": manning_enabled,
        "rainfall_enabled": True,
        "rainfall_applied": True,
        "solver_version": SOURCE_SOLVER_VERSION,
        "mesh_artifact": Path(os.path.relpath(capability.base_run.mesh_path, result_path.parent)).as_posix(),
        "business_package": Path(os.path.relpath(capability.base_run.business_path, result_path.parent)).as_posix(),
        "base_run_config_artifact": Path(os.path.relpath(capability.base_run_config_path, result_path.parent)).as_posix(),
        "source_run_config_artifact": Path(os.path.relpath(capability.config_path, result_path.parent)).as_posix(),
        "source_config_artifact": Path(os.path.relpath(capability.source_config_path, result_path.parent)).as_posix(),
        "source_preparation_artifact": Path(os.path.relpath(capability.source_preparation_path, result_path.parent)).as_posix(),
        "source_run_config_sha256": _sha256(capability.config_path),
        "source_config_sha256": _sha256(capability.source_config_path),
        "source_preparation_sha256": _sha256(capability.source_preparation_path),
        "rainfall_input_unit": prep.rainfall.input_unit,
        "rainfall_schedule": [item.model_dump(mode="python") for item in capability.applied_rainfall_schedule],
        "rainfall_rate_m_s_by_interval": stats["rainfall_rate_m_s_by_interval"],
        "rainfall_volume_m3_by_interval": stats["rainfall_volume_m3_by_interval"],
        "cumulative_rainfall_volume_m3_by_time": stats["cumulative_rainfall_volume_m3_by_time"],
        "rainfall_total_depth_m": stats["rainfall_total_depth_m"],
        "rainfall_total_volume_m3": stats["rainfall_total_volume_m3"],
        "rainfall_stage_evaluation_count": stats["rainfall_stage_evaluation_count"],
        "rainfall_nonzero_source_stage_evaluation_count": stats["rainfall_nonzero_source_stage_evaluation_count"],
        "rainfall_nonzero_effect_applied": stats["rainfall_nonzero_effect_applied"],
        "security_rainfall_accounting": security_accounting,
        "manning_source_unit": manning_source_unit,
        "manning_physical_unit": manning_physical_unit,
        "manning_interpretation_policy": manning_policy,
        "manning_interpretation_rationale": manning_rationale,
        "manning_face_id": manning_face_ids,
        "manning_n": manning_values,
        "source_cfl_fraction": source_cfl_fraction,
        "maximum_manning_gamma_per_s": maximum_gamma,
        "maximum_manning_source_magnitude_m2_s2": maximum_source,
        "maximum_manning_gamma_per_s_by_time": gamma_by_time,
        "maximum_manning_source_magnitude_m2_s2_by_time": source_by_time,
        "manning_nonzero_face_stage_evaluation_count": nonzero_stage_count,
        "source_cfl_limit_evaluation_count": source_limit_count,
        "minimum_source_cfl_limit_s": minimum_source_limit,
        "maximum_source_cfl_limit_s": maximum_source_limit,
        "manning_source_cfl_limited_step_count": limited_step_count,
        "manning_source_cfl_rejected_step_count": rejected_source_step_count,
        "nonzero_manning_effect_applied": nonzero_manning_effect,
    }
    return TriangularSourceResultContract.model_validate(data)


def run_triangular_source(source_run_config_path: str | Path) -> dict[str, Any]:
    """Run one experimental rainfall-only or combined Manning/rainfall case."""
    capability = validate_triangular_source_run_request(source_run_config_path)
    manning_values = (
        capability.preparation.manning.interpreted_values
        if capability.config.sources == "manning_and_rainfall" else None
    )
    source_fraction = capability.source_config.source_cfl_fraction if manning_values is not None else None
    base_result_data, stats = _integrate(
        capability.base_run,
        manning_n_m=manning_values,
        source_cfl_fraction=source_fraction,
        rainfall_rate_m_s_by_interval=capability.rainfall_rate_m_s_by_interval,
    )
    if not isinstance(base_result_data, Mapping):
        raise TriangularSourceContractError("rainfall integration did not return the source-aware result payload")
    result = _make_source_result(base_result_data, capability, stats)
    result = validate_triangular_source_result_for_run(result, source_run_config_path)
    from .triangular_source_netcdf import write_triangular_source_result_atomic
    output_path = write_triangular_source_result_atomic(result, source_run_config_path)
    return {
        "status": "completed",
        "experimental": True,
        "production_approved": False,
        "case_id": result.case_id,
        "result_file": str(output_path),
        "security_exclusion": capability.base_run.report["security_exclusion"],
        "sources_applied": result.sources_applied,
        "manning_enabled": result.manning_enabled,
        "manning_applied": result.manning_applied,
        "rainfall_applied": result.rainfall_applied,
        "manning_nonzero_effect_applied": result.nonzero_manning_effect_applied,
        "rainfall_nonzero_effect_applied": result.rainfall_nonzero_effect_applied,
        "rainfall_input_unit": result.rainfall_input_unit,
        "rainfall_stage_evaluation_count": result.rainfall_stage_evaluation_count,
        "rainfall_nonzero_source_stage_evaluation_count": result.rainfall_nonzero_source_stage_evaluation_count,
        "maximum_manning_gamma_per_s": result.maximum_manning_gamma_per_s,
        "maximum_manning_source_magnitude_m2_s2": result.maximum_manning_source_magnitude_m2_s2,
        "source_cfl_fraction": result.source_cfl_fraction,
        "minimum_source_cfl_limit_s": result.minimum_source_cfl_limit_s,
        "maximum_source_cfl_limit_s": result.maximum_source_cfl_limit_s,
        "manning_source_cfl_limited_step_count": result.manning_source_cfl_limited_step_count,
        "manning_source_cfl_rejected_step_count": result.manning_source_cfl_rejected_step_count,
        "rainfall_total_depth_m": result.rainfall_total_depth_m,
        "rainfall_total_volume_m3": result.rainfall_total_volume_m3,
        "rainfall_volume_m3_by_interval": result.rainfall_volume_m3_by_interval,
        "security_rainfall_accounting": (
            None if result.security_rainfall_accounting is None
            else result.security_rainfall_accounting.model_dump(mode="json")
        ),
        "accepted_step_count": result.accepted_step_count,
        "accepted_cfl_step_count": result.accepted_cfl_step_count,
        "rejected_step_count": result.rejected_step_count,
        "rejected_step_reasons": result.rejected_step_reasons,
        "mass_budget": [item.model_dump(mode="json") for item in result.mass_budget],
        "minimum_accepted_step_s": result.minimum_accepted_step_s,
        "maximum_accepted_step_s": result.maximum_accepted_step_s,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the experimental triangular rainfall or combined-source solver.")
    parser.add_argument("source_run_config", help="Versioned triangular source execution YAML")
    args = parser.parse_args(argv)
    try:
        result = run_triangular_source(args.source_run_config)
    except (TriangularSourceContractError, TriangularSolverExecutionError, OSError, ValueError) as exc:
        if isinstance(exc, TriangularSolverExecutionError):
            print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2))
            return 2
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "SOURCE_SOLVER_VERSION", "TriangularSourceContractError", "TriangularSourceResultContract",
    "TriangularSourceRunConfig", "load_triangular_source_run_config", "main", "run_triangular_source",
    "validate_triangular_source_result_for_run", "validate_triangular_source_run_request",
]


if __name__ == "__main__":
    raise SystemExit(main())
