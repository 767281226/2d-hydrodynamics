"""Synthetic end-to-end driver for the large-run output profile (no solver).

Feeds synthetic per-face fields through ``NativeUgridNetcdfSink`` exactly as a
solver would, then exports and verifies every deliverable. Output must go
outside the repository, e.g.::

    python -X utf8 examples/large_run_output/run_large_synthetic.py \
        --mesh C:/Users/lenovo/hydro_exp/exp_mesh_graded.npz \
        --output C:/Users/lenovo/hydro_exp/output_test/large_synth_001 --hours 288

The fields are SYNTHETIC_TEST_FIELDS: they test I/O scale only, not hydraulics.
"""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hydrodynamics.frame_sink import NativeUgridNetcdfSink  # noqa: E402
from hydrodynamics.large_run_export import export_large_run_bundle, verify_large_run_bundle  # noqa: E402
from hydrodynamics.large_run_profile import (LargeRunOutputProfile, hourly_times_s, rain_only_forcing,  # noqa: E402
                                             river_assumed_sections_forcing)


GROUPS = [
    {"group_id": "daqinghe_west", "role": "inflow", "name": "大清河 west section (assumed)",
     "section_status": "ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE", "discharge_series_status": "RECONSTRUCTED_NOT_MEASURED"},
    {"group_id": "duliujianhe", "role": "outflow", "name": "独流减河 section (assumed)",
     "section_status": "ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE", "discharge_series_status": "COMPUTED_OUTFLOW"},
    {"group_id": "xihe", "role": "outflow", "name": "西河 section (assumed)",
     "section_status": "ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE", "discharge_series_status": "COMPUTED_OUTFLOW"},
]


def peak_rss_bytes() -> int | None:
    if sys.platform != "win32":
        try:
            import resource
            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
        except Exception:
            return None

    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    counters = PMC(); counters.cb = ctypes.sizeof(PMC)
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
    if not kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return int(counters.PeakWorkingSetSize)


