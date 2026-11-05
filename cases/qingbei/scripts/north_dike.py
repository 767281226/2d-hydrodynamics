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
from shapely.geometry import LineString
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"]=["Microsoft YaHei"]
L=pickle.load(open("centerline.pkl","rb")); S=pickle.load(open("parts.pkl","rb"))["S"]
ss=np.arange(0,L.length,25.0); off=np.arange(0,401,1.0); P=[]
with rasterio.open(str(REPO / 'data/dem/DEMn_1m.tif')) as r:
    for s in ss:
        p=np.array(L.interpolate(s).coords[0]); q=np.array(L.interpolate(min(s+5,L.length)).coords[0]); o=np.array(L.interpolate(max(s-5,0)).coords[0])
        t=(q-o)/np.linalg.norm(q-o); n=np.array([-t[1],t[0]])
        z=np.array([v[0] for v in r.sample([tuple(a) for a in p+off[:,None]*n])],float); z[z<-1000]=np.nan; P.append(z)
    # S ring profile: crest along ring = max within +-15 m normal
    ring=S.exterior; sr=np.arange(0,ring.length,20.0); zr=[]
    for s in sr:
        p=np.array(ring.interpolate(s).coords[0]); q=np.array(ring.interpolate((s+5)%ring.length).coords[0]); t=(q-p)/np.linalg.norm(q-p); n=np.array([-t[1],t[0]])
        z=np.array([v[0] for v in r.sample([tuple(p+k*n) for k in range(-40,41)])],float); zr.append(z)
P=np.array(P); zr=np.array(zr)
np.savez("north_raw.npz",s=ss,prof=P,sr=sr,zr=zr)
fig,ax=plt.subplots(2,1,figsize=(22,10))
im=ax[0].imshow(P.T,origin="lower",aspect="auto",extent=(0,ss[-1],0,400),cmap="terrain",vmin=-1,vmax=8); ax[0].set_title("大清河中心线向北偏移剖面")
ax[1].imshow(zr.T,origin="lower",aspect="auto",extent=(0,sr[-1],-40,40),cmap="terrain",vmin=-1,vmax=8); ax[1].set_title("安全区边界两侧±40m（正=外侧/左）")
fig.savefig("north_and_S.png",dpi=55,bbox_inches="tight")
