"""Glue driver for one real long triangular shallow-water run (large-run profile).

Pipeline (no new equations; every piece already exists)::

    mesh artifact (triangle-domain-mesh-only-artifact-v1)
    + per-face terrain .npz (face_id, elevation_m)
    + assumed river sections audit (assumed-river-sections-audit-v1)
    + discharge CSV (time_s + series columns; applied Q for inflow / prescribed outflow,
      reference-only for sections configured as free_outflow)
        -> TriangularLongRunInputs / run_triangular_long_run        (hydro_pipeline.triangular_long_run)
        -> NativeUgridNetcdfSink streaming <dir>/bundle/result_native.nc (hydro_pipeline.frame_sink)
        -> export_large_run_bundle + verify_large_run_bundle        (hydro_pipeline.large_run_export)

Input contract ``large-run-driver-config-v1`` (JSON Schema:
``examples/large_run_output/large_run_driver_config.schema.json``; field table:
``docs/large_run_driver.md``).

Run directory (``output.directory``, or ``<output.root>/<scheme id>`` with the scheme id from ``--id``,
``output.script_id`` or a generated Beijing-date + Unix-ns id; must not exist unless ``--resume``)::

    run_report.json       status running | failed | complete (complete only after frames, close, export, verify)
    run_receipt.json      input SHA-256s, options, kernel, step counts, wall times, library versions
    progress.jsonl        driver phases + solver progress callbacks (one JSON object per line)
    ledger.jsonl          one solver ledger row per saved frame (rewritten from the solver summary at the end)
    config.resolved.json  the validated configuration with absolute paths
    checkpoints/          checkpoint_XXXXXXXX.npz written by the solver (never deleted by the driver)
    bundle/               result_native.nc + every exported deliverable (exporter-owned, no-clobber)

CLI::

    python -m hydro_pipeline.large_run_driver <config.json> [--id SCHEME_ID] [--resume] [--check]
    python -m hydro_pipeline.large_run_driver <config.json> --id NEW_ID --branch-from <parent>/checkpoints/<ckpt>.npz
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
import time
import traceback
from typing import Any, Literal, Mapping

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, ValidationError, field_validator, model_validator

from .frame_sink import FrameSinkError, LargeNativeResult, NativeUgridNetcdfSink, display_grid_extent, estimate_bundle_bytes
from .large_run_export import LargeRunExportError, export_large_run_bundle, verify_large_run_bundle
from .large_run_profile import (LargeRunMetadata, LargeRunOutputProfile, LargeRunProfileError, hourly_times_s, load_profile,
                                parse_metadata, parse_profile, river_assumed_sections_forcing)
from .triangular_long_run import (FREE_OUTFLOW_DEFAULT_DISCHARGE_COEFFICIENT, LONG_RUN_SOLVER_VERSION, TriangularFrame, TriangularLongRunError, TriangularLongRunInputs,
                                  TriangularLongRunOptions, TriangularLongRunSummary, _edges, _inputs_fingerprint,
                                  load_long_run_checkpoint, numba_status, run_triangular_long_run)
from .triangular_mesh import TriangularMesh, TriangularMeshError
from .triangular_solver import TriangularSolverExecutionError


CONFIG_FORMAT = "large-run-driver-config-v1"
CONFIG_VERSION = "1.0"
MESH_ARTIFACT_FORMAT = "triangle-domain-mesh-only-artifact-v1"
SECTIONS_FORMAT = "assumed-river-sections-audit-v1"
REPORT_FORMAT = "large-run-driver-report-v1"
RECEIPT_FORMAT = "large-run-driver-receipt-v1"
CHECK_FORMAT = "large-run-driver-check-v1"
BUNDLE_SUBDIR = "bundle"
CHECKPOINT_SUBDIR = "checkpoints"
MAX_RUN_SECONDS = 31_536_000.0
SOLVER_NAME = "hydro_pipeline.triangular_long_run"
_KEY = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")

Number = StrictFloat | StrictInt
DischargeStatus = Literal["RECONSTRUCTED_NOT_MEASURED", "MEASURED", "COMPUTED_OUTFLOW"]
Distribution = Literal["length", "conveyance_depth_5_3"]


class LargeRunDriverError(ValueError):
    """Invalid driver configuration or inconsistent run inputs."""


# ---------------------------------------------------------------------------
# Configuration contract
# ---------------------------------------------------------------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _finite(value: float, name: str) -> float:
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    return value


class MeshSection(_Strict):
    path: StrictStr = Field(min_length=1, description="triangle-domain-mesh-only-artifact-v1 JSON")
    expected_sha256: StrictStr | None = Field(None, pattern=r"^[0-9a-f]{64}$", description="refuse the artifact if its SHA-256 differs")


class TerrainSection(_Strict):
    path: StrictStr = Field(min_length=1, description=".npz with int64 face_id and float64 elevation_m (matched by face_id)")
    dem_path: StrictStr = Field(min_length=1, description="provenance only; the driver never opens the DEM")
    dem_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$", description="provenance only; recorded as supplied")
    mapping_report_path: StrictStr | None = Field(None, description="provenance only; hashed when present")
    method: StrictStr = Field(min_length=1, max_length=1000, description="free-text label of the terrain mapping method")
    vertical_datum: StrictStr | None = Field(None, description="null: water levels are labelled unavailable in derived outputs")
    vertical_unit: Literal["m"] = "m"

    @field_validator("method", "vertical_datum")
    @classmethod
    def _non_blank(cls, value: str | None, info: Any) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must not be blank")
        return value


class OutflowBoundarySettings(_Strict):
    """How one audit outflow section leaves the domain."""

    boundary_type: Literal["prescribed_outflow", "free_outflow"] = Field(
        "prescribed_outflow", description="prescribed_outflow: the CSV column is the applied Q(t); free_outflow: stage-dependent "
                                          "critical-flow weir outflow, the CSV column is only a reference series in the ledger")
    discharge_coefficient: Number | None = Field(None, gt=0.0, le=1.5, description="free_outflow only; dimensionless C_d in (0, 1.5]; null = (2/3)^(3/2) ~= 0.5443")
    crest_elevation_m: Number | None = Field(None, ge=-1000.0, le=10000.0, description="free_outflow only; m, bed datum, [-1000, 10000]; null = bed of each adjacent face (head = depth)")

    @field_validator("discharge_coefficient", "crest_elevation_m")
    @classmethod
    def _finite_numbers(cls, value: float | None, info: Any) -> float | None:
        return None if value is None else _finite(value, info.field_name)

    @model_validator(mode="after")
    def _consistent(self) -> "OutflowBoundarySettings":
        if self.boundary_type == "prescribed_outflow" and (self.discharge_coefficient is not None or self.crest_elevation_m is not None):
            raise ValueError("discharge_coefficient and crest_elevation_m are only valid with boundary_type free_outflow")
        return self


class SectionsSection(_Strict):
    path: StrictStr = Field(min_length=1, description="assumed-river-sections-audit-v1 JSON")
    discharge_series_status: dict[StrictStr, DischargeStatus] = Field(description="one entry per section key in the audit")
    distribution: dict[StrictStr, Distribution] = Field(default_factory=dict, description="per section; default inflow=length, prescribed outflow=conveyance_depth_5_3; not allowed for free_outflow sections")
    outflow_boundary: dict[StrictStr, OutflowBoundarySettings] = Field(default_factory=dict, description="per outflow section; default boundary_type prescribed_outflow")

    @field_validator("discharge_series_status", "distribution", "outflow_boundary")
    @classmethod
    def _keys(cls, value: dict[str, Any], info: Any) -> dict[str, Any]:
        for key in value:
            if not _KEY.match(key) or "__" in key:
                raise ValueError(f"{info.field_name} key {key!r} must match [A-Za-z0-9_]{{1,64}} without '__'")
        return value


class ForcingSection(_Strict):
    path: StrictStr = Field(min_length=1, description="CSV with time_s and the discharge series columns named by the audit")
    rainfall: None = Field(None, description="must be null in v1 (no rainfall); a rainfall column contract is reserved for a later version")


class RunSection(_Strict):
    hours: StrictInt | None = Field(None, ge=1, le=8760, description="hourly output times 0..hours (exclusive with output_times_s)")
    output_times_s: list[Number] | None = Field(None, min_length=2, max_length=100_000, description="s; explicit output times starting at 0")
    initial_depth_m: Number = Field(ge=0.0, le=1000.0, description="m; uniform initial water depth")
    manning_n: Number = Field(gt=0.0, le=1.0, description="s m^-1/3; uniform Manning n")
    long_run_options: dict[str, Any] = Field(default_factory=dict, description="triangular-long-run-options-v1 fields (checkpoint_interval_s lives in run.checkpoint_interval_s)")
    checkpoint_interval_s: Number | None = Field(None, gt=0.0, le=MAX_RUN_SECONDS, description="s; null disables checkpoints and therefore --resume")
    time_origin: StrictStr = Field(description="ISO 8601 with explicit UTC offset, e.g. 2023-08-01T02:00:00+08:00")
    run_id: StrictStr | None = Field(None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
                                     description="result run id shown in the bundle/viewer; null = the scheme id (output.script_id / --id)")
    case_id: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", description="also names the derived <case_id>.nc")
    coordinate_system: StrictStr = Field(min_length=1, description="must equal the mesh artifact coordinate_system")

    @field_validator("initial_depth_m", "manning_n", "checkpoint_interval_s")
    @classmethod
    def _finite_numbers(cls, value: float | None, info: Any) -> float | None:
        return None if value is None else _finite(value, info.field_name)

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

    @field_validator("output_times_s")
    @classmethod
    def _times(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        times = [float(_finite(item, "output_times_s")) for item in value]
        if times[0] != 0.0 or any(b <= a for a, b in zip(times, times[1:])) or times[-1] > MAX_RUN_SECONDS:
            raise ValueError("output_times_s must start at 0, be strictly increasing and end <= 31536000 s")
        return times

    @field_validator("case_id")
    @classmethod
    def _case_id(cls, value: str) -> str:
        if value == "result_native":
            raise ValueError("case_id must not be 'result_native'")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "RunSection":
        if (self.hours is None) == (self.output_times_s is None):
            raise ValueError("exactly one of hours or output_times_s is required")
        if "checkpoint_interval_s" in self.long_run_options:
            raise ValueError("long_run_options.checkpoint_interval_s is not allowed; use run.checkpoint_interval_s")
        try:
            TriangularLongRunOptions.model_validate(dict(self.long_run_options))
        except ValidationError as exc:
            raise ValueError(f"long_run_options: {_format_errors(exc)}") from exc
        if self.long_run_options.get("manning_friction") == "none":
            raise ValueError("long_run_options.manning_friction 'none' is incompatible with the required uniform manning_n")
        return self

    def times_s(self) -> tuple[float, ...]:
        return tuple(self.output_times_s) if self.output_times_s is not None else hourly_times_s(int(self.hours))

    def options(self) -> TriangularLongRunOptions:
        data = dict(self.long_run_options)
        data["checkpoint_interval_s"] = None if self.checkpoint_interval_s is None else float(self.checkpoint_interval_s)
        return TriangularLongRunOptions.model_validate(data)


class OutputSection(_Strict):
    directory: StrictStr | None = Field(None, min_length=1, description="explicit run directory (exclusive with root); must not exist unless --resume; bundle is written to <directory>/bundle")
    root: StrictStr | None = Field(None, min_length=1, description="results root (exclusive with directory), e.g. work/results; the run directory is <root>/<scheme id>")
    script_id: StrictStr | None = Field(None, description="scheme id = run directory name under root: 1-96 ASCII letters/digits/._-, starting with a letter or digit, no Windows reserved names; CLI --id wins; null with root = Beijing date + '-' + Unix ns (as the workflow)")
    profile: dict[str, Any] | StrictStr | None = Field(None, description="LargeRunOutputProfile fields, or a YAML/JSON profile path; null = defaults")
    export: StrictBool = Field(True, description="run export_large_run_bundle + verify_large_run_bundle after the solver")

    @model_validator(mode="after")
    def _one_target(self) -> "OutputSection":
        if (self.directory is None) == (self.root is None):
            raise ValueError("set exactly one of output.directory or output.root")
        if self.directory is not None and self.script_id is not None:
            raise ValueError("output.script_id names a directory under output.root; it cannot be combined with output.directory")
        if self.script_id is not None:
            from .run_identity import validate_run_id
            try:
                validate_run_id(self.script_id)
            except Exception as exc:
                raise ValueError(f"output.script_id: {exc}") from exc
        return self


class LargeRunDriverConfig(_Strict):
    format: Literal["large-run-driver-config-v1"] = CONFIG_FORMAT
    schema_version: Literal["1.0"] = CONFIG_VERSION
    description: StrictStr | None = None
    mesh: MeshSection
    terrain: TerrainSection
    sections: SectionsSection
    forcing: ForcingSection
    run: RunSection
    output: OutputSection

    def profile(self) -> LargeRunOutputProfile:
        value = self.output.profile
        if isinstance(value, str):
            return load_profile(value)
        return parse_profile(value)


def _format_errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in row['loc']) or '<root>'}: {row['msg']}" for row in exc.errors())


def parse_driver_config(value: Mapping[str, Any]) -> LargeRunDriverConfig:
    if not isinstance(value, Mapping):
        raise LargeRunDriverError("driver config root must be a JSON object")
    try:
        return LargeRunDriverConfig.model_validate(dict(value))
    except ValidationError as exc:
        raise LargeRunDriverError(f"invalid {CONFIG_FORMAT}: {_format_errors(exc)}") from exc


_PATH_FIELDS = (("mesh", "path"), ("terrain", "path"), ("terrain", "dem_path"), ("terrain", "mapping_report_path"),
                ("sections", "path"), ("forcing", "path"), ("output", "directory"), ("output", "root"), ("output", "profile"))


def resolve_config_paths(config: LargeRunDriverConfig, base_dir: Path) -> LargeRunDriverConfig:
    """Return a copy with every path field absolute (relative paths are taken from ``base_dir``)."""
    data = config.model_dump(mode="json")
    for section, name in _PATH_FIELDS:
        value = data[section][name]
        if isinstance(value, str):
            data[section][name] = str((base_dir / value).resolve()) if not Path(value).is_absolute() else str(Path(value))
    return parse_driver_config(data)


def finalize_output(config: LargeRunDriverConfig, script_id: str | None = None) -> LargeRunDriverConfig:
    """Resolve the run directory and run_id.

    With ``output.root`` the run directory is ``<root>/<scheme id>``; the id is ``script_id`` (CLI ``--id``),
    else ``output.script_id``, else a new Beijing-date + Unix-ns id (the workflow rule).  ``run.run_id`` null
    becomes the scheme id (or the directory name with ``output.directory``).
    """
    from .run_identity import resolve_script_id
    from .run_identity import RunIdentityError as WorkflowError
    data = config.model_dump(mode="json")
    out = data["output"]
    if script_id is not None and out["root"] is None:
        raise LargeRunDriverError("--id selects a directory under output.root; this config uses output.directory")
    if out["root"] is not None:
        try:
            scheme = resolve_script_id(script_id if script_id is not None else out["script_id"])
        except WorkflowError as exc:
            raise LargeRunDriverError(f"invalid scheme id: {exc}") from exc
        out["directory"] = str(Path(out["root"]) / scheme)
        out["script_id"] = scheme
    else:
        scheme = Path(out["directory"]).name
    if data["run"]["run_id"] is None:
        data["run"]["run_id"] = scheme
    resolved = LargeRunDriverConfig.model_validate({**data, "output": {**out, "root": None, "script_id": None}})
    object.__setattr__(resolved, "_scheme_id", scheme)
    return resolved


def load_driver_config(path: str | Path, *, script_id: str | None = None) -> LargeRunDriverConfig:
    """Load, resolve paths and resolve the run directory/run_id (see :func:`finalize_output`)."""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LargeRunDriverError(f"cannot read driver config {source}: {exc}") from exc
    except ValueError as exc:
        raise LargeRunDriverError(f"driver config {source} is not valid JSON: {exc}") from exc
    return finalize_output(resolve_config_paths(parse_driver_config(raw), source.resolve().parent), script_id)


def driver_config_json_schema() -> dict[str, Any]:
    schema = LargeRunDriverConfig.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = CONFIG_FORMAT
    return schema


# ---------------------------------------------------------------------------
# Input readers (each raises LargeRunDriverError with a clear message)
# ---------------------------------------------------------------------------
def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> Any:
    if not path.is_file():
        raise LargeRunDriverError(f"{label} file is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LargeRunDriverError(f"{label} file {path} cannot be read as JSON: {exc}") from exc


def read_mesh_artifact(path: str | Path, expected_sha256: str | None = None) -> dict[str, Any]:
    """Read and shape-check a mesh-only artifact without building the TriangularMesh."""
    source = Path(path)
    raw = _read_json(source, "mesh artifact")
    if not isinstance(raw, dict) or raw.get("format") != MESH_ARTIFACT_FORMAT:
        raise LargeRunDriverError(f"mesh artifact {source} is not a {MESH_ARTIFACT_FORMAT} file")
    sha = _sha_file(source)
    if expected_sha256 is not None and sha != expected_sha256:
        raise LargeRunDriverError(f"mesh artifact {source} sha256 {sha} differs from mesh.expected_sha256 {expected_sha256}")
    for key in ("node_xy", "face_nodes", "face_id", "coordinate_system", "mesh_id"):
        if key not in raw:
            raise LargeRunDriverError(f"mesh artifact is missing {key}")
    try:
        node_xy = np.asarray(raw["node_xy"], dtype=np.float64)
        face_nodes = np.asarray(raw["face_nodes"], dtype=np.int64)
        face_id = np.asarray(raw["face_id"], dtype=np.int64)
    except (TypeError, ValueError) as exc:
        raise LargeRunDriverError(f"mesh artifact arrays are not numeric: {exc}") from exc
    if node_xy.ndim != 2 or node_xy.shape[1] != 2 or node_xy.shape[0] < 3 or not np.isfinite(node_xy).all():
        raise LargeRunDriverError("mesh artifact node_xy must be a finite (n_node>=3, 2) list")
    if face_nodes.ndim != 2 or face_nodes.shape[1] != 3 or face_nodes.shape[0] < 1:
        raise LargeRunDriverError("mesh artifact face_nodes must be an (n_face>=1, 3) list")
    if face_nodes.min() < 0 or face_nodes.max() >= node_xy.shape[0]:
        raise LargeRunDriverError("mesh artifact face_nodes reference a node outside node_xy")
    if face_id.shape != (face_nodes.shape[0],) or face_id.min() < 0 or np.unique(face_id).size != face_id.size:
        raise LargeRunDriverError("mesh artifact face_id must hold one unique non-negative int64 per face")
    if not isinstance(raw["coordinate_system"], str) or not raw["coordinate_system"].strip():
        raise LargeRunDriverError("mesh artifact coordinate_system must be a non-empty string")
    if not isinstance(raw["mesh_id"], str) or not raw["mesh_id"].strip():
        raise LargeRunDriverError("mesh artifact mesh_id must be a non-empty string")
    return {"path": str(source), "sha256": sha, "mesh_id": raw["mesh_id"], "coordinate_system": raw["coordinate_system"].strip(),
            "node_xy": node_xy, "face_nodes": face_nodes, "face_id": face_id, "boundary_groups": raw.get("boundary_groups") or {},
            "geometry_tolerance_m": raw.get("geometry_tolerance_m", 1e-8), "minimum_face_area_m2": raw.get("minimum_face_area_m2", 1e-12),
            "n_node": int(node_xy.shape[0]), "n_face": int(face_nodes.shape[0]), "raw": raw}


def edge_table(face_nodes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Unique sorted node pairs in TriangularMesh edge order and their face incidence counts (1 = exterior)."""
    faces = np.asarray(face_nodes, dtype=np.int64)
    pairs = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    pairs.sort(axis=1)
    return np.unique(pairs, axis=0, return_counts=True)


