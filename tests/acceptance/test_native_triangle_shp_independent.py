from pathlib import Path
import hashlib
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
from native_fixture import make_case

@pytest.fixture
def source(tmp_path):
    return make_case(tmp_path/'source',bed_m=1.25)


def request(source,name,aggregation='FRAME',scope='FULL_DOMAIN',target='NATIVE',threshold=0.):
    selector=({'aggregation':'FRAME','frame_index':2,'time_s':.05} if aggregation=='FRAME' else {'aggregation':'MAX_OVER_WINDOW','window_start_s':0.,'window_end_s':.05})
    data={'format':'native-triangle-result-shp-export-request-v1','schema_version':'1.0','run_config_file':'run.yaml','output_directory':name,'selector':selector,'scope_selection':scope,'wet_depth_threshold_m':threshold,'target_crs':target}
    path=source['root']/(name+'.yaml');path.write_text(yaml.safe_dump(data),encoding='utf-8')
    return path,data,source['root']/name


def exported(source,name,**kwargs):
    from hydrodynamics.triangular_shp_export import export_native_triangle_shp,validate_native_triangle_shp_package
    path,data,directory=request(source,name,**kwargs)
    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in source['root'].iterdir() if p.is_file()}
    report=export_native_triangle_shp(path)
    assert report.valid
    assert validate_native_triangle_shp_package(path).valid
    assert before=={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in source['root'].iterdir() if p.is_file()}
    manifest=json.loads((directory/'manifest.json').read_bytes())
    assert manifest['experimental'] is True
    assert manifest['engineering_validated'] is False and manifest['can_publish'] is False
    files=list(directory.rglob('*.shp'));assert len(files)==1
    return path,directory,files[0],manifest


def records(path):
    with shapefile.Reader(str(path),encoding='utf-8') as reader:
        return {str(item.record.face_id):(list(item.shape.points),item.record.as_dict()) for item in reader.iterShapeRecords()}


def assert_native_triangle(points,expected,area):
    assert len(points)==4 and points[0]==points[-1]
    assert len(set(tuple(p) for p in points))==3
    for point in points[:-1]:
        assert any(np.max(np.abs(np.asarray(point)-np.asarray(wanted)))<1e-8 for wanted in expected)
    polygon=Polygon(points)
    assert polygon.is_valid and abs(polygon.area-area)<1e-12


def rewrite_manifest_digest(directory,path):
    manifest_path=directory/'manifest.json';data=json.loads(manifest_path.read_bytes())
    key=path.relative_to(directory).as_posix();matching=[item for item in data['files'] if item['key']==key];assert len(matching)==1
    raw=path.read_bytes();matching[0]['sha256']=hashlib.sha256(raw).hexdigest();matching[0]['byte_count']=len(raw)
    manifest_path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')


def test_frame_has_actual_native_geometry_values_and_int64_ids(source):
    _,directory,path,manifest=exported(source,'frame')
    actual=records(path);assert set(actual)=={str(v) for v in source['mesh'].face_id}
    for index,face_id in enumerate(source['mesh'].face_id):
        points,attrs=actual[str(face_id)]
        expected=[source['mesh'].node_xy[n] for n in source['mesh'].face_nodes[index]]
        assert_native_triangle(points,expected,source['areas'][index])
        assert attrs['face_id']==str(face_id)
        assert abs(attrs['depth_m']-source['depth'][2,index])<1e-12
        assert abs(attrs['water_lvl']-(source['depth'][2,index]+source['bed'][index]))<1e-12
        assert abs(attrs['ux_m_s']-source['momentum_x'][2,index]/source['depth'][2,index])<1e-12
        assert abs(attrs['uy_m_s']-source['momentum_y'][2,index]/source['depth'][2,index])<1e-12
        assert abs(attrs['time_s']-.05)<1e-12
        assert abs(attrs['area_m2']-source['areas'][index])<1e-12
    assert CRS.from_wkt(path.with_suffix('.prj').read_text()).to_epsg()==32650


