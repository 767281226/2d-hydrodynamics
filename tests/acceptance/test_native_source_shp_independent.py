from pathlib import Path
import contextlib
import hashlib
import io
import json
import struct
import sys
import numpy as np
import pytest
import shapefile
import yaml
from shapely.geometry import Polygon
from pyproj import CRS,Transformer

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from source_fixture import make_case,add_family,request,file_hashes,dump_yaml

def cli(command,path):
    from hydrodynamics.triangular_shp_export import main
    with contextlib.redirect_stdout(io.StringIO()) as out,contextlib.redirect_stderr(io.StringIO()) as err:
        code=main([command,'--request',str(path)])
    return code,out.getvalue(),err.getvalue()

def export(case,name='export',**options):
    path,data,directory=request(case,name,**options)
    before=file_hashes(case['root'])
    code,out,err=cli('export',path)
    assert code==0,(out,err)
    assert cli('validate',path)[0]==0
    for key,digest in before.items():assert hashlib.sha256((case['root']/key).read_bytes()).hexdigest()==digest
    assert not (case['root']/'uncomputed_base_none.nc').exists()
    manifest=json.loads((directory/'manifest.json').read_bytes())
    assert manifest['format']=='native-triangular-result-shapefile-v2'
    assert manifest['schema_version']=='2.0' and manifest['result_kind']==case['kind']
    assert manifest['experimental'] is True
    assert manifest['engineering_validated'] is False and manifest['can_publish'] is False
    paths=list(directory.rglob('*.shp'));assert len(paths)==1
    return path,directory,paths[0],manifest

def records(shp):
    with shapefile.Reader(str(shp)) as reader:
        return {str(row.record.face_id):(list(row.shape.points),row.record.as_dict()) for row in reader.iterShapeRecords()}

def values_at_key(data,name):
    if isinstance(data,dict):return ([data[name]] if name in data else [])+[v for child in data.values() for v in values_at_key(child,name)]
    if isinstance(data,list):return [v for child in data for v in values_at_key(child,name)]
    return []

@pytest.mark.parametrize('kind',['manning','rainfall','manning_and_rainfall'])
def test_frame_exports_real_source_state_and_native_faces_without_base_result(tmp_path,kind):
    case=make_case(tmp_path/kind,kind)
    _,_,shp,manifest=export(case)
    actual=records(shp);mesh=case['mesh'];result=case['result']
    assert set(actual)=={str(v) for v in mesh.face_id}
    for i,face_id in enumerate(mesh.face_id):
        points,attrs=actual[str(face_id)]
        assert len(points)==4 and points[0]==points[-1] and len(set(map(tuple,points[:-1])))==3
        assert set(map(tuple,points[:-1]))=={tuple(mesh.node_xy[n]) for n in mesh.face_nodes[i]}
        assert abs(Polygon(points).area-case['areas'][i])<1e-12
        assert abs(attrs['area_m2']-case['areas'][i])<1e-12
        h=case['depth'][-1,i]
        assert abs(attrs['depth_m']-h)<1e-12
        assert abs(attrs['water_lvl']-h-1.25)<1e-12
        assert abs(attrs['ux_m_s']-result.momentum_x_m2_s[-1][i]/h)<1e-12
        assert abs(attrs['uy_m_s']-result.momentum_y_m2_s[-1][i]/h)<1e-12
        assert attrs['time_s']==float(case['times'][-1])
    assert CRS.from_wkt(shp.with_suffix('.prj').read_text(encoding='utf-8')).to_epsg()==4548
    assert case['kind'] in values_at_key(manifest,'sources_applied')
    for flag in ['manning_enabled','rainfall_enabled','manning_applied','rainfall_applied']:
        assert bool(getattr(result,flag)) in values_at_key(manifest,flag),flag
    assert not np.array_equal(case['depth'][0],case['depth'][-1])
    if kind!='rainfall':assert result.nonzero_manning_effect_applied is True
    if kind!='manning':assert result.rainfall_nonzero_effect_applied is True
    roles={item['role']:item for item in manifest['source_graph']}
    assert {'request','top_level_run_config','base_run_config','source_config','source_preparation',
            'p3_business','p2_mesh','native_result'}.issubset(roles)
    assert len(roles)==len(manifest['source_graph'])
    for item in roles.values():
        raw=Path(item['path']).read_bytes()
        assert item['byte_count']==len(raw) and item['sha256']==hashlib.sha256(raw).hexdigest()
    assert Path(roles['native_result']['path']).resolve()==case['native'].resolve()
    assert all(Path(item['path']).name!='uncomputed_base_none.nc' for item in roles.values())
    rows=manifest['frame_budgets'];assert len(rows)==len(case['times'])
    for i,row in enumerate(rows):
        assert row['frame_index']==i and row['time_s']==float(case['times'][i])
        assert abs(row['integrated_volume_m3']-case['volumes'][i])<1e-12
        assert abs(row['ledger_final_volume_m3']-case['volumes'][i])<1e-12
        assert abs(row['ledger_residual_m3'])<1e-12
    expected_rain=np.diff(case['times'])*case['rate']*float(case['areas'].sum())
    summary=manifest['source_summary']
    assert abs(summary['rainfall_accepted_active_volume_m3']-float(expected_rain.sum()))<1e-12
    if kind!='manning':
        assert np.max(np.abs(np.asarray(summary['rainfall_accepted_active_volume_m3_by_interval'])-expected_rain))<1e-12
    else:assert summary['rainfall_accepted_active_volume_m3_by_interval']==[]
    assert abs(manifest['maximum_synchronous_water_volume_m3']-float(case['volumes'].max()))<1e-12

