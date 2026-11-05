"""Gate dispatch LOG for large-run bundles (log only; gate opening never enters the computation)."""
from __future__ import annotations

import csv
from datetime import date, datetime
import io
import json
from pathlib import Path

import pytest

pytest.importorskip("openpyxl"); pytest.importorskip("netCDF4"); pytest.importorskip("shapefile")

from hydrodynamics.large_run_driver import check_case, run_large_case
from hydrodynamics.large_run_export import LargeRunExportError, verify_large_run_bundle
from hydrodynamics.large_run_gate_log import (CSV_COLUMNS, CSV_FILE, HOURLY_FILE, JSON_FILE, STATEMENT, GateLogError,
                                              external_records, main, match_gates, opening_from_text, read_ledger)
from hydrodynamics.large_run_profile import LargeRunProfileError, parse_profile

from test_large_run_driver import _config_dict, _free_outflow_case, _rewrite, make_case

ORIGIN = datetime.fromisoformat("2023-08-01T02:00:00+08:00")


def make_ledger(path: Path, actions=None) -> Path:
    import openpyxl
    book = openpyxl.Workbook()
    sheet = book.active; sheet.title = "调度动作"
    sheet.append(["水闸与口门调度动作序列"])
    sheet.append(["序号", "日期", "时刻", "调度对象", "调度动作", "类别", "依据 / 判据", "效果与特征值", "备注"])
    rows = actions if actions is not None else [
        (1, datetime(2023, 7, 28), "—", "全市闸涵泵站", "预泄腾库", "预泄", "强降雨预报", "累计预泄", "—"),
        (2, datetime(2023, 7, 30), "11:00", "独流减河进洪闸南闸", "27 孔全部提起敞泄", "提闸", "水位上涨", "南闸敞泄", "—"),
        (3, datetime(2023, 7, 31), "—", "独流减河进洪闸北闸", "全部闸门提起敞泄", "提闸", "", "南北全开", "—"),
        (4, datetime(2023, 8, 1), "02:00", "东淀蓄滞洪区", "启用滞洪", "启用", "", "清北滞洪", "—"),
        (5, datetime(2023, 8, 1), "—", "西河闸", "开启", "开闸", "", "控泄不超过 150 m³/s", "—"),
        (6, datetime(2023, 8, 12), "17:00", "西河闸", "关闭", "闭闸", "", "", "—"),
    ]
    for row in rows:
        sheet.append(list(row))
    inv = book.create_sheet("闸门台账")
    inv.append(["闸门台账"]); inv.append(["说明"])
    inv.append(["序号", "工程名称", "孔数", "总净宽(m)", "底槛高程(m)", "底槛基准"])
    inv.append([1, "独流减河进洪闸（北闸）", 13, 123.5, 2.26, "待核"])
    inv.append([2, "独流减河进洪闸（南闸）", 27, 256.5, 1.06, "黄海"])
    inv.append([3, "西河闸（节制闸）", 6, 56.4, -3.047, "1985 国家"])
    book.save(path)
    return path


def _rows(path: Path) -> list[dict]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))


# ------------------------------------------------------------------ unit
@pytest.mark.parametrize("text,total,ratio,bays", [
    ("27 孔全部提起敞泄", 27, 1.0, 27),
    ("全部闸门提起敞泄", 13, 1.0, 13),
    ("18 孔全部提起敞泄", 27, None, 18),      # ledger text and inventory disagree: not converted
    ("提起 11—15 孔", 27, None, None),        # range: bay numbers or count, not converted
    ("提起 9 孔", 27, 9 / 27, 9),
    ("关闭", 6, 0.0, 0),
    ("开启", 6, None, None),
])
def test_opening_from_text(text, total, ratio, bays):
    got_ratio, got_bays, note = opening_from_text(text, total)
    assert got_ratio == (pytest.approx(ratio) if ratio is not None else None) and got_bays == bays and note


def test_match_gates_splits_multi_object_cells_and_prefers_sub_gates():
    assert [g.gate_id for g in match_gates("老龙湾、第六埠、水高庄口门")] == ["laolongwan", "sixth_port_return", "shuigaozhuang"]
    assert [g.gate_id for g in match_gates("独流减河进洪闸南闸")] == ["duliu_inlet_south"]
    assert [g.gate_id for g in match_gates("独流减河进洪闸")] == ["duliu_inlet"]
    assert match_gates("东淀蓄滞洪区") == []


