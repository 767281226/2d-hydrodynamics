from pathlib import Path
import hashlib
import json
import sys
import math
import numpy as np
import netCDF4
import pytest
import shapefile
import yaml
from shapely.geometry import Polygon,mapping
from shapely.ops import unary_union

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from security_fixture import make_inputs,assert_actual_face_coverage

def dump_yaml(path,value):
    path.write_text(yaml.safe_dump(value,sort_keys=False),encoding='utf-8')

def configure(source):
    root=source['root']
    (root/'trusted_domain.json').write_text(json.dumps({
        'format':'security-domain-geometry-v1','schema_version':'1.0',
        'coordinate_system':'EPSG:32650','geometry':mapping(source['domain'])}),encoding='utf-8')
    config=source['preparation']
    config['security_exclusion']={
        'format':'security-area-exclusion-v1','schema_version':'1.0','enabled':True,
        'source_file':'security.geojson','source_crs':'EPSG:32650','constraint':'excluded_closed_wall'}
    from hydrodynamics.security_exclusion import SecurityExclusionInput
    if 'domain_geometry_file' in SecurityExclusionInput.model_fields:
        config['security_exclusion']['domain_geometry_file']='trusted_domain.json'
        config['security_exclusion']['domain_crs']='EPSG:32650'
    else:
        config['security_domain_geometry_file']='trusted_domain.json'
        config['security_domain_crs']='EPSG:32650'
    dump_yaml(root/'mesh_preparation.yaml',config)
    return source

def prepare(source):
    from hydrodynamics.mesh_preparation import prepare_mesh,read_ugrid_preparation
    from hydrodynamics.triangular_business import prepare_triangular_business
    result=prepare_mesh(source['root']/'mesh_preparation.yaml')
    mesh,terrain=read_ugrid_preparation(source['root']/'prepared_mesh.nc')
    assert_actual_face_coverage(mesh,source)
    prepare_triangular_business(source['root']/'business_preparation.yaml')
    return mesh,terrain

def export_max(source,name='shp'):
    from hydrodynamics.triangular_shp_export import export_native_triangle_shp,validate_native_triangle_shp_package
    root=source['root']
    request=root/(name+'.yaml')
    dump_yaml(request,{'format':'native-triangle-result-shp-export-request-v1','schema_version':'1.0',
        'run_config_file':'run.yaml','output_directory':name,
        'selector':{'aggregation':'MAX_OVER_WINDOW','window_start_s':0.,'window_end_s':1.},
        'scope_selection':'MAXIMUM_WET_SURFACE','wet_depth_threshold_m':0.,'target_crs':'NATIVE'})
    assert export_native_triangle_shp(request).valid
    assert validate_native_triangle_shp_package(request).valid
    return request,root/name

@pytest.mark.parametrize('reference_id',[
    'internal_square_hole','overlap_union_once','annulus_preserves_wet_island',
    'outside_no_effect','boundary_strip_two_chambers','partly_outside_domain',
])
def test_imported_conforming_native_faces_cover_exact_active_domain(tmp_path,reference_id):
    from hydrodynamics.security_exclusion import read_security_exclusion_evidence
    source=configure(make_inputs(tmp_path/reference_id,reference_id))
    originals={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in source['root'].iterdir()}
    mesh,_=prepare(source)
    evidence=read_security_exclusion_evidence(source['root']/'prepared_mesh.nc')
    assert evidence is not None and evidence.enabled is True
    assert abs(evidence.original_domain_area_m2-source['reference']['domain_area'])<1e-12
    assert abs(evidence.security_inside_area_m2-source['reference']['excluded_inside_area'])<1e-12
    assert abs(evidence.active_mesh_area_m2-source['reference']['active_area'])<1e-12
    assert evidence.active_face_id_sha256
    assert evidence.source_feature_count==len(source['reference']['safe_wkt'])
    if source['reference']['excluded_inside_area']>0.:
        edges=mesh.boundary_groups[evidence.security_boundary_group_name]
        assert len(edges)>0
        assert all(mesh.edge_faces[index][1]==-1 for index in edges)
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==digest for p,digest in originals.items())

def test_gmsh_generates_actual_hole_without_discarding_exterior_area(tmp_path):
    from hydrodynamics.mesh_preparation import prepare_mesh
    source=configure(make_inputs(tmp_path/'gmsh'))
    config=source['preparation']
    config['mesh']={'type':'gmsh','outer_boundary':[[0.,0.],[4.,0.],[4.,4.],[0.,4.]],
                    'holes':[],'default_size_m':2.,'minimum_size_m':.5,'maximum_nodes':128}
    config.pop('security_domain_geometry_file',None);config.pop('security_domain_crs',None)
    config['security_exclusion'].pop('domain_geometry_file',None)
    config['security_exclusion'].pop('domain_crs',None)
    dump_yaml(source['root']/'mesh_preparation.yaml',config)
    result=prepare_mesh(source['root']/'mesh_preparation.yaml')
    assert result.mesh.n_face<=64
    assert_actual_face_coverage(result.mesh,source)

