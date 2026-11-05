"""Design hydrographs at 新盖房 (P10/P20/P50/P100) by typical-flood peak+volume dual-control scaling.

Typical flood: 23-7 新盖房 series (xingaifang_m3s in ../qb/forcing_qb_23_7.csv, peak 2790 m3/s).
Anchors (see README.md): P100 peak 5500 m3/s (official), passed 6-day volume 17.84-3.17 = 14.67e8 m3.
P50/P20 from 白沟河 frequency ratios 3920/4200, 2690/4200 (secondary source, unverified);
P10 log-linear extrapolation in ln(T) between P20 and P50.
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
import csv, math, json
from pathlib import Path
import numpy as np
HERE = CASE / "design"
rows = list(csv.DictReader(open(CASE / "inputs" / "forcing_qb_23_7.csv")))
t = np.array([float(r["time_s"]) for r in rows]) / 3600.0
q = np.array([float(r["xingaifang_m3s"]) for r in rows])
tp = t[q.argmax()]

def vol6(x):  # max 6-day volume, 1e8 m3
    return max(np.trapezoid(x[(t >= s) & (t <= s + 144)], t[(t >= s) & (t <= s + 144)] * 3600) for s in range(0, int(t[-1]) - 143)) / 1e8

r20, r50 = 2690 / 4200, 3920 / 4200
r10 = r20 / (r50 / r20) ** (math.log(2) / math.log(2.5))
ratio = {10: r10, 20: r20, 50: r50, 100: 1.0}
P100_PEAK, P100_V6 = 5500.0, 17.84 - 3.17
w = np.exp(-0.5 * ((t - tp) / 24.0) ** 2)
v6_typ, summary = vol6(q), {}
for rp, k in ratio.items():
    peak, v6 = P100_PEAK * k, P100_V6 * k
    rp_ = peak / q.max(); rv = v6 / v6_typ
    for _ in range(50):
        qd = q * (rv + (rp_ - rv) * w)
        rv *= v6 / vol6(qd)
    qd = q * (rv + (rp_ - rv) * w)
    with open(HERE / f"design_Q_P{rp}.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh); wr.writerow(["time_h", "Q_m3s"])
        for ti, qi in zip(t, qd): wr.writerow([ti, round(float(qi), 2)])
    summary[f"P{rp}"] = {"peak_m3s": round(float(qd.max()), 1), "peak_ratio_to_P100": round(k, 4),
                         "vol6d_1e8m3": round(vol6(qd), 3), "vol288h_1e8m3": round(np.trapezoid(qd, t * 3600) / 1e8, 3)}
summary["typical_23_7"] = {"peak_m3s": float(q.max()), "vol6d_1e8m3": round(v6_typ, 3)}
json.dump(summary, open(HERE / "design_hydrographs_summary.json", "w", encoding="utf-8"), indent=1)
print(json.dumps(summary, indent=1))
