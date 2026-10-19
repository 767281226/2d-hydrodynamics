from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime
import math
from typing import Sequence


@dataclass(frozen=True)
class VerticalDatumConversionEvidence:
    """Scoped datum relation; it is evidence, not an applied DEM transform."""

    source_datum: str
    target_datum: str
    scope: str
    direction: str
    offset_m: float
    source_description: str
    evidence_ref: str
    applied: bool = False
    provenance: str = ""

    def __post_init__(self) -> None:
        for name in ("source_datum", "target_datum", "scope", "direction", "source_description", "evidence_ref"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.offset_m, bool) or not math.isfinite(float(self.offset_m)):
            raise ValueError("offset_m must be finite")
        object.__setattr__(self, "offset_m", float(self.offset_m))
        if self.applied:
            raise ValueError("datum conversion evidence must not claim that source data was modified")
        object.__setattr__(self, "provenance", str(self.provenance or ""))

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SocioEconomicFieldEvidence:
    """Provider-confirmed field semantics with values preserved in source order."""

    field_name: str
    semantic: str
    source: str
    source_year: str
    unit: str
    formula: str
    provided_values: tuple[float, ...]
    value_interpretation: str = "user-provided values; range-vs-example uncertain"
    provenance: str = ""
    is_cell_level: bool = True
    granularity: str = "cell_level"

    def __post_init__(self) -> None:
        for name in ("field_name", "semantic", "source", "source_year", "unit", "formula", "value_interpretation"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be a non-empty string")
        values = tuple(float(value) for value in self.provided_values)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("provided_values must contain finite numbers")
        object.__setattr__(self, "provided_values", values)
        object.__setattr__(self, "provenance", str(self.provenance or ""))
        if not isinstance(self.is_cell_level, bool):
            raise ValueError("is_cell_level must be boolean")
        granularity = str(self.granularity).strip().lower()
        if granularity not in {"cell_level", "administrative", "unknown"}:
            raise ValueError("granularity must be cell_level, administrative, or unknown")
        object.__setattr__(self, "granularity", granularity)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SocioEconomicEvidenceReport:
    status: str
    source_layer: str
    fields: tuple[SocioEconomicFieldEvidence, ...]
    administrative_fields_note: str
    provenance: str
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        expected = {"s_pop", "s_ar_mu", "s_ar_km2"}
        actual = {item.field_name for item in self.fields}
        missing = expected - actual
        if missing:
            raise ValueError(f"social-economic field evidence missing: {sorted(missing)}")
        if not str(self.source_layer).strip() or not str(self.provenance).strip():
            raise ValueError("source_layer and provenance must be non-empty")
        if not str(self.administrative_fields_note).strip():
            raise ValueError("administrative_fields_note must be non-empty")
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "blockers", tuple(str(item) for item in self.blockers))
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings))

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DispatchEventEvidence:
    """A narrative/manual-review event, explicitly not a station time series."""

    event_time: str
    decision_source: str
    trigger_narrative: str
    target: str
    activation_scope: str
    manual_review_required: bool
    annual_plan_dependency: bool
    evidence_ref: str
    observation_status: str = "narrative_only"
    rule_conditions: tuple[str, ...] = ()
    provenance: str = ""

    def __post_init__(self) -> None:
        for name in ("event_time", "decision_source", "trigger_narrative", "target", "activation_scope", "evidence_ref"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.observation_status != "narrative_only":
            raise ValueError("dispatch event must remain narrative_only in this phase")
        if not self.manual_review_required or not self.annual_plan_dependency:
            raise ValueError("dispatch event must retain manual review and annual-plan dependency")
        object.__setattr__(self, "rule_conditions", tuple(str(item) for item in self.rule_conditions))
        object.__setattr__(self, "provenance", str(self.provenance or ""))

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class VerticalDatumAttestation:
    source:str; datum_name:str; elevation_type:str; confirmed_by:str; confirmed_at:str; evidence_ref:str
@dataclass(frozen=True)
class DispatchApprovalEvidence:
    status:str; reviewer:str|None; reviewed_at:str|None; summary_version:str|None; evidence_ref:str|None
    approval_basis: str = "external_review"
@dataclass(frozen=True)
class LeveeCrestEvidence:
    levee_id:str; segment:str; chainage:float; crest_elevation:float; vertical_datum:str; overtopping_allowed:bool; breach_allowed:bool
@dataclass(frozen=True)
class BenchmarkEvidence:
    benchmark_id:str; reference_source:str; variable:str; time_window:str; spatial_extent:str; metric:str; tolerance:float; result:float; review_status:str
@dataclass(frozen=True)
class ExternalEvidenceReport:
    status:str; datum_status:str; dispatch_status:str; levee_status:str; benchmark_status:str; blockers:tuple[str,...]; warnings:tuple[str,...]
    datum_conversion_status: str = "not_supplied"
    socio_economic_status: str = "not_supplied"
    dispatch_event_status: str = "not_supplied"
    provenance: str = ""
    annual_plan_status: str = "not_supplied"
    annual_plan_waiver_reason: str | None = None
    benchmark_evidence_status: str = "not_supplied"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_confirmed_socio_economic_evidence(
    *, source_layer: str = "data/grid_50m_social_economy/shp/grid_cells_socio.shp",
    provenance: str = "user-confirmed input; 2026-09-14",
) -> SocioEconomicEvidenceReport:
    """Build the confirmed field dictionary without changing source values."""
    fields = (
        SocioEconomicFieldEvidence(
            "s_pop", "格内常住人口（七普）", "第七次全国人口普查", "2020", "人/格",
            "区常住人口 / 区面积(km²) * 0.0025", (1.3335, 5.2848),
            provenance=provenance, is_cell_level=True, granularity="cell_level",
        ),
        SocioEconomicFieldEvidence(
            "s_ar_mu", "格内耕地面积（三调）", "第三次全国国土调查", "2019", "亩/格",
            "区耕地面积(km²) * 1500 / 区面积(km²) * 0.0025", (1.4292, 0.558),
            provenance=provenance, is_cell_level=True, granularity="cell_level",
        ),
        SocioEconomicFieldEvidence(
            "s_ar_km2", "格内耕地面积（三调）", "第三次全国国土调查", "2019", "km²/格",
            "s_ar_mu / 1500", (),
            value_interpretation="derived relation confirmed; no per-cell values supplied",
            provenance=provenance, is_cell_level=True, granularity="cell_level",
        ),
    )
    return SocioEconomicEvidenceReport(
        status="confirmed",
        source_layer=source_layer,
        fields=fields,
        administrative_fields_note=(
            "区常住人口和区耕地面积是行政汇总来源；仅按给定公式作为字段来源说明，"
            "不得把行政汇总值直接当作逐格值。"
        ),
        provenance=provenance,
        warnings=("s_pop/s_ar_mu 用户数值未说明是最小/最大值或样例，保持原顺序。",),
    )


def build_confirmed_datum_conversion_evidence(
    *, provenance: str = "user-confirmed input; 2026-09-14",
) -> VerticalDatumConversionEvidence:
    return VerticalDatumConversionEvidence(
        source_datum="大沽高程",
        target_datum="1985国家高程基准",
        scope="下游东淀-文安洼区域",
        direction="target = source - 1.56 m",
        offset_m=-1.56,
        source_description="85国家高程 = 大沽高程 - 1.56m（用户提供）",
        evidence_ref="user-confirmed-input:datum-conversion",
        applied=False,
        provenance=provenance,
    )


def build_confirmed_dispatch_event_evidence(
    *, provenance: str = "user-confirmed input; 2026-09-14",
) -> DispatchEventEvidence:
    return DispatchEventEvidence(
        event_time="2023-08-01T02:00:00+08:00",
        decision_source="国务院批复《大清河防御洪水方案》（国函〔2007〕33号）用户摘要；国家防总事件叙述",
        trigger_narrative=(
            "大清河上游新盖房枢纽来水加大、大清河水位持续上涨达到启用条件；"
            "东淀保证水位第六堡站8.00m（大沽高程），枣林庄枢纽泄洪流量超过700m³/s时启用东淀；"
            "十方院水位达到10.80m（大沽高程）且继续上涨时开启王村分洪闸，"
            "第六堡站继续上涨并威胁天津安全时可启用滩里口门。"
        ),
        target="东淀",
        activation_scope="大清河以北部分",
        manual_review_required=True,
        annual_plan_dependency=True,
        evidence_ref="user-confirmed-input:dispatch-event-2023-08-01",
        rule_conditions=(
            "第六堡站保证水位 >= 8.00 m（大沽高程）",
            "枣林庄枢纽泄洪流量 > 700 m³/s",
            "十方院水位 >= 10.80 m（大沽高程）且继续上涨",
            "实际汛期执行依赖河北省年度洪水调度方案",
        ),
        provenance=provenance,
    )


def build_user_confirmed_dispatch_approval(
    *,
    reviewer: str = "用户确认",
    approved_at: str = "2026-09-16T00:00:00+08:00",
    scope: str = "2023-08-01 02:00 东淀启用事件及相关规则",
) -> DispatchApprovalEvidence:
    """Record a user decision as review evidence, not as an official approval."""
    return DispatchApprovalEvidence(
        status="approved",
        reviewer=reviewer,
        reviewed_at=approved_at,
        summary_version=f"user-confirmed:{scope}",
        evidence_ref="user-confirmed-input:dispatch-review",
        approval_basis="user_confirmed",
    )


def build_user_waived_annual_plan(
    *, reason: str = "用户明确决定忽略河北省年度洪水调度方案版本及审核凭证",
) -> dict[str, str]:
    return {
        "status": "user_waived",
        "waiver_reason": reason,
        "evidence_ref": "user-confirmed-input:annual-plan-waiver",
    }


def validate_external_evidence(*, datum:VerticalDatumAttestation|None=None, target_datum:str="1985国家高程基准", dispatch:DispatchApprovalEvidence|None=None, levee:Sequence[LeveeCrestEvidence]=(), benchmark:BenchmarkEvidence|None=None, absolute_level_required:bool=False, datum_conversion:VerticalDatumConversionEvidence|None=None, socio_economic:SocioEconomicEvidenceReport|None=None, dispatch_event:DispatchEventEvidence|None=None, annual_plan_waived: bool = False, annual_plan_waiver_reason: str | None = None, provenance: str = "")->ExternalEvidenceReport:
    blockers=[]; warnings=[]
    datum_mismatch = False
    if datum is None: datum_status="block" if absolute_level_required else "warn"; (blockers if absolute_level_required else warnings).append("vertical datum attestation missing")
    elif not all((datum.source,datum.datum_name,datum.elevation_type,datum.confirmed_by,datum.confirmed_at,datum.evidence_ref)): datum_status="block"; blockers.append("vertical datum attestation incomplete")
    elif datum.datum_name != target_datum:
        datum_mismatch = True
        datum_status="block"
    else: datum_status="verified"
    datum_conversion_status="not_supplied"
    if datum_conversion is not None:
        try:
            if datum_conversion.source_datum != "大沽高程" or datum_conversion.target_datum != target_datum:
                raise ValueError("scoped datum conversion source/target does not match the confirmed material")
            if "东淀" not in datum_conversion.scope or "文安洼" not in datum_conversion.scope:
                raise ValueError("scoped datum conversion must identify the 东淀-文安洼 region")
            if datum_conversion.direction.strip().lower().replace(" ", "") not in {"target=source-1.56m", "target=source−1.56m"} or datum_conversion.offset_m != -1.56:
                raise ValueError("scoped datum conversion must preserve target = source - 1.56 m")
            datum_conversion_status="verified_scoped"
            if datum is None or datum.datum_name != target_datum:
                datum_status="verified_scoped"
        except ValueError as exc:
            datum_conversion_status="fail"
            blockers.append(f"scoped datum conversion evidence invalid: {exc}")
    if datum_mismatch and datum_conversion_status != "verified_scoped":
        blockers.append("source datum does not match target datum; explicit conversion evidence required")
    if dispatch is None or dispatch.status != "approved" or not dispatch.reviewer or not dispatch.reviewed_at or not dispatch.summary_version or not dispatch.evidence_ref:
        dispatch_status="block"; blockers.append("dispatch approval evidence missing or not approved")
    elif getattr(dispatch, "approval_basis", "external_review") == "user_confirmed":
        dispatch_status="user_confirmed"
        warnings.append("dispatch approval is user-confirmed, not an official government or annual-plan approval")
    else: dispatch_status="verified"
    if not levee: levee_status="warn"; warnings.append("levee crest evidence not supplied; overtopping remains disabled")
    elif any(not e.vertical_datum or e.crest_elevation != e.crest_elevation for e in levee): levee_status="block"; blockers.append("levee crest evidence incomplete")
    else: levee_status="verified"
    benchmark_evidence_status="not_supplied"
    if benchmark is None:
        benchmark_status="block"; blockers.append("engineering benchmark evidence missing or not approved")
    elif benchmark.review_status == "synthetic_only":
        benchmark_status="synthetic_only"; benchmark_evidence_status="synthetic_only"
        warnings.append("engineering benchmark is synthetic_only/unverified; it cannot establish real engineering accuracy")
    elif benchmark.review_status != "approved":
        benchmark_status="block"; blockers.append("engineering benchmark evidence missing or not approved")
    else:
        benchmark_status="verified"; benchmark_evidence_status="verified"
    socio_economic_status="not_supplied"
    if socio_economic is not None:
        socio_economic_status = "verified" if socio_economic.status in {"pass", "verified", "confirmed"} else socio_economic.status
        for item in socio_economic.fields:
            if not item.is_cell_level or item.granularity != "cell_level":
                blockers.append(f"social-economic field {item.field_name} is not confirmed as cell-level")
        blockers.extend(socio_economic.blockers)
        warnings.extend(socio_economic.warnings)
    dispatch_event_status="not_supplied"
    if dispatch_event is not None:
        dispatch_event_status="pending_manual_review"
        warnings.append("dispatch event is a user-provided narrative; no complete station observation time series is asserted")
        warnings.append("第六堡、枣林庄、十方院实际水位/流量时序尚未提供")
        warnings.append("annual flood-control plan version and approval credential remain unresolved")
    annual_plan_status = "user_waived" if annual_plan_waived else "not_supplied"
    if annual_plan_waived:
        warnings.append(annual_plan_waiver_reason or "annual flood-control plan approval was user-waived")
    status="block" if blockers else ("warn" if warnings else "pass")
    return ExternalEvidenceReport(status,datum_status,dispatch_status,levee_status,benchmark_status,tuple(dict.fromkeys(blockers)),tuple(dict.fromkeys(warnings)),datum_conversion_status,socio_economic_status,dispatch_event_status,provenance,annual_plan_status,annual_plan_waiver_reason,benchmark_evidence_status)
__all__=["BenchmarkEvidence","DispatchApprovalEvidence","DispatchEventEvidence","ExternalEvidenceReport","LeveeCrestEvidence","SocioEconomicEvidenceReport","SocioEconomicFieldEvidence","VerticalDatumAttestation","VerticalDatumConversionEvidence","build_confirmed_datum_conversion_evidence","build_confirmed_dispatch_event_evidence","build_confirmed_socio_economic_evidence","build_user_confirmed_dispatch_approval","build_user_waived_annual_plan","validate_external_evidence"]