def test_equivalent_crs_spelling_cannot_create_an_unreadable_security_artifact(tmp_path):
    source=configure(make_inputs(tmp_path/'crs_alias'))
    source['preparation']['security_exclusion']['source_crs']='epsg:32650'
    dump_yaml(source['root']/'mesh_preparation.yaml',source['preparation'])
    mesh,_=prepare(source)
    assert_actual_face_coverage(mesh,source)

@pytest.mark.parametrize('failure',['positive_overlap','missing_coverage','centroid_false_negative'])
def test_import_rejects_nonconforming_or_incomplete_mesh_before_artifact_creation(tmp_path,failure):
    from hydrodynamics.mesh_preparation import prepare_mesh
    ref='partial_face_centroid_false_negative' if failure=='centroid_false_negative' else 'internal_square_hole'
    source=configure(make_inputs(tmp_path/failure,ref,
         full_domain_mesh=failure!='missing_coverage',omit_last_face=failure=='missing_coverage'))
    if failure=='centroid_false_negative':
        assert all(not source['safe'].covers(t.centroid) for t in source['triangles'])
        assert any(t.intersection(source['safe']).area>.08 for t in source['triangles'])
    with pytest.raises((ValueError,RuntimeError,OSError)):
        prepare_mesh(source['root']/'mesh_preparation.yaml')
    assert not (source['root']/'prepared_mesh.nc').exists()

def test_none_run_and_max_shp_share_only_actual_active_faces_and_volume(tmp_path):
    from hydrodynamics.triangular_solver import run_triangular_solver
    from hydrodynamics.triangular_solver_netcdf import read_triangular_result
    source=configure(make_inputs(tmp_path/'none'))
    mesh,_=prepare(source)
    report=run_triangular_solver(source['root']/'run.yaml')
    read_triangular_result(source['root']/'result.nc',source['root']/'run.yaml')
    with netCDF4.Dataset(source['root']/'result.nc') as ds:
        h=np.asarray(ds.variables['water_depth'][:]);areas=np.asarray(ds.variables['face_area'][:])
        ids=np.asarray(ds.variables['face_id'][:])
        assert tuple(int(v) for v in ids)==tuple(mesh.face_id)
        assert h.shape==(3,mesh.n_face)
        assert np.max(np.abs(h-.2))<1e-12
        assert np.max(np.abs(h@areas-3.))<1e-12
    assert all(abs(row['residual_m3'])<1e-12 for row in report['mass_budget'])
    _,directory=export_max(source)
    with shapefile.Reader(str(directory/'faces.shp')) as reader:
        assert {record.face_id for record in reader.records()}=={str(v) for v in mesh.face_id}
        polygons=[Polygon(shape.points) for shape in reader.shapes()]
        assert all(len(shape.points)==4 for shape in reader.shapes())
        assert all(p.intersection(source['safe']).area<1e-12 for p in polygons)
        assert unary_union(polygons).symmetric_difference(source['active']).area<1e-12
        assert abs(math.fsum(record.area_m2*record.max_depth for record in reader.records())-3.)<1e-12

def test_two_chambers_cannot_exchange_mass_or_use_global_fill(tmp_path):
    from hydrodynamics.triangular_solver import run_triangular_solver
    source=configure(make_inputs(tmp_path/'two_chambers','boundary_strip_two_chambers'))
    assignments=[]
    for face_id,triangle in zip(source['mesh']['face_id'],source['triangles']):
        assignments.append({'face_id':face_id,'value':.4 if triangle.centroid.x<1. else .1})
    source['business']['initial_condition']={'type':'face_values','quantity':'depth','assignments':assignments,
                                            'source':'independent separated chamber volumes'}
    dump_yaml(source['root']/'business_preparation.yaml',source['business'])
    mesh,_=prepare(source)
    run_triangular_solver(source['root']/'run.yaml')
    left=[i for i,c in enumerate(mesh.face_centroid) if c[0]<1.]
    right=[i for i,c in enumerate(mesh.face_centroid) if c[0]>2.]
    assert len(left)==len(right)==2
    with netCDF4.Dataset(source['root']/'result.nc') as ds:
        h=np.asarray(ds.variables['water_depth'][:]);a=np.asarray(ds.variables['face_area'][:])
        assert np.max(np.abs(h[:,left]-.4))<1e-12
        assert np.max(np.abs(h[:,right]-.1))<1e-12
        assert np.max(np.abs(h[:,left]@a[left]-.8))<1e-12
        assert np.max(np.abs(h[:,right]@a[right]-.4))<1e-12
        assert np.max(np.abs(h@a-1.2))<1e-12

