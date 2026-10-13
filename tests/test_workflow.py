from pathlib import Path
import csv
import json
import struct

import numpy as np
import pytest
from pydantic import ValidationError
import yaml

from hydrodynamics.config import load_config
from hydrodynamics import workflow
from hydrodynamics.workflow_config import WorkflowConfig, WorkflowError, validate_run_id
from hydrodynamics.workflow_inputs import load_prepared_workflow_grid, load_workflow_forcing


@pytest.fixture
def job(tmp_path):
    case = {
        'model': {'name':'tiny','end_time':7200,'coordinate_system':'EPSG:4548'},
        'domain': {'xmin':474150,'xmax':474300,'ymin':4299600,'ymax':4299700,'dx':50,'dy':50},
        'terrain': {'type':'constant','elevation':0},
        'initial_condition': {'type':'constant_depth','depth':0},
        'roughness': {'type':'constant','manning_n':0.035},
        'boundary': [{'id':'in','location':'west','type':'discharge','value':1}],
        'source': {'rainfall':{'enabled':False}},
        'numerics': {},
        'validation': {'enabled':False},
        'output': {'interval':3600},
    }
    (tmp_path/'case.yaml').write_text(yaml.safe_dump(case),encoding='utf-8')
    active = np.array([[True,True,True],[True,True,False]])
    zones = np.where(active,'qingbei','qingnan')
    pack = dict(elevation=np.zeros((2,3)), active_mask=active, combined_mask=np.ones((2,3),bool),
                inlet_mask=np.array([[True,False,False],[True,False,False]]),
                conveyance_mask=np.zeros((2,3),bool), zones=zones,
                x=np.array([474175.,474225.,474275.]),y=np.array([4299625.,4299675.]),
                coordinate_system='EPSG:4548',vertical_datum='1985国家高程基准',elevation_unit='m',
                provenance='synthetic pipeline fixture')
    np.savez(tmp_path/'grid.npz',**pack)
    rows = [[0,1,.1,0,.1,0],[3600,1,.3,0,.1,.2],[7200,1,.3,0,.1,.2]]
    columns = ['time_s','inflow_m3s','outflow_m3s','rainfall_mm_per_step','duliu_component_m3s','xihe_component_m3s']
    def write_forcing(new_rows):
        with (tmp_path/'forcing.csv').open('w',newline='',encoding='utf-8') as stream:
            writer=csv.writer(stream);writer.writerow(columns);writer.writerows(new_rows)
    write_forcing(rows)
    spec = {'case_config':'case.yaml','prepared_grid':'grid.npz',
            'forcing':{'file':'forcing.csv','source':'synthetic fixture'},
            'time_origin':'2023-08-01T02:00:00+08:00','output_directory':'runs'}
    job_file=tmp_path/'workflow.yaml'
    job_file.write_text(yaml.safe_dump(spec),encoding='utf-8')
    return {'root':tmp_path,'file':job_file,'case':case,'pack':pack,'spec':spec,'rows':rows,'write_forcing':write_forcing}


def test_complete_command_generates_all_outputs_in_one_run(job,monkeypatch):
    pytest.importorskip('netCDF4');pytest.importorskip('pyproj')
    monkeypatch.chdir(job['root'])
    assert workflow.main(['--workflow',str(job['file']),'--id','full']) == 0
    bundle=job['root']/'runs/full'
    report=json.loads((bundle/'run_report.json').read_text(encoding='utf-8'))
    assert report['status']=='complete'
    assert all(v['status']=='complete' for k,v in report['workflow'].items() if k!='socio_overlay')
    assert report['workflow']['socio_overlay']['status']=='skipped'
    assert len(list((bundle/'nc2web').glob('frame_*.bin')))==3
    assert len(list((bundle/'stepjson').glob('step_*.json')))==3
    assert len(list((bundle/'shp').glob('*.shp')))==4
    assert (bundle/'viewer.html').is_file() and (bundle/'run.json').is_file()
    assert report['workflow']['gate_dispatch']['reported_operations']==1
    journal=json.loads((bundle/'result_gate_dispatch.json').read_text(encoding='utf-8'))
    event=next(r for r in journal['records'] if r['record_type']=='operation')
    assert event['discharge_after_m3s']==.2 and event['rule_status']=='indeterminate'
    assert event['operation_source'].startswith('model_prescribed_forcing:')
    assert 'prepared_grid' in report['workflow']['preflight']['input_sha256']
    assert workflow.find_latest_completed_bundle(job['root']/'runs')==bundle.resolve()
    assert workflow.main(['--show-latest','--out-dir',str(job['root']/'runs')])==0


