"""Versioned status contract for the retained structured workflow."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator


class _WorkflowReportModel(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, validate_default=True)


class WorkflowStageReport(_WorkflowReportModel):
    status: Literal["pending", "running", "complete", "failed", "skipped"]
    started_at: StrictStr | None = None
    finished_at: StrictStr | None = None
    reason: StrictStr | None = None
    error: StrictStr | None = None


class StructuredShpStageReport(_WorkflowReportModel):
    status: Literal["pending", "not_applicable"]
    started_at: StrictStr | None = None
    finished_at: StrictStr | None = None
    reason: Literal["native_triangular_result_required"] | None = None
    output_kind: Literal["native_triangular_result_shp"] | None = None

    @model_validator(mode="after")
    def not_applicable_has_explicit_reason(self) -> "StructuredShpStageReport":
        if self.status == "not_applicable":
            if self.reason != "native_triangular_result_required" or self.output_kind != "native_triangular_result_shp":
                raise ValueError("structured SHP stage must explain that native triangular results are required")
        elif self.reason is not None or self.output_kind is not None:
            raise ValueError("pending SHP stage must not claim an output kind or completion reason")
        return self


class WorkflowReportStages(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
    preflight: WorkflowStageReport
    prepare: WorkflowStageReport
    model: WorkflowStageReport
    gate_dispatch: WorkflowStageReport
    shp: StructuredShpStageReport
    nc2web: WorkflowStageReport
    stepjson: WorkflowStageReport
    socio_overlay: WorkflowStageReport
    viewer: WorkflowStageReport
    validate_stage: WorkflowStageReport = Field(alias="validate")
    audit_database: WorkflowStageReport


class WorkflowRunReport(BaseModel):
    """Run-report schema for the existing structured hourly route."""

    model_config = ConfigDict(extra="forbid", strict=True)
    format: Literal["hydrodynamics-workflow-run-report-v1"]
    schema_version: Literal["1.1"]
    id: StrictStr
    script_id: StrictStr
    status: Literal["running", "complete", "failed"]
    started_at: StrictStr
    finished_at: StrictStr | None = None
    elapsed_seconds: float | None = None
    workflow_file: StrictStr
    workflow_spec: dict[str, Any]
    bundle_dir: StrictStr
    result_file: StrictStr
    engine_mode: Literal["hourly_inundation"]
    grid_topology: Literal["structured_grid"]
    workflow: WorkflowReportStages
    failed_stage: StrictStr | None = None
    error: StrictStr | None = None
