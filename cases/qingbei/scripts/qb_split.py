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
import numpy as np, shapefile, netCDF4, json
from shapely.geometry import shape, LineString, Polygon, MultiLineString
from shapely.ops import transform, split, unary_union, polygonize
from shapely import contains_xy
from pyproj import Transformer
tr=Transformer.from_crs("EPSG:4490","EPSG:4548",always_xy=True)
base=str(REPO / 'map')
def rd(n):
    r=shapefile.Reader(base+n, encoding="gbk", encodingErrors="replace"); return [(sr.record[0], transform(tr.transform, shape(sr.shape.__geo_interface__))) for sr in r.shapeRecords()]
D=rd(r"\东淀&文安洼_shape")[0][1]; S=rd(r"\securityArea\dd_securityArea_shape")[0][1]; riv=rd(r"\river_track\river_track")
ds=netCDF4.Dataset(str(REPO / 'examples/case_001/results/dongdian_qingbei_2023_288h.nc'))
act=np.array(ds["active_mask"][:])>0; x=np.array(ds["x"][:]); y=np.array(ds["y"][:]); X,Y=np.meshgrid(x,y)
ax, ay = X[act], Y[act]
print("D area", D.area/1e6, "S area", S.area/1e6, "D valid", D.is_valid)
for i,(n,g) in enumerate(riv):
    inter=g.intersection(D); print(i, n, g.geom_type, round(g.length), "in D", round(inter.length))
lines=unary_union([g for n,g in riv]+[D.exterior])
parts=[p for p in polygonize(lines) if D.contains(p.representative_point())]
print("parts", len(parts))
rows=[]
for k,p in enumerate(sorted(parts,key=lambda p:-p.area)):
    n_act=int(contains_xy(p,ax,ay).sum()); rows.append((k,p,n_act))
    print(k, round(p.area/1e6,3),"km2 old-active cells",n_act, "frac", round(n_act*2500/p.area,2), "rp", [round(c) for c in p.representative_point().coords[0]])
import pickle; pickle.dump({"D":D,"S":S,"riv":riv,"parts":[p for _,p,_ in rows],"nact":[n for *_,n in rows]},open("parts.pkl","wb"))