def test_dry_run_exports_empty_wet_layers_and_full_grid(job):
    pytest.importorskip('netCDF4');pytest.importorskip('pyproj')
    job['write_forcing']([[0,0,0,0,0,0],[3600,0,0,0,0,0],[7200,0,0,0,0,0]])
    bundle=workflow.run_workflow(job['file'],run_id='dry')
    report=json.loads((bundle/'run_report.json').read_text(encoding='utf-8'))
    assert report['status']=='complete'
    for name in ('inundation_max_50m','inundation_max_50m_epsg4490'):
        path=bundle/'shp'/f'{name}.shp'
        assert path.stat().st_size==100
        assert path.with_suffix('.shx').stat().st_size==100
        assert struct.unpack('<I',path.with_suffix('.dbf').read_bytes()[4:8])[0]==0
    assert all(count==0 for count in json.loads((bundle/'stepjson/index.json').read_text())['wet_counts'])


def test_failed_postprocessing_is_reported_and_command_exits_nonzero(job,monkeypatch):
    pytest.importorskip('netCDF4');pytest.importorskip('pyproj')
    def fail(*args,**kwargs): raise RuntimeError('step writer failed')
    monkeypatch.setattr(workflow,'convert_netcdf_to_step_json',fail)
    assert workflow.main(['--workflow',str(job['file']),'--id','failed'])==1
    bundle=job['root']/'runs/failed'
    report=json.loads((bundle/'run_report.json').read_text(encoding='utf-8'))
    assert report['status']=='failed' and report['failed_stage']=='stepjson'
    assert report['workflow']['model']['status']=='complete'
    assert report['workflow']['nc2web']['status']=='complete'
    assert report['workflow']['viewer']['status']=='pending'
    assert report['workflow']['validate']['status']=='pending'
    assert 'step writer failed' in report['error']
    assert (bundle/'result.nc').is_file()


def test_partial_web_artifact_cannot_be_marked_complete(job,monkeypatch):
    pytest.importorskip('netCDF4');pytest.importorskip('pyproj')
    original=workflow.convert_netcdf_to_step_json
    def incomplete(*args,**kwargs):
        result=original(*args,**kwargs)
        (Path(args[1])/'step_001.json').unlink()
        return result
    monkeypatch.setattr(workflow,'convert_netcdf_to_step_json',incomplete)
    assert workflow.main(['--workflow',str(job['file']),'--id','missing-frame'])==1
    report=json.loads((job['root']/'runs/missing-frame/run_report.json').read_text(encoding='utf-8'))
    assert report['status']=='failed' and report['failed_stage']=='validate'


def test_existing_run_directory_is_preserved(job):
    path=job['root']/'runs/existing'
    path.mkdir(parents=True)
    (path/'marker').write_text('preserve')
    with pytest.raises(WorkflowError,match='already exists'):
        workflow.run_workflow(job['file'],run_id='existing')
    assert (path/'marker').read_text()=='preserve' and not (path/'run_report.json').exists()


def test_missing_inputs_fail_before_model_stage(job):
    (job['root']/'grid.npz').unlink()
    assert workflow.main(['--workflow',str(job['file']),'--id','bad-input'])==1
    report=json.loads((job['root']/'runs/bad-input/run_report.json').read_text(encoding='utf-8'))
    assert report['failed_stage']=='preflight' and report['workflow']['model']['status']=='pending'


@pytest.mark.parametrize('patch',[
    {'settings':{'max_spread_m_s':0}}, {'settings':{'eta_crest_m':True}},
    {'settings':{'eta_crest_m':float('nan')}}, {'settings':{'area_schedule':[{'time_h':0,'fraction':1.1}]}},
    {'time_origin':'2023-08-01T00:00:00'}, {'record_xihe_component_changes':'true'},
    {'prepared_grid':'data/dem.tif'}, {'result_name':'../unsafe.nc'}, {'extra_field':1},
])
def test_workflow_schema_rejects_invalid_values(job,patch):
    with pytest.raises((ValidationError,WorkflowError)):
        WorkflowConfig.model_validate({**job['spec'],**patch})


