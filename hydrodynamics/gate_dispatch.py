"""Rule evaluation and gate-operation journals, independent of solver physics."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from .config import Number, Text
from .external_evidence import build_confirmed_datum_conversion_evidence


RULE_SOURCE = "docs/dispatchRule.docx"
RULE_SOURCE_SHA256 = "849f1d6c1579355dd129badcc1ae78816652e744cf6cb81a537837b9cbfa120b"


class _AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class GateWaterLevel(_AuditModel):
    value_m: Number
    vertical_datum: Text
    unit: Literal["m"] = "m"


class GateDatumConversionEvidence(_AuditModel):
    """The existing scoped datum relation, retained as evidence and never applied here."""

    source_datum: Text
    target_datum: Text
    scope: Text
    direction: Text
    offset_m: Number
    source_description: Text
    evidence_ref: Text
    applied: Literal[False] = False
    provenance: Text = "user-confirmed project evidence"

    @model_validator(mode="after")
    def only_known_scoped_relation(self):
        normalized = self.direction.casefold().replace(" ", "")
        if (self.source_datum != "大沽高程" or self.target_datum != "1985国家高程基准"
                or "东淀" not in self.scope or "文安洼" not in self.scope
                or normalized not in {"target=source-1.56m", "target=source−1.56m"}
                or self.offset_m != -1.56):
            raise ValueError("datum evidence must preserve the scoped target = source - 1.56 m relation for 东淀-文安洼")
        return self


class GateDispatchOptions(_AuditModel):
    near_5_75_tolerance_m: Number | None = Field(default=None, ge=0)
    near_5_75_tolerance_source: Text = "caller_supplied"
    qingnan_threshold_datum: Text | None = None
    qingnan_threshold_datum_source: Text = "caller_supplied"
    snapshot_time_mode: Literal["output_aligned", "independent_observations"] = "output_aligned"


class GateDispatchSnapshot(_AuditModel):
    time_s: Number = Field(ge=0)
    source: Text
    source_category: Literal["model_prescribed_change", "reconstruction", "external_report", "undeclared"] = "undeclared"
    sixth_port_level: GateWaterLevel | None = None
    sixth_port_forecast_rising: StrictBool | None = None
    main_channel_discharge_m3s: Number | None = Field(default=None, ge=0)
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
    tidal_release_window_reference: Text | None = None
    tidal_release_authorization_reference: Text | None = None


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

SOURCE_CATEGORIES = ("model_prescribed_change", "reconstruction", "external_report", "undeclared")
RULE_COVERAGE_MANIFEST = {
    "source_paragraph_count": 29,
    "automated_rule_ids": [rule.rule_id for rule in GATE_RULES],
    "automation_scope": "only the 13 explicitly mapped gate/sluice rule summaries; conditions remain audit evaluations",
    "manual_review_only": [
        {"source_paragraph_number": 5, "subject": "台头镇西破埝及启用条件", "automatic_action": False},
        {"source_paragraph_number": 6, "subject": "提请市防指使用清北", "automatic_action": False},
        {"source_paragraph_numbers": [13, 14], "subject": "启用准备、转移及新盖房泄洪后的区域启用", "automatic_action": False},
        {"source_paragraph_number": 18, "subject": "提请使用文安洼", "automatic_action": False},
        {"source_paragraph_number": 19, "subject": "提请使用贾口洼", "automatic_action": False},
        {"source_paragraph_number": 20, "subject": "经批准的应急破堤导洪", "automatic_action": False, "breach_inference": False},
        {"source_paragraph_numbers": [23, 24], "subject": "北槽/北大港分洪及调蓄工程阶段条件", "automatic_action": False},
        {"source_paragraph_number": 28, "subject": "南水北调东线调蓄工程完成前后方案", "automatic_action": False},
    ],
    "not_inferred": ["区域启用", "年度方案状态", "政府批准", "堤防越流", "破堤", "闸门开度", "实际执行"],
}
APPENDIX_DATUM_CONTEXT = [
    {
        "source_document": RULE_SOURCE,
        "source_document_sha256": RULE_SOURCE_SHA256,
        "source_location": "embedded water-level-table footnote; station-specific applicability not confirmed",
        "relation": "大沽高程 = 黄海高程 + 1.5 m",
        "applied": False,
    },
    {
        "source_document": RULE_SOURCE,
        "source_document_sha256": RULE_SOURCE_SHA256,
        "source_location": "embedded water-level-table footnote; 2013 water-gauge datum survey note",
        "relation": "大沽高程 = 国家85 + 1.668 m",
        "applied": False,
    },
]

CSV_COLUMNS = (
    # V1.0 columns remain first and retain their existing names/order.
    "event_id", "time_s", "occurred_at", "gate_id", "gate_name", "rule_id", "rule_status",
    "record_type", "action", "reason", "opening_before_ratio", "opening_after_ratio",
    "discharge_before_m3s", "discharge_after_m3s", "operation_source", "approval_ref",
    "snapshot_source", "evidence_json", "source_document", "source_para_index_0based",
    # V1.1 additions are appended.
    "event_key", "operation_id", "snapshot_id", "evaluation_id", "snapshot_source_category",
    "operation_source_category", "water_condition_status", "action_stage", "conditions_json",
    "threshold_reference_json", "source_document_sha256", "source_para_number_1based", "consistency_id",
)


def _parse_aware_time_origin(value: str, name: str = "time_origin") -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO8601 timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO8601 timestamp with timezone") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def check_time_origin_consistency(input_origin: str | None, result_origin: str | None) -> str | None:
    """Compare aware origins by instant; return the input spelling when present."""
    input_time = _parse_aware_time_origin(input_origin, "dispatch.time_origin") if input_origin else None
    result_time = _parse_aware_time_origin(result_origin, "result.time_origin") if result_origin else None
    if input_time is not None and result_time is not None:
        if input_time.astimezone(timezone.utc) != result_time.astimezone(timezone.utc):
            raise ValueError("dispatch.time_origin and result.time_origin must represent the same instant")
        return input_origin
    return input_origin or result_origin


def _stable_id(prefix: str, value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _confirmed_scoped_datum_reference() -> dict:
    """Retain the existing user-confirmed relation without applying it to a rule."""
    return build_confirmed_datum_conversion_evidence().to_dict()


class GateOperation(_AuditModel):
    """An operation reported by the caller, with its original execution source."""

    time_s: Number = Field(ge=0)
    gate_id: Text
    action: Text
    reason: Text
    source: Text
    source_category: Literal["model_prescribed_change", "reconstruction", "external_report", "undeclared"] = "undeclared"
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
    datum_conversion_evidence: GateDatumConversionEvidence | None = None
    snapshots: tuple[GateDispatchSnapshot, ...] = ()
    operations: tuple[GateOperation, ...] = ()

    @field_validator("time_origin")
    @classmethod
    def timezone_required(cls, value):
        if value is not None:
            _parse_aware_time_origin(value, "time_origin")
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
    conditions: tuple[dict, ...] = ()
    water_condition_status: str = "indeterminate"
    action_stage: str = "evidence_incomplete"
    threshold_reference: dict = field(default_factory=dict)
    snapshot_id: str = ""
    evaluation_id: str = ""


def _threshold_reference(rule: GateRule, options: GateDispatchOptions) -> dict:
    rid = rule.rule_id
    reference = {
        "source_document": RULE_SOURCE,
        "source_document_sha256": RULE_SOURCE_SHA256,
        "source_paragraph_index_0based": rule.source_para_index_0based,
        "source_paragraph_number_1based": rule.source_para_index_0based + 1,
        "original_rule_text": rule.condition,
        "conversion_applied": False,
        "thresholds": [],
    }
    if rid == "GDR-001":
        reference["thresholds"] = [{"role": "comparison", "operator": ">=", "original_threshold": 4.2, "comparison_threshold": 4.2, "unit": "m", "datum": "大沽高程", "source": "source paragraph text"}]
    elif rid in {"GDR-002", "GDR-003"}:
        reference["thresholds"] = [{
            "role": "comparison", "operator": "within_absolute_tolerance", "original_threshold": 5.75,
            "comparison_threshold": 5.75, "tolerance_m": options.near_5_75_tolerance_m,
            "tolerance_interpretation_source": options.near_5_75_tolerance_source,
            "unit": "m", "datum": "大沽高程", "source": "source paragraph plus caller interpretation",
        }, {
            "role": "background_only", "operator": "context_only", "reference_value": 850.0,
            "comparison_threshold": None, "unit": "m3/s", "datum": None,
            "source": "source paragraph 4 main-channel flow context; not a gate target or trigger",
        }]
    elif rid == "GDR-004":
        reference["thresholds"] = [{"role": "comparison", "operator": ">", "original_threshold": 700.0, "comparison_threshold": 700.0, "unit": "m3/s", "datum": None, "source": "source paragraph text"}]
    elif rid == "GDR-005":
        reference["thresholds"] = [{"role": "background_only", "operator": "context_only", "original_threshold": 400.0, "comparison_threshold": None, "unit": "m3/s", "datum": None, "source": "source paragraph text; not an XiHe trigger"}]
    elif rid in {"GDR-006", "GDR-007", "GDR-008", "GDR-009"}:
        reference["thresholds"] = [{"role": "comparison", "operator": ">", "original_threshold": None, "comparison_threshold": None, "unit": "m", "datum": "both observations must share the same declared datum", "source": "source paragraph text"}]
    elif rid in {"GDR-010", "GDR-011"}:
        reference["thresholds"] = [{"role": "comparison", "operator": ">=", "original_threshold": 7.0, "comparison_threshold": 7.0, "unit": "m", "datum": options.qingnan_threshold_datum, "datum_source": options.qingnan_threshold_datum_source, "source": "source paragraph text; datum confirmation is caller supplied"}]
    elif rid == "GDR-012":
        reference["thresholds"] = [{"role": "comparison", "operator": ">", "original_threshold": 3.0, "comparison_threshold": 3.0, "unit": "m", "datum": "大沽冻结", "source": "source paragraph text"}]
    else:
        reference["thresholds"] = [{"role": "comparison", "operator": ">", "original_threshold": 3.8, "comparison_threshold": 3.8, "unit": "m", "datum": "大沽冻结", "source": "source paragraph text"}]
    return reference


def _evaluate(rule: GateRule, snapshot: GateDispatchSnapshot, options: GateDispatchOptions) -> RuleEvaluation:
    checks: list[bool | None] = []
    hydro_checks: list[bool | None] = []
    missing: list[str] = []
    conditions: list[dict] = []
    evidence: dict = {
        "source": snapshot.source,
        "source_category": snapshot.source_category,
        "condition": rule.condition,
    }

    def require(name: str):
        value = getattr(snapshot, name)
        evidence[name] = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        if value is None:
            missing.append(name)
        return value

    def add_condition(
        *, condition_id: str, source_field: str, observed: object, operator: str,
        threshold: object = None, unit: str | None = None, datum: str | None = None,
        passed: bool | None, role: str = "hydrological", detail: str | None = None,
    ) -> bool | None:
        status = "indeterminate" if passed is None else ("met" if passed else "not_met")
        item = {
            "condition_id": condition_id,
            "source_field": source_field,
            "observed": observed.model_dump(mode="json") if isinstance(observed, BaseModel) else observed,
            "operator": operator,
            "threshold": threshold,
            "unit": unit,
            "datum": datum,
            "role": role,
            "status": status,
            "detail": detail,
        }
        conditions.append(item)
        checks.append(passed)
        if role == "hydrological":
            hydro_checks.append(passed)
        return passed

    def flag(name: str, expected: bool = True, *, role: str = "hydrological"):
        value = require(name)
        add_condition(
            condition_id=f"{rule.rule_id}-C{len(conditions) + 1}", source_field=name,
            observed=value, operator="is", threshold=expected, unit=None, datum=None,
            passed=None if value is None else value is expected, role=role,
        )

    def level(name: str, datum: str | None):
        value = require(name)
        if datum is None:
            missing.append("options.qingnan_threshold_datum")
            add_condition(
                condition_id=f"{rule.rule_id}-C{len(conditions) + 1}", source_field=name,
                observed=value, operator="datum_confirmed", threshold=None, unit="m", datum=None,
                passed=None, detail="comparison datum is not confirmed",
            )
            return None
        if value is None:
            add_condition(
                condition_id=f"{rule.rule_id}-C{len(conditions) + 1}", source_field=name,
                observed=None, operator="datum_match", threshold=datum, unit="m", datum=datum,
                passed=None,
            )
            return None
        if value.vertical_datum != datum:
            missing.append(f"{name}.vertical_datum: {value.vertical_datum} != {datum}")
            add_condition(
                condition_id=f"{rule.rule_id}-C{len(conditions) + 1}", source_field=name,
                observed=value, operator="datum_match", threshold=datum, unit="m", datum=datum,
                passed=None, detail="no datum conversion is applied",
            )
            return None
        return value.value_m

    rid = rule.rule_id
    if rid in {"GDR-001", "GDR-002", "GDR-003"}:
        value = level("sixth_port_level", "大沽高程")
        if rid == "GDR-001":
            observed = snapshot.sixth_port_level
            add_condition(condition_id=f"{rid}-C{len(conditions) + 1}", source_field="sixth_port_level", observed=observed,
                          operator=">=", threshold=4.2, unit="m", datum="大沽高程", passed=None if value is None else value >= 4.2)
        else:
            tolerance = options.near_5_75_tolerance_m
            evidence["near_5_75_tolerance_m"] = tolerance
            evidence["near_5_75_tolerance_source"] = options.near_5_75_tolerance_source
            if tolerance is None:
                missing.append("options.near_5_75_tolerance_m")
            add_condition(condition_id=f"{rid}-C{len(conditions) + 1}", source_field="sixth_port_level", observed=snapshot.sixth_port_level,
                          operator="within_absolute_tolerance", threshold=5.75, unit="m", datum="大沽高程",
                          passed=None if value is None or tolerance is None else abs(value - 5.75) <= tolerance,
                          detail=None if tolerance is not None else "near-5.75 tolerance was not supplied")
            flag("sixth_port_forecast_rising")
            conditions.append({
                "condition_id": f"{rid}-C{len(conditions) + 1}", "source_field": "main_channel_discharge_m3s",
                "observed": snapshot.main_channel_discharge_m3s, "operator": "context_only",
                "threshold": None, "reference_value": 850.0, "unit": "m3/s", "datum": None,
                "role": "background_context", "status": "context_only",
                "detail": "850 m3/s is main-channel context, not a gate target or trigger",
            })
            evidence["main_channel_discharge_m3s"] = snapshot.main_channel_discharge_m3s
            if rid == "GDR-003":
                flag("xihe_diversion_suitable")
    elif rid == "GDR-004":
        value = require("zaolinzhuang_discharge_m3s")
        add_condition(condition_id=f"{rid}-C{len(conditions) + 1}", source_field="zaolinzhuang_discharge_m3s", observed=value,
                      operator=">", threshold=700.0, unit="m3/s", datum=None, passed=None if value is None else value > 700.0)
    elif rid == "GDR-005":
        # The <400 clause describes river drainage, not a XiHe opening threshold.
        evidence["zhongting_discharge_m3s"] = snapshot.zhongting_discharge_m3s
        conditions.append({
            "condition_id": f"{rid}-C{len(conditions) + 1}", "source_field": "zhongting_discharge_m3s",
            "observed": snapshot.zhongting_discharge_m3s, "operator": "context_only",
            "threshold": 400.0, "unit": "m3/s", "datum": None, "role": "background_context",
            "status": "context_only", "detail": "400 m3/s does not automatically trigger XiHe diversion",
        })
        flag("xihe_diversion_suitable")
    elif rid in {"GDR-006", "GDR-007", "GDR-008", "GDR-009"}:
        basin = require("dongdian_level")
        river = require("daqing_river_level")
        if basin is not None and river is not None and basin.vertical_datum != river.vertical_datum:
            missing.append("dongdian_level / daqing_river_level vertical_datum mismatch")
            add_condition(condition_id=f"{rid}-C{len(conditions) + 1}", source_field="dongdian_level > daqing_river_level",
                          observed={"dongdian_level": basin.model_dump(mode="json"), "daqing_river_level": river.model_dump(mode="json")},
                          operator=">", threshold=None, unit="m", datum="same declared datum required", passed=None,
                          detail="datum mismatch; no conversion is applied")
        else:
            add_condition(condition_id=f"{rid}-C{len(conditions) + 1}", source_field="dongdian_level > daqing_river_level",
                          observed={"dongdian_level": None if basin is None else basin.model_dump(mode="json"), "daqing_river_level": None if river is None else river.model_dump(mode="json")},
                          operator=">", threshold=None, unit="m", datum=None if basin is None else basin.vertical_datum,
                          passed=None if basin is None or river is None else basin.value_m > river.value_m)
    elif rid in {"GDR-010", "GDR-011"}:
        evidence["qingnan_threshold_datum"] = options.qingnan_threshold_datum
        evidence["qingnan_threshold_datum_source"] = options.qingnan_threshold_datum_source
        name = "qingbei_level" if rid == "GDR-010" else "dongdian_sixth_port_level"
        value = level(name, options.qingnan_threshold_datum)
        add_condition(condition_id=f"{rid}-C{len(conditions) + 1}", source_field=name, observed=getattr(snapshot, name),
                      operator=">=", threshold=7.0, unit="m", datum=options.qingnan_threshold_datum,
                      passed=None if value is None else value >= 7.0,
                      detail="caller-confirmed threshold datum required" if options.qingnan_threshold_datum is None else None)
        flag("qingbei_rising" if rid == "GDR-010" else "dongdian_sixth_port_rising")
        if rid == "GDR-011":
            flag("qingbei_fully_used")
    else:
        flag("main_flood_season", expected=rid == "GDR-012", role="hydrological")
        value_record = require("tidal_gate_level")
        if value_record is None:
            value = None
        elif value_record.vertical_datum != "大沽冻结":
            missing.append(f"tidal_gate_level.vertical_datum: {value_record.vertical_datum} != 大沽冻结")
            value = None
        else:
            value = value_record.value_m
        threshold = 3.0 if rid == "GDR-012" else 3.8
        add_condition(condition_id=f"{rid}-C{len(conditions) + 1}", source_field="tidal_gate_level", observed=value_record,
                      operator=">", threshold=threshold, unit="m", datum="大沽冻结",
                      passed=None if value is None else value > threshold,
                      detail="no conversion from 大沽高程 or 国家85 is applied" if value_record is not None and value_record.vertical_datum != "大沽冻结" else None)
        flag("tidal_release_window", role="tide_window")
        flag("tidal_release_authorized", role="authorization")
        for field_name, flag_name, label in (
            ("tidal_release_authorization_reference", "tidal_release_authorized", "approval/arrangement reference"),
            ("tidal_release_window_reference", "tidal_release_window", "tide-window evidence reference"),
        ):
            reference = getattr(snapshot, field_name)
            evidence[field_name] = reference
            if reference is None and getattr(snapshot, flag_name) is True:
                missing.append(field_name)
            conditions.append({
                "condition_id": f"{rid}-C{len(conditions) + 1}",
                "source_field": field_name,
                "observed": reference,
                "operator": "evidence_reference_present",
                "threshold": "non-empty reference",
                "unit": None,
                "datum": None,
                "role": "operational_evidence",
                "status": "evidence_supplied" if reference is not None else "evidence_missing",
                "detail": label,
            })

    status = "not_triggered" if False in checks else ("indeterminate" if None in checks else "triggered")
    water_condition_status = "not_triggered" if False in hydro_checks else ("indeterminate" if None in hydro_checks else "triggered")
    reason = rule.condition
    if missing:
        reason += "；缺少或不匹配：" + "; ".join(missing)
    if rid in {"GDR-012", "GDR-013"} and water_condition_status == "triggered":
        if snapshot.tidal_release_authorized is None:
            action_stage = "request_arrangement"
        elif not snapshot.tidal_release_authorized:
            action_stage = "awaiting_arrangement"
        elif not snapshot.tidal_release_authorization_reference:
            action_stage = "awaiting_authorization_evidence"
        elif snapshot.tidal_release_window is None or not snapshot.tidal_release_window:
            action_stage = "awaiting_tidal_window"
        elif not snapshot.tidal_release_window_reference:
            action_stage = "awaiting_tidal_window_evidence"
        else:
            action_stage = "ready_for_manual_decision"
    elif water_condition_status == "indeterminate":
        action_stage = "evidence_incomplete"
    elif water_condition_status == "not_triggered":
        action_stage = "condition_not_met"
    elif status == "indeterminate":
        action_stage = "manual_condition_incomplete"
    elif status == "not_triggered":
        action_stage = "manual_condition_not_met"
    elif rid == "GDR-004":
        action_stage = "request_external_authority"
    elif rid in {"GDR-003", "GDR-005", "GDR-006", "GDR-007", "GDR-008", "GDR-009", "GDR-010", "GDR-011"}:
        action_stage = "manual_decision_required"
    else:
        action_stage = "awaiting_dispatch_decision"
    return RuleEvaluation(
        rid, status, reason, tuple(dict.fromkeys(missing)), evidence, tuple(conditions),
        water_condition_status, action_stage, _threshold_reference(rule, options),
    )


@dataclass(frozen=True)
class GateDispatchLog:
    times_s: tuple[float, ...]
    inputs: GateDispatchInput
    records: tuple[dict, ...]
    final_evaluations: tuple[RuleEvaluation, ...]
    indeterminate_sample_counts: dict[str, int]
    snapshots: tuple[dict, ...]
    evaluations: tuple[dict, ...]
    supplied_input_time_origin: str | None = None
    result_time_origin: str | None = None
    result_context: dict = field(default_factory=dict)

    def to_dict(self, result_path: str | Path, *, output_consistency: Mapping | None = None) -> dict:
        operation_categories = {category: 0 for category in SOURCE_CATEGORIES}
        snapshot_categories = {category: 0 for category in SOURCE_CATEGORIES}
        for operation in self.inputs.operations:
            operation_categories[operation.source_category] += 1
        for snapshot in self.inputs.snapshots:
            snapshot_categories[snapshot.source_category] += 1
        conversion = (
            self.inputs.datum_conversion_evidence.model_dump(mode="json")
            if self.inputs.datum_conversion_evidence is not None
            else _confirmed_scoped_datum_reference()
        )
        if self.supplied_input_time_origin and self.result_time_origin:
            origin_resolution = "same_instant"
        elif self.supplied_input_time_origin:
            origin_resolution = "input_origin_only"
        elif self.result_time_origin:
            origin_resolution = "inherited_from_result_origin"
        else:
            origin_resolution = "relative_time_only"
        return {
            "schema_version": "1.1", "input_schema_version": self.inputs.schema_version,
            "compatibility": {
                "previous_json_schema_version": "1.0",
                "v1_0_fields_preserved": True,
                "new_fields_appended_to_csv": True,
                "legacy_reported_operation_count_semantics": "count of supplied operation records, not a count of verified physical gate actions",
            },
            "result_file": str(Path(result_path).resolve()),
            "rule_source": RULE_SOURCE, "rule_source_sha256": RULE_SOURCE_SHA256,
            "time_unit": "s", "time_origin": self.inputs.time_origin,
            "time_origins": {
                "input_time_origin": self.supplied_input_time_origin,
                "result_time_origin": self.result_time_origin,
                "effective_time_origin": self.inputs.time_origin,
                "resolution": origin_resolution,
            },
            "provenance": self.inputs.provenance,
            "window_start_s": self.times_s[0], "window_end_s": self.times_s[-1],
            "output_sample_count": len(self.times_s), "provided_snapshot_count": len(self.inputs.snapshots),
            "snapshot_time_mode": self.inputs.options.snapshot_time_mode,
            "snapshot_count": len(self.snapshots), "evaluation_count": len(self.evaluations),
            "trigger_count": sum(r["record_type"] == "trigger" for r in self.records),
            "reported_operation_count": len(self.inputs.operations),
            "operation_source_category_counts": operation_categories,
            "snapshot_source_category_counts": snapshot_categories,
            "model_prescribed_change_count": operation_categories["model_prescribed_change"],
            "reconstruction_operation_count": operation_categories["reconstruction"],
            "external_reported_operation_count": operation_categories["external_report"],
            "undeclared_operation_source_count": operation_categories["undeclared"],
            "asserted_external_action_count": operation_categories["external_report"],
            "indeterminate_sample_counts": self.indeterminate_sample_counts,
            "options": self.inputs.options.model_dump(), "result_context": self.result_context,
            "rules": [asdict(rule) for rule in GATE_RULES],
            "final_evaluations": [asdict(item) for item in self.final_evaluations],
            "snapshots": self.snapshots,
            "evaluations": self.evaluations,
            "rule_coverage": RULE_COVERAGE_MANIFEST,
            "datum_reference_context": {
                "scoped_project_evidence": conversion,
                "scoped_project_evidence_applied": False,
                "appendix_notes": APPENDIX_DATUM_CONTEXT,
                "note": "rule comparison uses each rule's stated basis; no appendix offset is globally applied",
            },
            "output_consistency": output_consistency,
            "records": self.records,
            "note": "规则触发记录仅供人工调度判断；operation仅转录调用方提供的记录及结构化来源。模型预设流量变化不等于真实调闸。记录器不发命令、不改变闸门或计算边界。",
        }


def build_gate_dispatch_log(
    times_s: Sequence[float], inputs: GateDispatchInput | None = None, *,
    result_context: dict | None = None, time_origin: str | None = None,
) -> GateDispatchLog:
    inputs = GateDispatchInput() if inputs is None else inputs
    if not isinstance(inputs, GateDispatchInput):
        raise TypeError("gate_dispatch must be GateDispatchInput or None")
    supplied_input_origin = inputs.time_origin
    effective_origin = check_time_origin_consistency(inputs.time_origin, time_origin)
    if effective_origin != inputs.time_origin:
        inputs = GateDispatchInput.model_validate({**inputs.model_dump(), "time_origin": effective_origin})
    # Reuse the typed time validator, including bool/nonfinite rejection.
    times = tuple(GateDispatchSnapshot(time_s=t, source="time validation").time_s for t in times_s)
    if not times or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("output times_s must be non-empty and strictly increasing")
    time_set = set(times)
    if inputs.options.snapshot_time_mode == "output_aligned" and any(s.time_s not in time_set for s in inputs.snapshots):
        raise ValueError("snapshots.time_s must match an output time exactly in output_aligned mode; no interpolation is performed")
    if any(s.time_s < times[0] or s.time_s > times[-1] for s in inputs.snapshots):
        raise ValueError("snapshots.time_s must lie inside the output time window")
    if any(op.time_s < times[0] or op.time_s > times[-1] for op in inputs.operations):
        raise ValueError("operations.time_s must lie inside the output time window")
    supplied_snapshots = {snapshot.time_s: snapshot for snapshot in inputs.snapshots}
    snapshots_by_time: dict[float, dict] = {}
    evaluations_by_rule_time: dict[tuple[float, str], RuleEvaluation] = {}
    evaluations: list[dict] = []
    finals_by_rule: dict[str, RuleEvaluation] = {}
    unknown_counts = {rule.rule_id: 0 for rule in GATE_RULES}
    records: list[dict] = []
    previous: dict[str, tuple] = {}
    triggered_episode: dict[str, bool] = {rule.rule_id: False for rule in GATE_RULES}
    epoch = _parse_aware_time_origin(inputs.time_origin) if inputs.time_origin else None

    def get_snapshot(time_s: float) -> dict:
        existing = snapshots_by_time.get(time_s)
        if existing is not None:
            return existing
        selected = supplied_snapshots.get(time_s)
        provided = selected is not None
        if selected is None:
            selected = GateDispatchSnapshot(time_s=time_s, source="not_supplied", source_category="undeclared")
        snapshot_data = selected.model_dump(mode="json")
        snapshot_id = _stable_id("SNP", {"provided": provided, "snapshot": snapshot_data})
        entry = {"snapshot_id": snapshot_id, "provided": provided, **snapshot_data}
        snapshots_by_time[time_s] = entry
        return entry

    def evaluate_time(time_s: float, *, output_sample: bool, update_finals: bool = True) -> tuple[RuleEvaluation, ...]:
        entry = get_snapshot(time_s)
        snapshot = supplied_snapshots.get(time_s)
        if snapshot is None:
            snapshot = GateDispatchSnapshot(time_s=time_s, source="not_supplied", source_category="undeclared")
        current: list[RuleEvaluation] = []
        for rule in GATE_RULES:
            raw_evaluation = _evaluate(rule, snapshot, inputs.options)
            evaluation_data = {
                "snapshot_id": entry["snapshot_id"],
                "time_s": time_s,
                "rule_id": rule.rule_id,
                "evaluation": asdict(raw_evaluation),
            }
            evaluation_id = _stable_id("EVAL", evaluation_data)
            evaluation = RuleEvaluation(
                raw_evaluation.rule_id, raw_evaluation.status, raw_evaluation.reason,
                raw_evaluation.missing, raw_evaluation.evidence, raw_evaluation.conditions,
                raw_evaluation.water_condition_status, raw_evaluation.action_stage,
                raw_evaluation.threshold_reference, entry["snapshot_id"], evaluation_id,
            )
            current.append(evaluation)
            evaluations_by_rule_time[(time_s, rule.rule_id)] = evaluation
            if update_finals:
                finals_by_rule[rule.rule_id] = evaluation
            evaluations.append({
                "evaluation_id": evaluation_id,
                "snapshot_id": entry["snapshot_id"],
                "time_s": time_s,
                "rule_id": rule.rule_id,
                "rule_status": evaluation.status,
                "water_condition_status": evaluation.water_condition_status,
                "action_stage": evaluation.action_stage,
                "missing": list(evaluation.missing),
                "conditions": list(evaluation.conditions),
                "evidence": evaluation.evidence,
                "threshold_reference": evaluation.threshold_reference,
            })
            if output_sample and evaluation.status == "indeterminate":
                unknown_counts[rule.rule_id] += 1
        return tuple(current)

    def make_record(time_s: float, rule: GateRule | None, evaluation: RuleEvaluation | None,
                    snapshot_entry: dict | None, **extra) -> dict:
        return {
            "time_s": time_s,
            "occurred_at": (epoch + timedelta(seconds=time_s)).isoformat() if epoch else "",
            "gate_id": rule.gate_id if rule else "",
            "gate_name": rule.gate_name if rule else "",
            "rule_id": rule.rule_id if rule else "",
            "rule_status": evaluation.status if evaluation else ("indeterminate" if rule else "not_linked"),
            "record_type": "",
            "action": "",
            "reason": evaluation.reason if evaluation else "",
            "opening_before_ratio": None,
            "opening_after_ratio": None,
            "discharge_before_m3s": None,
            "discharge_after_m3s": None,
            "operation_source": "",
            "approval_ref": "",
            "snapshot_source": snapshot_entry["source"] if snapshot_entry else "",
            "evidence": evaluation.evidence if evaluation else {},
            "source_document": RULE_SOURCE,
            "source_para_index_0based": rule.source_para_index_0based if rule else None,
            "source_para_number_1based": rule.source_para_index_0based + 1 if rule else None,
            "source_document_sha256": RULE_SOURCE_SHA256,
            "snapshot_id": evaluation.snapshot_id if evaluation else (snapshot_entry["snapshot_id"] if snapshot_entry else ""),
            "evaluation_id": evaluation.evaluation_id if evaluation else "",
            "snapshot_source_category": snapshot_entry["source_category"] if snapshot_entry else "",
            "operation_source_category": "",
            "water_condition_status": evaluation.water_condition_status if evaluation else "indeterminate",
            "action_stage": evaluation.action_stage if evaluation else "evidence_incomplete",
            "conditions": list(evaluation.conditions) if evaluation else [],
            "threshold_reference": evaluation.threshold_reference if evaluation else (_threshold_reference(rule, inputs.options) if rule else {}),
            **extra,
        }

    evaluation_times = times if inputs.options.snapshot_time_mode == "output_aligned" else tuple(snapshot.time_s for snapshot in inputs.snapshots)
    for time_s in evaluation_times:
        # In aligned mode each result frame is a sample; in independent mode
        # each supplied observation time is a sample, even when no NC frame is
        # present at that exact instant.
        current = evaluate_time(time_s, output_sample=True)
        entry = snapshots_by_time[time_s]
        for rule, evaluation in zip(GATE_RULES, current):
            signature = (evaluation.status, evaluation.missing, evaluation.action_stage)
            old = previous.get(rule.rule_id)
            if signature != old:
                if evaluation.status == "triggered":
                    kind = "trigger" if old is None or old[0] != "triggered" else "condition_state"
                    triggered_episode[rule.rule_id] = True
                elif evaluation.status == "indeterminate":
                    kind = "unavailable"
                else:
                    kind = "condition_cleared" if triggered_episode[rule.rule_id] else "condition_state"
                    triggered_episode[rule.rule_id] = False
                records.append(make_record(
                    time_s, rule, evaluation, entry,
                    record_type=kind, action=rule.action if kind == "trigger" else "",
                ))
                previous[rule.rule_id] = signature

    for op in inputs.operations:
        rule = _RULE_BY_ID.get(op.rule_id)
        operation_id = _stable_id("OPR", op.model_dump(mode="json"))
        evaluation = evaluations_by_rule_time.get((op.time_s, op.rule_id)) if op.rule_id else None
        snapshot_entry = snapshots_by_time.get(op.time_s)
        if rule is not None and evaluation is None and inputs.options.snapshot_time_mode == "output_aligned":
            # Preserve V1.0's indeterminate operation linkage, but keep the
            # synthetic missing evidence explicit and never reuse an old sample.
            evaluate_time(op.time_s, output_sample=False, update_finals=False)
            snapshot_entry = snapshots_by_time[op.time_s]
            evaluation = evaluations_by_rule_time.get((op.time_s, op.rule_id))
        if op.source_category == "model_prescribed_change":
            operation_stage = "model_input_change_not_verified_gate_action"
        elif op.source_category == "external_report":
            operation_stage = "external_action_report_transcribed"
        elif op.source_category == "reconstruction":
            operation_stage = "reconstructed_record_not_verified_action"
        else:
            operation_stage = "operation_source_unclassified"
        records.append(make_record(
            op.time_s, rule, evaluation, snapshot_entry,
            gate_id=op.gate_id,
            gate_name=_GATE_NAMES[op.gate_id],
            operation_id=operation_id,
            record_type="operation",
            action=op.action,
            reason=op.reason,
            opening_before_ratio=op.opening_before_ratio,
            opening_after_ratio=op.opening_after_ratio,
            discharge_before_m3s=op.discharge_before_m3s,
            discharge_after_m3s=op.discharge_after_m3s,
            operation_source=op.source,
            operation_source_category=op.source_category,
            approval_ref=op.approval_ref or "",
            action_stage=operation_stage,
        ))
    records.sort(key=lambda r: (r["time_s"], r["record_type"] == "operation", r["gate_id"], r["rule_id"], r.get("operation_id", "")))
    finalized: list[dict] = []
    for event_id, item in enumerate(records, 1):
        stable_event = {key: value for key, value in item.items() if key not in {"event_id", "event_key"}}
        event_key = _stable_id("EVT", stable_event)
        finalized.append(dict(event_id=event_id, event_key=event_key, **item))
    snapshot_documents = tuple(snapshots_by_time[time_s] for time_s in sorted(snapshots_by_time))
    final_evaluations = tuple(finals_by_rule[rule.rule_id] for rule in GATE_RULES if rule.rule_id in finals_by_rule)
    return GateDispatchLog(
        times, inputs, tuple(finalized), final_evaluations, unknown_counts,
        snapshot_documents, tuple(evaluations), supplied_input_origin, time_origin, result_context or {},
    )


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
    csv_path, json_path = gate_dispatch_paths(result_path)
    json_payload = log.to_dict(result_path)
    consistency_id = _payload_consistency_id(json_payload)
    csv_text = io.StringIO(newline="")
    writer = csv.DictWriter(csv_text, fieldnames=CSV_COLUMNS, extrasaction="raise")
    writer.writeheader()
    for record in log.records:
        row = dict(record)
        row["evidence_json"] = json.dumps(row.pop("evidence"), ensure_ascii=False, allow_nan=False, sort_keys=True)
        row["conditions_json"] = json.dumps(row.pop("conditions"), ensure_ascii=False, allow_nan=False, sort_keys=True)
        row["threshold_reference_json"] = json.dumps(row.pop("threshold_reference"), ensure_ascii=False, allow_nan=False, sort_keys=True)
        row["consistency_id"] = consistency_id
        writer.writerow(row)
    csv_bytes = csv_text.getvalue().encode("utf-8-sig")
    manifest = {
        "consistency_id": consistency_id,
        "csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "csv_record_count": len(log.records),
        "csv_columns": list(CSV_COLUMNS),
        "pair_replacement": "staged outputs with rollback on replacement failure; two-file replacement is not a cross-file atomic transaction",
    }
    json_payload["output_consistency"] = manifest
    json_bytes = (json.dumps(json_payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    check_gate_dispatch_paths(result_path, overwrite=overwrite)
    _replace_gate_dispatch_pair((csv_path, json_path), (csv_bytes, json_bytes), overwrite=overwrite)
    return csv_path, json_path


class GateDispatchWriteError(OSError):
    """Raised when a companion pair fails to install or restore safely."""


def _payload_consistency_id(payload: Mapping) -> str:
    semantic_payload = dict(payload)
    semantic_payload.pop("output_consistency", None)
    raw = json.dumps(semantic_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _stage_bytes(directory: Path, prefix: str, content: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix + ".", suffix=".tmp", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _replace_gate_dispatch_pair(paths: tuple[Path, Path], contents: tuple[bytes, bytes], *, overwrite: bool) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    backups: dict[Path, Path | None] = {}
    installed: list[Path] = []
    recovery_backups: set[Path] = set()
    try:
        for path, content in zip(paths, contents):
            staged.append(_stage_bytes(path.parent, path.name, content))
        if overwrite:
            for path in paths:
                backups[path] = _stage_bytes(path.parent, path.name + ".backup", path.read_bytes()) if path.exists() else None
        for path, temporary in zip(paths, staged):
            if overwrite:
                os.replace(temporary, path)
            else:
                # On Windows rename fails rather than replacing an existing
                # target; on POSIX a hard link provides that no-clobber rule.
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
                temporary.unlink(missing_ok=True)
            installed.append(path)
    except Exception as exc:
        rollback_errors: list[str] = []
        for path in reversed(installed):
            try:
                backup = backups.get(path)
                if backup is not None:
                    os.replace(backup, path)
                    backups[path] = None
                else:
                    path.unlink(missing_ok=True)
            except OSError as rollback_exc:
                backup = backups.get(path)
                if backup is not None and backup.is_file():
                    recovery_backups.add(backup)
                    rollback_errors.append(f"{path}: {rollback_exc}; recovery backup retained at {backup}")
                else:
                    rollback_errors.append(f"{path}: {rollback_exc}")
        detail = f"dispatch CSV/JSON pair write failed: {exc}"
        if rollback_errors:
            detail += "; rollback also failed: " + "; ".join(rollback_errors)
        raise GateDispatchWriteError(detail) from exc
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
        for backup in backups.values():
            if backup is not None and backup not in recovery_backups:
                backup.unlink(missing_ok=True)


def validate_gate_dispatch_exports(result_path: str | Path) -> dict:
    """Read both companion files and verify their shared consistency manifest."""
    csv_path, json_path = gate_dispatch_paths(result_path)
    try:
        csv_bytes = csv_path.read_bytes()
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = tuple(reader.fieldnames or ())
            rows = list(reader)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, csv.Error) as exc:
        raise ValueError(f"cannot read gate dispatch companion pair: {exc}") from exc
    manifest = payload.get("output_consistency") if isinstance(payload, dict) else None
    if not isinstance(manifest, dict):
        raise ValueError("gate dispatch JSON is missing output_consistency manifest")
    if columns != CSV_COLUMNS:
        raise ValueError("gate dispatch CSV header must match the fixed CSV_COLUMNS definition exactly")
    if tuple(manifest.get("csv_columns", ())) != CSV_COLUMNS:
        raise ValueError("gate dispatch JSON csv_columns must match the fixed CSV_COLUMNS definition exactly")
    if columns != tuple(manifest.get("csv_columns", ())):
        raise ValueError("gate dispatch CSV columns do not match JSON output_consistency manifest")
    if hashlib.sha256(csv_bytes).hexdigest() != manifest.get("csv_sha256"):
        raise ValueError("gate dispatch CSV SHA-256 does not match JSON output_consistency manifest")
    records = payload.get("records")
    if not isinstance(records, list) or len(records) != len(rows) or len(rows) != manifest.get("csv_record_count"):
        raise ValueError("gate dispatch CSV row count does not match JSON records/manifest")
    if _payload_consistency_id(payload) != manifest.get("consistency_id"):
        raise ValueError("gate dispatch JSON contents do not match their consistency_id")
    nested_columns = {
        "evidence_json": "evidence",
        "conditions_json": "conditions",
        "threshold_reference_json": "threshold_reference",
    }
    for row, record in zip(rows, records):
        if row.get("consistency_id") != manifest.get("consistency_id"):
            raise ValueError("gate dispatch CSV rows do not share the JSON consistency_id")
        for column in CSV_COLUMNS:
            if column == "consistency_id":
                continue
            row_value = row.get(column, "")
            key = nested_columns.get(column, column)
            expected = record.get(key)
            if column in nested_columns:
                if json.loads(row_value or "{}") != expected:
                    raise ValueError(f"gate dispatch CSV {column} does not match JSON record")
            elif expected is None:
                if row_value != "":
                    raise ValueError(f"gate dispatch CSV {column} does not match JSON record")
            elif isinstance(expected, bool):
                if row_value.casefold() not in {"true", "false"} or (row_value.casefold() == "true") is not expected:
                    raise ValueError(f"gate dispatch CSV {column} does not match JSON record")
            elif isinstance(expected, int):
                try:
                    matches = int(row_value) == expected
                except (TypeError, ValueError):
                    matches = False
                if not matches:
                    raise ValueError(f"gate dispatch CSV {column} does not match JSON record")
            elif isinstance(expected, float):
                try:
                    matches = float(row_value) == expected
                except (TypeError, ValueError):
                    matches = False
                if not matches:
                    raise ValueError(f"gate dispatch CSV {column} does not match JSON record")
            elif row_value != str(expected):
                raise ValueError(f"gate dispatch CSV {column} does not match JSON record")
    return {"status": "consistent", "consistency_id": manifest["consistency_id"], "record_count": len(rows), "csv_sha256": manifest["csv_sha256"]}


def export_gate_dispatch_for_netcdf(
    result_path: str | Path, inputs: GateDispatchInput | None = None, *, overwrite: bool = False,
    script_id: str | None = None,
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
            "case", "case_id", "run_id", "script_id", "format", "sources_applied", "event_start", "gate_dispatch_time_origin", "activation_scope", "vertical_datum", "inflow_note", "outflow_note",
        ) if key in dataset.ncattrs()}
        result_dispatch_origin = getattr(dataset, "gate_dispatch_time_origin", None)
        result_event_origin = getattr(dataset, "event_start", None)
        origin = check_time_origin_consistency(result_dispatch_origin, result_event_origin)
    if script_id is not None:
        from .run_identity import resolve_script_id
        script_id = resolve_script_id(script_id)
        if any(context.get(key) not in (None, script_id) for key in ("script_id", "run_id")):
            raise ValueError("script_id differs from the NetCDF run identity")
        context["script_id"] = script_id
    if inputs is None:
        inputs = GateDispatchInput()
    log = build_gate_dispatch_log(times, inputs, result_context=context, time_origin=origin)
    return write_gate_dispatch_log(result_path, log, overwrite=overwrite)


__all__ = [
    "APPENDIX_DATUM_CONTEXT", "CSV_COLUMNS", "GATE_RULES", "RULE_COVERAGE_MANIFEST", "SOURCE_CATEGORIES", "GateDatumConversionEvidence",
    "GateDispatchInput", "GateDispatchLog", "GateDispatchOptions",
    "GateDispatchSnapshot", "GateDispatchWriteError", "GateOperation", "GateWaterLevel", "build_gate_dispatch_log",
    "check_time_origin_consistency", "export_gate_dispatch_for_netcdf", "gate_dispatch_paths",
    "validate_gate_dispatch_exports", "write_gate_dispatch_log",
]
