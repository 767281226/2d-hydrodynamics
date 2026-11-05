"""Native `(time, face)` NetCDF output for the experimental triangular solver."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping

import numpy as np
from pydantic import ValidationError

from .mesh_preparation import MeshPreparationError
from .triangular_solver_contract import (
    TriangularResultContract,
    TriangularSolverContractError,
    validate_triangular_result_for_run,
    validate_triangular_run_request,
)


_RESULT_FORMAT = "triangular-solver-result-v1"
_RESULT_PRODUCT = "experimental_triangular_shallow_water_result"
_RESULT_VERSION = "1.0"


class TriangularResultNetCDFError(ValueError):
    """Raised when a triangular result artifact cannot be written or read."""


def _netcdf4():
    try:
        import netCDF4
    except ImportError as exc:  # pragma: no cover - depends on optional installation
        raise TriangularResultNetCDFError("triangular result NetCDF requires optional dependency 'netCDF4'; install the triangular extra") from exc
    return netCDF4


def _metadata(result: TriangularResultContract) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    for name in (
        "node_xy_m", "face_nodes", "face_id", "face_area_m2", "time_s",
        "water_depth_m", "momentum_x_m2_s", "momentum_y_m2_s",
        "minimum_water_depth_m_by_time", "maximum_speed_m_s_by_time", "mass_budget",
        "subminimum_alignment_step_s", "subminimum_alignment_cfl_limit_s",
    ):
        payload.pop(name)
    return payload


def _validate_crs_metadata(variable: Any, coordinate_system: str) -> None:
    """Compare WKT/CF metadata as CRS objects, not as spelling-sensitive text."""
    try:
        from pyproj import CRS
        expected = CRS.from_user_input(coordinate_system)
        for name in ("crs_wkt", "spatial_ref"):
            value = getattr(variable, name, None)
            if not isinstance(value, str) or not CRS.from_user_input(value).equals(expected, ignore_axis_order=True):
                raise TriangularResultNetCDFError(f"crs.{name} is missing or conflicts with coordinate_system {coordinate_system!r}")
        if getattr(variable, "coordinate_system", None) != coordinate_system:
            raise TriangularResultNetCDFError("crs.coordinate_system conflicts with the result coordinate_system")
        for name, expected_value in expected.to_cf().items():
            if name in {"crs_wkt", "spatial_ref"} or expected_value is None:
                continue
            actual = getattr(variable, name, None)
            if actual is None:
                raise TriangularResultNetCDFError(f"crs is missing CF attribute {name!r}")
            expected_array = np.asarray(expected_value)
            actual_array = np.asarray(actual)
            if expected_array.dtype.kind in "iuf" and actual_array.dtype.kind in "iuf":
                equal = expected_array.shape == actual_array.shape and np.allclose(actual_array, expected_array, rtol=1e-12, atol=1e-12)
            else:
                equal = np.array_equal(actual_array, expected_array)
            if not equal:
                raise TriangularResultNetCDFError(f"crs CF attribute {name!r} conflicts with coordinate_system {coordinate_system!r}")
    except TriangularResultNetCDFError:
        raise
    except ImportError as exc:  # pragma: no cover - triangular extra includes pyproj
        raise TriangularResultNetCDFError("CRS validation requires optional dependency 'pyproj'; install the triangular extra") from exc
    except Exception as exc:
        raise TriangularResultNetCDFError(f"cannot validate result CRS metadata: {exc}") from exc


def _write_file(
    path: Path,
    result: TriangularResultContract,
    run_config_path: Path,
    *,
    metadata_builder: Callable[[Any], Mapping[str, Any]] | None = None,
    result_validator: Callable[[Any, str | Path], Any] | None = None,
    validation_config_path: str | Path | None = None,
) -> None:
    netCDF4 = _netcdf4()
    from .netcdf_path_io import open_netcdf_dataset
    capability = validate_triangular_run_request(run_config_path, allow_existing_result=True)
    validator = result_validator or validate_triangular_result_for_run
    result = validator(result, validation_config_path or run_config_path)
    mesh = capability.business.mesh
    terrain = capability.business.terrain
    cfg = capability.config
    metadata = dict((metadata_builder or _metadata)(result))
    with open_netcdf_dataset(netCDF4, path, "w", format="NETCDF4") as root:
        root.setncattr("format", _RESULT_FORMAT)
        root.setncattr("result_schema_version", _RESULT_VERSION)
        root.setncattr("product_type", _RESULT_PRODUCT)
        root.setncattr("simulation_result", np.int8(1))
        root.setncattr("preparation_only", np.int8(0))
        root.setncattr("coordinate_system", mesh.coordinate_system)
        root.setncattr("mesh_id", mesh.mesh_id)
        root.setncattr("contract_metadata_json", json.dumps(metadata, sort_keys=True, separators=(",", ":")))
        root.setncattr("run_config_json", json.dumps(cfg.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))

        root.createDimension("time", len(result.time_s))
        root.createDimension("face", mesh.n_face)
        root.createDimension("node", mesh.n_node)
        root.createDimension("edge", mesh.n_edge)
        root.createDimension("max_face_nodes", 3)
        root.createDimension("two", 2)
        root.createDimension("output_interval", len(result.time_s) - 1)

        topology = root.createVariable("mesh2d", "i4")
        topology.assignValue(0)
        topology.cf_role = "mesh_topology"
        topology.topology_dimension = 2
        topology.node_coordinates = "node_x node_y"
        topology.face_coordinates = "face_x face_y"
        topology.face_node_connectivity = "face_nodes"
        topology.edge_node_connectivity = "edge_nodes"
        topology.edge_face_connectivity = "edge_faces"
        topology.face_edge_connectivity = "face_edges"
        topology.face_dimension = "face"
        topology.edge_dimension = "edge"

        crs = root.createVariable("crs", "i4")
        crs.assignValue(0)
        crs.coordinate_system = mesh.coordinate_system
        try:
            from pyproj import CRS
            wkt = CRS.from_user_input(mesh.coordinate_system).to_wkt(version="WKT2_2019")
            crs.crs_wkt = wkt
            crs.spatial_ref = wkt
            for key, value in CRS.from_user_input(mesh.coordinate_system).to_cf().items():
                if key not in {"crs_wkt", "spatial_ref"} and value is not None:
                    setattr(crs, key, value)
        except ImportError:
            pass
        except Exception as exc:
            raise TriangularResultNetCDFError(f"cannot encode projected CRS '{mesh.coordinate_system}': {exc}") from exc

        node_x = root.createVariable("node_x", "f8", ("node",))
        node_y = root.createVariable("node_y", "f8", ("node",))
        node_x[:] = [point[0] for point in mesh.node_xy]
        node_y[:] = [point[1] for point in mesh.node_xy]
        for variable, axis in ((node_x, "X"), (node_y, "Y")):
            variable.units = "m"
            variable.axis = axis
            variable.grid_mapping = "crs"
            variable.standard_name = "projection_x_coordinate" if axis == "X" else "projection_y_coordinate"

        face_nodes = root.createVariable("face_nodes", "i4", ("face", "max_face_nodes"))
        face_nodes[:] = np.asarray(mesh.face_nodes, dtype=np.int32)
        face_nodes.cf_role = "face_node_connectivity"
        face_nodes.start_index = 0
        edge_nodes = root.createVariable("edge_nodes", "i4", ("edge", "two"))
        edge_nodes[:] = np.asarray(mesh.edge_nodes, dtype=np.int32)
        edge_nodes.cf_role = "edge_node_connectivity"
        edge_nodes.start_index = 0
        edge_faces = root.createVariable("edge_faces", "i8", ("edge", "two"))
        edge_faces[:] = np.asarray(mesh.edge_faces, dtype=np.int64)
        edge_faces.cf_role = "edge_face_connectivity"
        edge_faces.start_index = 0
        face_edges = root.createVariable("face_edges", "i4", ("face", "max_face_nodes"))
        face_edges[:] = np.asarray(mesh.face_edges, dtype=np.int32)
        face_edges.cf_role = "face_edge_connectivity"
        face_edges.start_index = 0
        face_id = root.createVariable("face_id", "i8", ("face",))
        face_id[:] = np.asarray(mesh.face_id, dtype=np.int64)
        face_id.cf_role = "face_id"
        face_area = root.createVariable("face_area", "f8", ("face",))
        face_area[:] = np.asarray(mesh.face_area, dtype=np.float64)
        face_area.units = "m2"
        face_area.mesh = "mesh2d"
        face_area.location = "face"
        face_area.grid_mapping = "crs"
        edge_length = root.createVariable("edge_length", "f8", ("edge",))
        edge_length[:] = np.asarray(mesh.edge_length, dtype=np.float64)
        edge_length.units = "m"
        edge_length.mesh = "mesh2d"
        edge_length.location = "edge"
        edge_length.grid_mapping = "crs"
        face_x = root.createVariable("face_x", "f8", ("face",))
        face_y = root.createVariable("face_y", "f8", ("face",))
        face_x[:] = [point[0] for point in mesh.face_centroid]
        face_y[:] = [point[1] for point in mesh.face_centroid]
        face_x.units = face_y.units = "m"
        face_x.standard_name = "projection_x_coordinate"
        face_y.standard_name = "projection_y_coordinate"
        face_x.axis = "X"
        face_y.axis = "Y"
        face_x.grid_mapping = face_y.grid_mapping = "crs"
        face_x.mesh = face_y.mesh = "mesh2d"
        face_x.location = face_y.location = "face"
        bed = root.createVariable("bed_elevation", "f8", ("face",))
        bed[:] = np.asarray(terrain.elevation_m, dtype=np.float64)
        bed.units = "m"
        bed.mesh = "mesh2d"
        bed.location = "face"
        bed.grid_mapping = "crs"

        time = root.createVariable("time", "f8", ("time",))
        time[:] = np.asarray(result.time_s, dtype=np.float64)
        time.units = "s"
        time.standard_name = "time"
        time.axis = "T"
        time.calendar = "proleptic_gregorian"

        for name, values, units, long_name in (
            ("water_depth", result.water_depth_m, "m", "water depth"),
            ("momentum_x", result.momentum_x_m2_s, "m2 s-1", "x momentum per unit width"),
            ("momentum_y", result.momentum_y_m2_s, "m2 s-1", "y momentum per unit width"),
        ):
            variable = root.createVariable(name, "f8", ("time", "face"), zlib=True, complevel=4, shuffle=True)
            variable[:] = np.asarray(values, dtype=np.float64)
            variable.units = units
            variable.long_name = long_name
            variable.mesh = "mesh2d"
            variable.location = "face"
            variable.grid_mapping = "crs"

        diagnostics = (
            ("minimum_water_depth", result.minimum_water_depth_m_by_time, "m"),
            ("maximum_speed", result.maximum_speed_m_s_by_time, "m s-1"),
            ("mass_budget_initial_volume", [item.initial_volume_m3 for item in result.mass_budget], "m3"),
            ("mass_budget_boundary_volume", [item.boundary_net_volume_m3 for item in result.mass_budget], "m3"),
            ("mass_budget_source_volume", [item.source_net_volume_m3 for item in result.mass_budget], "m3"),
            ("mass_budget_correction_volume", [item.correction_volume_m3 for item in result.mass_budget], "m3"),
            ("mass_budget_final_volume", [item.final_volume_m3 for item in result.mass_budget], "m3"),
            ("mass_budget_residual", [item.residual_m3 for item in result.mass_budget], "m3"),
        )
        for name, values, units in diagnostics:
            variable = root.createVariable(name, "f8", ("time",))
            variable[:] = np.asarray(values, dtype=np.float64)
            variable.units = units
        align = root.createVariable("subminimum_alignment_step", "f8", ("output_interval",))
        align[:] = np.asarray(result.subminimum_alignment_step_s, dtype=np.float64)
        align.units = "s"
        align_limit = root.createVariable("subminimum_alignment_cfl_limit", "f8", ("output_interval",))
        align_limit[:] = np.asarray(result.subminimum_alignment_cfl_limit_s, dtype=np.float64)
        align_limit.units = "s"


def _read_result_contract(
    path: str | Path,
    run_config_path: str | Path,
    *,
    expected_format: str,
    expected_product_type: str,
    extra_variables: frozenset[str] = frozenset(),
    metadata_attribute: str = "contract_metadata_json",
    enforce_configured_result_path: bool = True,
    result_model: type[TriangularResultContract] = TriangularResultContract,
    result_validator: Callable[[Any, str | Path], Any] | None = None,
    validation_config_path: str | Path | None = None,
    extra_data_loader: Callable[[Any, dict[str, Any], Callable[[str], np.ndarray]], None] | None = None,
    metadata_validator: Callable[[Mapping[str, Any], Mapping[str, Any]], None] | None = None,
) -> TriangularResultContract:
    """Read the shared state/topology contract from a versioned native result."""
    source = Path(path).resolve()
    config_path = Path(run_config_path).resolve()
    capability = validate_triangular_run_request(config_path, allow_existing_result=True)
    if enforce_configured_result_path and source != capability.result_path:
        raise TriangularResultNetCDFError("result path does not equal the run config's result_file")
    netCDF4 = _netcdf4()
    from .netcdf_path_io import open_netcdf_dataset
    required = {
        "mesh2d", "crs", "node_x", "node_y", "face_nodes", "edge_nodes", "edge_faces", "face_edges",
        "face_id", "face_area", "edge_length", "face_x", "face_y", "bed_elevation", "time",
        "water_depth", "momentum_x", "momentum_y", "minimum_water_depth", "maximum_speed",
        "mass_budget_initial_volume", "mass_budget_boundary_volume", "mass_budget_source_volume",
        "mass_budget_correction_volume", "mass_budget_final_volume", "mass_budget_residual",
        "subminimum_alignment_step", "subminimum_alignment_cfl_limit",
    }
    try:
        with open_netcdf_dataset(netCDF4, source, "r") as root:
            root.set_auto_mask(True)
            for attr, expected in (("format", expected_format), ("result_schema_version", _RESULT_VERSION), ("product_type", expected_product_type)):
                if getattr(root, attr, None) != expected:
                    raise TriangularResultNetCDFError(f"unsupported triangular result {attr}; expected {expected!r}")
            if int(getattr(root, "simulation_result", 0)) != 1 or int(getattr(root, "preparation_only", 1)) != 0:
                raise TriangularResultNetCDFError("NetCDF artifact is not tagged as a triangular simulation result")
            missing = (required | extra_variables) - set(root.variables)
            if missing:
                raise TriangularResultNetCDFError(f"triangular result is missing variables: {', '.join(sorted(missing))}")
            unexpected = set(root.variables) - required - extra_variables
            if unexpected:
                raise TriangularResultNetCDFError(f"triangular result contains unsupported variables: {', '.join(sorted(unexpected))}")
            dims = {name: len(root.dimensions[name]) for name in root.dimensions}
            mesh = capability.business.mesh
            expected_dimensions = {
                "time": len(capability.config.output_times_s), "face": mesh.n_face, "node": mesh.n_node,
                "edge": mesh.n_edge, "max_face_nodes": 3, "two": 2,
                "output_interval": len(capability.config.output_times_s) - 1,
            }
            if set(dims) != set(expected_dimensions) or dims != expected_dimensions:
                raise TriangularResultNetCDFError(f"triangular result dimensions must be exactly {expected_dimensions}")
            expected_variable_dimensions = {
                "mesh2d": (), "crs": (),
                "node_x": ("node",), "node_y": ("node",),
                "face_nodes": ("face", "max_face_nodes"), "edge_nodes": ("edge", "two"),
                "edge_faces": ("edge", "two"), "face_edges": ("face", "max_face_nodes"),
                "face_id": ("face",), "face_area": ("face",), "edge_length": ("edge",),
                "face_x": ("face",), "face_y": ("face",), "bed_elevation": ("face",),
                "time": ("time",), "water_depth": ("time", "face"),
                "momentum_x": ("time", "face"), "momentum_y": ("time", "face"),
                "minimum_water_depth": ("time",), "maximum_speed": ("time",),
                "mass_budget_initial_volume": ("time",), "mass_budget_boundary_volume": ("time",),
                "mass_budget_source_volume": ("time",), "mass_budget_correction_volume": ("time",),
                "mass_budget_final_volume": ("time",), "mass_budget_residual": ("time",),
                "subminimum_alignment_step": ("output_interval",),
                "subminimum_alignment_cfl_limit": ("output_interval",),
            }
            for name, expected in expected_variable_dimensions.items():
                if tuple(root.variables[name].dimensions) != expected:
                    raise TriangularResultNetCDFError(f"triangular result {name} dimensions must be {expected}")

            expected_types = {
                "mesh2d": ("i", 4), "crs": ("i", 4),
                "node_x": ("f", 8), "node_y": ("f", 8),
                "face_nodes": ("i", 4), "edge_nodes": ("i", 4), "edge_faces": ("i", 8),
                "face_edges": ("i", 4), "face_id": ("i", 8),
            }
            float_variables = set(expected_variable_dimensions) - set(expected_types)
            expected_types.update({name: ("f", 8) for name in float_variables})
            for name, (kind, itemsize) in expected_types.items():
                var = root.variables[name]
                if var.dtype.kind != kind or var.dtype.itemsize != itemsize:
                    raise TriangularResultNetCDFError(f"triangular result {name} must use {kind}{itemsize}")

            expected_units = {
                "node_x": "m", "node_y": "m", "face_area": "m2", "edge_length": "m",
                "face_x": "m", "face_y": "m", "bed_elevation": "m", "time": "s",
                "water_depth": "m", "momentum_x": "m2 s-1", "momentum_y": "m2 s-1",
                "minimum_water_depth": "m", "maximum_speed": "m s-1",
                "mass_budget_initial_volume": "m3", "mass_budget_boundary_volume": "m3",
                "mass_budget_source_volume": "m3", "mass_budget_correction_volume": "m3",
                "mass_budget_final_volume": "m3", "mass_budget_residual": "m3",
                "subminimum_alignment_step": "s", "subminimum_alignment_cfl_limit": "s",
            }
            for name, units in expected_units.items():
                if getattr(root.variables[name], "units", None) != units:
                    raise TriangularResultNetCDFError(f"triangular result {name}.units must be {units!r}")

            for name in ("face_nodes", "edge_nodes", "edge_faces", "face_edges"):
                var = root.variables[name]
                if int(getattr(var, "start_index", -1)) != 0:
                    raise TriangularResultNetCDFError(f"triangular result {name} must be integer connectivity with start_index=0")
            if getattr(root.variables["face_id"], "cf_role", None) != "face_id":
                raise TriangularResultNetCDFError("triangular result face_id.cf_role must be 'face_id'")
            for name in ("face_nodes", "edge_nodes", "edge_faces", "face_edges"):
                if getattr(root.variables[name], "cf_role", None) != {
                    "face_nodes": "face_node_connectivity", "edge_nodes": "edge_node_connectivity",
                    "edge_faces": "edge_face_connectivity", "face_edges": "face_edge_connectivity",
                }[name]:
                    raise TriangularResultNetCDFError(f"triangular result {name}.cf_role is invalid")

            topology = root.variables["mesh2d"]
            topology_attributes = {
                "cf_role": "mesh_topology", "topology_dimension": 2,
                "node_coordinates": "node_x node_y", "face_coordinates": "face_x face_y",
                "face_node_connectivity": "face_nodes", "edge_node_connectivity": "edge_nodes",
                "edge_face_connectivity": "edge_faces", "face_edge_connectivity": "face_edges",
                "face_dimension": "face", "edge_dimension": "edge",
            }
            for name, expected in topology_attributes.items():
                actual = getattr(topology, name, None)
                if actual != expected:
                    raise TriangularResultNetCDFError(f"mesh2d.{name} must be {expected!r}")
            for name, standard_name, axis in (
                ("node_x", "projection_x_coordinate", "X"), ("face_x", "projection_x_coordinate", "X"),
                ("node_y", "projection_y_coordinate", "Y"), ("face_y", "projection_y_coordinate", "Y"),
            ):
                var = root.variables[name]
                if getattr(var, "standard_name", None) != standard_name or getattr(var, "axis", None) != axis or getattr(var, "grid_mapping", None) != "crs":
                    raise TriangularResultNetCDFError(f"{name} coordinate metadata must identify projected {axis} values and grid_mapping='crs'")
            for name, location in (("face_area", "face"), ("face_x", "face"), ("face_y", "face"), ("bed_elevation", "face"),
                                   ("edge_length", "edge")):
                var = root.variables[name]
                if getattr(var, "mesh", None) != "mesh2d" or getattr(var, "location", None) != location or getattr(var, "grid_mapping", None) != "crs":
                    raise TriangularResultNetCDFError(f"{name} mesh/location/grid_mapping association is invalid")
            for name in ("water_depth", "momentum_x", "momentum_y"):
                var = root.variables[name]
                if getattr(var, "mesh", None) != "mesh2d" or getattr(var, "location", None) != "face" or getattr(var, "grid_mapping", None) != "crs":
                    raise TriangularResultNetCDFError(f"{name} must be associated with mesh2d faces and crs")
            time_variable = root.variables["time"]
            if getattr(time_variable, "standard_name", None) != "time" or getattr(time_variable, "axis", None) != "T" or getattr(time_variable, "calendar", None) != "proleptic_gregorian":
                raise TriangularResultNetCDFError("time standard_name/axis/calendar metadata is invalid")

            for name in ("face_nodes", "edge_nodes", "edge_faces", "face_edges", "face_id"):
                var = root.variables[name]
                if "scale_factor" in var.ncattrs() or "add_offset" in var.ncattrs():
                    raise TriangularResultNetCDFError(f"triangular result {name} may not use scale_factor or add_offset")

            if getattr(root, "coordinate_system", None) != capability.business.mesh.coordinate_system:
                raise TriangularResultNetCDFError("result coordinate_system does not match current P2 input")
            if getattr(root, "mesh_id", None) != capability.business.mesh.mesh_id:
                raise TriangularResultNetCDFError("result mesh_id does not match current P2 input")
            _validate_crs_metadata(root.variables["crs"], capability.business.mesh.coordinate_system)
            try:
                metadata = json.loads(getattr(root, metadata_attribute))
                run_config_json = json.loads(getattr(root, "run_config_json"))
            except (AttributeError, TypeError, json.JSONDecodeError) as exc:
                raise TriangularResultNetCDFError("result contract/config metadata is missing or malformed JSON") from exc
            if run_config_json != capability.config.model_dump(mode="json"):
                raise TriangularResultNetCDFError("embedded run config does not equal the current run config")
            if not isinstance(metadata, dict):
                raise TriangularResultNetCDFError("result contract metadata must be a JSON object")
            def read_array(name: str) -> np.ndarray:
                variable = root.variables[name]
                raw = variable[()] if variable.ndim == 0 else variable[:]
                if np.ma.isMaskedArray(raw):
                    mask = np.ma.getmaskarray(raw)
                    if np.any(mask):
                        raise TriangularResultNetCDFError(f"triangular result {name} contains masked or missing values")
                    raw = raw.data
                values = np.asarray(raw)
                if values.dtype.kind == "f" and not np.isfinite(values).all():
                    raise TriangularResultNetCDFError(f"triangular result {name} contains NaN or infinity")
                return values

            arrays = {name: read_array(name) for name in required}
            array = arrays.__getitem__
            node_xy = np.column_stack((array("node_x"), array("node_y")))
            time_s = array("time").astype(np.float64).tolist()
            h = array("water_depth").astype(np.float64).tolist()
            hx = array("momentum_x").astype(np.float64).tolist()
            hy = array("momentum_y").astype(np.float64).tolist()
            values: dict[str, Any] = dict(metadata)
            values.update({
                "node_xy_m": node_xy.astype(np.float64).tolist(),
                # The required on-disk types were checked before reading.  Keep
                # integer arrays integer here: a float64 round trip would lose
                # exact IDs above 2**53.
                "face_nodes": array("face_nodes").tolist(),
                "face_id": array("face_id").tolist(),
                "face_area_m2": array("face_area").astype(np.float64).tolist(),
                "time_s": time_s,
                "water_depth_m": h,
                "momentum_x_m2_s": hx,
                "momentum_y_m2_s": hy,
                "minimum_water_depth_m_by_time": array("minimum_water_depth").astype(np.float64).tolist(),
                "maximum_speed_m_s_by_time": array("maximum_speed").astype(np.float64).tolist(),
                "subminimum_alignment_step_s": array("subminimum_alignment_step").astype(np.float64).tolist(),
                "subminimum_alignment_cfl_limit_s": array("subminimum_alignment_cfl_limit").astype(np.float64).tolist(),
            })
            budget_arrays = {
                name: array(name)
                for name in (
                    "mass_budget_initial_volume", "mass_budget_boundary_volume", "mass_budget_source_volume",
                    "mass_budget_correction_volume", "mass_budget_final_volume", "mass_budget_residual",
                )
            }
            mass_budget = []
            for i in range(len(time_s)):
                mass_budget.append({
                    "initial_volume_m3": float(budget_arrays["mass_budget_initial_volume"][i]),
                    "boundary_net_volume_m3": float(budget_arrays["mass_budget_boundary_volume"][i]),
                    "source_net_volume_m3": float(budget_arrays["mass_budget_source_volume"][i]),
                    "correction_volume_m3": float(budget_arrays["mass_budget_correction_volume"][i]),
                    "final_volume_m3": float(budget_arrays["mass_budget_final_volume"][i]),
                    "residual_m3": float(budget_arrays["mass_budget_residual"][i]),
                })
            values["mass_budget"] = mass_budget
            if extra_data_loader is not None:
                extra_data_loader(root, values, read_array)
            if metadata_validator is not None:
                metadata_validator(metadata, values)
            # The contract carries face connectivity; verify all derived P2
            # topology arrays as well so no NetCDF-only edge mutation is lost.
            actual_mesh = capability.business.mesh
            for name, expected in (("edge_nodes", actual_mesh.edge_nodes), ("edge_faces", actual_mesh.edge_faces), ("face_edges", actual_mesh.face_edges)):
                if not np.array_equal(array(name).astype(np.int64), np.asarray(expected, dtype=np.int64)):
                    raise TriangularResultNetCDFError(f"result {name} does not match linked P2 topology")
            if not np.allclose(array("edge_length"), actual_mesh.edge_length, rtol=1e-12, atol=1e-12):
                raise TriangularResultNetCDFError("result edge_length does not match linked P2 geometry")
            if not np.allclose(array("face_x"), [p[0] for p in actual_mesh.face_centroid], rtol=1e-12, atol=1e-12) or not np.allclose(array("face_y"), [p[1] for p in actual_mesh.face_centroid], rtol=1e-12, atol=1e-12):
                raise TriangularResultNetCDFError("result face centroids do not match linked P2 geometry")
            if not np.array_equal(array("bed_elevation"), np.asarray(capability.business.terrain.elevation_m, dtype=np.float64)):
                raise TriangularResultNetCDFError("result bed_elevation does not match linked P2 terrain")
            try:
                contract = result_model.model_validate(values)
            except ValidationError as exc:
                raise TriangularSolverContractError(f"triangular result contract is invalid: {exc}") from exc
            validator = result_validator or validate_triangular_result_for_run
            return validator(contract, validation_config_path or config_path)
    except TriangularResultNetCDFError:
        raise
    except (OSError, RuntimeError, ValueError, TriangularSolverContractError, MeshPreparationError) as exc:
        raise TriangularResultNetCDFError(f"cannot read triangular result '{source}': {exc}") from exc


def read_triangular_result(
    path: str | Path,
    run_config_path: str | Path,
    *,
    enforce_configured_result_path: bool = True,
) -> TriangularResultContract:
    """Read and bind a v1 P4-1 result to its current run inputs."""
    return _read_result_contract(
        path,
        run_config_path,
        expected_format=_RESULT_FORMAT,
        expected_product_type=_RESULT_PRODUCT,
        enforce_configured_result_path=enforce_configured_result_path,
    )


def write_triangular_result_atomic(result: TriangularResultContract, run_config_path: str | Path) -> Path:
    """Validate, write, re-read and atomically publish the configured result."""
    config_path = Path(run_config_path).resolve()
    capability = validate_triangular_run_request(config_path)
    contract = validate_triangular_result_for_run(result, config_path)
    destination = capability.result_path
    if destination.is_symlink():
        raise TriangularResultNetCDFError("result_file must not be a symbolic link")
    if not destination.parent.is_dir():
        raise TriangularResultNetCDFError(f"result parent directory does not exist: {destination.parent}")
    protected = {capability.config_path, capability.business_path, capability.mesh_path}
    if destination.resolve() in protected:
        raise TriangularResultNetCDFError("result_file resolves to a protected run config, P3-A package or P2 mesh")
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp.nc", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        _write_file(temp_path, contract, config_path)
        read_triangular_result(temp_path, config_path, enforce_configured_result_path=False)
        # Flush file contents before replace where the platform supports it.
        with temp_path.open("r+b") as stream:
            os.fsync(stream.fileno())
        if capability.config.overwrite_existing_result:
            os.replace(temp_path, destination)
        elif os.name == "nt":
            # Windows rename is atomic within a volume and refuses to replace
            # an existing destination.  This remains no-clobber even if the
            # target appears after preflight.
            try:
                os.rename(temp_path, destination)
            except FileExistsError as exc:
                raise TriangularResultNetCDFError(
                    f"result_file appeared before atomic publication; refusing to overwrite: {destination}"
                ) from exc
            except OSError as exc:
                if destination.exists():
                    raise TriangularResultNetCDFError(
                        f"result_file appeared before atomic publication; refusing to overwrite: {destination}"
                    ) from exc
                raise TriangularResultNetCDFError(
                    f"atomic no-clobber publication failed for result_file '{destination}': {exc}"
                ) from exc
        else:
            # A same-directory hard link publishes the already-validated inode
            # atomically and fails if another task created the destination
            # after preflight.  A final exists() check followed by replace
            # would still have a race and could destroy that task's file.
            try:
                os.link(temp_path, destination)
            except FileExistsError as exc:
                raise TriangularResultNetCDFError(
                    f"result_file appeared before atomic publication; refusing to overwrite: {destination}"
                ) from exc
            except OSError as exc:
                raise TriangularResultNetCDFError(
                    f"atomic no-clobber publication is unavailable for result_file '{destination}': {exc}"
                ) from exc
            temp_path.unlink()
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


__all__ = ["TriangularResultNetCDFError", "read_triangular_result", "write_triangular_result_atomic"]
