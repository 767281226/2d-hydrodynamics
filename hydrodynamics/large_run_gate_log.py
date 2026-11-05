"""Gate dispatch LOG for large-run bundles -- record only, never part of the computation.

Writes three files into an exported large-run bundle::

    gate_dispatch.csv          one row per record (UTF-8 with BOM for spreadsheet use)
    gate_dispatch.json         the same records + window, statement, sources, excluded ledger rows
    gate_discharge_hourly.csv  per saved frame and logged boundary gate: modelled discharge and volumes

Two record kinds, always labelled:

* ``MODEL``: the boundary condition the solver actually applied, derived from the run metadata
  and the forcing CSV it hashed -- a ``free_outflow`` section is a fully-open free weir for the
  whole run; a prescribed section logs every step of its given discharge (e.g. 西河闸 0 -> 125 m3/s).
* ``EXTERNAL_REPORT``: actions transcribed from the dispatch ledger workbook (sheet 调度动作) that
  fall inside the run window, plus the pre-window actions of registered gates (initial state at t=0).
  Bay counts are converted to an opening ratio only when the ledger text and the gate inventory
  (sheet 闸门台账) agree; clock-less rows keep ``time_precision="date"`` and no invented time.

Gate opening does not affect the computation; the bundle states this in run.json and in every file.

CLI (append to an already exported bundle, in place, no-clobber)::

    python -m hydrodynamics.large_run_gate_log <bundle_dir> --gate B=duliu_inlet --gate C=xihe [--ledger ledger.xlsx]
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping

import numpy as np

from .gate_dispatch import GATE_RULES, _GATE_NAMES

GATE_LOG_FORMAT = "large-run-gate-dispatch-log-v1"
CSV_FILE = "gate_dispatch.csv"
JSON_FILE = "gate_dispatch.json"
HOURLY_FILE = "gate_discharge_hourly.csv"
GATE_LOG_FILES = (CSV_FILE, JSON_FILE, HOURLY_FILE)
STATEMENT = ("LOG ONLY - gate opening does not affect the computation. MODEL rows describe the boundary condition the "
             "solver actually applied (free_outflow = fully-open free weir for the whole run; prescribed = the given "
             "discharge series). EXTERNAL_REPORT rows are transcribed from the dispatch ledger and were not used by the solver.")
STATEMENT_ZH = "仅为调度日志：闸门开度不参与计算。MODEL 行为模型实际采用的边界条件；EXTERNAL_REPORT 行摘自调度台账，未进入计算。"
CSV_COLUMNS = ("record_id", "record_kind", "source_category", "applied_in_model", "gate_id", "gate_name", "ledger_object",
               "boundary_group", "boundary_type", "time_s", "datetime_local", "time_precision", "window_position",
               "action", "opening_before_ratio", "opening_after_ratio", "open_bays", "total_bays",
               "discharge_before_m3_s", "discharge_after_m3_s", "reason", "source", "note", "candidate_rule_ids")
HOURLY_COLUMNS = ("time_s", "datetime_local", "gate_id", "boundary_group", "boundary_type", "applied_discharge_m3_s",
                  "mean_delivered_discharge_prev_interval_m3_s", "cumulative_delivered_volume_m3",
                  "cumulative_requested_volume_m3", "cumulative_shortfall_volume_m3", "cumulative_inflow_volume_m3")


class GateLogError(ValueError):
    """Invalid gate log input (ledger, mapping, bundle)."""


@dataclass(frozen=True)
class GateEntry:
    gate_id: str
    name: str
    ledger_keywords: tuple[str, ...]
    inventory_keyword: str | None = None
    parent: str | None = None

    @property
    def rule_ids(self) -> tuple[str, ...]:
        key = self.parent or self.gate_id
        return tuple(rule.rule_id for rule in GATE_RULES if rule.gate_id == key)


# Order matters: sub-gates before their parent (a match on a sub-gate suppresses the parent).
GATE_REGISTRY = (
    GateEntry("duliu_inlet_south", "独流减河进洪闸南闸", ("独流减河进洪闸南闸",), "独流减河进洪闸（南闸）", "duliu_inlet"),
    GateEntry("duliu_inlet_north", "独流减河进洪闸北闸", ("独流减河进洪闸北闸",), "独流减河进洪闸（北闸）", "duliu_inlet"),
    GateEntry("duliu_inlet", _GATE_NAMES.get("duliu_inlet", "独流减河进洪闸"), ("独流减河进洪闸",)),
    GateEntry("xihe", _GATE_NAMES.get("xihe", "西河闸"), ("西河闸",), "西河闸"),
    GateEntry("duliu_tidal", _GATE_NAMES.get("duliu_tidal", "独流减河防潮闸"), ("独流减河防潮闸",), "独流减河防潮闸"),
    GateEntry("laolongwan", _GATE_NAMES.get("laolongwan", "老龙湾退洪闸"), ("老龙湾",)),
    GateEntry("sixth_port_return", _GATE_NAMES.get("sixth_port_return", "第六埠退洪闸"), ("第六埠",)),
    GateEntry("shuigaozhuang", _GATE_NAMES.get("shuigaozhuang", "水高庄退洪闸"), ("水高庄",)),
    GateEntry("yangliuqing", _GATE_NAMES.get("yangliuqing", "杨柳青退洪闸"), ("杨柳青",)),
    GateEntry("qingnan", _GATE_NAMES.get("qingnan", "清南分洪闸"), ("清南分洪闸",)),
    GateEntry("renzhuangzi", _GATE_NAMES.get("renzhuangzi", "任庄子口门"), ("任庄子",)),
    GateEntry("xingaifang", "新盖房分洪闸", ("新盖房分洪闸",)),
)
GATE_IDS = tuple(entry.gate_id for entry in GATE_REGISTRY)
_BY_ID = {entry.gate_id: entry for entry in GATE_REGISTRY}
_DASHES = {"", "—", "-", "–", "--", "——", "/"}


# ------------------------------------------------------------------ helpers
def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _offset(text: str) -> timezone:
    sign = 1 if text[0] == "+" else -1
    hours, minutes = int(text[1:3]), int(text[4:6])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _num(value: float | None) -> float | None:
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def match_gates(text: str) -> list[GateEntry]:
    """Registered gates named in a ledger 调度对象 cell (several for '老龙湾、第六埠、水高庄口门')."""
    found: list[GateEntry] = []
    for entry in GATE_REGISTRY:
        if any(k in text for k in entry.ledger_keywords) and not any(m.parent == entry.gate_id for m in found):
            found.append(entry)
    return found


def opening_from_text(action: str, total_bays: int | None) -> tuple[float | None, int | None, str]:
    """(opening_after_ratio, open_bays, note) from a ledger action; None when the text does not determine it."""
    text = action.replace(" ", "")
    if re.search(r"\d+[—\-–~至]+\d+孔", text):
        return None, None, "孔号/孔数范围，未换算开度"
    single = re.search(r"(\d+)孔", text)
    if any(k in text for k in ("全部提起", "全部闸门提起", "全开", "全部开启")):
        if single and total_bays and int(single.group(1)) != total_bays:
            return None, int(single.group(1)), f"台账写 {single.group(1)} 孔全部提起，但闸门台账为 {total_bays} 孔，口径不一致，未换算"
        if total_bays:
            return 1.0, total_bays, "全部提起：开度 1.0（按孔数）"
        return 1.0, None, "全部提起：开度 1.0（孔数未载明）"
    if "关闭" in text or "闭闸" in text:
        return 0.0, 0, "关闭：开度 0"
    if single and total_bays:
        n = int(single.group(1))
        if n <= total_bays:
            return n / total_bays, n, f"{n}/{total_bays} 孔"
        return None, n, f"{n} 孔超过闸门台账 {total_bays} 孔，未换算"
    return None, None, "开度未载明"


# ------------------------------------------------------------------ ledger
def _header_index(rows: list[tuple], required: str, sheet: str) -> tuple[int, dict[str, int]]:
    for i, row in enumerate(rows[:10]):
        cells = [str(c).strip() if c is not None else "" for c in row]
        if required in cells:
            return i, {name: j for j, name in enumerate(cells) if name}
    raise GateLogError(f"ledger sheet {sheet!r}: header row with {required!r} not found in the first 10 rows")


def read_ledger(path: str | Path, actions_sheet: str = "调度动作", inventory_sheet: str = "闸门台账",
                utc_offset: str = "+08:00") -> dict[str, Any]:
    """Raw ledger rows (actions with parsed date/time) and gate inventory (bays / net width) -- no interpretation."""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GateLogError("reading the dispatch ledger needs openpyxl (pip install '.[gate-log]')") from exc
    source = Path(path)
    if not source.is_file():
        raise GateLogError(f"dispatch ledger workbook is missing: {source}")
    try:
        book = openpyxl.load_workbook(source, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several unrelated types for bad files
        raise GateLogError(f"dispatch ledger {source} cannot be read: {exc}") from exc
    try:
        for name in (actions_sheet, inventory_sheet):
            if name not in book.sheetnames:
                raise GateLogError(f"dispatch ledger lacks sheet {name!r} (sheets: {book.sheetnames})")
        tz = _offset(utc_offset)
        rows = [tuple(r) for r in book[actions_sheet].iter_rows(values_only=True)]
        head, cols = _header_index(rows, "调度对象", actions_sheet)
        for need in ("日期", "调度对象", "调度动作"):
            if need not in cols:
                raise GateLogError(f"ledger sheet {actions_sheet!r} lacks column {need!r}")

        def cell(row: tuple, name: str) -> Any:
            j = cols.get(name)
            return row[j] if j is not None and j < len(row) else None

        actions = []
        for number, row in enumerate(rows[head + 1:], start=head + 2):
            obj = cell(row, "调度对象")
            if obj is None or not str(obj).strip():
                continue
            raw_date = cell(row, "日期")
            if isinstance(raw_date, datetime):
                day = raw_date.date()
            elif isinstance(raw_date, date):
                day = raw_date
            else:
                try:
                    day = date.fromisoformat(str(raw_date).strip()[:10])
                except ValueError as exc:
                    raise GateLogError(f"ledger sheet {actions_sheet!r} row {number}: 日期 {raw_date!r} is not a date") from exc
            raw_time = cell(row, "时刻")
            clock: dtime | None
            if isinstance(raw_time, dtime):
                clock = raw_time
            elif isinstance(raw_time, datetime):
                clock = raw_time.time()
            else:
                text = "" if raw_time is None else str(raw_time).strip()
                if text in _DASHES:
                    clock = None
                else:
                    m = re.fullmatch(r"(\d{1,2})[:：](\d{2})", text)
                    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                        raise GateLogError(f"ledger sheet {actions_sheet!r} row {number}: 时刻 {text!r} is not HH:MM or '—'")
                    clock = dtime(int(m.group(1)), int(m.group(2)))
            actions.append({"row": number, "date": day, "clock": clock,
                            "datetime": None if clock is None else datetime.combine(day, clock, tzinfo=tz),
                            "object": str(obj).strip(), "action": str(cell(row, "调度动作") or "").strip(),
                            "category": str(cell(row, "类别") or "").strip(),
                            "basis": str(cell(row, "依据 / 判据") or cell(row, "依据") or "").strip(),
                            "effect": str(cell(row, "效果与特征值") or "").strip(),
                            "remark": str(cell(row, "备注") or "").strip()})
        inv_rows = [tuple(r) for r in book[inventory_sheet].iter_rows(values_only=True)]
        ihead, icols = _header_index(inv_rows, "工程名称", inventory_sheet)
        inventory = []
        for row in inv_rows[ihead + 1:]:
            j = icols["工程名称"]
            if j >= len(row) or row[j] is None:
                continue

            def icell(name: str) -> Any:
                k = icols.get(name)
                return row[k] if k is not None and k < len(row) else None

            bays = icell("孔数")
            inventory.append({"name": str(row[j]).strip(),
                              "bays": int(bays) if isinstance(bays, (int, float)) and not isinstance(bays, bool) else None,
                              "net_width_m": _num(icell("总净宽(m)")) if isinstance(icell("总净宽(m)"), (int, float)) else None,
                              "sill_m": _num(icell("底槛高程(m)")) if isinstance(icell("底槛高程(m)"), (int, float)) else None,
                              "sill_datum": None if icell("底槛基准") is None else str(icell("底槛基准")).strip()})
    finally:
        book.close()
    return {"path": str(source), "sha256": _sha(source), "actions_sheet": actions_sheet, "inventory_sheet": inventory_sheet,
            "utc_offset": utc_offset, "actions": actions, "inventory": inventory}


def _inventory_for(entry: GateEntry, inventory: list[dict[str, Any]]) -> dict[str, Any] | None:
    if entry.inventory_keyword is None:
        return None
    hits = [item for item in inventory if entry.inventory_keyword in item["name"]]
    return hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------ records
def _record(**fields: Any) -> dict[str, Any]:
    base = {name: None for name in CSV_COLUMNS}
    base.update(fields)
    return base


def _window_position(action: Mapping[str, Any], origin: datetime, end: datetime) -> str:
    if action["datetime"] is not None:
        t = action["datetime"]
        return "before_window" if t < origin else ("after_window" if t > end else "in_window")
    tz = origin.tzinfo
    start = datetime.combine(action["date"], dtime(0, 0), tzinfo=tz)
    stop = start + timedelta(days=1)
    if stop <= origin:
        return "before_window"
    if start > end:
        return "after_window"
    return "in_window" if start >= origin and stop <= end else "window_boundary_day"


_OPERATION_WORDS = ("提起", "提闸", "关闭", "闭闸", "关闸", "开启", "开闸", "敞泄", "控泄", "扒开", "封堵")


def _is_gate_operation(action: Mapping[str, Any]) -> bool:
    """True for rows that change a gate state (not e.g. an emergency-response announcement)."""
    text = action["action"] + action["category"]
    return any(word in text for word in _OPERATION_WORDS)


def external_records(ledger: Mapping[str, Any], origin: datetime, end_s: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """EXTERNAL_REPORT records, excluded ledger rows and the inventory used (no interpretation beyond bay ratios)."""
    end = origin + timedelta(seconds=end_s)
    records: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    inventory_used: dict[str, Any] = {}
    last_ratio: dict[str, float | None] = {}
    pre_window: dict[str, dict[str, Any]] = {}
    ordered = sorted(ledger["actions"], key=lambda a: (a["datetime"] or datetime.combine(a["date"], dtime(0, 0), tzinfo=origin.tzinfo), a["row"]))
    for action in ordered:
        position = _window_position(action, origin, end)
        gates = match_gates(action["object"])
        if position == "after_window" or (position == "before_window" and not gates):
            excluded.append({"ledger_row": action["row"], "date": action["date"].isoformat(), "object": action["object"],
                             "reason": "after the run window" if position == "after_window" else "before the run window, not a registered gate"})
            continue
        for entry in gates or [None]:
            gate_id = entry.gate_id if entry else "unregistered"
            inv = _inventory_for(entry, ledger["inventory"]) if entry else None
            if inv is not None:
                inventory_used[gate_id] = inv
            total = inv["bays"] if inv else None
            ratio, bays, note = opening_from_text(action["action"], total) if entry else (None, None, "未登记闸门，只记录原文")
            t_s = None if action["datetime"] is None else (action["datetime"] - origin).total_seconds()
            stamp = (action["datetime"].isoformat() if action["datetime"] is not None else action["date"].isoformat())
            rec = _record(record_kind="EXTERNAL_REPORT", source_category="external_report", applied_in_model=False,
                          gate_id=gate_id, gate_name=entry.name if entry else action["object"], ledger_object=action["object"],
                          time_s=t_s, datetime_local=stamp, time_precision="minute" if action["clock"] else "date",
                          window_position=position, action=action["action"],
                          opening_before_ratio=last_ratio.get(gate_id), opening_after_ratio=ratio, open_bays=bays, total_bays=total,
                          reason=" | ".join(x for x in (action["category"], action["basis"]) if x),
                          source=f"调度台账 {ledger['actions_sheet']} 第 {action['row']} 行：{action['effect']}" + (f"（{action['remark']}）" if action["remark"] and action["remark"] not in _DASHES else ""),
                          note=note, candidate_rule_ids=";".join(entry.rule_ids) if entry else "")
            if ratio is not None:
                last_ratio[gate_id] = ratio
            records.append(rec)
            if position == "before_window" and entry is not None and _is_gate_operation(action):
                pre_window[gate_id] = rec
    initial = []
    for gate_id, rec in pre_window.items():
        entry = _BY_ID[gate_id]
        initial.append(_record(record_kind="EXTERNAL_REPORT", source_category="external_report_initial_state", applied_in_model=False,
                               gate_id=gate_id, gate_name=entry.name, ledger_object=rec["ledger_object"], time_s=0.0,
                               datetime_local=origin.isoformat(), time_precision="derived", window_position="initial_state",
                               action=f"计算开始时的状态（取窗口前最后一次调度：{rec['datetime_local']} {rec['action']}）",
                               opening_after_ratio=rec["opening_after_ratio"], open_bays=rec["open_bays"], total_bays=rec["total_bays"],
                               reason="窗口前调度动作延续到计算开始", source=rec["source"], note=rec["note"],
                               candidate_rule_ids=rec["candidate_rule_ids"]))
    return records + initial, excluded, inventory_used


def _series_at(times: np.ndarray, values: np.ndarray, t: float) -> float:
    return float(np.interp(t, times, values))


def model_records(boundary_gates: Mapping[str, str], solver: Mapping[str, Any], forcing: Mapping[str, Any] | None,
                  origin: datetime, times_s: list[float], group_series: Mapping[str, list[float]], min_change: float) -> list[dict[str, Any]]:
    groups = (solver.get("sections") or {}).get("groups") or {}
    end_s = float(times_s[-1])
    out = []
    for group, gate_id in boundary_gates.items():
        if group not in groups:
            raise GateLogError(f"gate_log.boundary_gates names boundary group {group!r}, not in the run (groups: {sorted(groups)})")
        meta = groups[group]
        entry = _BY_ID[gate_id]
        btype = meta.get("boundary_type") or ("inflow" if meta.get("role") == "inflow" else "prescribed_outflow")
        common = dict(record_kind="MODEL", applied_in_model=True, gate_id=gate_id, gate_name=entry.name, boundary_group=group,
                      boundary_type=btype, candidate_rule_ids=";".join(entry.rule_ids))
        if btype == "free_outflow":
            q0 = group_series.get(f"groups__{group}__outflow_discharge_m3_s")
            out.append(_record(**common, source_category="model_boundary_state", time_s=0.0, datetime_local=origin.isoformat(),
                               time_precision="second", window_position="whole_run",
                               action="自由出流（全开堰流），贯穿整个计算时段 / modelled as fully-open free weir for the whole run",
                               opening_after_ratio=1.0, discharge_after_m3_s=None if not q0 else _num(q0[0]),
                               reason=f"free_outflow: C_d {meta.get('discharge_coefficient')}, crest {meta.get('crest_elevation_m')} m",
                               source="run metadata solver.sections.groups", note="开度固定为全开；流量由水位按堰流公式计算，见 gate_discharge_hourly.csv"))
            continue
        if forcing is None:
            raise GateLogError(f"boundary group {group} is prescribed; the forcing CSV is needed to log its discharge changes")
        column = meta.get("discharge_series_column")
        if column not in forcing["series"]:
            raise GateLogError(f"forcing CSV lacks column {column!r} of boundary group {group}")
        t = np.asarray(forcing["time_s"], dtype=float); v = np.asarray(forcing["series"][column], dtype=float)
        q_start = _series_at(t, v, 0.0)
        label = "关闭（给定流量 0）" if q_start == 0.0 else f"给定流量 {q_start:g} m3/s"
        out.append(_record(**common, source_category="model_prescribed_change", time_s=0.0, datetime_local=origin.isoformat(),
                           time_precision="second", window_position="initial_state", action=f"计算开始：{label}",
                           discharge_after_m3_s=q_start, reason=f"prescribed series column {column}",
                           source="forcing CSV (hashed in run source_hashes.forcing_csv)",
                           note="开度未建模；按给定流量出流"))
        previous_t, previous_q = 0.0, q_start
        for ti, qi in zip(t.tolist(), v.tolist()):
            if ti <= 0.0 or ti > end_s:
                if ti > end_s:
                    break
                continue
            if abs(qi - previous_q) > float(min_change):
                action = "开启" if previous_q == 0.0 and qi > 0.0 else ("关闭" if qi == 0.0 and previous_q > 0.0 else "调整")
                out.append(_record(**common, source_category="model_prescribed_change", time_s=ti,
                                   datetime_local=(origin + timedelta(seconds=ti)).isoformat(), time_precision="second",
                                   window_position="in_window", action=f"{action}：给定流量 {previous_q:g} -> {qi:g} m3/s",
                                   discharge_before_m3_s=previous_q, discharge_after_m3_s=qi,
                                   reason=f"prescribed series column {column}", source="forcing CSV",
                                   note=f"样本间线性插值：{previous_t:g} s 至 {ti:g} s 由 {previous_q:g} 渐变到 {qi:g} m3/s；开度未建模"))
            previous_t, previous_q = ti, qi
    return out


def hourly_rows(boundary_gates: Mapping[str, str], solver: Mapping[str, Any], forcing: Mapping[str, Any] | None,
                origin: datetime, times_s: list[float], group_series: Mapping[str, list[float]]) -> list[dict[str, Any]]:
    groups = (solver.get("sections") or {}).get("groups") or {}
    rows = []
    for k, t in enumerate(times_s):
        for group, gate_id in boundary_gates.items():
            meta = groups[group]
            btype = meta.get("boundary_type") or ("inflow" if meta.get("role") == "inflow" else "prescribed_outflow")

            def series(name: str) -> list[float] | None:
                return group_series.get(f"groups__{group}__{name}")

            delivered = series("cumulative_outflow_delivered_volume_m3"); requested = series("cumulative_outflow_requested_volume_m3")
            inflow = series("cumulative_inflow_volume_m3")
            if btype == "free_outflow":
                q = series("outflow_discharge_m3_s"); applied = None if q is None else _num(q[k])
            elif forcing is not None:
                applied = _series_at(np.asarray(forcing["time_s"], float), np.asarray(forcing["series"][meta["discharge_series_column"]], float), t)
            else:
                applied = None
            mean = None
            if k > 0 and delivered is not None and t > times_s[k - 1]:
                mean = (delivered[k] - delivered[k - 1]) / (t - times_s[k - 1])
            rows.append({"time_s": t, "datetime_local": (origin + timedelta(seconds=t)).isoformat(), "gate_id": gate_id,
                         "boundary_group": group, "boundary_type": btype, "applied_discharge_m3_s": applied,
                         "mean_delivered_discharge_prev_interval_m3_s": mean,
                         "cumulative_delivered_volume_m3": None if delivered is None else _num(delivered[k]),
                         "cumulative_requested_volume_m3": None if requested is None else _num(requested[k]),
                         "cumulative_shortfall_volume_m3": None if delivered is None or requested is None else _num(requested[k] - delivered[k]),
                         "cumulative_inflow_volume_m3": None if inflow is None else _num(inflow[k])})
    return rows


# ------------------------------------------------------------------ build / write
def _csv_bytes(columns: tuple[str, ...], rows: list[Mapping[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})
    return buffer.getvalue().encode("utf-8-sig")


def load_forcing_for_run(solver: Mapping[str, Any], source_hashes: Mapping[str, str], override: str | Path | None = None) -> dict[str, Any] | None:
    """Re-read the forcing CSV the run hashed (sha must match); None when the run metadata names none."""
    from .large_run_driver import LargeRunDriverError, read_forcing_csv
    info = solver.get("forcing_csv") or {}
    path = override or info.get("path")
    if not path:
        return None
    groups = (solver.get("sections") or {}).get("groups") or {}
    columns = {k: g["discharge_series_column"] for k, g in groups.items() if g.get("discharge_series_column")}
    try:
        forcing = read_forcing_csv(path, columns)
    except LargeRunDriverError as exc:
        raise GateLogError(f"forcing CSV for the gate log: {exc}") from exc
    expected = source_hashes.get("forcing_csv")
    if expected and forcing["sha256"] != expected:
        raise GateLogError(f"forcing CSV {path} sha256 differs from the run's source_hashes.forcing_csv")
    return forcing


def build_gate_log(options: Any, *, run_id: str, case_id: str, time_origin: str, times_s: list[float],
                   group_series: Mapping[str, list[float]], solver: Mapping[str, Any], source_hashes: Mapping[str, str],
                   forcing_override: str | Path | None = None) -> dict[str, Any]:
    """Return {'json': doc, 'csv': bytes, 'hourly': bytes} for :class:`large_run_profile.GateLogOptions` ``options``."""
    origin = datetime.fromisoformat(time_origin)
    forcing = load_forcing_for_run(solver, source_hashes, forcing_override)
    records = model_records(options.boundary_gates, solver, forcing, origin, times_s, group_series, float(options.prescribed_change_min_m3_s))
    excluded: list[dict[str, Any]] = []
    inventory: dict[str, Any] = {}
    ledger_info = None
    if options.ledger_xlsx is not None:
        ledger = read_ledger(options.ledger_xlsx, options.actions_sheet, options.inventory_sheet, options.ledger_utc_offset)
        external, excluded, inventory = external_records(ledger, origin, float(times_s[-1]))
        records += external
        ledger_info = {"file": Path(ledger["path"]).name, "path": ledger["path"], "sha256": ledger["sha256"],
                       "actions_sheet": ledger["actions_sheet"], "inventory_sheet": ledger["inventory_sheet"],
                       "utc_offset": ledger["utc_offset"], "action_rows": len(ledger["actions"])}

    def order(rec: Mapping[str, Any]) -> tuple:
        if rec["time_s"] is not None:
            return (rec["time_s"], 0 if rec["record_kind"] == "MODEL" else 1)
        day = datetime.fromisoformat(rec["datetime_local"]).replace(tzinfo=origin.tzinfo) if len(rec["datetime_local"]) > 10 else \
            datetime.combine(date.fromisoformat(rec["datetime_local"]), dtime(0, 0), tzinfo=origin.tzinfo)
        return ((day - origin).total_seconds(), 2)

    records.sort(key=order)
    for i, rec in enumerate(records, start=1):
        rec["record_id"] = f"G{i:04d}"
    hourly = hourly_rows(options.boundary_gates, solver, forcing, origin, times_s, group_series)
    doc = {"format": GATE_LOG_FORMAT, "schema_version": "1.0", "run_id": run_id, "case_id": case_id,
           "statement": STATEMENT, "statement_zh": STATEMENT_ZH, "gate_opening_affects_computation": False,
           "time_origin": time_origin, "window": {"start": origin.isoformat(), "end": (origin + timedelta(seconds=times_s[-1])).isoformat(),
                                                 "end_time_s": float(times_s[-1])},
           "boundary_gates": dict(options.boundary_gates), "options": options.model_dump(mode="json"),
           "ledger": ledger_info, "forcing_csv": None if forcing is None else {"path": forcing["path"], "sha256": forcing["sha256"]},
           "gate_registry": {e.gate_id: {"name": e.name, "parent": e.parent, "candidate_rule_ids": list(e.rule_ids)} for e in GATE_REGISTRY},
           "inventory_used": inventory, "record_columns": list(CSV_COLUMNS), "records": records,
           "excluded_ledger_rows": excluded, "hourly_file": HOURLY_FILE, "hourly_columns": list(HOURLY_COLUMNS),
           "counts": {"records": len(records), "model": sum(r["record_kind"] == "MODEL" for r in records),
                      "external_report": sum(r["record_kind"] == "EXTERNAL_REPORT" for r in records),
                      "date_only": sum(r["time_precision"] == "date" for r in records), "hourly_rows": len(hourly)},
           "notes": ["candidate_rule_ids lists the dispatch rules written for that gate (docs/dispatchRule.docx); "
                     "it does not claim a rule was evaluated or triggered.",
                     "opening ratios come only from bay counts the ledger states and the gate inventory confirms.",
                     "CSV files are UTF-8 with BOM."]}
    return {"json": doc, "csv": _csv_bytes(CSV_COLUMNS, records), "hourly": _csv_bytes(HOURLY_COLUMNS, hourly)}


def write_gate_log(out: Path, built: Mapping[str, Any]) -> dict[str, Any]:
    """Write the three files (no-clobber) and return the run.json block."""
    contents = {CSV_FILE: built["csv"], JSON_FILE: (json.dumps(built["json"], ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"),
                HOURLY_FILE: built["hourly"]}
    for name in GATE_LOG_FILES:
        if (out / name).exists():
            raise GateLogError(f"{name} already exists in {out}; no-clobber refused")
    for name in GATE_LOG_FILES:
        with (out / name).open("xb") as stream:
            stream.write(contents[name])
    doc = built["json"]
    return {"status": "WRITTEN_LOG_ONLY", "statement": STATEMENT, "gate_opening_affects_computation": False,
            "files": {name: {"sha256": _sha(out / name), "bytes": (out / name).stat().st_size} for name in GATE_LOG_FILES},
            "counts": doc["counts"], "boundary_gates": doc["boundary_gates"],
            "ledger_sha256": None if doc["ledger"] is None else doc["ledger"]["sha256"]}


def legacy_entries(block: Mapping[str, Any]) -> dict[str, Any]:
    return {CSV_FILE: {"status": block["status"], "sha256": block["files"][CSV_FILE]["sha256"]},
            JSON_FILE: {"status": block["status"], "sha256": block["files"][JSON_FILE]["sha256"]},
            "dispatch_input.json": {"status": "NOT_APPLICABLE_LOG_ONLY_NO_DISPATCH_INPUT"}}


def verify_gate_log(out: Path, run: Mapping[str, Any]) -> dict[str, Any] | None:
    """Check the three files against run.json; None when the bundle has no gate log."""
    block = run.get("gate_dispatch")
    if not block:
        if any((out / name).exists() for name in GATE_LOG_FILES):
            raise GateLogError("gate dispatch files exist but run.json has no gate_dispatch block")
        return None
    for name in GATE_LOG_FILES:
        path = out / name
        if not path.is_file():
            raise GateLogError(f"gate log file {name} is missing")
        if _sha(path) != block["files"][name]["sha256"]:
            raise GateLogError(f"gate log file {name} sha256 differs from run.json")
    doc = json.loads((out / JSON_FILE).read_text(encoding="utf-8"))
    if doc.get("format") != GATE_LOG_FORMAT or doc.get("gate_opening_affects_computation") is not False or doc.get("statement") != STATEMENT:
        raise GateLogError("gate_dispatch.json lacks the log-only statement")
    rows = list(csv.DictReader(io.StringIO((out / CSV_FILE).read_text(encoding="utf-8-sig"))))
    if len(rows) != len(doc["records"]) or [r["record_id"] for r in rows] != [r["record_id"] for r in doc["records"]]:
        raise GateLogError("gate_dispatch.csv rows differ from gate_dispatch.json records")
    if any(r["record_kind"] not in ("MODEL", "EXTERNAL_REPORT") for r in rows):
        raise GateLogError("gate_dispatch.csv has a record without MODEL / EXTERNAL_REPORT kind")
    if any(r["record_kind"] == "EXTERNAL_REPORT" and r["applied_in_model"] != "False" for r in rows):
        raise GateLogError("an EXTERNAL_REPORT row claims to be applied in the model")
    hourly = list(csv.DictReader(io.StringIO((out / HOURLY_FILE).read_text(encoding="utf-8-sig"))))
    expected = len(run["time_s"]) * len(doc["boundary_gates"])
    if len(hourly) != expected:
        raise GateLogError(f"gate_discharge_hourly.csv has {len(hourly)} rows, expected {expected}")
    if run.get("legacy_optional_artifacts", {}).get(CSV_FILE, {}).get("status") != block["status"]:
        raise GateLogError("run.json legacy_optional_artifacts does not reference the gate log")
    return {"status": "PASS", "records": len(rows), "hourly_rows": len(hourly), "counts": doc["counts"]}


def apply_to_exported_bundle(bundle_dir: str | Path, options: Any, *, forcing_override: str | Path | None = None,
                             log: Any = None) -> dict[str, Any]:
    """Append the gate log to an exported bundle in place (no-clobber); updates run/summary/parse_summary and checksums."""
    from .large_run_export import _json_bytes, _write_checksums, verify_large_run_bundle
    log = log or (lambda message: None)
    out = Path(bundle_dir).resolve()
    run_path = out / "run.json"
    if not run_path.is_file() or not (out / "summary.json").is_file():
        raise GateLogError(f"not an exported large-run bundle: {out}")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if run.get("gate_dispatch") or any((out / name).exists() for name in GATE_LOG_FILES):
        raise GateLogError("bundle already has a gate dispatch log; no-clobber refused")
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    built = build_gate_log(options, run_id=run["run_id"], case_id=run["case_id"], time_origin=run["time_origin"],
                           times_s=[float(t) for t in run["time_s"]], group_series=summary.get("boundary_group_ledger_series") or {},
                           solver=run.get("solver") or {}, source_hashes=run.get("source_hashes") or {}, forcing_override=forcing_override)
    block = write_gate_log(out, built)
    block["applied"] = "post_hoc"
    run["gate_dispatch"] = block
    run.setdefault("legacy_optional_artifacts", {}).update(legacy_entries(block))
    run_path.write_bytes(_json_bytes(run))
    for name in ("summary.json", "parse_summary.json"):
        path = out / name
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["gate_dispatch"] = {"status": block["status"], "statement": STATEMENT, "counts": block["counts"]}
        path.write_bytes(_json_bytes(doc))
    (out / "checksums.json").unlink()
    _write_checksums(out, run["run_id"])
    log(f"gate log: {block['counts']}")
    return {"status": "PASS", "bundle": str(out), "gate_dispatch": block, "verify": verify_large_run_bundle(out)}


def main(argv: list[str] | None = None) -> int:
    from .large_run_profile import GateLogOptions
    parser = argparse.ArgumentParser(prog="python -m hydrodynamics.large_run_gate_log",
                                     description="Append the gate dispatch LOG (log only; not part of the computation) to an exported large-run bundle.")
    parser.add_argument("bundle_dir")
    parser.add_argument("--gate", action="append", required=True, metavar="GROUP=GATE_ID",
                        help=f"boundary group -> gate_id (repeatable); gate ids: {', '.join(GATE_IDS)}")
    parser.add_argument("--ledger", help="dispatch ledger workbook (.xlsx); omitted = MODEL rows only")
    parser.add_argument("--actions-sheet", default="调度动作")
    parser.add_argument("--inventory-sheet", default="闸门台账")
    parser.add_argument("--utc-offset", default="+08:00")
    parser.add_argument("--min-change", type=float, default=1.0, help="m3/s; smaller prescribed steps are not logged")
    parser.add_argument("--forcing", help="forcing CSV when the run's recorded path moved (sha256 must still match)")
    args = parser.parse_args(argv)
    try:
        gates = {}
        for item in args.gate:
            group, sep, gate = item.partition("=")
            if not sep or not group or not gate:
                raise GateLogError(f"--gate {item!r} must be GROUP=GATE_ID")
            gates[group] = gate
        options = GateLogOptions(ledger_xlsx=args.ledger, actions_sheet=args.actions_sheet, inventory_sheet=args.inventory_sheet,
                                 ledger_utc_offset=args.utc_offset, boundary_gates=gates, prescribed_change_min_m3_s=args.min_change)
        report = apply_to_exported_bundle(args.bundle_dir, options, forcing_override=args.forcing,
                                          log=lambda m: print(m, file=sys.stderr, flush=True))
        print(json.dumps({k: report[k] for k in ("status", "bundle", "gate_dispatch")}, ensure_ascii=False))
    except (GateLogError, ValueError, OSError) as exc:
        print(json.dumps({"status": "failed", "type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
