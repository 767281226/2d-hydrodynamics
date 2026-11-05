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
import pickle, json, numpy as np, shapefile
from shapely.geometry import Point, mapping
from shapely.ops import unary_union
from shapely.validation import make_valid
d=pickle.load(open("parts.pkl","rb")); parts=d["parts"]; S=d["S"]; D=d["D"]
sel=[i for i,n in enumerate(d["nact"]) if n>50]
QB=unary_union([parts[i] for i in sel]).buffer(0.01).buffer(-0.01)
print("sel", sel, "QB", QB.geom_type, round(QB.area/1e6,4), "holes", len(getattr(QB,"interiors",[])), "valid", QB.is_valid)
if QB.geom_type=="Polygon": print("exterior pts", len(QB.exterior.coords))
print("S within QB", QB.contains(S), "S∩QB km2", round(S.intersection(QB).area/1e6,4), "S∩D", round(S.intersection(D).area/1e6,4))
for k,xy in {"A":(478618,4320778),"B":(492931,4324745),"C":(499491,4334475)}.items():
    p=Point(xy); print(k, "dist to QB boundary", round(QB.boundary.distance(p),1), "inside", QB.contains(p))
pickle.dump(QB,open("QB.pkl","wb"))
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"]=["Microsoft YaHei"]
fig,ax=plt.subplots(figsize=(11,11))
x,y=D.exterior.xy; ax.plot(x,y,"k-",lw=.5)
for g in getattr(QB,"geoms",[QB]): x,y=g.exterior.xy; ax.fill(x,y,color="lightblue"); ax.plot(x,y,"b-",lw=1)
x,y=S.exterior.xy; ax.fill(x,y,color="orange",alpha=.7)
for n,g in d["riv"]:
    for l in getattr(g,"geoms",[g]): xx,yy=l.xy; ax.plot(xx,yy,"g-",lw=.8)
for k,xy in {"A":(478618,4320778),"B":(492931,4324745),"C":(499491,4334475)}.items(): ax.plot(*xy,"m*",ms=14); ax.annotate(k,xy,fontsize=14)
ax.set_aspect("equal"); ax.set_xlim(476000,501000); ax.set_ylim(4317000,4336000); ax.set_title(f"清北计算域 {QB.area/1e6:.2f} km²（蓝）、安全区（橙）、河道（绿）")
fig.savefig("qb_domain.png",dpi=65,bbox_inches="tight")
