"""Versioned P4-2B Manning execution entry point."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .triangular_solver import TriangularSolverExecutionError, _integrate
from .triangular_solver_contract import TriangularResultContract, validate_triangular_result_for_run
from .triangular_manning_contract import (
    TriangularManningContractError as TriangularManningExecutionError,
    TriangularManningResultContract,
    TriangularManningRunCapability,
    TriangularManningRunConfig,
    _sha256,
    load_triangular_manning_run_config,
    validate_triangular_manning_result_for_run,
    validate_triangular_manning_run_request,
)


MANNING_SOLVER_VERSION = "triangular-manning-1.0.0"


def _make_manning_result(
    base_result: TriangularResultContract,
    capability: TriangularManningRunCapability,
    stats: Mapping[str, Any],
) -> TriangularManningResultContract:
    result_path = capability.result_path
    base_data = base_result.model_dump(mode="python")
    data = {
        **base_data,
        "format": "triangular-manning-result-v1",
        "product_type": "experimental_triangular_manning_shallow_water_result",
        "sources_applied": "manning",
        "manning_enabled": True,
        "manning_applied": True,
        "rainfall_enabled": False,
        "rainfall_applied": False,
        "solver_version": MANNING_SOLVER_VERSION,
        "base_run_config_artifact": Path(os.path.relpath(capability.base_run_config_path, result_path.parent)).as_posix(),
        "manning_run_config_artifact": Path(os.path.relpath(capability.config_path, result_path.parent)).as_posix(),
        "source_config_artifact": Path(os.path.relpath(capability.source_config_path, result_path.parent)).as_posix(),
        "source_preparation_artifact": Path(os.path.relpath(capability.source_preparation_path, result_path.parent)).as_posix(),
        "manning_run_config_sha256": _sha256(capability.config_path),
        "source_config_sha256": _sha256(capability.source_config_path),
        "source_preparation_sha256": _sha256(capability.source_preparation_path),
        "manning_source_unit": capability.preparation.manning.source_unit,
        "manning_physical_unit": capability.preparation.manning.physical_unit,
        "manning_interpretation_policy": capability.preparation.manning.legacy_unit1_policy,
        "manning_interpretation_rationale": capability.preparation.manning.interpretation_rationale,
        "source_cfl_fraction": capability.source_config.source_cfl_fraction,
        "manning_face_id": list(capability.preparation.manning.face_id),
        "manning_n": [float(value) for value in capability.preparation.manning.interpreted_values],
        "maximum_manning_gamma_per_s": stats["maximum_manning_gamma_per_s"],
        "maximum_manning_source_magnitude_m2_s2": stats["maximum_manning_source_magnitude_m2_s2"],
        "maximum_manning_gamma_per_s_by_time": stats["maximum_manning_gamma_per_s_by_time"],
        "maximum_manning_source_magnitude_m2_s2_by_time": stats["maximum_manning_source_magnitude_m2_s2_by_time"],
        "manning_nonzero_face_stage_evaluation_count": stats["manning_nonzero_face_stage_evaluation_count"],
        "source_cfl_limit_evaluation_count": stats["source_cfl_limit_evaluation_count"],
        "minimum_source_cfl_limit_s": stats["minimum_source_cfl_limit_s"],
        "maximum_source_cfl_limit_s": stats["maximum_source_cfl_limit_s"],
        "manning_source_cfl_limited_step_count": stats["manning_source_cfl_limited_step_count"],
        "manning_source_cfl_rejected_step_count": stats["manning_source_cfl_rejected_step_count"],
        "nonzero_manning_effect_applied": stats["nonzero_manning_effect_applied"],
    }
    return TriangularManningResultContract.model_validate(data)


def run_triangular_manning(manning_run_config_path: str | Path) -> dict[str, Any]:
    """Execute one experimental Manning-only triangular run and publish v1 Manning output."""
    capability = validate_triangular_manning_run_request(manning_run_config_path)
    base_result, stats = _integrate(
        capability.base_run,
        manning_n_m=capability.preparation.manning.interpreted_values,
        source_cfl_fraction=capability.source_config.source_cfl_fraction,
    )
    base_result = validate_triangular_result_for_run(base_result, capability.base_run_config_path)
    result = _make_manning_result(base_result, capability, stats)
    result = validate_triangular_manning_result_for_run(result, manning_run_config_path)
    from .triangular_manning_netcdf import write_triangular_manning_result_atomic
    output_path = write_triangular_manning_result_atomic(result, manning_run_config_path)
    return {
        "status": "completed",
        "experimental": True,
        "production_approved": False,
        "case_id": result.case_id,
        "result_file": str(output_path),
        "security_exclusion": capability.base_run.report["security_exclusion"],
        "sources_applied": result.sources_applied,
        "manning_applied": result.manning_applied,
        "nonzero_manning_effect_applied": result.nonzero_manning_effect_applied,
        "maximum_manning_gamma_per_s": result.maximum_manning_gamma_per_s,
        "maximum_manning_source_magnitude_m2_s2": result.maximum_manning_source_magnitude_m2_s2,
        "source_cfl_fraction": result.source_cfl_fraction,
        "source_cfl_limit_evaluation_count": result.source_cfl_limit_evaluation_count,
        "minimum_source_cfl_limit_s": result.minimum_source_cfl_limit_s,
        "maximum_source_cfl_limit_s": result.maximum_source_cfl_limit_s,
        "manning_source_cfl_limited_step_count": result.manning_source_cfl_limited_step_count,
        "manning_source_cfl_rejected_step_count": result.manning_source_cfl_rejected_step_count,
        "accepted_step_count": result.accepted_step_count,
        "accepted_cfl_step_count": result.accepted_cfl_step_count,
        "rejected_step_count": result.rejected_step_count,
        "rejected_step_reasons": result.rejected_step_reasons,
        "mass_budget": [item.model_dump(mode="json") for item in result.mass_budget],
        "minimum_accepted_step_s": result.minimum_accepted_step_s,
        "maximum_accepted_step_s": result.maximum_accepted_step_s,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the experimental triangular Manning shallow-water solver.")
    parser.add_argument("manning_run_config", help="Versioned triangular Manning execution YAML")
    args = parser.parse_args(argv)
    try:
        result = run_triangular_manning(args.manning_run_config)
    except (TriangularManningExecutionError, TriangularSolverExecutionError, OSError, ValueError) as exc:
        if isinstance(exc, TriangularSolverExecutionError):
            print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2))
            return 2
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "MANNING_SOLVER_VERSION", "TriangularManningExecutionError", "TriangularManningResultContract",
    "TriangularManningRunConfig", "TriangularManningRunCapability", "load_triangular_manning_run_config",
    "main", "run_triangular_manning", "validate_triangular_manning_result_for_run",
    "validate_triangular_manning_run_request",
]


if __name__ == "__main__":
    raise SystemExit(main())
