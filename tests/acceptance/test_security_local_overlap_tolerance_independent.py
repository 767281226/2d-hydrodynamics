from pathlib import Path
import json
import sys
import pytest
import yaml
from shapely.geometry import Polygon,box,mapping
from shapely.ops import triangulate,unary_union

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

@pytest.mark.parametrize('side_m',[.05,.001,.00001])
def test_large_domain_never_hides_a_valid_native_face_wholly_inside_security_area(tmp_path,side_m):
    from hydrodynamics.triangular_mesh import TriangularMesh
    from hydrodynamics.security_exclusion import load_security_exclusion_input,load_security_area_geometry,assess_security_domain,check_mesh_against_security
    domain=box(0.,0.,100000.,10000.)
    safe=box(10.,10.,11.,11.)
    active=domain.difference(safe)
    triangles=[t for t in triangulate(active) if active.covers(t)]
    assert unary_union(triangles).symmetric_difference(active).area<1e-8
    rogue=Polygon([(10.4,10.4),(10.4+side_m,10.4),(10.4,10.4+side_m)])
    assert safe.contains(rogue) and rogue.area>1e-12
    triangles.append(rogue)
    assert len(triangles)<=64
    nodes=sorted(set(tuple(p) for t in triangles for p in list(t.exterior.coords)[:-1]))
    indices={p:i for i,p in enumerate(nodes)}
    faces=[[indices[tuple(p)] for p in list(t.exterior.coords)[:-1]] for t in triangles]
    mesh=TriangularMesh(nodes,faces,'EPSG:32650',mesh_id='independent-large-domain-intrusion',
                        face_id=list(range(100,100+len(faces))))
    source=tmp_path/'safety.geojson'
    source.write_text(json.dumps({'type':'Feature','properties':{},'geometry':mapping(safe)}),encoding='utf-8')
    policy_file=tmp_path/'policy.yaml'
    policy_file.write_text(yaml.safe_dump({'format':'security-area-exclusion-v1','schema_version':'1.0',
        'enabled':True,'source_file':source.name,'source_crs':'EPSG:32650','constraint':'excluded_closed_wall'}),encoding='utf-8')
    policy,_,_=load_security_exclusion_input(policy_file)
    geometry=load_security_area_geometry(policy,policy_file,'EPSG:32650')
    assessment=assess_security_domain(domain,geometry)
    with pytest.raises((ValueError,RuntimeError,OSError)):
        check_mesh_against_security(mesh,assessment,tolerance_m=mesh.geometry_tolerance_m)
