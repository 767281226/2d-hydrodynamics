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
from rasterio.windows import from_bounds
from rasterio import features
from shapely.geometry import shape, box, Polygon, LineString
from shapely.ops import unary_union, split
d=pickle.load(open('parts.pkl',"rb")); D=d["D"]
dom=pickle.load(open('domain_qb.pkl',"rb"))["domain"]
L=pickle.load(open('centerline.pkl',"rb"))
b=(483800,4323500,489600,4327500)
with rasterio.open(str(REPO / 'data/dem/DEMn_1m.tif')) as r:
    w=from_bounds(*b,r.transform); z=r.read(1,window=w); tr=r.window_transform(w); nd=r.nodata
valid=(z!=nd)&np.isfinite(z)
f=10; v=valid[:valid.shape[0]//f*f,:valid.shape[1]//f*f].reshape(valid.shape[0]//f,f,valid.shape[1]//f,f).all(axis=(1,3))
tr10=tr*tr.scale(f,f)
V=unary_union([shape(g) for g,val in features.shapes(v.astype(np.uint8),transform=tr10) if val==1]).buffer(-5).buffer(5)
# north of centreline: polygon from centreline extended to box top
cl=LineString([(b[0]-10, L.interpolate(L.project(__import__("shapely").geometry.Point(b[0],4324000))).y)]+[c for c in L.coords if b[0]<c[0]<b[2]]+[(b[2]+10, L.interpolate(L.project(__import__("shapely").geometry.Point(b[2],4324500))).y)])
north=Polygon(list(cl.coords)+[(b[2]+10,b[3]),(b[0]-10,b[3])]).buffer(0)
ext=box(*b).intersection(V).intersection(north).difference(dom.buffer(0.5))
ext=unary_union([g for g in getattr(ext,"geoms",[ext]) if g.area>5000 and g.distance(dom)<1.0])
new=unary_union([dom,ext]).buffer(1).buffer(-1)
if new.geom_type=="MultiPolygon": new=max(new.geoms,key=lambda g:g.area)
new=Polygon(new.exterior)
print("extension km2", round(ext.area/1e6,3), "new domain km2", round(new.area/1e6,3), "valid", new.is_valid, "verts", len(new.exterior.coords))
pickle.dump({"domain":new,"extension":ext},open("domain_qb_ext.pkl","wb"))
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"]=["Microsoft YaHei"]
fig,ax=plt.subplots(figsize=(14,9)); x,y=new.exterior.xy; ax.fill(x,y,color="lightblue"); ax.plot(x,y,"b-",lw=.6)
for g in getattr(ext,"geoms",[ext]): x,y=g.exterior.xy; ax.fill(x,y,color="orange",alpha=.6)
x,y=D.exterior.xy; ax.plot(x,y,"k-",lw=.5); ax.set_aspect("equal"); ax.set_xlim(476500,501000); ax.set_ylim(4319000,4336000)
ax.set_title(f"扩展后计算域 {new.area/1e6:.2f} km²（橙=新增 {ext.area/1e6:.2f} km²）"); fig.savefig("domain_ext.png",dpi=60,bbox_inches="tight")
