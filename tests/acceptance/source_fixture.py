from pathlib import Path
import hashlib
import importlib.util
import json
import sys
import numpy as np
import yaml

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

def dump_yaml(path,data):
    path.write_text(yaml.safe_dump(data,sort_keys=False),encoding='utf-8')

def make_case(root,kind='manning',*,security=False,zero=False,dry=False):
    from hydrodynamics.triangular_mesh import TriangularMesh,FaceTerrainField
    from hydrodynamics.mesh_preparation import write_ugrid_preparation
    from hydrodynamics.triangular_business import TriangularBusinessPackage,TriangularInitialConditionField,TriangularRoughnessField,write_triangular_business_package
    root=Path(root)
    if security:
        for name in ['security_fixture','test_security_integration_independent']:
            path=ROOT/'tests/acceptance'/f'{name}.py'
            spec=importlib.util.spec_from_file_location(name,path)
            module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
        fixture=sys.modules['security_fixture'];helper=sys.modules['test_security_integration_independent']
        old=helper.configure(fixture.make_inputs(root))
        mesh,_=helper.prepare(old)
        depths=[0. if dry else (.2 if zero else .2+.02*point[0]) for point in mesh.face_centroid]
        from hydrodynamics.mesh_preparation import read_ugrid_preparation
        mesh,terrain=read_ugrid_preparation(root/'prepared_mesh.nc')
        # Keep the actual P2 security evidence bound through the existing writer.
        from hydrodynamics.triangular_business import read_triangular_business_package
        prior=read_triangular_business_package(root/'business.json')
        initial=TriangularInitialConditionField.from_assignments(mesh,terrain,'depth',list(zip(mesh.face_id,depths)),source='independent active source fixture')
        business=TriangularBusinessPackage(mesh,terrain,TriangularRoughnessField.from_constant(mesh,.5,source='synthetic SI declaration only'),initial,(),security_exclusion=prior.security_exclusion)
        write_triangular_business_package(root/'business.json',business,mesh_artifact=root/'prepared_mesh.nc')
        times=[0.,.5,1.];rain_rate=.0001;mesh_file='prepared_mesh.nc'
    else:
        root.mkdir(parents=True)
        nodes=[(474150.1,4299600.1),(474153.3,4299600.2),(474152.6,4299602.7),(474150.2,4299601.4)]
        mesh=TriangularMesh(nodes,[(0,1,2),(0,2,3)],'EPSG:4548',mesh_id='independent-native-source-v2',
                            face_id=[9007199254740993,9223372036854775806])
        areas=tuple(mesh.face_area)
        terrain=FaceTerrainField(mesh_id=mesh.mesh_id,elevation_m=(1.25,1.25),valid_mask=(True,True),
            covered_area_m2=areas,valid_area_m2=areas,coverage_ratio=(1.,1.),coordinate_system=mesh.coordinate_system,
            vertical_datum='Independent source SHP synthetic datum',vertical_datum_source='config_declaration')
        depths=[0.,0.] if dry else ([.2,.2] if zero else [.3,.1])
        write_ugrid_preparation(root/'mesh.nc',mesh,terrain)
        initial=TriangularInitialConditionField.from_assignments(mesh,terrain,'depth',list(zip(mesh.face_id,depths)),source='independent dynamic source fixture')
        business=TriangularBusinessPackage(mesh,terrain,TriangularRoughnessField.from_constant(mesh,.5,source='synthetic SI declaration only'),initial,())
        write_triangular_business_package(root/'business.json',business,mesh_artifact=root/'mesh.nc')
        times=[0.,.025,.05];rain_rate=.02;mesh_file='mesh.nc'
    base={'format':'triangular-solver-run-v1','schema_version':'1.0','case_id':'independent-native-source-v2',
          'business_file':'business.json','result_file':'uncomputed_base_none.nc','time_origin':None,
          'output_times_s':times,'boundary_policy':'closed_wall','initial_momentum':'zero','sources':'none',
          'gravity_m_s2':9.81,'cfl_number':.25,'minimum_time_step_s':1e-6,
          'maximum_time_step_s':.005 if not security else .02,'maximum_step_retries':8,'maximum_face_count':64}
    dump_yaml(root/'base.yaml',base)
    case={'root':root,'mesh':mesh,'terrain':terrain,'areas':np.asarray(mesh.face_area),
          'initial_depth':np.asarray(depths),'times':np.asarray(times),'base':base,
          'initial_volume':float(np.asarray(depths)@np.asarray(mesh.face_area)),
          'rain_rate':rain_rate,'mesh_file':mesh_file,'security':security}
    return add_family(case,kind,zero=zero)