def test_wet_window_uses_saved_peak_including_earliest_frame(source):
    _,_,path,_=exported(source,'peak',aggregation='MAX_OVER_WINDOW',scope='MAXIMUM_WET_SURFACE',threshold=.299)
    actual=records(path);assert set(actual)=={str(source['mesh'].face_id[0])}
    points,attrs=actual[str(source['mesh'].face_id[0])]
    expected=source['depth'][:,0];index=int(np.argmax(expected))
    assert abs(attrs['max_depth']-expected[index])<1e-12
    assert abs(attrs['max_time']-source['times'][index])<1e-12
    assert attrs['area_m2']==3.
    assert source['depth'][2,0]<.299<source['depth'][0,0]


def test_geographic_copy_keeps_each_native_face_and_projected_area(source):
    _,_,path,_=exported(source,'geographic',target='EPSG:4490')
    assert CRS.from_wkt(path.with_suffix('.prj').read_text()).to_epsg()==4490
    actual=records(path);assert set(actual)=={str(v) for v in source['mesh'].face_id}
    transform=Transformer.from_crs(32650,4490,always_xy=True)
    for index,face_id in enumerate(source['mesh'].face_id):
        points,attrs=actual[str(face_id)]
        expected=[transform.transform(*source['mesh'].node_xy[n]) for n in source['mesh'].face_nodes[index]]
        assert len(points)==4 and len(set(tuple(p) for p in points))==3
        for point in points[:-1]:assert any(np.max(np.abs(np.asarray(point)-np.asarray(v)))<1e-11 for v in expected)
        assert attrs['area_m2']==source['areas'][index]
        assert abs(attrs['water_lvl']-source['depth'][2,index]-1.25)<1e-12
        assert Polygon(points).area<1e-8


def test_true_dry_wet_selection_is_a_valid_empty_shapefile(tmp_path):
    source=make_case(tmp_path/'dry',dry=True,bed_m=1.25)
    _,_,path,_=exported(source,'dry_wet',aggregation='MAX_OVER_WINDOW',scope='MAXIMUM_WET_SURFACE')
    with shapefile.Reader(str(path)) as reader:assert len(reader)==0 and reader.shapes()==[]
    assert all(path.with_suffix(s).is_file() for s in ['.shp','.shx','.dbf','.prj','.cpg'])


@pytest.mark.parametrize('selector',[
    {'aggregation':'FRAME','frame_index':2,'time_s':.025},
    {'aggregation':'FRAME','frame_index':3,'time_s':.05},
    {'aggregation':'FRAME','frame_index':True,'time_s':.05},
    {'aggregation':'FRAME','frame_index':2,'time_s':'.05'},
    {'aggregation':'MAX_OVER_WINDOW','window_start_s':0.,'window_end_s':.051},
])
def test_wrong_or_nonexact_selector_cannot_create_output(source,selector):
    from hydrodynamics.triangular_shp_export import export_native_triangle_shp
    path,data,directory=request(source,'wrong_selector');data['selector']=selector;path.write_text(yaml.safe_dump(data),encoding='utf-8')
    with pytest.raises((ValueError,RuntimeError,OSError)):export_native_triangle_shp(path)
    assert not directory.exists()


def test_existing_directory_is_not_overwritten(source):
    from hydrodynamics.triangular_shp_export import export_native_triangle_shp
    path,directory,_,_=exported(source,'existing')
    before={p.relative_to(directory):hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()}
    with pytest.raises((ValueError,RuntimeError,OSError)):export_native_triangle_shp(path)
    assert before=={p.relative_to(directory):hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()}


@pytest.mark.parametrize('field,replacement',[('depth_m','9.000000000000'),('face_id','9007199254740992')])
def test_rehashed_dbf_changes_are_checked_against_native_values(source,field,replacement):
    from hydrodynamics.triangular_shp_export import validate_native_triangle_shp_package
    path,directory,shp_path,_=exported(source,'dbf_tamper')
    with shapefile.Reader(str(shp_path)) as reader:
        fields=reader.fields[1:]
        row_index=next(i for i,record in enumerate(reader.records()) if str(record.face_id)==str(source['mesh'].face_id[0]))
    dbf_path=shp_path.with_suffix('.dbf');raw=bytearray(dbf_path.read_bytes());header=struct.unpack_from('<H',raw,8)[0];record_length=struct.unpack_from('<H',raw,10)[0]
    offset=1
    for name,typ,length,decimal in fields:
        if name==field:break
        offset+=length
    else:raise AssertionError('expected field absent')
    encoded=replacement.rjust(length).encode('ascii');assert len(encoded)==length
    start=header+row_index*record_length+offset
    raw[start:start+length]=encoded;dbf_path.write_bytes(raw);rewrite_manifest_digest(directory,dbf_path)
    with pytest.raises((ValueError,RuntimeError,OSError)):validate_native_triangle_shp_package(path)


