"""Versioned, opt-in "large run" output profile and run metadata.

This module is configuration only. It describes how a full-size native
triangular run (about 10^5..10^6 faces and hundreds of saved frames) is
streamed to disk and exported. It never runs a solver and does not change the
small-sample M1 bundle contract, which keeps its own fixed caps.

Every run-affecting option has a type, unit, default, legal range and a
validation error (AGENTS.md). The JSON schema mirror lives in
``examples/large_run_output/large_run_output_profile.schema.json``.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, ValidationError, field_validator, model_validator
import yaml


PROFILE_FORMAT = "hydrodynamics-large-run-output-profile"
PROFILE_VERSION = "1.0"
METADATA_FORMAT = "hydrodynamics-large-run-metadata"
METADATA_VERSION = "1.0"
RAIN_ONLY_NOTICE = (
    "RAIN-ONLY RUN WITH CLOSED BOUNDARIES: river inflow/outflow is NOT included "
    "(本次计算仅含降雨、边界闭合，未包含河道入流/出流)"
)
RIVER_ASSUMED_SECTIONS_NOTICE = (
    "RIVER INFLOW/OUTFLOW AT ASSUMED (USER-CONFIRMED, NON-AUTHORITATIVE) SECTIONS; NO RAINFALL; "
    "Q SERIES RECONSTRUCTED, NOT MEASURED "
    "(河道入流/出流位于假定断面（用户确认、非权威）；无降雨；流量过程为重建值、非实测)"
)


class LargeRunProfileError(ValueError):
    """The large-run output profile or run metadata is invalid."""


def _finite(value: float, name: str) -> float:
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    return float(value)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class LargeRunOutputProfile(_Strict):
    """Explicit large-run output options.

    Units: metres (m), bytes, counts. Ranges are enforced by pydantic.
    """

    format: Literal["hydrodynamics-large-run-output-profile"] = PROFILE_FORMAT
    schema_version: Literal["1.0"] = PROFILE_VERSION
    # Size guards (counts). They replace the M1 fixed small caps for this opt-in path only.
    max_face_count: StrictInt = Field(2_000_000, ge=1, le=20_000_000, description="count")
    max_node_count: StrictInt = Field(4_000_000, ge=3, le=40_000_000, description="count")
    max_frame_count: StrictInt = Field(10_000, ge=1, le=100_000, description="count")
    # Native UGRID NetCDF streaming layout.
    native_nc_complevel: StrictInt = Field(4, ge=0, le=9, description="zlib level; 0 disables compression")
    native_nc_face_chunk: StrictInt = Field(65_536, ge=1, le=16_777_216, description="faces per chunk; time chunk is 1")
    # Derived legacy compatibility display grid (NOT the computing mesh).
    display_grid_cell_size_m: StrictFloat | StrictInt = Field(50.0, ge=1.0, le=10_000.0, description="m")
    display_grid_anchor_x_m: StrictFloat | StrictInt = Field(474_150.0, description="m, lower-left cell edge anchor")
    display_grid_anchor_y_m: StrictFloat | StrictInt = Field(4_299_600.0, description="m, lower-left cell edge anchor")
    max_display_grid_cells: StrictInt = Field(4_000_000, ge=1, le=50_000_000, description="count")
    compat_nc_complevel: StrictInt = Field(4, ge=0, le=9)
    # Native web frames.
    native_web_value_dtype: Literal["float32", "float64"] = "float32"
    # Step JSON.
    stepjson_min_depth_m: StrictFloat | StrictInt = Field(0.0, ge=0.0, le=100.0, description="m; record written iff depth > value")
    stepjson_still_direction_deg: StrictFloat | StrictInt = Field(180.0, ge=0.0, lt=360.0, description="deg")
    write_stepjson_native: StrictBool = True
    # SHP: wet threshold for MAXIMUM_WET_SURFACE; None means the run dry_depth_threshold_m.
    shp_wet_depth_threshold_m: StrictFloat | StrictInt | None = Field(None, ge=0.0, le=100.0, description="m")
    # Mass check: |sum(h*A) - ledger volume| <= rel_tol * max(1, |ledger|).
    volume_check_rel_tol: StrictFloat | StrictInt = Field(1e-6, gt=0.0, le=0.1, description="1")
    # Disk preflight: refuse to start unless free bytes >= estimate * factor.
    disk_safety_factor: StrictFloat | StrictInt = Field(1.2, ge=1.0, le=10.0, description="1")
    disk_preflight: StrictBool = True

    @field_validator("display_grid_anchor_x_m", "display_grid_anchor_y_m", "display_grid_cell_size_m",
                     "stepjson_min_depth_m", "stepjson_still_direction_deg", "volume_check_rel_tol", "disk_safety_factor")
    @classmethod
    def _finite_numbers(cls, value: float, info: Any) -> float:
        return _finite(value, info.field_name)


class BoundaryGroup(_Strict):
    """One open-boundary group named in the run label (e.g. an assumed river section)."""

    group_id: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$")
    role: Literal["inflow", "outflow"]
    name: StrictStr = Field(min_length=1, max_length=200, description="human label, e.g. 大清河 west section")
    section_status: Literal["ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE", "AUTHORITATIVE"]
    discharge_series_status: Literal["RECONSTRUCTED_NOT_MEASURED", "MEASURED", "COMPUTED_OUTFLOW"]


class ForcingScope(_Strict):
    """Which forcing the run includes. Required, explicit label carried by every output."""

    scope: Literal["RAIN_ONLY", "RIVER_ONLY", "RAIN_AND_RIVER", "NONE"]
    boundary_policy: Literal["closed_wall", "open"]
    rainfall_included: StrictBool
    river_inflow_outflow_included: StrictBool
    river_section_status: Literal["NOT_APPLICABLE", "ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE", "AUTHORITATIVE"] = "NOT_APPLICABLE"
    discharge_series_status: Literal["NOT_APPLICABLE", "RECONSTRUCTED_NOT_MEASURED", "MEASURED"] = "NOT_APPLICABLE"
    boundary_groups: tuple[BoundaryGroup, ...] = ()
    notice: StrictStr = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _consistent(self) -> "ForcingScope":
        river = self.scope in ("RIVER_ONLY", "RAIN_AND_RIVER")
        rain = self.scope in ("RAIN_ONLY", "RAIN_AND_RIVER")
        if self.river_inflow_outflow_included != river:
            raise ValueError(f"river_inflow_outflow_included must be {river} for scope {self.scope}")
        if self.rainfall_included != rain:
            raise ValueError(f"rainfall_included must be {rain} for scope {self.scope}")
        if river:
            if self.boundary_policy != "open":
                raise ValueError("river inflow/outflow requires boundary_policy=open")
            if self.river_section_status == "NOT_APPLICABLE" or self.discharge_series_status == "NOT_APPLICABLE":
                raise ValueError("river runs must state river_section_status and discharge_series_status")
            roles = {group.role for group in self.boundary_groups}
            if "inflow" not in roles:
                raise ValueError("river runs must name at least one inflow boundary group")
            if len({group.group_id for group in self.boundary_groups}) != len(self.boundary_groups):
                raise ValueError("boundary group_id values must be unique")
        else:
            if self.boundary_groups or self.river_section_status != "NOT_APPLICABLE" or self.discharge_series_status != "NOT_APPLICABLE":
                raise ValueError("boundary groups/section/discharge status apply only to river runs")
        if not self.notice.strip():
            raise ValueError("forcing notice must be a non-empty label")
        return self


def rain_only_forcing() -> ForcingScope:
    return ForcingScope(scope="RAIN_ONLY", boundary_policy="closed_wall", rainfall_included=True,
                        river_inflow_outflow_included=False, notice=RAIN_ONLY_NOTICE)


def river_assumed_sections_forcing(groups: list[dict[str, Any]] | tuple[BoundaryGroup, ...]) -> ForcingScope:
    """Label for the first real run: river Q at assumed sections, no rain, reconstructed Q."""
    return ForcingScope(scope="RIVER_ONLY", boundary_policy="open", rainfall_included=False,
                        river_inflow_outflow_included=True,
                        river_section_status="ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE",
                        discharge_series_status="RECONSTRUCTED_NOT_MEASURED",
                        boundary_groups=tuple(BoundaryGroup.model_validate(g) if not isinstance(g, BoundaryGroup) else g for g in groups),
                        notice=RIVER_ASSUMED_SECTIONS_NOTICE)


class LargeRunMetadata(_Strict):
    """Run identity passed to ``FrameSink.open``."""

    format: Literal["hydrodynamics-large-run-metadata"] = METADATA_FORMAT
    schema_version: Literal["1.0"] = METADATA_VERSION
    run_id: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    case_id: StrictStr = Field(min_length=1, max_length=128)
    time_origin: StrictStr = Field(description="ISO 8601 with UTC offset, e.g. 2023-08-01T02:00:00+08:00")
    coordinate_system: StrictStr = Field(min_length=1, description="projected CRS in metres, e.g. EPSG:4548")
    vertical_datum: StrictStr | None = None
    dry_depth_threshold_m: StrictFloat | StrictInt = Field(ge=0.0, le=10.0, description="m")
    expected_times_s: tuple[StrictFloat | StrictInt, ...] | None = Field(None, description="s since time_origin; if given, saved frames must match exactly")
    data_origin: Literal["LIVE_SOLVER_RUN", "SYNTHETIC_TEST_FIELDS"]
    live_solver_ran: StrictBool
    production_approved: StrictBool = False
    forcing: ForcingScope
    solver: dict[str, Any] = Field(default_factory=dict, description="free-form solver provenance (JSON-serialisable)")
    source_hashes: dict[StrictStr, StrictStr] = Field(default_factory=dict)

    @field_validator("time_origin")
    @classmethod
    def _iso_with_offset(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"time_origin must be ISO 8601: {exc}") from exc
        if parsed.tzinfo is None:
            raise ValueError("time_origin must carry an explicit UTC offset")
        return value

    @field_validator("expected_times_s")
    @classmethod
    def _times(cls, value: tuple[float, ...] | None) -> tuple[float, ...] | None:
        if value is None:
            return None
        times = tuple(_finite(item, "expected_times_s") for item in value)
        if not times or any(b <= a for a, b in zip(times, times[1:])) or times[0] < 0:
            raise ValueError("expected_times_s must be non-empty, non-negative and strictly increasing")
        return times

    @model_validator(mode="after")
    def _origin(self) -> "LargeRunMetadata":
        if self.data_origin == "SYNTHETIC_TEST_FIELDS" and (self.live_solver_ran or self.production_approved):
            raise ValueError("synthetic test fields cannot claim live_solver_ran or production_approved")
        try:
            json.dumps(self.solver, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"solver provenance must be strict JSON: {exc}") from exc
        return self


def hourly_times_s(hours: int = 288, step_s: float = 3600.0) -> tuple[float, ...]:
    """0..hours inclusive in step_s seconds (289 frames for the 288 h run)."""
    if type(hours) is not int or hours < 0:
        raise LargeRunProfileError("hours must be a non-negative integer")
    return tuple(float(index * step_s) for index in range(hours + 1))


def _format_errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in row['loc']) or '<root>'}: {row['msg']}" for row in exc.errors())


def parse_profile(value: dict[str, Any] | None) -> LargeRunOutputProfile:
    try:
        return LargeRunOutputProfile.model_validate({} if value is None else value)
    except ValidationError as exc:
        raise LargeRunProfileError(f"invalid large-run output profile: {_format_errors(exc)}") from exc


def parse_metadata(value: dict[str, Any] | LargeRunMetadata) -> LargeRunMetadata:
    if isinstance(value, LargeRunMetadata):
        return value
    try:
        return LargeRunMetadata.model_validate(value)
    except ValidationError as exc:
        raise LargeRunProfileError(f"invalid large-run metadata: {_format_errors(exc)}") from exc


def load_profile(path: str | Path) -> LargeRunOutputProfile:
    """Load a YAML/JSON profile file (strict mapping)."""
    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LargeRunProfileError(f"cannot read large-run profile {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise LargeRunProfileError("large-run profile root must be a mapping")
    return parse_profile(raw)


__all__ = [
    "LargeRunOutputProfile", "LargeRunMetadata", "ForcingScope", "BoundaryGroup", "LargeRunProfileError",
    "PROFILE_FORMAT", "PROFILE_VERSION", "RAIN_ONLY_NOTICE", "RIVER_ASSUMED_SECTIONS_NOTICE", "hourly_times_s",
    "rain_only_forcing", "river_assumed_sections_forcing",
    "load_profile", "parse_metadata", "parse_profile",
]
