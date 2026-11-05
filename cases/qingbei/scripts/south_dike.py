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
import os, pickle, numpy as np, rasterio
os.environ["GDAL_PAM_ENABLED"]="NO"
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import linemerge, unary_union
d=pickle.load(open("parts.pkl","rb")); QB=pickle.load(open("QB.pkl","rb")); D=d["D"]
dq=d["riv"][0][1]
L=linemerge(dq) if dq.geom_type=="MultiLineString" else dq
print(L.geom_type, [round(g.length) for g in getattr(L,"geoms",[L])])
gs=[]
for g in getattr(L,"geoms",[L]):
    c=list(g.coords)
    if c[0][0]>c[-1][0]: c=c[::-1]
    gs.append(c)
gs.sort(key=lambda c:c[0][0]); print("gap", [ (gs[i][-1], gs[i+1][0]) for i in range(len(gs)-1)])
L=LineString([p for c in gs for p in c])
L=L.intersection(D.buffer(50)); L=max(getattr(L,"geoms",[L]),key=lambda g:g.length)
print("centerline in D", round(L.length), L.coords[0], L.coords[-1])
ss=np.arange(0,L.length,25.0); off=np.arange(0,401,1.0)
crest=[];prof=[]
with rasterio.open(str(REPO / 'data/dem/DEMn_1m.tif')) as r:
    for s in ss:
        p=np.array(L.interpolate(s).coords[0]); q=np.array(L.interpolate(min(s+5,L.length)).coords[0]); o=np.array(L.interpolate(max(s-5,0)).coords[0])
        t=(q-o)/np.linalg.norm(q-o); n=np.array([t[1],-t[0]])  # right normal (south when walking east)
        pts=p+off[:,None]*n
        z=np.array([v[0] for v in r.sample([tuple(a) for a in pts])],float); z[z<-1000]=np.nan
        prof.append(z)
        # crest: highest point between 20 and 400 m, but stop at first point where ground drops >1.5 m below crest after crest (outer toe)
        k=20+int(np.nanargmax(z[20:])); crest.append((s,off[k],z[k],z[0],np.nanmin(z[:k+1]),pts[k]))
off_c=np.array([c[1] for c in crest]); zc=np.array([c[2] for c in crest])
print("crest offset percentiles", np.percentile(off_c,[0,10,50,90,100]), "crest z p", np.nanpercentile(zc,[0,10,50,90,100]).round(2))
np.savez("south_dike_raw.npz", s=ss, off=off_c, zc=zc, prof=np.array(prof), cl=np.array(L.coords))
pickle.dump(L,open("centerline.pkl","wb"))
