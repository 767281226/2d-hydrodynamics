from pathlib import Path
import hashlib
import json
import sys
import pytest
import yaml
from shapely.geometry import Polygon,box,mapping,MultiPolygon

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from security_fixture import make_inputs,references

def policy(source,crs='EPSG:32650'):
    data={'format':'security-area-exclusion-v1','schema_version':'1.0','enabled':True,
          'source_file':'security.geojson','source_crs':crs,'constraint':'excluded_closed_wall'}
    path=source['root']/'security.yaml'
    path.write_text(yaml.safe_dump(data),encoding='utf-8')
    return path

def load(source,crs='EPSG:32650'):
    from hydrodynamics.security_exclusion import load_security_exclusion_input,load_security_area_geometry
    path=policy(source,crs)
    model,_,_=load_security_exclusion_input(path)
    return load_security_area_geometry(model,path,'EPSG:32650')

@pytest.mark.parametrize('reference_id',[item['id'] for item in references()])
def test_geometric_area_and_hole_meaning_match_frozen_independent_references(tmp_path,reference_id):
    from hydrodynamics.security_exclusion import assess_security_domain
    source=make_inputs(tmp_path/reference_id,reference_id)
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source['root'].iterdir()}
    geometry=load(source)
    report=assess_security_domain(source['domain'],geometry)
    expected=source['reference']
    assert abs(report.original_domain_area_m2-expected['domain_area'])<1e-12
    assert abs(report.security_inside_area_m2-expected['excluded_inside_area'])<1e-12
    assert abs(report.active_domain_area_m2-expected['active_area'])<1e-12
    assert report.active_geometry.symmetric_difference(source['active']).area<1e-12
    assert report.source_union_geometry.symmetric_difference(source['safe']).area<1e-12
    assert abs(report.security_outside_area_m2-expected.get('safe_outside_area',0.))<1e-12
    if 'wet_island_area' in expected:
        island=box(1.5,1.5,2.5,2.5)
        assert report.active_geometry.covers(island)
        assert report.security_inside_geometry.intersection(island).area==0.
    if 'rain_active_volume' in expected:
        assert abs(report.active_domain_area_m2*1e-4-expected['rain_active_volume'])<1e-15
        assert abs(report.security_inside_area_m2*1e-4-expected['rain_excluded_not_modelled_volume'])<1e-15
    assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source['root'].iterdir() if p.name!='security.yaml'}

def test_disjoint_parts_in_one_multipolygon_are_both_excluded(tmp_path):
    from hydrodynamics.security_exclusion import assess_security_domain
    source=make_inputs(tmp_path/'multi')
    safe=MultiPolygon([box(.5,.5,1.,1.),box(2.,2.,3.,3.)])
    path=source['root']/'security.geojson'
    path.write_text(json.dumps({'type':'Feature','id':'independent-multipart','properties':{},'geometry':mapping(safe)}),encoding='utf-8')
    report=assess_security_domain(source['domain'],load(source))
    assert abs(report.security_inside_area_m2-1.25)<1e-12
    assert abs(report.active_domain_area_m2-14.75)<1e-12
    assert report.active_geometry.symmetric_difference(source['domain'].difference(safe)).area<1e-12

def test_conflicting_geojson_crs_is_rejected_before_geometry_can_be_treated_as_outside(tmp_path):
    source=make_inputs(tmp_path/'crs_conflict')
    path=source['root']/'security.geojson'
    data=json.loads(path.read_text());data['crs']['properties']['name']='EPSG:4326'
    path.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises((ValueError,RuntimeError,OSError)):
        load(source,'EPSG:32650')

@pytest.mark.parametrize('geometry',[
    {'type':'Polygon','coordinates':[[[0.,0.],[2.,2.],[0.,2.],[2.,0.],[0.,0.]]]},
    {'type':'Polygon','coordinates':[]},
    {'type':'LineString','coordinates':[[0.,0.],[1.,1.]]},
    {'type':'Polygon','coordinates':[[[0.,0.],[1.,0.],[float('nan'),1.],[0.,0.]]]},
])
def test_invalid_empty_nonpolygon_or_nonfinite_source_is_rejected_without_repair(tmp_path,geometry):
    source=make_inputs(tmp_path/'bad')
    path=source['root']/'security.geojson'
    raw=json.dumps({'type':'Feature','properties':{},'geometry':geometry}).encode()
    path.write_bytes(raw)
    with pytest.raises((ValueError,RuntimeError,OSError)):
        load(source)
    assert path.read_bytes()==raw

def test_unknown_crs_is_not_guessed(tmp_path):
    source=make_inputs(tmp_path/'unknown_crs')
    with pytest.raises((ValueError,RuntimeError,OSError)):
        load(source,'EPSG:999999999')
