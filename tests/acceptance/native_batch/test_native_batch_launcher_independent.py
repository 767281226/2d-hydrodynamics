from pathlib import Path
import json
import math
import os
import re
import shutil
import subprocess

import pytest
import yaml

from _native_batch_probe import ROOT, BATCH, invoke, snapshot, assert_protected_baseline


@pytest.fixture(scope='module')
def actual_batch(tmp_path_factory):
    base=tmp_path_factory.mktemp('independent-bat')
    caller=base/'外部 调用目录';caller.mkdir()
    output=base/'原生 输出目录'
    execution=invoke(BATCH,['--script-id','independent-native-20261020','--out-dir',output],caller,timeout=180)
    assert execution.returncode==0,execution.stdout+execution.stderr
    return output,execution


def test_actual_bat_completes_real_native_source_pipeline(actual_batch):
    output,execution=actual_batch
    report=json.loads((output/'run_report.json').read_text(encoding='utf-8'))
    assert report['format']=='native-batch-run-report-v1'
    assert report['status']=='complete' and report['mode']=='synthetic_demo'
    assert report['data_origin']=='SYNTHETIC_ONLY'
    assert report['script_id']=='independent-native-20261020'
    assert report['grid_topology']=='native_triangular_mesh'
    assert not report['preflight_only'] and report['execution_available']
    assert not report['failed_stage'] and not report['error']
    stages=report['stages']
    assert len(stages)>=10 and all(s['status']=='complete' and s['exit_code']==0 for s in stages)
    assert any(s['name']=='combined_source_run' for s in stages)
    assert all('hourly_inundation' not in str(s['command']) for s in stages)
    assert len(report['copied_inputs'])==11
    assert all(Path(report['output_directory']).resolve()==output.resolve() for _ in [0])
    assert Path(report['python_executable']).resolve()==(ROOT/'.venv/Scripts/python.exe').resolve()
    assert not list(output.rglob('base_none_dynamic.nc'))
    assert 'SYNTHETIC_ONLY' in execution.stdout
    assert_protected_baseline()


def test_native_values_geometry_and_shp_are_actual_calculation_faces(actual_batch):
    import shapefile
    from shapely.geometry import Polygon,box
    from hydrodynamics.triangular_source_netcdf import read_triangular_source_result
    from hydrodynamics.triangular_shp_export import validate_native_source_triangle_shp_package
    output,_=actual_batch
    case=output/'inputs/security_exclusion'
    result=read_triangular_source_result(case/'combined.nc',case/'combined_run.yaml',enforce_configured_result_path=True)
    assert list(result.face_id)==[101,102,201,202]
    assert list(result.time_s)==[0.,.05,.1]
    assert result.nonzero_manning_effect_applied and result.rainfall_nonzero_effect_applied
    triangles=[Polygon([result.node_xy_m[i] for i in nodes]) for nodes in result.face_nodes]
    assert all(len(p.exterior.coords)==4 for p in triangles)
    assert all(p.intersection(box(1.5,0,2.5,2)).area<1e-12 for p in triangles)
    areas=[p.area for p in triangles]
    assert areas==pytest.approx([1.5]*4,abs=1e-12)
    volumes=[math.fsum(h*a for h,a in zip(row,areas)) for row in result.water_depth_m]
    assert volumes==pytest.approx([.9,.90003,.90006],abs=1e-12)
    accounting=result.security_rainfall_accounting
    assert accounting.accepted_active_rainfall_volume_m3==pytest.approx(.00006,abs=1e-12)
    assert accounting.security_excluded_unmodeled_rainfall_volume_m3==pytest.approx(.00002,abs=1e-12)
    assert not accounting.excluded_rain_is_accepted_budget_loss
    assert not accounting.automatic_flow_transfer_from_excluded_area
    assert max(abs(b.residual_m3) for b in result.mass_budget)<1e-12
    for request,directory in [('export_combined_v2.yaml','combined_native_shp_v2'),('export_frame_native_v2.yaml','combined_frame_native')]:
        assert validate_native_source_triangle_shp_package(case/request).valid
        package=case/directory
        assert all((package/('faces'+suffix)).is_file() for suffix in ['.shp','.shx','.dbf','.prj','.cpg'])
        with shapefile.Reader(str(package/'faces.shp')) as reader:
            records=list(reader.iterShapeRecords())
        assert {str(r.record.face_id) for r in records}=={'101','102','201','202'}
        assert all(len(r.shape.points)==4 and len(r.shape.parts)==1 for r in records)
        if directory=='combined_frame_native':
            by_id={str(r.record.face_id):Polygon(r.shape.points) for r in records}
            for face,p in zip(result.face_id,triangles):
                assert by_id[str(face)].symmetric_difference(p).area<1e-12
    summary=json.loads((output/'summary.json').read_text(encoding='utf-8'))
    assert summary['active_domain_area_m2']==pytest.approx(6.)
    assert summary['security_excluded_area_m2']==pytest.approx(2.)
    assert summary['frame_integrated_volumes_m3']==pytest.approx(volumes,abs=1e-12)