def test_external_records_window_initial_state_and_date_only(tmp_path):
    ledger = read_ledger(make_ledger(tmp_path / "l.xlsx"))
    assert ledger["actions"][2]["clock"] is None and ledger["actions"][2]["date"] == date(2023, 7, 31)
    records, excluded, inventory = external_records(ledger, ORIGIN, 10.0)
    by = {(r["gate_id"], r["window_position"]): r for r in records}
    south = by[("duliu_inlet_south", "before_window")]
    assert south["opening_after_ratio"] == 1.0 and south["total_bays"] == 27 and south["time_precision"] == "minute"
    north = by[("duliu_inlet_north", "before_window")]
    assert north["time_precision"] == "date" and north["time_s"] is None and north["datetime_local"] == "2023-07-31"
    assert by[("duliu_inlet_south", "initial_state")]["time_s"] == 0.0 and by[("duliu_inlet_north", "initial_state")]["opening_after_ratio"] == 1.0
    assert by[("unregistered", "in_window")]["ledger_object"] == "东淀蓄滞洪区" and by[("unregistered", "in_window")]["time_s"] == 0.0
    assert by[("xihe", "window_boundary_day")]["time_precision"] == "date"
    assert all(r["record_kind"] == "EXTERNAL_REPORT" and r["applied_in_model"] is False for r in records)
    assert {e["ledger_row"] for e in excluded} == {3, 8}  # 全市闸涵泵站 (before, unregistered), 西河闸 08-12 (after)
    assert inventory["xihe"]["bays"] == 6


def test_ledger_errors(tmp_path):
    with pytest.raises(GateLogError, match="missing"):
        read_ledger(tmp_path / "none.xlsx")
    bad = make_ledger(tmp_path / "bad.xlsx", [(1, datetime(2023, 8, 1), "25:00", "西河闸", "开启", "", "", "", "")])
    with pytest.raises(GateLogError, match="HH:MM"):
        read_ledger(bad)
    with pytest.raises(GateLogError, match="lacks sheet"):
        read_ledger(make_ledger(tmp_path / "ok.xlsx"), actions_sheet="不存在")


def test_profile_option_validation():
    assert parse_profile({"gate_log": {"boundary_gates": {"B": "duliu_inlet"}}}).gate_log.ledger_xlsx is None
    with pytest.raises(LargeRunProfileError, match="unknown gate_id"):
        parse_profile({"gate_log": {"boundary_gates": {"B": "nonexistent"}}})
    with pytest.raises(LargeRunProfileError, match="boundary_gates"):
        parse_profile({"gate_log": {"boundary_gates": {}}})
    with pytest.raises(LargeRunProfileError, match="ledger_utc_offset"):
        parse_profile({"gate_log": {"boundary_gates": {"B": "xihe"}, "ledger_utc_offset": "8h"}})


# ------------------------------------------------------------------ integration
def _prescribed_step_case(root: Path, *, gate_log: dict | None) -> Path:
    path = make_case(root)
    (root / "forcing.csv").write_text("time_s,inflow_m3s,outflow_m3s,extra\n0,0.5,0,1\n5,0.5,0,1\n10,0.5,0.2,1\n15,0.5,0.2,1\n",
                                      encoding="utf-8")
    config = _config_dict(path)
    if gate_log is not None:
        config["output"]["profile"]["gate_log"] = gate_log
    return _rewrite(path, config)