@pytest.mark.parametrize('kind',['manning','rainfall','manning_and_rainfall'])
def test_saved_subwindow_peak_and_geographic_geometry_use_source_result(tmp_path,kind):
    case=make_case(tmp_path/kind,kind)
    _,_,shp,manifest=export(case,aggregation='MAX_OVER_WINDOW',start=case['times'][1],target='EPSG:4490')
    actual=records(shp);transform=Transformer.from_crs(4548,4490,always_xy=True)
    assert CRS.from_wkt(shp.with_suffix('.prj').read_text(encoding='utf-8')).to_epsg()==4490
    for i,face_id in enumerate(case['mesh'].face_id):
        points,attrs=actual[str(face_id)]
        expected=[transform.transform(*case['mesh'].node_xy[n]) for n in case['mesh'].face_nodes[i]]
        assert len(points)==4 and len(set(map(tuple,points[:-1])))==3
        assert all(any(np.max(np.abs(np.asarray(p)-np.asarray(e)))<1e-11 for e in expected) for p in points[:-1])
        peak_index=1+int(np.argmax(case['depth'][1:,i]))
        assert abs(attrs['max_depth']-case['depth'][peak_index,i])<1e-12
        assert attrs['max_time']==float(case['times'][peak_index])
        assert abs(attrs['area_m2']-case['areas'][i])<1e-12
    assert manifest['selected_frame_indices']==[1,2]
    assert abs(manifest['selected_window_maximum_synchronous_water_volume_m3']-float(case['volumes'][1:].max()))<1e-12
    envelope_volume=float(np.max(case['depth'][1:],axis=0)@case['areas'])
    assert envelope_volume>float(case['volumes'][1:].max())+1e-6

@pytest.mark.parametrize('kind',['manning','rainfall'])
def test_zero_effect_does_not_masquerade_as_none_and_peak_ties_are_earliest(tmp_path,kind):
    case=make_case(tmp_path/kind,kind,zero=True)
    _,_,shp,manifest=export(case,aggregation='MAX_OVER_WINDOW')
    assert np.array_equal(case['depth'],np.full_like(case['depth'],.2))
    assert manifest['result_kind']==kind
    assert kind in values_at_key(manifest,'sources_applied')
    for _,attrs in records(shp).values():
        assert attrs['max_depth']==.2 and attrs['max_time']==0.