def synthetic_run(mesh_npz: Path, output: Path, hours: int, forcing_mode: str, profile: LargeRunOutputProfile) -> dict:
    data = np.load(mesh_npz, allow_pickle=False)
    xy = np.asarray(data["nodes_xy"], dtype=np.float64); faces = np.asarray(data["faces"], dtype=np.int64)
    crs = str(data["crs"]) if "crs" in data.files else "EPSG:4548"
    centroid = xy[faces].mean(axis=1)
    x = (centroid[:, 0] - centroid[:, 0].min()) / np.ptp(centroid[:, 0]); y = (centroid[:, 1] - centroid[:, 1].min()) / np.ptp(centroid[:, 1])
    bed = 2.0 + 3.0 * x + 1.5 * np.sin(6.0 * y) + 0.5 * np.cos(9.0 * x * y)
    times = hourly_times_s(hours)
    forcing = river_assumed_sections_forcing(GROUPS) if forcing_mode == "river" else rain_only_forcing()
    metadata = {"run_id": f"synthetic-large-{len(faces)}f-{len(times)}t", "case_id": "qingbei_synthetic_large",
                "time_origin": "2023-08-01T02:00:00+08:00", "coordinate_system": crs,
                "vertical_datum": "SYNTHETIC_TEST_DATUM", "dry_depth_threshold_m": 0.001,
                "expected_times_s": list(times), "data_origin": "SYNTHETIC_TEST_FIELDS", "live_solver_ran": False,
                "production_approved": False, "forcing": forcing.model_dump(mode="json"),
                "solver": {"name": "synthetic_field_generator", "note": "no hydraulics; I/O scale test"}}
    output.mkdir(parents=True, exist_ok=False)
    sink = NativeUgridNetcdfSink(output / "result_native.nc", profile)
    sink.open({"node_xy_m": xy, "face_nodes": faces, "face_id": np.arange(1, len(faces) + 1, dtype=np.int64),
               "bed_elevation_m": bed}, metadata)
    area = sink.mesh.face_area_m2
    rng = np.random.default_rng(7)
    noise = rng.normal(0.0, 0.02, len(faces))
    group_inflow, group_out1, group_out2 = [], [], []
    t0 = time.perf_counter()
    for index, t in enumerate(times):
        phase = t / times[-1] if times[-1] else 0.0
        front = 0.15 + 0.8 * phase
        h = np.clip((front - x) * 2.5 + noise, 0.0, None) * (0.6 + 0.4 * np.sin(np.pi * phase))
        h[h < 0.001] = 0.0
        ux = np.where(h > 0, 0.4 + 0.3 * np.sin(5 * y), 0.0); uy = np.where(h > 0, 0.15 * np.cos(4 * x), 0.0)
        q_in = 120.0 + 80.0 * np.sin(np.pi * phase); q_o1 = 0.6 * q_in * phase; q_o2 = 0.25 * q_in * phase
        group_inflow.append(q_in); group_out1.append(q_o1); group_out2.append(q_o2)
        ledger = {"volume_m3": float(np.dot(h, area)), "inflow_m3_s": q_in, "outflow_m3_s": q_o1 + q_o2}
        if forcing_mode == "river":
            ledger["groups"] = {"daqinghe_west": {"q_m3_s": q_in}, "duliujianhe": {"q_m3_s": q_o1}, "xihe": {"q_m3_s": q_o2}}
        else:
            ledger = {"volume_m3": ledger["volume_m3"], "rain_depth_mm": 0.0 if index == 0 else 1.5}
        sink.write_frame(index, t, {"water_depth_m": h, "velocity_x_m_s": ux, "velocity_y_m_s": uy}, ledger=ledger)
    sink_s = time.perf_counter() - t0
    dt = np.diff(np.asarray(times))
    vol = lambda q: float(np.sum(0.5 * (np.asarray(q[1:]) + np.asarray(q[:-1])) * dt))
    close_ledger = {"initial_volume_m3": 0.0,
                    "groups": {"daqinghe_west": {"role": "inflow", "volume_m3": vol(group_inflow)},
                               "duliujianhe": {"role": "outflow", "volume_m3": vol(group_out1), "requested_volume_m3": vol(group_out1) * 1.05,
                                               "outflow_shortfall_m3": vol(group_out1) * 0.05},
                               "xihe": {"role": "outflow", "volume_m3": vol(group_out2), "requested_volume_m3": vol(group_out2),
                                        "outflow_shortfall_m3": 0.0}},
                    "outflow_shortfall_m3": vol(group_out1) * 0.05} if forcing_mode == "river" else {"initial_volume_m3": 0.0}
    report = sink.close({"max_water_depth_m": sink._max_h + 0.001}, close_ledger)
    return {"sink_report": report, "sink_seconds": sink_s}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mesh", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--hours", type=int, default=288)
    parser.add_argument("--forcing", choices=("river", "rain"), default="river")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    repo = Path(__file__).resolve().parents[2]
    if output.is_relative_to(repo):
        raise SystemExit("write synthetic outputs outside the repository")
    profile = LargeRunOutputProfile()
    started = time.perf_counter()
    result = synthetic_run(Path(args.mesh), output, args.hours, args.forcing, profile)
    t1 = time.perf_counter()
    export = export_large_run_bundle(output, profile, progress=lambda m: print(m, flush=True))
    t2 = time.perf_counter()
    verify = verify_large_run_bundle(output)
    t3 = time.perf_counter()
    sizes = {}
    for path in output.iterdir():
        sizes[path.name] = path.stat().st_size if path.is_file() else sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    summary = {"sink": result, "export": export, "verify": verify, "seconds": {"sink": t1 - started, "export": t2 - t1, "verify": t3 - t2,
               "total": t3 - started}, "peak_working_set_bytes": peak_rss_bytes(), "sizes_bytes": sizes}
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
