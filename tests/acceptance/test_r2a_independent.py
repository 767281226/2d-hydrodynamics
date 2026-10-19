from pathlib import Path
import hashlib
import json
import sys
import numpy as np
import pytest
import yaml
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

@pytest.fixture
def source(tmp_path):
    from hydrodynamics.triangular_mesh import TriangularMesh,FaceTerrainField
    from hydrodynamics.mesh_preparation import write_ugrid_preparation
    from hydrodynamics.triangular_business import TriangularBusinessPackage,TriangularInitialConditionField,TriangularRoughnessField,write_triangular_business_package
    from hydrodynamics.triangular_solver import run_triangular_solver
    from hydrodynamics.rehearsal_package import create_local_execution_envelope
    profile_raw=(ROOT/'hydrodynamics/rehearsal_data/r2a_flat_bed_closed_v1.json').read_bytes()
    assert hashlib.sha256(profile_raw).hexdigest()=='52589d5056c5e3b246c95c7affbc8ca3a0e43f63b3d8ac8db05dd9e43e21ea8b'
    profile=json.loads(profile_raw)
    m=profile['mesh']; t=profile['terrain']
    mesh=TriangularMesh(m['node_xy_m'],m['face_nodes'],m['coordinate_system'],mesh_id=m['mesh_id'],face_id=m['face_id'],boundary_groups=m['boundary_groups'],constraint_groups=m['constraint_groups'])
    terrain=FaceTerrainField(mesh_id=mesh.mesh_id,elevation_m=tuple(t['elevation_m']),valid_mask=(True,True),covered_area_m2=tuple(mesh.face_area),valid_area_m2=tuple(mesh.face_area),coverage_ratio=(1.,1.),coordinate_system=mesh.coordinate_system,vertical_datum=t['vertical_datum'],vertical_datum_source=t['vertical_datum_source'])
    mesh_path=tmp_path/'mesh.nc';write_ugrid_preparation(mesh_path,mesh,terrain)
    initial=TriangularInitialConditionField.from_assignments(mesh,terrain,'depth',tuple(zip(mesh.face_id,[.2,.2])),source='independent frozen reference')
    business=TriangularBusinessPackage(mesh,terrain,TriangularRoughnessField.from_constant(mesh,.035,source='independent provenance'),initial,())
    write_triangular_business_package(tmp_path/'business.json',business,mesh_artifact=mesh_path)
    run={'format':'triangular-solver-run-v1','schema_version':'1.0','case_id':profile['model_case_id'],'business_file':'business.json','result_file':'result.nc','time_origin':None,'output_times_s':[0.,.5,1.],'boundary_policy':'closed_wall','initial_momentum':'zero','sources':'none','gravity_m_s2':9.81,'cfl_number':.25,'minimum_time_step_s':1e-6,'maximum_time_step_s':.1,'maximum_step_retries':8}
    (tmp_path/'run.yaml').write_text(yaml.safe_dump(run),encoding='utf-8')
    assignment={'format':'rehearsal-local-dispatch-assignment-v1','assignment_basis':'LOCAL_SYNTHETIC_TEST_CONTEXT','tenant_id':'independent-tenant','project_id':'independent-project','scenario_id':'independent-scenario','scenario_version_id':'independent-version','platform_run_id':'independent-run','attempt_id':'independent-attempt-1','attempt_no':1,'script_id':'20261019-1792400000000000000','local_run_id':'20261019-1792400000000000000','model_case_id':profile['model_case_id'],'scope_id':'R2A_SYNTHETIC_CLOSED_SCOPE'}
    (tmp_path/'assignment.json').write_text(json.dumps(assignment),encoding='utf-8')
    request={'format':'rehearsal-result-package-export-request-v1','schema_version':'1.0','run_config_file':'run.yaml','dispatch_assignment_file':'assignment.json','execution_envelope_file':'envelope.json','output_directory':'package','scope_id':'R2A_SYNTHETIC_CLOSED_SCOPE','metric_policy_version':'r2a-static-flat-bed-v1','requested_products':[{'product_code':'water_depth','required':True,'scope_id':'R2A_SYNTHETIC_CLOSED_SCOPE','aggregation':'FRAME'},{'product_code':'maximum_depth','required':True,'scope_id':'R2A_SYNTHETIC_CLOSED_SCOPE','aggregation':'MAX_OVER_WINDOW'}],'require_complete':False}
    request_path=tmp_path/'request.yaml';request_path.write_text(yaml.safe_dump(request),encoding='utf-8')
    create_local_execution_envelope(request_path)
    run_triangular_solver(tmp_path/'run.yaml')
    return {'root':tmp_path,'request_path':request_path,'request':request}


