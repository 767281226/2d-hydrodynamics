from pathlib import Path
import hashlib
import json
import math
from shapely import affinity,wkt
from shapely.geometry import Polygon,mapping
from shapely.ops import triangulate,unary_union
import yaml

ROOT=Path(__file__).resolve().parents[2]
REFERENCES=ROOT/'tests/acceptance/data/security_exclusion_references_v1.json'
REFERENCE_SHA='b7bb12a783f711e114bc8706ebda5c21b9c6f5c344a0c732a314b804c0e4b370'

def references():
    raw=REFERENCES.read_bytes()
    assert hashlib.sha256(raw).hexdigest()==REFERENCE_SHA
    return json.loads(raw)['cases']

def make_inputs(root,reference_id='internal_square_hole',*,full_domain_mesh=False,omit_last_face=False):
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    reference=next(item for item in references() if item['id']==reference_id)
    domain=affinity.translate(wkt.loads(reference['domain_wkt']),xoff=0.)
    safe_parts=[affinity.translate(wkt.loads(text),xoff=0.) for text in reference['safe_wkt']]
    safe=unary_union(safe_parts)
    active=domain.difference(safe)
    assert abs(domain.area-reference['domain_area'])<1e-12
    assert abs(active.area-reference['active_area'])<1e-12
    target=domain if full_domain_mesh else active
    triangles=[t for t in triangulate(target) if target.covers(t)]
    assert triangles and len(triangles)<=64
    assert unary_union(triangles).symmetric_difference(target).area<1e-12
    if omit_last_face:triangles.pop()
    coords=sorted(set(tuple(p) for t in triangles for p in list(t.exterior.coords)[:-1]))
    indices={p:i for i,p in enumerate(coords)}
    faces=[[indices[tuple(p)] for p in list(t.exterior.coords)[:-1]] for t in triangles]
    edge_counts={}
    for face in faces:
        for a,b in zip(face,face[1:]+face[:1]):
            edge=tuple(sorted((a,b)));edge_counts[edge]=edge_counts.get(edge,0)+1
    boundary=[list(edge) for edge,count in edge_counts.items() if count==1]
    mesh={'format':'triangular-mesh-json-v1','mesh_id':'independent-security-'+reference_id,
          'coordinate_system':'EPSG:32650','node_xy':coords,'face_nodes':faces,
          'face_id':[9007199254741001+17*i for i in range(len(faces))],
          'boundary_groups':{'closed_boundary':boundary},'constraint_groups':{}}
    def dump(name,value):
        (root/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    dump('mesh.json',mesh)
    dump('domain.geojson',{'type':'FeatureCollection','crs':{'type':'name','properties':{'name':'EPSG:32650'}},
        'features':[{'type':'Feature','id':'trusted-original-domain','properties':{},'geometry':mapping(domain)}]})
    dump('security.geojson',{'type':'FeatureCollection','crs':{'type':'name','properties':{'name':'EPSG:32650'}},
        'features':[{'type':'Feature','id':'security-'+str(i),'properties':{},'geometry':mapping(p)} for i,p in enumerate(safe_parts)]})
    xmin,ymin,xmax,ymax=domain.bounds
    # Each fixture covers at most 4x4 m. A small 6x6 constant raster encloses it.
    dump('dem.json',{'format':'synthetic-dem-json-v1','coordinate_system':'EPSG:32650',
        'transform':[1.,0.,xmin-1.,0.,-1.,ymax+1.],
        'vertical_unit':'m','vertical_datum':'Independent security synthetic datum',
        'elevation':[[0.]*6 for _ in range(6)]})
    preparation={'version':1,'coordinate_system':'EPSG:32650','mesh':{'type':'json','file':'mesh.json'},
        'terrain':{'type':'json','file':'dem.json','minimum_valid_coverage':1.,
                   'vertical_unit':'m','vertical_datum':'Independent security synthetic datum'},
        'output_file':'prepared_mesh.nc','maximum_faces':64,'maximum_dem_pixels':64,
        'maximum_overlap_checks':4096}
    (root/'mesh_preparation.yaml').write_text(yaml.safe_dump(preparation),encoding='utf-8')
    business={'version':1,'mesh_file':'prepared_mesh.nc','output_file':'business.json',
        'roughness':{'type':'constant','manning_n':.035,'source':'independent synthetic fixture'},
        'initial_condition':{'type':'constant','quantity':'depth','value_m':reference.get('initial_depth',.2),
                             'source':'independent active-domain constant depth'},'boundary_bindings':[]}
    (root/'business_preparation.yaml').write_text(yaml.safe_dump(business),encoding='utf-8')
    run={'format':'triangular-solver-run-v1','schema_version':'1.0','case_id':'independent-security-'+reference_id,
        'business_file':'business.json','result_file':'result.nc','time_origin':None,'output_times_s':[0.,.5,1.],
        'boundary_policy':'closed_wall','initial_momentum':'zero','sources':'none','gravity_m_s2':9.81,
        'cfl_number':.25,'minimum_time_step_s':1e-6,'maximum_time_step_s':.02,
        'maximum_step_retries':8,'maximum_face_count':64}
    (root/'run.yaml').write_text(yaml.safe_dump(run),encoding='utf-8')
    return {'root':root,'reference':reference,'domain':domain,'safe':safe,'active':active,
            'mesh':mesh,'triangles':triangles,'preparation':preparation,'business':business,'run':run}

def assert_actual_face_coverage(mesh,source):
    triangles=[Polygon([mesh.node_xy[n] for n in face]) for face in mesh.face_nodes]
    assert all(t.intersection(source['safe']).area<1e-12 for t in triangles)
    union=unary_union(triangles)
    assert union.symmetric_difference(source['active']).area<1e-12
    assert abs(math.fsum(t.area for t in triangles)-union.area)<1e-12
    assert abs(math.fsum(mesh.face_area)-source['reference']['active_area'])<1e-12
    return triangles