@pytest.mark.parametrize('kind',['manning','rainfall','manning_and_rainfall'])
def test_security_active_native_faces_and_source_volume_are_preserved(tmp_path,kind):
    from shapely.geometry import box
    case=make_case(tmp_path/kind,kind,security=True)
    _,_,shp,manifest=export(case,aggregation='MAX_OVER_WINDOW',scope='MAXIMUM_WET_SURFACE')
    actual=records(shp);safe=box(1.,1.,2.,2.)
    assert set(actual)=={str(v) for v in case['mesh'].face_id}
    assert abs(float(case['areas'].sum())-15.)<1e-12
    for i,face_id in enumerate(case['mesh'].face_id):
        points,attrs=actual[str(face_id)]
        assert Polygon(points).intersection(safe).area<1e-12
        assert abs(attrs['area_m2']-case['areas'][i])<1e-12
    if kind!='manning':
        accounting=case['result'].security_rainfall_accounting
        assert abs(accounting.accepted_active_rainfall_volume_m3-.0015)<1e-15
        assert abs(accounting.security_excluded_unmodeled_rainfall_volume_m3-.0001)<1e-15
        summary=manifest['source_summary']
        assert abs(summary['rainfall_accepted_active_volume_m3']-.0015)<1e-15
        assert abs(summary['rainfall_excluded_unmodeled_volume_m3']-.0001)<1e-15
        assert summary['excluded_rain_is_accepted_budget_loss'] is False
        assert summary['automatic_flow_transfer_from_excluded_area'] is False
    assert manifest['security_exclusion_evidence']['mesh_face_count']==case['mesh'].n_face
    roles={item['role'] for item in manifest['source_graph']}
    assert {'security_input_config','security_geometry','security_domain'}.issubset(roles)

def test_identical_zero_effect_states_keep_distinct_family_identities(tmp_path):
    case=make_case(tmp_path/'siblings','manning',zero=True)
    sibling=add_family(case,'rainfall',zero=True)
    assert np.array_equal(case['depth'],sibling['depth'])
    _,_,_,left=export(case,'manning_shp')
    _,_,_,right=export(sibling,'rainfall_shp')
    assert left['result_set_id']!=right['result_set_id']
    assert left['result_kind']=='manning' and right['result_kind']=='rainfall'

def test_true_dry_zero_rain_source_exports_valid_empty_wet_layer(tmp_path):
    case=make_case(tmp_path/'dry','rainfall',zero=True,dry=True)
    assert np.array_equal(case['depth'],np.zeros_like(case['depth']))
    _,_,shp,manifest=export(case,aggregation='MAX_OVER_WINDOW',scope='MAXIMUM_WET_SURFACE')
    with shapefile.Reader(str(shp)) as reader:
        assert len(reader)==0 and reader.shapes()==[]
    assert all(shp.with_suffix(ext).is_file() for ext in ['.shp','.shx','.dbf','.prj','.cpg'])
    assert manifest['result_kind']=='rainfall'

@pytest.mark.parametrize('change',[
    {'result_kind':'none'},{'result_kind':'MANNING'},{'result_kind':True},
    {'selector':{'aggregation':'FRAME','frame_index':True,'time_s':.05}},
    {'selector':{'aggregation':'FRAME','frame_index':2,'time_s':'.05'}},
    {'selector':{'aggregation':'FRAME','frame_index':2,'time_s':.025}},
    {'selector':{'aggregation':'MAX_OVER_WINDOW','window_start_s':.01,'window_end_s':.05}},
    {'output_directory':'../outside'},
])
def test_invalid_v2_request_is_rejected_before_creating_output(tmp_path,change):
    case=make_case(tmp_path/'case','manning')
    path,data,directory=request(case,'bad');data.update(change);dump_yaml(path,data)
    assert cli('export',path)[0]!=0
    assert not directory.exists() and not (case['root'].parent/'outside').exists()

def test_wrong_explicit_family_and_v1_source_request_remain_rejected(tmp_path):
    case=make_case(tmp_path/'case','rainfall')
    path,data,directory=request(case,'wrong_family');data['result_kind']='manning';dump_yaml(path,data)
    assert cli('export',path)[0]!=0 and not directory.exists()

    data['format']='native-triangle-result-shp-export-request-v1';data['schema_version']='1.0';data.pop('result_kind')
    dump_yaml(path,data)
    assert cli('export',path)[0]!=0 and not directory.exists()

