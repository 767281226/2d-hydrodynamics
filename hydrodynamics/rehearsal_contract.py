"""Strict rehearsal scenario, identity, selector and preflight contracts.

This R1 module validates business inputs and adapts a verified synthetic profile
for preflight. It does not start a solver, read network/database inputs, publish
results, or mark any product as production-ready.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from importlib import resources
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Annotated, Literal, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator
import yaml

from .config import Number, Text
from .triangular_business import (
    TriangularBoundaryBinding,
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    terrain_fingerprint,
    topology_fingerprint,
)
from .triangular_mesh import FaceTerrainField, TriangularMesh
from .workflow_config import validate_run_id
from .rehearsal_catalog import (
    RehearsalCatalogError,
    clone_builtin_template,
    list_catalog_items,
    load_builtin_rehearsal_catalog,
    show_catalog_item,
)


class RehearsalContractError(ValueError):
    """Raised when a rehearsal document, profile or execution envelope is invalid."""


class _RehearsalStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, allow_inf_nan=False)


MAX_DURATION_S = 31_536_000.0
MAX_OUTPUT_TIMES = 100_000
MIN_DISTINCT_OUTPUT_INTERVAL_S = 1e-6
_PROFILE_SHA256 = "6a8c247e4c44718db2a3cfb98504ec24196ec0dc1489fec478e01775fa427318"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _shared_time_tolerance_s(value: float) -> float:
    return max(1e-12, 8.0 * math.ulp(float(value)))


def _digest_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate_local_datetime(value: str, zone_name: str, *, field: str) -> str:
    try:
        local = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"{field} must use yyyy-MM-dd HH:mm:ss") from exc
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"{field} time_zone must be a recognized IANA timezone") from exc
    candidates: list[datetime] = []
    for fold in (0, 1):
        local_aware = local.replace(tzinfo=zone, fold=fold)
        roundtrip = local_aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if roundtrip == local:
            candidates.append(local_aware)
    if not candidates:
        raise ValueError(f"{field} is a nonexistent local time in {zone_name}")
    if len(candidates) == 2 and candidates[0].utcoffset() != candidates[1].utcoffset():
        raise ValueError(f"{field} is an ambiguous local time in {zone_name}; an explicit offset is required")
    return candidates[0].astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ProfileReference(_RehearsalStrictModel):
    profile_id: Text = Field(description="Resolved input profile identifier; the user cannot assert its support status")
    profile_version: Text = Field(description="Immutable profile version")


class ScenarioTimePoint(_RehearsalStrictModel):
    time_s: Number = Field(ge=0.0, le=MAX_DURATION_S, description="Seconds relative to the scenario start; output-axis alignment is checked in preflight")
    value: Number = Field(description="Finite sample in the series quantity/unit declared by the parent")


class BoundarySeriesReference(_RehearsalStrictModel):
    series_id: Text = Field(description="Stable series reference, not a display label")
    dataset_version_id: Text | None = Field(default=None, description="Immutable external dataset version; unresolved references block computation")
    content_sha256: Text | None = Field(default=None, description="Claimed source digest; never treated as trusted until a resolver verifies it")
    quantity: Literal["discharge", "water_level"] = Field(description="Boundary quantity represented by the samples")
    unit: Literal["m3/s", "m"] = Field(description="Required native unit: discharge m3/s or absolute level m")
    direction: Literal["inflow", "outflow"] | None = Field(default=None, description="Required for non-negative discharge; direction is separate from Q sign")
    vertical_datum: Text | None = Field(default=None, description="Required for absolute water_level; must exactly match resolved terrain datum")
    time_basis: Literal["SCENARIO_RELATIVE_S"] = Field(default="SCENARIO_RELATIVE_S", description="R1 normalized sample times in seconds from scenario start")
    temporal_policy: Literal["piecewise_constant", "piecewise_linear"] = Field(description="Explicit input interpolation policy; engine support is checked separately")
    points: list[ScenarioTimePoint] | None = Field(default=None, min_length=2, max_length=100_000, description="Inline samples; omitted means external series content has not been resolved")

    @field_validator("content_sha256")
    @classmethod
    def valid_optional_sha(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_RE.fullmatch(value):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def quantity_and_unit_agree(self) -> "BoundarySeriesReference":
        if self.quantity == "discharge":
            if self.unit != "m3/s" or self.direction is None or self.vertical_datum is not None:
                raise ValueError("discharge requires unit m3/s, explicit direction, and no vertical_datum")
            if self.points is not None and any(point.value < 0.0 for point in self.points):
                raise ValueError("discharge samples must remain non-negative; direction carries the sign")
        else:
            if self.unit != "m" or self.direction is not None or self.vertical_datum is None:
                raise ValueError("water_level requires absolute metres and a known vertical_datum; direction is not valid")
        if self.points is not None:
            times = [point.time_s for point in self.points]
            if any(right <= left for left, right in zip(times, times[1:])):
                raise ValueError("series points time_s must be strictly increasing and unique")
        return self


class ForecastBoundarySource(_RehearsalStrictModel):
    source_mode: Literal["FORECAST"]
    section_id: Text = Field(description="Stable hydraulic section ID; display name is not an identity")
    forecast_dataset_id: Text | None = Field(default=None, description="Forecast product/batch identity")
    forecast_version_id: Text | None = Field(default=None, description="Immutable forecast publication version")
    issued_at_local: Text | None = Field(default=None, description="Forecast issue time in yyyy-MM-dd HH:mm:ss; not valid time")
    issued_time_zone: Text | None = Field(default=None, description="IANA timezone for issued_at_local")
    series_ref: BoundarySeriesReference | None = Field(default=None, description="Per-section forecast series; missing content is a draft blocker for computation")

    @model_validator(mode="after")
    def issue_time_pair(self) -> "ForecastBoundarySource":
        if (self.issued_at_local is None) != (self.issued_time_zone is None):
            raise ValueError("issued_at_local and issued_time_zone must be supplied together")
        if self.issued_at_local is not None:
            _validate_local_datetime(self.issued_at_local, str(self.issued_time_zone), field="issued_at_local")
        return self


class DesignBoundarySource(_RehearsalStrictModel):
    source_mode: Literal["DESIGN"]
    section_id: Text = Field(description="Stable hydraulic section ID")
    design_dataset_id: Text | None = Field(default=None, description="Approved design source identity")
    design_version_id: Text | None = Field(default=None, description="Immutable design data version")
    design_method_id: Text | None = Field(default=None, description="Reviewed design hydrograph method reference")
    return_period_years: Number | None = Field(default=None, gt=0.0, le=10_000.0, description="Return period in years; does not generate a hydrograph by itself")
    approval_ref: Text | None = Field(default=None, description="Approval/evidence reference for a design dataset")
    series_ref: BoundarySeriesReference | None = Field(default=None, description="Complete section-specific design series")

    @model_validator(mode="after")
    def approval_requires_design_identity(self) -> "DesignBoundarySource":
        if self.approval_ref is not None and self.design_dataset_id is None:
            raise ValueError("approval_ref cannot be supplied without a design_dataset_id")
        return self


class HistoricalBoundarySource(_RehearsalStrictModel):
    source_mode: Literal["HISTORICAL"]
    section_id: Text = Field(description="Stable observed/model section ID")
    event_id: Text | None = Field(default=None, description="Historical event identity; a catalog event alone is not a continuous series")
    observation_dataset_id: Text | None = Field(default=None, description="Observation dataset identity")
    observation_version_id: Text | None = Field(default=None, description="Immutable observation/quality-control version")
    observation_kind: Literal["OBSERVED", "RECONSTRUCTED", "TRANSCRIBED"] | None = None
    station_or_section_ref: Text | None = Field(default=None, description="Observed station/section mapping evidence")
    series_ref: BoundarySeriesReference | None = Field(default=None, description="Complete time series; isolated news values do not qualify")


class ManualBoundarySource(_RehearsalStrictModel):
    source_mode: Literal["MANUAL"]
    section_id: Text = Field(description="Stable hydraulic section ID")
    rationale: Text | None = Field(default=None, description="Why the explicit manual series is being used")
    input_source_ref: Text | None = Field(default=None, description="Source document/file reference; not a verified approval by itself")
    series_ref: BoundarySeriesReference | None = Field(default=None, description="Explicit manual series; values are not defaulted or interpolated silently")


UpperBoundarySource = Annotated[
    ForecastBoundarySource | DesignBoundarySource | HistoricalBoundarySource | ManualBoundarySource,
    Field(discriminator="source_mode"),
]


class RuleSnapshotReference(_RehearsalStrictModel):
    rule_id: Text = Field(description="Existing dispatch-rule identity")
    rule_version: Text = Field(description="Immutable rule version")
    source_sha256: Text | None = Field(default=None, description="Source-document digest verified by an authorized resolver")
    rule_usage: Literal["REFERENCE_ONLY", "EVALUATE_APPROVED_RULE"] = Field(default="REFERENCE_ONLY", description="R1 retains references only; rule evaluation is not implemented")

    @field_validator("source_sha256")
    @classmethod
    def valid_rule_sha(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_RE.fullmatch(value):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        return value


class TemplateReference(_RehearsalStrictModel):
    template_key: Literal[
        "TJ_2023_DONGDIAN_PUBLIC_HISTORY_V1", "TJ_2023_YONGDING_PUBLIC_HISTORY_V1",
        "TJ_2025_ECO_WATER_REPLENISHMENT_V1", "TJ_2025_FLOOD_JOINT_THEME_V1",
        "DESIGN_FLOOD_PARAMETERIZED_V1", "FORECAST_ROLLING_V1", "MANUAL_COMPARISON_V1",
    ]
    catalog_version: Text


class InitialConditionInput(_RehearsalStrictModel):
    mode: Literal["UNKNOWN", "PROFILE_REF"] = Field(default="UNKNOWN", description="UNKNOWN is a saved but non-computable state; never infer zero depth")
    profile_ref: ProfileReference | None = Field(default=None, description="Trusted initial profile reference, required only for PROFILE_REF")

    @model_validator(mode="after")
    def profile_matches_mode(self) -> "InitialConditionInput":
        if (self.mode == "PROFILE_REF") != (self.profile_ref is not None):
            raise ValueError("initial condition PROFILE_REF requires a profile_ref; UNKNOWN must not carry one")
        return self


class RainfallInput(_RehearsalStrictModel):
    mode: Literal["UNKNOWN", "EXPLICIT_NONE", "PROFILE_REF"] = Field(default="UNKNOWN", description="UNKNOWN differs from an explicit no-rain declaration")
    profile_ref: ProfileReference | None = Field(default=None, description="Trusted rainfall profile reference, required only for PROFILE_REF")

    @model_validator(mode="after")
    def profile_matches_mode(self) -> "RainfallInput":
        if (self.mode == "PROFILE_REF") != (self.profile_ref is not None):
            raise ValueError("rainfall PROFILE_REF requires a profile_ref; UNKNOWN/EXPLICIT_NONE must not carry one")
        return self


class DownstreamPolicy(_RehearsalStrictModel):
    mode: Literal["DEFERRED_UNKNOWN", "SYNTHETIC_CLOSED_DOMAIN"] = Field(default="DEFERRED_UNKNOWN", description="Unknown is not zero flow/stage, free outflow, or a wall")
    profile_ref: ProfileReference | None = Field(default=None, description="Only the pinned synthetic profile can authorize the synthetic closed-wall policy")
    user_input_required: Literal[False] = Field(default=False, description="Lower-boundary entry is deferred from the ordinary user form")
    applied: Literal[False] = Field(default=False, description="R1 does not apply boundary conditions")

    @model_validator(mode="after")
    def profile_matches_mode(self) -> "DownstreamPolicy":
        if (self.mode == "SYNTHETIC_CLOSED_DOMAIN") != (self.profile_ref is not None):
            raise ValueError("SYNTHETIC_CLOSED_DOMAIN must reference an explicit verified synthetic profile; unknown must carry no profile")
        return self


Aggregation = Literal[
    "FRAME", "SERIES", "MAX_OVER_WINDOW", "PEAK_AREA_OVER_WINDOW", "UNION_FOOTPRINT",
    "FIRST_ARRIVAL", "WET_DURATION", "EVENT_TIMELINE",
]
ProductCode = Literal[
    "water_depth", "water_level", "velocity", "maximum_depth", "wet_area", "storage_volume",
    "peak_inundation_area", "inundation_footprint", "first_arrival", "wet_duration",
    "depth_band_stat", "boundary_series", "gate_audit", "affected_population", "flood_loss",
    "evacuation_plan", "scene_3d",
]
_PRODUCT_AGGREGATIONS: dict[str, frozenset[str]] = {
    "water_depth": frozenset({"FRAME"}),
    "water_level": frozenset({"FRAME"}),
    "velocity": frozenset({"FRAME"}),
    "wet_area": frozenset({"FRAME"}),
    "storage_volume": frozenset({"FRAME", "SERIES", "MAX_OVER_WINDOW"}),
    "maximum_depth": frozenset({"MAX_OVER_WINDOW"}),
    "peak_inundation_area": frozenset({"PEAK_AREA_OVER_WINDOW"}),
    "inundation_footprint": frozenset({"UNION_FOOTPRINT"}),
    "first_arrival": frozenset({"FIRST_ARRIVAL"}),
    "wet_duration": frozenset({"WET_DURATION"}),
    "depth_band_stat": frozenset({"FRAME", "MAX_OVER_WINDOW"}),
    "boundary_series": frozenset({"SERIES"}),
    "gate_audit": frozenset({"EVENT_TIMELINE"}),
    "affected_population": frozenset({"FRAME", "MAX_OVER_WINDOW", "UNION_FOOTPRINT", "FIRST_ARRIVAL", "WET_DURATION"}),
    "flood_loss": frozenset({"FRAME", "MAX_OVER_WINDOW", "UNION_FOOTPRINT"}),
    "evacuation_plan": frozenset({"EVENT_TIMELINE"}),
    "scene_3d": frozenset({"FRAME"}),
}
_SPATIAL_PRODUCTS = frozenset(set(_PRODUCT_AGGREGATIONS) - {"gate_audit"})
_FRAME_AGGREGATIONS = frozenset({"FRAME"})
_WINDOW_AGGREGATIONS = frozenset({"MAX_OVER_WINDOW", "PEAK_AREA_OVER_WINDOW", "UNION_FOOTPRINT", "FIRST_ARRIVAL", "WET_DURATION"})


class ProductRequest(_RehearsalStrictModel):
    product_code: ProductCode = Field(description="Controlled product registry code")
    required: StrictBool = Field(description="Required products block preflight when unsupported; optional products remain explicitly unavailable")
    scope_id: Text = Field(description="Stable requested scope ID; resolution/hash comes from the trusted profile or result manifest")
    aggregation: Aggregation = Field(description="Product-specific aggregation selector from the closed allowlist")

    @model_validator(mode="after")
    def product_aggregation_is_allowed(self) -> "ProductRequest":
        if self.aggregation not in _PRODUCT_AGGREGATIONS[self.product_code]:
            raise ValueError(f"aggregation {self.aggregation} is not allowed for product {self.product_code}")
        if self.product_code == "gate_audit" and self.scope_id != "AUDIT_RUN_GLOBAL":
            raise ValueError("gate_audit must use the explicit AUDIT_RUN_GLOBAL scope")
        if self.product_code != "gate_audit" and self.scope_id == "AUDIT_RUN_GLOBAL":
            raise ValueError("AUDIT_RUN_GLOBAL is valid only for gate_audit")
        return self


class ScenarioDraft(_RehearsalStrictModel):
    """Strict business draft; saving and calculation readiness are separate."""

    format: Literal["rehearsal-scenario-v1"] = "rehearsal-scenario-v1"
    schema_version: Literal["1.0"] = "1.0"
    scenario_name: Text = Field(description="Required non-empty business display name; not a model case ID")
    description: Text | None = None
    creation_type: Literal["MANUAL", "BUILTIN_CLONE", "COPY", "SYSTEM_RECOMMENDED"]
    template_ref: TemplateReference | None = None
    source_document_plan_id: Text | None = Field(default=None, description="Optional reference to the existing document catalog; not a run ID")
    region_ids: list[Text] = Field(default_factory=list, max_length=1_000, description="Stable region IDs; required to save and bound by the trusted region registry")
    region_aliases: list[Text] = Field(default_factory=list, max_length=1_000)
    event_id: Text | None = None
    time_origin_local: Text | None = Field(description="Required field; null means absolute event time is unknown, otherwise yyyy-MM-dd HH:mm:ss")
    time_zone: Text = Field(description="Required IANA timezone for displayed local times")
    duration_hours: Number | None = Field(default=None, gt=0.0, le=8_760.0, description="Scenario duration in hours; optional in a saved draft, required for computation")
    output_step_minutes: Number | None = Field(default=None, gt=0.0, le=525_600.0, description="Requested output/save interval in minutes; not the solver integration dt; optional in draft")
    engine_mode: Literal["structured_hourly", "triangular_closed_experimental", "triangular_source_experimental"] | None = Field(default=None, description="Engine identifier checked against the package-owned capability registry")
    input_profile_ref: ProfileReference | None = None
    upper_boundaries: list[UpperBoundarySource] = Field(default_factory=list, max_length=10_000)
    rule_snapshot_ref: RuleSnapshotReference | None = None
    initial_condition: InitialConditionInput = Field(default_factory=InitialConditionInput)
    rainfall: RainfallInput = Field(default_factory=RainfallInput)
    downstream_policy: DownstreamPolicy = Field(default_factory=DownstreamPolicy)
    requested_products: list[ProductRequest] = Field(default_factory=list, max_length=100)
    metric_policy_version: Text | None = Field(default=None, description="Versioned metric thresholds/aggregation policy; required by preflight")
    historical_event_refs: list[Text] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def unique_ids_and_source_sections(self) -> "ScenarioDraft":
        if len(set(self.region_ids)) != len(self.region_ids):
            raise ValueError("region_ids must be unique stable IDs")
        if len(set(self.historical_event_refs)) != len(self.historical_event_refs):
            raise ValueError("historical_event_refs must be unique catalog event keys")
        section_ids = [source.section_id for source in self.upper_boundaries]
        if len(set(section_ids)) != len(section_ids):
            raise ValueError("upper boundary sources must not duplicate a section_id")
        if len({(item.product_code, item.scope_id, item.aggregation) for item in self.requested_products}) != len(self.requested_products):
            raise ValueError("requested_products must not repeat a product/scope/aggregation selector")
        if self.creation_type == "BUILTIN_CLONE" and self.template_ref is None:
            raise ValueError("BUILTIN_CLONE requires a template_ref")
        if self.creation_type != "BUILTIN_CLONE" and self.template_ref is not None:
            raise ValueError("template_ref is valid only for BUILTIN_CLONE scenarios")
        try:
            ZoneInfo(self.time_zone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("time_zone must be a recognized IANA timezone") from exc
        if self.time_origin_local is not None:
            _validate_local_datetime(self.time_origin_local, self.time_zone, field="time_origin_local")
        if (self.duration_hours is None) != (self.output_step_minutes is None):
            # A partial form remains saveable as a draft; preflight reports the missing half.
            return self
        if self.duration_hours is not None and self.output_step_minutes is not None:
            normalize_output_times(self.duration_hours, self.output_step_minutes)
        return self


class ResultProductSelector(_RehearsalStrictModel):
    """Exact selector for a published result query; the server checks its manifest."""

    result_set_id: Text = Field(description="Immutable published result-set ID")
    product_code: ProductCode = Field(description="Controlled result product code")
    scope_id: Text = Field(description="Stable manifest scope; use AUDIT_RUN_GLOBAL only for G1 audit")
    scope_hash: Text | None = None
    aggregation: Aggregation = Field(description="Product-specific aggregation; not interchangeable with frame/time")
    frame_index: StrictInt | None = Field(default=None, ge=0)
    time_s: Number | None = Field(default=None, ge=0.0, le=MAX_DURATION_S)
    window_start_s: Number | None = Field(default=None, ge=0.0, le=MAX_DURATION_S)
    window_end_s: Number | None = Field(default=None, ge=0.0, le=MAX_DURATION_S)
    metric_policy_version: Text = Field(description="Must match the result set's frozen policy version")
    object_id: Text | None = Field(default=None, description="Required for object-scoped series products")

    @field_validator("scope_hash")
    @classmethod
    def valid_scope_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_RE.fullmatch(value):
            raise ValueError("scope_hash must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def selector_tuple_is_unambiguous(self) -> "ResultProductSelector":
        if self.aggregation not in _PRODUCT_AGGREGATIONS[self.product_code]:
            raise ValueError(f"aggregation {self.aggregation} is not allowed for product {self.product_code}")
        if self.product_code in _SPATIAL_PRODUCTS:
            if self.scope_id == "AUDIT_RUN_GLOBAL" or self.scope_hash is None:
                raise ValueError("spatial products require a registered scope_id and scope_hash")
        elif self.product_code == "gate_audit":
            if self.scope_id != "AUDIT_RUN_GLOBAL" or self.scope_hash is not None:
                raise ValueError("gate_audit requires AUDIT_RUN_GLOBAL with scope_hash=null")
        if self.aggregation == "FRAME":
            if self.frame_index is None or self.time_s is None or self.window_start_s is not None or self.window_end_s is not None:
                raise ValueError("FRAME requires frame_index/time_s and no time_window")
        elif self.aggregation == "SERIES":
            if self.frame_index is not None or self.time_s is not None or self.window_start_s is None or self.window_end_s is None or self.object_id is None:
                raise ValueError("SERIES requires object_id/time_window and no frame_index/time_s")
        elif self.aggregation in _WINDOW_AGGREGATIONS | {"EVENT_TIMELINE"}:
            if self.frame_index is not None or self.time_s is not None or self.window_start_s is None or self.window_end_s is None:
                raise ValueError(f"{self.aggregation} requires time_window and no frame_index/time_s")
        if self.window_start_s is not None and self.window_end_s is not None and self.window_end_s <= self.window_start_s:
            raise ValueError("time_window end must be greater than time_window start")
        return self


class ScopeDescriptor(_RehearsalStrictModel):
    scope_id: Text
    scope_hash: Text

    @field_validator("scope_hash")
    @classmethod
    def valid_sha(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("scope_hash must be a lowercase SHA-256 digest")
        return value


class ProductCoverage(_RehearsalStrictModel):
    product_code: ProductCode
    scope_id: Text
    scope_hash: Text | None = None
    aggregation: Aggregation
    frame_index: StrictInt | None = Field(default=None, ge=0)
    time_s: Number | None = Field(default=None, ge=0.0, le=MAX_DURATION_S)
    window_start_s: Number | None = Field(default=None, ge=0.0, le=MAX_DURATION_S)
    window_end_s: Number | None = Field(default=None, ge=0.0, le=MAX_DURATION_S)
    object_id: Text | None = None
    metric_policy_version: Text
    status: Literal["COMPLETE", "PARTIAL", "UNSUPPORTED", "MISSING_INPUT", "NOT_COMPUTED"]
    expected_count: StrictInt | None = Field(default=None, ge=0)
    valid_count: StrictInt | None = Field(default=None, ge=0)

    @field_validator("scope_hash")
    @classmethod
    def valid_optional_scope_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_RE.fullmatch(value):
            raise ValueError("scope_hash must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def coverage_tuple_matches_product(self) -> "ProductCoverage":
        if self.aggregation not in _PRODUCT_AGGREGATIONS[self.product_code]:
            raise ValueError("coverage aggregation is not allowed for its product_code")
        if self.aggregation == "FRAME":
            if self.frame_index is None or self.time_s is None or self.window_start_s is not None or self.window_end_s is not None:
                raise ValueError("FRAME coverage requires frame_index/time_s and no window")
        elif self.frame_index is not None or self.time_s is not None:
            raise ValueError("window/series coverage must not carry frame_index/time_s")
        if self.aggregation in _WINDOW_AGGREGATIONS | {"EVENT_TIMELINE", "SERIES"}:
            if self.window_start_s is None or self.window_end_s is None:
                raise ValueError(f"{self.aggregation} coverage requires an explicit time_window")
        if self.aggregation == "SERIES" and self.object_id is None:
            raise ValueError("SERIES coverage requires an object_id")
        if (self.window_start_s is None) != (self.window_end_s is None):
            raise ValueError("coverage window must include both start and end or neither")
        if self.window_start_s is not None and self.window_end_s is not None and self.window_end_s <= self.window_start_s:
            raise ValueError("coverage window end must be greater than its start")
        if self.product_code == "gate_audit":
            if self.scope_id != "AUDIT_RUN_GLOBAL" or self.scope_hash is not None:
                raise ValueError("gate audit coverage must use AUDIT_RUN_GLOBAL with no spatial hash")
        elif self.product_code in _SPATIAL_PRODUCTS and self.scope_hash is None:
            raise ValueError("spatial coverage requires a scope_hash")
        if self.expected_count is not None and self.valid_count is not None and self.valid_count > self.expected_count:
            raise ValueError("valid_count cannot exceed expected_count")
        if self.status in {"COMPLETE", "PARTIAL"} and self.product_code != "gate_audit":
            if self.expected_count is None or self.valid_count is None:
                raise ValueError("spatial/series coverage status requires expected_count and valid_count")
            if self.status == "COMPLETE" and self.valid_count != self.expected_count:
                raise ValueError("COMPLETE coverage requires valid_count == expected_count")
            if self.status == "PARTIAL" and self.valid_count == self.expected_count:
                raise ValueError("PARTIAL coverage must report an incomplete valid_count")
        return self


class ResultSetManifest(_RehearsalStrictModel):
    result_set_id: Text
    metric_policy_version: Text
    frame_times_s: list[Number] = Field(default_factory=list, max_length=MAX_OUTPUT_TIMES)
    duration_s: Number = Field(ge=0.0, le=MAX_DURATION_S)
    scopes: list[ScopeDescriptor] = Field(default_factory=list)
    product_coverage: list[ProductCoverage] = Field(default_factory=list)

    @model_validator(mode="after")
    def manifest_axes_are_valid(self) -> "ResultSetManifest":
        if self.frame_times_s and (self.frame_times_s[0] != 0.0 or self.frame_times_s[-1] != self.duration_s):
            raise ValueError("frame_times_s must include 0 and the exact manifest duration")
        if any(right <= left for left, right in zip(self.frame_times_s, self.frame_times_s[1:])):
            raise ValueError("frame_times_s must be strictly increasing")
        scope_keys = [item.scope_id for item in self.scopes]
        if len(scope_keys) != len(set(scope_keys)):
            raise ValueError("manifest scopes must have unique scope_id values")
        scopes_by_id = {item.scope_id: item.scope_hash for item in self.scopes}
        coverage_keys: set[str] = set()
        for item in self.product_coverage:
            selector_key = item.model_dump(mode="json")
            for field_name in ("status", "expected_count", "valid_count"):
                selector_key.pop(field_name, None)
            key = _digest_json(selector_key)
            if key in coverage_keys:
                raise ValueError("manifest product coverage selectors must be unique")
            coverage_keys.add(key)
            if item.product_code == "gate_audit":
                if item.scope_id != "AUDIT_RUN_GLOBAL" or item.scope_hash is not None:
                    raise ValueError("gate audit coverage must use the non-spatial AUDIT_RUN_GLOBAL selector")
            elif scopes_by_id.get(item.scope_id) != item.scope_hash:
                raise ValueError("product coverage scope_id/scope_hash must match an immutable manifest scope")
            if item.aggregation == "FRAME":
                if item.frame_index is None or item.frame_index >= len(self.frame_times_s) or item.time_s is None:
                    raise ValueError("FRAME coverage must reference a valid frame_index and time_s")
                if abs(float(item.time_s) - float(self.frame_times_s[item.frame_index])) > _shared_time_tolerance_s(max(abs(float(item.time_s)), abs(float(self.frame_times_s[item.frame_index])))):
                    raise ValueError("FRAME coverage time_s must match frame_times_s[frame_index]")
            if item.window_end_s is not None and item.window_end_s > self.duration_s + _shared_time_tolerance_s(self.duration_s):
                raise ValueError("product coverage time_window cannot extend beyond result duration")
        return self


class SelectorValidation(_RehearsalStrictModel):
    available: StrictBool
    status: Literal["AVAILABLE", "PARTIAL", "UNSUPPORTED", "MISSING_INPUT", "NOT_COMPUTED", "SELECTOR_MISMATCH"]
    reason: Text | None = None


class ValidationIssue(_RehearsalStrictModel):
    code: Text
    path: Text
    message: Text
    severity: Literal["BLOCKER", "WARNING"]


class ProductCapabilityState(_RehearsalStrictModel):
    product_code: ProductCode
    raw_available: StrictBool
    adapter_ready: StrictBool
    quality_passed: StrictBool
    publishable: StrictBool
    reason: Text | None = None


class ScenarioPreflightReport(_RehearsalStrictModel):
    can_save: StrictBool = Field(description="Structural draft readiness only; the service still validates tenant/project ownership and registered region IDs")
    can_compute: StrictBool = Field(description="True only when R1 has independently resolved inputs and a trusted compatible engine/profile")
    can_publish: StrictBool = Field(description="Always false in R1 because result export and publication are not implemented")
    execution_readiness: Literal["READY_FOR_SYNTHETIC_COMPUTE", "NOT_ASSESSED", "BLOCKED"]
    provenance: Literal["SYNTHETIC_MODEL", "USER_INPUT", "NOT_RESOLVED"]
    usage: Literal["TEST_ONLY", "UNASSESSED"]
    production_approved: Literal[False] = False
    scenario_input_sha256: Text
    time_origin_utc: Text | None
    output_times_s: list[Number] = Field(default_factory=list, max_length=MAX_OUTPUT_TIMES)
    output_frame_count: StrictInt = Field(ge=0)
    output_step_is_solver_dt: Literal[False] = False
    downstream_user_input_required: Literal[False] = False
    downstream_applied: Literal[False] = False
    blockers: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    product_capabilities: list[ProductCapabilityState] = Field(default_factory=list)
    proposed_output_times_s: list[Number] | None = None
    resolved_profile_id: Text | None = None
    resolved_profile_sha256: Text | None = None
    resolved_input_sha256: Text | None = None
    resolved_model_case_id: Text | None = None
    scope_id: Text | None = None
    scope_hash: Text | None = None


class ExecutionEnvelope(_RehearsalStrictModel):
    """Platform execution identity; platform IDs never replace legacy local IDs."""

    format: Literal["rehearsal-execution-envelope-v1"]
    tenant_id: Text = Field(description="Trusted service execution context; never accepted from the ordinary scenario form")
    project_id: Text = Field(description="Trusted service execution context")
    scenario_id: Text = Field(description="Platform business identity; separate from model case_id")
    scenario_version_id: Text = Field(description="Immutable platform input version")
    platform_run_id: Text = Field(description="Platform calculation request ID; must not be copied into legacy NetCDF/G1 run_id")
    attempt_id: Text = Field(description="Platform technical execution attempt ID")
    attempt_no: StrictInt = Field(ge=1)
    script_id: Text = Field(description="Backend-assigned per-attempt legacy execution identity, returned unchanged by worker")
    local_run_id: Text = Field(description="Existing local workflow run_id; equals script_id")
    model_case_id: Text = Field(description="Resolved existing model case identity; never substituted with scenario_id")
    input_sha256: Text = Field(description="Digest of the fully resolved immutable input/profile/output-time snapshot")
    engine_mode: Literal["structured_hourly", "triangular_closed_experimental", "triangular_source_experimental"]
    execution_origin: Literal["PLATFORM_MODEL"] = "PLATFORM_MODEL"

    @field_validator("script_id", "local_run_id")
    @classmethod
    def valid_local_script_id(cls, value: str) -> str:
        try:
            return validate_run_id(value)
        except ValueError as exc:
            raise ValueError(f"script/local run ID is invalid: {exc}") from exc

    @field_validator("input_sha256")
    @classmethod
    def valid_input_sha(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("input_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def local_identity_is_legacy_compatible(self) -> "ExecutionEnvelope":
        if self.script_id != self.local_run_id:
            raise ValueError("local_run_id must equal script_id; platform_run_id stays in the platform envelope")
        if self.platform_run_id == self.script_id:
            raise ValueError("platform_run_id must not be reused as the legacy local_run_id/script_id")
        return self


class SyntheticMeshProfile(_RehearsalStrictModel):
    mesh_id: Text
    coordinate_system: Text
    node_xy_m: list[list[Number]] = Field(min_length=3)
    face_nodes: list[list[StrictInt]] = Field(min_length=1)
    face_id: list[StrictInt] = Field(min_length=1)
    boundary_groups: dict[str, list[list[StrictInt]]]
    constraint_groups: dict[str, list[list[StrictInt]]]


class SyntheticTerrainProfile(_RehearsalStrictModel):
    elevation_m: list[Number] = Field(min_length=1)
    vertical_datum: Text
    vertical_datum_source: Literal["config_declaration"]


class SyntheticInitialProfile(_RehearsalStrictModel):
    quantity: Literal["depth"]
    values_m: list[Number] = Field(min_length=1)
    source: Text


class SyntheticRoughnessProfile(_RehearsalStrictModel):
    manning_n: Number = Field(gt=0.0, le=1.0)
    source: Text


class SyntheticClosedProfile(_RehearsalStrictModel):
    format: Literal["rehearsal-synthetic-closed-profile-v1"]
    profile_id: Literal["synthetic_closed_box_v1"]
    profile_version: Literal["1.0"]
    classification: Literal["SYNTHETIC_ONLY"]
    engine_mode: Literal["triangular_closed_experimental"]
    mesh: SyntheticMeshProfile
    terrain: SyntheticTerrainProfile
    initial_condition: SyntheticInitialProfile
    roughness: SyntheticRoughnessProfile
    boundary_policy: Literal["closed_wall"]
    initial_momentum: Literal["zero"]
    sources: Literal["none"]
    scope_id: Literal["SYNTHETIC_CLOSED_SCOPE"]
    region_id: Literal["SYNTHETIC_REGION_1"]


@dataclass(frozen=True)
class _ResolvedSyntheticProfile:
    profile: SyntheticClosedProfile
    profile_sha256: str
    topology_fingerprint: str
    terrain_fingerprint: str
    scope_hash: str
    model_case_id: str


def normalize_output_times(duration_hours: float, output_step_minutes: float) -> list[float]:
    """Normalize a requested output interval; it never selects solver dt."""
    if (isinstance(duration_hours, bool) or not isinstance(duration_hours, (int, float))
            or isinstance(output_step_minutes, bool) or not isinstance(output_step_minutes, (int, float))):
        raise RehearsalContractError("duration_hours and output_step_minutes must be numeric int/float values, not strings or booleans")
    if not math.isfinite(float(duration_hours)) or not math.isfinite(float(output_step_minutes)):
        raise RehearsalContractError("duration_hours and output_step_minutes must be finite numeric values")
    try:
        duration = Decimal(str(duration_hours)) * Decimal(3600)
        step = Decimal(str(output_step_minutes)) * Decimal(60)
    except (InvalidOperation, ValueError) as exc:
        raise RehearsalContractError("duration_hours and output_step_minutes must be finite numeric values") from exc
    if not duration.is_finite() or not step.is_finite() or duration <= 0 or step <= 0:
        raise RehearsalContractError("duration_hours and output_step_minutes must be finite and greater than zero")
    if duration > Decimal(str(MAX_DURATION_S)) or step > Decimal(str(MAX_DURATION_S)):
        raise RehearsalContractError("normalized duration and output step must not exceed 31,536,000 s")
    min_interval = Decimal(str(MIN_DISTINCT_OUTPUT_INTERVAL_S))
    if step < min_interval:
        raise RehearsalContractError(f"output_step_minutes normalizes below the distinct-output minimum {MIN_DISTINCT_OUTPUT_INTERVAL_S:g} s")
    interval_count = int(duration // step)
    exact_terminal = step * interval_count == duration
    point_count = interval_count + 1 if exact_terminal else interval_count + 2
    if point_count > MAX_OUTPUT_TIMES:
        raise RehearsalContractError(f"normalized output axis exceeds the {MAX_OUTPUT_TIMES} point limit")
    times = [step * index for index in range(interval_count + 1)]
    if times[-1] != duration:
        times.append(duration)
    if len(times) < 2 or len(times) > MAX_OUTPUT_TIMES:
        raise RehearsalContractError(f"normalized output axis must contain 2 to {MAX_OUTPUT_TIMES} points")
    gaps = [right - left for left, right in zip(times, times[1:])]
    if any(gap < min_interval for gap in gaps):
        raise RehearsalContractError(f"normalized output intervals must be at least {MIN_DISTINCT_OUTPUT_INTERVAL_S:g} s; adjust duration or output step explicitly")
    floats = [float(value) for value in times]
    if not all(math.isfinite(value) for value in floats) or any(right <= left for left, right in zip(floats, floats[1:])):
        raise RehearsalContractError("normalized output times collapse to duplicate/non-finite floating-point values")
    if floats[-1] != float(duration):
        raise RehearsalContractError("normalized output axis does not retain the exact terminal duration")
    return floats


def _load_synthetic_profile() -> _ResolvedSyntheticProfile:
    try:
        raw_bytes = resources.files("hydrodynamics").joinpath("rehearsal_data/synthetic_closed_box_v1.json").read_bytes()
    except OSError as exc:
        raise RehearsalContractError(f"trusted synthetic profile is unavailable: {exc}") from exc
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if digest != _PROFILE_SHA256:
        raise RehearsalContractError("trusted synthetic profile content hash differs from the pinned R1 fixture")
    try:
        profile = SyntheticClosedProfile.model_validate_json(raw_bytes)
    except ValidationError as exc:
        raise RehearsalContractError(f"trusted synthetic profile violates its strict schema: {exc}") from exc
    mesh_data = profile.mesh
    terrain_data = profile.terrain
    initial_data = profile.initial_condition
    roughness_data = profile.roughness
    try:
        mesh = TriangularMesh(
            node_xy=mesh_data.node_xy_m, face_nodes=mesh_data.face_nodes,
            coordinate_system=mesh_data.coordinate_system, mesh_id=mesh_data.mesh_id,
            face_id=mesh_data.face_id, boundary_groups=mesh_data.boundary_groups,
            constraint_groups=mesh_data.constraint_groups,
        )
        elevation = tuple(float(value) for value in terrain_data.elevation_m)
        if len(elevation) != mesh.n_face:
            raise ValueError("terrain elevation count must match the P2 face count")
        terrain = FaceTerrainField(
            mesh_id=mesh.mesh_id or "", elevation_m=elevation, valid_mask=(True,) * mesh.n_face,
            covered_area_m2=tuple(mesh.face_area), valid_area_m2=tuple(mesh.face_area),
            coverage_ratio=(1.0,) * mesh.n_face, coordinate_system=mesh.coordinate_system,
            vertical_datum=terrain_data.vertical_datum, vertical_datum_source=terrain_data.vertical_datum_source,
        )
        roughness = TriangularRoughnessField.from_constant(mesh, float(roughness_data.manning_n), source=roughness_data.source)
        initial_values = tuple(float(value) for value in initial_data.values_m)
        if len(initial_values) != mesh.n_face:
            raise ValueError("initial depth count must match the P2 face count")
        initial = TriangularInitialConditionField.from_assignments(
            mesh, terrain, initial_data.quantity, tuple(zip(mesh.face_id, initial_values)), source=initial_data.source,
        )
        business = TriangularBusinessPackage(mesh, terrain, roughness, initial, ())
        top_hash = topology_fingerprint(business.mesh)
        bed_hash = terrain_fingerprint(business.mesh, business.terrain)
    except Exception as exc:
        raise RehearsalContractError(f"trusted synthetic profile failed independent P2/P3-A validation: {exc}") from exc
    scope_hash = _digest_json({"profile_sha256": digest, "scope_id": profile.scope_id,
                               "topology_fingerprint": top_hash, "terrain_fingerprint": bed_hash})
    return _ResolvedSyntheticProfile(profile, digest, top_hash, bed_hash, scope_hash, mesh.mesh_id or "")


def resolve_verified_synthetic_profile(profile_ref: ProfileReference) -> _ResolvedSyntheticProfile:
    """Resolve only the fixed packaged synthetic profile; never trust user capability flags."""
    if profile_ref.profile_id != "synthetic_closed_box_v1" or profile_ref.profile_version != "1.0":
        raise RehearsalContractError("profile reference is not present in the trusted synthetic registry")
    return _load_synthetic_profile()


def load_scenario_document(path: str | Path) -> ScenarioDraft:
    source = Path(path).resolve()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RehearsalContractError(f"scenario file not found: {source}") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RehearsalContractError(f"invalid scenario YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise RehearsalContractError("scenario document root must be a YAML mapping")
    try:
        return ScenarioDraft.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors())
        raise RehearsalContractError(f"invalid rehearsal scenario: {details}") from exc


def _source_series(source: UpperBoundarySource) -> BoundarySeriesReference | None:
    return source.series_ref


def _evaluate_source_readiness(scenario: ScenarioDraft, output_times: Sequence[float], issues: list[ValidationIssue]) -> list[float]:
    duration = output_times[-1] if output_times else None
    proposed_additions: set[float] = set()
    for index, source in enumerate(scenario.upper_boundaries):
        path = f"upper_boundaries[{index}]"
        series = _source_series(source)
        required_fields: dict[str, tuple[str, ...]] = {
            "FORECAST": ("forecast_dataset_id", "forecast_version_id", "issued_at_local", "issued_time_zone"),
            "DESIGN": ("design_dataset_id", "design_version_id", "design_method_id", "return_period_years", "approval_ref"),
            "HISTORICAL": ("event_id", "observation_dataset_id", "observation_version_id", "observation_kind"),
            "MANUAL": ("rationale",),
        }
        missing = [name for name in required_fields[source.source_mode] if getattr(source, name, None) is None]
        if missing:
            issues.append(ValidationIssue(code="BOUNDARY_SOURCE_METADATA_MISSING", path=path,
                                          message=f"{source.source_mode} source is missing {', '.join(missing)}", severity="BLOCKER"))
        if series is None:
            issues.append(ValidationIssue(code="BOUNDARY_SERIES_UNRESOLVED", path=f"{path}.series_ref",
                                          message="boundary series reference/data is not resolved", severity="BLOCKER"))
            continue
        if series.points is None:
            issues.append(ValidationIssue(code="BOUNDARY_SERIES_UNRESOLVED", path=f"{path}.series_ref",
                                          message="dataset reference has no trusted parser result/content binding", severity="BLOCKER"))
            continue
        if duration is not None:
            tolerance = _shared_time_tolerance_s(duration)
            if abs(series.points[0].time_s) > _shared_time_tolerance_s(series.points[0].time_s) or abs(series.points[-1].time_s - duration) > tolerance:
                issues.append(ValidationIssue(code="BOUNDARY_SERIES_COVERAGE_GAP", path=f"{path}.series_ref.points",
                                              message="boundary series must explicitly cover the complete scenario interval [0,duration]", severity="BLOCKER"))
            axis = list(output_times)
            unmatched = [point.time_s for point in series.points
                         if not any(abs(point.time_s - output_time) <= _shared_time_tolerance_s(max(abs(point.time_s), abs(output_time))) for output_time in axis)]
            if unmatched:
                proposed_additions.update(float(point.time_s) for point in series.points if point.time_s in unmatched)
                issues.append(ValidationIssue(code="SOURCE_BREAKPOINT_NOT_ON_OUTPUT_AXIS", path=f"{path}.series_ref.points",
                                              message=f"source breakpoints require explicit output-axis confirmation; proposed additions={unmatched}", severity="BLOCKER"))
    return sorted(proposed_additions)


def _product_capabilities(engine_mode: str | None, requested: Sequence[ProductRequest]) -> tuple[list[ProductCapabilityState], list[ValidationIssue]]:
    raw_by_engine: dict[str, frozenset[str]] = {
        "triangular_closed_experimental": frozenset({"water_depth", "water_level", "velocity", "maximum_depth", "wet_area", "storage_volume"}),
        "triangular_source_experimental": frozenset({"water_depth", "water_level", "velocity", "maximum_depth", "wet_area", "storage_volume"}),
        "structured_hourly": frozenset({"water_depth", "water_level", "velocity", "maximum_depth", "wet_area", "storage_volume", "first_arrival", "gate_audit"}),
    }
    known_raw = raw_by_engine.get(engine_mode or "", frozenset())
    states: list[ProductCapabilityState] = []
    blockers: list[ValidationIssue] = []
    for item in requested:
        raw_available = item.product_code in known_raw
        states.append(ProductCapabilityState(
            product_code=item.product_code, raw_available=raw_available, adapter_ready=False,
            quality_passed=False, publishable=False,
            reason=None if raw_available else "NO_TRUSTED_ENGINE_CAPABILITY",
        ))
        if not raw_available and item.required:
            blockers.append(ValidationIssue(code="ENGINE_PRODUCT_UNSUPPORTED", path="requested_products",
                                            message=f"required product {item.product_code} is not in the trusted capability registry for {engine_mode or 'unselected engine'}",
                                            severity="BLOCKER"))
    return states, blockers


def evaluate_scenario(scenario: ScenarioDraft) -> ScenarioPreflightReport:
    """Return save/compute/publish readiness using only package-owned evidence."""
    blockers: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    can_save = bool(scenario.scenario_name.strip() and scenario.region_ids)
    if not scenario.region_ids:
        blockers.append(ValidationIssue(code="REGION_SCOPE_REQUIRED", path="region_ids",
                                        message="select at least one stable region ID before saving", severity="BLOCKER"))
    output_times: list[float] = []
    if scenario.duration_hours is None or scenario.output_step_minutes is None:
        blockers.append(ValidationIssue(code="OUTPUT_AXIS_INCOMPLETE", path="duration_hours/output_step_minutes",
                                        message="duration_hours and output_step_minutes are both required for computation", severity="BLOCKER"))
    else:
        try:
            output_times = normalize_output_times(scenario.duration_hours, scenario.output_step_minutes)
        except RehearsalContractError as exc:
            blockers.append(ValidationIssue(code="OUTPUT_AXIS_INVALID", path="duration_hours/output_step_minutes",
                                            message=str(exc), severity="BLOCKER"))
    try:
        origin_utc = None if scenario.time_origin_local is None else _validate_local_datetime(
            scenario.time_origin_local, scenario.time_zone, field="time_origin_local",
        )
    except ValueError as exc:
        origin_utc = None
        blockers.append(ValidationIssue(code="TIME_ORIGIN_INVALID", path="time_origin_local/time_zone",
                                        message=str(exc), severity="BLOCKER"))
    if scenario.time_origin_local is None:
        warnings.append(ValidationIssue(code="TIME_ORIGIN_UNKNOWN", path="time_origin_local",
                                        message="absolute event dates are unavailable; retain elapsed seconds only", severity="WARNING"))
    if scenario.engine_mode is None:
        blockers.append(ValidationIssue(code="ENGINE_MODE_REQUIRED", path="engine_mode",
                                        message="select a trusted engine mode before calculation", severity="BLOCKER"))
    if scenario.input_profile_ref is None:
        blockers.append(ValidationIssue(code="INPUT_PROFILE_UNRESOLVED", path="input_profile_ref",
                                        message="terrain/grid/initial/roughness profile is not resolved", severity="BLOCKER"))
    if scenario.region_ids and scenario.input_profile_ref is None:
        warnings.append(ValidationIssue(code="REGION_RESOLUTION_NOT_ASSESSED", path="region_ids",
                                        message="R1 has no trusted business-region resolver; the service must verify region ownership/mapping before persistence or computation", severity="WARNING"))
    if scenario.initial_condition.mode == "UNKNOWN":
        blockers.append(ValidationIssue(code="INITIAL_CONDITION_UNKNOWN", path="initial_condition",
                                        message="initial state profile is unresolved", severity="BLOCKER"))
    if scenario.rainfall.mode == "UNKNOWN":
        blockers.append(ValidationIssue(code="RAINFALL_UNKNOWN", path="rainfall",
                                        message="choose a rainfall profile or explicitly confirm no rainfall", severity="BLOCKER"))
    if scenario.metric_policy_version is None:
        blockers.append(ValidationIssue(code="METRIC_POLICY_REQUIRED", path="metric_policy_version",
                                        message="metric policy version must be explicitly selected", severity="BLOCKER"))
    if not scenario.requested_products:
        blockers.append(ValidationIssue(code="PRODUCT_REQUEST_REQUIRED", path="requested_products",
                                        message="select at least one product profile", severity="BLOCKER"))
    if scenario.rule_snapshot_ref is not None and scenario.rule_snapshot_ref.rule_usage == "EVALUATE_APPROVED_RULE":
        blockers.append(ValidationIssue(code="RULE_EVALUATION_NOT_IMPLEMENTED", path="rule_snapshot_ref.rule_usage",
                                        message="R1 can retain a rule reference but cannot evaluate or apply gate actions", severity="BLOCKER"))
    if scenario.creation_type == "BUILTIN_CLONE" and scenario.template_ref is not None:
        try:
            catalog = load_builtin_rehearsal_catalog()
            template = next(item for item in catalog.templates if item.template_key == scenario.template_ref.template_key)
            if scenario.template_ref.catalog_version != catalog.catalog_version:
                blockers.append(ValidationIssue(code="TEMPLATE_CATALOG_VERSION_MISMATCH", path="template_ref.catalog_version",
                                                message="the clone references a catalog version that is not packaged in this R1 build", severity="BLOCKER"))
            if not set(scenario.historical_event_refs).issubset(set(template.event_keys)):
                blockers.append(ValidationIssue(code="TEMPLATE_EVENT_LINK_MISMATCH", path="historical_event_refs",
                                                message="cloned history event refs must be a subset of the selected template's immutable event list", severity="BLOCKER"))
        except (RehearsalCatalogError, StopIteration) as exc:
            blockers.append(ValidationIssue(code="TEMPLATE_REF_UNRESOLVED", path="template_ref",
                                            message=f"built-in template reference cannot be resolved: {exc}", severity="BLOCKER"))

    resolved: _ResolvedSyntheticProfile | None = None
    if scenario.input_profile_ref is not None and scenario.input_profile_ref.profile_id == "synthetic_closed_box_v1":
        try:
            candidate = _load_synthetic_profile()
            if candidate.profile.profile_version != scenario.input_profile_ref.profile_version:
                blockers.append(ValidationIssue(code="INPUT_PROFILE_VERSION_MISMATCH", path="input_profile_ref.profile_version",
                                                message="requested version differs from the pinned synthetic profile", severity="BLOCKER"))
            else:
                resolved = candidate
        except RehearsalContractError as exc:
            blockers.append(ValidationIssue(code="TRUSTED_PROFILE_INVALID", path="input_profile_ref",
                                            message=str(exc), severity="BLOCKER"))
    elif scenario.input_profile_ref is not None:
        blockers.append(ValidationIssue(code="INPUT_PROFILE_NOT_IN_TRUSTED_REGISTRY", path="input_profile_ref.profile_id",
                                        message="no trusted parser/profile resolver is registered for this input profile", severity="BLOCKER"))

    if scenario.downstream_policy.mode == "DEFERRED_UNKNOWN":
        blockers.append(ValidationIssue(code="DOWNSTREAM_DATA_UNRESOLVED", path="downstream_policy",
                                        message="lower-boundary policy remains unknown; this is not zero flow, zero stage, free outflow, or closed wall", severity="BLOCKER"))
    elif resolved is None or resolved.profile.classification != "SYNTHETIC_ONLY":
        blockers.append(ValidationIssue(code="SYNTHETIC_CLOSED_PROFILE_REQUIRED", path="downstream_policy",
                                        message="closed_wall is allowed only when the trusted profile is the packaged synthetic closed-domain fixture", severity="BLOCKER"))
    elif scenario.downstream_policy.profile_ref != scenario.input_profile_ref:
        blockers.append(ValidationIssue(code="DOWNSTREAM_PROFILE_MISMATCH", path="downstream_policy.profile_ref",
                                        message="synthetic closed-domain policy must reference the same verified profile", severity="BLOCKER"))

    if resolved is not None:
        if scenario.engine_mode != resolved.profile.engine_mode:
            blockers.append(ValidationIssue(code="ENGINE_PROFILE_MISMATCH", path="engine_mode",
                                            message="selected engine mode does not match the trusted synthetic profile", severity="BLOCKER"))
        if scenario.region_ids != [resolved.profile.region_id]:
            blockers.append(ValidationIssue(code="SYNTHETIC_SCOPE_MISMATCH", path="region_ids",
                                            message="the synthetic fixture is registered only for SYNTHETIC_REGION_1", severity="BLOCKER"))
        if scenario.initial_condition.mode != "PROFILE_REF" or scenario.initial_condition.profile_ref != scenario.input_profile_ref:
            blockers.append(ValidationIssue(code="INITIAL_PROFILE_MISMATCH", path="initial_condition.profile_ref",
                                            message="the synthetic profile includes the required initial depth field", severity="BLOCKER"))
        if scenario.rainfall.mode != "EXPLICIT_NONE":
            blockers.append(ValidationIssue(code="SYNTHETIC_RAINFALL_PROFILE_MISMATCH", path="rainfall",
                                            message="the pinned synthetic fixture has an explicit no-rain source profile", severity="BLOCKER"))
        if scenario.upper_boundaries:
            blockers.append(ValidationIssue(code="SYNTHETIC_CLOSED_PROFILE_HAS_OPEN_BOUNDARY", path="upper_boundaries",
                                            message="the pinned synthetic closed-domain fixture has no open-boundary groups", severity="BLOCKER"))
        if scenario.rule_snapshot_ref is not None and scenario.rule_snapshot_ref.rule_usage == "REFERENCE_ONLY":
            warnings.append(ValidationIssue(code="RULE_REFERENCE_NOT_APPLIED", path="rule_snapshot_ref",
                                            message="R1 retains a rule reference only; no gate operation or physical control is executed", severity="WARNING"))
        if scenario.metric_policy_version != "synthetic-demo-policy-v1":
            blockers.append(ValidationIssue(code="METRIC_POLICY_NOT_REGISTERED", path="metric_policy_version",
                                            message="only synthetic-demo-policy-v1 is registered for this packaged fixture", severity="BLOCKER"))

    proposed_output_times: list[float] = []
    if scenario.upper_boundaries:
        proposed_output_times = _evaluate_source_readiness(scenario, output_times, blockers)
        if scenario.engine_mode in {"triangular_closed_experimental", "triangular_source_experimental"}:
            blockers.append(ValidationIssue(code="TRIANGULAR_OPEN_BOUNDARY_UNSUPPORTED", path="upper_boundaries",
                                            message="the accepted triangular execution route remains closed-wall; boundary preparation does not apply an open boundary", severity="BLOCKER"))

    capabilities, product_blockers = _product_capabilities(scenario.engine_mode, scenario.requested_products)
    blockers.extend(product_blockers)
    if resolved is not None:
        for request in scenario.requested_products:
            if request.scope_id != resolved.profile.scope_id:
                issue = ValidationIssue(code="PRODUCT_SCOPE_UNRESOLVED", path="requested_products",
                                        message=f"scope {request.scope_id} is not part of the verified synthetic profile", severity="BLOCKER")
                if request.required:
                    blockers.append(issue)
                else:
                    warnings.append(ValidationIssue(**{**issue.model_dump(), "severity": "WARNING"}))
    if not scenario.historical_event_refs:
        pass
    else:
        try:
            catalog = load_builtin_rehearsal_catalog()
            event_keys = {event.event_key for event in catalog.events}
            unknown_events = sorted(set(scenario.historical_event_refs) - event_keys)
            if unknown_events:
                blockers.append(ValidationIssue(code="HISTORICAL_EVENT_NOT_IN_CATALOG", path="historical_event_refs",
                                                message=f"unknown built-in event keys: {', '.join(unknown_events)}", severity="BLOCKER"))
            else:
                warnings.append(ValidationIssue(code="HISTORY_IS_NOT_BOUNDARY_INPUT", path="historical_event_refs",
                                                message="public history events are display/evidence only and cannot create a continuous boundary series", severity="WARNING"))
                for event_key in scenario.historical_event_refs:
                    event = next(item for item in catalog.events if item.event_key == event_key)
                    if event.mapped_region_id is None:
                        warnings.append(ValidationIssue(code="HISTORICAL_SCOPE_UNMAPPED", path=f"historical_event_refs.{event_key}",
                                                        message=f"event scope alias {event.scope_alias} is not mapped to a trusted region ID and will not be joined to region_ids", severity="WARNING"))
        except RehearsalCatalogError as exc:
            blockers.append(ValidationIssue(code="BUILTIN_CATALOG_INVALID", path="historical_event_refs", message=str(exc), severity="BLOCKER"))

    can_compute = bool(can_save and output_times and resolved is not None and not blockers)
    readiness = "READY_FOR_SYNTHETIC_COMPUTE" if can_compute else ("NOT_ASSESSED" if resolved is None else "BLOCKED")
    if resolved is not None:
        provenance = "SYNTHETIC_MODEL"
        usage = "TEST_ONLY"
        warnings.append(ValidationIssue(code="SYNTHETIC_ONLY", path="input_profile_ref",
                                        message="this fixture is for synthetic preflight/contract checks, not an operational forecast", severity="WARNING"))
    elif any(source.source_mode == "MANUAL" for source in scenario.upper_boundaries):
        provenance = "USER_INPUT"
        usage = "UNASSESSED"
    else:
        provenance = "NOT_RESOLVED"
        usage = "UNASSESSED"
    warnings.append(ValidationIssue(code="RESULT_ADAPTER_NOT_IMPLEMENTED", path="requested_products",
                                    message="R1 does not export/publish a result_set; can_publish remains false", severity="WARNING"))
    input_hash = _digest_json(scenario.model_dump(mode="json"))
    resolved_input_hash = None
    if can_compute and resolved is not None:
        resolved_input_hash = _digest_json({
            "scenario_input_sha256": input_hash,
            "output_times_s": output_times,
            "profile_sha256": resolved.profile_sha256,
            "topology_fingerprint": resolved.topology_fingerprint,
            "terrain_fingerprint": resolved.terrain_fingerprint,
        })
    return ScenarioPreflightReport(
        can_save=can_save, can_compute=can_compute, can_publish=False, execution_readiness=readiness,
        provenance=provenance, usage=usage, scenario_input_sha256=input_hash,
        time_origin_utc=origin_utc, output_times_s=output_times, output_frame_count=len(output_times),
        downstream_user_input_required=False, downstream_applied=False,
        blockers=blockers, warnings=warnings, product_capabilities=capabilities,
        proposed_output_times_s=proposed_output_times or None,
        resolved_profile_id=resolved.profile.profile_id if resolved else None,
        resolved_profile_sha256=resolved.profile_sha256 if resolved else None,
        resolved_input_sha256=resolved_input_hash,
        resolved_model_case_id=resolved.model_case_id if resolved else None,
        scope_id=resolved.profile.scope_id if resolved else None,
        scope_hash=resolved.scope_hash if resolved else None,
    )


def validate_execution_envelope(
    envelope: ExecutionEnvelope,
    scenario: ScenarioDraft,
    report: ScenarioPreflightReport,
    *,
    trusted_tenant_id: str,
    trusted_project_id: str,
    trusted_scenario_id: str,
    trusted_scenario_version_id: str,
    trusted_platform_run_id: str,
    trusted_attempt_id: str,
    trusted_attempt_no: int,
    trusted_script_id: str,
) -> None:
    actual_report = evaluate_scenario(scenario)
    if report.model_dump(mode="json") != actual_report.model_dump(mode="json"):
        raise RehearsalContractError("preflight report is stale, self-asserted, or does not correspond to the current scenario")
    if not actual_report.can_compute:
        raise RehearsalContractError("execution envelope cannot be accepted because scenario preflight can_compute is false")
    if actual_report.resolved_input_sha256 is None or actual_report.resolved_model_case_id is None:
        raise RehearsalContractError("current preflight does not contain trusted resolved input/model identity")
    validate_execution_identity(
        envelope,
        trusted_tenant_id=trusted_tenant_id,
        trusted_project_id=trusted_project_id,
        trusted_scenario_id=trusted_scenario_id,
        trusted_scenario_version_id=trusted_scenario_version_id,
        trusted_platform_run_id=trusted_platform_run_id,
        trusted_attempt_id=trusted_attempt_id,
        trusted_attempt_no=trusted_attempt_no,
        trusted_script_id=trusted_script_id,
        trusted_model_case_id=actual_report.resolved_model_case_id,
        trusted_input_sha256=actual_report.resolved_input_sha256,
        trusted_engine_mode=scenario.engine_mode,
    )


def validate_execution_identity(
    envelope: ExecutionEnvelope,
    *,
    trusted_tenant_id: str,
    trusted_project_id: str,
    trusted_scenario_id: str,
    trusted_scenario_version_id: str,
    trusted_platform_run_id: str,
    trusted_attempt_id: str,
    trusted_attempt_no: int,
    trusted_script_id: str,
    trusted_model_case_id: str,
    trusted_input_sha256: str,
    trusted_engine_mode: str,
) -> None:
    """Compare an envelope to separate trusted dispatch values, never to itself.

    R2A may reuse this identity-only validator with an explicitly labeled local
    source-graph assignment. R1 platform execution additionally uses
    ``validate_execution_envelope`` to recompute and verify the preflight.
    """
    if envelope.tenant_id != trusted_tenant_id or envelope.project_id != trusted_project_id:
        raise RehearsalContractError("execution envelope tenant/project does not match trusted service context")
    if envelope.scenario_id != trusted_scenario_id or envelope.scenario_version_id != trusted_scenario_version_id:
        raise RehearsalContractError("execution envelope scenario/version does not match trusted platform identity")
    if envelope.platform_run_id != trusted_platform_run_id:
        raise RehearsalContractError("execution envelope platform_run_id differs from the backend assignment")
    if envelope.attempt_id != trusted_attempt_id or envelope.attempt_no != trusted_attempt_no:
        raise RehearsalContractError("execution envelope attempt identity differs from the backend assignment")
    try:
        assigned_script_id = validate_run_id(trusted_script_id)
    except ValueError as exc:
        raise RehearsalContractError(f"backend-assigned script_id is invalid: {exc}") from exc
    if envelope.script_id != assigned_script_id or envelope.local_run_id != assigned_script_id:
        raise RehearsalContractError("execution envelope script/local run ID differs from the backend assignment")
    if envelope.input_sha256 != trusted_input_sha256:
        raise RehearsalContractError("execution envelope input_sha256 differs from the trusted resolved-input digest")
    if envelope.engine_mode != trusted_engine_mode:
        raise RehearsalContractError("execution envelope engine_mode differs from trusted dispatch context")
    if envelope.model_case_id != trusted_model_case_id:
        raise RehearsalContractError("model_case_id differs from trusted model case identity")


def validate_selector_against_manifest(selector: ResultProductSelector, manifest: ResultSetManifest) -> SelectorValidation:
    if selector.result_set_id != manifest.result_set_id:
        return SelectorValidation(available=False, status="SELECTOR_MISMATCH", reason="result_set_id mismatch")
    if selector.metric_policy_version != manifest.metric_policy_version:
        return SelectorValidation(available=False, status="SELECTOR_MISMATCH", reason="metric_policy_version mismatch")
    if selector.aggregation == "FRAME":
        if selector.frame_index is None or selector.frame_index >= len(manifest.frame_times_s):
            return SelectorValidation(available=False, status="SELECTOR_MISMATCH", reason="frame_index is outside the manifest frame axis")
        manifest_time = float(manifest.frame_times_s[selector.frame_index])
        if abs(float(selector.time_s) - manifest_time) > _shared_time_tolerance_s(max(abs(float(selector.time_s)), abs(manifest_time))):
            return SelectorValidation(available=False, status="SELECTOR_MISMATCH", reason="time_s does not match frame_times_s[frame_index]")
    elif selector.window_start_s is not None and selector.window_end_s is not None:
        if selector.window_end_s > manifest.duration_s + _shared_time_tolerance_s(manifest.duration_s):
            return SelectorValidation(available=False, status="SELECTOR_MISMATCH", reason="time_window extends outside the result set")
    if selector.scope_id != "AUDIT_RUN_GLOBAL":
        scope = next((item for item in manifest.scopes if item.scope_id == selector.scope_id), None)
        if scope is None or scope.scope_hash != selector.scope_hash:
            return SelectorValidation(available=False, status="SELECTOR_MISMATCH", reason="scope_id/scope_hash does not match the immutable manifest scope")
    elif selector.product_code != "gate_audit":
        return SelectorValidation(available=False, status="SELECTOR_MISMATCH", reason="AUDIT_RUN_GLOBAL is not a spatial result scope")

    def same_time(left: float | None, right: float | None) -> bool:
        if left is None or right is None:
            return left is None and right is None
        return abs(float(left) - float(right)) <= _shared_time_tolerance_s(max(abs(float(left)), abs(float(right))))

    expected_window = (selector.window_start_s, selector.window_end_s)
    for item in manifest.product_coverage:
        if (item.product_code == selector.product_code and item.scope_id == selector.scope_id
                and item.scope_hash == selector.scope_hash and item.aggregation == selector.aggregation
                and item.frame_index == selector.frame_index and item.metric_policy_version == selector.metric_policy_version
                and item.object_id == selector.object_id
                and same_time(item.window_start_s, expected_window[0]) and same_time(item.window_end_s, expected_window[1])
                and same_time(item.time_s, selector.time_s)):
            if item.status == "COMPLETE":
                return SelectorValidation(available=True, status="AVAILABLE", reason=None)
            return SelectorValidation(available=False, status=item.status, reason="product coverage is not COMPLETE for this exact selector")
    return SelectorValidation(available=False, status="SELECTOR_MISMATCH", reason="manifest has no coverage record for this exact product selector")


def _format_validation_error(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors())


def _atomic_write_new(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir():
        raise RehearsalContractError(f"output parent directory does not exist: {path.parent}")
    if path.exists() or path.is_symlink():
        raise RehearsalContractError(f"refusing to overwrite existing output: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        temporary.write_bytes(payload)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        if os.name == "nt":
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
            temporary.unlink()
    except FileExistsError as exc:
        temporary.unlink(missing_ok=True)
        raise RehearsalContractError(f"output appeared before publication; refusing to overwrite: {path}") from exc
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def export_schemas(model: Literal["scenario", "execution-envelope", "product-selector", "preflight", "all"] = "all") -> dict[str, Any]:
    models: dict[str, type[BaseModel]] = {
        "scenario": ScenarioDraft,
        "execution-envelope": ExecutionEnvelope,
        "product-selector": ResultProductSelector,
        "preflight": ScenarioPreflightReport,
    }
    if model == "all":
        return {name: schema.model_json_schema() for name, schema in models.items()}
    return models[model].model_json_schema()


def _clone_scenario(template_key: str) -> ScenarioDraft:
    try:
        raw = clone_builtin_template(template_key)
        return ScenarioDraft.model_validate(raw)
    except (RehearsalCatalogError, ValidationError) as exc:
        raise RehearsalContractError(f"cannot clone built-in template: {exc}") from exc


def _report_dict(scenario: ScenarioDraft) -> dict[str, Any]:
    return evaluate_scenario(scenario).model_dump(mode="json")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate rehearsal contracts and inspect read-only public templates; does not run a model or connect to a database.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    schema_parser = subparsers.add_parser("schema", help="export formal JSON Schemas")
    schema_parser.add_argument("--model", choices=("scenario", "execution-envelope", "product-selector", "preflight", "all"), default="all")
    schema_parser.add_argument("--output", type=Path)

    catalog_parser = subparsers.add_parser("catalog", help="list or show the immutable S1-S4 public-history catalog")
    catalog_sub = catalog_parser.add_subparsers(dest="catalog_command", required=True)
    list_parser = catalog_sub.add_parser("list")
    list_parser.add_argument("--kind", choices=("sources", "events", "templates"), default="templates")
    show_parser = catalog_sub.add_parser("show")
    show_parser.add_argument("kind", choices=("sources", "events", "templates"))
    show_parser.add_argument("key")

    template_parser = subparsers.add_parser("template", help="clone a non-runnable built-in template as a draft")
    template_sub = template_parser.add_subparsers(dest="template_command", required=True)
    clone_parser = template_sub.add_parser("clone")
    clone_parser.add_argument("template_key")
    clone_parser.add_argument("--output", required=True, type=Path)

    validate_parser = subparsers.add_parser("validate", help="validate a draft or preflight its calculation readiness")
    validate_parser.add_argument("scenario", type=Path)
    validate_parser.add_argument("--mode", choices=("draft", "preflight"), default="preflight")
    validate_parser.add_argument("--require-compute", action="store_true", help="return exit code 3 if preflight cannot compute")

    args = parser.parse_args(argv)
    try:
        if args.command == "schema":
            raw = json.dumps(export_schemas(args.model), ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8") + b"\n"
            if args.output:
                output_path = args.output.expanduser().absolute()
                _atomic_write_new(output_path, raw)
                result: Any = {"status": "exported", "path": str(output_path), "model": args.model}
            else:
                sys.stdout.write(raw.decode("utf-8"))
                return 0
        elif args.command == "catalog":
            if args.catalog_command == "list":
                result = {"items": list_catalog_items(args.kind)}
            else:
                result = show_catalog_item(args.kind, args.key)
        elif args.command == "template":
            scenario = _clone_scenario(args.template_key)
            raw = yaml.safe_dump(scenario.model_dump(mode="json", exclude_none=False), allow_unicode=True, sort_keys=False).encode("utf-8")
            output_path = args.output.expanduser().absolute()
            _atomic_write_new(output_path, raw)
            result = {"status": "cloned_draft", "template_key": args.template_key, "path": str(output_path),
                      "runnable": False, "can_compute": False}
        else:
            scenario = load_scenario_document(args.scenario)
            if args.mode == "draft":
                result = {"status": "valid_draft_schema", "scenario_input_sha256": _digest_json(scenario.model_dump(mode="json")),
                          "can_save": bool(scenario.scenario_name.strip() and scenario.region_ids),
                          "can_compute": False, "can_publish": False,
                          "draft_blockers": ([] if scenario.region_ids else ["REGION_SCOPE_REQUIRED"])}
            else:
                report = evaluate_scenario(scenario)
                result = report.model_dump(mode="json")
                if args.require_compute and not report.can_compute:
                    result["status"] = "preflight_blocked"
                    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
                    return 3
        sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
        return 0
    except (RehearsalContractError, RehearsalCatalogError, OSError, ValidationError, yaml.YAMLError) as exc:
        sys.stderr.write(f"rehearsal {args.command} failed: {exc}\n")
        return 2


__all__ = [
    "DownstreamPolicy", "ExecutionEnvelope", "InitialConditionInput", "ProductRequest", "RainfallInput",
    "ResultProductSelector", "ResultSetManifest", "RehearsalContractError", "ScenarioDraft",
    "ScenarioPreflightReport", "UpperBoundarySource", "evaluate_scenario", "export_schemas",
    "load_scenario_document", "main", "normalize_output_times", "validate_execution_envelope",
    "resolve_verified_synthetic_profile", "validate_execution_identity", "validate_selector_against_manifest",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
