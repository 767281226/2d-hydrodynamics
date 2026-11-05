"""Build forcing CSV + large-run-driver config for a design-flood (return period) scenario.

Usage:
  python -X utf8 make_design_case.py --rp 20 --hydrograph design_Q_P20.csv --at xingaifang \
      --hours 288 --xihe-open-h 255 --xihe-q 125

hydrograph CSV: columns time_h,Q_m3s (design hydrograph from an APPROVED design result; this
script never invents a curve). --at xingaifang applies the 23-7 calibrated transfer
(delay 80 h, x0.413, + base 200 m3/s) to get the inflow at section A; --at A uses Q as-is.
"""
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
import argparse, csv, json, sys
from pathlib import Path
import numpy as np

HERE = CASE / "design"
BASE_CONFIG = CASE / "configs" / "real_23_7_288h.json"

ap = argparse.ArgumentParser()
ap.add_argument("--rp", type=int, required=True, help="return period, years")
ap.add_argument("--hydrograph", required=True, help="CSV time_h,Q_m3s (approved design hydrograph)")
ap.add_argument("--at", choices=["xingaifang", "A"], required=True, help="where the hydrograph is defined")
ap.add_argument("--delay-h", type=float, default=80.0)
ap.add_argument("--k", type=float, default=0.4129234046651985)
ap.add_argument("--base", type=float, default=200.0, help="m3/s base flow at A")
ap.add_argument("--hours", type=int, default=288)
ap.add_argument("--xihe-open-h", type=float, default=None, help="hour 西河闸 starts diverting (null = closed all run)")
ap.add_argument("--xihe-q", type=float, default=125.0, help="m3/s prescribed 西河闸 outflow once open")
ap.add_argument("--manning", type=float, default=0.035)
ap.add_argument("--time-origin", default="2000-01-01T00:00:00+08:00", help="nominal; design events have no calendar date")
ap.add_argument("--source-ref", default="", help="design dataset / approval reference (recorded in description)")
a = ap.parse_args()

rows = list(csv.DictReader(open(a.hydrograph, encoding="utf-8-sig")))
th = np.array([float(r["time_h"]) for r in rows]); q = np.array([float(r["Q_m3s"]) for r in rows])
if np.any(np.diff(th) <= 0) or np.any(q < 0) or not np.all(np.isfinite(q)):
    sys.exit("hydrograph: time_h must increase strictly and Q_m3s must be finite >= 0")

t = np.arange(a.hours + 1, dtype=float)
if a.at == "xingaifang":
    qa = a.base + a.k * np.interp(t - a.delay_h, th, q, left=0.0, right=q[-1])
else:
    qa = np.interp(t, th, q, left=q[0], right=q[-1])
xihe = np.zeros_like(t) if a.xihe_open_h is None else np.where(t >= a.xihe_open_h, a.xihe_q, 0.0)

tag = f"P{a.rp}"
fcsv = HERE / f"forcing_design_{tag}.csv"
with open(fcsv, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh); w.writerow(["time_s", "inflow_A_m3s", "duliu_reference_m3s", "xihe_controlled_m3s"])
    for ti, qi, xi in zip(t, qa, xihe):
        w.writerow([ti * 3600.0, round(float(qi), 4), 0.0, xi])

cfg = json.load(open(BASE_CONFIG, encoding="utf-8"))
cfg["description"] = (f"DESIGN {a.rp}-year flood, {a.hours} h, domain 清北+大清河 channel (62.96 km², S walled); "
    f"A inflow from design hydrograph at {a.at}" + (f" (delay {a.delay_h} h, x{a.k:.4f}, base {a.base})" if a.at == "xingaifang" else "")
    + f"; B 独流减河进洪闸 free outflow; C 西河闸 {'closed' if a.xihe_open_h is None else f'{a.xihe_q} m3/s from t={a.xihe_open_h} h'}; "
    f"no rainfall; DEM datum taken as 1985. Design source: {a.source_ref or 'UNSPECIFIED'}")
cfg["sections"]["discharge_series_status"]["A"] = "RECONSTRUCTED_NOT_MEASURED"  # driver v1 has no DESIGN status; description says design
cfg["forcing"]["path"] = f"../design/{fcsv.name}"  # configs/-relative
r = cfg["run"]; r["hours"] = a.hours; r["manning_n"] = a.manning; r["time_origin"] = a.time_origin
r["case_id"] = f"qingbei_design_{tag}"
cfg["output"]["script_id"] = f"qingbei_design_{tag}_{a.hours}h"
out = CASE / "configs" / f"design_{tag}_config.json"
json.dump(cfg, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"{fcsv}\n{out}\nA peak {qa.max():.1f} m3/s at t={int(t[qa.argmax()])} h, A volume {np.trapezoid(qa, t*3600)/1e8:.3f} 亿m3")
