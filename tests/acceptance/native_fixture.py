from pathlib import Path
import json
import numpy as np
import netCDF4
import yaml
from hydrodynamics.triangular_mesh import TriangularMesh,FaceTerrainField
from hydrodynamics.mesh_preparation import write_ugrid_preparation
from hydrodynamics.triangular_business import TriangularBusinessPackage,TriangularInitialConditionField,TriangularRoughnessField,write_triangular_business_package
from hydrodynamics.triangular_solver import run_triangular_solver
from hydrodynamics.triangular_solver_netcdf import read_triangular_result


def make_case(case_root,dry=False,bed_m=0.0):
    case_root=Path(case_root);case_root.mkdir(parents=True,exist_ok=False)
    nodes=[(500000.,0.),(500003.,0.),(500002.,2.),(500000.,1.)]
    mesh=TriangularMesh(nodes,[(0,1,2),(0,2,3)],'EPSG:32650',mesh_id='independent-native-shp-scalene',face_id=[9007199254740993,9223372036854775806])
    assert np.allclose(mesh.face_area,[3.,1.])
    terrain=FaceTerrainField(mesh_id=mesh.mesh_id,elevation_m=(float(bed_m),float(bed_m)),valid_mask=(True,True),covered_area_m2=tuple(mesh.face_area),valid_area_m2=tuple(mesh.face_area),coverage_ratio=(1.,1.),coordinate_system=mesh.coordinate_system,vertical_datum='independent synthetic local datum',vertical_datum_source='config_declaration')
    mesh_path=case_root/'mesh.nc';write_ugrid_preparation(mesh_path,mesh,terrain)
    depths=[0.,0.] if dry else [.3,.1]
    initial=TriangularInitialConditionField.from_assignments(mesh,terrain,'depth',tuple(zip(mesh.face_id,depths)),source='independent native SHP reference')
    business=TriangularBusinessPackage(mesh,terrain,TriangularRoughnessField.from_constant(mesh,.035,source='retained provenance only'),initial,())
    write_triangular_business_package(case_root/'business.json',business,mesh_artifact=mesh_path)
    config={'format':'triangular-solver-run-v1','schema_version':'1.0','case_id':'independent-native-shp-dry' if dry else 'independent-native-shp-dynamic','business_file':'business.json','result_file':'result.nc','time_origin':None,'output_times_s':[0.,.025,.05],'boundary_policy':'closed_wall','initial_momentum':'zero','sources':'none','gravity_m_s2':9.81,'cfl_number':.25,'minimum_time_step_s':1e-6,'maximum_time_step_s':.005,'maximum_step_retries':8,'maximum_face_count':64}
    run_path=case_root/'run.yaml';run_path.write_text(yaml.safe_dump(config),encoding='utf-8')
    run_triangular_solver(run_path)
    read_triangular_result(case_root/'result.nc',run_path)
    with netCDF4.Dataset(case_root/'result.nc') as ds:
        h=np.asarray(ds.variables['water_depth'][:]);areas=np.asarray(ds.variables['face_area'][:]);times=np.asarray(ds.variables['time'][:]);mx=np.asarray(ds.variables['momentum_x'][:]);my=np.asarray(ds.variables['momentum_y'][:])
    assert np.max(np.abs(h@areas-(0. if dry else 1.)))<1e-12
    if not dry:assert h[-1,0]<.299<h[0,0] and h[-1,1]>h[0,1]
    else:assert np.array_equal(h,np.zeros_like(h))
    return {'root':case_root,'config':run_path,'native':case_root/'result.nc','mesh':mesh,'depth':h,'areas':areas,'times':times,'momentum_x':mx,'momentum_y':my,'bed':np.array([float(bed_m),float(bed_m)])}
