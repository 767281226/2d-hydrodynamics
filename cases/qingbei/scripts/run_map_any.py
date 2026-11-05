"""Exact project mapper (M2-A windowed) + B0 CRS alias wrapper on the full mesh005 (read-only DEM use)."""
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
import os, sys, time, json, hashlib
os.environ["GDAL_PAM_ENABLED"] = "NO"
import numpy as np
from hydrodynamics.dem_contract import GeoTIFFDEMReader
from hydrodynamics.triangular_mesh import TriangularMesh
from hydrodynamics.terrain_windowed_mapping import (WindowedTriangleMappingConfig, map_triangular_mesh_from_windows,
    WindowedTerrainMappingError, write_windowed_mapping_report)
from hydrodynamics.restricted_worker import _CRSAliasReader

DEM = str(REPO / 'data/dem/DEMn_1m.tif')
OUT = sys.argv[2]
t0 = time.perf_counter()
m = np.load(sys.argv[1], allow_pickle=False)
xy = m["nodes_xy"]; faces = m["faces"]; fid = m["face_id"]
mesh = TriangularMesh(node_xy=xy.tolist(), face_nodes=faces.tolist(), face_id=fid.tolist(),
                      coordinate_system=str(m["crs"]), mesh_id=str(m["mesh_id"]), geometry_tolerance_m=1e-8)
t_mesh = time.perf_counter() - t0
print("mesh built", t_mesh, flush=True)
reader = GeoTIFFDEMReader()
wkt = reader.read_metadata(DEM).crs
alias = _CRSAliasReader(reader, {"expected_source_wkt_sha256": hashlib.sha256(wkt.encode()).hexdigest(), "target_crs": "EPSG:4548"})
dem_sha = open(str(CASE / 'inputs' / 'dem_sha256.txt')).read().strip()
cfg = WindowedTriangleMappingConfig(max_source_pixels=1_000_000_000, max_candidate_overlap_checks=1_000_000_000,
                                    coverage_policy="diagnostic_only")
t1 = time.perf_counter(); status = "ok"
try:
    terrain, report = map_triangular_mesh_from_windows(alias, DEM, mesh, source_sha256=dem_sha, config=cfg)
except WindowedTerrainMappingError as exc:
    status = f"failed: {str(exc)[:400]}"; report = exc.diagnostics; terrain = None
t_map = time.perf_counter() - t1
print("mapped", status, t_map, flush=True)
rows = report.get("faces", [])
summary = {"status": status, "n_face": mesh.n_face, "t_mesh_s": t_mesh, "t_map_s": t_map, "stats": report.get("stats"),
           "alias_evidence_status": (alias.evidence or {}).get("status"), "dem_sha256": dem_sha,
           "vertical_unit": report.get("vertical_unit"), "vertical_datum": report.get("vertical_datum"),
           "n_failures": len(report.get("failures") or []), "failures_head": (report.get("failures") or [])[:5]}
if rows:
    z = np.array([r["elevation_m"] if r["elevation_m"] is not None else np.nan for r in rows])
    cov = np.array([r["coverage_ratio"] for r in rows]); area = np.array([r["face_area_m2"] for r in rows])
    nod = np.array([r["nodata_area_m2"] + r["nonfinite_area_m2"] + r["masked_other_area_m2"] for r in rows])
    summary.update({"min_cov": float(cov.min()), "faces_any_nodata": int((nod > 0).sum()), "faces_zero_valid": int(np.isnan(z).sum()),
                    "z_min": float(np.nanmin(z)), "z_max": float(np.nanmax(z)), "z_area_weighted_mean": float(np.nansum(z*area)/area[~np.isnan(z)].sum())})
    np.savez_compressed(OUT + "_faces.npz", face_id=np.array([r["face_id"] for r in rows]), elevation_m=z, coverage=cov, nodata_area_m2=nod, face_area_m2=area)
    report_small = dict(report); report_small.pop("faces", None)
    json.dump(report_small, open(OUT + "_report_nofaces.json", "w"), indent=1, default=str)
    try:
        write_windowed_mapping_report(OUT + "_report_full.json", report)
    except Exception as exc:
        summary["report_full_write_error"] = str(exc)[:300]
if terrain is not None:
    try:
        import pickle; pickle.dump(terrain, open(OUT + "_terrain.pkl", "wb"))
    except Exception as exc:
        summary["terrain_pickle_error"] = str(exc)[:300]
json.dump(summary, open(OUT + "_summary.json", "w"), indent=1, default=str)
print(json.dumps(summary, indent=1, default=str))
