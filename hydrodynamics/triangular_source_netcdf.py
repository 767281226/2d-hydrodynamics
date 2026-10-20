"""Native source-aware rainfall/combined result I/O using the shared P4 topology reader."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import numpy as np

from .triangular_solver_contract import TriangularResultContract, TriangularSolverContractError
from .triangular_solver_netcdf import TriangularResultNetCDFError, _netcdf4, _read_result_contract, _write_file
from .triangular_source_contract import (
    SOURCE_RESULT_FORMAT,
    SOURCE_RESULT_PRODUCT,
    SOURCE_SOLVER_VERSION,
    TriangularSourceContractError,
    TriangularSourceResultContract,
    validate_triangular_source_result_for_run,
    validate_triangular_source_run_request,
)


_RAINFALL_VARIABLES = frozenset({
    "rainfall_rate_m_s", "rainfall_volume", "rainfall_cumulative_volume",
})
_MANNING_VARIABLES = frozenset({
    "manning_n", "maximum_manning_gamma", "maximum_manning_source_magnitude",
})
_SOURCE_ARRAY_FIELDS = frozenset({
    "node_xy_m", "face_nodes", "face_id", "face_area_m2", "time_s",
    "water_depth_m", "momentum_x_m2_s", "momentum_y_m2_s",
    "minimum_water_depth_m_by_time", "maximum_speed_m_s_by_time", "mass_budget",
    "subminimum_alignment_step_s", "subminimum_alignment_cfl_limit_s",
    "rainfall_rate_m_s_by_interval", "rainfall_volume_m3_by_interval",
    "cumulative_rainfall_volume_m3_by_time", "manning_face_id", "manning_n",
    "maximum_manning_gamma_per_s_by_time", "maximum_manning_source_magnitude_m2_s2_by_time",
})
_SOURCE_METADATA_OVERRIDES = {
    "format": SOURCE_RESULT_FORMAT,
    "product_type": SOURCE_RESULT_PRODUCT,
    "sources_applied": None,
    "manning_enabled": None,
    "manning_applied": None,
    "rainfall_enabled": True,
    "rainfall_applied": True,
    "solver_version": SOURCE_SOLVER_VERSION,
}


def _source_metadata(result: TriangularSourceResultContract) -> dict[str, Any]:
    data = result.model_dump(mode="json")
    for name in _SOURCE_ARRAY_FIELDS:
        data.pop(name)
    return data


def _read_source_extra_variables(root: Any, values: dict[str, Any], read_array) -> None:
    def require_variable(name: str, dimensions: tuple[str, ...], units: str) -> np.ndarray:
        var = root.variables[name]
        if tuple(var.dimensions) != dimensions or var.dtype.kind != "f" or var.dtype.itemsize != 8:
            raise TriangularResultNetCDFError(f"source result {name} must be float64 with dimensions {dimensions}")
        if getattr(var, "units", None) != units:
            raise TriangularResultNetCDFError(f"source result {name}.units must be {units!r}")
        if "scale_factor" in var.ncattrs() or "add_offset" in var.ncattrs():
            raise TriangularResultNetCDFError(f"source result {name} may not use scale_factor or add_offset")
        return read_array(name).astype(np.float64).tolist()

    values["rainfall_rate_m_s_by_interval"] = require_variable(
        "rainfall_rate_m_s", ("output_interval",), "m s-1",
    )
    values["rainfall_volume_m3_by_interval"] = require_variable(
        "rainfall_volume", ("output_interval",), "m3",
    )
    values["cumulative_rainfall_volume_m3_by_time"] = require_variable(
        "rainfall_cumulative_volume", ("time",), "m3",
    )

    if values["manning_enabled"]:
        values["manning_n"] = require_variable("manning_n", ("face",), "s/m^(1/3)")
        values["manning_face_id"] = read_array("face_id").astype(np.int64).tolist()
        values["maximum_manning_gamma_per_s_by_time"] = require_variable(
            "maximum_manning_gamma", ("time",), "s-1",
        )
        values["maximum_manning_source_magnitude_m2_s2_by_time"] = require_variable(
            "maximum_manning_source_magnitude", ("time",), "m2 s-2",
        )
        n_var = root.variables["manning_n"]
        for name, expected in (
            ("source_unit", "1"), ("interpretation_policy", "legacy_unit1_as_si_manning"),
            ("mesh", "mesh2d"), ("location", "face"), ("grid_mapping", "crs"),
        ):
            if getattr(n_var, name, None) != expected:
                raise TriangularResultNetCDFError(f"manning_n.{name} must be {expected!r}")
        if getattr(n_var, "interpretation_rationale", None) != values["manning_interpretation_rationale"]:
            raise TriangularResultNetCDFError("manning_n interpretation rationale conflicts with source metadata")
    else:
        time_count = len(values["time_s"])
        values["manning_face_id"] = []
        values["manning_n"] = []
        values["maximum_manning_gamma_per_s_by_time"] = [0.0] * time_count
        values["maximum_manning_source_magnitude_m2_s2_by_time"] = [0.0] * time_count


def _validate_source_metadata(metadata: Mapping[str, Any], values: Mapping[str, Any]) -> None:
    expected = set(TriangularSourceResultContract.model_fields) - _SOURCE_ARRAY_FIELDS
    actual = set(metadata)
    if actual != expected:
        raise TriangularResultNetCDFError(
            f"source metadata keys do not match the version-1 schema; extra={sorted(actual - expected)}, missing={sorted(expected - actual)}"
        )
    fixed = dict(_SOURCE_METADATA_OVERRIDES)
    fixed["sources_applied"] = values["sources_applied"]
    fixed["manning_enabled"] = values["manning_enabled"]
    fixed["manning_applied"] = values["manning_applied"]
    for name, expected_value in fixed.items():
        if metadata.get(name) != expected_value:
            raise TriangularResultNetCDFError(f"source metadata {name} must equal {expected_value!r}")
    base_fields = set(TriangularResultContract.model_fields)
    source_only_fields = set(TriangularSourceResultContract.model_fields) - base_fields
    ignored = source_only_fields | set(_SOURCE_METADATA_OVERRIDES)
    for name in expected - ignored:
        if name in values and metadata[name] != values[name]:
            raise TriangularResultNetCDFError(f"source metadata {name} conflicts with the validated result data")


def read_triangular_source_result(
    path: str | Path,
    source_run_config_path: str | Path,
    *,
    enforce_configured_result_path: bool = True,
    allow_overwrite_flag_change: bool = False,
) -> TriangularSourceResultContract:
    """Read a new-format source result and bind it to current config, prep and P2/P3-A inputs."""
    source_path = Path(path).resolve()
    run_path = Path(source_run_config_path).resolve()
    capability = validate_triangular_source_run_request(run_path, allow_existing_result=True)
    if enforce_configured_result_path and source_path != capability.result_path:
        raise TriangularResultNetCDFError("source result path does not match configured result_file")
    netCDF4 = _netcdf4()
    from .netcdf_path_io import open_netcdf_dataset
    expected_manning = capability.config.sources == "manning_and_rainfall"
    expected_variables = _RAINFALL_VARIABLES | (_MANNING_VARIABLES if expected_manning else frozenset())
    try:
        with open_netcdf_dataset(netCDF4, source_path, "r") as root:
            root.set_auto_mask(True)
            for name, expected in (
                ("format", SOURCE_RESULT_FORMAT), ("result_schema_version", "1.0"),
                ("product_type", SOURCE_RESULT_PRODUCT),
            ):
                if getattr(root, name, None) != expected:
                    raise TriangularResultNetCDFError(f"unsupported source result {name}; expected {expected!r}")
            if int(getattr(root, "simulation_result", 0)) != 1 or int(getattr(root, "preparation_only", 1)) != 0:
                raise TriangularResultNetCDFError("source NetCDF is not tagged as a simulation result")
            expected_flags = {
                "manning_enabled": int(expected_manning),
                "manning_applied": int(expected_manning),
                "rainfall_enabled": 1,
                "rainfall_applied": 1,
                "gate_hydraulics_enabled": 0,
                "production_approved": 0,
            }
            for name, expected in expected_flags.items():
                if int(getattr(root, name, -1)) != expected:
                    raise TriangularResultNetCDFError(f"source result {name} must be {expected}")
            if getattr(root, "sources_applied", None) != capability.config.sources:
                raise TriangularResultNetCDFError("source result sources_applied does not match the requested source mode")
            try:
                source_run_json = json.loads(getattr(root, "source_run_config_json"))
                source_config_json = json.loads(getattr(root, "source_config_json"))
                metadata_json = json.loads(getattr(root, "contract_metadata_json"))
            except (AttributeError, TypeError, json.JSONDecodeError) as exc:
                raise TriangularResultNetCDFError("source run/source config JSON is missing or malformed") from exc
            if not isinstance(metadata_json, dict):
                raise TriangularResultNetCDFError("source contract metadata must be a JSON object")
            current_run_config = capability.config.model_dump(mode="json")
            if source_run_json != current_run_config:
                if not allow_overwrite_flag_change:
                    raise TriangularResultNetCDFError("embedded source run config does not equal current run config")
                old_without_overwrite = dict(source_run_json)
                current_without_overwrite = dict(current_run_config)
                old_without_overwrite.pop("overwrite_existing_result", None)
                current_without_overwrite.pop("overwrite_existing_result", None)
                if old_without_overwrite != current_without_overwrite:
                    raise TriangularResultNetCDFError("existing source result belongs to a different source run request")
            if source_config_json != capability.source_config.model_dump(mode="json"):
                raise TriangularResultNetCDFError("embedded source-preparation config does not equal current config")
            for root_name, metadata_name in (
                ("rainfall_nonzero_effect_applied", "rainfall_nonzero_effect_applied"),
                ("manning_nonzero_effect_applied", "nonzero_manning_effect_applied"),
            ):
                if int(getattr(root, root_name, -1)) != int(bool(metadata_json.get(metadata_name, False))):
                    raise TriangularResultNetCDFError(f"source result {root_name} conflicts with contract metadata")

        def bind_source_result(result: Any, _base_path: str | Path) -> TriangularSourceResultContract:
            return validate_triangular_source_result_for_run(
                result, run_path, allow_overwrite_flag_change=allow_overwrite_flag_change,
            )

        return _read_result_contract(
            source_path,
            capability.base_run_config_path,
            expected_format=SOURCE_RESULT_FORMAT,
            expected_product_type=SOURCE_RESULT_PRODUCT,
            extra_variables=expected_variables,
            metadata_attribute="contract_metadata_json",
            enforce_configured_result_path=False,
            result_model=TriangularSourceResultContract,
            result_validator=bind_source_result,
            validation_config_path=run_path,
            extra_data_loader=_read_source_extra_variables,
            metadata_validator=_validate_source_metadata,
        )
    except TriangularResultNetCDFError:
        raise
    except (OSError, RuntimeError, ValueError, TriangularSourceContractError, TriangularSolverContractError) as exc:
        raise TriangularResultNetCDFError(f"cannot read source result '{source_path}': {exc}") from exc


def _source_metadata(result: TriangularSourceResultContract) -> dict[str, Any]:
    data = result.model_dump(mode="json")
    for name in _SOURCE_ARRAY_FIELDS:
        data.pop(name)
    return data


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def write_triangular_source_result_atomic(
    result: TriangularSourceResultContract,
    source_run_config_path: str | Path,
) -> Path:
    """Write, validate, re-read and atomically publish a source-aware NetCDF result."""
    run_path = Path(source_run_config_path).resolve()
    capability = validate_triangular_source_run_request(run_path)
    contract = validate_triangular_source_result_for_run(result, run_path)
    destination = capability.result_path
    if not destination.parent.is_dir():
        raise TriangularResultNetCDFError(f"source result parent directory does not exist: {destination.parent}")

    existing_identity: tuple[int, int, int, int, int] | None = None
    if destination.exists() and capability.config.overwrite_existing_result:
        try:
            read_triangular_source_result(destination, run_path, allow_overwrite_flag_change=True)
            existing_identity = _file_identity(destination)
        except (TriangularResultNetCDFError, OSError, ValueError) as exc:
            raise TriangularResultNetCDFError(
                "overwrite_existing_result may replace only a prior result bound to this exact source request"
            ) from exc

    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp.nc", dir=destination.parent)
    temporary = Path(temp_name)
    os.close(fd)
    netCDF4 = _netcdf4()
    from .netcdf_path_io import open_netcdf_dataset
    try:
        _write_file(
            temporary,
            contract,
            capability.base_run_config_path,
            metadata_builder=_source_metadata,
            result_validator=lambda item, _base: validate_triangular_source_result_for_run(item, run_path),
            validation_config_path=run_path,
        )
        with open_netcdf_dataset(netCDF4, temporary, "r+") as root:
            root.setncattr("format", SOURCE_RESULT_FORMAT)
            root.setncattr("product_type", SOURCE_RESULT_PRODUCT)
            root.setncattr("manning_enabled", np.int8(contract.manning_enabled))
            root.setncattr("manning_applied", np.int8(contract.manning_applied))
            root.setncattr("rainfall_enabled", np.int8(1))
            root.setncattr("rainfall_applied", np.int8(1))
            root.setncattr("gate_hydraulics_enabled", np.int8(0))
            root.setncattr("production_approved", np.int8(0))
            root.setncattr("sources_applied", contract.sources_applied)
            root.setncattr("rainfall_nonzero_effect_applied", np.int8(contract.rainfall_nonzero_effect_applied))
            root.setncattr("manning_nonzero_effect_applied", np.int8(contract.nonzero_manning_effect_applied))
            root.setncattr("source_run_config_json", json.dumps(capability.config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
            root.setncattr("source_config_json", json.dumps(capability.source_config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
            root.setncattr("contract_metadata_json", json.dumps(_source_metadata(contract), sort_keys=True, separators=(",", ":")))

            rainfall_rate = root.createVariable("rainfall_rate_m_s", "f8", ("output_interval",))
            rainfall_rate[:] = np.asarray(contract.rainfall_rate_m_s_by_interval, dtype=np.float64)
            rainfall_rate.units = "m s-1"
            rainfall_volume = root.createVariable("rainfall_volume", "f8", ("output_interval",))
            rainfall_volume[:] = np.asarray(contract.rainfall_volume_m3_by_interval, dtype=np.float64)
            rainfall_volume.units = "m3"
            cumulative = root.createVariable("rainfall_cumulative_volume", "f8", ("time",))
            cumulative[:] = np.asarray(contract.cumulative_rainfall_volume_m3_by_time, dtype=np.float64)
            cumulative.units = "m3"

            if contract.manning_enabled:
                n_var = root.createVariable("manning_n", "f8", ("face",))
                n_var[:] = np.asarray(contract.manning_n, dtype=np.float64)
                n_var.units = contract.manning_physical_unit
                n_var.source_unit = contract.manning_source_unit
                n_var.interpretation_policy = contract.manning_interpretation_policy
                n_var.interpretation_rationale = contract.manning_interpretation_rationale
                n_var.mesh = "mesh2d"
                n_var.location = "face"
                n_var.grid_mapping = "crs"
                gamma = root.createVariable("maximum_manning_gamma", "f8", ("time",))
                gamma[:] = np.asarray(contract.maximum_manning_gamma_per_s_by_time, dtype=np.float64)
                gamma.units = "s-1"
                magnitude = root.createVariable("maximum_manning_source_magnitude", "f8", ("time",))
                magnitude[:] = np.asarray(contract.maximum_manning_source_magnitude_m2_s2_by_time, dtype=np.float64)
                magnitude.units = "m2 s-2"
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        read_triangular_source_result(temporary, run_path, enforce_configured_result_path=False)

        if existing_identity is not None:
            if not destination.exists() or _file_identity(destination) != existing_identity:
                raise TriangularResultNetCDFError("source result changed during overwrite; refusing to replace a late competing target")
            os.replace(temporary, destination)
        elif os.name == "nt":
            try:
                os.rename(temporary, destination)
            except OSError as exc:
                if destination.exists():
                    raise TriangularResultNetCDFError(f"source result appeared before publication; refusing to overwrite: {destination}") from exc
                raise TriangularResultNetCDFError(f"atomic source result no-clobber publication failed: {exc}") from exc
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise TriangularResultNetCDFError(f"source result appeared before publication; refusing to overwrite: {destination}") from exc
            temporary.unlink()
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


__all__ = ["read_triangular_source_result", "write_triangular_source_result_atomic"]