def test_repeated_run_preserves_every_existing_byte(actual_batch,tmp_path):
    output,_=actual_batch
    before=snapshot(output)
    caller=tmp_path/'repeat caller';caller.mkdir()
    execution=invoke(BATCH,['--id','different-id','--out-dir',output],caller)
    assert execution.returncode!=0
    assert snapshot(output)==before


def test_manual_bat_without_arguments_uses_new_default_id_and_directory(tmp_path):
    results=ROOT/'work/results'
    before={p.name for p in results.iterdir()} if results.is_dir() else set()
    caller=tmp_path/'默认 手动调用';caller.mkdir()
    execution=invoke(BATCH,[],caller,timeout=180)
    assert execution.returncode==0,execution.stdout+execution.stderr
    created=[p for p in results.iterdir() if p.name not in before]
    assert len(created)==1
    output=created[0]
    assert re.fullmatch(r'\d{8}-\d{18,}',output.name)
    report=json.loads((output/'run_report.json').read_text(encoding='utf-8'))
    assert report['script_id']==output.name
    assert Path(report['output_directory']).resolve()==output.resolve()
    assert report['status']=='complete' and report['data_origin']=='SYNTHETIC_ONLY'
    assert (output/'summary.json').is_file()
    assert_protected_baseline()


def test_actual_batch_script_path_with_chinese_and_spaces(tmp_path):
    alias=tmp_path/'项目 中文 空格路径'
    created=subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(alias),str(ROOT)],capture_output=True,text=True)
    assert created.returncode==0,created.stdout+created.stderr
    caller=tmp_path/'caller';caller.mkdir()
    output=tmp_path/'script-path-output'
    try:
        execution=invoke(alias/BATCH.name,['--id','independent-script-path','--out-dir',output],caller,timeout=180)
        assert execution.returncode==0,execution.stdout+execution.stderr
        report=json.loads((output/'run_report.json').read_text(encoding='utf-8'))
        assert report['status']=='complete' and report['data_origin']=='SYNTHETIC_ONLY'
        assert_protected_baseline()
    finally:
        os.rmdir(alias)


@pytest.mark.parametrize('args',[
    ['--id','../invalid'],['--id','one','--script-id','two'],['--not-a-supported-flag'],
    ['--engineering-config','unselected.yaml'],['--engineering','--config','wrong.yaml'],
])
def test_bad_cli_never_claims_complete_or_creates_target(args,tmp_path):
    caller=tmp_path/'caller';caller.mkdir()
    target=tmp_path/'must-not-exist'
    execution=invoke(BATCH,[*args,'--out-dir',target],caller)
    assert execution.returncode!=0
    assert not target.exists()


def test_default_engineering_bat_stops_with_specific_blockers(tmp_path):
    caller=tmp_path/'工程 预检调用';caller.mkdir()
    target=tmp_path/'preflight'
    execution=invoke(BATCH,['--engineering','--out-dir',target],caller)
    assert execution.returncode!=0
    report=json.loads((target/'run_report.json').read_text(encoding='utf-8'))
    assert report['status']=='blocked'
    assert report['mode']=='engineering_preflight' and report['preflight_only']
    assert not report['execution_available']
    assert re.fullmatch(r'\d{8}-\d{18,}',report['script_id'])
    codes={b['code'] for b in report['blockers']}
    assert {'UNSUPPORTED_OPEN_BOUNDARY_EXECUTION','ENGINEERING_EXECUTION_NOT_IN_SCOPE','MISSING_ORIGINAL_DOMAIN_D','MISSING_DEM_VERTICAL_DATUM'}<=codes
    assert all(b['field'] and b['message'] and b['next_action'] for b in report['blockers'])
    assert all(not c['content_read'] for c in report['input_path_checks'])
    assert not list(target.rglob('*.nc')) and not list(target.rglob('*.shp'))
    assert all('source_run' not in s['name'] for s in report['stages'])
    assert_protected_baseline()


@pytest.mark.parametrize('tail',['fresh-output','../fresh-output'])
def test_windows_junction_lexical_parent_is_rejected(tmp_path,tail):
    from hydrodynamics import native_batch_launcher as launcher
    actual=tmp_path/'actual';actual.mkdir()
    alias=tmp_path/'junction'
    created=subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(alias),str(actual)],capture_output=True,text=True)
    assert created.returncode==0,created.stdout+created.stderr
    try:
        with pytest.raises(launcher.NativeBatchError):
            launcher._output_directory(ROOT,str(alias/tail),'independent-junction')
        assert list(actual.iterdir())==[]
    finally:
        os.rmdir(alias)


def test_missing_project_python_actionable_without_installing(tmp_path):
    isolated=tmp_path/'缺少 Python 项目';isolated.mkdir()
    isolated_batch=isolated/BATCH.name;shutil.copyfile(BATCH,isolated_batch)
    caller=tmp_path/'caller';caller.mkdir()
    execution=invoke(isolated_batch,[],caller,env={'PATH':os.environ['SystemRoot']+'\\System32'})
    assert execution.returncode!=0
    assert 'venv' in (execution.stdout+execution.stderr).lower() or 'python' in (execution.stdout+execution.stderr).lower()