def test_changed_security_source_invalidates_old_preparation_result_and_shp(tmp_path):
    from hydrodynamics.mesh_preparation import read_ugrid_preparation
    from hydrodynamics.triangular_solver import run_triangular_solver
    from hydrodynamics.triangular_solver_netcdf import read_triangular_result
    from hydrodynamics.triangular_shp_export import validate_native_triangle_shp_package
    source=configure(make_inputs(tmp_path/'drift'));prepare(source)
    run_triangular_solver(source['root']/'run.yaml')
    request,_=export_max(source)
    path=source['root']/'security.geojson'
    data=json.loads(path.read_bytes());data['features'][0]['geometry']['coordinates']=[[[1.,1.],[2.5,1.],[2.5,2.],[1.,2.],[1.,1.]]]
    path.write_text(json.dumps(data),encoding='utf-8')
    for operation in [
        lambda:read_ugrid_preparation(source['root']/'prepared_mesh.nc'),
        lambda:read_triangular_result(source['root']/'result.nc',source['root']/'run.yaml'),
        lambda:validate_native_triangle_shp_package(request),
    ]:
        with pytest.raises((ValueError,RuntimeError,OSError)):operation()

def test_forged_rehashed_p2_area_evidence_is_not_trusted(tmp_path):
    from hydrodynamics.mesh_preparation import read_ugrid_preparation
    from hydrodynamics.security_exclusion import SecurityExclusionEvidence,security_evidence_sha256
    source=configure(make_inputs(tmp_path/'forged'));prepare(source)
    with netCDF4.Dataset(source['root']/'prepared_mesh.nc','r+') as ds:
        data=json.loads(ds.security_exclusion_json)
        data['original_domain_area_m2']=17.;data['active_domain_area_m2']=16.;data['active_mesh_area_m2']=16.
        evidence=SecurityExclusionEvidence.model_validate(data)
        ds.security_exclusion_json=json.dumps(data)
        ds.security_exclusion_sha256=security_evidence_sha256(evidence)
    with pytest.raises((ValueError,RuntimeError,OSError)):
        read_ugrid_preparation(source['root']/'prepared_mesh.nc')

def prepare_sources(source,kind='rainfall'):
    from hydrodynamics.triangular_sources import prepare_triangular_sources
    manning={'enabled':False}
    rainfall={'enabled':False}
    if kind=='manning':
        manning={'enabled':True,'legacy_unit1_policy':'legacy_unit1_as_si_manning',
                 'interpretation_rationale':'independent synthetic SI Manning check'}
    else:
        rainfall={'enabled':True,'unit':'m/s','intervals':[{'start_time_s':0.,'end_time_s':1.,'intensity':1e-4}]}
    config={'format':'triangular-source-preparation-v1','schema_version':'1.0',
            'case_id':source['run']['case_id'],'run_config_file':'run.yaml','output_file':'sources.json',
            'source_cfl_fraction':.5,'manning':manning,'rainfall':rainfall}
    path=source['root']/'sources.yaml';dump_yaml(path,config)
    result=prepare_triangular_sources(path)
    run={'format':'triangular-'+('manning' if kind=='manning' else 'source')+'-run-v1',
         'schema_version':'1.0','case_id':source['run']['case_id'],'sources':kind,
         'base_run_config_file':'run.yaml','source_config_file':'sources.yaml','result_file':kind+'.nc'}
    dump_yaml(source['root']/(kind+'_run.yaml'),run)
    return result

def test_uniform_rain_enters_only_active_area_without_losing_accepted_water(tmp_path):
    from hydrodynamics.triangular_source import run_triangular_source
    source=configure(make_inputs(tmp_path/'rain'))
    mesh,_=prepare(source);prepare_sources(source)
    report=run_triangular_source(source['root']/'rainfall_run.yaml')
    with netCDF4.Dataset(source['root']/'rainfall.nc') as ds:
        h=np.asarray(ds.variables['water_depth'][:]);a=np.asarray(ds.variables['face_area'][:])
        times=np.asarray(ds.variables['time'][:])
        assert h.shape==(3,mesh.n_face)
        expected=.2+times[:,None]*1e-4
        assert np.max(np.abs(h-expected))<1e-12
        assert np.max(np.abs(h@a-(3.+times*.0015)))<1e-12
        assert abs(float((h[-1]-h[0])@a)-.0015)<1e-12
    assert all(abs(row['residual_m3'])<1e-12 for row in report['mass_budget'])
    def scalar_paths(value,path=''):
        if isinstance(value,dict):
            return [item for key,child in value.items() for item in scalar_paths(child,path+'/'+key)]
        if isinstance(value,list):
            return [item for i,child in enumerate(value) for item in scalar_paths(child,path+'/'+str(i))]
        if isinstance(value,(float,int)) and not isinstance(value,bool):return [(path,float(value))]
        return []
    candidates=[(path,value) for path,value in scalar_paths(report)
                if 'rain' in path.lower() and ('volume' in path.lower() or 'm3' in path.lower())
                and any(word in path.lower() for word in ['not_model','unmodel','excluded','outside_domain'])]
    assert any(abs(value-.0001)<1e-15 for _,value in candidates),(
        'report must separately identify the 0.0001 m3 potential rain outside the active domain; '+str(candidates))

