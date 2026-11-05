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
import sys, pickle, json, numpy as np
from shapely.geometry import Polygon, LineString
from hydrodynamics.boundary_simplification import simplify_domain_and_security
d=pickle.load(open("domain_qb_ext.pkl","rb")); dom=d["domain"]; S=pickle.load(open("parts.pkl","rb"))["S"]
dom=Polygon([(round(x,3),round(y,3)) for x,y in dom.exterior.coords]).buffer(0)
res=simplify_domain_and_security(dom, S, minimum_vertex_spacing_m=25.0, geometry_tolerance_m=1e-8, area_tolerance_m2=1e-7)
sd=res.domain if hasattr(res,"domain") else None
print([k for k in dir(res) if not k.startswith("_")])
D2=res.domain_geometry; S2=res.security_geometry
print("simplified D vertices", len(D2.exterior.coords), "area km2", round(D2.area/1e6,4), "hausdorff", round(D2.exterior.hausdorff_distance(dom.exterior),2), "S verts", len(S2.exterior.coords))
def densify_open(ring_coords, step):
    import math
    line=LineString(ring_coords); n=math.ceil(line.length/step); st=line.length/n; pts=[line.interpolate(i*st).coords[0] for i in range(n)]
    # drop the closing segment: last point is ~step before the start -> leave one-segment gap
    return [(round(x,3),round(y,3)) for x,y in pts[:-1]]
inner=densify_open(list(S2.exterior.coords), 25.0)
outer_ring=S2.buffer(30.0, join_style=2).exterior
assert D2.contains(outer_ring.buffer(5)), "dike toe ring leaves domain"
outer=densify_open(list(outer_ring.coords), 25.0)
geom={"outer_boundary":[[round(x,3),round(y,3)] for x,y in list(D2.exterior.coords)[:-1]],
      "constraints":[{"name":"security_ring","points":inner},{"name":"security_dike_toe_30m","points":outer}],
      "S_polygon":[list(c) for c in S2.exterior.coords], "audit":res.audit}
json.dump(geom,open("qb_geom_ext.json","w"),default=str)
print("constraint pts", len(inner), len(outer), "min dist dike-toe ring to boundary", round(outer_ring.distance(D2.exterior),1))
