"""Scheme-id output directories for the large-run driver (``<output.root>/<scheme id>``) and viewer lookup by id."""
from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from hydrodynamics.large_run_driver import LargeRunDriverError, check_case, load_driver_config, main, run_large_case
from hydrodynamics import viewer_server

from test_large_run_driver import _config_dict, _rewrite, make_case


def _rooted(path: Path, *, script_id: str | None = "scheme-01", run_id: str | None = None) -> Path:
    data = _config_dict(path)
    data["output"] = {"root": "results", "script_id": script_id, "profile": data["output"]["profile"], "export": data["output"]["export"]}
    data["run"]["run_id"] = run_id
    return _rewrite(path, data)


def test_root_and_script_id_name_the_run_directory_and_default_the_run_id(tmp_path):
    path = _rooted(make_case(tmp_path))
    config = load_driver_config(path)
    assert Path(config.output.directory) == (tmp_path / "results" / "scheme-01").resolve()
    assert config.run.run_id == "scheme-01"


def test_cli_id_overrides_config_script_id(tmp_path):
    path = _rooted(make_case(tmp_path))
    config = load_driver_config(path, script_id="cli.id_2")
    assert Path(config.output.directory).name == "cli.id_2" and config.run.run_id == "cli.id_2"


def test_explicit_run_id_is_kept(tmp_path):
    path = _rooted(make_case(tmp_path), run_id="result-label")
    assert load_driver_config(path).run.run_id == "result-label"


def test_missing_script_id_generates_the_workflow_style_id(tmp_path):
    path = _rooted(make_case(tmp_path), script_id=None)
    config = load_driver_config(path)
    name = Path(config.output.directory).name
    assert re.fullmatch(r"\d{8}-\d{19}", name) and config.run.run_id == name


@pytest.mark.parametrize("mutate, message", [
    (lambda o: o.update(directory="x"), "exactly one of output.directory or output.root"),
    (lambda o: o.pop("root"), "exactly one of output.directory or output.root"),
    (lambda o: o.update(script_id="CON"), "Windows reserved"),
    (lambda o: o.update(script_id="a/b"), "1-96 ASCII"),
    (lambda o: o.update(script_id="-lead"), "1-96 ASCII"),
    (lambda o: o.update(script_id="x" * 97), "1-96 ASCII"),
])
def test_invalid_output_target_or_scheme_id_is_rejected(tmp_path, mutate, message):
    path = _rooted(make_case(tmp_path))
    data = _config_dict(path)
    mutate(data["output"])
    _rewrite(path, data)
    with pytest.raises(LargeRunDriverError, match=message):
        load_driver_config(path)


def test_cli_id_with_explicit_directory_is_rejected(tmp_path):
    path = make_case(tmp_path)
    with pytest.raises(LargeRunDriverError, match="--id selects a directory under output.root"):
        load_driver_config(path, script_id="x1")
    with pytest.raises(LargeRunDriverError, match="invalid scheme id"):
        load_driver_config(_rooted(make_case(tmp_path / "b")), script_id="bad/id")


def test_check_reports_scheme_id_and_directory(tmp_path):
    path = _rooted(make_case(tmp_path))
    report = check_case(path, script_id="scheme-02")
    assert report["status"] == "ok", report
    assert report["run"]["scheme_id"] == "scheme-02"
    assert Path(report["run"]["output_directory"]) == (tmp_path / "results" / "scheme-02").resolve()


def test_full_run_under_scheme_id_and_viewer_finds_it_by_id(tmp_path, capsys):
    path = _rooted(make_case(tmp_path))
    assert main([str(path), "--quiet", "--id", "qb-2307-test"]) == 0
    run_dir = tmp_path / "results" / "qb-2307-test"
    report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "complete" and report["run_id"] == "qb-2307-test"
    assert json.loads((run_dir / "bundle" / "run.json").read_text(encoding="utf-8"))["run_id"] == "qb-2307-test"
    # a second fresh run with the same id refuses to overwrite
    assert main([str(path), "--quiet", "--id", "qb-2307-test"]) == 1
    # viewer: a run directory with a complete large-run bundle is resolved to <id>/bundle and is the latest success
    project = tmp_path / "project"
    (project / "work").mkdir(parents=True)
    (tmp_path / "results").rename(project / "work" / "results")
    assert viewer_server._large_run_bundle(project / "work" / "results" / "qb-2307-test") is not None
    assert viewer_server._latest_success(project) == (project / "work" / "results" / "qb-2307-test" / "bundle").resolve()