@pytest.mark.parametrize('name',['../run','a/b','a\\b','CON','run.','.', ''])
def test_run_ids_cannot_traverse_or_alias_windows_paths(name):
    with pytest.raises(WorkflowError): validate_run_id(name)


def test_prepared_grid_rejects_unit_mask_datum_and_coordinate_errors(job):
    case=load_config(job['root']/'case.yaml')
    for key,value in (
        ('elevation_unit','ft'),('vertical_datum','wrong'),('active_mask',np.full((2,3),2)),
        ('zones',np.full((2,3),'qingbei')),('x',np.array([0,1,2])),('elevation',np.full((2,3),np.nan)),
    ):
        np.savez(job['root']/'bad.npz',**{**job['pack'],key:value})
        with pytest.raises(WorkflowError): load_prepared_workflow_grid(job['root']/'bad.npz',case)


def test_forcing_rejects_missing_times_negative_values_and_component_conflicts(job):
    case=load_config(job['root']/'case.yaml')
    for rows in (
        [job['rows'][0],job['rows'][2]],
        [[0,1,.1,0,.1,0],[3600,-1,.3,0,.1,.2],job['rows'][2]],
        [[0,1,.1,0,.1,0],[3600,1,.2,0,.1,.2],job['rows'][2]],
    ):
        job['write_forcing'](rows)
        with pytest.raises(WorkflowError): load_workflow_forcing(job['root']/'forcing.csv',case)


def test_unknown_forcing_columns_are_rejected(job):
    path=job['root']/'forcing.csv'
    path.write_text(path.read_text().replace('xihe_component_m3s','xihe_typo'))
    with pytest.raises(WorkflowError): load_workflow_forcing(path,load_config(job['root']/'case.yaml'))


def test_gate_csv_partial_output_fails_before_postprocessing(job,monkeypatch):
    pytest.importorskip('netCDF4');pytest.importorskip('pyproj')
    original=workflow.write_hourly_netcdf
    def incomplete(*args,**kwargs):
        result=original(*args,**kwargs)
        path=workflow.gate_dispatch_paths(result)[0]
        path.write_text(path.read_text(encoding='utf-8-sig').splitlines()[0]+'\n',encoding='utf-8-sig')
        return result
    monkeypatch.setattr(workflow,'write_hourly_netcdf',incomplete)
    assert workflow.main(['--workflow',str(job['file']),'--id','partial-log'])==1
    report=json.loads((job['root']/'runs/partial-log/run_report.json').read_text(encoding='utf-8'))
    assert report['failed_stage']=='gate_dispatch' and report['workflow']['shp']['status']=='pending'
    assert workflow.find_latest_completed_bundle(job['root']/'runs') is None


def test_tampered_frame_header_fails_final_validation(job,monkeypatch):
    pytest.importorskip('netCDF4');pytest.importorskip('pyproj')
    original=workflow.convert_netcdf_to_step_json
    def incomplete(*args,**kwargs):
        result=original(*args,**kwargs)
        path=Path(args[1]).parent/'nc2web/frame_001.bin'
        with path.open('r+b') as stream:
            stream.seek(len(workflow.FRAME_MAGIC))
            stream.write(struct.pack('<III',99,99,4))
        return result
    monkeypatch.setattr(workflow,'convert_netcdf_to_step_json',incomplete)
    assert workflow.main(['--workflow',str(job['file']),'--id','bad-header'])==1
    report=json.loads((job['root']/'runs/bad-header/run_report.json').read_text(encoding='utf-8'))
    assert report['failed_stage']=='validate' and report['status']=='failed'


def test_latest_ignores_partial_and_corrupt_run_reports(job):
    folder=job['root']/'runs/incomplete'
    folder.mkdir(parents=True)
    (folder/'run_report.json').write_text('{"status":"complete","result_file":"missing.nc"}')
    corrupt=job['root']/'runs/corrupt'
    corrupt.mkdir()
    (corrupt/'run_report.json').write_text('invalid json')
    assert workflow.find_latest_completed_bundle(job['root']/'runs') is None


def test_prepared_coordinates_within_declared_tolerance_can_export(job):
    pytest.importorskip('netCDF4');pytest.importorskip('pyproj')
    np.savez(job['root']/'grid.npz',**{**job['pack'],'x':job['pack']['x']+1e-7})
    result=workflow.run_workflow(job['file'],run_id='coordinate-tolerance')
    assert json.loads((result/'run_report.json').read_text(encoding='utf-8'))['status']=='complete'