def test_missing_dependency_returns_actionable_error_without_installing(monkeypatch):
    from hydrodynamics import native_batch_launcher as launcher
    original=launcher.importlib.util.find_spec
    monkeypatch.setattr(launcher.importlib.util,'find_spec',lambda name:None if name=='netCDF4' else original(name))
    with pytest.raises(launcher.NativeBatchError) as caught:
        launcher._project_python(ROOT)
    error=str(caught.value)
    assert 'netCDF4' in error and 'pip install' in error


def test_filled_engineering_declarations_still_never_read_data_or_compute(tmp_path,monkeypatch):
    from hydrodynamics import native_batch_launcher as launcher
    data=yaml.safe_load((ROOT/'examples/case_001/native_engineering_preflight.yaml').read_text(encoding='utf-8'))
    sentinel=tmp_path/'opaque-engineering-data.tif';sentinel.write_bytes(b'not an actual raster or native result')
    for field in ['original_domain_geometry_file','dem_source_file','dem_nodata_mask_file','dem_coverage_evidence_file','native_preparation_config_file','native_mesh_artifact_file','p3_business_artifact_file','native_run_config_file','native_source_config_file','native_capability_validation_file']:
        data[field]=str(sentinel)
    data.update(original_domain_crs='EPSG:4548',mesh_coordinate_system='EPSG:4548',dem_source_crs='EPSG:4548',dem_vertical_unit='m',dem_vertical_datum='independent test declaration')
    data['initial_condition']={'source_file':str(sentinel),'quantity':'depth','unit':'m','vertical_datum':'depth datum independent'}
    data['roughness']={'source_file':str(sentinel),'source_unit':'1','physical_unit':'s/m^(1/3)','interpretation_policy':'legacy_unit1_as_si_manning','rationale':'independent declared provenance'}
    data['rainfall']={'source_file':str(sentinel),'input_unit':'m/s','intervals_file':str(sentinel)}
    data['boundary']={'direction':'inflow_and_outflow','inflow_file':str(sentinel),'outflow_file':str(sentinel),'group_mapping_file':str(sentinel),'provenance_file':str(sentinel),'time_origin':'2023-08-01T02:00:00+08:00','time_unit':'s','discharge_unit':'m3/s','vertical_datum':'independent declared datum'}
    data['security_exclusion']['source_file']=sentinel.name
    config=tmp_path/'filled.yaml';config.write_text(yaml.safe_dump(data),encoding='utf-8')
    original_open=Path.open
    def guarded_open(self,*args,**kwargs):
        assert self.resolve()!=sentinel.resolve(),'engineering preflight opened a data payload'
        return original_open(self,*args,**kwargs)
    def forbidden_stage(*args,**kwargs):
        raise AssertionError('engineering preflight invoked a numeric or export stage')
    monkeypatch.setattr(Path,'open',guarded_open)
    monkeypatch.setattr(launcher,'_run_cli_stage',forbidden_stage)
    output=tmp_path/'filled-preflight'
    code=launcher.main(['--engineering','--engineering-config',str(config),'--out-dir',str(output),'--id','independent-filled'])
    assert code!=0
    report=json.loads((output/'run_report.json').read_text(encoding='utf-8'))
    assert report['status']=='blocked' and not report['execution_available']
    assert all(check['exists'] and not check['content_read'] for check in report['input_path_checks'])
    assert {b['code'] for b in report['blockers']}=={'UNSUPPORTED_OPEN_BOUNDARY_EXECUTION','ENGINEERING_EXECUTION_NOT_IN_SCOPE'}


def test_actual_child_stage_failure_is_reported_without_success(tmp_path,monkeypatch):
    from hydrodynamics import native_batch_launcher as launcher
    calls=[]
    def child_failure(command,**kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command,7,stdout='',stderr='independent controlled child failure')
    monkeypatch.setattr(launcher.subprocess,'run',child_failure)
    output=tmp_path/'failed-child'
    code=launcher.main(['--id','independent-failure','--out-dir',str(output)])
    assert code!=0 and len(calls)==1
    report=json.loads((output/'run_report.json').read_text(encoding='utf-8'))
    assert report['status']=='failed' and report['failed_stage']=='p2_mesh_preparation'
    assert report['stages'][-1]['exit_code']==7
    assert 'controlled child failure' in report['error']
    assert not (output/'summary.json').exists()
    assert not list(output.rglob('*.nc')) and not list(output.rglob('manifest.json'))


@pytest.mark.parametrize('field,value',[('maximum_face_count',65),('maximum_duration_s',2.),('maximum_saved_frame_count','3'),('maximum_face_count',True)])
def test_launch_input_contract_rejects_out_of_scope_budgets(field,value):
    from hydrodynamics.native_batch_launcher import NativeBatchLaunchConfig
    from pydantic import ValidationError
    data={'format':'native-batch-launch-config-v1','schema_version':'1.0','mode':'synthetic_demo','data_origin':'SYNTHETIC_ONLY','case_id':'security-exclusion-synthetic-dynamic-v1','source_case_directory':'../security_exclusion'}
    data[field]=value
    with pytest.raises(ValidationError):NativeBatchLaunchConfig.model_validate(data)
