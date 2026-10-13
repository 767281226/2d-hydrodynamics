"""Rule evaluation and gate-operation journals, independent of solver physics."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from .config import Number, Text


RULE_SOURCE = "docs/dispatchRule.docx"
RULE_SOURCE_SHA256 = "849f1d6c1579355dd129badcc1ae78816652e744cf6cb81a537837b9cbfa120b"


class _AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class GateWaterLevel(_AuditModel):
    value_m: Number
    vertical_datum: Text
    unit: Literal["m"] = "m"


class GateDispatchOptions(_AuditModel):
    near_5_75_tolerance_m: Number | None = Field(default=None, ge=0)
    qingnan_threshold_datum: Text | None = None


class GateDispatchSnapshot(_AuditModel):
    time_s: Number = Field(ge=0)
    source: Text
    sixth_port_level: GateWaterLevel | None = None
    sixth_port_forecast_rising: StrictBool | None = None
    zaolinzhuang_discharge_m3s: Number | None = Field(default=None, ge=0)
    zhongting_discharge_m3s: Number | None = Field(default=None, ge=0)
    xihe_diversion_suitable: StrictBool | None = None
    dongdian_level: GateWaterLevel | None = None
    daqing_river_level: GateWaterLevel | None = None
    qingbei_level: GateWaterLevel | None = None
    qingbei_rising: StrictBool | None = None
    qingbei_fully_used: StrictBool | None = None
    dongdian_sixth_port_level: GateWaterLevel | None = None
    dongdian_sixth_port_rising: StrictBool | None = None
    tidal_gate_level: GateWaterLevel | None = None
    main_flood_season: StrictBool | None = None
    tidal_release_window: StrictBool | None = None
    tidal_release_authorized: StrictBool | None = None


@dataclass(frozen=True)
class GateRule:
    rule_id: str
    gate_id: str
    gate_name: str
    source_para_index_0based: int
    condition: str
    action: str


GATE_RULES = (
    GateRule("GDR-001", "duliu_inlet", "独流减河进洪闸", 2,
             "大清河第六埠水位达到4.2 m（大沽高程）", "充分下泄"),
    GateRule("GDR-002", "duliu_inlet", "独流减河进洪闸", 3,
             "第六埠水位接近5.75 m（大沽高程）且预报仍将上涨", "充分泄水"),
    GateRule("GDR-003", "xihe", "西河闸", 3,
             "第六埠水位接近5.75 m且预报仍将上涨，人工判断适宜分泄", "相机分泄"),
    GateRule("GDR-004", "renzhuangzi", "任庄子口门", 7,
             "枣林庄下泄洪水大于700 m3/s", "提请海委督促河北省按规定利用口门分洪行洪"),
    GateRule("GDR-005", "xihe", "西河闸", 10,
             "结合市区排沥及来水水质判断分泄；400 m3/s为河道下泄背景值", "相机分泄"),
    *(GateRule(f"GDR-{i:03d}", gate_id, name, 14,
               "东淀水位高于同一垂向基准的大清河河道水位", "适时开启，将东淀洪水导入下游河道")
      for i, gate_id, name in (
          (6, "laolongwan", "老龙湾退洪闸"), (7, "sixth_port_return", "第六埠退洪闸"),
          (8, "shuigaozhuang", "水高庄退洪闸"), (9, "yangliuqing", "杨柳青退洪闸"))),
    GateRule("GDR-010", "qingnan", "清南分洪闸", 6,
             "清北区域水位达到7.0 m且持续上涨（阈值基准待确认）", "向大清河以南东淀区域分洪"),
    GateRule("GDR-011", "qingnan", "清南分洪闸", 15,
             "清北已充分运用，东淀第六埠水位达到7.0 m且持续上涨（阈值基准待确认）",
             "向大清河以南东淀区域分洪"),
    GateRule("GDR-012", "duliu_tidal", "独流减河防潮闸", 25,
             "主汛期闸上水位超过3.0 m（大沽冻结），获安排且处于赶潮窗口", "赶潮提闸泄水"),
    GateRule("GDR-013", "duliu_tidal", "独流减河防潮闸", 25,
             "其它时段闸上水位超过3.8 m（大沽冻结），获安排且处于赶潮窗口", "赶潮提闸泄水"),
)
_RULE_BY_ID = {r.rule_id: r for r in GATE_RULES}
_GATE_NAMES = {r.gate_id: r.gate_name for r in GATE_RULES}


class GateOperation(_AuditModel):
    """An operation reported by the caller, with its original execution source."""

    time_s: Number = Field(ge=0)
    gate_id: Text
    action: Text
    reason: Text
    source: Text
    rule_id: Text | None = None
    approval_ref: Text | None = None
    opening_before_ratio: Number | None = Field(default=None, ge=0, le=1)
    opening_after_ratio: Number | None = Field(default=None, ge=0, le=1)
    discharge_before_m3s: Number | None = Field(default=None, ge=0)
    discharge_after_m3s: Number | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_gate_and_rule(self):
        if self.gate_id not in _GATE_NAMES:
            raise ValueError(f"gate_id must be one of {', '.join(_GATE_NAMES)}")
        if self.rule_id is not None:
            rule = _RULE_BY_ID.get(self.rule_id)
            if rule is None or rule.gate_id != self.gate_id:
                raise ValueError("rule_id must identify a rule for the same gate_id")
        return self


class GateDispatchInput(_AuditModel):
    schema_version: Literal["1.0"] = "1.0"
    time_origin: Text | None = None
    provenance: Text = "未提供站点调度输入或实际调闸记录"
    options: GateDispatchOptions = Field(default_factory=GateDispatchOptions)
    snapshots: tuple[GateDispatchSnapshot, ...] = ()
    operations: tuple[GateOperation, ...] = ()

    @field_validator("time_origin")
    @classmethod
    def timezone_required(cls, value):
        if value is not None:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("time_origin must be an ISO8601 timestamp with timezone") from exc
            if parsed.utcoffset() is None:
                raise ValueError("time_origin must include a timezone")
        return value

    @model_validator(mode="after")
    def ordered_inputs(self):
        if any(b.time_s <= a.time_s for a, b in zip(self.snapshots, self.snapshots[1:])):
            raise ValueError("snapshots.time_s must be strictly increasing and unique")
        if any(b.time_s < a.time_s for a, b in zip(self.operations, self.operations[1:])):
            raise ValueError("operations.time_s must be non-decreasing")
        keys = [(op.time_s, op.gate_id) for op in self.operations]
        if len(set(keys)) != len(keys):
            raise ValueError("operations cannot repeat the same time_s and gate_id")
        return self


@dataclass(frozen=True)
class RuleEvaluation:
    rule_id: str
    status: str
    reason: str
    missing: tuple[str, ...]
    evidence: dict


def _evaluate(rule: GateRule, snapshot: GateDispatchSnapshot, options: GateDispatchOptions) -> RuleEvaluation:
    checks: list[bool | None] = []
    missing: list[str] = []
    evidence: dict = {"source": snapshot.source, "condition": rule.condition}

    def require(name: str):
        value = getattr(snapshot, name)
        evidence[name] = value.model_dump() if isinstance(value, BaseModel) else value
        if value is None:
            missing.append(name)
        return value

    def flag(name: str, expected: bool = True):
        value = require(name)
        checks.append(None if value is None else value is expected)

    def level(name: str, datum: str | None):
        value = require(name)
        if datum is None:
            missing.append("options.qingnan_threshold_datum")
            return None
        if value is None:
            return None
        if value.vertical_datum != datum:
            missing.append(f"{name}.vertical_datum: {value.vertical_datum} != {datum}")
            return None
        return value.value_m

    rid = rule.rule_id
    if rid in {"GDR-001", "GDR-002", "GDR-003"}:
        value = level("sixth_port_level", "大沽高程")
        if rid == "GDR-001":
            checks.append(None if value is None else value >= 4.2)
        else:
            tolerance = options.near_5_75_tolerance_m
            evidence["near_5_75_tolerance_m"] = tolerance
            if tolerance is None:
                missing.append("options.near_5_75_tolerance_m")
            checks.append(None if value is None or tolerance is None else abs(value - 5.75) <= tolerance)
            flag("sixth_port_forecast_rising")
            if rid == "GDR-003":
                flag("xihe_diversion_suitable")
    elif rid == "GDR-004":
        value = require("zaolinzhuang_discharge_m3s")
        checks.append(None if value is None else value > 700.0)
    elif rid == "GDR-005":
        # The <400 clause describes river drainage, not a XiHe opening threshold.
        evidence["zhongting_discharge_m3s"] = snapshot.zhongting_discharge_m3s
        flag("xihe_diversion_suitable")
    elif rid in {"GDR-006", "GDR-007", "GDR-008", "GDR-009"}:
        basin = require("dongdian_level")
        river = require("daqing_river_level")
        if basin is not None and river is not None and basin.vertical_datum != river.vertical_datum:
            missing.append("dongdian_level / daqing_river_level vertical_datum mismatch")
            checks.append(None)
        else:
            checks.append(None if basin is None or river is None else basin.value_m > river.value_m)
    elif rid in {"GDR-010", "GDR-011"}:
        evidence["qingnan_threshold_datum"] = options.qingnan_threshold_datum
        name = "qingbei_level" if rid == "GDR-010" else "dongdian_sixth_port_level"
        value = level(name, options.qingnan_threshold_datum)
        checks.append(None if value is None else value >= 7.0)
        flag("qingbei_rising" if rid == "GDR-010" else "dongdian_sixth_port_rising")
        if rid == "GDR-011":
            flag("qingbei_fully_used")
    else:
        flag("main_flood_season", expected=rid == "GDR-012")
        value = level("tidal_gate_level", "大沽冻结")
        threshold = 3.0 if rid == "GDR-012" else 3.8
        checks.append(None if value is None else value > threshold)
        flag("tidal_release_window")
        flag("tidal_release_authorized")

    status = "not_triggered" if False in checks else ("indeterminate" if None in checks else "triggered")
    reason = rule.condition
    if missing:
        reason += "；缺少或不匹配：" + "; ".join(missing)
    return RuleEvaluation(rid, status, reason, tuple(missing), evidence)


@dataclass(frozen=True)
class GateDispatchLog:
    times_s: tuple[float, ...]
    inputs: GateDispatchInput
    records: tuple[dict, ...]
    final_evaluations: tuple[RuleEvaluation, ...]
    indeterminate_sample_counts: dict[str, int]
    result_context: dict = field(default_factory=dict)

    def to_dict(self, result_path: str | Path) -> dict:
        return {
            "schema_version": "1.0", "result_file": str(Path(result_path).resolve()),
            "rule_source": RULE_SOURCE, "rule_source_sha256": RULE_SOURCE_SHA256,
            "time_unit": "s", "time_origin": self.inputs.time_origin,
            "provenance": self.inputs.provenance,
            "window_start_s": self.times_s[0], "window_end_s": self.times_s[-1],
            "output_sample_count": len(self.times_s), "provided_snapshot_count": len(self.inputs.snapshots),
            "trigger_count": sum(r["record_type"] == "trigger" for r in self.records),
            "reported_operation_count": len(self.inputs.operations),
            "indeterminate_sample_counts": self.indeterminate_sample_counts,
            "options": self.inputs.options.model_dump(), "result_context": self.result_context,
            "rules": [asdict(rule) for rule in GATE_RULES],
            "final_evaluations": [asdict(item) for item in self.final_evaluations],
            "records": self.records,
            "note": "规则触发记录供调度判断；operation仅转录调用方提供的调闸记录及来源。记录器不改变闸门或计算边界。",
        }


def build_gate_dispatch_log(
    times_s: Sequence[float], inputs: GateDispatchInput | None = None, *,
    result_context: dict | None = None, time_origin: str | None = None,
) -> GateDispatchLog:
    inputs = GateDispatchInput() if inputs is None else inputs
    if not isinstance(inputs, GateDispatchInput):
        raise TypeError("gate_dispatch must be GateDispatchInput or None")
    if inputs.time_origin is None and time_origin is not None:
        inputs = GateDispatchInput.model_validate({**inputs.model_dump(), "time_origin": time_origin})
    # Reuse the typed time validator, including bool/nonfinite rejection.
    times = tuple(GateDispatchSnapshot(time_s=t, source="time validation").time_s for t in times_s)
    if not times or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("output times_s must be non-empty and strictly increasing")
    time_set = set(times)
    if any(s.time_s not in time_set for s in inputs.snapshots):
        raise ValueError("snapshots.time_s must match an output time exactly; no interpolation is performed")
    if any(op.time_s < times[0] or op.time_s > times[-1] for op in inputs.operations):
        raise ValueError("operations.time_s must lie inside the output time window")
    snapshots = {s.time_s: s for s in inputs.snapshots}
    epoch = datetime.fromisoformat(inputs.time_origin) if inputs.time_origin else None

    def record(time_s: float, rule: GateRule | None, evaluation: RuleEvaluation | None, **extra):
        return {
            "time_s": time_s, "occurred_at": (epoch + timedelta(seconds=time_s)).isoformat() if epoch else "",
            "gate_id": rule.gate_id if rule else "", "gate_name": rule.gate_name if rule else "",
            "rule_id": rule.rule_id if rule else "", "rule_status": evaluation.status if evaluation else "not_linked",
            "record_type": "", "action": "", "reason": evaluation.reason if evaluation else "",
            "opening_before_ratio": None, "opening_after_ratio": None,
            "discharge_before_m3s": None, "discharge_after_m3s": None,
            "operation_source": "", "approval_ref": "",
            "snapshot_source": evaluation.evidence["source"] if evaluation else "",
            "evidence": evaluation.evidence if evaluation else {}, "source_document": RULE_SOURCE,
            "source_para_index_0based": rule.source_para_index_0based if rule else None, **extra,
        }

    previous: dict[str, tuple] = {}
    records: list[dict] = []
    unknown_counts = {rule.rule_id: 0 for rule in GATE_RULES}
    finals = ()
    for time_s in times:
        snapshot = snapshots.get(time_s) or GateDispatchSnapshot(time_s=time_s, source="not_supplied")
        finals = tuple(_evaluate(rule, snapshot, inputs.options) for rule in GATE_RULES)
        for rule, evaluation in zip(GATE_RULES, finals):
            if evaluation.status == "indeterminate":
                unknown_counts[rule.rule_id] += 1
            signature = (evaluation.status, evaluation.missing)
            old = previous.get(rule.rule_id)
            if signature != old:
                kind = ("trigger" if evaluation.status == "triggered" else
                        "unavailable" if evaluation.status == "indeterminate" else
                        "condition_cleared" if old and old[0] == "triggered" else "condition_state")
                records.append(record(time_s, rule, evaluation, record_type=kind,
                                      action=rule.action if kind == "trigger" else ""))
                previous[rule.rule_id] = signature
    for op in inputs.operations:
        rule = _RULE_BY_ID.get(op.rule_id)
        snapshot = snapshots.get(op.time_s) or GateDispatchSnapshot(time_s=op.time_s, source="not_supplied")
        evaluation = _evaluate(rule, snapshot, inputs.options) if rule else None
        records.append(record(
            op.time_s, rule, evaluation, gate_id=op.gate_id, gate_name=_GATE_NAMES[op.gate_id],
            record_type="operation", action=op.action, reason=op.reason,
            opening_before_ratio=op.opening_before_ratio, opening_after_ratio=op.opening_after_ratio,
            discharge_before_m3s=op.discharge_before_m3s, discharge_after_m3s=op.discharge_after_m3s,
            operation_source=op.source, approval_ref=op.approval_ref or "",
        ))
    records.sort(key=lambda r: (r["time_s"], r["record_type"] == "operation", r["gate_id"], r["rule_id"]))
    records = [dict(event_id=i, **item) for i, item in enumerate(records, 1)]
    return GateDispatchLog(times, inputs, tuple(records), finals, unknown_counts, result_context or {})


def gate_dispatch_paths(result_path: str | Path) -> tuple[Path, Path]:
    result = Path(result_path)
    return (result.with_name(result.stem + "_gate_dispatch.csv"),
            result.with_name(result.stem + "_gate_dispatch.json"))


def check_gate_dispatch_paths(result_path: str | Path, *, overwrite: bool) -> None:
    for path in gate_dispatch_paths(result_path):
        if path.exists() and not path.is_file():
            raise IsADirectoryError(f"gate dispatch output must be a file: {path}")
        if path.exists() and not overwrite:
            raise FileExistsError(f"gate dispatch output already exists: {path}")


def write_gate_dispatch_log(
    result_path: str | Path, log: GateDispatchLog, *, overwrite: bool = False,
) -> tuple[Path, Path]:
    check_gate_dispatch_paths(result_path, overwrite=overwrite)
    csv_path, json_path = gate_dispatch_paths(result_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(log.records[0])
    columns[columns.index("evidence")] = "evidence_json"
    mode = "w" if overwrite else "x"
    with csv_path.open(mode, encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for record in log.records:
            row = dict(record)
            row["evidence_json"] = json.dumps(row.pop("evidence"), ensure_ascii=False, allow_nan=False)
            writer.writerow(row)
    with json_path.open(mode, encoding="utf-8") as stream:
        json.dump(log.to_dict(result_path), stream, ensure_ascii=False, indent=2, allow_nan=False)
    return csv_path, json_path


def export_gate_dispatch_for_netcdf(
    result_path: str | Path, inputs: GateDispatchInput | None = None, *, overwrite: bool = False,
) -> tuple[Path, Path]:
    """Add companion journals to an existing result, reading only times and attributes."""
    import netCDF4

    with netCDF4.Dataset(str(result_path), "r") as dataset:
        time = dataset.variables.get("time")
        if time is None or time.ndim != 1:
            raise ValueError("result NetCDF must contain a one-dimensional time variable")
        units = getattr(time, "units", None)
        known_format = getattr(dataset, "format", None) == "2d-hydrodynamics-netcdf"
        if units not in {"s", "seconds"} and not (units is None and known_format):
            raise ValueError("result time.units must be s/seconds (legacy project results may omit it)")
        values = time[:]
        if hasattr(values, "mask") and bool(values.mask.any()):
            raise ValueError("result time contains missing values")
        times = values.tolist()
        context = {key: str(dataset.getncattr(key)) for key in (
            "case", "event_start", "activation_scope", "vertical_datum", "inflow_note", "outflow_note",
        ) if key in dataset.ncattrs()}
        origin = getattr(dataset, "gate_dispatch_time_origin", None)
        if not origin and times and times[0] == 0:
            origin = getattr(dataset, "event_start", None)
    if inputs is None:
        inputs = GateDispatchInput(time_origin=origin)
    elif inputs.time_origin is None and origin:
        inputs = GateDispatchInput.model_validate({**inputs.model_dump(), "time_origin": origin})
    log = build_gate_dispatch_log(times, inputs, result_context=context)
    return write_gate_dispatch_log(result_path, log, overwrite=overwrite)


__all__ = [
    "GATE_RULES", "GateDispatchInput", "GateDispatchLog", "GateDispatchOptions",
    "GateDispatchSnapshot", "GateOperation", "GateWaterLevel", "build_gate_dispatch_log",
    "export_gate_dispatch_for_netcdf", "gate_dispatch_paths", "write_gate_dispatch_log",
]
