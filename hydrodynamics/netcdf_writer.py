from __future__ import annotations
from pathlib import Path
import numpy as np
from .output import OutputError, SimulationOutput
from .gate_dispatch import build_gate_dispatch_log, check_gate_dispatch_paths, gate_dispatch_paths, write_gate_dispatch_log
class OutputDependencyError(ImportError): pass
class NetCDFWriter:
    @staticmethod
    def write(output: SimulationOutput, path: str | Path, *, overwrite: bool=False) -> Path:
        if not isinstance(output, SimulationOutput): raise TypeError("output must be SimulationOutput")
        output.validate()
        if output.format != "netcdf": raise OutputError("NetCDFWriter requires output.format=netcdf")
        target=Path(path)
        if target.exists() and not overwrite: raise OutputError(f"output file already exists: {target}")
        if not output.fields: raise OutputError("at least one output field is required")
        first=output.fields[0]; times=tuple(first.times or (0.0,)); ds_values=[]
        vertical_datums = set()
        for field in output.fields:
            values=np.asarray(field.values,dtype=float)
            if values.ndim==2: values=values[None,...]
            if values.shape[1:]!=(first.ny,first.nx): raise OutputError("output field spatial shapes must match")
            if (field.dx,field.dy,field.xmin,field.ymin,field.coordinate_system) != (first.dx,first.dy,first.xmin,first.ymin,first.coordinate_system): raise OutputError("output field grid metadata must match")
            ft=tuple(field.times or (times[0],))
            if ft!=times: raise OutputError("output field times must match exactly")
            if field.vertical_datum is not None:
                if not isinstance(field.vertical_datum, str) or not field.vertical_datum.strip():
                    raise OutputError("vertical_datum must be a non-empty string when provided")
                vertical_datums.add(field.vertical_datum.strip())
            ds_values.append((field,values))
        if len(vertical_datums) > 1:
            raise OutputError("output fields with vertical_datum must use one consistent datum")
        origin = (output.metadata or {}).get("event_start")
        dispatch_log = build_gate_dispatch_log(times, output.gate_dispatch, time_origin=origin)
        check_gate_dispatch_paths(target, overwrite=overwrite)
        try: import netCDF4
        except ImportError as exc: raise OutputDependencyError("NetCDF writing requires optional dependency 'netCDF4'") from exc
        target.parent.mkdir(parents=True,exist_ok=True)
        with netCDF4.Dataset(str(target),"w") as ds:
            ds.createDimension("time",len(times)); ds.createDimension("y",first.ny); ds.createDimension("x",first.nx)
            tv=ds.createVariable("time","f8",("time",)); yv=ds.createVariable("y","f8",("y",)); xv=ds.createVariable("x","f8",("x",))
            tv[:]=times; yv[:]=first.ymin+(np.arange(first.ny)+.5)*first.dy; xv[:]=first.xmin+(np.arange(first.nx)+.5)*first.dx
            tv.units="s"
            ds.format="2d-hydrodynamics-netcdf"; ds.version="1.0"; ds.grid_spacing=f"{first.dx},{first.dy}"; ds.origin=f"{first.xmin},{first.ymin}"
            if first.coordinate_system: ds.coordinate_system=first.coordinate_system
            for field,values in ds_values:
                var=ds.createVariable(field.variable.value,"f8",("time","y","x")); var[:]=values; var.units=field.unit
                if field.vertical_datum: var.vertical_datum=field.vertical_datum
                if field.valid_mask is not None:
                    m=ds.createVariable(field.variable.value+"_valid_mask","i1",("y","x")); m[:]=np.asarray(field.valid_mask,dtype=np.int8); m.flag_values="0,1"
                if field.nodata_mask is not None:
                    m=ds.createVariable(field.variable.value+"_nodata_mask","i1",("y","x")); m[:]=np.asarray(field.nodata_mask,dtype=np.int8); m.flag_values="0,1"
            csv_path, json_path = gate_dispatch_paths(target)
            ds.gate_dispatch_csv = csv_path.name
            ds.gate_dispatch_json = json_path.name
            ds.gate_dispatch_reported_operation_count = len(dispatch_log.inputs.operations)
            categories = ("model_prescribed_change", "reconstruction", "external_report", "undeclared")
            counts = {category: sum(op.source_category == category for op in dispatch_log.inputs.operations) for category in categories}
            ds.gate_dispatch_model_prescribed_change_count = counts["model_prescribed_change"]
            ds.gate_dispatch_reconstruction_operation_count = counts["reconstruction"]
            ds.gate_dispatch_external_reported_operation_count = counts["external_report"]
            ds.gate_dispatch_undeclared_operation_source_count = counts["undeclared"]
            ds.gate_dispatch_schema_version = "1.1"
            if dispatch_log.inputs.time_origin:
                ds.gate_dispatch_time_origin = dispatch_log.inputs.time_origin
        write_gate_dispatch_log(target, dispatch_log, overwrite=overwrite)
        return target
__all__=["OutputDependencyError","NetCDFWriter"]