def test_v1_public_schema_remains_the_original_none_contract():
    from hydrodynamics.triangular_shp_export import main,package_schema
    schema=package_schema()
    assert schema['request_schema']['properties']['format']['const']=='native-triangle-result-shp-export-request-v1'
    assert schema['request_schema']['properties']['schema_version']['const']=='1.0'
    assert 'result_kind' not in schema['request_schema']['properties']
    assert schema['manifest_schema']['properties']['format']['const']=='native-triangular-result-shapefile-v1'
    assert schema['manifest_schema']['properties']['validation_basis']['const']=='P4_1_NATIVE_RESULT_READER_AND_SOURCE_GRAPH'
    assert schema['manifest_schema']['properties']['engine_mode']['const']=='triangular_closed_experimental'
    with contextlib.redirect_stdout(io.StringIO()) as stdout:assert main(['schema'])==0
    assert json.loads(stdout.getvalue())==schema

def test_security_source_version_drift_invalidates_v2_package_and_new_export(tmp_path):
    case=make_case(tmp_path/'case','manning_and_rainfall',security=True)
    path,_,_,_=export(case)
    source=case['root']/'security.geojson'
    data=json.loads(source.read_bytes());data['features'][0]['properties']['source_version']='changed-after-export'
    source.write_text(json.dumps(data),encoding='utf-8')
    assert cli('validate',path)[0]!=0
    new_request,_,directory=request(case,'stale_security')
    assert cli('export',new_request)[0]!=0 and not directory.exists()

@pytest.mark.parametrize('mode',['missing_source','none_result'])
def test_base_none_result_is_never_a_fallback_for_source_result(tmp_path,mode):
    from hydrodynamics.triangular_solver import run_triangular_solver
    case=make_case(tmp_path/'case','manning')
    run_triangular_solver(case['root']/'base.yaml')
    path,data,directory=request(case,'fallback')
    if mode=='missing_source':case['native'].unlink()
    else:
        top=yaml.safe_load(case['run_config'].read_text());top['result_file']='uncomputed_base_none.nc';dump_yaml(case['run_config'],top)
    assert cli('export',path)[0]!=0 and not directory.exists()

@pytest.mark.parametrize('role',['base','source_config','source_preparation','native_budget'])
def test_changed_source_graph_or_native_budget_cannot_export(tmp_path,role):
    import netCDF4
    case=make_case(tmp_path/'case','manning_and_rainfall')
    path,_,directory=request(case,'drift')
    if role=='native_budget':
        with netCDF4.Dataset(case['native'],'r+') as ds:
            values=ds.variables['water_depth'][:];values[-1,0]+=.01;ds.variables['water_depth'][:]=values
    else:
        target=(case['root']/'base.yaml' if role=='base' else case['root']/('manning_and_rainfall_preparation.yaml' if role=='source_config' else 'manning_and_rainfall_preparation.json'))
        if target.suffix=='.json':
            data=json.loads(target.read_bytes());data['case_id']='wrong-stale-case';target.write_text(json.dumps(data),encoding='utf-8')
        else:
            with target.open('ab') as stream:stream.write(b'\n# independent source graph drift\n')
    assert cli('export',path)[0]!=0 and not directory.exists()

def refresh_digest(directory,path):
    marker=directory/'manifest.json';manifest=json.loads(marker.read_bytes())
    matches=[item for item in manifest['files'] if item['key']==path.relative_to(directory).as_posix()];assert len(matches)==1
    raw=path.read_bytes();matches[0]['byte_count']=len(raw);matches[0]['sha256']=hashlib.sha256(raw).hexdigest()
    marker.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')

def test_rehashed_dbf_source_depth_tamper_is_checked_against_true_native(tmp_path):
    case=make_case(tmp_path/'case','rainfall')
    path,directory,shp,_=export(case)
    with shapefile.Reader(str(shp)) as reader:fields=reader.fields[1:]
    offset=1
    for name,typ,width,decimal in fields:
        if name=='depth_m':break
        offset+=width
    else:raise AssertionError('depth field absent')
    dbf=shp.with_suffix('.dbf');raw=bytearray(dbf.read_bytes());header=struct.unpack_from('<H',raw,8)[0]
    raw[header+offset:header+offset+width]='9.000000000000'.rjust(width).encode('ascii');dbf.write_bytes(raw)
    refresh_digest(directory,dbf)
    assert cli('validate',path)[0]!=0

