"""Native `(time, face)` NetCDF I/O for experimental Manning runs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

from .triangular_manning import MANNING_SOLVER_VERSION, TriangularManningExecutionError
from .triangular_manning_contract import (
    TriangularManningResultContract,
    _base_result_contract,
    validate_triangular_manning_result_for_run,
    validate_triangular_manning_run_request,
)
from .triangular_solver_netcdf import (
    TriangularResultNetCDFError,
    _netcdf4,
    _read_result_contract,
    _write_file,
)
from .triangular_solver_contract import TriangularResultContract


_MANNING_FORMAT = "triangular-manning-result-v1"
_MANNING_PRODUCT = "experimental_triangular_manning_shallow_water_result"
_MANNING_EXTRA_VARIABLES = frozenset({
    "manning_n", "maximum_manning_gamma", "maximum_manning_source_magnitude",
})
_MANNING_ARRAY_FIELDS = frozenset({
    "node_xy_m", "face_nodes", "face_id", "face_area_m2", "time_s",
    "water_depth_m", "momentum_x_m2_s", "momentum_y_m2_s",
    "minimum_water_depth_m_by_time", "maximum_speed_m_s_by_time", "mass_budget",
    "subminimum_alignment_step_s", "subminimum_alignment_cfl_limit_s",
    "manning_face_id", "manning_n", "maximum_manning_gamma_per_s_by_time",
    "maximum_manning_source_magnitude_m2_s2_by_time",
})
_MANNING_METADATA_OVERRIDES = {
    "format": _MANNING_FORMAT,
    "product_type": _MANNING_PRODUCT,
    "sources_applied": "manning",
    "manning_enabled": True,
}
_MANNING_METADATA_FLAGS = {
    **_MANNING_METADATA_OVERRIDES,
    "manning_applied": True,
    "rainfall_enabled": False,
    "rainfall_applied": False,
    "solver_version": MANNING_SOLVER_VERSION,
}


def _manning_metadata(result: TriangularManningResultContract) -> dict[str, Any]:
    data = result.model_dump(mode="json")
    for name in _MANNING_ARRAY_FIELDS:
        data.pop(name)
    return data


def read_triangular_manning_result(
    path: str | Path,
    manning_run_config_path: str | Path,
    *,
    enforce_configured_result_path: bool = True,
) -> TriangularManningResultContract:
    """Read a Manning result and rebind base, source and prepared inputs."""
    source_path = Path(path).resolve()
    run_path = Path(manning_run_config_path).resolve()
    capability = validate_triangular_manning_run_request(run_path, allow_existing_result=True)
    if enforce_configured_result_path and source_path != capability.result_path:
        raise TriangularResultNetCDFError("Manning result path does not match the configured result_file")
    netCDF4 = _netcdf4()
    try:
        with netCDF4.Dataset(source_path, "r") as root:
            root.set_auto_mask(True)
            for name, expected in (("format", _MANNING_FORMAT), ("result_schema_version", "1.0"), ("product_type", _MANNING_PRODUCT)):
                if getattr(root, name, None) != expected:
                    raise TriangularResultNetCDFError(f"unsupported Manning result {name}; expected {expected!r}")
            if int(getattr(root, "simulation_result", 0)) != 1 or int(getattr(root, "preparation_only", 1)) != 0:
                raise TriangularResultNetCDFError("Manning NetCDF is not tagged as a simulation result")
            for name, expected in (("manning_enabled", 1), ("manning_applied", 1), ("rainfall_enabled", 0),
                                   ("rainfall_applied", 0),
                                   ("gate_hydraulics_enabled", 0), ("production_approved", 0)):
                if int(getattr(root, name, -1)) != expected:
                    raise TriangularResultNetCDFError(f"Manning result {name} must be {expected}")
            if getattr(root, "sources_applied", None) != "manning":
                raise TriangularResultNetCDFError("Manning result sources_applied must be 'manning'")
            try:
                metadata = json.loads(getattr(root, "manning_metadata_json"))
                manning_config_json = json.loads(getattr(root, "manning_run_config_json"))
                source_config_json = json.loads(getattr(root, "source_config_json"))
            except (AttributeError, TypeError, json.JSONDecodeError) as exc:
                raise TriangularResultNetCDFError("Manning result metadata/config JSON is missing or malformed") from exc
            if not isinstance(metadata, dict):
                raise TriangularResultNetCDFError("Manning metadata must be a JSON object")
            if manning_config_json != capability.config.model_dump(mode="json"):
                raise TriangularResultNetCDFError("embedded Manning run config does not equal current run config")
            if source_config_json != capability.source_config.model_dump(mode="json"):
                raise TriangularResultNetCDFError("embedded source-preparation config does not equal current source config")
            if set(root.variables) != set(_MANNING_EXTRA_VARIABLES) | {
                "mesh2d", "crs", "node_x", "node_y", "face_nodes", "edge_nodes", "edge_faces", "face_edges",
                "face_id", "face_area", "edge_length", "face_x", "face_y", "bed_elevation", "time",
                "water_depth", "momentum_x", "momentum_y", "minimum_water_depth", "maximum_speed",
                "mass_budget_initial_volume", "mass_budget_boundary_volume", "mass_budget_source_volume",
                "mass_budget_correction_volume", "mass_budget_final_volume", "mass_budget_residual",
                "subminimum_alignment_step", "subminimum_alignment_cfl_limit",
            }:
                raise TriangularResultNetCDFError("Manning result variables do not match the complete version-1 schema")
            for name, dims, kind, size, units in (
                ("manning_n", ("face",), "f", 8, "s/m^(1/3)"),
                ("maximum_manning_gamma", ("time",), "f", 8, "s-1"),
                ("maximum_manning_source_magnitude", ("time",), "f", 8, "m2 s-2"),
            ):
                var = root.variables[name]
                if tuple(var.dimensions) != dims or var.dtype.kind != kind or var.dtype.itemsize != size:
                    raise TriangularResultNetCDFError(f"Manning result {name} must be float64 with dimensions {dims}")
                if getattr(var, "units", None) != units:
                    raise TriangularResultNetCDFError(f"Manning result {name}.units must be {units!r}")
                if "scale_factor" in var.ncattrs() or "add_offset" in var.ncattrs():
                    raise TriangularResultNetCDFError(f"Manning result {name} may not use scale_factor or add_offset")
            n_var = root.variables["manning_n"]
            for name, expected in (("source_unit", "1"), ("interpretation_policy", "legacy_unit1_as_si_manning"),
                                   ("mesh", "mesh2d"), ("location", "face"), ("grid_mapping", "crs")):
                if getattr(n_var, name, None) != expected:
                    raise TriangularResultNetCDFError(f"manning_n.{name} must be {expected!r}")
            if getattr(n_var, "interpretation_rationale", None) != capability.preparation.manning.interpretation_rationale:
                raise TriangularResultNetCDFError("manning_n interpretation rationale does not match preparation artifact")

        # Reuse the full P4-1 topology, units, masks, CRS and input-binding
        # reader for the shared `(time,face)` state contract.  The source-aware
        # result has a distinct global format but keeps the base contract in
        # contract_metadata_json and the base P4-1 run in run_config_json.
        base = _read_result_contract(
            source_path,
            capability.base_run_config_path,
            expected_format=_MANNING_FORMAT,
            expected_product_type=_MANNING_PRODUCT,
            extra_variables=_MANNING_EXTRA_VARIABLES,
            metadata_attribute="contract_metadata_json",
            enforce_configured_result_path=False,
        )
        expected_metadata_keys = set(TriangularManningResultContract.model_fields) - _MANNING_ARRAY_FIELDS
        if set(metadata) != expected_metadata_keys:
            extra = sorted(set(metadata) - expected_metadata_keys)
            missing = sorted(expected_metadata_keys - set(metadata))
            raise TriangularResultNetCDFError(
                f"Manning metadata keys do not match the version-1 contract; extra={extra}, missing={missing}"
            )
        manning_only_fields = set(TriangularManningResultContract.model_fields) - set(TriangularResultContract.model_fields)
        base_metadata = base.model_dump(mode="json")
        for name in expected_metadata_keys - manning_only_fields - set(_MANNING_METADATA_OVERRIDES):
            if metadata[name] != base_metadata[name]:
                raise TriangularResultNetCDFError(f"Manning metadata {name} conflicts with the validated base result")
        for name, expected in _MANNING_METADATA_FLAGS.items():
            if metadata.get(name) != expected:
                raise TriangularResultNetCDFError(f"Manning metadata {name} must equal {expected!r}")

        with netCDF4.Dataset(source_path, "r") as root:
            root.set_auto_mask(True)

            def read_values(name: str) -> np.ndarray:
                variable = root.variables[name]
                raw = variable[()] if variable.ndim == 0 else variable[:]
                if np.ma.isMaskedArray(raw):
                    if np.any(np.ma.getmaskarray(raw)):
                        raise TriangularResultNetCDFError(f"Manning result {name} contains masked or missing values")
                    raw = raw.data
                result = np.asarray(raw)
                if result.dtype.kind == "f" and not np.isfinite(result).all():
                    raise TriangularResultNetCDFError(f"Manning result {name} contains NaN or infinity")
                return result

            ids = read_values("face_id").tolist()
            n_values = read_values("manning_n").tolist()
            if ids != list(capability.preparation.manning.face_id):
                raise TriangularResultNetCDFError("Manning result face_id does not match prepared P3-A roughness")
            if n_values != list(capability.preparation.manning.interpreted_values):
                raise TriangularResultNetCDFError("manning_n values do not match the explicitly interpreted P3-A values")
            data = base.model_dump(mode="python")
            for name in manning_only_fields:
                if name in metadata:
                    data[name] = metadata[name]
            data.update({
                **_MANNING_METADATA_FLAGS,
                "manning_face_id": ids,
                "manning_n": n_values,
                "maximum_manning_gamma_per_s_by_time": read_values("maximum_manning_gamma").tolist(),
                "maximum_manning_source_magnitude_m2_s2_by_time": read_values("maximum_manning_source_magnitude").tolist(),
            })
            contract = TriangularManningResultContract.model_validate(data)
        return validate_triangular_manning_result_for_run(contract, run_path)
    except TriangularResultNetCDFError:
        raise
    except (OSError, RuntimeError, ValueError, TriangularManningExecutionError) as exc:
        raise TriangularResultNetCDFError(f"cannot read Manning result '{source_path}': {exc}") from exc


def write_triangular_manning_result_atomic(
    result: TriangularManningResultContract,
    manning_run_config_path: str | Path,
) -> Path:
    """Write, validate read-back and atomically publish a Manning-only result."""
    run_path = Path(manning_run_config_path).resolve()
    capability = validate_triangular_manning_run_request(run_path)
    contract = validate_triangular_manning_result_for_run(result, run_path)
    destination = capability.result_path
    if not destination.parent.is_dir():
        raise TriangularResultNetCDFError(f"Manning result parent directory does not exist: {destination.parent}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp.nc", dir=destination.parent)
    temporary = Path(temp_name)
    os.close(fd)
    try:
        _write_file(temporary, _base_result_contract(contract), capability.base_run_config_path)
        netCDF4 = _netcdf4()
        with netCDF4.Dataset(temporary, "r+") as root:
            root.setncattr("format", _MANNING_FORMAT)
            root.setncattr("product_type", _MANNING_PRODUCT)
            root.setncattr("manning_enabled", np.int8(1))
            root.setncattr("manning_applied", np.int8(1))
            root.setncattr("rainfall_enabled", np.int8(0))
            root.setncattr("rainfall_applied", np.int8(0))
            root.setncattr("gate_hydraulics_enabled", np.int8(0))
            root.setncattr("production_approved", np.int8(0))
            root.setncattr("sources_applied", "manning")
            root.setncattr("manning_metadata_json", json.dumps(_manning_metadata(contract), sort_keys=True, separators=(",", ":")))
            root.setncattr("manning_run_config_json", json.dumps(capability.config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
            root.setncattr("source_config_json", json.dumps(capability.source_config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
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
            source = root.createVariable("maximum_manning_source_magnitude", "f8", ("time",))
            source[:] = np.asarray(contract.maximum_manning_source_magnitude_m2_s2_by_time, dtype=np.float64)
            source.units = "m2 s-2"
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        read_triangular_manning_result(temporary, run_path, enforce_configured_result_path=False)
        if capability.config.overwrite_existing_result:
            os.replace(temporary, destination)
        elif os.name == "nt":
            try:
                os.rename(temporary, destination)
            except OSError as exc:
                if destination.exists():
                    raise TriangularResultNetCDFError(f"Manning result appeared before publication; refusing to overwrite: {destination}") from exc
                raise TriangularResultNetCDFError(f"atomic Manning no-clobber publication failed: {exc}") from exc
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise TriangularResultNetCDFError(f"Manning result appeared before publication; refusing to overwrite: {destination}") from exc
            temporary.unlink()
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


__all__ = ["read_triangular_manning_result", "write_triangular_manning_result_atomic"]
