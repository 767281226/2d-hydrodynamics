"""Read-only public-history and scenario-template catalog for rehearsal R1.

Catalog entries are evidence/display references only. Historical events cannot
be used as continuous boundary series or gate commands, and built-in templates
are never runnable.
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator

from .config import Number, Text


class RehearsalCatalogError(ValueError):
    """Raised when the packaged built-in catalog is missing or inconsistent."""


class _CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, allow_inf_nan=False)


class CatalogSource(_CatalogModel):
    source_id: Literal["S1", "S2", "S3", "S4"]
    title: Text
    publisher: Text
    original_publisher: Text | None = None
    url: Text
    published_at_local: Text
    published_at_precision: Literal["MINUTE", "HOUR", "DATE"]
    time_zone: Text
    source_timezone_explicit: Literal[False] = False
    accessed_on: Text
    retrieval_status: Literal["BODY_REVIEWED"]
    review_status: Literal["VERIFIED_BODY"]
    content_sha256: Text | None = None
    excerpt_locator: Text
    summary: Text

    @field_validator("url")
    @classmethod
    def secure_public_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("catalog source URL must be a public HTTPS URL without embedded credentials")
        return value

    @field_validator("time_zone")
    @classmethod
    def fixed_report_timezone(cls, value: str) -> str:
        if value != "Asia/Shanghai":
            raise ValueError("R1 public-report catalog only normalizes the explicitly documented Asia/Shanghai assumption")
        return value

    @model_validator(mode="after")
    def publication_timestamp_is_local(self) -> "CatalogSource":
        try:
            datetime.strptime(self.published_at_local, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError("published_at_local must use yyyy-MM-dd HH:mm:ss and is not the source event time") from exc
        return self

    @field_validator("content_sha256")
    @classmethod
    def optional_sha256(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest when source bytes are retained")
        return value


class CatalogMeasurement(_CatalogModel):
    code: Text
    value: Number
    unit: Literal["m", "m3/s"]
    vertical_datum: Text | None = None

    @model_validator(mode="after")
    def datum_only_for_stage(self) -> "CatalogMeasurement":
        if self.unit == "m3/s" and self.vertical_datum is not None:
            raise ValueError("discharge observations must not carry a vertical datum")
        if self.unit == "m" and self.vertical_datum is None:
            raise ValueError("absolute water-level observations require an explicit vertical datum")
        return self


class CatalogEvent(_CatalogModel):
    event_key: Text
    event_type: Literal["REPORTED_ACTION", "OBSERVATION_REPORT", "DECISION_EFFECTIVE", "STATUS_REPORT"]
    raw_time_text: Text
    event_time_local: Text | None = None
    reported_at_local: Text
    reported_at_time_zone: Literal["Asia/Shanghai"]
    reported_timezone_explicit: Literal[False] = False
    time_precision: Literal["MINUTE", "HOUR", "DATE", "UNKNOWN"]
    normalization_policy: Literal[
        "ASSUMED_ASIA_SHANGHAI_FOR_CHINA_PUBLIC_REPORT",
        "NO_EVENT_TIME_INFERENCE",
    ]
    time_zone: Literal["Asia/Shanghai"]
    source_timezone_explicit: Literal[False] = False
    scope_alias: Text
    mapped_region_id: None = None
    description: Text
    measurements: list[CatalogMeasurement] = Field(default_factory=list)
    source_refs: list[Literal["S1", "S2", "S3", "S4"]] = Field(min_length=1)
    evidence_stage: Literal["PUBLIC_REPORT"]
    missing_fields: list[Text] = Field(default_factory=list)
    can_drive_model: Literal[False] = False
    gate_commands_sent: Literal[0] = 0

    @model_validator(mode="after")
    def preserve_event_time_evidence(self) -> "CatalogEvent":
        try:
            datetime.strptime(self.reported_at_local, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError("reported_at_local must use yyyy-MM-dd HH:mm:ss") from exc
        if self.time_precision == "UNKNOWN":
            if self.event_time_local is not None or self.normalization_policy != "NO_EVENT_TIME_INFERENCE":
                raise ValueError("unknown event time must stay null and use NO_EVENT_TIME_INFERENCE")
        else:
            if self.event_time_local is None or self.normalization_policy != "ASSUMED_ASIA_SHANGHAI_FOR_CHINA_PUBLIC_REPORT":
                raise ValueError("reported event time must preserve local time and its explicit normalization policy")
            try:
                event_time = datetime.strptime(self.event_time_local, "%Y-%m-%d %H:%M:%S")
            except ValueError as exc:
                raise ValueError("event_time_local must use yyyy-MM-dd HH:mm:ss") from exc
            if self.time_precision == "DATE" and (event_time.hour or event_time.minute or event_time.second):
                raise ValueError("DATE precision must not claim a time of day")
            if self.time_precision == "HOUR" and (event_time.minute or event_time.second):
                raise ValueError("HOUR precision must be normalized to the start of the stated hour")
            if self.time_precision == "MINUTE" and event_time.second:
                raise ValueError("MINUTE precision must not claim seconds")
        return self


class CatalogTemplate(_CatalogModel):
    template_key: Literal[
        "TJ_2023_DONGDIAN_PUBLIC_HISTORY_V1",
        "TJ_2023_YONGDING_PUBLIC_HISTORY_V1",
        "TJ_2025_ECO_WATER_REPLENISHMENT_V1",
        "TJ_2025_FLOOD_JOINT_THEME_V1",
        "DESIGN_FLOOD_PARAMETERIZED_V1",
        "FORECAST_ROLLING_V1",
        "MANUAL_COMPARISON_V1",
    ]
    display_name: Text
    aliases: list[Text] = Field(default_factory=list)
    template_kind: Literal["PUBLIC_HISTORY_PARTIAL", "PUBLIC_THEME_PARTIAL", "DESIGN_SHELL", "FORECAST_SHELL", "MANUAL_SHELL"]
    scope_aliases: list[Text] = Field(default_factory=list)
    source_ids: list[Literal["S1", "S2", "S3", "S4"]] = Field(default_factory=list)
    event_keys: list[Text] = Field(default_factory=list)
    runnable: Literal[False] = False
    engineering_validated: Literal[False] = False
    needs: list[Text] = Field(min_length=1)


class BuiltInRehearsalCatalog(_CatalogModel):
    format: Literal["rehearsal-built-in-catalog-v1"]
    catalog_version: Text
    sources: list[CatalogSource] = Field(min_length=4)
    events: list[CatalogEvent] = Field(min_length=1)
    templates: list[CatalogTemplate] = Field(min_length=7)

    @model_validator(mode="after")
    def catalog_links_are_closed(self) -> "BuiltInRehearsalCatalog":
        source_ids = [item.source_id for item in self.sources]
        event_keys = [item.event_key for item in self.events]
        template_keys = [item.template_key for item in self.templates]
        if set(source_ids) != {"S1", "S2", "S3", "S4"} or len(source_ids) != len(set(source_ids)):
            raise ValueError("the enabled public source catalog must contain exactly unique verified sources S1-S4")
        if len(event_keys) != len(set(event_keys)) or len(template_keys) != len(set(template_keys)):
            raise ValueError("catalog event_key and template_key values must be unique")
        known_sources = set(source_ids)
        known_events = set(event_keys)
        if any(not set(event.source_refs).issubset(known_sources) for event in self.events):
            raise ValueError("each event source_refs entry must identify an enabled S1-S4 source")
        if any(not set(template.source_ids).issubset(known_sources) for template in self.templates):
            raise ValueError("each template source_ids entry must identify an enabled S1-S4 source")
        if any(not set(template.event_keys).issubset(known_events) for template in self.templates):
            raise ValueError("each template event_keys entry must identify a catalog event")
        by_event = {event.event_key: event for event in self.events}
        for template in self.templates:
            for event_key in template.event_keys:
                if not set(by_event[event_key].source_refs).issubset(set(template.source_ids)):
                    raise ValueError("template source_ids must include the source_refs of each included event")
        dongdian = next((item for item in self.templates if item.template_key == "TJ_2023_DONGDIAN_PUBLIC_HISTORY_V1"), None)
        if dongdian is None or dongdian.runnable or dongdian.engineering_validated:
            raise ValueError("the 2023 Dongdian public-history template must remain non-runnable and unvalidated")
        transfer_event = by_event.get("TJ23-XIQING-JINGHAI-EVAC-STATUS")
        if transfer_event is None or transfer_event.mapped_region_id is not None or "TJ23-XIQING-JINGHAI-EVAC-STATUS" not in dongdian.event_keys:
            raise ValueError("the Xiqing/Jinghai transfer report must retain an unresolved scope even when referenced by the Dongdian history template")
        if any(template.runnable or template.engineering_validated for template in self.templates):
            raise ValueError("all built-in history and shell templates must remain non-runnable and unvalidated")
        return self


_PACKAGED_CATALOG = "rehearsal_data/catalog.json"
_BUILTIN_CATALOG_SHA256 = "6b150f868ae35037554f4c42a3436ef5988ca388e228c804a0217067cb2df99b"


def load_builtin_rehearsal_catalog() -> BuiltInRehearsalCatalog:
    """Load and validate the immutable packaged source/event/template catalog."""
    try:
        raw = resources.files("hydrodynamics").joinpath(_PACKAGED_CATALOG).read_bytes()
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        if actual_sha256 != _BUILTIN_CATALOG_SHA256:
            raise RehearsalCatalogError("packaged catalog digest differs from the reviewed R1 catalog")
        return BuiltInRehearsalCatalog.model_validate_json(raw)
    except (OSError, ValidationError, ValueError) as exc:
        if isinstance(exc, RehearsalCatalogError):
            raise
        raise RehearsalCatalogError(f"packaged rehearsal catalog is invalid: {exc}") from exc


def list_catalog_items(kind: Literal["sources", "events", "templates"] = "templates") -> list[dict[str, Any]]:
    catalog = load_builtin_rehearsal_catalog()
    return [item.model_dump(mode="json") for item in getattr(catalog, kind)]


def show_catalog_item(kind: Literal["sources", "events", "templates"], key: str) -> dict[str, Any]:
    catalog = load_builtin_rehearsal_catalog()
    id_field = {"sources": "source_id", "events": "event_key", "templates": "template_key"}[kind]
    for item in getattr(catalog, kind):
        if getattr(item, id_field) == key:
            return item.model_dump(mode="json")
    raise RehearsalCatalogError(f"unknown {kind[:-1]} key: {key}")


def clone_builtin_template(template_key: str) -> dict[str, Any]:
    """Create an incomplete scenario draft; never synthesize missing physical input."""
    catalog = load_builtin_rehearsal_catalog()
    template = next((item for item in catalog.templates if item.template_key == template_key), None)
    if template is None:
        raise RehearsalCatalogError(f"unknown template_key: {template_key}")
    return {
        "format": "rehearsal-scenario-v1",
        "schema_version": "1.0",
        "scenario_name": f"{template.display_name}（副本）",
        "description": "Draft cloned from a read-only built-in catalog template; no model input series are populated.",
        "creation_type": "BUILTIN_CLONE",
        "template_ref": {"template_key": template.template_key, "catalog_version": catalog.catalog_version},
        "source_document_plan_id": None,
        "region_ids": [],
        "region_aliases": list(template.scope_aliases),
        "event_id": None,
        "time_origin_local": None,
        "time_zone": "Asia/Shanghai",
        "duration_hours": None,
        "output_step_minutes": None,
        "engine_mode": None,
        "input_profile_ref": None,
        "upper_boundaries": [],
        "rule_snapshot_ref": None,
        "initial_condition": {"mode": "UNKNOWN"},
        "rainfall": {"mode": "UNKNOWN"},
        "downstream_policy": {"mode": "DEFERRED_UNKNOWN"},
        "requested_products": [],
        "metric_policy_version": None,
        "historical_event_refs": list(template.event_keys),
    }


__all__ = [
    "BuiltInRehearsalCatalog", "CatalogEvent", "CatalogSource", "CatalogTemplate", "RehearsalCatalogError",
    "clone_builtin_template", "list_catalog_items", "load_builtin_rehearsal_catalog", "show_catalog_item",
]