def build_triangular_mesh(artifact: Mapping[str, Any]) -> TriangularMesh:
    raw = artifact["raw"]
    try:
        return TriangularMesh(
            node_xy=raw["node_xy"], face_nodes=raw["face_nodes"], coordinate_system=artifact["coordinate_system"],
            mesh_id=artifact["mesh_id"], face_id=raw["face_id"], boundary_groups=raw.get("boundary_groups"),
            constraint_groups=raw.get("constraint_groups"), geometry_tolerance_m=float(artifact["geometry_tolerance_m"]),
            minimum_face_area_m2=float(artifact["minimum_face_area_m2"]),
        )
    except (TriangularMeshError, TypeError, ValueError) as exc:
        raise LargeRunDriverError(f"mesh artifact does not build a valid TriangularMesh: {exc}") from exc


def read_terrain(path: str | Path, face_id: np.ndarray) -> dict[str, Any]:
    """Per-face bed elevation matched by face_id (missing, duplicate, extra or non-finite entries are rejected)."""
    source = Path(path)
    if not source.is_file():
        raise LargeRunDriverError(f"terrain file is missing: {source}")
    try:
        with np.load(source, allow_pickle=False) as data:
            if "face_id" not in data.files or "elevation_m" not in data.files:
                raise LargeRunDriverError(f"terrain {source} must contain arrays face_id and elevation_m")
            ids = np.asarray(data["face_id"]); elevation = np.asarray(data["elevation_m"])
    except (OSError, ValueError) as exc:
        raise LargeRunDriverError(f"terrain {source} cannot be read as .npz: {exc}") from exc
    if ids.ndim != 1 or not np.issubdtype(ids.dtype, np.integer):
        raise LargeRunDriverError("terrain face_id must be a 1-D integer array")
    if elevation.shape != ids.shape or not np.issubdtype(elevation.dtype, np.floating):
        raise LargeRunDriverError("terrain elevation_m must be a 1-D float array aligned with face_id")
    ids = ids.astype(np.int64); elevation = elevation.astype(np.float64)
    if np.unique(ids).size != ids.size:
        raise LargeRunDriverError(f"terrain face_id contains {ids.size - np.unique(ids).size} duplicate entries")
    bad = int(np.count_nonzero(~np.isfinite(elevation)))
    if bad:
        raise LargeRunDriverError(f"terrain elevation_m contains {bad} non-finite values")
    mesh_ids = np.asarray(face_id, dtype=np.int64)
    order = np.argsort(ids, kind="stable")
    sorted_ids = ids[order]
    pos = np.searchsorted(sorted_ids, mesh_ids)
    pos_clipped = np.minimum(pos, sorted_ids.size - 1)
    found = sorted_ids[pos_clipped] == mesh_ids
    missing = int(np.count_nonzero(~found))
    if missing:
        sample = mesh_ids[~found][:5].tolist()
        raise LargeRunDriverError(f"terrain lacks elevation for {missing} mesh faces (face_id sample {sample})")
    if ids.size != mesh_ids.size:
        raise LargeRunDriverError(f"terrain lists {ids.size - mesh_ids.size} face_id values that are not mesh faces")
    bed = elevation[order][pos_clipped]
    return {"path": str(source), "sha256": _sha_file(source), "elevation_m": bed, "face_count": int(bed.size),
            "min_m": float(bed.min()), "max_m": float(bed.max()), "mean_m": float(bed.mean())}


