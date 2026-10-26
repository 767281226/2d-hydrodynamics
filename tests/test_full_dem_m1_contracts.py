"""Self-contained M1 codec, metadata, and viewer contract regressions.

Existing-result integration checks live in the local M1 acceptance suite;
this module needs no generated native result, work directory, or DEM.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import struct
import socketserver
from types import SimpleNamespace
import numpy as np
import pytest
from hydrodynamics.nc2web import FRAME_MAGIC,MESH_MAGIC as LEGACY_MESH_MAGIC,_read_hydro_values,convert_netcdf_to_step_json,convert_netcdf_to_web
from hydrodynamics.native_triangle_web import MESH_MAGIC as NATIVE_MESH_MAGIC
from hydrodynamics.viewer_server import ViewerBundleError,_ExclusiveThreadingHTTPServer,validate_bundle
ROOT=Path(__file__).resolve().parents[1]


def test_legacy_fill_zero_is_valid_only_on_available_active_cells(tmp_path):
    netcdf4=pytest.importorskip("netCDF4")
    source=tmp_path/"legacy_fill_zero.nc"
    with netcdf4.Dataset(source,"w",format="NETCDF4") as ds:
        ds.createDimension("time",1);ds.createDimension("y",1);ds.createDimension("x",4)
        ds.coordinate_system="EPSG:4548";ds.dx=1.0;ds.dy=1.0;ds.grid_spacing="1,1"
        x=ds.createVariable("x","f8",("x",));x[:]=[0.5,1.5,2.5,3.5]
        y=ds.createVariable("y","f8",("y",));y[:]=[0.5]
        active=ds.createVariable("active_mask","i1",("y","x"));active[:]=[[1,1,1,0]]
        h=ds.createVariable("water_depth","f4",("time","y","x"),fill_value=np.float32(0.0));h[:]=[[[0.25,0.25,0.25,0.0]]]
        u=ds.createVariable("velocity_x","f4",("time","y","x"),fill_value=np.float32(0.0));u.availability="AVAILABLE_DERIVED";u.missing_value=np.float32(-9999.0);u[:]=[[[0.0,-9999.0,0.0,0.0]]]
        v=ds.createVariable("velocity_y","f4",("time","y","x"),fill_value=np.float32(0.0));v.availability="AVAILABLE_DERIVED";v.missing_value=np.float32(-8888.0);v[:]=[[[0.0,0.0,-8888.0,0.0]]]
        level=ds.createVariable("water_level","f4",("time","y","x"),fill_value=np.float32(0.0));level.availability="NOT_AVAILABLE_DATUM_UNSPECIFIED";level[:]=[[[0.0,0.0,0.0,0.0]]]
    web=convert_netcdf_to_web(source,tmp_path/"legacy-web")
    raw=(tmp_path/"legacy-web"/web.frames[0]["file"]).read_bytes()
    values=np.frombuffer(raw,dtype="<f4",offset=20).reshape(4,4)
    assert values[0,:3].tolist()==pytest.approx([0.25,0.25,0.25]) and np.isnan(values[0,3])
    assert values[2,0]==0.0 and np.isnan(values[2,1]) and values[2,2]==0.0 and np.isnan(values[2,3])
    assert values[3,0]==0.0 and values[3,1]==0.0 and np.isnan(values[3,2]) and np.isnan(values[3,3])
    assert np.isnan(values[1]).all()
    steps=convert_netcdf_to_step_json(source,tmp_path/"legacy-steps")
    records=json.loads((tmp_path/"legacy-steps"/steps.files[0]).read_text(encoding="utf-8"))["all"]
    assert records==["1_0.25_0.0_180.0"]

@pytest.mark.parametrize(("extra_attrs","raw_value"),[
    ({"missing_value":0.0},0.0),
    ({"valid_range":(0.1,1.0)},0.0),
    ({"valid_min":0.1},0.0),
    ({"scale_factor":2.0},0.0),
    ({},float("nan")),
])
def test_ambiguous_legacy_zero_masks_are_not_recovered(extra_attrs,raw_value):
    variable=SimpleNamespace(_FillValue=0.0,availability="AVAILABLE_DERIVED",**extra_attrs)
    raw=np.ma.array([raw_value],mask=[True])
    values=_read_hydro_values(variable,raw,np.asarray([True]))
    assert np.isnan(values[0])

def test_explicit_m1_qa_bundle_is_viewable_but_never_latest(tmp_path):
    root=tmp_path/"qa"
    (root/"native_web").mkdir(parents=True)
    (root/"viewer.html").write_text("<html>fixture</html>",encoding="utf-8")
    (root/"native_web"/"mesh.bin").write_bytes(NATIVE_MESH_MAGIC+struct.pack("<II",3,1)+b"\0"*100)
    mesh=root/"native_web"/"mesh.bin"
    mesh_sha=hashlib.sha256(mesh.read_bytes()).hexdigest()
    frame=root/"native_web"/"frame_000000.bin";frame.write_bytes(b"f"*40)
    manifest={"format":"native-triangle-web","version":"1.0","run_id":"qa-id",
        "topology":"native_triangular_mesh","node_count":3,"face_count":1,
        "mesh_file":{"name":"mesh.bin","sha256":mesh_sha,"bytes":mesh.stat().st_size},
        "times_s":[0.0],"frames":[{"index":0,"time_s":0.0,"file":frame.name,"bytes":frame.stat().st_size}]}
    (root/"native_web"/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
    run={"format":"m1-output-bundle-v1","run_id":"qa-id","status":"QA_FIXTURE_READY_NOT_PRODUCTION",
        "data_origin":"SYNTHETIC_SMALL_NATIVE_FIXTURE","qa_fixture":True,"production_approved":False,
        "viewer":{"profiles":{"native_web":"native_web/manifest.json"}}}
    (root/"run.json").write_text(json.dumps(run),encoding="utf-8")
    report={"status":"qa_fixture","complete":False,"can_publish":False}
    (root/"run_report.json").write_text(json.dumps(report),encoding="utf-8")
    checksum_rows=[]
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        raw=path.read_bytes();checksum_rows.append({"path":path.relative_to(root).as_posix(),"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()})
    (root/"checksums.json").write_text(json.dumps({"file_count":len(checksum_rows),"files":checksum_rows}),encoding="utf-8")
    opened,validated=validate_bundle(root,explicit=True)
    assert opened==root.resolve() and validated["qa_fixture"] is True
    with pytest.raises(ViewerBundleError,match="explicit"):
        validate_bundle(root,explicit=False)

def test_partial_qa_attempt_without_report_and_checksums_is_refused(tmp_path):
    root=tmp_path/"partial";root.mkdir()
    (root/"viewer.html").write_text("<html>partial</html>",encoding="utf-8")
    (root/"run.json").write_text(json.dumps({"format":"m1-output-bundle-v1","run_id":"m1qa-000000000000-00000000",
        "status":"QA_FIXTURE_READY_NOT_PRODUCTION","qa_fixture":True,"data_origin":"SYNTHETIC_SMALL_NATIVE_FIXTURE",
        "production_approved":False,"viewer":{"profiles":{}}}),encoding="utf-8")
    with pytest.raises(ViewerBundleError,match="QA fixtures"):
        validate_bundle(root,explicit=True)

def test_legacy_structured_bundle_remains_an_explicit_compatibility_view(tmp_path):
    root=tmp_path/"legacy_qa";(root/"nc2web").mkdir(parents=True)
    (root/"viewer.html").write_text("<html>legacy display</html>",encoding="utf-8")
    mesh=(LEGACY_MESH_MAGIC+struct.pack("<IIfff",4,2,0.0,0.0,0.001)+
        np.asarray([0,1000,0,1000],dtype="<i4").tobytes()+
        np.asarray([0,0,1000,1000],dtype="<i4").tobytes()+
        np.asarray([[0,1,2],[1,3,2]],dtype="<u4").tobytes())
    (root/"nc2web"/"mesh.bin").write_bytes(mesh)
    frame=FRAME_MAGIC+struct.pack("<III",1,1,3)+np.asarray([0.1,0.2,0.0],dtype="<f4").tobytes()
    (root/"nc2web"/"frame_000.bin").write_bytes(frame)
    manifest={"format":"nc2web","version":"1.0","status":"pass","nx":1,"ny":1,
        "times":[0.0],"variables":["water_depth","velocity_x","velocity_y"],
        "frames":[{"index":0,"time":0.0,"file":"frame_000.bin"}]}
    (root/"nc2web"/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
    report={"status":"qa_fixture","qa_fixture":True,"complete":False,"can_publish":False,
        "data_origin":"SYNTHETIC_SMALL_NATIVE_FIXTURE","run_id":"legacy-qa"}
    (root/"run_report.json").write_text(json.dumps(report),encoding="utf-8")
    opened,bootstrap=validate_bundle(root,explicit=True)
    assert opened==root.resolve() and bootstrap["legacy_structured"] is True
    assert bootstrap["viewer"]["default_profile"]=="compatibility_grid"
    with pytest.raises(ViewerBundleError,match="explicit"):
        validate_bundle(root,explicit=False)

def test_actual_historical_workflow_run_json_is_normalized_without_approval(tmp_path):
    root=tmp_path/"legacy_workflow";(root/"nc2web").mkdir(parents=True)
    (root/"viewer.html").write_text("<html>legacy workflow</html>",encoding="utf-8")
    (root/"nc2web"/"mesh.bin").write_bytes(LEGACY_MESH_MAGIC+struct.pack("<IIfff",4,2,0.0,0.0,0.001)+
        np.asarray([0,1000,0,1000],dtype="<i4").tobytes()+np.asarray([0,0,1000,1000],dtype="<i4").tobytes()+
        np.asarray([[0,1,2],[1,3,2]],dtype="<u4").tobytes())
    frame=FRAME_MAGIC+struct.pack("<III",1,1,3)+np.asarray([0.1,0.0,0.0],dtype="<f4").tobytes()
    (root/"nc2web"/"frame_000.bin").write_bytes(frame)
    compat={"format":"nc2web","version":"1.0","status":"pass","nx":1,"ny":1,"times":[0.0],
        "variables":["water_depth","velocity_x","velocity_y"],"frames":[{"index":0,"time":0.0,"file":"frame_000.bin"}]}
    (root/"nc2web"/"manifest.json").write_text(json.dumps(compat),encoding="utf-8")
    metadata={"id":"old-workflow-20261026","script_id":"old-workflow-20261026","out_dir":str(root.parent),
        "bundle_dir":str(root.resolve()),"engine_mode":"hourly_inundation","grid_topology":"structured_grid",
        "shp_status":"not_applicable","shp_reason":"native triangular result required"}
    metadata_raw=json.dumps(metadata,indent=2)
    (root/"run.json").write_text(metadata_raw,encoding="utf-8")
    report={"format":"hydrodynamics-workflow-run-report-v1","schema_version":"1.1","engine_mode":"hourly_inundation",
        "grid_topology":"structured_grid","id":metadata["id"],"script_id":metadata["script_id"],"status":"complete",
        "started_at":"2026-10-26T00:00:00+08:00","workflow_file":"workflow.yaml","workflow_spec":{},
        "bundle_dir":metadata["bundle_dir"],"result_file":str(root/"legacy.nc"),
        "workflow":{"nc2web":{"status":"complete"},"stepjson":{"status":"complete"},"viewer":{"status":"complete"},
            "validate":{"status":"complete"},"shp":{"status":"not_applicable"}}}
    (root/"run_report.json").write_text(json.dumps(report),encoding="utf-8")
    opened,normalized=validate_bundle(root,explicit=True)
    assert opened==root.resolve()
    assert normalized["legacy_structured"] is True and normalized["normalized_from_legacy_workflow"] is True
    assert normalized["run_id"]==metadata["script_id"]
    assert "production_approved" not in normalized
    assert (root/"run.json").read_text(encoding="utf-8")==metadata_raw
    with pytest.raises(ViewerBundleError,match="explicit path"):
        validate_bundle(root,explicit=False)
    failed={**report,"status":"failed"}
    (root/"run_report.json").write_text(json.dumps(failed),encoding="utf-8")
    with pytest.raises(ViewerBundleError,match="failed"):
        validate_bundle(root,explicit=True)

def test_viewer_server_binds_ports_exclusively():
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):pass
    first=_ExclusiveThreadingHTTPServer(("127.0.0.1",0),Handler)
    port=first.server_address[1]
    try:
        with pytest.raises(OSError):_ExclusiveThreadingHTTPServer(("127.0.0.1",port),Handler)
        second=_ExclusiveThreadingHTTPServer(("127.0.0.1",port+1),Handler)
        try:assert second.server_address[1]==port+1
        finally:second.server_close()
    finally:first.server_close()

def test_m1_schemas_examples_and_domain_selection_are_synchronized():
    example_dir=ROOT/"examples/native_triangle_shp"
    native_schema=json.loads((example_dir/"native_web_manifest.schema.json").read_text(encoding="utf-8"))
    grid_schema=json.loads((example_dir/"compatibility_grid.schema.json").read_text(encoding="utf-8"))
    nc_schema=json.loads((example_dir/"nc2web_v1_manifest.schema.json").read_text(encoding="utf-8"))
    step_schema=json.loads((example_dir/"stepjson_v1_schema.json").read_text(encoding="utf-8"))
    bundle_schema=json.loads((example_dir/"m1_output_bundle.schema.json").read_text(encoding="utf-8"))
    native=json.loads((example_dir/"native_web_manifest.example.json").read_text(encoding="utf-8"))
    grid=json.loads((example_dir/"compatibility_grid.example.json").read_text(encoding="utf-8"))
    nc=json.loads((example_dir/"nc2web_v1_manifest.example.json").read_text(encoding="utf-8"))
    step_index=json.loads((example_dir/"stepjson_v1_index.example.json").read_text(encoding="utf-8"))
    step=json.loads((example_dir/"stepjson_v1_frame.example.json").read_text(encoding="utf-8"))
    bundle=json.loads((example_dir/"m1_output_bundle.example.json").read_text(encoding="utf-8"))
    assert native_schema["properties"]["format"]["const"]==native["format"]=="native-triangle-web"
    assert set(native_schema["required"]).issubset(native)
    assert grid_schema["properties"]["format"]["const"]==grid["format"]=="hydrodynamics-legacy-grid-compatibility-v1"
    assert set(grid_schema["required"]).issubset(grid)
    assert any(0<f<1 for row in grid["active_coverage_fraction"] for f in row)
    assert nc_schema["properties"]["format"]["const"]==nc["format"]=="nc2web"
    assert set(nc_schema["required"]).issubset(nc)
    assert step_schema["properties"]["record_format"]["const"]==step_index["record_format"]
    assert set(step_schema["required"]).issubset(step_index)
    assert list(step)==["all"] and all(type(row) is str for row in step["all"])
    assert bundle_schema["properties"]["status"]["const"]==bundle["status"]=="QA_FIXTURE_READY_NOT_PRODUCTION"
    assert set(bundle_schema["required"]).issubset(bundle)
    assert bundle["domain_selection"]["choice"]=="EXISTING_DOMAIN_MINUS_VALIDATED_SECURITY_EXCLUSION"
    assert bundle["domain_selection"]["area_m2"] is None
