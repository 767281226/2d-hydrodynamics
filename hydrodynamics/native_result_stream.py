"""Small-result, source-bound read-only frame iteration for M1 output adapters.

The strict P4/P4-2 contract readers remain the authority for run, P2/P3,
source graph, topology, and numeric contracts. This M1 adapter places a hard
element cap before those current eager readers, then yields one face frame at
a time to exporters. It cannot be used for full-run-sized input; that requires
the future fully streaming strict contract reader.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
import hashlib
import json

import numpy as np

from . import triangular_shp_export as shp
from .triangular_solver_netcdf import validate_triangular_run_request
from .triangular_source_contract import validate_triangular_source_run_request


M1_MAX_MATERIALIZED_TIME_FACE_VALUES=50_000
M1_MAX_NATIVE_RESULT_BYTES=16*1024*1024
M1_MAX_NATIVE_NODES=16_384


class NativeFrameSourceError(ValueError):
    """The selected native result is not a bounded, strictly source-bound M1 input."""


@dataclass(frozen=True,slots=True)
class NativeResultFrame:
    index:int
    time_s:float
    water_depth_m:np.ndarray
    momentum_x_m2_s:np.ndarray
    momentum_y_m2_s:np.ndarray
    velocity_x_m_s:np.ndarray
    velocity_y_m_s:np.ndarray
    speed_m_s:np.ndarray
    mass_budget:dict[str,float]
    rainfall_rate_m_s:float|None
    rainfall_volume_m3:float|None
    manning_max_gamma_per_s:float|None


@dataclass(frozen=True,slots=True)
class ValidatedNativeResultFrames:
    request_path:Path
    native_result_path:Path
    native_result_sha256:str
    context:Any
    face_id:tuple[int,...]
    node_xy_m:tuple[tuple[float,float],...]
    face_nodes:tuple[tuple[int,int,int],...]
    face_area_m2:tuple[float,...]
    bed_elevation_m:tuple[float,...]
    time_s:tuple[float,...]
    time_origin:str|None
    dry_depth_threshold_m:float
    coordinate_system:str
    vertical_datum:str|None
    source_kind:str
    source_hashes:tuple[tuple[str,str],...]
    security_exclusion:dict[str,Any]|None

    def iter_frames(self)->Iterator[NativeResultFrame]:
        result=self.context.result
        count=len(self.time_s)
        if len(result.water_depth_m)!=count or len(result.momentum_x_m2_s)!=count or len(result.momentum_y_m2_s)!=count:
            raise NativeFrameSourceError("strict native contract state arrays no longer align with the time axis")
        rainfall_rates=getattr(result,"rainfall_rate_m_s_by_interval",None)
        rainfall_volumes=getattr(result,"rainfall_volume_m3_by_interval",None)
        manning_gamma=getattr(result,"maximum_manning_gamma_per_s_by_time",None)
        face_count=len(self.face_id)
        for index,time_s in enumerate(self.time_s):
            h=np.asarray(result.water_depth_m[index],dtype=np.float64)
            mx=np.asarray(result.momentum_x_m2_s[index],dtype=np.float64)
            my=np.asarray(result.momentum_y_m2_s[index],dtype=np.float64)
            if any(row.shape!=(face_count,) or not np.isfinite(row).all() for row in (h,mx,my)):
                raise NativeFrameSourceError(f"native frame {index} failed strict finite {face_count}-face alignment")
            wet=h>self.dry_depth_threshold_m
            if np.any((~wet)&((mx!=0.0)|(my!=0.0))):
                raise NativeFrameSourceError(f"native dry face carries momentum in saved frame {index}")
            ux=np.zeros(face_count,dtype=np.float64);uy=np.zeros(face_count,dtype=np.float64)
            np.divide(mx,h,out=ux,where=wet);np.divide(my,h,out=uy,where=wet)
            speed=np.hypot(ux,uy)
            budget_model=result.mass_budget[index]
            budget=(budget_model.model_dump(mode="python") if hasattr(budget_model,"model_dump") else dict(budget_model))
            interval_index=index-1
            rate=(None if interval_index<0 or rainfall_rates is None else float(rainfall_rates[interval_index]))
            rain_volume=(None if interval_index<0 or rainfall_volumes is None else float(rainfall_volumes[interval_index]))
            gamma=(None if manning_gamma is None else float(manning_gamma[index]))
            yield NativeResultFrame(index,float(time_s),h,mx,my,ux,uy,speed,budget,rate,rain_volume,gamma)
        shp._assert_sources_unchanged(self.context)


def open_validated_native_result_frames(request_path:str|Path,*,max_time_face_values:int=M1_MAX_MATERIALIZED_TIME_FACE_VALUES)->ValidatedNativeResultFrames:
    request=Path(request_path).resolve()
    if type(max_time_face_values) is not int or max_time_face_values<=0:
        raise NativeFrameSourceError("strict positive M1 element cap is required")
    try:
        raw=shp._regular_bytes(request,"M1 native SHP source request")
        header=shp._strict_yaml(raw,"M1 native SHP source request")
        if not isinstance(header,dict):raise NativeFrameSourceError("source request root must be a strict mapping")
        if header.get("format")=="native-triangle-result-shp-export-request-v1":
            request_file,typed,_=shp._load_request(request)
            run_path=shp._resolve_relative_file(request_file.parent,typed.run_config_file,"run_config_file")
            capability=validate_triangular_run_request(run_path,allow_existing_result=True)
            frame_count=len(capability.config.output_times_s);face_count=capability.business.mesh.n_face
            if frame_count*face_count>max_time_face_values:
                raise NativeFrameSourceError("strict small-result cap refused before the eager P4 contract reader")
            if capability.business.mesh.n_node>M1_MAX_NATIVE_NODES or capability.result_path.stat().st_size>M1_MAX_NATIVE_RESULT_BYTES:
                raise NativeFrameSourceError("strict M1 native node/file-size cap refused before the eager P4 contract reader")
            context=shp._read_result_context(request)
            source_kind="sources_none_closed_wall"
        elif header.get("format")=="native-triangle-result-shp-export-request-v2":
            request_file,typed,_=shp._load_source_request(request)
            run_path=shp._resolve_relative_file(request_file.parent,typed.run_config_file,"run_config_file")
            capability=validate_triangular_source_run_request(run_path,allow_existing_result=True)
            frame_count=len(capability.base_run.config.output_times_s);face_count=capability.base_run.business.mesh.n_face
            if frame_count*face_count>max_time_face_values:
                raise NativeFrameSourceError("strict small-result cap refused before the eager source-result contract reader")
            if capability.base_run.business.mesh.n_node>M1_MAX_NATIVE_NODES or capability.result_path.stat().st_size>M1_MAX_NATIVE_RESULT_BYTES:
                raise NativeFrameSourceError("strict M1 native node/file-size cap refused before the eager source-result contract reader")
            context=shp._read_native_source_context(request)
            source_kind=typed.result_kind
        else:
            raise NativeFrameSourceError("unsupported versioned native SHP request format")
    except NativeFrameSourceError:raise
    except Exception as exc:raise NativeFrameSourceError(f"strict native request/source validation failed: {type(exc).__name__}: {exc}") from exc
    result=context.result
    if (not bool(result.simulation_result) or bool(result.production_approved) or
            result.boundary_policy!="closed_wall" or result.gate_hydraulics_enabled or
            result.face_nodes is None or any(len(face)!=3 or len(set(face))!=3 for face in result.face_nodes)):
        raise NativeFrameSourceError("M1 source must be an existing closed-wall native triangular simulation result")
    ids=tuple(int(value) for value in result.face_id)
    node_xy=tuple((float(point[0]),float(point[1])) for point in result.node_xy_m)
    faces=tuple(tuple(int(value) for value in face) for face in result.face_nodes)
    areas=tuple(float(value) for value in result.face_area_m2)
    bed=tuple(float(value) for value in context.capability.business.terrain.elevation_m)
    times=tuple(float(value) for value in result.time_s)
    if not (ids and len(ids)==len(faces)==len(areas)==len(bed)==face_count and len(times)==frame_count):
        raise NativeFrameSourceError("strict native source graph, face geometry, and saved frames differ in size")
    if tuple(ids)!=tuple(int(value) for value in context.capability.business.mesh.face_id):
        raise NativeFrameSourceError("native face_id order differs from the exact validated P2 source")
    hashes=tuple(sorted((str(role),str(digest)) for role,digest in context.source_hashes.items()))
    raw_native=Path(context.native_result_path).read_bytes()
    native_sha=hashlib.sha256(raw_native).hexdigest()
    if dict(hashes).get("native_result")!=native_sha:
        raise NativeFrameSourceError("native result changed after the strict reader source-graph snapshot")
    security=(None if result.security_exclusion is None else result.security_exclusion.model_dump(mode="json"))
    return ValidatedNativeResultFrames(
        request,Path(context.native_result_path).resolve(),native_sha,context,ids,node_xy,faces,areas,bed,times,
        result.time_origin,float(result.dry_depth_threshold_m),str(result.coordinate_system),
        context.capability.business.terrain.vertical_datum,source_kind,hashes,security)