@pytest.mark.parametrize('kind',['none','manning','rainfall'])
def test_all_supported_runners_reject_changed_security_before_writing_result(tmp_path,kind):
    from hydrodynamics.triangular_solver import run_triangular_solver
    from hydrodynamics.triangular_manning import run_triangular_manning
    from hydrodynamics.triangular_source import run_triangular_source
    source=configure(make_inputs(tmp_path/kind));prepare(source)
    if kind!='none':prepare_sources(source,kind)
    path=source['root']/'security.geojson'
    data=json.loads(path.read_bytes());data['features'][0]['geometry']['coordinates']=[[[1.,1.],[2.5,1.],[2.5,2.],[1.,2.],[1.,1.]]]
    path.write_text(json.dumps(data),encoding='utf-8')
    if kind=='none':operation=lambda:run_triangular_solver(source['root']/'run.yaml')
    elif kind=='manning':operation=lambda:run_triangular_manning(source['root']/'manning_run.yaml')
    else:operation=lambda:run_triangular_source(source['root']/'rainfall_run.yaml')
    with pytest.raises((ValueError,RuntimeError,OSError)):operation()
    result=source['root']/('result.nc' if kind=='none' else kind+'.nc')
    assert not result.exists(),'security mismatch must be rejected before a native result is written'

def workflow_fixture(folder):
    import importlib.util
    spec=importlib.util.spec_from_file_location('independent_security_workflow_fixture',ROOT/'tests/test_workflow.py')
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    folder.mkdir()
    job=module.job.__wrapped__(folder)
    job['case']['model']['end_time']=.2;job['case']['output']['interval']=.1
    dump_yaml(folder/'case.yaml',job['case'])
    job['write_forcing']([[0.,1.,.1,0.,.1,0.],[.1,1.,.1,0.,.1,0.],[.2,1.,.1,0.,.1,0.]])
    return job

def test_security_enabled_structured_workflow_refuses_without_false_success(tmp_path):
    from hydrodynamics.workflow import run_workflow
    job=workflow_fixture(tmp_path/'structured')
    source=configure(make_inputs(job['root']/'security_source'))
    job['spec']['security_exclusion']={
        'format':'security-area-exclusion-v1','schema_version':'1.0','enabled':True,
        'source_file':'security_source/security.geojson','source_crs':'EPSG:32650','constraint':'excluded_closed_wall'}
    dump_yaml(job['file'],job['spec'])
    with pytest.raises((ValueError,RuntimeError,OSError)) as error:
        run_workflow(job['file'],run_id='must-reject')
    message=str(error.value).lower()
    assert 'structured' in message and ('security' in message or '安全' in message)
    assert not list(job['root'].rglob('result.nc'))
    assert not list(job['root'].rglob('*.shp'))
    for path in job['root'].rglob('run_report.json'):
        assert json.loads(path.read_bytes())['status']!='complete'

def test_native_security_result_is_not_accepted_by_legacy_nc2web(tmp_path):
    from hydrodynamics.triangular_solver import run_triangular_solver
    from hydrodynamics.nc2web import convert_netcdf_to_web
    source=configure(make_inputs(tmp_path/'native_web'));prepare(source)
    run_triangular_solver(source['root']/'run.yaml')
    output=source['root']/'unsupported_web'
    with pytest.raises((ValueError,RuntimeError,OSError)):
        convert_netcdf_to_web(source['root']/'result.nc',output)
    assert not (output/'manifest.json').exists()

@pytest.mark.parametrize('extra',[
    {'total_volume_m3':3.2},
    {'points':[{'x':1.5,'y':1.5,'depth_m':.2}]},
])
def test_unsupported_old_total_volume_or_excluded_positive_point_is_never_silently_discarded(tmp_path,extra):
    from hydrodynamics.mesh_preparation import prepare_mesh
    from hydrodynamics.triangular_business import prepare_triangular_business
    source=configure(make_inputs(tmp_path/'initial_semantics'))
    prepare_mesh(source['root']/'mesh_preparation.yaml')
    source['business']['initial_condition'].update(extra)
    dump_yaml(source['root']/'business_preparation.yaml',source['business'])
    with pytest.raises((ValueError,RuntimeError,OSError)):
        prepare_triangular_business(source['root']/'business_preparation.yaml')
    assert not (source['root']/'business.json').exists()
