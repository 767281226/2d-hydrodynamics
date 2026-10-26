"""M1 only: export existing bounded native fixtures into a QA-only output bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any

import numpy as np
import yaml

from .compatibility_grid import build_compatibility_grid,variable_mapping,write_compatibility_netcdf
from .native_result_stream import open_validated_native_result_frames
from .native_triangle_web import NativeTriangleWebWriter
from .nc2web import convert_netcdf_to_step_json,convert_netcdf_to_web
from .triangular_shp_export import export_native_source_triangle_shp,export_native_triangle_shp


class M1BundleError(RuntimeError):pass


def _sha_file(path:Path)->str:
    digest=hashlib.sha256()
    with path.open("rb",buffering=0) as stream:
        while True:
            raw=stream.read(1024*1024)
            if not raw:break
            digest.update(raw)
    return digest.hexdigest()


def _write_json(path:Path,value:Any)->None:
    raw=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n").encode("utf-8")
    with path.open("xb") as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())


def _write_text(path:Path,text:str)->None:
    with path.open("x",encoding="utf-8",newline="\n") as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())


def _copy_verified(source:Path,destination:Path,expected_sha256:str)->dict[str,Any]:
    if source.is_symlink() or not source.is_file() or destination.exists():
        raise M1BundleError(f"source copy path is unsafe or output exists: {source} -> {destination}")
    digest=hashlib.sha256();size=0
    with source.open("rb",buffering=0) as src,destination.open("xb") as dst:
        while True:
            chunk=src.read(1024*1024)
            if not chunk:break
            dst.write(chunk);digest.update(chunk);size+=len(chunk)
        dst.flush();os.fsync(dst.fileno())
    actual=digest.hexdigest()
    if actual!=expected_sha256:
        destination.unlink(missing_ok=True)
        raise M1BundleError(f"source changed while snapshotting: {source}")
    return {"path":destination.name,"original_path":str(source.resolve()),"sha256":actual,"bytes":size}


def _four_shp_requests(source_request:Path,output:Path,source:Any)->list[dict[str,Any]]:
    # The existing exporter requires requests and output directories to remain
    # under the validated source case. Each target is a fresh subdirectory of
    # the new M1 bundle; no pre-existing request/package is changed.
    request_parent=source_request.parent.resolve()
    if output.parent.resolve()!=request_parent:
        raise M1BundleError("M1 run directory must be a new child of the native source case directory")
    (output/"shp").mkdir(parents=True,exist_ok=False)
    original=yaml.safe_load(source_request.read_text(encoding="utf-8"))
    if not isinstance(original,dict):raise M1BundleError("native SHP request must be a strict mapping")
    source_format=original.get("format")
    source_is_v2=source_format=="native-triangle-result-shp-export-request-v2"
    rows=[]
    selectors={
        "full_domain":{"scope":"FULL_DOMAIN","target":"NATIVE","selection":{"aggregation":"FRAME","frame_index":len(source.time_s)-1,"time_s":source.time_s[-1],"window_start_s":None,"window_end_s":None}},
        "full_domain_4490":{"scope":"FULL_DOMAIN","target":"EPSG:4490","selection":{"aggregation":"FRAME","frame_index":len(source.time_s)-1,"time_s":source.time_s[-1],"window_start_s":None,"window_end_s":None}},
        "maximum_wet_surface":{"scope":"MAXIMUM_WET_SURFACE","target":"NATIVE","selection":{"aggregation":"MAX_OVER_WINDOW","frame_index":None,"time_s":None,"window_start_s":source.time_s[0],"window_end_s":source.time_s[-1]}},
        "maximum_wet_surface_4490":{"scope":"MAXIMUM_WET_SURFACE","target":"EPSG:4490","selection":{"aggregation":"MAX_OVER_WINDOW","frame_index":None,"time_s":None,"window_start_s":source.time_s[0],"window_end_s":source.time_s[-1]}},
    }
    function=export_native_source_triangle_shp if source_is_v2 else export_native_triangle_shp
    for name,choice in selectors.items():
        request_data=dict(original)
        request_data.update({"output_directory":f"{output.name}/shp/{name}",
            "selector":choice["selection"],"scope_selection":choice["scope"],"target_crs":choice["target"]})
        if not source_is_v2:request_data.pop("result_kind",None)
        request_path=request_parent/(output.name+f"_request_{name}.yaml")
        _write_text(request_path,yaml.safe_dump(request_data,sort_keys=False,allow_unicode=True))
        report=function(request_path)
        if not report.valid or not report.complete:
            raise M1BundleError(f"new {name} native triangle package did not validate completely")
        package_dir=output/"shp"/name
        rows.append({"role":name,"request_path":str(request_path),"request_sha256":_sha_file(request_path),
            "package_path":str(package_dir),"manifest_sha256":_sha_file(package_dir/"manifest.json"),
            "target_crs":choice["target"],"scope":choice["scope"],
            "validation":{"valid":report.valid,"complete":report.complete,"feature_count":report.feature_count,
                "result_set_id":report.result_set_id,"checks":list(report.checks)},
            "components":sorted(path.name for path in package_dir.iterdir() if path.is_file())})
    return rows


def derive_m1_run_id(source_request_path:str|Path)->str:
    source=open_validated_native_result_frames(source_request_path)
    case_id=str(source.context.result.case_id)
    base=hashlib.sha256((case_id+"|"+source.native_result_sha256).encode()).hexdigest()[:12]
    return f"m1qa-{base}-{uuid.uuid4().hex[:8]}"


def create_m1_output_bundle(source_request_path:str|Path,output_dir:str|Path,*,cell_size_m:float=1.0)->dict[str,Any]:
    source_request=Path(source_request_path).resolve();output=Path(output_dir).resolve()
    if output.parent.resolve()!=source_request.parent.resolve():
        raise M1BundleError("new M1 output must be a child directory of the validated native source case")
    if output.exists():raise M1BundleError("M1 bundle target exists; refusing to overwrite prior output")
    native=open_validated_native_result_frames(source_request)
    case_id=str(native.context.result.case_id)
    base_id=hashlib.sha256((case_id+"|"+native.native_result_sha256).encode()).hexdigest()[:12]
    if not output.name.startswith(f"m1qa-{base_id}-") or len(output.name.split("-"))!=3:
        raise M1BundleError(f"output directory name must use the source-bound M1 run ID prefix m1qa-{base_id}-<nonce>")
    run_id=output.name
    # Source-linked run and exporter artifacts are small, already-existing
    # fixtures. Preserve each exact source as a read-only hash-addressed copy.
    output.mkdir()
    inputs=output/"inputs";inputs.mkdir()
    source_rows=[]
    for role,path in sorted(native.context.source_paths.items()):
        role_dir=inputs/role;role_dir.mkdir()
        source_rows.append({"role":role,**_copy_verified(Path(path),role_dir/Path(path).name,dict(native.source_hashes)[role])})
    native_copy=_copy_verified(native.native_result_path,output/"result_native.nc",native.native_result_sha256)
    grid=build_compatibility_grid(native,cell_size_m=cell_size_m)
    mapping=variable_mapping(native)
    compatibility_grid_doc={"format":"hydrodynamics-legacy-grid-compatibility-v1",
        "schema_version":"1.0","run_id":run_id,"grid_id_digest":grid.grid_id_digest,
        "native_result_sha256":native.native_result_sha256,"id_rule":"1-based row-major: row*nx+col+1",
        "nx":grid.nx,"ny":grid.ny,"origin_lower_left_m":[grid.xmin,grid.ymin],"dx":grid.dx,"dy":grid.dy,
        "coordinate_system":native.coordinate_system,"active_coverage_fraction":grid.coverage_fraction.tolist(),
        "source_covered_area_m2":grid.source_covered_area_m2.tolist(),
        "display_wet_area_semantics":"sum source_covered_area_m2 where mapped display depth is strictly positive; distinct from native wet_area, which uses the native face dry-depth threshold",
        "cell_shapes_normalized":grid.mapping_metadata["cell_shapes_normalized"],
        "mapping":grid.mapping_metadata,"variables":mapping,
        "display_is_derived":True,"native_triangles_remain_authoritative":True,
        "project_full_run_domain_selection":{"choice":"EXISTING_DOMAIN_MINUS_VALIDATED_SECURITY_EXCLUSION",
            "source_geometry":"Use the actual original D and S geometry sources and revalidate them in M2",
            "area_m2":None,"area_policy":"Recompute from source geometries; do not copy a cached area or use a bounding box",
            "m1_sample_relation":"This small synthetic sample's native domain is not Qingbei D-S"}}
    comp_path=output/(case_id+".nc")
    native_state=write_compatibility_netcdf(comp_path,native,grid,native.iter_frames(),run_id=run_id)
    nc2web=convert_netcdf_to_web(comp_path,output/"nc2web")
    stepjson=convert_netcdf_to_step_json(comp_path,output/"stepjson")
    compatibility_grid_doc["nc2web_frame_files"]=[{"file":row["file"],"bytes":(output/"nc2web"/row["file"]).stat().st_size,
        "sha256":_sha_file(output/"nc2web"/row["file"])} for row in nc2web.frames]
    _write_json(output/"compatibility_grid.json",compatibility_grid_doc)
    web_writer=NativeTriangleWebWriter(output/"native_web",native,run_id=run_id)
    for frame in native.iter_frames():web_writer.write_frame(frame)
    native_web=web_writer.finish()
    native_frame_stats=[]
    for frame in native.iter_frames():
        native_frame_stats.append({"time_s":frame.time_s,"max_depth_m":float(np.max(frame.water_depth_m)),
            "max_speed_m_s":float(np.max(frame.speed_m_s))})
    shp_rows=_four_shp_requests(source_request,output,native)
    # Use the unmodified project viewer asset for the bundle-local entry point.
    viewer_source=Path(__file__).resolve().parent/"assets"/"viewer.html"
    _copy_verified(viewer_source,output/"viewer.html",_sha_file(viewer_source))
    source_profile={"format":"m1-validated-native-source-v1","request_path":str(native.request_path),
        "source_kind":native.source_kind,"source_hashes":dict(native.source_hashes),
        "native_result_sha256":native.native_result_sha256,"native_result_path":str(native.native_result_path),
        "face_count":len(native.face_id),"node_count":len(native.node_xy_m),"saved_frame_count":len(native.time_s),
        "face_id_sha256":hashlib.sha256("\n".join(str(value) for value in native.face_id).encode("ascii")).hexdigest(),
        "face_ids":[str(value) for value in native.face_id],"simulation_result":True,
        "production_approved":False,"security_exclusion":native.security_exclusion}
    provenance=output/"provenance";provenance.mkdir()
    _write_json(provenance/"source_graph.json",{"format":"m1-source-graph-v1","source_files":source_rows,
        "native_result_copy":native_copy,"source_profile":source_profile})
    legacy_optional={
        "gate_dispatch.csv":{"status":"NOT_REQUESTED_FOR_CLOSED_WALL_QA_FIXTURE","baseline_schema":"UNKNOWN_PENDING_OLD_SUCCESS_BUNDLE"},
        "gate_dispatch.json":{"status":"NOT_REQUESTED_FOR_CLOSED_WALL_QA_FIXTURE","baseline_schema":"UNKNOWN_PENDING_OLD_SUCCESS_BUNDLE"},
        "dispatch_input.json":{"status":"NOT_REQUESTED_FOR_CLOSED_WALL_QA_FIXTURE","baseline_schema":"UNKNOWN_PENDING_OLD_SUCCESS_BUNDLE"},
        "database_receipt":{"status":"DISABLED_NOT_REQUESTED"},
        "social_economy_overlay":{"status":"NOT_READ_NOT_REQUESTED"},
    }
    shp_matrix=[{"artifact":f"shp/{row['role']}","status":"PASS_SMALL_NATIVE_TRIANGLE_FIXTURE",
        "manifest_sha256":row["manifest_sha256"],"components":row["components"],
        "feature_count":row["validation"]["feature_count"],"target_crs":row["target_crs"],"scope":row["scope"]} for row in shp_rows]
    _write_json(output/"run.json",{"format":"m1-output-bundle-v1","schema_version":"1.0",
        "run_id":run_id,"case_id":case_id,"status":"QA_FIXTURE_READY_NOT_PRODUCTION",
        "data_origin":"SYNTHETIC_SMALL_NATIVE_FIXTURE","qa_fixture":True,"live_solver_ran":False,
        "simulation_result":True,"production_approved":False,
        "native_result":{"file":"result_native.nc","sha256":native.native_result_sha256,
            "source_path":str(native.native_result_path),"face_count":len(native.face_id),
            "node_count":len(native.node_xy_m),"saved_frame_count":len(native.time_s),
            "grid_topology":"native_triangular_mesh","source_kind":native.source_kind,
            "dry_depth_threshold_m":native.dry_depth_threshold_m},
        "time_origin":native.time_origin,"time_s":list(native.time_s),
        "selected_full_run_domain":"EXISTING_DOMAIN_MINUS_VALIDATED_SECURITY_EXCLUSION",
        "domain_selection":{"choice":"EXISTING_DOMAIN_MINUS_VALIDATED_SECURITY_EXCLUSION",
            "source_geometry":"actual original D and S source geometries; revalidate from their files in M2",
            "area_m2":None,"calculation":"M2 computes area from validated D minus validated S, not a hard-coded number or bbox",
            "m1_sample_relation":"This QA fixture does not represent Qingbei D-S"},
        "viewer":{"default_profile":"native_web","profiles":{"native_web":"native_web/manifest.json",
            "compatibility_grid":"nc2web/manifest.json"}},
        "compatibility":{"profile":"legacy-structured-grid-v1-derived-from-native-triangles",
            "file":comp_path.name,"sha256":native_state["sha256"],"grid_id_digest":grid.grid_id_digest,
            "run_id":run_id,"mapping_file":"compatibility_grid.json",
            "nc2web_v1":"nc2web/manifest.json","stepjson_v1":"stepjson/index.json"},
        "native_web":{"manifest":"native_web/manifest.json","sha256":native_web["sha256"]},
        "shp_packages":shp_matrix,"legacy_optional_artifacts":legacy_optional,
        "security_exclusion":native.security_exclusion,
        "database_status":"DISABLED_NOT_REQUESTED","social_economy_status":"NOT_READ_NOT_REQUESTED",
        "source_graph_file":"provenance/source_graph.json","source_graph_sha256":_sha_file(provenance/"source_graph.json"),
        "completeness":"M1 small-data format/reader/viewer acceptance only; not a full DEM or production result"})
    mass_summary={"format":"m1-native-mass-budget-derived-v1","source_native_result_sha256":native.native_result_sha256,
        "time_s":list(native.time_s),"native_final_volume_m3":[float(row.final_volume_m3) for row in native.context.result.mass_budget],
        "compatibility_volume_check":"sum(native water_depth_m * native face_area_m2) matched each saved final-volume ledger within 1e-10 m3",
        "native_wet_area_m2":native_state["wet_area_m2"],
        "compatibility_display_wet_covered_area_m2":native_state["display_wet_covered_area_m2"],
        "rainfall_mm":native_state["rainfall_mm"],"inflow_m3_s":native_state["inflow_m3_s"],
        "outflow_m3_s":native_state["outflow_m3_s"],"production_status":"QA_FIXTURE_ONLY"}
    (output/"mass_budget").mkdir();_write_json(output/"mass_budget"/"summary.json",mass_summary)
    (output/"quality").mkdir();_write_json(output/"quality"/"compatibility_mapping.json",{
        "format":"m1-compatibility-quality-v1","grid_id_digest":grid.grid_id_digest,
        "native_active_area_m2":sum(native.face_area_m2),"covered_area_m2":grid.mapping_metadata["coverage_area_m2"],
        "area_residual_m2":grid.mapping_metadata["coverage_area_m2"]-sum(native.face_area_m2),
        "partial_display_cell_count":int(np.count_nonzero((grid.coverage_fraction>0)&(grid.coverage_fraction<1))),
        "no_coverage_cell_count":int(np.count_nonzero(grid.coverage_fraction==0)),
        "native_wet_area_m2":native_state["wet_area_m2"],
        "compatibility_display_wet_covered_area_m2":native_state["display_wet_covered_area_m2"],
        "native_topology_is_authoritative":True,"compatibility_display_is_derived":True})
    (output/"logs").mkdir();_write_json(output/"logs"/"m1_export.json",{
        "format":"m1-export-log-v1","status":"QA_FIXTURE_READY_NOT_PRODUCTION",
        "source_request":str(source_request),"run_id":run_id,"source_result_sha256":native.native_result_sha256,
        "native_frame_iterator":"PASS_SOURCE_VALIDATED_BOUNDED_M1_CAP","old_structured_codec":"PASS",
        "stepjson_v1":"PASS","native_web":"PASS","four_triangle_shp_packages":"PASS",
        "database":"DISABLED_NOT_REQUESTED","social_economy":"NOT_READ_NOT_REQUESTED"})
    parse_summary={"format":"m1-parse-summary-v1","run_id":run_id,"qa_fixture":True,
        "native_web":{"status":native_web["status"],"frames":native_web["frame_count"],"faces":native_web["face_count"]},
        "compatibility_nc":{"status":"PASS","path":comp_path.name,"sha256":native_state["sha256"]},
        "nc2web":{"status":nc2web.status,"frames":len(nc2web.frames),"variables":list(nc2web.variables)},
        "stepjson":{"status":stepjson.status,"files":len(stepjson.files),"record_format":stepjson.record_format},
        "shp_packages":shp_matrix,"qa_only":True,"full_dem_result":False}
    _write_json(output/"parse_summary.json",parse_summary)
    run_report={"format":"m1-run-report-v1","run_id":run_id,"status":"qa_fixture",
        "complete":False,"can_publish":False,"production_approved":False,
        "message":"Existing small native calculation was exported and validated; no new solver or full DEM run occurred.",
        "outputs":{"compatibility_nc":native_state["sha256"],"native_web":native_web["sha256"],
            "nc2web_manifest":_sha_file(output/"nc2web"/"manifest.json"),
            "stepjson_index":_sha_file(output/"stepjson"/"index.json")},
        "shp_packages":shp_matrix,"legacy_success_bundle_baseline":"NOT_FOUND_IN_CURRENT_RESULTS"}
    _write_json(output/"run_report.json",run_report)
    summary={"format":"m1-summary-v1","run_id":run_id,"case_id":case_id,"data_origin":"SYNTHETIC_SMALL_NATIVE_FIXTURE",
        "native_face_count":len(native.face_id),"native_node_count":len(native.node_xy_m),"saved_frames":len(native.time_s),
        "time_s":list(native.time_s),"native_source_sha256":native.native_result_sha256,
        "compatibility_grid":{"nx":grid.nx,"ny":grid.ny,"dx":grid.dx,"dy":grid.dy,"grid_id_digest":grid.grid_id_digest},
        "max_depth_m":{"saved_frame_only":True,"values":[row["max_depth_m"] for row in native_frame_stats]},
        "max_speed_m_s":{"saved_frame_only":True,"values":[row["max_speed_m_s"] for row in native_frame_stats]},
        "volume_m3":native_state["volume_m3"],"native_wet_area_m2":native_state["wet_area_m2"],
        "compatibility_display_wet_covered_area_m2":native_state["display_wet_covered_area_m2"],
        "inflow_m3_s":native_state["inflow_m3_s"],"outflow_m3_s":native_state["outflow_m3_s"],
        "rainfall_mm":native_state["rainfall_mm"],"unmapped_fields":{"eta":"NOT_AVAILABLE_LEGACY_HYDROSTATIC_FILL"},
        "database":"DISABLED_NOT_REQUESTED","social_economy":"NOT_READ_NOT_REQUESTED","production_result":False}
    _write_json(output/"summary.json",summary)
    file_rows=[]
    for path in sorted(item for item in output.rglob("*") if item.is_file() and item.name!="checksums.json"):
        file_rows.append({"path":path.relative_to(output).as_posix(),"bytes":path.stat().st_size,"sha256":_sha_file(path)})
    _write_json(output/"checksums.json",{"format":"m1-bundle-checksums-v1","run_id":run_id,
        "file_count":len(file_rows),"files":file_rows})
    return {"run_id":run_id,"output_dir":str(output),"status":"QA_FIXTURE_READY_NOT_PRODUCTION",
        "face_count":len(native.face_id),"node_count":len(native.node_xy_m),"frame_count":len(native.time_s),
        "source_result_sha256":native.native_result_sha256,"compatibility_nc_sha256":native_state["sha256"],
        "native_web_sha256":native_web["sha256"],"shp_packages":shp_matrix,
        "checksums_file_count":len(file_rows)}


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(prog="hydrodynamics-m1-output-bundle")
    parser.add_argument("--source-request",required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--output-parent")
    parser.add_argument("--cell-size-m",type=float,default=1.0)
    args=parser.parse_args(argv)
    try:
        if bool(args.output_dir)==bool(args.output_parent):raise M1BundleError("provide exactly one of --output-dir or --output-parent")
        target=Path(args.output_dir).resolve() if args.output_dir else Path(args.output_parent).resolve()/derive_m1_run_id(args.source_request)
        print(json.dumps(create_m1_output_bundle(args.source_request,target,cell_size_m=args.cell_size_m),ensure_ascii=False,sort_keys=True))
    except Exception as exc:
        print(json.dumps({"status":"failed","type":type(exc).__name__,"error":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1
    return 0


if __name__=="__main__":raise SystemExit(main())