def read_sections_audit(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    raw = _read_json(source, "sections audit")
    if not isinstance(raw, dict) or raw.get("format") != SECTIONS_FORMAT:
        raise LargeRunDriverError(f"sections audit {source} is not a {SECTIONS_FORMAT} file")
    sections = raw.get("sections")
    if not isinstance(sections, dict) or not sections:
        raise LargeRunDriverError("sections audit must contain a non-empty 'sections' mapping")
    top_status = raw.get("section_status")
    out: dict[str, dict[str, Any]] = {}
    used: set[int] = set()
    for key, item in sections.items():
        if not isinstance(key, str) or not _KEY.match(key) or "__" in key:
            raise LargeRunDriverError(f"section key {key!r} must match [A-Za-z0-9_]{{1,64}} without '__'")
        if not isinstance(item, dict):
            raise LargeRunDriverError(f"section {key} must be a mapping")
        role = item.get("role")
        if role not in ("inflow", "outflow"):
            raise LargeRunDriverError(f"section {key}: role must be 'inflow' or 'outflow'")
        edges = item.get("edge_indices")
        if not isinstance(edges, list) or not edges or any(isinstance(e, bool) or not isinstance(e, int) or e < 0 for e in edges):
            raise LargeRunDriverError(f"section {key}: edge_indices must be a non-empty list of non-negative integers")
        if len(set(edges)) != len(edges):
            raise LargeRunDriverError(f"section {key}: edge_indices must be unique")
        overlap = used.intersection(edges)
        if overlap:
            raise LargeRunDriverError(f"section {key}: edge indices {sorted(overlap)[:5]} already belong to another section")
        used.update(edges)
        column = item.get("discharge_series_column")
        if not isinstance(column, str) or not column.strip():
            raise LargeRunDriverError(f"section {key}: discharge_series_column must be a non-empty string")
        name = item.get("name_zh") or item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise LargeRunDriverError(f"section {key}: name_zh must be a non-empty string")
        status = item.get("section_status", top_status)
        if status not in ("ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE", "AUTHORITATIVE"):
            raise LargeRunDriverError(f"section {key}: section_status must be ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE or AUTHORITATIVE (per section or top-level)")
        edge_nodes = item.get("edge_nodes")
        if edge_nodes is not None:
            if (not isinstance(edge_nodes, list) or len(edge_nodes) != len(edges)
                    or any(not isinstance(p, list) or len(p) != 2 or any(isinstance(n, bool) or not isinstance(n, int) for n in p) for p in edge_nodes)):
                raise LargeRunDriverError(f"section {key}: edge_nodes must list one [node, node] pair per edge index")
        out[key] = {"role": role, "edge_indices": [int(e) for e in edges], "column": column.strip(), "name": name.strip()[:200],
                    "section_status": status, "edge_nodes": edge_nodes, "width_m": item.get("width_m")}
    if "inflow" not in {s["role"] for s in out.values()}:
        raise LargeRunDriverError("sections audit must contain at least one inflow section")
    return {"path": str(source), "sha256": _sha_file(source), "sections": out, "mesh_id": raw.get("mesh_id"),
            "coordinate_system": raw.get("crs"), "section_status": top_status, "raw_keys": sorted(k for k in raw if k != "sections")}


def check_sections_against_edges(sections: Mapping[str, Mapping[str, Any]], pairs: np.ndarray, counts: np.ndarray) -> None:
    """Every section edge must be an exterior mesh edge; audit edge_nodes (if present) must match the mesh edge."""
    for key, item in sections.items():
        idx = np.asarray(item["edge_indices"], dtype=np.int64)
        if idx.max() >= pairs.shape[0]:
            raise LargeRunDriverError(f"section {key}: edge index {int(idx.max())} is outside the mesh ({pairs.shape[0]} edges)")
        interior = idx[counts[idx] != 1]
        if interior.size:
            raise LargeRunDriverError(f"section {key}: edges {interior[:5].tolist()} are interior edges, not exterior edges")
        if item.get("edge_nodes") is not None:
            given = np.sort(np.asarray(item["edge_nodes"], dtype=np.int64), axis=1)
            if not np.array_equal(pairs[idx], given):
                raise LargeRunDriverError(f"section {key}: audit edge_nodes do not match the mesh edges at edge_indices (mesh/audit mismatch)")


def read_forcing_csv(path: str | Path, columns: Mapping[str, str]) -> dict[str, Any]:
    """``columns`` maps section key -> CSV column. Values are magnitudes in m3/s (>= 0, finite)."""
    source = Path(path)
    if not source.is_file():
        raise LargeRunDriverError(f"forcing CSV is missing: {source}")
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            header = [h.strip() for h in (reader.fieldnames or [])]
            rows = list(reader)
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise LargeRunDriverError(f"forcing CSV {source} cannot be read: {exc}") from exc
    if "time_s" not in header:
        raise LargeRunDriverError("forcing CSV must contain a time_s column")
    needed = sorted(set(columns.values()))
    missing = [c for c in needed if c not in header]
    if missing:
        raise LargeRunDriverError(f"forcing CSV lacks series columns {missing} (available: {header})")
    if len(rows) < 2:
        raise LargeRunDriverError("forcing CSV needs at least two rows")

    def column(name: str) -> np.ndarray:
        values = []
        for number, row in enumerate(rows, start=2):
            text = (row.get(name) or "").strip()
            try:
                value = float(text)
            except ValueError as exc:
                raise LargeRunDriverError(f"forcing CSV row {number} column {name}: {text!r} is not a number") from exc
            if not math.isfinite(value):
                raise LargeRunDriverError(f"forcing CSV row {number} column {name} is not finite")
            values.append(value)
        return np.asarray(values, dtype=np.float64)

    time_s = column("time_s")
    if np.any(np.diff(time_s) <= 0.0):
        raise LargeRunDriverError("forcing CSV time_s must be strictly increasing")
    series = {}
    for name in needed:
        values = column(name)
        if np.any(values < 0.0):
            raise LargeRunDriverError(f"forcing CSV column {name} has negative values; discharges are magnitudes (direction comes from the section role)")
        series[name] = values
    return {"path": str(source), "sha256": _sha_file(source), "time_s": time_s, "series": series, "rows": len(rows),
            "columns": header, "first_time_s": float(time_s[0]), "last_time_s": float(time_s[-1])}


def check_forcing_coverage(forcing: Mapping[str, Any], times: tuple[float, ...]) -> None:
    if forcing["first_time_s"] > 0.0 or forcing["last_time_s"] < times[-1]:
        raise LargeRunDriverError(
            f"forcing CSV covers [{forcing['first_time_s']}, {forcing['last_time_s']}] s but the run needs [0, {times[-1]}] s")


# ---------------------------------------------------------------------------
# Case assembly
# ---------------------------------------------------------------------------
def boundary_group_specs(config: LargeRunDriverConfig, sections: Mapping[str, Any], forcing: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Solver open-boundary group mappings (triangular-open-boundary-group-v1), one per audit section."""
    statuses = config.sections.discharge_series_status
    unknown = sorted(set(statuses) - set(sections["sections"]))
    if unknown:
        raise LargeRunDriverError(f"sections.discharge_series_status names unknown sections {unknown}")
    unknown = sorted(set(config.sections.distribution) - set(sections["sections"]))
    if unknown:
        raise LargeRunDriverError(f"sections.distribution names unknown sections {unknown}")
    unknown = sorted(set(config.sections.outflow_boundary) - set(sections["sections"]))
    if unknown:
        raise LargeRunDriverError(f"sections.outflow_boundary names unknown sections {unknown}")
    missing = sorted(set(sections["sections"]) - set(statuses))
    if missing:
        raise LargeRunDriverError(f"sections.discharge_series_status is required for every audit section; missing {missing}")
    groups = []
    times = forcing["time_s"].tolist()
    for key, item in sections["sections"].items():
        btype = section_boundary_type(config, key, item)
        series = forcing["series"][item["column"]].tolist()
        if btype == "free_outflow":
            settings = config.sections.outflow_boundary[key]
            group = {"name": key, "role": "free_outflow", "edge_indices": list(item["edge_indices"]),
                     "reference_time_s": times, "reference_discharge_m3_s": series}
            if settings.discharge_coefficient is not None:
                group["discharge_coefficient"] = float(settings.discharge_coefficient)
            if settings.crest_elevation_m is not None:
                group["crest_elevation_m"] = float(settings.crest_elevation_m)
            groups.append(group)
            continue
        groups.append({"name": key, "role": item["role"], "edge_indices": list(item["edge_indices"]), "time_s": times,
                       "discharge_m3_s": series, "distribution": section_distribution(config, key, item)})
    return groups


def section_boundary_type(config: LargeRunDriverConfig, key: str, item: Mapping[str, Any]) -> str:
    """``inflow``, ``prescribed_outflow`` or ``free_outflow`` for one audit section (raises on inconsistent config)."""
    settings = config.sections.outflow_boundary.get(key)
    if item["role"] == "inflow":
        if settings is not None:
            raise LargeRunDriverError(f"sections.outflow_boundary.{key}: section {key} is an inflow section; outflow_boundary applies to outflow sections only")
        return "inflow"
    btype = "prescribed_outflow" if settings is None else settings.boundary_type
    if btype == "free_outflow" and key in config.sections.distribution:
        raise LargeRunDriverError(f"sections.distribution.{key}: distribution does not apply to a free_outflow section (every edge uses its own head)")
    return btype


def section_distribution(config: LargeRunDriverConfig, key: str, item: Mapping[str, Any]) -> str | None:
    """Edge weighting of a prescribed section; None for free_outflow sections."""
    if section_boundary_type(config, key, item) == "free_outflow":
        return None
    return config.sections.distribution.get(key, "length" if item["role"] == "inflow" else "conveyance_depth_5_3")


def section_boundary_summary(config: LargeRunDriverConfig, key: str, item: Mapping[str, Any], *, full: bool = True) -> dict[str, Any]:
    """Boundary treatment of one section for metadata, receipts and --check.

    ``full=False`` (run metadata) keeps prescribed sections exactly as before
    free_outflow existed (only ``distribution``), so partial results written by
    earlier prescribed runs still reopen on ``--resume``.
    """
    btype = section_boundary_type(config, key, item)
    if not full and btype != "free_outflow":
        return {"distribution": section_distribution(config, key, item)}
    out: dict[str, Any] = {"boundary_type": btype, "distribution": section_distribution(config, key, item),
                           "discharge_series_use": "reference_only" if btype == "free_outflow" else "applied"}
    if btype == "free_outflow":
        settings = config.sections.outflow_boundary[key]
        out["discharge_coefficient"] = (FREE_OUTFLOW_DEFAULT_DISCHARGE_COEFFICIENT if settings.discharge_coefficient is None
                                        else float(settings.discharge_coefficient))
        out["crest_elevation_m"] = None if settings.crest_elevation_m is None else float(settings.crest_elevation_m)
        out["crest"] = "adjacent_face_bed" if settings.crest_elevation_m is None else "fixed_elevation"
    return out


def metadata_boundary_groups(config: LargeRunDriverConfig, sections: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"group_id": key, "role": item["role"], "name": item["name"], "section_status": item["section_status"],
             "discharge_series_status": config.sections.discharge_series_status[key]}
            for key, item in sections["sections"].items()]


def terrain_provenance(config: LargeRunDriverConfig, terrain: Mapping[str, Any] | None) -> dict[str, Any]:
    t = config.terrain
    mapping = None
    if t.mapping_report_path is not None:
        report = Path(t.mapping_report_path)
        mapping = {"path": str(report), "present": report.is_file(), "sha256": _sha_file(report) if report.is_file() else None}
    return {"source": "per-face bed elevation .npz matched by face_id", "path": t.path,
            "sha256": None if terrain is None else terrain["sha256"], "dem_path": t.dem_path, "dem_sha256_as_supplied": t.dem_sha256,
            "dem_present": Path(t.dem_path).is_file(), "dem_opened_by_driver": False, "mapping_report": mapping,
            "method": t.method, "vertical_datum": t.vertical_datum, "vertical_unit": t.vertical_unit,
            "elevation_range_m": None if terrain is None else [terrain["min_m"], terrain["max_m"]]}


def build_metadata(config: LargeRunDriverConfig, mesh: Mapping[str, Any], sections: Mapping[str, Any], forcing: Mapping[str, Any],
                   terrain: Mapping[str, Any] | None, options: TriangularLongRunOptions, times: tuple[float, ...],
                   config_sha256: str) -> LargeRunMetadata:
    groups = metadata_boundary_groups(config, sections)
    hashes = {"driver_config": config_sha256, "mesh_artifact": mesh["sha256"], "sections_audit": sections["sha256"],
              "forcing_csv": forcing["sha256"], "dem_as_supplied": config.terrain.dem_sha256}
    if terrain is not None:
        hashes["terrain_npz"] = terrain["sha256"]
    solver = {
        "name": SOLVER_NAME, "version": LONG_RUN_SOLVER_VERSION, "driver": "hydro_pipeline.large_run_driver",
        "driver_config_format": CONFIG_FORMAT, "options": options.model_dump(mode="json"),
        "numerics_fingerprint": options.numerics_fingerprint(), "kernel_requested": options.kernel,
        "initial_depth_m_uniform": float(config.run.initial_depth_m), "manning_n_uniform": float(config.run.manning_n),
        "rainfall": None,
        "mesh": {"path": mesh["path"], "mesh_id": mesh["mesh_id"], "face_count": mesh["n_face"], "node_count": mesh["n_node"]},
        "terrain": terrain_provenance(config, terrain),
        "sections": {"path": sections["path"], "format": SECTIONS_FORMAT, "mesh_id": sections["mesh_id"],
                     "groups": {key: {"role": item["role"], "edge_count": len(item["edge_indices"]), "discharge_series_column": item["column"],
                                      **section_boundary_summary(config, key, item, full=False),
                                      "discharge_series_status": config.sections.discharge_series_status[key], "width_m": item["width_m"]}
                                for key, item in sections["sections"].items()}},
        "forcing_csv": {"path": forcing["path"], "rows": forcing["rows"], "time_range_s": [forcing["first_time_s"], forcing["last_time_s"]],
                        "columns_used": sorted(forcing["series"])},
    }
    try:
        return parse_metadata({
            "run_id": config.run.run_id, "case_id": config.run.case_id, "time_origin": config.run.time_origin,
            "coordinate_system": config.run.coordinate_system, "vertical_datum": config.terrain.vertical_datum,
            "dry_depth_threshold_m": float(options.dry_depth_threshold_m), "expected_times_s": list(times),
            "data_origin": "LIVE_SOLVER_RUN", "live_solver_ran": True, "production_approved": False,
            "forcing": river_assumed_sections_forcing(groups).model_dump(mode="json"), "solver": solver, "source_hashes": hashes,
        })
    except (LargeRunProfileError, ValidationError) as exc:
        raise LargeRunDriverError(f"run metadata is invalid: {exc}") from exc


@dataclass
class PreparedCase:
    config: LargeRunDriverConfig
    config_sha256: str
    mesh_info: dict[str, Any]
    sections: dict[str, Any]
    forcing: dict[str, Any]
    terrain: dict[str, Any]
    times: tuple[float, ...]
    options: TriangularLongRunOptions
    profile: LargeRunOutputProfile
    metadata: LargeRunMetadata
    groups: list[dict[str, Any]]
    mesh: TriangularMesh | None = None
    inputs: TriangularLongRunInputs | None = None
    timings_s: dict[str, float] = field(default_factory=dict)

    def sink_mesh(self) -> dict[str, Any]:
        assert self.mesh is not None
        return {"node_xy_m": np.asarray(self.mesh.node_xy, dtype=np.float64), "face_nodes": np.asarray(self.mesh.face_nodes, dtype=np.int64),
                "face_id": np.asarray(self.mesh.face_id, dtype=np.int64), "bed_elevation_m": self.terrain["elevation_m"],
                "face_area_m2": np.asarray(self.mesh.face_area, dtype=np.float64)}


def prepare_case(config: LargeRunDriverConfig, config_sha256: str, *, build_mesh: bool = True, log: Any = None) -> PreparedCase:
    """Read and cross-check every input; optionally build the TriangularMesh and solver inputs."""
    say = log or (lambda message: None)
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    mesh_info = read_mesh_artifact(config.mesh.path, config.mesh.expected_sha256)
    if mesh_info["coordinate_system"] != config.run.coordinate_system:
        raise LargeRunDriverError(f"run.coordinate_system {config.run.coordinate_system!r} differs from the mesh artifact {mesh_info['coordinate_system']!r}")
    pairs, counts = edge_table(mesh_info["face_nodes"])
    mesh_info["n_edge"] = int(pairs.shape[0]); mesh_info["n_exterior_edge"] = int(np.count_nonzero(counts == 1))
    timings["mesh_artifact_read_s"] = time.perf_counter() - t0
    sections = read_sections_audit(config.sections.path)
    if sections["mesh_id"] is not None and sections["mesh_id"] != mesh_info["mesh_id"]:
        raise LargeRunDriverError(f"sections audit mesh_id {sections['mesh_id']!r} differs from the mesh artifact mesh_id {mesh_info['mesh_id']!r}")
    if sections["coordinate_system"] is not None and sections["coordinate_system"] != config.run.coordinate_system:
        raise LargeRunDriverError(f"sections audit crs {sections['coordinate_system']!r} differs from run.coordinate_system")
    check_sections_against_edges(sections["sections"], pairs, counts)
    times = config.run.times_s()
    options = config.run.options()
    forcing = read_forcing_csv(config.forcing.path, {key: item["column"] for key, item in sections["sections"].items()})
    check_forcing_coverage(forcing, times)
    groups = boundary_group_specs(config, sections, forcing)
    terrain = read_terrain(config.terrain.path, mesh_info["face_id"])
    try:
        profile = config.profile()
    except LargeRunProfileError as exc:
        raise LargeRunDriverError(f"output.profile: {exc}") from exc
    metadata = build_metadata(config, mesh_info, sections, forcing, terrain, options, times, config_sha256)
    case = PreparedCase(config, config_sha256, mesh_info, sections, forcing, terrain, times, options, profile, metadata, groups, timings_s=timings)
    if build_mesh:
        say(f"building TriangularMesh ({mesh_info['n_face']} faces) ...")
        t1 = time.perf_counter()
        case.mesh = build_triangular_mesh(mesh_info)
        timings["mesh_build_s"] = time.perf_counter() - t1
        if list(case.mesh.face_id) != mesh_info["face_id"].tolist():
            raise LargeRunDriverError("TriangularMesh face_id order differs from the artifact face order")
        for key, item in sections["sections"].items():
            if item.get("edge_nodes") is not None:
                given = [sorted(p) for p in item["edge_nodes"]]
                if [list(case.mesh.edge_nodes[i]) for i in item["edge_indices"]] != given:
                    raise LargeRunDriverError(f"section {key}: audit edge_nodes differ from TriangularMesh edge_nodes")
        try:
            case.inputs = TriangularLongRunInputs.build(
                case.mesh, bed_m=terrain["elevation_m"], initial_depth_m=np.full(case.mesh.n_face, float(config.run.initial_depth_m)),
                output_times_s=times, manning_n=np.full(case.mesh.n_face, float(config.run.manning_n)),
                rainfall_rate_m_s_by_interval=None, open_boundaries=groups)
        except TriangularLongRunError as exc:
            raise LargeRunDriverError(f"solver inputs are invalid: {exc}") from exc
    return case


# ---------------------------------------------------------------------------
# Ledger adapters (solver row -> sink per-frame ledger / close ledger)
# ---------------------------------------------------------------------------
_SINK_ROW_KEYS = (
    "initial_volume_m3", "interval_rainfall_volume_m3", "interval_inflow_volume_m3", "interval_outflow_requested_volume_m3",
    "interval_outflow_delivered_volume_m3", "interval_outflow_shortfall_volume_m3", "interval_correction_volume_m3",
    "cumulative_rainfall_volume_m3", "cumulative_inflow_volume_m3", "cumulative_outflow_requested_volume_m3",
    "cumulative_outflow_delivered_volume_m3", "cumulative_outflow_shortfall_volume_m3", "cumulative_correction_volume_m3",
    "residual_m3", "volume_correction_reference_scale_m3", "minimum_water_depth_m", "maximum_water_depth_m",
    "interval_accepted_step_count", "interval_rejected_step_count",
)


def sink_ledger_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Finite scalars only; ``volume_m3`` lets the sink cross-check sum(h*A). Wall clock and None-able step sizes stay in ledger.jsonl."""
    out: dict[str, Any] = {"volume_m3": float(row["stored_volume_m3"])}
    for key in _SINK_ROW_KEYS:
        out[key] = float(row[key])
    out["groups"] = {name: {key: float(value) for key, value in values.items()} for name, values in row.get("groups", {}).items()}
    return out


def close_ledger_from_summary(summary: TriangularLongRunSummary, roles: Mapping[str, str]) -> dict[str, Any]:
    last = summary.ledger[-1]
    groups = {}
    for name, values in last.get("groups", {}).items():
        role = roles[name]
        entry = {"role": role, "cumulative_inflow_volume_m3": values["cumulative_inflow_volume_m3"],
                 "cumulative_outflow_requested_volume_m3": values["cumulative_outflow_requested_volume_m3"],
                 "cumulative_outflow_delivered_volume_m3": values["cumulative_outflow_delivered_volume_m3"],
                 "outflow_shortfall_m3": values["cumulative_outflow_shortfall_volume_m3"],
                 "volume_m3": values["cumulative_inflow_volume_m3"] if role == "inflow" else values["cumulative_outflow_delivered_volume_m3"]}
        if role == "outflow":
            entry["requested_volume_m3"] = values["cumulative_outflow_requested_volume_m3"]
        if role == "free_outflow":
            # requested = uncapped weir discharge integrated over the run; reference = CSV comparison series only.
            entry["outflow_discharge_m3_s"] = values["outflow_discharge_m3_s"]
            if "cumulative_reference_outflow_volume_m3" in values:
                entry["cumulative_reference_outflow_volume_m3"] = values["cumulative_reference_outflow_volume_m3"]
        groups[name] = entry
    return {
        "initial_volume_m3": last["initial_volume_m3"], "final_stored_volume_m3": last["stored_volume_m3"],
        "cumulative_rainfall_volume_m3": last["cumulative_rainfall_volume_m3"], "cumulative_inflow_volume_m3": last["cumulative_inflow_volume_m3"],
        "cumulative_outflow_requested_volume_m3": last["cumulative_outflow_requested_volume_m3"],
        "cumulative_outflow_delivered_volume_m3": last["cumulative_outflow_delivered_volume_m3"],
        "outflow_shortfall_m3": last["cumulative_outflow_shortfall_volume_m3"],
        "cumulative_correction_volume_m3": last["cumulative_correction_volume_m3"], "residual_m3": last["residual_m3"],
        "groups": groups, "accepted_step_count": summary.accepted_step_count, "rejected_step_count": summary.rejected_step_count,
        "rejected_step_reasons": {key: int(value) for key, value in summary.rejected_step_reasons.items() if _KEY.match(key) and "__" not in key},
        "mass_correction_count": summary.mass_correction_count,
        "minimum_accepted_step_s": summary.minimum_accepted_step_s, "maximum_accepted_step_s": summary.maximum_accepted_step_s,
        "maximum_internal_edge_mass_pair_residual_m3_s": summary.maximum_internal_edge_mass_pair_residual_m3_s,
        "maximum_manning_gamma_per_s": summary.maximum_manning_gamma_per_s,
        "volume_correction_reference_scale_m3": summary.volume_correction_reference_scale_m3,
        "volume_correction_budget_m3": summary.volume_correction_budget_m3,
        "kernel": summary.kernel, "solver_version": LONG_RUN_SOLVER_VERSION,
    }


# ---------------------------------------------------------------------------
# Run-directory files
# ---------------------------------------------------------------------------
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, value: Any) -> None:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


class _Progress:
    """Append-only progress.jsonl writer (one JSON object per line, flushed)."""

    def __init__(self, path: Path, log: Any = None):
        self.path = path
        self.log = log
        self.started = time.perf_counter()
        self._stream = path.open("a", encoding="utf-8")

    def event(self, name: str, **fields: Any) -> None:
        record = {"event": name, "utc": _utc_now(), "elapsed_s": round(time.perf_counter() - self.started, 3), **fields}
        self._stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        self._stream.flush()
        if self.log is not None:
            self.log(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))

    def close(self) -> None:
        self._stream.close()