def test_rehashed_triangle_with_same_area_but_wrong_native_vertex_is_rejected(source):
    from hydrodynamics.triangular_shp_export import validate_native_triangle_shp_package
    path,directory,shp_path,_=exported(source,'geometry_tamper')
    with shapefile.Reader(str(shp_path)) as reader:
        row_index=next(i for i,record in enumerate(reader.records()) if str(record.face_id)==str(source['mesh'].face_id[0]))
    index=shp_path.with_suffix('.shx').read_bytes();record_offset_words=struct.unpack_from('>i',index,100+row_index*8)[0]
    raw=bytearray(shp_path.read_bytes());body=record_offset_words*2+8
    parts=struct.unpack_from('<i',raw,body+36)[0];count=struct.unpack_from('<i',raw,body+40)[0]
    assert parts==1 and count==4
    start=body+44+parts*4
    points=[struct.unpack_from('<2d',raw,start+16*i) for i in range(count)]
    xmin=min(p[0] for p in points);xmax=max(p[0] for p in points)
    apex=next(p for p in points[:-1] if xmin<p[0]<xmax)
    moved=[(x+.125 if (x,y)==apex else x,y) for x,y in points]
    assert abs(Polygon(moved).area-Polygon(points).area)<1e-12
    for i,p in enumerate(moved):struct.pack_into('<2d',raw,start+16*i,*p)
    shp_path.write_bytes(raw);rewrite_manifest_digest(directory,shp_path)
    with pytest.raises((ValueError,RuntimeError,OSError)):validate_native_triangle_shp_package(path)


def test_preparation_artifact_never_becomes_a_hydraulic_result(source):
    import shutil
    from hydrodynamics.triangular_shp_export import export_native_triangle_shp
    fake=source['root']/'fake_result.nc';shutil.copyfile(source['root']/'mesh.nc',fake)
    config=yaml.safe_load(source['config'].read_text());config['result_file']='fake_result.nc'
    (source['root']/'preparation_as_run.yaml').write_text(yaml.safe_dump(config),encoding='utf-8')
    path,data,directory=request(source,'preparation');data['run_config_file']='preparation_as_run.yaml';path.write_text(yaml.safe_dump(data),encoding='utf-8')
    with pytest.raises((ValueError,RuntimeError,OSError)):export_native_triangle_shp(path)
    assert not directory.exists()


def test_constant_saved_peak_ties_choose_the_earliest_time(tmp_path):
    import importlib.util
    import netCDF4
    from hydrodynamics.triangular_shp_export import export_native_triangle_shp,validate_native_triangle_shp_package
    fixture_path=ROOT/'tests/acceptance/test_r2a_independent.py'
    spec=importlib.util.spec_from_file_location('native_shp_independent_constant_fixture',fixture_path)
    fixture=importlib.util.module_from_spec(spec);sys.modules[spec.name]=fixture;spec.loader.exec_module(fixture)
    case_root=tmp_path/'constant';case_root.mkdir()
    case=fixture.source.__wrapped__(case_root)
    with netCDF4.Dataset(case_root/'result.nc') as ds:
        h=np.asarray(ds.variables['water_depth'][:]);times=np.asarray(ds.variables['time'][:])
        assert np.array_equal(h,np.full_like(h,.2))
    path,data,directory=request(case,'constant_peak',aggregation='MAX_OVER_WINDOW')
    data['selector']['window_end_s']=1.
    path.write_text(yaml.safe_dump(data),encoding='utf-8')
    assert export_native_triangle_shp(path).valid
    assert validate_native_triangle_shp_package(path).valid
    shape_paths=list(directory.rglob('*.shp'));assert len(shape_paths)==1
    actual=records(shape_paths[0]);assert len(actual)==2
    for _,attrs in actual.values():
        assert abs(attrs['max_depth']-.2)<1e-12
        assert attrs['max_time']==float(times[0])==0.
