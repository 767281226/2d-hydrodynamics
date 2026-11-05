"""Watchdog for large-run driver run directories."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from hydrodynamics.run_watchdog import WatchdogError, WatchdogSettings, evaluate, handle_alert, main, read_events, watch

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def _iso(minutes_before: float) -> str:
    return (NOW - timedelta(minutes=minutes_before)).isoformat(timespec="seconds")


def _run_dir(tmp_path: Path, events: list[dict], status: str = "running") -> Path:
    run = tmp_path / "run"
    (run / "checkpoints").mkdir(parents=True)
    (run / "checkpoints" / "checkpoint_00000018.npz").write_bytes(b"x")
    (run / "run_report.json").write_text(json.dumps({"status": status, "run_id": "r"}), encoding="utf-8")
    (run / "progress.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return run


def _start(pid: int, minutes_before: float = 120.0) -> dict:
    return {"event": "start", "utc": _iso(minutes_before), "pid": pid, "config": "C:/cfg.json"}


def _progress(minutes_before: float, time_s: float, dt_s: float) -> dict:
    return {"event": "solver_progress", "utc": _iso(minutes_before), "time_s": time_s, "dt_s": dt_s, "accepted_step_count": 1}


def _healthy(pid: int) -> list[dict]:
    # 60 progress events, one per minute, 1350 s simulated per minute (22.5x), dt 0.135 s
    return [_start(pid)] + [_progress(60 - k, 66000.0 + 1350.0 * k, 0.135) for k in range(60)]


LIVE = os.getpid()


def test_healthy_run_is_ok(tmp_path):
    run = _run_dir(tmp_path, _healthy(LIVE))
    result = evaluate(run, WatchdogSettings(), now=NOW)
    assert result["state"] == "ok" and result["reason"] == "healthy"
    assert result["metrics"]["median_dt_s"] == pytest.approx(0.135) and result["metrics"]["speedup"] == pytest.approx(22.5)


def test_dt_collapse_like_the_23_7_outlet_stall(tmp_path):
    events = _healthy(LIVE)[:-5] + [_progress(5 - k, 66025.0 + 0.3 * k, 2.6e-4) for k in range(5)]
    result = evaluate(_run_dir(tmp_path, events), WatchdogSettings(), now=NOW)
    assert result["state"] == "alert" and result["reason"] == "dt_collapse"


def test_slow_progress(tmp_path):
    events = [_start(LIVE)] + [_progress(60 - k, 66000.0 + 30.0 * k, 0.05) for k in range(60)]  # 0.5x real time
    result = evaluate(_run_dir(tmp_path, events), WatchdogSettings(min_dt_s=0.01), now=NOW)
    assert result["state"] == "alert" and result["reason"] == "slow_progress"
    assert evaluate(_run_dir(tmp_path / "b", events), WatchdogSettings(min_dt_s=0.01, min_speedup=0.0), now=NOW)["state"] == "ok"


def test_stale_while_solving_and_longer_limit_after_solver(tmp_path):
    events = [_start(LIVE)] + [_progress(100 - k, 1000.0 * k, 0.1) for k in range(40)]  # last line 61 min ago
    assert evaluate(_run_dir(tmp_path, events), WatchdogSettings(), now=NOW)["reason"] == "stale"
    exporting = events + [{"event": "solver_done", "utc": _iso(61)}, {"event": "export", "utc": _iso(60), "message": "nc"}]
    assert evaluate(_run_dir(tmp_path / "b", exporting), WatchdogSettings(), now=NOW)["state"] == "ok"
    assert evaluate(_run_dir(tmp_path / "c", exporting), WatchdogSettings(stale_after_solver_min=30.0), now=NOW)["reason"] == "stale"


def test_grace_period_and_finished_and_waiting(tmp_path):
    early = [_start(LIVE, minutes_before=3)] + [_progress(2, 10.0, 1e-5)]
    assert evaluate(_run_dir(tmp_path, early), WatchdogSettings(), now=NOW)["reason"] == "grace period"
    assert evaluate(_run_dir(tmp_path / "b", _healthy(LIVE), status="complete"), WatchdogSettings(), now=NOW)["state"] == "finished"
    assert evaluate(_run_dir(tmp_path / "c", []), WatchdogSettings(), now=NOW)["state"] == "waiting"


def test_process_died(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    result = evaluate(_run_dir(tmp_path, _healthy(dead.pid)), WatchdogSettings(), now=NOW)
    assert result["state"] == "alert" and result["reason"] == "process_died"


def test_read_events_restarts_at_the_last_start_and_skips_partial_lines(tmp_path):
    run = _run_dir(tmp_path, _healthy(1) + [_start(LIVE, 5), _progress(1, 5.0, 0.1)])
    with (run / "progress.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"event": "solver_prog')
    events = read_events(run)
    assert [e["event"] for e in events] == ["start", "solver_progress"] and events[0]["pid"] == LIVE


def test_stop_action_terminates_driver_and_marks_report_failed(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        events = [dict(_start(child.pid), utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat())]
        now = datetime.now(timezone.utc)
        events[0]["utc"] = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
        events += [{"event": "solver_progress", "utc": (now - timedelta(minutes=5 - k)).isoformat(timespec="seconds"),
                    "time_s": 66025.0 + k * 0.01, "dt_s": 2.6e-4, "accepted_step_count": k} for k in range(5)]
        run = _run_dir(tmp_path, events)
        result = watch(run, WatchdogSettings(poll_s=1.0), once=True)
        assert result["state"] == "alert" and result["alert"]["reason"] == "dt_collapse" and result["alert"]["terminated"]
        child.wait(timeout=30)
        report = json.loads((run / "run_report.json").read_text(encoding="utf-8"))
        assert report["status"] == "failed" and report["failed_stage"] == "watchdog" and "dt_collapse" in report["error"]
        alert = json.loads((run / "WATCHDOG_ALERT.json").read_text(encoding="utf-8"))
        assert alert["checkpoints_kept"] == ["checkpoint_00000018.npz"] and "--resume" in alert["next_step"] and "C:/cfg.json" in alert["next_step"]
        assert (run / "checkpoints" / "checkpoint_00000018.npz").is_file()
        assert (run / "watchdog.jsonl").read_text(encoding="utf-8").strip()
    finally:
        if child.poll() is None:
            child.kill()


def test_alert_action_leaves_process_and_report(tmp_path):
    events = _healthy(LIVE)[:-5] + [_progress(5 - k, 66025.0, 2.6e-4) for k in range(5)]
    run = _run_dir(tmp_path, events)
    result = evaluate(run, WatchdogSettings(action="alert"), now=NOW)
    alert = handle_alert(run, result, WatchdogSettings(action="alert"))
    assert alert["terminated"] is None
    assert json.loads((run / "run_report.json").read_text(encoding="utf-8"))["status"] == "running"


@pytest.mark.parametrize("kwargs, message", [
    ({"min_dt_s": 0.0}, "min_dt_s"), ({"min_dt_s": float("nan")}, "min_dt_s"), ({"dt_window": 0}, "dt_window"),
    ({"min_speedup": -1.0}, "min_speedup"), ({"stale_min": 0.0}, "stale_min"), ({"poll_s": 0.5}, "poll_s"),
    ({"grace_min": -1.0}, "grace_min"), ({"action": "kill"}, "action"), ({"dt_window": True}, "dt_window"),
])
def test_invalid_settings_are_rejected(kwargs, message):
    with pytest.raises(WatchdogError, match=message):
        WatchdogSettings(**kwargs)


def test_cli_once_exit_codes(tmp_path, capsys):
    run = _run_dir(tmp_path, [_start(LIVE, 1)])
    assert main([str(run), "--once"]) == 0
    assert main([str(tmp_path / "missing"), "--once"]) == 1
    assert main([str(run), "--once", "--min-dt-s", "0"]) == 1
