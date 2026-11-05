"""Make the safety-zone boundary a closed wall: duplicate nodes on the interface between S faces and outside faces."""
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
import json, hashlib, numpy as np, shapefile
from shapely.geometry import shape
from shapely.ops import transform
from shapely import contains_xy
from pyproj import Transformer
src='qb_ext_s25_domain-mesh.json'
a=json.load(open(src,encoding="utf-8")); xy=np.array(a["node_xy"]); fn=np.array(a["face_nodes"])
r=shapefile.Reader(str(REPO / 'map/securityArea/dd_securityArea_shape'),encoding="gbk")
S=transform(Transformer.from_crs("EPSG:4490","EPSG:4548",always_xy=True).transform, shape(r.shape(0).__geo_interface__))
cen=xy[fn].mean(1); ins=contains_xy(S,cen[:,0],cen[:,1])
print("S faces", ins.sum(), "area km2", round(sum(abs(np.cross(xy[f[1]]-xy[f[0]],xy[f[2]]-xy[f[0]]))/2 for f in fn[ins])/1e6,4), "S polygon", round(S.area/1e6,4))
nin=set(fn[ins].ravel()); nout=set(fn[~ins].ravel()); shared=sorted(nin&nout)
print("interface nodes", len(shared))
new=np.arange(len(xy), len(xy)+len(shared)); remap=dict(zip(shared,new))
fn2=fn.copy()
for k in np.where(ins)[0]:
    fn2[k]=[remap.get(int(v),int(v)) for v in fn[k]]
dup=xy[shared].copy()
for k,nd in enumerate(shared):
    fs=np.where(ins & (fn==nd).any(1))[0]; c=xy[fn[fs]].reshape(-1,2).mean(0); d=c-xy[nd]; dup[k]=xy[nd]+1e-3*d/np.linalg.norm(d)
xy2=np.vstack([xy, dup])  # S-side copies moved 1 mm into S so the two walls are distinct boundaries
# rebuild ordered exterior loops
e=np.sort(np.concatenate([fn2[:,[0,1]],fn2[:,[1,2]],fn2[:,[2,0]]]),1); u,c=np.unique(e,axis=0,return_counts=True); ext=u[c==1]
adj={}
for i,j in ext: adj.setdefault(int(i),[]).append(int(j)); adj.setdefault(int(j),[]).append(int(i))
assert all(len(v)==2 for v in adj.values()), "non-manifold boundary"
seen=set(); loops=[]
for st in adj:
    if st in seen: continue
    loop=[]; prev=None; cur=st
    while True:
        seen.add(cur); nxt=[n for n in adj[cur] if n!=prev][0] if prev is not None else adj[cur][0]
        loop.append([cur,nxt]); prev,cur=cur,nxt
        if cur==st: break
    loops.append(loop)
loops.sort(key=len, reverse=True); print("exterior loops", [len(l) for l in loops])
a["node_xy"]=xy2.tolist(); a["face_nodes"]=fn2.tolist()
a["boundary_groups"]={"outer_boundary":loops[0], **{f"security_wall_{i}":l for i,l in enumerate(loops[1:])}}
a["mesh_id"]="qb_ext_s25_Swall_"+hashlib.sha256(fn2.tobytes()+xy2.tobytes()).hexdigest()[:16]
a["provenance"]["security_wall"]=("safety zone meshed but disconnected: nodes on the interface between faces whose centroid lies in "
  "map/securityArea/dd_securityArea_shape (EPSG:4490->4548) and outside faces are duplicated (S-side copy moved 1 mm into S), so that interface is a closed wall "
  "(embankment assumed not to overtop, user decision 2026-11-04). Faces, face_id and face order unchanged.")
out='qb_ext_s25_Swall_domain-mesh.json'
json.dump(a,open(out,"w",encoding="utf-8"),ensure_ascii=False)
np.savez_compressed("mesh_qbext_Swall.npz",nodes_xy=xy2,faces=fn2,face_id=np.array(a["face_id"]),crs=np.array("EPSG:4548"),mesh_id=np.array(a["mesh_id"]),outer_boundary=xy2[[p[0] for p in loops[0]]],hole_0=np.zeros((0,2)))
np.save("S_face_mask_ext.npy", ins)
print(out, hashlib.sha256(open(out,"rb").read()).hexdigest())
