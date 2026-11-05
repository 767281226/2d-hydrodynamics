"""Watchdog for a large-run driver run directory (``hydrodynamics.large_run_driver``).

It polls ``<run dir>/progress.jsonl`` and ``run_report.json`` and raises an alert when the
solver is stuck:

* ``dt_collapse``   median ``dt_s`` of the last ``dt_window`` solver_progress events is below ``min_dt_s``;
* ``slow_progress`` simulated seconds per wall-clock second over the last ``speedup_window_min``
  minutes are below ``min_speedup``;
* ``stale``         no progress line for ``stale_min`` minutes while the solver runs
                    (``stale_after_solver_min`` once the solver has finished: close/export/verify);
* ``process_died``  run_report.json says running but the driver PID from the last ``start`` event is gone.

On an alert it writes ``WATCHDOG_ALERT.json`` and appends to ``watchdog.jsonl`` in the run directory.
With ``--action stop`` (default) it also terminates the driver process tree and marks
``run_report.json`` ``status: failed`` with ``failed_stage: watchdog``.  Checkpoints are never
touched, so the run can be continued with ``large_run_driver <config> --resume`` after the cause is
fixed.  Checks begin ``grace_min`` minutes after the last ``start`` event.

CLI::

    python -m hydrodynamics.run_watchdog <run dir> [--action stop|alert] [--once] [thresholds]

Field table and defaults: ``docs/run_watchdog.md``.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any

ALERT_FORMAT = "large-run-watchdog-alert-v1"
LOG_NAME = "watchdog.jsonl"
ALERT_NAME = "WATCHDOG_ALERT.json"
SOLVER_FINISHED_EVENTS = {"solver_done", "sink_closed", "export", "export_done", "verify_done", "native_result_already_complete"}
TERMINAL_STATUSES = {"complete", "failed"}


class WatchdogError(ValueError):
    """Invalid watchdog settings or run directory."""


@dataclass(frozen=True)
class WatchdogSettings:
    min_dt_s: float = 0.005            # s, (0, 60]
    dt_window: int = 5                 # solver_progress events, [1, 1000]
    min_speedup: float = 2.0           # simulated s per wall s, (0, 1e6]; 0 disables
    speedup_window_min: float = 30.0   # min, (0, 1440]
    stale_min: float = 30.0            # min, (0, 1440]
    stale_after_solver_min: float = 240.0  # min, (0, 10080]; export/verify phase
    grace_min: float = 10.0            # min, [0, 1440]
    poll_s: float = 60.0               # s, [1, 3600]
    action: str = "stop"               # stop | alert

    def __post_init__(self) -> None:
        def check(name: str, value: float, low: float, high: float, low_open: bool = True) -> None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise WatchdogError(f"{name} must be a finite number")
            if (value <= low if low_open else value < low) or value > high:
                raise WatchdogError(f"{name}={value} is outside {'(' if low_open else '['}{low}, {high}]")
        check("min_dt_s", self.min_dt_s, 0.0, 60.0)
        if isinstance(self.dt_window, bool) or not isinstance(self.dt_window, int) or not 1 <= self.dt_window <= 1000:
            raise WatchdogError("dt_window must be an integer in [1, 1000]")
        check("min_speedup", self.min_speedup, 0.0, 1e6, low_open=False)
        check("speedup_window_min", self.speedup_window_min, 0.0, 1440.0)
        check("stale_min", self.stale_min, 0.0, 1440.0)
        check("stale_after_solver_min", self.stale_after_solver_min, 0.0, 10080.0)
        check("grace_min", self.grace_min, 0.0, 1440.0, low_open=False)
        check("poll_s", self.poll_s, 1.0, 3600.0, low_open=False)
        if self.action not in ("stop", "alert"):
            raise WatchdogError("action must be 'stop' or 'alert'")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    """All parseable progress.jsonl objects since the most recent ``start`` event (a partial last line is ignored)."""
    path = run_dir / "progress.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            if item.get("event") == "start":
                events = []
            events.append(item)
    return events


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True)
        return f'"{pid}"'.encode("ascii") in out.stdout  # console code page output; the PID is ASCII
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate(pid: int) -> str:
    if sys.platform == "win32":
        out = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        text = (out.stdout + out.stderr).decode("mbcs" if hasattr(__import__("codecs"), "mbcs_decode") else "utf-8", errors="replace").strip()
        return text or f"taskkill exit {out.returncode}"
    import signal
    os.kill(pid, signal.SIGTERM)
    return "SIGTERM sent"


def evaluate(run_dir: Path, settings: WatchdogSettings, *, now: datetime | None = None) -> dict[str, Any]:
    """One check.  Returns ``{"state": ok|alert|finished|waiting, "reason": ..., "metrics": {...}}``."""
    now = now or _utc_now()
    report_path = run_dir / "run_report.json"
    status = None
    if report_path.is_file():
        try:
            status = json.loads(report_path.read_text(encoding="utf-8")).get("status")
        except ValueError:
            status = None
    events = read_events(run_dir)
    metrics: dict[str, Any] = {"run_report_status": status, "events_since_start": len(events)}
    if status in TERMINAL_STATUSES:
        return {"state": "finished", "reason": f"run_report status {status}", "metrics": metrics}
    if not events or events[0].get("event") != "start":
        return {"state": "waiting", "reason": "no start event yet", "metrics": metrics}
    start = events[0]
    pid = int(start.get("pid") or 0)
    started = _parse_utc(start.get("utc"))
    last = _parse_utc(events[-1].get("utc"))
    metrics.update(pid=pid, started_utc=start.get("utc"), last_event=events[-1].get("event"), last_event_utc=events[-1].get("utc"))
    solver = [e for e in events if e.get("event") == "solver_progress"]
    solver_finished = any(e.get("event") in SOLVER_FINISHED_EVENTS for e in events)
    if solver:
        metrics.update(time_s=solver[-1].get("time_s"), dt_s=solver[-1].get("dt_s"), accepted_step_count=solver[-1].get("accepted_step_count"))
    if pid and not _pid_alive(pid):
        return {"state": "alert", "reason": "process_died",
                "detail": f"driver PID {pid} is gone but run_report.json status is {status!r}", "metrics": metrics}
    if started is not None and (now - started).total_seconds() < settings.grace_min * 60.0:
        return {"state": "ok", "reason": "grace period", "metrics": metrics}
    idle_min = (now - last).total_seconds() / 60.0 if last else 0.0
    metrics["idle_min"] = round(idle_min, 2)
    stale_limit = settings.stale_after_solver_min if solver_finished else settings.stale_min
    if idle_min > stale_limit:
        phase = "after the solver (close/export/verify)" if solver_finished else "while the solver runs"
        return {"state": "alert", "reason": "stale", "detail": f"no progress line for {idle_min:.1f} min {phase} (limit {stale_limit} min)", "metrics": metrics}
    if solver_finished or not solver:
        return {"state": "ok", "reason": "solver finished" if solver_finished else "no solver progress yet", "metrics": metrics}
    recent = [float(e["dt_s"]) for e in solver[-settings.dt_window:] if isinstance(e.get("dt_s"), (int, float))]
    if len(recent) >= settings.dt_window:
        median_dt = statistics.median(recent)
        metrics["median_dt_s"] = median_dt
        if median_dt < settings.min_dt_s:
            return {"state": "alert", "reason": "dt_collapse",
                    "detail": f"median dt of the last {settings.dt_window} progress events is {median_dt:.3g} s < {settings.min_dt_s} s", "metrics": metrics}
    if settings.min_speedup > 0.0:
        window_start = now.timestamp() - settings.speedup_window_min * 60.0
        inside = [e for e in solver if (_parse_utc(e.get("utc")) or now).timestamp() >= window_start]
        before = [e for e in solver if (_parse_utc(e.get("utc")) or now).timestamp() < window_start]
        if before and inside:  # the window is fully covered by solver progress
            first, final = before[-1], inside[-1]
            wall = (_parse_utc(final["utc"]) - _parse_utc(first["utc"])).total_seconds()
            if wall > 0:
                speedup = (float(final["time_s"]) - float(first["time_s"])) / wall
                metrics["speedup"] = speedup
                if speedup < settings.min_speedup:
                    return {"state": "alert", "reason": "slow_progress",
                            "detail": f"simulated/wall speed {speedup:.3g} over {wall / 60:.1f} min < {settings.min_speedup}", "metrics": metrics}
    return {"state": "ok", "reason": "healthy", "metrics": metrics}


def _append(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def handle_alert(run_dir: Path, result: dict[str, Any], settings: WatchdogSettings, *, config_hint: str | None = None) -> dict[str, Any]:
    """Write the alert file; with action=stop terminate the driver and mark run_report.json failed."""
    pid = int(result["metrics"].get("pid") or 0)
    terminated = None
    if settings.action == "stop" and pid and result["reason"] != "process_died" and _pid_alive(pid):
        terminated = _terminate(pid)
    alert = {"format": ALERT_FORMAT, "utc": _utc_now().isoformat(timespec="seconds"), "run_dir": str(run_dir),
             "reason": result["reason"], "detail": result.get("detail"), "metrics": result["metrics"],
             "action": settings.action, "terminated": terminated, "settings": asdict(settings),
             "checkpoints_kept": sorted(p.name for p in (run_dir / "checkpoints").glob("checkpoint_*.npz")) if (run_dir / "checkpoints").is_dir() else [],
             "next_step": ("fix the cause, then: python -X utf8 -m hydrodynamics.large_run_driver "
                           f"{config_hint or '<config.json>'} --resume (add --id <scheme id> if the id was generated)")}
    _write_json_atomic(run_dir / ALERT_NAME, alert)
    report_path = run_dir / "run_report.json"
    if settings.action == "stop" and report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except ValueError:
            report = None
        if isinstance(report, dict) and report.get("status") not in TERMINAL_STATUSES:
            report.update(status="failed", failed_stage="watchdog",
                          error=f"watchdog {result['reason']}: {result.get('detail')}", finished_utc=alert["utc"])
            _write_json_atomic(report_path, report)
    return alert


def watch(run_dir: str | Path, settings: WatchdogSettings, *, once: bool = False, log: Any = None) -> dict[str, Any]:
    root = Path(run_dir)
    if not root.is_dir():
        raise WatchdogError(f"run directory does not exist: {root}")
    say = log or (lambda message: None)
    config_hint = None
    while True:
        result = evaluate(root, settings)
        events = read_events(root)
        if events and events[0].get("event") == "start":
            config_hint = events[0].get("config") or config_hint
        _append(root / LOG_NAME, {"utc": _utc_now().isoformat(timespec="seconds"), **result})
        say(json.dumps(result, ensure_ascii=False, default=str))
        if result["state"] == "alert":
            alert = handle_alert(root, result, settings, config_hint=config_hint)
            return {"state": "alert", "alert": alert}
        if result["state"] == "finished" or once:
            return result
        time.sleep(settings.poll_s)


def main(argv: list[str] | None = None) -> int:
    defaults = WatchdogSettings()
    parser = argparse.ArgumentParser(prog="python -m hydrodynamics.run_watchdog",
                                     description="Stop (or flag) a large-run driver run whose solver has stalled.")
    parser.add_argument("run_dir", help="large-run driver run directory (contains progress.jsonl and run_report.json)")
    parser.add_argument("--action", choices=("stop", "alert"), default=defaults.action, help="stop = terminate the driver and mark the run failed (default); alert = only write the alert")
    parser.add_argument("--min-dt-s", type=float, default=defaults.min_dt_s, help="s; alert if the median recent dt is below this (default %(default)s)")
    parser.add_argument("--dt-window", type=int, default=defaults.dt_window, help="number of recent solver_progress events for the dt median (default %(default)s)")
    parser.add_argument("--min-speedup", type=float, default=defaults.min_speedup, help="simulated s per wall s; alert if slower; 0 disables (default %(default)s)")
    parser.add_argument("--speedup-window-min", type=float, default=defaults.speedup_window_min, help="min; window for the speed check (default %(default)s)")
    parser.add_argument("--stale-min", type=float, default=defaults.stale_min, help="min; alert if no progress line while the solver runs (default %(default)s)")
    parser.add_argument("--stale-after-solver-min", type=float, default=defaults.stale_after_solver_min, help="min; same during close/export/verify (default %(default)s)")
    parser.add_argument("--grace-min", type=float, default=defaults.grace_min, help="min after the start event before checks begin (default %(default)s)")
    parser.add_argument("--poll-s", type=float, default=defaults.poll_s, help="s between checks (default %(default)s)")
    parser.add_argument("--once", action="store_true", help="check once and exit (0 ok/finished/waiting, 2 alert)")
    args = parser.parse_args(argv)
    try:
        settings = WatchdogSettings(min_dt_s=args.min_dt_s, dt_window=args.dt_window, min_speedup=args.min_speedup,
                                    speedup_window_min=args.speedup_window_min, stale_min=args.stale_min,
                                    stale_after_solver_min=args.stale_after_solver_min, grace_min=args.grace_min,
                                    poll_s=args.poll_s, action=args.action)
        result = watch(args.run_dir, settings, once=args.once, log=lambda m: print(m, flush=True))
    except WatchdogError as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 2 if result["state"] == "alert" else 0


__all__ = ["WatchdogError", "WatchdogSettings", "evaluate", "handle_alert", "main", "read_events", "watch"]

if __name__ == "__main__":
    raise SystemExit(main())
