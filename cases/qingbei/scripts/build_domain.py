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
import pickle, numpy as np, json
from shapely.geometry import LineString, Polygon, Point, mapping
from shapely.ops import unary_union
d=np.load("south_dike_raw.npz"); P=d["prof"]; ss=d["s"]
L=pickle.load(open("centerline.pkl","rb")); parts=pickle.load(open("parts.pkl","rb")); D=parts["D"]; S=parts["S"]; QB=pickle.load(open("QB.pkl","rb"))
# continuity tracking of the crest: start at median of first 20, then search +-25 m of previous pick
prev=float(np.median(d["off"][:20])); offs=[]
for k in range(len(ss)):
    z=P[k]; lo=max(20,int(prev-25)); hi=min(400,int(prev+25)); j=lo+int(np.nanargmax(z[lo:hi+1])); offs.append(j); prev=j
offs=np.array(offs,float)
# median smoothing (window 5)
sm=np.array([np.median(offs[max(0,i-2):i+3]) for i in range(len(offs))])
zc=np.array([P[k][int(sm[k])] for k in range(len(ss))])
print("tracked offset p", np.percentile(sm,[0,5,50,95,100]), "crest z p", np.percentile(zc,[0,5,50,95,100]).round(2))
pts=[];cl=[]
for s,o in zip(ss,sm):
    p=np.array(L.interpolate(s).coords[0]); q=np.array(L.interpolate(min(s+5,L.length)).coords[0]); r=np.array(L.interpolate(max(s-5,0)).coords[0])
    t=(q-r)/np.linalg.norm(q-r); n=np.array([t[1],-t[0]]); pts.append(tuple(p+o*n)); cl.append(tuple(p))
dike=LineString(pts).simplify(2.0)
chan=Polygon(cl+pts[::-1]).buffer(0)
dom=unary_union([QB,chan]).intersection(D).buffer(0.5).buffer(-0.5)
print("chan km2", round(chan.area/1e6,3), "domain", dom.geom_type, round(dom.area/1e6,3))
if dom.geom_type=="MultiPolygon":
    print([round(g.area/1e6,4) for g in dom.geoms]); dom=max(dom.geoms,key=lambda g:g.area)
dom=Polygon(dom.exterior)  # drop slivers holes
print("final domain", round(dom.area/1e6,3), "km2, exterior vertices", len(dom.exterior.coords), "S inside", dom.contains(S))
for k,xy in {"A":(478618,4320778),"B":(492931,4324745),"C":(499491,4334475)}.items():
    print(k,"dist to boundary",round(dom.boundary.distance(Point(xy)),1))
pickle.dump({"domain":dom,"dike":dike,"crest_z":zc,"crest_off":sm,"s":ss},open("domain_qb.pkl","wb"))
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"]=["Microsoft YaHei"]
fig,ax=plt.subplots(figsize=(12,10)); x,y=D.exterior.xy; ax.plot(x,y,"k-",lw=.5)
x,y=dom.exterior.xy; ax.fill(x,y,color="lightblue"); ax.plot(x,y,"b-",lw=.8); x,y=S.exterior.xy; ax.plot(x,y,color="orange",lw=2)
x,y=dike.xy; ax.plot(x,y,"r-",lw=1); x,y=L.xy; ax.plot(x,y,"g--",lw=.8)
for k,xy in {"A":(478618,4320778),"B":(492931,4324745),"C":(499491,4334475)}.items(): ax.plot(*xy,"m*",ms=14); ax.annotate(k,xy,fontsize=14)
ax.set_aspect("equal"); ax.set_xlim(476500,501000); ax.set_ylim(4318000,4336000); ax.set_title(f"清北+大清河计算域 {dom.area/1e6:.2f} km²；红=南堤顶线，绿虚=中心线，橙=安全区(剖分)")
fig.savefig("domain_qb.png",dpi=65,bbox_inches="tight")
