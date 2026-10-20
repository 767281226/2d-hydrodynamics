from pathlib import Path
import numpy as np
import shapefile
import yaml
from shapely.geometry import Polygon

def test_fractional_faces_at_realistic_projected_offsets_remain_exportable(tmp_path):
    from hydrodynamics.triangular_mesh import TriangularMesh,FaceTerrainField
    from hydrodynamics.mesh_preparation import write_ugrid_preparation
    from hydrodynamics.triangular_business import TriangularBusinessPackage,TriangularInitialConditionField,TriangularRoughnessField,write_triangular_business_package
    from hydrodynamics.triangular_solver import run_triangular_solver
    from hydrodynamics.triangular_shp_export import export_native_triangle_shp,validate_native_triangle_shp_package
    root=tmp_path/'projected';root.mkdir()
    nodes=[(474150.1,4299600.1),(474153.3,4299600.2),
           (474152.6,4299602.7),(474150.2,4299601.4)]
    mesh=TriangularMesh(nodes,[(0,1,2),(0,2,3)],'EPSG:4548',
                        mesh_id='independent-fractional-projected-faces',face_id=[901,415])
    areas=tuple(mesh.face_area)
    terrain=FaceTerrainField(mesh_id=mesh.mesh_id,elevation_m=(1.25,1.25),valid_mask=(True,True),
        covered_area_m2=areas,valid_area_m2=areas,coverage_ratio=(1.,1.),coordinate_system=mesh.coordinate_system,
        vertical_datum='Independent projected precision synthetic datum',vertical_datum_source='config_declaration')
    write_ugrid_preparation(root/'mesh.nc',mesh,terrain)
    initial=TriangularInitialConditionField.from_assignments(mesh,terrain,'depth',[(v,.2) for v in mesh.face_id],source='independent constant depth')
    business=TriangularBusinessPackage(mesh,terrain,TriangularRoughnessField.from_constant(mesh,.035,source='synthetic only'),initial,())
    write_triangular_business_package(root/'business.json',business,mesh_artifact=root/'mesh.nc')
    config={'format':'triangular-solver-run-v1','schema_version':'1.0','case_id':'projected-fractional-native',
        'business_file':'business.json','result_file':'result.nc','time_origin':None,'output_times_s':[0.,.025,.05],
        'boundary_policy':'closed_wall','initial_momentum':'zero','sources':'none','gravity_m_s2':9.81,
        'cfl_number':.25,'minimum_time_step_s':1e-6,'maximum_time_step_s':.005,'maximum_step_retries':8,'maximum_face_count':64}
    (root/'run.yaml').write_text(yaml.safe_dump(config),encoding='utf-8')
    run_triangular_solver(root/'run.yaml')
    request={'format':'native-triangle-result-shp-export-request-v1','schema_version':'1.0',
        'run_config_file':'run.yaml','output_directory':'shp',
        'selector':{'aggregation':'FRAME','frame_index':2,'time_s':.05},
        'scope_selection':'FULL_DOMAIN','wet_depth_threshold_m':0.,'target_crs':'NATIVE'}
    path=root/'export.yaml';path.write_text(yaml.safe_dump(request),encoding='utf-8')
    assert export_native_triangle_shp(path).valid
    assert validate_native_triangle_shp_package(path).valid
    with shapefile.Reader(str(root/'shp/faces.shp')) as reader:
        assert len(reader)==2
        by_id={str(row.record.face_id):row for row in reader.iterShapeRecords()}
        for index,face_id in enumerate(mesh.face_id):
            row=by_id[str(face_id)];points=row.shape.points
            assert len(points)==4 and len(set(points[:-1]))==3
            assert set(tuple(p) for p in points[:-1])=={tuple(mesh.node_xy[n]) for n in mesh.face_nodes[index]}
            assert abs(Polygon(points).area-areas[index])<1e-12
            assert abs(row.record.area_m2-areas[index])<1e-12
