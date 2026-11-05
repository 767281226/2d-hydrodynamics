"""Direct generate_gmsh_mesh on the 清北+大清河 domain (S meshed, dike constraints). Not the restricted-worker MESH_READY route."""
# --- repo-relative paths (added when moved into cases/qingbei/scripts) ---
import os as _os, sys as _sys
from pathlib import Path as _Path
REPO = _Path(__file__).resolve().parents[3]
CASE = REPO / "cases" / "qingbei"
WORK = REPO / "work" / "cases" / "qingbei"
WORK.mkdir(parents=True, exist_ok=True)
_os.chdir(WORK)  # intermediates (*.pkl, *.npz, *.png) are written here, not in the repo
_sys.path.insert(0, str(REPO))
# -------------------------------------------------------------------------
import sys, json, time, hashlib, math
import numpy as np
from hydrodynamics.mesh_preparation import GmshMeshConfig
from hydrodynamics.mesh_generation import generate_gmsh_mesh
g=json.load(open('qb_geom_ext.json'))
cfg=GmshMeshConfig(type="gmsh", outer_boundary=g["outer_boundary"], constraints=[{"name":c["name"],"points":c["points"]} for c in g["constraints"]],
                   default_size_m=50.0, minimum_size_m=1.0, maximum_nodes=2_000_000)
plan={"format":"triangle-mesh-algorithm-plan-v1","schema_version":"1.0","algorithm_2d":5,"smoothing":1,"algorithm_switch_on_failure":0,"optimizer":"none","iterations":0,"force":False,"entity_scope":"active_surface"}
receipt={}; t=time.perf_counter()
mesh=generate_gmsh_mesh(cfg, coordinate_system="EPSG:4548", geometry_tolerance_m=1e-8, minimum_face_area_m2=1e-12,
                        mesh_algorithm_plan=plan, generation_receipt=receipt, boundary_adaptive_sizing={"enabled":True,"grading_ratio":0.3})
print("generated", mesh.n_node, mesh.n_face, round(time.perf_counter()-t,1), "s", flush=True)
xy=np.asarray(mesh.node_xy,float); fn=np.asarray(mesh.face_nodes,int); P=xy[fn]
a=0.5*((P[:,1,0]-P[:,0,0])*(P[:,2,1]-P[:,0,1])-(P[:,2,0]-P[:,0,0])*(P[:,1,1]-P[:,0,1]))
e=np.stack([np.hypot(*(P[:,(i+1)%3]-P[:,i]).T) for i in range(3)],1)
q=4*math.sqrt(3)*np.abs(a)/(e**2).sum(1); r=np.abs(a)/e.sum(1)
print("min q", q.min(), "faces q<0.1", int((q<0.1).sum()), "min area", np.abs(a).min(), "min r", r.min(), "r<1", int((r<1).sum()), "r<1.5", int((r<1.5).sum()), "area km2", np.abs(a).sum()/1e6)
fid=[int(v) for v in mesh.face_id]
art={"format":"triangle-domain-mesh-only-artifact-v1","mesh_only":True,"coordinate_system":"EPSG:4548","geometry_tolerance_m":1e-8,"minimum_face_area_m2":1e-12,
     "mesh_id":"qb_ext_s25_" + hashlib.sha256(fn.tobytes()+xy.tobytes()).hexdigest()[:16],
     "node_xy":xy.tolist(),"face_nodes":fn.tolist(),"face_id":fid,
     "boundary_groups":{"outer_boundary":g["outer_boundary"]},"constraint_groups":{c["name"]:c["points"] for c in g["constraints"]},
     "provenance":{"route":"direct hydrodynamics.mesh_generation.generate_gmsh_mesh (not restricted-worker MESH_READY)","domain":"清北 (old 55.74 km2 activation, D split by OSM river centrelines) + 大清河 channel to DEM-tracked south dike crest + low land north of 大清河 between the 清北 lobes (x 484-489.6 km, DEM-valid, user-confirmed part of 清北); boundary simplified at 25 m; S meshed with S ring and +30 m dike toe ring as constraints (uniform ~25 m spacing, each with a one-segment gap)",
                   "mesh_algorithm_plan":plan,"boundary_adaptive_sizing":{"enabled":True,"grading_ratio":0.3},"default_size_m":50.0,"minimum_size_m":1.0,
                   "quality":{"min_q":float(q.min()),"faces_q_lt_0_1":int((q<0.1).sum()),"min_r_m":float(r.min())},"receipt":receipt}}
out='qb_ext_s25_domain-mesh.json'
json.dump(art,open(out,"w",encoding="utf-8"),default=str,ensure_ascii=False)
np.savez_compressed('mesh_qbext.npz',nodes_xy=xy,faces=fn,face_id=np.array(fid),crs=np.array("EPSG:4548"),mesh_id=np.array(art["mesh_id"]),
                    outer_boundary=np.array(g["outer_boundary"]),hole_0=np.zeros((0,2)))
print("wrote", out)