def test_rehashed_valid_triangle_with_same_area_but_shifted_native_vertices_is_rejected(tmp_path):
    case=make_case(tmp_path/'case','manning')
    path,directory,shp,_=export(case)
    with shapefile.Reader(str(shp)) as reader:
        fields=reader.fields[1:]
        rows=[(list(shape.points),list(record)) for shape,record in zip(reader.shapes(),reader.records())]
    replacement=case['root']/'altered_geometry'
    with shapefile.Writer(str(replacement),shapeType=shapefile.POLYGON,encoding='utf-8') as writer:
        for name,typ,width,decimal in fields:writer.field(name,typ,size=width,decimal=decimal)
        for i,(points,attrs) in enumerate(rows):
            shifted=[(x+.125,y) for x,y in points] if i==0 else points
            assert Polygon(shifted).is_valid
            assert abs(Polygon(shifted).area-Polygon(points).area)<1e-12
            writer.poly([shifted]);writer.record(*attrs)
    for suffix in ['.shp','.shx']:
        target=shp.with_suffix(suffix);target.write_bytes(replacement.with_suffix(suffix).read_bytes())
        refresh_digest(directory,target)
    assert cli('validate',path)[0]!=0

@pytest.mark.parametrize('attribute,value',[
    ('manning_enabled',0),('sources_applied','none'),('case_id','wrong-case'),
    ('solver_version','triangular-closed-wall-1.0.0'),
])
def test_false_native_source_identity_cannot_be_accepted_by_v2(tmp_path,attribute,value):
    import netCDF4
    case=make_case(tmp_path/'case','manning_and_rainfall')
    path,_,directory=request(case,'false_identity')
    with netCDF4.Dataset(case['native'],'r+') as ds:
        if attribute in {'case_id','solver_version'}:
            metadata=json.loads(ds.contract_metadata_json);metadata[attribute]=value
            ds.contract_metadata_json=json.dumps(metadata)
        else:ds.setncattr(attribute,value)
    assert cli('export',path)[0]!=0 and not directory.exists()

@pytest.mark.parametrize('field',['frame_budget','source_graph','manning_values'])
def test_self_consistent_manifest_tamper_still_fails_fresh_native_validation(tmp_path,field):
    case=make_case(tmp_path/'case','manning_and_rainfall')
    path,directory,_,manifest=export(case)
    if field=='frame_budget':
        row=manifest['frame_budgets'][-1]
        row['integrated_volume_m3']+=.1;row['ledger_final_volume_m3']+=.1;row['ledger_residual_m3']+=.1
        manifest['maximum_synchronous_water_volume_m3']=max(r['integrated_volume_m3'] for r in manifest['frame_budgets'])
        manifest['selected_window_maximum_synchronous_water_volume_m3']=row['integrated_volume_m3']
    elif field=='source_graph':
        entry=next(r for r in manifest['source_graph'] if r['role']=='source_preparation')
        entry['sha256']='0'*64
        raw=json.dumps(manifest['source_graph'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        manifest['source_graph_sha256']=hashlib.sha256(raw).hexdigest()
    else:
        manifest['source_summary']['manning_n_min']=.9;manifest['source_summary']['manning_n_max']=.9
    (directory/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    assert cli('validate',path)[0]!=0

def test_existing_output_is_preserved_and_postpublish_source_drift_raises(tmp_path,monkeypatch):
    import hydrodynamics.triangular_shp_export as module
    case=make_case(tmp_path/'case','manning')
    path,data,directory=request(case,'existing');directory.mkdir();(directory/'user.txt').write_bytes(b'preserve')
    assert cli('export',path)[0]!=0 and (directory/'user.txt').read_bytes()==b'preserve'
    path,data,directory=request(case,'postpublish')
    rename=module.os.rename
    def publish_then_change(src,dst,*args,**kwargs):
        value=rename(src,dst,*args,**kwargs)
        if Path(dst).resolve()==directory.resolve():
            with case['run_config'].open('ab') as stream:stream.write(b'\n# concurrent source run drift\n')
            (directory/'user.txt').write_bytes(b'preserve concurrent output')
        return value
    monkeypatch.setattr(module.os,'rename',publish_then_change)
    assert cli('export',path)[0]!=0
    assert not (directory/'manifest.json').exists()
    assert (directory/'user.txt').read_bytes()==b'preserve concurrent output'
