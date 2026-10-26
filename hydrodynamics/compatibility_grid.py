"""Explicit small-result projection to the legacy structured-grid contract.

This is a display/compatibility adapter only. Geometry and calculation remain
the source native triangles; this module never calls a solver.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any,Iterable

import numpy as np

from .native_result_stream import NativeResultFrame,ValidatedNativeResultFrames


FORMAT="hydrodynamics-legacy-grid-compatibility-v1"
MAX_GRID_CELLS=16_384


class CompatibilityGridError(ValueError):
    pass


@dataclass(frozen=True,slots=True)
class CompatibilityGrid:
    nx:int
    ny:int
    xmin:float
    ymin:float
    dx:float
    dy:float
    x:tuple[float,...]
    y:tuple[float,...]
    coverage_fraction:np.ndarray
    source_covered_area_m2:np.ndarray
    contributions:tuple[tuple[tuple[int,float],...],...]
    cell_shapes:tuple[Any,...]
    elevation_m:np.ndarray
    grid_id_digest:str
    mapping_metadata:dict[str,Any]


def build_compatibility_grid(source:ValidatedNativeResultFrames,*,cell_size_m:float=1.0)->CompatibilityGrid:
    if type(source) is not ValidatedNativeResultFrames:
        raise CompatibilityGridError("exact source-bound native result frames are required")
    if type(cell_size_m) not in (int,float) or not math.isfinite(float(cell_size_m)) or float(cell_size_m)<=0:
        raise CompatibilityGridError("explicit positive finite display cell size is required")
    cell_size=float(cell_size_m)
    try:
        from shapely.geometry import Polygon,box
        from shapely.strtree import STRtree
    except ImportError as exc:raise CompatibilityGridError("compatibility area mapping requires the project GIS extra") from exc
    points=source.node_xy_m
    face_polygons=[]
    for face_index,ids in enumerate(source.face_nodes):
        poly=Polygon([points[index] for index in ids])
        if not poly.is_valid or poly.area<=0 or not math.isclose(poly.area,source.face_area_m2[face_index],rel_tol=1e-9,abs_tol=1e-10):
            raise CompatibilityGridError(f"native face {source.face_id[face_index]} geometry/area differs from strict P2/P4 source")
        face_polygons.append(poly)
    xmin0=min(point[0] for point in points);xmax=max(point[0] for point in points)
    ymin0=min(point[1] for point in points);ymax0=max(point[1] for point in points)
    xmin=math.floor(xmin0/cell_size)*cell_size;ymin=math.floor(ymin0/cell_size)*cell_size
    xmax=math.ceil(xmax/cell_size)*cell_size;ymax=math.ceil(ymax0/cell_size)*cell_size
    nx=max(1,int(round((xmax-xmin)/cell_size)));ny=max(1,int(round((ymax-ymin)/cell_size)))
    if nx*ny>MAX_GRID_CELLS:raise CompatibilityGridError("small M1 compatibility display grid exceeds its fixed 16,384-cell cap")
    tree=STRtree(face_polygons)
    identity_by_geometry={id(poly):index for index,poly in enumerate(face_polygons)}
    per_cell=[];cell_shapes=[];coverage=np.zeros((ny,nx),dtype=np.float64)
    source_covered_area=np.zeros((ny,nx),dtype=np.float64);elevation=np.full((ny,nx),np.nan,dtype=np.float64)
    covered_total=0.0
    for row in range(ny):
        y0=ymin+row*cell_size;y1=y0+cell_size
        for col in range(nx):
            x0=xmin+col*cell_size;x1=x0+cell_size;cell=box(x0,y0,x1,y1)
            found=tree.query(cell);overlaps=[];shapes=[]
            for candidate in found:
                if isinstance(candidate,(int,np.integer)):index=int(candidate);poly=face_polygons[index]
                else:poly=candidate;index=identity_by_geometry.get(id(poly),-1)
                if index<0:raise CompatibilityGridError("spatial index returned a face outside the trusted topology")
                clipped=poly.intersection(cell);area=float(clipped.area)
                if area>0:
                    overlaps.append((index,area))
                    polygons=list(clipped.geoms) if clipped.geom_type=="MultiPolygon" else ([clipped] if clipped.geom_type=="Polygon" else [])
                    for part in polygons:
                        rings=[]
                        for ring in [part.exterior,*part.interiors]:
                            rings.append(tuple(((float(x)-x0)/cell_size,(float(y)-y0)/cell_size) for x,y in ring.coords))
                        if rings:shapes.append((index,tuple(rings)))
            covered=sum(area for _,area in overlaps)
            if covered>cell_size*cell_size*(1+1e-9):
                raise CompatibilityGridError(f"display cell {row},{col} overlaps native faces beyond its cell area")
            fraction=min(1.0,covered/(cell_size*cell_size))
            coverage[row,col]=fraction
            source_covered_area[row,col]=covered
            if covered>0:
                elevation[row,col]=sum(area*source.bed_elevation_m[i] for i,area in overlaps)/covered
            per_cell.append(tuple(sorted(overlaps)));cell_shapes.append(tuple(sorted(shapes,key=lambda row:row[0])))
            covered_total+=covered
    native_area=sum(source.face_area_m2)
    if not math.isclose(covered_total,native_area,rel_tol=1e-9,abs_tol=max(1e-9,native_area*1e-10)):
        raise CompatibilityGridError("compatibility display grid does not cover the exact native active face area")
    body={"format":FORMAT,"native_result_sha256":source.native_result_sha256,"cell_size_m":cell_size,
        "origin_lower_left_m":[xmin,ymin],"shape_ny_nx":[ny,nx],"id_rule":"1-based row-major: row*nx+col+1",
        "coverage_area_m2":covered_total,"native_active_area_m2":native_area,
        "face_overlap_rule":"exact planar native triangle ∩ display cell area in source projected CRS",
        "source_covered_area_m2":[row.tolist() for row in source_covered_area],
        "wet_area_semantics":"native wet_area uses the native face dry threshold; compatibility display wet-covered area sums source_covered_area_m2 where mapped display depth is strictly positive",
        "display_area_semantics":"dx*dy is the source-CRS display-cell area and is not substituted for source overlap area after reprojection",
        "partial_cell_rule":"store covered-area mean and active_coverage_fraction; never pad uncovered area with zero",
        "field_rule":"area-average h and per-width momentum over covered area; recover u/v as mean(momentum)/mean(h) for wet cells",
        "cell_contributions":[[[str(source.face_id[i]),area] for i,area in row] for row in per_cell],
        "cell_shapes_normalized":[[[str(source.face_id[i]),[[[x,y] for x,y in ring] for ring in rings]] for i,rings in row] for row in cell_shapes]}
    digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
    return CompatibilityGrid(nx,ny,xmin,ymin,cell_size,cell_size,
        tuple(xmin+(col+0.5)*cell_size for col in range(nx)),
        tuple(ymin+(row+0.5)*cell_size for row in range(ny)),coverage,source_covered_area,
        tuple(per_cell),tuple(cell_shapes),elevation,digest,body)


def project_frame_to_grid(source:ValidatedNativeResultFrames,grid:CompatibilityGrid,frame:NativeResultFrame)->dict[str,np.ndarray]:
    face_count=len(source.face_id)
    if any(row.shape!=(face_count,) for row in (frame.water_depth_m,frame.momentum_x_m2_s,frame.momentum_y_m2_s)):
        raise CompatibilityGridError("native frame arrays differ from source face_id")
    h=np.full((grid.ny,grid.nx),np.nan);mx=np.full_like(h,np.nan);my=np.full_like(h,np.nan)
    level=np.full_like(h,np.nan);ux=np.full_like(h,np.nan);uy=np.full_like(h,np.nan);speed=np.full_like(h,np.nan)
    discharge=np.full_like(h,np.nan);mask=(grid.coverage_fraction>0).astype(np.int8)
    for flat,contributions in enumerate(grid.contributions):
        if not contributions:continue
        row,col=divmod(flat,grid.nx);area=sum(weight for _,weight in contributions)
        depth=sum(weight*float(frame.water_depth_m[i]) for i,weight in contributions)/area
        momentum_x=sum(weight*float(frame.momentum_x_m2_s[i]) for i,weight in contributions)/area
        momentum_y=sum(weight*float(frame.momentum_y_m2_s[i]) for i,weight in contributions)/area
        bed=sum(weight*source.bed_elevation_m[i] for i,weight in contributions)/area
        if depth<=0.0:
            if momentum_x!=0 or momentum_y!=0:raise CompatibilityGridError("projected dry cell carries source momentum")
            u=v=0.0
        else:u=momentum_x/depth;v=momentum_y/depth
        h[row,col]=depth;mx[row,col]=momentum_x;my[row,col]=momentum_y
        level[row,col]=bed+depth;ux[row,col]=u;uy[row,col]=v
        speed[row,col]=math.hypot(u,v);discharge[row,col]=depth*u*grid.dy
    return {"water_depth":h,"momentum_x":mx,"momentum_y":my,"water_level":level,
        "velocity_x":ux,"velocity_y":uy,"velocity_magnitude":speed,"discharge":discharge,
        "active_mask":mask}


def variable_mapping(source:ValidatedNativeResultFrames)->dict[str,dict[str,Any]]:
    closed=source.context.result.boundary_policy=="closed_wall"
    rainfall_available=source.source_kind!="sources_none_closed_wall" and getattr(source.context.result,"rainfall_applied",False)
    return {
        "time":{"availability":"AVAILABLE","source":"strict native saved output times","mapping":"copy float64 seconds; time_origin remains source value"},
        "x":{"availability":"AVAILABLE","source":"native projected mesh extent","mapping":"1 m display-cell centers in source CRS"},
        "y":{"availability":"AVAILABLE","source":"native projected mesh extent","mapping":"1 m display-cell centers in source CRS"},
        "water_depth":{"availability":"AVAILABLE_DERIVED","source":"native water_depth_m","mapping":"exact triangle∩cell overlap area mean over covered native area"},
        "water_level":{"availability":"AVAILABLE_DERIVED" if source.vertical_datum else "NOT_AVAILABLE_DATUM_UNSPECIFIED",
            "source":"P3 bed_elevation plus native water_depth","mapping":"area-mean bed + area-mean depth; no datum conversion"},
        "velocity_x":{"availability":"AVAILABLE_DERIVED","source":"native momentum_x_m2_s and depth","mapping":"area-mean momentum_x / area-mean depth when mapped depth is strictly positive; native dry threshold is not applied to aggregate cells"},
        "velocity_y":{"availability":"AVAILABLE_DERIVED","source":"native momentum_y_m2_s and depth","mapping":"area-mean momentum_y / area-mean depth when mapped depth is strictly positive; native dry threshold is not applied to aggregate cells"},
        "velocity_magnitude":{"availability":"AVAILABLE_DERIVED","source":"derived compatibility velocity_x/y","mapping":"hypot of mapped velocity components"},
        "discharge":{"availability":"AVAILABLE_LEGACY_DIAGNOSTIC","source":"mapped h and u_x","mapping":"legacy display diagnostic h * u_x * display_dy; not a native edge flux"},
        "elevation":{"availability":"AVAILABLE_DERIVED","source":"P3 terrain elevation","mapping":"area mean over covered native area"},
        "active_mask":{"availability":"AVAILABLE","source":"native active face coverage","mapping":"1 iff native projected coverage is positive; security-excluded gap remains 0"},
        "maximum_depth":{"availability":"AVAILABLE_DERIVED","source":"mapped saved-frame water_depth","mapping":"maximum over saved output frames only"},
        "maximum_velocity":{"availability":"AVAILABLE_DERIVED","source":"mapped saved-frame velocity magnitude","mapping":"maximum over saved output frames only"},
        "arrival_time":{"availability":"AVAILABLE_DERIVED","source":"mapped saved-frame water_depth","mapping":f"first saved frame with depth > native dry threshold {source.dry_depth_threshold_m:g} m; NaN if never wet"},
        "inflow":{"availability":"AVAILABLE_ZERO_CLOSED_WALL" if closed else "NOT_AVAILABLE","source":"native boundary_policy","mapping":"0 only because the validated sample has closed_wall and no open-boundary flux"},
        "outflow":{"availability":"AVAILABLE_ZERO_CLOSED_WALL" if closed else "NOT_AVAILABLE","source":"native boundary_policy","mapping":"0 only because the validated sample has closed_wall and no open-boundary flux"},
        "rainfall":{"availability":"AVAILABLE_SOURCE_NONE_ZERO" if not rainfall_available and source.source_kind=="sources_none_closed_wall" else ("AVAILABLE_DERIVED" if rainfall_available else "NOT_AVAILABLE"),
            "source":"native validated rainfall_rate_m_s_by_interval" if rainfall_available else "native sources_applied flags",
            "mapping":"preceding saved interval depth converted m to mm; initial frame 0; zero only for explicit sources=none"},
        "eta":{"availability":"NOT_AVAILABLE_LEGACY_HYDROSTATIC_FILL","source":None,
            "mapping":"old global hydrostatic DEM-fill parameter has no semantically equivalent native SWE field; NaN, never zero-filled"},
        "volume":{"availability":"AVAILABLE_DERIVED","source":"native water_depth_m and face_area_m2","mapping":"sum(native depth × native face area); cross-checks native final-volume ledger"},
        "wet_area":{"availability":"AVAILABLE_DERIVED","source":"native water_depth_m, face_area_m2 and dry threshold","mapping":"sum(native face area where depth > native dry threshold"},
        "active_coverage_fraction":{"availability":"AVAILABLE_DERIVED","source":"native face polygons","mapping":"exact overlap area / source-CRS display-cell area, including partial active cells"},
        "source_covered_area_m2":{"availability":"AVAILABLE_DERIVED","source":"native face polygons","mapping":"exact source-CRS native triangle∩display-cell overlap area in m2; used for compatibility covered-area summaries"},
    }


def write_compatibility_netcdf(path:str|Path,source:ValidatedNativeResultFrames,grid:CompatibilityGrid,
                               frames:Iterable[NativeResultFrame],*,run_id:str)->dict[str,Any]:
    """Write old named fields in a versioned derived profile, without overwriting."""
    destination=Path(path).resolve()
    if destination.exists() or not destination.parent.is_dir():
        raise CompatibilityGridError("compatibility NC target exists or its parent directory is absent")
    try:import netCDF4
    except ImportError as exc:raise CompatibilityGridError("compatibility NC output requires netCDF4") from exc
    temp_handle,temp_name=tempfile.mkstemp(prefix=f".{destination.name}.",suffix=".tmp.nc",dir=destination.parent)
    os.close(temp_handle);temporary=Path(temp_name)
    times=np.asarray(source.time_s,dtype=np.float64)
    if len(times)>10_000:raise CompatibilityGridError("M1 compatibility NC frame count exceeds its fixed 10,000-frame cap")
    if source.vertical_datum is None:level_available=False
    else:level_available=True
    mapping=variable_mapping(source)
    max_depth=np.full((grid.ny,grid.nx),np.nan);max_velocity=np.full_like(max_depth,np.nan)
    arrival=np.full_like(max_depth,np.nan);coverage=grid.coverage_fraction.copy()
    volume_rows=[];wet_area_rows=[];display_wet_area_rows=[];inflow_rows=[];outflow_rows=[];rain_rows=[]
    try:
        with netCDF4.Dataset(temporary,"w",format="NETCDF4") as ds:
            ds.createDimension("time",len(times));ds.createDimension("y",grid.ny);ds.createDimension("x",grid.nx)
            ds.format="legacy-structured-compatible-grid-v1";ds.result_schema_version="1.0"
            ds.product_type="derived_structured_compatibility_grid";ds.artifact_type="derived_display_compatibility"
            ds.run_id=run_id;ds.case_id=source.context.result.case_id;ds.grid_id_digest=grid.grid_id_digest
            ds.native_result_sha256=source.native_result_sha256;ds.source_native_result_path=str(source.native_result_path)
            ds.origin=f"{grid.xmin},{grid.ymin}";ds.grid_spacing=f"{grid.dx},{grid.dy}";ds.dx=grid.dx;ds.dy=grid.dy
            ds.coordinate_system=source.coordinate_system;ds.vertical_datum=source.vertical_datum or "UNSPECIFIED_NATIVE_SOURCE_DATUM"
            ds.compatibility_profile="legacy-structured-grid-v1-derived-from-native-triangles"
            ds.compatibility_variable_mappings_json=json.dumps(mapping,sort_keys=True,separators=(",",":"),ensure_ascii=False)
            ds.active_coverage_rule="area(native triangle intersect display cell) / source-CRS display cell area"
            ds.wet_area_semantics="native wet_area is native wet-face area; compatibility display covered area is per-cell source_covered_area_m2 summed where mapped depth exceeds native dry threshold"
            ds.partial_cell_semantics="cell values are covered-area means; display renders the full cell and coverage fraction is an explicit auxiliary variable"
            ds.maximum_semantics="maximum over saved native output frames only"
            ds.arrival_time_semantics=f"earliest saved frame where mapped water_depth > {source.dry_depth_threshold_m:g} m; NaN means no saved frame exceeded threshold"
            ds.time_origin=source.time_origin or "UNSPECIFIED_RELATIVE_SOURCE_TIME"
            ds.boundary_policy=source.context.result.boundary_policy;ds.sources_applied=source.context.result.sources_applied
            ds.database_status="DISABLED_NOT_REQUESTED";ds.social_economy_status="NOT_READ_NOT_REQUESTED"
            t=ds.createVariable("time","f8",("time",));t.units="s";t.standard_name="time";t.calendar="proleptic_gregorian";t[:]=times
            xv=ds.createVariable("x","f8",("x",));xv.units="m";xv.axis="X";xv[:]=np.asarray(grid.x)
            yv=ds.createVariable("y","f8",("y",));yv.units="m";yv.axis="Y";yv[:]=np.asarray(grid.y)
            fields={}
            for name,units in (("water_depth","m"),("water_level","m"),("velocity_x","m/s"),("velocity_y","m/s"),
                               ("velocity_magnitude","m/s"),("discharge","m3/s")):
                fill=np.float32(np.nan)
                var=ds.createVariable(name,"f4",("time","y","x"),fill_value=fill)
                var.units=units;var.availability=mapping[name]["availability"];var.mapping=mapping[name]["mapping"]
                fields[name]=var
            if not level_available:fields["water_level"].availability=mapping["water_level"]["availability"]
            static={}
            for name,units in (("elevation","m"),("maximum_depth","m"),("maximum_velocity","m/s"),("arrival_time","s")):
                var=ds.createVariable(name,"f4",("y","x"),fill_value=np.float32(np.nan))
                var.units=units;var.availability=mapping[name]["availability"];var.mapping=mapping[name]["mapping"]
                static[name]=var
            active=ds.createVariable("active_mask","i1",("y","x"));active[:]=grid.coverage_fraction>0
            active.units="1";active.mapping=mapping["active_mask"]["mapping"]
            cov=ds.createVariable("active_coverage_fraction","f4",("y","x"),fill_value=np.float32(np.nan))
            cov.units="1";cov[:]=coverage
            covered=ds.createVariable("source_covered_area_m2","f8",("y","x"),fill_value=np.float64(np.nan))
            covered.units="m2";covered.availability=mapping["source_covered_area_m2"]["availability"]
            covered.mapping=mapping["source_covered_area_m2"]["mapping"];covered[:]=grid.source_covered_area_m2
            scalar_fields={}
            for name,units in (("inflow","m3/s"),("outflow","m3/s"),("rainfall","mm"),("eta","m"),("volume","m3"),("wet_area","m2")):
                fill=np.float64(np.nan)
                var=ds.createVariable(name,"f8",("time",),fill_value=fill)
                var.units=units;var.availability=mapping[name]["availability"];var.mapping=mapping[name]["mapping"]
                scalar_fields[name]=var
            static["elevation"][:]=grid.elevation_m
            seen=0
            for frame in frames:
                if frame.index!=seen:raise CompatibilityGridError("native frames must be written once in contiguous time order")
                if frame.index>=len(times) or frame.time_s!=times[frame.index]:raise CompatibilityGridError("frame order/time differs from frozen native output axis")
                mapped=project_frame_to_grid(source,grid,frame)
                for name in ("water_depth","water_level","velocity_x","velocity_y","velocity_magnitude","discharge"):
                    data=mapped[name]
                    if name=="water_level" and not level_available:data=np.full_like(data,np.nan)
                    fields[name][frame.index,:,:]=data
                active_cells=grid.coverage_fraction>0
                h=mapped["water_depth"];speed=mapped["velocity_magnitude"]
                if frame.index==0:
                    max_depth[active_cells]=h[active_cells];max_velocity[active_cells]=speed[active_cells]
                    arrived=active_cells & np.isfinite(h) & (h>source.dry_depth_threshold_m)
                    arrival[arrived]=frame.time_s
                else:
                    max_depth[active_cells]=np.fmax(max_depth[active_cells],h[active_cells])
                    max_velocity[active_cells]=np.fmax(max_velocity[active_cells],speed[active_cells])
                    arrived=active_cells & np.isnan(arrival) & np.isfinite(h) & (h>source.dry_depth_threshold_m)
                    arrival[arrived]=frame.time_s
                volume=float(sum(source.face_area_m2[i]*frame.water_depth_m[i] for i in range(len(source.face_id))))
                native_ledger=float(frame.mass_budget["final_volume_m3"])
                if not math.isclose(volume,native_ledger,rel_tol=1e-10,abs_tol=1e-10):
                    raise CompatibilityGridError(f"native volume sum at frame {frame.index} differs from its stored mass ledger")
                wet_area=float(sum(source.face_area_m2[i] for i,hf in enumerate(frame.water_depth_m) if hf>source.dry_depth_threshold_m))
                display_wet_area=float(np.sum(grid.source_covered_area_m2[np.isfinite(h)&(h>0.0)]))
                inflow=0.0;outflow=0.0
                if frame.index==0:
                    rain=0.0
                elif frame.rainfall_rate_m_s is None:
                    rain=0.0 if source.source_kind=="sources_none_closed_wall" else np.nan
                else:
                    rain=frame.rainfall_rate_m_s*(times[frame.index]-times[frame.index-1])*1000.0
                scalar_fields["inflow"][frame.index]=inflow;scalar_fields["outflow"][frame.index]=outflow
                scalar_fields["rainfall"][frame.index]=rain;scalar_fields["eta"][frame.index]=np.nan
                scalar_fields["volume"][frame.index]=volume;scalar_fields["wet_area"][frame.index]=wet_area
                volume_rows.append(volume);wet_area_rows.append(wet_area);display_wet_area_rows.append(display_wet_area)
                inflow_rows.append(inflow);outflow_rows.append(outflow);rain_rows.append(float(rain))
                seen+=1
            if seen!=len(times):raise CompatibilityGridError("compatibility writer did not receive the complete native saved-time axis")
            static["maximum_depth"][:]=max_depth;static["maximum_velocity"][:]=max_velocity;static["arrival_time"][:]=arrival
            ds.compatibility_volume_check_tolerance_m3=1e-10
            ds.compatibility_frame_count=np.int32(len(times));ds.compatibility_display_grid_cells=np.int32(grid.nx*grid.ny)
            ds.rainfall_frame_semantics="rain depth accumulated over the preceding saved-frame interval; initial frame is zero"
        with temporary.open("r+b") as stream:os.fsync(stream.fileno())
        os.link(temporary,destination);temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True);raise
    digest=hashlib.sha256()
    with destination.open("rb",buffering=0) as stream:
        while True:
            chunk=stream.read(1024*1024)
            if not chunk:break
            digest.update(chunk)
    return {"path":str(destination),"sha256":digest.hexdigest(),
        "bytes":destination.stat().st_size,"grid_id_digest":grid.grid_id_digest,
        "volume_m3":volume_rows,"wet_area_m2":wet_area_rows,
        "display_wet_covered_area_m2":display_wet_area_rows,"inflow_m3_s":inflow_rows,
        "outflow_m3_s":outflow_rows,"rainfall_mm":rain_rows,"mapping":mapping}