def add_family(case,kind,*,zero=False):
    from hydrodynamics.triangular_sources import prepare_triangular_sources
    from hydrodynamics.triangular_manning import run_triangular_manning
    from hydrodynamics.triangular_source import run_triangular_source
    from hydrodynamics.triangular_manning_netcdf import read_triangular_manning_result
    from hydrodynamics.triangular_source_netcdf import read_triangular_source_result
    root=case['root'];manning=kind in {'manning','manning_and_rainfall'};rain=kind!='manning'
    prefix=kind+('_zero' if zero else '')
    prep={'format':'triangular-source-preparation-v1','schema_version':'1.0','case_id':case['base']['case_id'],
          'run_config_file':'base.yaml','output_file':prefix+'_preparation.json','source_cfl_fraction':.5,
          'manning':({'enabled':True,'legacy_unit1_policy':'legacy_unit1_as_si_manning',
                      'interpretation_rationale':'independent source SHP SI-unit interpretation'} if manning else {'enabled':False}),
          'rainfall':({'enabled':True,'unit':'m/s','intervals':[{'start_time_s':0.,'end_time_s':float(case['times'][-1]),
                       'intensity':0. if zero else case['rain_rate']}]} if rain else {'enabled':False})}
    prep_config=root/(prefix+'_preparation.yaml');dump_yaml(prep_config,prep)
    prepare_triangular_sources(prep_config)
    run={'format':'triangular-manning-run-v1' if kind=='manning' else 'triangular-source-run-v1',
         'schema_version':'1.0','case_id':case['base']['case_id'],'sources':kind,
         'base_run_config_file':'base.yaml','source_config_file':prep_config.name,'result_file':prefix+'.nc'}
    config=root/(prefix+'_run.yaml');dump_yaml(config,run)
    report=(run_triangular_manning if kind=='manning' else run_triangular_source)(config)
    result=(read_triangular_manning_result if kind=='manning' else read_triangular_source_result)(root/run['result_file'],config)
    depth=np.asarray(result.water_depth_m);areas=case['areas'];volumes=depth@areas
    rate=0. if zero or not rain else case['rain_rate']
    expected=case['initial_volume']+case['times']*rate*float(areas.sum())
    assert np.max(np.abs(volumes-expected))<1e-12
    assert not (root/'uncomputed_base_none.nc').exists()
    return {**case,'kind':kind,'zero':zero,'run_config':config,'native':root/run['result_file'],
            'result':result,'report':report,'depth':depth,'volumes':volumes,'rate':rate}

def file_hashes(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(root).rglob('*') if p.is_file()}

def request(case,name,*,aggregation='FRAME',scope='FULL_DOMAIN',target='NATIVE',start=None):
    times=case['times']
    selector=({'aggregation':'FRAME','frame_index':len(times)-1,'time_s':float(times[-1])} if aggregation=='FRAME'
              else {'aggregation':'MAX_OVER_WINDOW','window_start_s':float(times[0] if start is None else start),
                    'window_end_s':float(times[-1])})
    data={'format':'native-triangle-result-shp-export-request-v2','schema_version':'2.0',
          'result_kind':case['kind'],'run_config_file':case['run_config'].name,'output_directory':name,
          'selector':selector,'scope_selection':scope,'wet_depth_threshold_m':0.,'target_crs':target}
    path=case['root']/(name+'.yaml');dump_yaml(path,data)
    return path,data,case['root']/name