def test_independent_static_reference_and_read_only_sources(source):
    from hydrodynamics.rehearsal_package import export_result_package,validate_result_package
    root=source['root']; paths=[p for p in root.iterdir() if p.is_file()];before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report=export_result_package(source['request_path'])
    assert report.valid and not report.complete
    pkg=root/'package';manifest=json.loads((pkg/'manifest.json').read_bytes());frames=json.loads((pkg/'frames.json').read_bytes());summary=json.loads((pkg/'summary.json').read_bytes())
    assert manifest['can_publish'] is False and manifest['engineering_validated'] is False
    assert manifest['identity']['identity_authentication_status']=='NOT_ASSESSED'
    assert manifest['identity']['native_identity_basis']=='OUTER_EXECUTION_CONTEXT_ONLY'
    assert frames['frame_times_s']==[0.,.5,1.]
    assert len(frames['frames'])==3
    for frame in frames['frames']:
        assert abs(frame['maximum_depth_m']-.2)<1e-12
        assert abs(frame['water_volume_m3']-.4)<1e-12
    assert abs(summary['total_face_area_m2']-2.)<1e-12
    assert abs(summary['maximum_depth_m']-.2)<1e-12
    import netCDF4
    with netCDF4.Dataset(root/'result.nc') as ds:
        actual=np.asarray(ds.variables['water_depth'][:])
        times=np.asarray(ds.variables['time'][:])
        expected_time=float(times[np.flatnonzero(np.max(actual,axis=1)==np.max(actual))[0]])
    assert summary['maximum_depth_time_s']==expected_time
    assert before=={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    assert validate_result_package(source['request_path']).valid


@pytest.mark.parametrize('command',['export','validate'])
def test_request_require_complete_blocks_cli_without_duplicate_flag(source,command):
    import hydrodynamics.rehearsal_package as package
    source['request']['require_complete']=True
    source['request_path'].write_text(yaml.safe_dump(source['request']),encoding='utf-8')
    if command=='validate':package.export_result_package(source['request_path'])
    assert package.main([command,'--request',str(source['request_path'])])==3


def test_rehashed_false_metric_is_rejected(source):
    import hydrodynamics.rehearsal_package as package
    package.export_result_package(source['request_path']);pkg=source['root']/'package'
    p=pkg/'frames.json';data=json.loads(p.read_bytes());data['frames'][0]['water_volume_m3']=999.;p.write_text(json.dumps(data),encoding='utf-8')
    m=pkg/'manifest.json';manifest=json.loads(m.read_bytes())
    for item in manifest['files']:
        if item['key']=='frames.json':item.update(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),byte_count=len(p.read_bytes()))
    m.write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises((ValueError,RuntimeError)):package.validate_result_package(source['request_path'])


def test_worker_cannot_replace_attempt(source):
    import hydrodynamics.rehearsal_package as package
    p=source['root']/'envelope.json';data=json.loads(p.read_bytes());data['attempt_id']='forged-attempt';data['attempt_no']=2;p.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises((ValueError,RuntimeError)):package.export_result_package(source['request_path'])
    assert not (source['root']/'package').exists()


def test_missing_original_source_graph_is_rejected(source):
    import hydrodynamics.rehearsal_package as package
    package.export_result_package(source['request_path']);(source['root']/'mesh.nc').unlink()
    with pytest.raises((ValueError,RuntimeError)):package.validate_result_package(source['request_path'])


def test_reported_export_failure_does_not_leave_success_manifest(source,monkeypatch):
    import hydrodynamics.rehearsal_package as package
    _add_bound_g1(source)
    real_validate=package.validate_result_package
    destination=source['root']/'package'
    def fail_after_exposure(request_path,package_directory=None,**kwargs):
        if package_directory is not None and Path(package_directory).resolve()==destination.resolve():
            raise package.RehearsalPackageError('injected final source revalidation failure')
        return real_validate(request_path,package_directory,**kwargs)
    monkeypatch.setattr(package,'validate_result_package',fail_after_exposure)
    try:package.export_result_package(source['request_path'])
    except (ValueError,RuntimeError,OSError):
        assert not (destination/'manifest.json').exists(), 'failed export left a manifest that looks successful'


def test_complete_g1_keeps_original_bytes_and_native_identity_limit(source):
    import hydrodynamics.rehearsal_package as package
    from hydrodynamics.gate_dispatch import gate_dispatch_paths
    native=source['root']/'result.nc';before=native.read_bytes()
    _add_bound_g1(source)
    csv_path,json_path=gate_dispatch_paths(native);csv=csv_path.read_bytes();raw_json=json_path.read_bytes()
    report=package.export_result_package(source['request_path']);assert report.valid and report.complete
    pkg=source['root']/'package';assert (pkg/'audit/gate_dispatch.csv').read_bytes()==csv;assert (pkg/'audit/gate_dispatch.json').read_bytes()==raw_json
    assert native.read_bytes()==before
    payload=json.loads((pkg/'audit/gate_dispatch.json').read_bytes());assert Path(payload['result_file']).resolve()==native.resolve()
    manifest=json.loads((pkg/'manifest.json').read_bytes());assert manifest['can_publish'] is False
    assert manifest['identity']['native_identity_basis']=='OUTER_EXECUTION_CONTEXT_ONLY'


def _add_bound_g1(source):
    from hydrodynamics.triangular_solver_netcdf import read_triangular_result
    from hydrodynamics.gate_dispatch import GateDispatchInput,build_gate_dispatch_log,write_gate_dispatch_log
    native=source['root']/'result.nc'
    actual=read_triangular_result(native,source['root']/'run.yaml')
    log=build_gate_dispatch_log(actual.time_s,GateDispatchInput(),result_context={'format':'triangular-solver-result-v1','case_id':actual.case_id,'script_id':'20261019-1792400000000000000'})
    write_gate_dispatch_log(native,log)