def test_export_writes_log_only_gate_dispatch_files(tmp_path):
    make_ledger(tmp_path / "ledger.xlsx")
    path = _prescribed_step_case(tmp_path, gate_log={"ledger_xlsx": "ledger.xlsx", "boundary_gates": {"east_out": "xihe"},
                                                     "prescribed_change_min_m3_s": 0.0})
    assert check_case(path)["inputs"]["gate_log"]["action_rows"] == 6
    report = run_large_case(path)
    assert report["verify"]["status"] == "PASS"
    bundle = tmp_path / "run" / "bundle"
    assert verify_large_run_bundle(bundle)["checks"]["gate_dispatch"]["status"] == "PASS"
    run = json.loads((bundle / "run.json").read_text(encoding="utf-8"))
    assert run["gate_dispatch"]["status"] == "WRITTEN_LOG_ONLY" and run["gate_dispatch"]["gate_opening_affects_computation"] is False
    assert run["legacy_optional_artifacts"][CSV_FILE]["status"] == "WRITTEN_LOG_ONLY"
    assert json.loads((bundle / "summary.json").read_text(encoding="utf-8"))["gate_dispatch"]["statement"] == STATEMENT
    rows = _rows(bundle / CSV_FILE)
    assert list(rows[0]) == list(CSV_COLUMNS)
    model = [r for r in rows if r["record_kind"] == "MODEL"]
    assert [(r["time_s"], r["discharge_before_m3_s"], r["discharge_after_m3_s"]) for r in model] == [("0.0", "", "0.0"), ("10.0", "0.0", "0.2")]
    assert model[1]["action"].startswith("开启") and model[1]["applied_in_model"] == "True" and model[1]["boundary_type"] == "prescribed_outflow"
    assert {r["record_kind"] for r in rows} == {"MODEL", "EXTERNAL_REPORT"}
    hourly = _rows(bundle / HOURLY_FILE)
    assert [h["time_s"] for h in hourly] == ["0.0", "5.0", "10.0"] and hourly[-1]["gate_id"] == "xihe"
    assert float(hourly[1]["applied_discharge_m3_s"]) == 0.0 and float(hourly[2]["applied_discharge_m3_s"]) == pytest.approx(0.2)
    doc = json.loads((bundle / JSON_FILE).read_text(encoding="utf-8"))
    assert doc["gate_opening_affects_computation"] is False and doc["ledger"]["sha256"] and doc["counts"]["records"] == len(rows)
    # the log is checked: tampering fails verify
    (bundle / CSV_FILE).write_bytes((bundle / CSV_FILE).read_bytes() + b"x\n")
    with pytest.raises(LargeRunExportError, match="differ"):
        verify_large_run_bundle(bundle)
    from hydrodynamics.large_run_gate_log import verify_gate_log  # direct check, past the bundle-wide checksum list
    with pytest.raises(GateLogError, match="sha256 differs"):
        verify_gate_log(bundle, run)


def test_check_reports_gate_log_problems(tmp_path):
    path = _prescribed_step_case(tmp_path, gate_log={"ledger_xlsx": "missing.xlsx", "boundary_gates": {"ghost": "xihe"}})
    report = check_case(path)
    assert report["status"] == "invalid"
    assert any("unknown sections ['ghost']" in e for e in report["errors"])
    assert any("missing.xlsx" in m for m in report["missing"])


def test_post_hoc_cli_appends_to_exported_bundle(tmp_path, capsys):
    ledger = make_ledger(tmp_path / "ledger.xlsx")
    path = _free_outflow_case(tmp_path)
    assert run_large_case(path)["verify"]["status"] == "PASS"
    bundle = tmp_path / "run" / "bundle"
    assert not (bundle / CSV_FILE).exists()
    assert json.loads((bundle / "run.json").read_text(encoding="utf-8"))["legacy_optional_artifacts"][CSV_FILE]["status"].startswith("NOT_")
    assert main([str(bundle), "--gate", "east_out=duliu_inlet", "--ledger", str(ledger)]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "PASS" and out["gate_dispatch"]["applied"] == "post_hoc"
    assert verify_large_run_bundle(bundle)["checks"]["gate_dispatch"]["status"] == "PASS"
    rows = _rows(bundle / CSV_FILE)
    weir = [r for r in rows if r["record_kind"] == "MODEL"]
    assert len(weir) == 1 and weir[0]["boundary_type"] == "free_outflow" and weir[0]["opening_after_ratio"] == "1.0"
    hourly = _rows(bundle / HOURLY_FILE)
    assert len(hourly) == 3 and float(hourly[-1]["cumulative_delivered_volume_m3"]) > 0.0
    listed = {row["path"] for row in json.loads((bundle / "checksums.json").read_text(encoding="utf-8"))["files"]}
    assert {CSV_FILE, JSON_FILE, HOURLY_FILE} <= listed
    assert main([str(bundle), "--gate", "east_out=duliu_inlet"]) == 1   # no-clobber
    assert "no-clobber" in capsys.readouterr().err