class _FrameWriter:
    """Solver frame sink: writes new frames to the NetCDF sink, skips frames already present (resume)."""

    def __init__(self, sink: NativeUgridNetcdfSink, skip_below: int, ledger_path: Path, progress: _Progress, frame_count: int):
        self.sink = sink; self.skip_below = skip_below; self.ledger_path = ledger_path; self.progress = progress
        self.frame_count = frame_count
        self.written: list[int] = []; self.skipped: list[int] = []

    def __call__(self, frame: TriangularFrame) -> None:
        index = int(frame.output_index)
        if index < self.skip_below:
            self.skipped.append(index)
            self.progress.event("frame_already_present", index=index, time_s=frame.time_s)
            return
        started = time.perf_counter()
        self.sink.write_frame(index, frame.time_s, {
            "water_depth_m": frame.water_depth_m, "unit_discharge_x_m2_s": frame.unit_discharge_x_m2_s,
            "unit_discharge_y_m2_s": frame.unit_discharge_y_m2_s, "velocity_x_m_s": frame.velocity_x_m_s,
            "velocity_y_m_s": frame.velocity_y_m_s, "water_level_m": frame.water_level_m,
        }, ledger=sink_ledger_from_row(frame.ledger))
        with self.ledger_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(frame.ledger, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
        self.written.append(index)
        row = frame.ledger
        self.progress.event("frame_written", index=index, of=self.frame_count, time_s=frame.time_s,
                            sink_write_s=round(time.perf_counter() - started, 3), stored_volume_m3=row["stored_volume_m3"],
                            maximum_water_depth_m=row["maximum_water_depth_m"], residual_m3=row["residual_m3"],
                            interval_accepted_step_count=row["interval_accepted_step_count"],
                            interval_wall_clock_s=row["interval_wall_clock_s"])


def _versions() -> dict[str, Any]:
    out: dict[str, Any] = {"python": platform.python_version(), "numpy": np.__version__, "platform": platform.platform()}
    for name in ("numba", "netCDF4", "pyproj", "pydantic", "shapefile"):
        try:
            module = __import__(name)
            out[name] = getattr(module, "__version__", None) or getattr(module, "VERSION", None)
        except Exception:
            out[name] = None
    out["numba_kernel_status"] = numba_status()
    return out


def newest_checkpoint(directory: Path) -> Path | None:
    found = sorted(directory.glob("checkpoint_*.npz")) if directory.is_dir() else []
    return found[-1] if found else None


def _summary_dict(summary: TriangularLongRunSummary) -> dict[str, Any]:
    return {"status": summary.status, "final_time_s": summary.final_time_s, "accepted_step_count": summary.accepted_step_count,
            "rejected_step_count": summary.rejected_step_count, "rejected_step_reasons": dict(summary.rejected_step_reasons),
            "mass_correction_count": summary.mass_correction_count, "minimum_accepted_step_s": summary.minimum_accepted_step_s,
            "maximum_accepted_step_s": summary.maximum_accepted_step_s,
            "maximum_internal_edge_mass_pair_residual_m3_s": summary.maximum_internal_edge_mass_pair_residual_m3_s,
            "maximum_manning_gamma_per_s": summary.maximum_manning_gamma_per_s,
            "volume_correction_reference_scale_m3": summary.volume_correction_reference_scale_m3,
            "volume_correction_budget_m3": summary.volume_correction_budget_m3, "kernel": summary.kernel,
            "wall_clock_s": summary.wall_clock_s, "checkpoints_written": list(summary.checkpoints_written),
            "resumed_from": summary.resumed_from, "ledger_rows": len(summary.ledger)}


# ---------------------------------------------------------------------------
# Branch: continue a COMPLETE parent run from one of its checkpoints with
# boundary series that differ only after the checkpoint time.
# ---------------------------------------------------------------------------
_BRANCH_SERIES = (("time_s", "discharge_m3_s"), ("reference_time_s", "reference_discharge_m3_s"))


def _series_equal_until(t_a: Any, q_a: Any, t_b: Any, q_b: Any, until_s: float) -> bool:
    """Piecewise-linear series equal on [first sample, until_s]: compare at every breakpoint of both, and at until_s."""
    if t_a is None or t_b is None:
        return t_a is None and t_b is None
    ta, qa, tb, qb = (np.asarray(v, dtype=np.float64) for v in (t_a, q_a, t_b, q_b))
    if ta[0] != tb[0]:
        return False
    points = np.unique(np.concatenate([ta[ta <= until_s], tb[tb <= until_s], [until_s]]))
    return bool(np.array_equal(np.interp(points, ta, qa), np.interp(points, tb, qb)))


def verify_branch_inputs(parent: TriangularLongRunInputs, child: TriangularLongRunInputs, checkpoint_fingerprint: str,
                         branch_time_s: float) -> dict[str, Any]:
    """Prove the child inputs equal the parent's on [0, branch_time_s]; return the changed series.

    1. The parent inputs (rebuilt from the parent's resolved config) reproduce the checkpoint fingerprint.
    2. The child inputs with the parent's boundary groups swapped in reproduce it too: mesh, bed, initial
       depth, Manning n, rainfall and output times are identical.
    3. Every boundary group keeps its name, role, edges and every non-series field; its discharge (and
       free-outflow reference) series is identical up to branch_time_s.
    """
    parent_fp = _inputs_fingerprint(parent, _edges(parent.mesh).geometry)
    if parent_fp != checkpoint_fingerprint:
        raise LargeRunDriverError("branch: the parent config does not reproduce the checkpoint inputs fingerprint "
                                  "(parent inputs changed on disk since the run?)")
    hybrid = dataclasses.replace(child, open_boundaries=parent.open_boundaries)
    if _inputs_fingerprint(hybrid, _edges(child.mesh).geometry) != checkpoint_fingerprint:
        raise LargeRunDriverError("branch: mesh, terrain, initial depth, Manning n, rainfall or output times differ from the parent; "
                                  "only boundary discharge series after the branch time may change")
    parents = {g.name: g for g in parent.open_boundaries}
    children = {g.name: g for g in child.open_boundaries}
    if list(parents) != list(children):
        raise LargeRunDriverError(f"branch: boundary groups differ: parent {list(parents)} vs child {list(children)}")
    series_fields = {name for pair in _BRANCH_SERIES for name in pair}
    changed = []
    for name, pg in parents.items():
        cg = children[name]
        p_rest = {k: v for k, v in pg.model_dump(mode="json").items() if k not in series_fields}
        c_rest = {k: v for k, v in cg.model_dump(mode="json").items() if k not in series_fields}
        if p_rest != c_rest:
            raise LargeRunDriverError(f"branch: group {name!r} changed more than its discharge series")
        for t_name, q_name in _BRANCH_SERIES:
            pt, pq, ct, cq = getattr(pg, t_name), getattr(pg, q_name), getattr(cg, t_name), getattr(cg, q_name)
            if not _series_equal_until(pt, pq, ct, cq, branch_time_s):
                raise LargeRunDriverError(f"branch: group {name!r} {q_name} differs before the branch time {branch_time_s} s")
            if (pt, pq) != (ct, cq):
                samples = sorted(set(map(float, pt or [])) | set(map(float, ct or [])))
                differing = [t for t in samples if float(np.interp(t, pt, pq)) != float(np.interp(t, ct, cq))]
                changed.append({"group": name, "series": q_name, "first_differing_sample_time_s": differing[0] if differing else None,
                                "differing_sample_count": len(differing)})
    if not changed:
        raise LargeRunDriverError("branch: the inputs are identical to the parent; use --resume or a fresh run instead")
    return {"parent_inputs_fingerprint": parent_fp, "branch_time_s": branch_time_s, "changed_series": changed}


def _seed_frames_from_parent(sink: NativeUgridNetcdfSink, parent_native: Path, count: int, ledger_rows: list[dict[str, Any]],
                             ledger_path: Path, progress: Any, dry_depth_m: float) -> None:
    """Copy parent frames 0..count-1 (identical by construction) into the child sink, with the checkpoint ledger rows.

    Depth and momentum are stored in float64 and copied exactly; velocities are stored as float32, so they are rebuilt
    in float64 from momentum / depth on wet faces exactly as the solver does (the sink derives max speed from them).
    """
    if len(ledger_rows) < count:
        raise LargeRunDriverError(f"branch: checkpoint ledger holds {len(ledger_rows)} rows, need {count}")
    parent = LargeNativeResult(parent_native)
    try:
        if parent.n_frame < count:
            raise LargeRunDriverError(f"branch: parent result holds {parent.n_frame} frames, need {count}")
        with ledger_path.open("a", encoding="utf-8") as stream:
            for index in range(count):
                frame = parent.frame(index)
                h = frame.water_depth_m; wet = h > dry_depth_m
                u = np.zeros_like(h); v = np.zeros_like(h)
                np.divide(frame.momentum_x_m2_s, h, out=u, where=wet)
                np.divide(frame.momentum_y_m2_s, h, out=v, where=wet)
                sink.write_frame(index, frame.time_s, {
                    "water_depth_m": h, "unit_discharge_x_m2_s": frame.momentum_x_m2_s,
                    "unit_discharge_y_m2_s": frame.momentum_y_m2_s, "velocity_x_m_s": u,
                    "velocity_y_m_s": v, "water_level_m": frame.water_level_m,
                }, ledger=sink_ledger_from_row(ledger_rows[index]))
                stream.write(json.dumps(ledger_rows[index], ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
    finally:
        parent.close()
    progress.event("branch_frames_seeded", count=count, parent=str(parent_native))


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def run_large_case(config_path: str | Path, *, resume: bool = False, log: Any = None, script_id: str | None = None,
                   branch_from: str | Path | None = None) -> dict[str, Any]:
    """Run (or resume) one case end-to-end; returns the final run_report dict. Raises on failure after writing it.

    ``branch_from``: a checkpoint of a COMPLETE parent run.  The new run (fresh directory) copies the parent frames up to
    the checkpoint and continues from it; allowed only when the inputs differ from the parent's solely in boundary
    discharge series after the checkpoint time (see :func:`verify_branch_inputs`).
    """
    if branch_from is not None and resume:
        raise LargeRunDriverError("--branch-from starts a new run directory; it cannot be combined with --resume")
    say = log or (lambda message: None)
    config_file = Path(config_path).resolve()
    config = load_driver_config(config_file, script_id=script_id)
    config_sha = _sha_file(config_file)
    out = Path(config.output.directory)
    bundle = out / BUNDLE_SUBDIR
    ckpt_dir = out / CHECKPOINT_SUBDIR
    report_path = out / "run_report.json"
    receipt_path = out / "run_receipt.json"
    native_path = bundle / "result_native.nc"
    started_utc = _utc_now()
    wall_start = time.perf_counter()

    if resume:
        if not out.is_dir() or not report_path.is_file():
            raise LargeRunDriverError(f"--resume needs an existing run directory with run_report.json: {out}")
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        if previous.get("status") == "complete":
            raise LargeRunDriverError(f"run {out} is already complete; nothing to resume")
        if previous.get("run_id") != config.run.run_id:
            raise LargeRunDriverError(f"run_report.json run_id {previous.get('run_id')!r} differs from config run_id {config.run.run_id!r}")
        if config.run.checkpoint_interval_s is None:
            raise LargeRunDriverError("--resume requires run.checkpoint_interval_s (the failed run wrote no checkpoints)")
        checkpoint = newest_checkpoint(ckpt_dir)
        if checkpoint is None and not native_path.is_file():
            raise LargeRunDriverError(f"no checkpoint_*.npz in {ckpt_dir}; the run cannot be resumed (start a fresh run directory)")
        resume_count = int(previous.get("resume_count", 0)) + 1
    else:
        if out.exists():
            raise LargeRunDriverError(f"output.directory exists; refusing to overwrite (use --resume to continue a failed run): {out}")
        out.mkdir(parents=True, exist_ok=False)
        bundle.mkdir()
        checkpoint = None
        resume_count = 0

    branch: dict[str, Any] | None = None
    if branch_from is not None:
        branch_ckpt = Path(branch_from).resolve()
        parent_dir = branch_ckpt.parent.parent
        parent_native = parent_dir / BUNDLE_SUBDIR / "result_native.nc"
        if not branch_ckpt.is_file() or branch_ckpt.parent.name != CHECKPOINT_SUBDIR:
            raise LargeRunDriverError(f"--branch-from must be a checkpoint_*.npz inside a run's {CHECKPOINT_SUBDIR}/: {branch_ckpt}")
        parent_report = (json.loads((parent_dir / "run_report.json").read_text(encoding="utf-8"))
                         if (parent_dir / "run_report.json").is_file() else {})
        if parent_report.get("status") != "complete" or not parent_native.is_file():
            raise LargeRunDriverError(f"--branch-from needs a COMPLETE parent run (run_report status complete): {parent_dir}")
        branch = {"parent_run_directory": str(parent_dir), "checkpoint": str(branch_ckpt), "checkpoint_sha256": _sha_file(branch_ckpt),
                  "parent_run_id": parent_report.get("run_id"), "parent_native_result_sha256": _sha_file(parent_native)}
    report: dict[str, Any] = {"format": REPORT_FORMAT, "status": "running", "run_id": config.run.run_id, "case_id": config.run.case_id,
                              "config": str(config_file), "config_sha256": config_sha, "started_utc": started_utc, "finished_utc": None,
                              "resume": resume, "resume_count": resume_count, "resumed_from_checkpoint": None if checkpoint is None else str(checkpoint),
                              "frames_expected": None, "frames_written_this_process": 0, "frames_present_before": 0,
                              "bundle": BUNDLE_SUBDIR, "export": {"requested": config.output.export, "status": "pending"},
                              "verify": {"status": "pending"}, "error": None, "branch": branch}
    _write_json_atomic(report_path, report)
    _write_json_atomic(out / "config.resolved.json", config.model_dump(mode="json"))
    progress = _Progress(out / "progress.jsonl", log)
    progress.event("start", resume=resume, resume_count=resume_count, config=str(config_file), config_sha256=config_sha, pid=os.getpid())
    sink: NativeUgridNetcdfSink | None = None
    receipt: dict[str, Any] = {}
    try:
        case = prepare_case(config, config_sha, build_mesh=not native_path.is_file(), log=lambda m: progress.event("info", message=m))
        times = case.times
        report["frames_expected"] = len(times)
        progress.event("inputs_ready", faces=case.mesh_info["n_face"], nodes=case.mesh_info["n_node"], frames=len(times),
                       last_time_s=times[-1], groups=[g["name"] for g in case.groups], timings_s=case.timings_s)
        receipt = {"format": RECEIPT_FORMAT, "run_id": config.run.run_id, "case_id": config.run.case_id, "config": str(config_file),
                   "config_sha256": config_sha, "source_hashes": dict(case.metadata.source_hashes),
                   "mesh": {"path": case.mesh_info["path"], "mesh_id": case.mesh_info["mesh_id"], "face_count": case.mesh_info["n_face"],
                            "node_count": case.mesh_info["n_node"], "edge_count": case.mesh_info["n_edge"],
                            "exterior_edge_count": case.mesh_info["n_exterior_edge"], "coordinate_system": case.mesh_info["coordinate_system"]},
                   "terrain": case.metadata.solver["terrain"], "sections": case.metadata.solver["sections"],
                   "forcing_csv": case.metadata.solver["forcing_csv"], "rainfall": None,
                   "run": {"frames": len(times), "first_time_s": times[0], "last_time_s": times[-1], "time_origin": config.run.time_origin,
                           "initial_depth_m": float(config.run.initial_depth_m), "manning_n": float(config.run.manning_n),
                           "checkpoint_interval_s": config.run.checkpoint_interval_s},
                   "options": case.options.model_dump(mode="json"), "numerics_fingerprint": case.options.numerics_fingerprint(),
                   "profile": case.profile.model_dump(mode="json"), "versions": _versions(),
                   "forcing_label": case.metadata.forcing.model_dump(mode="json"),
                   "resume": {"resume": resume, "resume_count": resume_count, "checkpoint": None if checkpoint is None else str(checkpoint)},
                   "timings_s": dict(case.timings_s), "solver": None, "sink": None, "export": None, "verify": None, "branch": branch}
        if native_path.is_file():
            # Solver and sink finished in an earlier process; only export/verify remain.
            progress.event("native_result_already_complete", path=str(native_path))
            previous_receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else {}
            for key in ("solver", "sink", "timings_s"):
                if key in previous_receipt:
                    receipt[key] = previous_receipt[key]
            leftovers = sorted(p.name for p in bundle.iterdir() if p.name != "result_native.nc")
            if leftovers:
                raise LargeRunDriverError(f"bundle holds partial export outputs {leftovers[:5]}; remove every bundle entry except result_native.nc, then --resume")
        else:
            assert case.inputs is not None and case.mesh is not None
            sink = NativeUgridNetcdfSink(native_path, case.profile)
            present = 0
            if resume:
                present = sink.reopen(case.sink_mesh(), case.metadata)
                if checkpoint is None:
                    raise LargeRunDriverError("partial native result exists but no checkpoint; cannot resume")
                metadata, _ = load_long_run_checkpoint(checkpoint)
                if int(metadata["output_index"]) + 1 > present:
                    raise LargeRunDriverError(f"checkpoint {checkpoint.name} is at output index {metadata['output_index']} but the partial result holds only {present} frames")
                progress.event("sink_reopened", frames_present=present, checkpoint=str(checkpoint), checkpoint_output_index=int(metadata["output_index"]))
            else:
                sink.open(case.sink_mesh(), case.metadata)
                progress.event("sink_opened", path=str(native_path))
            resume_fingerprint = None
            if branch is not None:
                metadata, _ = load_long_run_checkpoint(branch["checkpoint"])
                parent_resolved = Path(branch["parent_run_directory"]) / "config.resolved.json"
                parent_config = parse_driver_config(json.loads(parent_resolved.read_text(encoding="utf-8")))
                if parent_config.run.time_origin != config.run.time_origin:
                    raise LargeRunDriverError("branch: run.time_origin differs from the parent")
                parent_case = prepare_case(parent_config, _sha_file(parent_resolved), build_mesh=True,
                                           log=lambda m: progress.event("info", message=f"parent: {m}"))
                if parent_case.options.numerics_fingerprint() != case.options.numerics_fingerprint():
                    raise LargeRunDriverError("branch: numerical options differ from the parent")
                assert parent_case.inputs is not None
                branch.update(verify_branch_inputs(parent_case.inputs, case.inputs, metadata["inputs_fingerprint"], float(metadata["time_s"])))
                branch["checkpoint_output_index"] = int(metadata["output_index"])
                parent_case = None
                progress.event("branch_verified", **branch)
                present = int(metadata["output_index"]) + 1
                _seed_frames_from_parent(sink, Path(branch["parent_run_directory"]) / BUNDLE_SUBDIR / "result_native.nc", present,
                                         list(metadata["ledger"]), out / "ledger.jsonl", progress,
                                         float(case.options.dry_depth_threshold_m))
                checkpoint = Path(branch["checkpoint"])
                resume_fingerprint = metadata["inputs_fingerprint"]
                report["branch"] = branch
                receipt["branch"] = branch
                _write_json_atomic(report_path, report)
            report["frames_present_before"] = present
            receipt["resume"]["frames_present_in_partial"] = present
            writer = _FrameWriter(sink, present, out / "ledger.jsonl", progress, len(times))
            _write_json_atomic(receipt_path, receipt)

            def on_progress(info: Mapping[str, Any]) -> None:
                progress.event("solver_progress", **{k: (float(v) if isinstance(v, (np.floating, float)) else v) for k, v in info.items()})
                report["frames_written_this_process"] = len(writer.written)

            t_solver = time.perf_counter()
            summary = run_triangular_long_run(case.inputs, case.options, frame_sink=writer, checkpoint_directory=ckpt_dir,
                                              resume_from=checkpoint, progress_callback=on_progress,
                                              resume_inputs_fingerprint=resume_fingerprint)
            receipt["timings_s"]["solver_s"] = time.perf_counter() - t_solver
            report["frames_written_this_process"] = len(writer.written)
            receipt["solver"] = _summary_dict(summary)
            receipt["resume"]["frames_skipped"] = writer.skipped
            progress.event("solver_done", **{k: v for k, v in receipt["solver"].items() if k != "ledger_rows"})
            roles = {g["name"]: g["role"] for g in case.groups}
            t_close = time.perf_counter()
            sink_report = sink.close(
                {"max_water_depth_m": summary.maximum_depth_m, "max_speed_m_s": summary.maximum_speed_m_s,
                 "time_of_max_water_depth_s": summary.time_of_maximum_depth_s},
                close_ledger_from_summary(summary, roles))
            sink = None
            receipt["timings_s"]["sink_close_s"] = time.perf_counter() - t_close
            receipt["sink"] = sink_report
            progress.event("sink_closed", **sink_report)
            ledger_path = out / "ledger.jsonl"
            temporary = ledger_path.with_name("ledger.jsonl.tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                for row in summary.ledger:
                    stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
            os.replace(temporary, ledger_path)
            case.inputs = None; case.mesh = None
            _write_json_atomic(receipt_path, receipt)
        if config.output.export:
            t_export = time.perf_counter()
            export = export_large_run_bundle(bundle, case.profile, progress=lambda m: progress.event("export", message=m))
            receipt["timings_s"]["export_s"] = time.perf_counter() - t_export
            receipt["export"] = export
            report["export"] = {"requested": True, "status": "complete", "file_count": export["file_count"], "total_bytes": export["total_bytes"]}
            progress.event("export_done", file_count=export["file_count"], total_bytes=export["total_bytes"])
            t_verify = time.perf_counter()
            verify = verify_large_run_bundle(bundle)
            receipt["timings_s"]["verify_s"] = time.perf_counter() - t_verify
            receipt["verify"] = verify
            report["verify"] = {"status": verify["status"]}
            progress.event("verify_done", status=verify["status"])
            if verify["status"] != "PASS":
                raise LargeRunDriverError(f"bundle verification did not pass: {verify}")
        else:
            report["export"] = {"requested": False, "status": "skipped"}
            report["verify"] = {"status": "skipped"}
        receipt["timings_s"]["total_this_process_s"] = time.perf_counter() - wall_start
        receipt["checkpoints_kept"] = sorted(p.name for p in ckpt_dir.glob("checkpoint_*.npz")) if ckpt_dir.is_dir() else []
        _write_json_atomic(receipt_path, receipt)
        report.update({"status": "complete", "finished_utc": _utc_now(), "wall_clock_s": time.perf_counter() - wall_start,
                       "receipt": "run_receipt.json", "checkpoints_kept": receipt["checkpoints_kept"]})
        _write_json_atomic(report_path, report)
        progress.event("complete", wall_clock_s=report["wall_clock_s"])
        return report
    except BaseException as exc:
        if sink is not None:
            try:
                sink.abort(f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        report.update({"status": "failed", "finished_utc": _utc_now(), "wall_clock_s": time.perf_counter() - wall_start,
                       "error": {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()},
                       "checkpoints_kept": sorted(p.name for p in ckpt_dir.glob("checkpoint_*.npz")) if ckpt_dir.is_dir() else []})
        try:
            _write_json_atomic(report_path, report)
            if receipt:
                receipt["timings_s"]["total_this_process_s"] = time.perf_counter() - wall_start
                receipt["failed"] = {"type": type(exc).__name__, "message": str(exc)}
                _write_json_atomic(receipt_path, receipt)
            progress.event("failed", type=type(exc).__name__, message=str(exc))
        finally:
            pass
        raise
    finally:
        progress.close()


# ---------------------------------------------------------------------------
# --check: validate configuration and inputs without building the mesh
# ---------------------------------------------------------------------------
def check_case(config_path: str | Path, *, resume: bool = False, script_id: str | None = None) -> dict[str, Any]:
    """Validate the config and every input; nothing heavy is built. Status ok | missing_inputs | invalid."""
    config_file = Path(config_path).resolve()
    report: dict[str, Any] = {"format": CHECK_FORMAT, "config": str(config_file), "status": "ok", "errors": [], "missing": [],
                              "warnings": [], "inputs": {}, "run": {}, "environment": {}}
    errors: list[str] = report["errors"]
    try:
        config = load_driver_config(config_file, script_id=script_id)
    except LargeRunDriverError as exc:
        errors.append(str(exc)); report["status"] = "invalid"
        return report
    report["config_sha256"] = _sha_file(config_file)
    report["run"] = {"run_id": config.run.run_id, "case_id": config.run.case_id, "time_origin": config.run.time_origin,
                     "coordinate_system": config.run.coordinate_system, "initial_depth_m": config.run.initial_depth_m,
                     "manning_n": config.run.manning_n, "checkpoint_interval_s": config.run.checkpoint_interval_s,
                     "export": config.output.export, "output_directory": config.output.directory,
                     "scheme_id": getattr(config, "_scheme_id", None)}
    times = config.run.times_s()
    report["run"].update({"frames": len(times), "last_time_s": times[-1]})
    options = config.run.options()
    report["run"]["options"] = options.model_dump(mode="json")
    report["run"]["numerics_fingerprint"] = options.numerics_fingerprint()
    try:
        profile = config.profile()
        report["run"]["profile"] = profile.model_dump(mode="json")
    except LargeRunProfileError as exc:
        errors.append(f"output.profile: {exc}"); profile = None

    mesh_info = None
    try:
        t0 = time.perf_counter()
        mesh_info = read_mesh_artifact(config.mesh.path, config.mesh.expected_sha256)
        pairs, counts = edge_table(mesh_info["face_nodes"])
        xy = mesh_info["node_xy"]
        report["inputs"]["mesh"] = {"path": mesh_info["path"], "sha256": mesh_info["sha256"], "mesh_id": mesh_info["mesh_id"],
                                   "coordinate_system": mesh_info["coordinate_system"], "face_count": mesh_info["n_face"],
                                   "node_count": mesh_info["n_node"], "edge_count": int(pairs.shape[0]),
                                   "exterior_edge_count": int(np.count_nonzero(counts == 1)),
                                   "bounds_m": [float(xy[:, 0].min()), float(xy[:, 1].min()), float(xy[:, 0].max()), float(xy[:, 1].max())],
                                   "boundary_groups": {k: len(v) for k, v in mesh_info["boundary_groups"].items()},
                                   "read_s": round(time.perf_counter() - t0, 2)}
        if mesh_info["coordinate_system"] != config.run.coordinate_system:
            errors.append(f"run.coordinate_system {config.run.coordinate_system!r} differs from the mesh artifact {mesh_info['coordinate_system']!r}")
    except LargeRunDriverError as exc:
        errors.append(str(exc)); pairs = counts = None

    sections = None
    try:
        sections = read_sections_audit(config.sections.path)
        report["inputs"]["sections"] = {"path": sections["path"], "sha256": sections["sha256"], "mesh_id": sections["mesh_id"],
                                       "crs": sections["coordinate_system"], "section_status": sections["section_status"],
                                       "sections": {key: {"role": item["role"], "edge_count": len(item["edge_indices"]), "column": item["column"],
                                                          "name": item["name"], "width_m": item["width_m"],
                                                          "discharge_series_status": config.sections.discharge_series_status.get(key)}
                                                    for key, item in sections["sections"].items()}}
        for key, item in sections["sections"].items():
            try:
                report["inputs"]["sections"]["sections"][key].update(section_boundary_summary(config, key, item))
            except LargeRunDriverError as exc:
                errors.append(str(exc))
        if mesh_info is not None:
            if sections["mesh_id"] is not None and sections["mesh_id"] != mesh_info["mesh_id"]:
                errors.append(f"sections audit mesh_id {sections['mesh_id']!r} differs from the mesh artifact mesh_id {mesh_info['mesh_id']!r}")
            if sections["coordinate_system"] is not None and sections["coordinate_system"] != config.run.coordinate_system:
                errors.append(f"sections audit crs {sections['coordinate_system']!r} differs from run.coordinate_system")
            try:
                check_sections_against_edges(sections["sections"], pairs, counts)
                report["inputs"]["sections"]["edges_exterior_and_matching"] = True
            except LargeRunDriverError as exc:
                errors.append(str(exc))
        missing = sorted(set(sections["sections"]) - set(config.sections.discharge_series_status))
        if missing:
            errors.append(f"sections.discharge_series_status is required for every audit section; missing {missing}")
        unknown = sorted((set(config.sections.discharge_series_status) | set(config.sections.distribution)
                          | set(config.sections.outflow_boundary)) - set(sections["sections"]))
        if unknown:
            errors.append(f"sections config names unknown sections {unknown}")
    except LargeRunDriverError as exc:
        errors.append(str(exc))

    forcing = None
    if sections is not None:
        try:
            forcing = read_forcing_csv(config.forcing.path, {key: item["column"] for key, item in sections["sections"].items()})
            report["inputs"]["forcing"] = {"path": forcing["path"], "sha256": forcing["sha256"], "rows": forcing["rows"],
                                          "time_range_s": [forcing["first_time_s"], forcing["last_time_s"]], "columns": forcing["columns"],
                                          "series_range_m3_s": {name: [float(v.min()), float(v.max())] for name, v in forcing["series"].items()},
                                          "rainfall": None}
            check_forcing_coverage(forcing, times)
        except LargeRunDriverError as exc:
            errors.append(str(exc))

    terrain = None
    terrain_path = Path(config.terrain.path)
    if not terrain_path.is_file():
        report["missing"].append(str(terrain_path))
        report["inputs"]["terrain"] = {"path": str(terrain_path), "present": False}
    elif mesh_info is not None:
        try:
            terrain = read_terrain(terrain_path, mesh_info["face_id"])
            report["inputs"]["terrain"] = {"path": terrain["path"], "sha256": terrain["sha256"], "present": True, "face_count": terrain["face_count"],
                                          "elevation_range_m": [terrain["min_m"], terrain["max_m"]], "mean_m": terrain["mean_m"]}
        except LargeRunDriverError as exc:
            errors.append(str(exc))
    report["inputs"]["terrain_provenance"] = terrain_provenance(config, terrain)
    if not Path(config.terrain.dem_path).is_file():
        report["warnings"].append(f"terrain.dem_path is not present on this machine (provenance only): {config.terrain.dem_path}")
    if config.terrain.mapping_report_path is not None and not Path(config.terrain.mapping_report_path).is_file():
        report["warnings"].append(f"terrain.mapping_report_path is not present (provenance only): {config.terrain.mapping_report_path}")

    if mesh_info is not None and sections is not None and forcing is not None:
        try:
            boundary_group_specs(config, sections, forcing)
            meta = build_metadata(config, mesh_info, sections, forcing, terrain, options, times, report["config_sha256"])
            report["run"]["forcing_label"] = meta.forcing.model_dump(mode="json")
            report["run"]["data_origin"] = meta.data_origin
        except LargeRunDriverError as exc:
            errors.append(str(exc))

    out = Path(config.output.directory)
    report["run"]["output_directory_exists"] = out.exists()
    if out.exists() and not resume:
        errors.append(f"output.directory exists; a fresh run refuses to overwrite it: {out}")
    if resume:
        ckpt = newest_checkpoint(out / CHECKPOINT_SUBDIR)
        report["run"]["newest_checkpoint"] = None if ckpt is None else str(ckpt)
        if config.run.checkpoint_interval_s is None:
            errors.append("--resume requires run.checkpoint_interval_s")
    elif config.run.checkpoint_interval_s is None:
        report["warnings"].append("run.checkpoint_interval_s is null: no checkpoints, --resume will be impossible")

    env: dict[str, Any] = report["environment"]
    env["versions"] = _versions()
    try:
        from pyproj import CRS
        crs = CRS.from_user_input(config.run.coordinate_system)
        env["crs"] = {"name": crs.name, "projected": bool(crs.is_projected)}
        if not crs.is_projected:
            errors.append("run.coordinate_system must be a projected CRS in metres")
    except Exception as exc:
        errors.append(f"coordinate_system cannot be resolved by pyproj: {exc}")
    if mesh_info is not None and profile is not None:
        try:
            grid = display_grid_extent(mesh_info["node_xy"], profile)
            need = estimate_bundle_bytes(mesh_info["n_face"], mesh_info["n_node"], len(times), grid["nx"] * grid["ny"], profile)
            probe = out if out.exists() else next((p for p in out.parents if p.exists()), Path.cwd())
            import shutil
            free = shutil.disk_usage(probe).free
            env["disk"] = {"display_grid": [grid["nx"], grid["ny"], grid["dx"]], "estimate_bytes": int(need),
                           "required_bytes": int(need * float(profile.disk_safety_factor)), "free_bytes": int(free), "probe": str(probe)}
            if profile.disk_preflight and free < need * float(profile.disk_safety_factor):
                errors.append(f"disk preflight: need about {int(need * float(profile.disk_safety_factor))} bytes, {free} free")
        except Exception as exc:  # pragma: no cover - environment dependent
            report["warnings"].append(f"disk estimate unavailable: {exc}")
    if errors:
        report["status"] = "invalid"
    elif report["missing"]:
        report["status"] = "missing_inputs"
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hydro_pipeline.large_run_driver",
                                     description="Run one real long triangular case: solver -> native sink -> export -> verify.")
    parser.add_argument("config", help=f"{CONFIG_FORMAT} JSON file")
    parser.add_argument("--resume", action="store_true", help="continue a failed run from its newest checkpoint; never rewrites saved frames")
    parser.add_argument("--branch-from", default=None, metavar="CHECKPOINT",
                        help="start a NEW run from a checkpoint of a complete parent run; only boundary discharge series after the "
                             "checkpoint time may differ (verified); parent frames up to the checkpoint are copied")
    parser.add_argument("--check", action="store_true", help="validate config and inputs only (no mesh build, no run); print a summary")
    parser.add_argument("--quiet", action="store_true", help="do not echo progress events to stderr")
    parser.add_argument("--id", "--script-id", dest="script_id", default=None,
                        help="scheme id = run directory name under output.root (overrides output.script_id; required with --resume when the id was generated)")
    args = parser.parse_args(argv)
    log = None if args.quiet else (lambda message: print(message, file=sys.stderr, flush=True))
    if args.check:
        report = check_case(args.config, resume=args.resume, script_id=args.script_id)
        print(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True, default=str))
        return 0 if report["status"] == "ok" else 1
    try:
        report = run_large_case(args.config, resume=args.resume, log=log, script_id=args.script_id, branch_from=args.branch_from)
    except (LargeRunDriverError, LargeRunProfileError, LargeRunExportError, FrameSinkError, TriangularLongRunError,
            TriangularSolverExecutionError, TriangularMeshError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True, default=str))
    return 0 if report["status"] == "complete" else 1


__all__ = [
    "CONFIG_FORMAT", "LargeRunDriverConfig", "LargeRunDriverError", "PreparedCase", "boundary_group_specs", "build_metadata",
    "build_triangular_mesh", "check_case", "check_sections_against_edges", "close_ledger_from_summary", "driver_config_json_schema",
    "edge_table", "finalize_output", "load_driver_config", "main", "newest_checkpoint", "parse_driver_config", "prepare_case", "read_forcing_csv",
    "read_mesh_artifact", "read_sections_audit", "read_terrain", "resolve_config_paths", "run_large_case", "section_boundary_summary",
    "section_boundary_type", "section_distribution", "sink_ledger_from_row",
]


if __name__ == "__main__":
    raise SystemExit(main())
