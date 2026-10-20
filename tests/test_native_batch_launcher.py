import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hydrodynamics.native_batch_launcher import (
    NativeBatchError,
    _output_directory,
    launcher_schema,
)
from hydrodynamics.run_identity import resolve_script_id


ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "run_qingbei_nc.bat"


def _invoke_batch(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    wrapper = cwd / "independent_call.cmd"
    batch_command = subprocess.list2cmdline([str(BATCH), *(str(item) for item in arguments)])
    wrapper.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f"call {batch_command}\r\n"
        "set \"__native_batch_rc=%ERRORLEVEL%\"\r\n"
        "echo __NATIVE_BATCH_CALLER_CWD__\r\n"
        "cd\r\n"
        "exit /b %__native_batch_rc%\r\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [comspec, "/d", "/c", str(wrapper)], cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=300,
    )


def test_launcher_schema_and_generated_run_id(tmp_path):
    schema_path = ROOT / "examples" / "native_batch" / "native_batch_launcher.schema.json"
    assert json.loads(schema_path.read_text(encoding="utf-8")) == launcher_schema()
    assert launcher_schema()["synthetic_launch_config_schema"]["additionalProperties"] is False
    assert launcher_schema()["engineering_preflight_config_schema"]["additionalProperties"] is False
    assert resolve_script_id("explicit-run_01") == "explicit-run_01"
    assert re.fullmatch(r"\d{8}-\d{19}", resolve_script_id())


def test_launcher_resolves_default_id_through_cli(tmp_path, monkeypatch, capsys):
    import hydrodynamics.native_batch_launcher as module

    expected_id = "20261020-1234567890123456789"
    resolved_values = []
    monkeypatch.setattr(module, "resolve_script_id", lambda value=None: resolved_values.append(value) or expected_id)
    monkeypatch.setattr(module, "_find_project_root", lambda: ROOT)
    monkeypatch.setattr(module, "_project_python", lambda _project_root: ROOT / ".venv" / "Scripts" / "python.exe")
    output = tmp_path / "default run"
    report = SimpleNamespace(
        status="complete", mode="synthetic_demo", data_origin="SYNTHETIC_ONLY",
        case_id="security-exclusion-synthetic-dynamic-v1", script_id=expected_id,
        output_directory=str(output), failed_stage=None, error=None, stages=[],
    )
    monkeypatch.setattr(module, "_run_synthetic_batch", lambda *_args: (report, 0))

    assert module.main(["--out-dir", str(output)]) == 0
    assert resolved_values == [None]
    assert json.loads(capsys.readouterr().out)["script_id"] == expected_id
    assert output.is_dir()


def test_output_collision_refuses_without_touching_existing_data(tmp_path):
    existing = tmp_path / "100001"
    existing.mkdir()
    sentinel = existing / "sentinel.bin"
    sentinel.write_bytes(b"preserve historical output")
    with pytest.raises(NativeBatchError, match="already exists"):
        _output_directory(tmp_path, str(existing), "100001")
    assert sentinel.read_bytes() == b"preserve historical output"


@pytest.mark.parametrize("relative", ["junction/fresh", "junction/../fresh"])
def test_output_path_rejects_reparse_component_before_normalizing(tmp_path, monkeypatch, relative):
    import hydrodynamics.native_batch_launcher as module

    project_root = tmp_path / "project"
    project_root.mkdir()
    junction = project_root / "junction"
    observed = []
    original_lstat = Path.lstat

    def lstat_with_junction(path):
        if path == junction:
            observed.append(path)
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_junction)
    with pytest.raises(NativeBatchError, match="reparse point"):
        _output_directory(project_root, relative, "reparse-check")
    assert junction in observed
    assert not (project_root / "fresh").exists()


def test_launcher_runs_synthetic_source_chain_from_external_unicode_cwd(tmp_path):
    caller = tmp_path / "caller path 中文"
    caller.mkdir()
    output = tmp_path / "output path 空格" / "launcher-script-01"
    completed = _invoke_batch(caller, "--script-id", "launcher-script-01", "--out-dir", str(output))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"__NATIVE_BATCH_CALLER_CWD__\n{caller}" in completed.stdout

    report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert report["mode"] == "synthetic_demo" and report["data_origin"] == "SYNTHETIC_ONLY"
    assert report["script_id"] == summary["script_id"] == "launcher-script-01"
    assert report["engine_mode"] == "triangular_source_experimental"
    assert all(stage["script_id"] == report["script_id"] for stage in report["stages"])
    assert all(stage["data_origin"] == "SYNTHETIC_ONLY" and stage["exit_code"] == 0 for stage in report["stages"])
    assert summary["case_id"] == "security-exclusion-synthetic-dynamic-v1"
    assert summary["face_count"] == 4 and summary["time_s"] == [0.0, 0.05, 0.1]
    assert summary["rainfall_accepted_active_total_m3"] == pytest.approx(0.00006)
    assert summary["rainfall_excluded_unmodeled_total_m3"] == pytest.approx(0.00002)
    assert summary["nonzero_manning_effect_applied"] is True
    assert summary["rainfall_nonzero_effect_applied"] is True
    assert summary["base_none_result_generated_or_read"] is False
    assert not (output / "inputs" / "security_exclusion" / "base_none_dynamic.nc").exists()
    assert (output / "inputs" / "security_exclusion" / "combined.nc").is_file()
    assert (output / "inputs" / "security_exclusion" / "combined_native_shp_v2" / "manifest.json").is_file()
    assert (output / "inputs" / "security_exclusion" / "combined_frame_native" / "manifest.json").is_file()
    assert (output / "preview.svg").is_file()


def test_launcher_rejects_unknown_args_and_engineering_is_preflight_only(tmp_path):
    caller = tmp_path / "external"
    caller.mkdir()
    unknown_out = tmp_path / "unknown-out"
    unknown = _invoke_batch(caller, "--workflow", "examples/case_001/workflow.yaml", "--out-dir", str(unknown_out))
    assert unknown.returncode != 0
    assert not unknown_out.exists()

    engineering_out = tmp_path / "engineering preflight"
    blocked = _invoke_batch(caller, "--engineering", "--script-id", "case001-preflight", "--out-dir", str(engineering_out))
    assert blocked.returncode == 3, blocked.stdout + blocked.stderr
    report = json.loads((engineering_out / "run_report.json").read_text(encoding="utf-8"))
    codes = {item["code"] for item in report["blockers"]}
    assert report["status"] == "blocked"
    assert report["data_origin"] == "ENGINEERING_PRECHECK"
    assert report["preflight_only"] is True and report["execution_available"] is False
    assert "UNSUPPORTED_OPEN_BOUNDARY_EXECUTION" in codes
    assert "ENGINEERING_EXECUTION_NOT_IN_SCOPE" in codes
    assert not list(engineering_out.rglob("*.nc"))
    assert not list(engineering_out.rglob("*.shp"))
    assert all(item["content_read"] is False for item in report["input_path_checks"])
