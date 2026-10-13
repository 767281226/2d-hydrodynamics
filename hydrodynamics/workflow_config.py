"""Validated workflow inputs; no solver or output actions occur here."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
import yaml

from .config import Number, Text


class WorkflowError(ValueError):
    pass


def validate_run_id(value: str) -> str:
    if not isinstance(value, str) or value.endswith('.') or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise WorkflowError("run id must contain 1-96 ASCII letters/digits/._- and start with a letter or digit")
    if value.split('.')[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise WorkflowError("run id cannot use a Windows reserved device name")
    return value


class _WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AreaProgressPoint(_WorkflowModel):
    time_h: Number = Field(ge=0)
    fraction: Number = Field(ge=0, le=1)


class HourlyWorkflowSettings(_WorkflowModel):
    eta_crest_m: Number | None = None
    water_surface_slope: Number | None = Field(default=None, gt=0)
    max_spread_m_s: Number | None = Field(default=None, gt=0)
    area_schedule: tuple[AreaProgressPoint, ...] = ()

    @model_validator(mode="after")
    def increasing_schedule(self):
        if self.area_schedule and (len(self.area_schedule) < 2 or any(
            b.time_h <= a.time_h for a, b in zip(self.area_schedule, self.area_schedule[1:])
        )):
            raise ValueError("area_schedule must have at least two points with strictly increasing time_h")
        return self


class ForcingInput(_WorkflowModel):
    file: Text
    source: Text
    time_unit: Literal["s"] = "s"
    discharge_unit: Literal["m3/s"] = "m3/s"
    rainfall_unit: Literal["mm/step"] = "mm/step"


class WorkflowConfig(_WorkflowModel):
    schema_version: Literal["1.0"] = "1.0"
    case_config: Text
    prepared_grid: Text
    forcing: ForcingInput
    time_origin: Text
    settings: HourlyWorkflowSettings = Field(default_factory=HourlyWorkflowSettings)
    output_directory: Text = "../../work/results"
    result_name: Text = "result.nc"
    dispatch_file: Text | None = None
    record_xihe_component_changes: StrictBool = True
    socio_overlay_shp: Text | None = None

    @field_validator("prepared_grid")
    @classmethod
    def prepared_format(cls, value):
        if Path(value).suffix.lower() != ".npz":
            raise ValueError("prepared_grid must reference a prepared .npz grid; raw DEM input is not accepted")
        return value

    @field_validator("time_origin")
    @classmethod
    def timezone_required(cls, value):
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("time_origin must include a timezone")
        return value

    @field_validator("result_name")
    @classmethod
    def safe_result_name(cls, value):
        if not value.endswith('.nc'):
            raise ValueError("result_name must end with .nc")
        validate_run_id(value)
        return value


def load_workflow_config(path: str | Path) -> WorkflowConfig:
    target = Path(path)
    if not target.is_file():
        raise WorkflowError(f"workflow file not found: {target.resolve()}")
    data = yaml.safe_load(target.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise WorkflowError("workflow file must contain a mapping")
    return WorkflowConfig.model_validate(data)


def resolve_workflow_path(workflow_path: Path, value: str) -> Path:
    target = Path(value).expanduser()
    return (target if target.is_absolute() else workflow_path.parent / target).resolve()
