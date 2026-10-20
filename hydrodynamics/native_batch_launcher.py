"""Project-local launcher for a synthetic native-triangle batch and engineering preflight."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator
import yaml

from .config import Number, Text
from .run_identity import resolve_script_id
from .security_exclusion import SecurityExclusionInput, security_evidence_sha256


class NativeBatchError(ValueError):
    """Raised when the project-local batch request cannot be safely started."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, allow_inf_nan=False)


class NativeBatchLaunchConfig(_StrictModel):
    format: Literal["native-batch-launch-config-v1"]
    schema_version: Literal["1.0"]
    mode: Literal["synthetic_demo"]
    data_origin: Literal["SYNTHETIC_ONLY"]
    case_id: Literal["security-exclusion-synthetic-dynamic-v1"]
    source_case_directory: Text = Field(description="Project-relative profile directory; this launcher currently accepts examples/security_exclusion")
    maximum_face_count: StrictInt = Field(default=64, ge=1, le=64, description="Maximum active triangular faces, count")
    maximum_saved_frame_count: StrictInt = Field(default=8, ge=2, le=8, description="Maximum native saved frames, count")
    maximum_duration_s: Number = Field(default=1.0, gt=0.0, le=1.0, description="Maximum synthetic run duration, seconds")

    @field_validator("source_case_directory")
    @classmethod
    def relative_source_case(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("source_case_directory must be relative to the launch config")
        return value


class NativeEngineeringInitialCondition(_StrictModel):
    source_file: Text | None = None
    quantity: Literal["depth", "level"] | None = None
    unit: Literal["m"] | None = None
    vertical_datum: Text | None = None


class NativeEngineeringRoughness(_StrictModel):
    source_file: Text | None = None
    source_unit: Text | None = None
    physical_unit: Literal["s/m^(1/3)"] | None = None
    interpretation_policy: Literal["legacy_unit1_as_si_manning"] | None = None
    rationale: Text | None = None


class NativeEngineeringRainfall(_StrictModel):
    source_file: Text | None = None
    input_unit: Literal["mm/h", "m/s"] | None = None
    intervals_file: Text | None = None


class NativeEngineeringBoundary(_StrictModel):
    direction: Literal["inflow", "outflow", "inflow_and_outflow"] | None = None
    inflow_file: Text | None = None
    outflow_file: Text | None = None
    group_mapping_file: Text | None = None
    provenance_file: Text | None = None
    time_origin: Text | None = None
    time_unit: Literal["s"] | None = None
    discharge_unit: Literal["m3/s"] | None = None
    vertical_datum: Text | None = None


class NativeEngineeringPreflightConfig(_StrictModel):
    """Twenty explicit native-engineering evidence fields; this model never authorizes execution."""

    format: Literal["native-engineering-preflight-v1"]
    schema_version: Literal["1.0"]
    mode: Literal["engineering_preflight"]
    case_id: Text
    data_origin: Literal["ENGINEERING_PRECHECK"]
    original_domain_geometry_file: Text | None = None
    original_domain_crs: Text | None = None
    mesh_coordinate_system: Text | None = None
    dem_source_file: Text | None = None
    dem_source_crs: Text | None = None
    dem_vertical_unit: Literal["m"] | None = None
    dem_vertical_datum: Text | None = None
    dem_nodata_mask_file: Text | None = None
    dem_coverage_evidence_file: Text | None = None
    initial_condition: NativeEngineeringInitialCondition = Field(default_factory=NativeEngineeringInitialCondition)
    roughness: NativeEngineeringRoughness = Field(default_factory=NativeEngineeringRoughness)
    rainfall: NativeEngineeringRainfall = Field(default_factory=NativeEngineeringRainfall)
    boundary: NativeEngineeringBoundary = Field(default_factory=NativeEngineeringBoundary)
    native_preparation_config_file: Text | None = None
    native_mesh_artifact_file: Text | None = None
    p3_business_artifact_file: Text | None = None
    native_run_config_file: Text | None = None
    native_source_config_file: Text | None = None
    native_capability_validation_file: Text | None = None
    security_exclusion: SecurityExclusionInput


class NativeBatchStageReport(_StrictModel):
    name: Text
    script_id: Text
    data_origin: Literal["SYNTHETIC_ONLY", "ENGINEERING_PRECHECK"]
    status: Literal["complete", "failed", "blocked"]
    command: list[Text]
    started_at: Text
    finished_at: Text
    elapsed_seconds: Number = Field(ge=0.0)
    exit_code: StrictInt
    stdout_file: Text | None = None
    stderr_file: Text | None = None
    error: Text | None = None


class NativeBatchBlocker(_StrictModel):
    code: Text
    field: Text
    message: Text
    next_action: Text


class NativeBatchInputPathCheck(_StrictModel):
    field: Text
    path: Text
    exists: StrictBool
    content_read: Literal[False] = False


class NativeBatchCopiedInput(_StrictModel):
    relative_path: Text
    byte_count: StrictInt = Field(ge=0)
    sha256: Text


class NativeBatchRunReport(_StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=False, validate_default=True, validate_assignment=True, allow_inf_nan=False)

    format: Literal["native-batch-run-report-v1"]
    schema_version: Literal["1.0"]
    mode: Literal["synthetic_demo", "engineering_preflight"]
    data_origin: Literal["SYNTHETIC_ONLY", "ENGINEERING_PRECHECK"]
    engine_mode: Literal["triangular_source_experimental", "native_triangular_engineering_preflight"]
    grid_topology: Literal["native_triangular_mesh"]
    case_id: Text
    script_id: Text
    status: Literal["running", "complete", "failed", "blocked"]
    preflight_only: StrictBool
    execution_available: StrictBool
    started_at: Text
    finished_at: Text | None = None
    elapsed_seconds: Number | None = Field(default=None, ge=0.0)
    project_root: Text
    output_directory: Text
    report_file: Text
    python_executable: Text
    module_source: Text
    package_version: Text
    launch_config_file: Text
    copied_inputs: list[NativeBatchCopiedInput] = Field(default_factory=list)
    input_path_checks: list[NativeBatchInputPathCheck] = Field(default_factory=list)
    stages: list[NativeBatchStageReport] = Field(default_factory=list)
    blockers: list[NativeBatchBlocker] = Field(default_factory=list)
    output_files: list[Text] = Field(default_factory=list)
    failed_stage: Text | None = None
    error: Text | None = None

    @model_validator(mode="after")
    def mode_status_and_stage_identity(self) -> "NativeBatchRunReport":
        expected_origin = "SYNTHETIC_ONLY" if self.mode == "synthetic_demo" else "ENGINEERING_PRECHECK"
        if self.data_origin != expected_origin:
            raise ValueError("mode and data_origin must be consistent")
        if any(stage.script_id != self.script_id or stage.data_origin != self.data_origin for stage in self.stages):
            raise ValueError("every stage must carry the outer script_id and data_origin")
        if self.status == "complete":
            if self.mode != "synthetic_demo" or self.preflight_only or not self.execution_available:
                raise ValueError("only a completed synthetic run may be marked complete")
            if self.finished_at is None or self.failed_stage is not None or self.error is not None:
                raise ValueError("complete report must have a finish time and no failure fields")
            if any(stage.status != "complete" or stage.exit_code != 0 for stage in self.stages):
                raise ValueError("complete report requires every stage to exit successfully")
        if self.status == "blocked" and (not self.preflight_only or self.execution_available):
            raise ValueError("blocked report must remain preflight_only with execution_available=false")
        return self


SYNTHETIC_INPUT_ALLOWLIST = (
    "mesh_preparation.yaml", "mesh.json", "dem.json", "security_area.geojson", "domain.geojson",
    "business_dynamic_preparation.yaml", "run_dynamic.yaml", "combined_source_preparation.yaml",
    "combined_run.yaml", "export_combined_v2.yaml", "export_frame_native_v2.yaml",
)

_ENGINEERING_REQUIRED = (
    ("original_domain_geometry_file", "MISSING_ORIGINAL_DOMAIN_D", "Supply the trusted source geometry for original domain D."),
    ("original_domain_crs", "MISSING_DOMAIN_CRS", "Declare the CRS of original domain D."),
    ("mesh_coordinate_system", "MISSING_NATIVE_MESH_CRS", "Provide the native projected mesh CRS."),
    ("dem_source_file", "MISSING_DEM_SOURCE", "Provide the authoritative DEM source path; the preflight will only check its presence."),
    ("dem_source_crs", "MISSING_DEM_SOURCE_CRS", "Declare the DEM source CRS."),
    ("dem_vertical_unit", "MISSING_DEM_VERTICAL_UNIT", "Declare the DEM vertical unit in metres."),
    ("dem_vertical_datum", "MISSING_DEM_VERTICAL_DATUM", "Declare authoritative DEM vertical-datum provenance."),
    ("dem_nodata_mask_file", "MISSING_DEM_NODATA_MASK", "Provide the source NoData mask or explicit mask contract."),
    ("dem_coverage_evidence_file", "MISSING_DEM_COVERAGE_EVIDENCE", "Provide evidence for active-domain DEM coverage."),
    ("initial_condition.source_file", "MISSING_ACTIVE_INITIAL_CONDITION", "Prepare an active-face initial condition on the exact P2 mesh."),
    ("initial_condition.quantity", "MISSING_INITIAL_CONDITION_QUANTITY", "Declare initial quantity as depth or level."),
    ("initial_condition.unit", "MISSING_INITIAL_CONDITION_UNIT", "Declare the initial field unit in metres."),
    ("initial_condition.vertical_datum", "MISSING_INITIAL_CONDITION_DATUM", "Declare the initial level datum, or explicitly mark depth as datum-independent."),
    ("roughness.source_file", "MISSING_ROUGHNESS_SOURCE", "Provide active-face roughness provenance."),
    ("roughness.source_unit", "MISSING_ROUGHNESS_SOURCE_UNIT", "Declare the source roughness unit."),
    ("roughness.physical_unit", "MISSING_ROUGHNESS_PHYSICAL_UNIT", "Declare Manning physical unit s/m^(1/3)."),
    ("roughness.interpretation_policy", "MISSING_ROUGHNESS_INTERPRETATION", "Use the explicit approved SI interpretation policy."),
    ("roughness.rationale", "MISSING_ROUGHNESS_RATIONALE", "Record why the source values have the declared SI interpretation."),
    ("rainfall.source_file", "MISSING_RAINFALL_SOURCE", "Provide the native rainfall source; this preflight will not read its data rows."),
    ("rainfall.input_unit", "MISSING_RAINFALL_UNIT", "Declare rainfall intensity unit as mm/h or m/s."),
    ("rainfall.intervals_file", "MISSING_RAINFALL_INTERVALS", "Provide explicit rainfall intervals aligned to native saved times."),
    ("boundary.inflow_file", "MISSING_NATIVE_INFLOW", "Provide the native inflow series and group assignment."),
    ("boundary.outflow_file", "MISSING_NATIVE_OUTFLOW", "Provide the native outflow series and group assignment."),
    ("boundary.group_mapping_file", "MISSING_BOUNDARY_GROUP_MAPPING", "Map each source series to a native mesh boundary group."),
    ("boundary.provenance_file", "MISSING_BOUNDARY_PROVENANCE", "Provide provenance for boundary values and directions."),
    ("boundary.time_origin", "MISSING_BOUNDARY_TIME_ORIGIN", "Declare the timezone-aware boundary time origin."),
    ("boundary.time_unit", "MISSING_BOUNDARY_TIME_UNIT", "Declare boundary time unit seconds."),
    ("boundary.direction", "MISSING_BOUNDARY_DIRECTION", "Declare the physical inflow/outflow direction."),
    ("boundary.discharge_unit", "MISSING_BOUNDARY_DISCHARGE_UNIT", "Declare boundary discharge unit m3/s."),
    ("boundary.vertical_datum", "MISSING_BOUNDARY_VERTICAL_DATUM", "Declare boundary level datum when level is supplied."),
    ("native_preparation_config_file", "MISSING_NATIVE_P2_CONFIG", "Provide the native P2 preparation config."),
    ("native_mesh_artifact_file", "MISSING_NATIVE_P2_ARTIFACT", "Prepare and bind the native P2 mesh/terrain artifact."),
    ("p3_business_artifact_file", "MISSING_NATIVE_P3_ARTIFACT", "Prepare active-face P3 initial/roughness fields on this exact mesh."),
    ("native_run_config_file", "MISSING_NATIVE_RUN_CONFIG", "Provide the top-level native source-run config."),
    ("native_source_config_file", "MISSING_NATIVE_SOURCE_CONFIG", "Provide the strict source-preparation config."),
    ("native_capability_validation_file", "MISSING_NATIVE_VALIDATION_EVIDENCE", "Provide independent native capability validation evidence."),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strict_yaml(raw: bytes, label: str) -> Any:
    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in result:
                    raise ValueError(f"duplicate key {key!r}")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    try:
        return yaml.load(raw.decode("utf-8-sig"), Loader=UniqueLoader)
    except (UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        raise NativeBatchError(f"{label} must be UTF-8 strict YAML without duplicate keys: {exc}") from exc


def _reparse_flag() -> int:
    return int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _reject_reparse_components(path: Path) -> None:
    lexical = Path(path).expanduser()
    windows = PureWindowsPath(str(path))
    if windows.drive and not windows.is_absolute():
        raise NativeBatchError(f"drive-relative paths are not allowed: {path}")
    if lexical.is_absolute():
        current = Path(lexical.anchor)
        parts = lexical.parts[1:]
    else:
        current = Path.cwd()
        parts = lexical.parts
    # Inspect each lexical component before applying abspath/resolve. In
    # particular, a path like alias/../fresh must still reject alias.
    for part in parts:
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NativeBatchError(f"cannot inspect output path component {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & _reparse_flag():
            raise NativeBatchError(f"path traverses a symlink or Windows reparse point: {current}")


def _find_project_root() -> Path:
    candidates = (Path(__file__).resolve().parents[1], Path.cwd().resolve())
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file() and (candidate / "examples" / "security_exclusion" / "mesh_preparation.yaml").is_file():
            return candidate
    raise NativeBatchError(
        "project-local synthetic inputs are unavailable; run run_qingbei_nc.bat from the checkout or provide a project-local config"
    )


def _project_python(project_root: Path) -> Path:
    python_path = project_root / ".venv" / "Scripts" / "python.exe"
    if not python_path.is_file():
        raise NativeBatchError(
            f"project virtual environment is missing: {python_path}; create it with python -m venv .venv, "
            "then install the project with .venv\\Scripts\\python.exe -m pip install -e \".[triangular,shp]\""
        )
    expected = os.path.normcase(str(python_path.resolve()))
    actual = os.path.normcase(str(Path(sys.executable).resolve()))
    if expected != actual:
        raise NativeBatchError(
            f"launcher must run with the project virtual environment Python {python_path}; current interpreter is {sys.executable}"
        )
    missing = [name for name in ("pydantic", "yaml", "numpy", "netCDF4", "pyproj", "shapely", "shapefile")
               if importlib.util.find_spec(name) is None]
    if missing:
        raise NativeBatchError(
            f"project virtual environment is missing dependencies {missing}; install with "
            f"{python_path} -m pip install -e \".[triangular,shp]\""
        )
    return python_path


def _load_model(path: Path, model_type, label):
    _reject_reparse_components(path)
    if not path.is_file():
        raise NativeBatchError(f"{label} not found: {path}")
    raw = path.read_bytes()
    data = _strict_yaml(raw, label)
    if not isinstance(data, dict):
        raise NativeBatchError(f"{label} root must be a YAML mapping")
    try:
        return model_type.model_validate(data), raw
    except ValidationError as exc:
        raise NativeBatchError(f"invalid {label}: {exc}") from exc


def _resolve_project_path(project_root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    # Keep the lexical components intact until callers inspect reparse points.
    return candidate if candidate.is_absolute() else project_root / candidate


def _output_directory(project_root: Path, value: str | None, script_id: str) -> Path:
    target_lexical = (
        project_root / "work" / "results" / script_id
        if value is None else _resolve_project_path(project_root, value)
    )
    _reject_reparse_components(target_lexical)
    target = Path(os.path.abspath(target_lexical))
    _reject_reparse_components(target)
    if target.exists() or target.is_symlink():
        raise NativeBatchError(f"output directory already exists; no files were changed: {target}; choose a new --id or --out-dir")
    parent = target.parent
    _reject_reparse_components(parent)
    try:
        parent.mkdir(parents=True, exist_ok=True)
        _reject_reparse_components(parent)
        _reject_reparse_components(target)
        target.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise NativeBatchError(f"output directory appeared during creation; no existing data was replaced: {target}") from exc
    except OSError as exc:
        raise NativeBatchError(f"cannot create output directory {target}: {exc}") from exc
    return target


def _write_json_atomic(path: Path, value: Any) -> None:
    validated = value if isinstance(value, BaseModel) else value
    data = validated.model_dump(mode="json") if isinstance(validated, BaseModel) else value
    raw = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _package_version() -> str:
    try:
        return importlib.metadata.version("two-d-hydrodynamics")
    except importlib.metadata.PackageNotFoundError:
        return "source-checkout"


def _new_report(
    *, project_root: Path, output_dir: Path, script_id: str, mode: Literal["synthetic_demo", "engineering_preflight"],
    case_id: str, data_origin: Literal["SYNTHETIC_ONLY", "ENGINEERING_PRECHECK"], config_path: Path,
) -> NativeBatchRunReport:
    import hydrodynamics

    return NativeBatchRunReport(
        format="native-batch-run-report-v1", schema_version="1.0", mode=mode, data_origin=data_origin,
        engine_mode="triangular_source_experimental" if mode == "synthetic_demo" else "native_triangular_engineering_preflight",
        grid_topology="native_triangular_mesh", case_id=case_id, script_id=script_id,
        status="running", preflight_only=(mode == "engineering_preflight"),
        execution_available=(mode == "synthetic_demo"), started_at=_now(),
        project_root=str(project_root), output_directory=str(output_dir), report_file="run_report.json",
        python_executable=str(Path(sys.executable).resolve()), module_source=str(Path(hydrodynamics.__file__).resolve()),
        package_version=_package_version(), launch_config_file=str(config_path.resolve()),
    )


def _save_report(report: NativeBatchRunReport, output_dir: Path) -> None:
    validated = NativeBatchRunReport.model_validate(report.model_dump(mode="python"))
    _write_json_atomic(output_dir / "run_report.json", validated)


def _append_internal_stage(
    report: NativeBatchRunReport, output_dir: Path, name: str, command: Sequence[str],
    status: Literal["complete", "failed", "blocked"], exit_code: int,
    started_at: str, started_clock: float, *, error: str | None = None,
) -> None:
    report.stages.append(NativeBatchStageReport(
        name=name, script_id=report.script_id, data_origin=report.data_origin,
        status=status, command=[str(value) for value in command],
        started_at=started_at, finished_at=_now(), elapsed_seconds=max(0.0, time.perf_counter() - started_clock),
        exit_code=exit_code, error=error,
    ))
    _save_report(report, output_dir)


def _run_cli_stage(
    *, project_root: Path, output_dir: Path, case_dir: Path, python_path: Path,
    report: NativeBatchRunReport, name: str, module: str, args: Sequence[str],
) -> bool:
    command = [str(python_path), "-X", "utf8", "-m", module, *args]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    env["PYTHONIOENCODING"] = "utf-8"
    started_at, started_clock = _now(), time.perf_counter()
    try:
        completed = subprocess.run(
            command, cwd=case_dir, env=env, check=False, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        log_dir = output_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        stdout_path = log_dir / f"{name}.stdout.txt"
        stderr_path = log_dir / f"{name}.stderr.txt"
        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")
        stage_status = "complete" if completed.returncode == 0 else "failed"
        stage_error = None if completed.returncode == 0 else (completed.stderr or completed.stdout or f"exit {completed.returncode}")[-2000:]
        report.stages.append(NativeBatchStageReport(
            name=name, script_id=report.script_id, data_origin=report.data_origin,
            status=stage_status, command=command, started_at=started_at, finished_at=_now(),
            elapsed_seconds=max(0.0, time.perf_counter() - started_clock), exit_code=int(completed.returncode),
            stdout_file=str(stdout_path.relative_to(output_dir)), stderr_file=str(stderr_path.relative_to(output_dir)),
            error=stage_error,
        ))
        for log_path in (stdout_path, stderr_path):
            relative_log = str(log_path.relative_to(output_dir)).replace("\\", "/")
            if relative_log not in report.output_files:
                report.output_files.append(relative_log)
        if completed.returncode != 0:
            report.status = "failed"
            report.failed_stage = name
            report.error = stage_error
            report.finished_at = _now()
            report.elapsed_seconds = _elapsed_since(report.started_at)
            _save_report(report, output_dir)
            return False
        _save_report(report, output_dir)
        return True
    except OSError as exc:
        error = f"could not launch stage {name}: {exc}"
        report.stages.append(NativeBatchStageReport(
            name=name, script_id=report.script_id, data_origin=report.data_origin,
            status="failed", command=command, started_at=started_at, finished_at=_now(),
            elapsed_seconds=max(0.0, time.perf_counter() - started_clock), exit_code=127, error=error,
        ))
        report.status, report.failed_stage, report.error, report.finished_at = "failed", name, error, _now()
        _save_report(report, output_dir)
        return False


def _elapsed_since(timestamp: str) -> float:
    started = datetime.fromisoformat(timestamp)
    return max(0.0, (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds())


def _copy_synthetic_inputs(
    *, project_root: Path, config_path: Path, config: NativeBatchLaunchConfig,
    output_dir: Path,
) -> tuple[Path, list[NativeBatchCopiedInput]]:
    source_case = (config_path.parent / config.source_case_directory).resolve()
    expected_case = (project_root / "examples" / "security_exclusion").resolve()
    if source_case != expected_case:
        raise NativeBatchError("synthetic source_case_directory must resolve to the approved examples/security_exclusion profile")
    case_dir = output_dir / "inputs" / "security_exclusion"
    case_dir.mkdir(parents=True, exist_ok=False)
    copied: list[NativeBatchCopiedInput] = []
    for name in SYNTHETIC_INPUT_ALLOWLIST:
        source_path = source_case / name
        _reject_reparse_components(source_path)
        if not source_path.is_file():
            raise NativeBatchError(f"approved synthetic input is missing: {source_path}")
        destination = case_dir / name
        raw = source_path.read_bytes()
        destination.write_bytes(raw)
        copied.append(NativeBatchCopiedInput(
            relative_path=str(destination.relative_to(output_dir)).replace("\\", "/"),
            byte_count=len(raw), sha256=__import__("hashlib").sha256(raw).hexdigest(),
        ))
    return case_dir, copied


def _finish_failure(report: NativeBatchRunReport, output_dir: Path, *, stage: str, error: str, exit_code: int = 1) -> int:
    if not any(item.name == stage for item in report.stages):
        now = _now()
        report.stages.append(NativeBatchStageReport(
            name=stage, script_id=report.script_id, data_origin=report.data_origin,
            status="failed", command=["native-batch-launcher", stage],
            started_at=now, finished_at=now, elapsed_seconds=0.0, exit_code=exit_code, error=error,
        ))
    report.status = "failed"
    report.failed_stage = stage
    report.error = error
    report.finished_at = _now()
    report.elapsed_seconds = _elapsed_since(report.started_at)
    _save_report(report, output_dir)
    return exit_code


def _summary_and_preview(
    *, project_root: Path, case_dir: Path, output_dir: Path, report: NativeBatchRunReport,
    config: NativeBatchLaunchConfig,
) -> dict[str, Any]:
    from .triangular_source_netcdf import read_triangular_source_result

    result_path = case_dir / "combined.nc"
    result = read_triangular_source_result(result_path, case_dir / "combined_run.yaml", enforce_configured_result_path=True)
    face_areas = tuple(float(value) for value in result.face_area_m2)
    frame_volumes = [math.fsum(float(depth) * area for depth, area in zip(row, face_areas)) for row in result.water_depth_m]
    if len(frame_volumes) != len(result.time_s) or len(frame_volumes) > config.maximum_saved_frame_count:
        raise NativeBatchError("native result frame count is outside the synthetic launcher contract")
    if len(result.face_id) > config.maximum_face_count or float(result.time_s[-1]) > config.maximum_duration_s:
        raise NativeBatchError("native result exceeds the synthetic launcher face/time limits")
    ledger_residuals = [float(row.residual_m3) for row in result.mass_budget]
    if any(not math.isclose(actual, float(row.final_volume_m3), rel_tol=1e-12, abs_tol=1e-12)
           for actual, row in zip(frame_volumes, result.mass_budget)):
        raise NativeBatchError("independently integrated native frame volumes differ from the native mass ledger")
    evidence = result.security_exclusion
    if evidence is None:
        raise NativeBatchError("the SYNTHETIC_ONLY security example must carry its active-domain evidence")
    rainfall_accounting = result.security_rainfall_accounting
    if rainfall_accounting is None:
        raise NativeBatchError("the combined security example must carry separate excluded-area rainfall accounting")
    max_residual = max((abs(value) for value in ledger_residuals), default=0.0)
    summary = {
        "format": "native-batch-synthetic-summary-v1", "mode": "synthetic_demo",
        "data_origin": "SYNTHETIC_ONLY", "case_id": result.case_id, "script_id": report.script_id,
        "script_id_scope": "outer batch identity in output directory/report/summary/stages; native combined.nc keeps its existing case_id and source graph",
        "engine_mode": "triangular_source_experimental", "grid_topology": "native_triangular_mesh",
        "module_source": report.module_source, "module_version": report.package_version,
        "result_file": str(result_path.relative_to(output_dir)).replace("\\", "/"),
        "result_kind": "manning_and_rainfall",
        "sources_applied": result.sources_applied,
        "manning_enabled": result.manning_enabled, "manning_applied": result.manning_applied,
        "nonzero_manning_effect_applied": result.nonzero_manning_effect_applied,
        "rainfall_enabled": result.rainfall_enabled, "rainfall_applied": result.rainfall_applied,
        "rainfall_nonzero_effect_applied": result.rainfall_nonzero_effect_applied,
        "face_count": len(result.face_id), "face_id": [str(value) for value in result.face_id],
        "face_area_m2": list(face_areas), "time_s": [float(value) for value in result.time_s],
        "original_domain_area_m2": evidence.original_domain_area_m2,
        "security_excluded_area_m2": evidence.security_inside_area_m2,
        "active_domain_area_m2": evidence.active_domain_area_m2,
        "active_mesh_area_m2": evidence.active_mesh_area_m2,
        "security_exclusion_sha256": security_evidence_sha256(evidence),
        "initial_volume_m3": frame_volumes[0], "final_volume_m3": frame_volumes[-1],
        "frame_integrated_volumes_m3": frame_volumes,
        "mass_budget": [item.model_dump(mode="json") for item in result.mass_budget],
        "max_abs_mass_residual_m3": max_residual,
        "rainfall_rate_m_s_by_interval": [float(value) for value in result.rainfall_rate_m_s_by_interval],
        "rainfall_accepted_active_m3_by_interval": list(rainfall_accounting.accepted_active_rainfall_volume_m3_by_interval),
        "rainfall_accepted_active_total_m3": rainfall_accounting.accepted_active_rainfall_volume_m3,
        "rainfall_excluded_unmodeled_m3_by_interval": list(rainfall_accounting.security_excluded_unmodeled_rainfall_volume_m3_by_interval),
        "rainfall_excluded_unmodeled_total_m3": rainfall_accounting.security_excluded_unmodeled_rainfall_volume_m3,
        "excluded_rain_is_accepted_budget_loss": rainfall_accounting.excluded_rain_is_accepted_budget_loss,
        "automatic_flow_transfer_from_excluded_area": rainfall_accounting.automatic_flow_transfer_from_excluded_area,
        "native_solver_version": result.solver_version,
        "base_none_result_file": "base_none_dynamic.nc",
        "base_none_result_generated_or_read": False,
        "exports": [],
        "limitations": ["SYNTHETIC_ONLY four-face example; not a real engineering prediction.",
                        "Closed-wall boundary only; no open-boundary execution.",
                        "Risk, population and loss statistics are unsupported and not inferred as zero."],
    }
    for request_name, output_name in (
        ("export_combined_v2.yaml", "combined_native_shp_v2"),
        ("export_frame_native_v2.yaml", "combined_frame_native"),
    ):
        manifest_path = case_dir / output_name / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        summary["exports"].append({
            "request": request_name, "directory": str((case_dir / output_name).relative_to(output_dir)).replace("\\", "/"),
            "result_set_id": manifest["result_set_id"], "target_crs": manifest["target_crs"],
            "selector": manifest["selector"], "scope_selection": manifest["scope_selection"],
            "feature_count": manifest["feature_count"], "files": [item["key"] for item in manifest["files"]],
            "validation": "passed",
        })
    _write_json_atomic(output_dir / "summary.json", summary)
    _write_preview_svg(result, case_dir / "security_area.geojson", output_dir / "preview.svg")
    readme = _readme_text(output_dir, report, summary)
    (output_dir / "README.md").write_text(readme, encoding="utf-8")
    return summary


def _write_preview_svg(result: Any, security_geojson_path: Path, target: Path) -> None:
    import html
    import xml.etree.ElementTree as ET
    from shapely import wkb
    from shapely.geometry import Polygon, MultiPolygon

    node_xy = tuple((float(point[0]), float(point[1])) for point in result.node_xy_m)
    faces = tuple(tuple(int(node) for node in face) for face in result.face_nodes)
    face_ids = tuple(int(value) for value in result.face_id)
    if len(faces) != len(face_ids) or any(len(face) != 3 for face in faces):
        raise NativeBatchError("native preview requires exactly three native node indices per active face")
    evidence = result.security_exclusion
    if evidence is None:
        raise NativeBatchError("native preview requires the validated security evidence from the source result")
    safe_geometry = wkb.loads(bytes.fromhex(evidence.security_union_wkb_hex))
    polygons = [safe_geometry] if safe_geometry.geom_type == "Polygon" else list(safe_geometry.geoms)
    xs = [point[0] for point in node_xy]
    ys = [point[1] for point in node_xy]
    for polygon in polygons:
        xs.extend(float(x) for x, _ in polygon.exterior.coords)
        ys.extend(float(y) for _, y in polygon.exterior.coords)
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    width, height, margin = 1040, 660, 120
    plot_width, plot_height = width - 2 * margin, height - 2 * margin
    scale = min(plot_width / max(max_x - min_x, 1e-12), plot_height / max(max_y - min_y, 1e-12))
    used_width, used_height = (max_x - min_x) * scale, (max_y - min_y) * scale
    x_offset, y_offset = (width - used_width) / 2.0, (height - used_height) / 2.0

    def screen(point: tuple[float, float]) -> tuple[float, float]:
        return x_offset + (point[0] - min_x) * scale, height - y_offset - (point[1] - min_y) * scale

    def poly_points(points) -> str:
        return " ".join(f"{screen((float(x), float(y)))[0]:.2f},{screen((float(x), float(y)))[1]:.2f}" for x, y in points)

    fragments = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f4f7fb"/>',
        '<text x="520" y="45" text-anchor="middle" font-family="Arial,sans-serif" font-size="24" font-weight="bold" fill="#1b354a">SYNTHETIC_ONLY · Manning + rainfall · t = 0.1 s</text>',
        f'<text x="520" y="76" text-anchor="middle" font-family="Arial,sans-serif" font-size="15" fill="#425b72">{len(faces)} native active faces · {evidence.active_mesh_area_m2:g} m² active · {evidence.security_inside_area_m2:g} m² excluded</text>',
    ]
    for polygon in polygons:
        fragments.append(f'<polygon points="{poly_points(polygon.exterior.coords)}" fill="#f5b8b8" stroke="#b32636" stroke-width="3"/>')
    depths = [float(value) for value in result.water_depth_m[-1]]
    max_depth = max(depths, default=0.0)
    for face_index, (face, face_id, depth) in enumerate(zip(faces, face_ids, depths)):
        points = [node_xy[node] for node in face]
        alpha = 0.30 if max_depth <= 0.0 else 0.30 + 0.55 * depth / max_depth
        fragments.append(f'<polygon points="{poly_points(points)}" fill="#6ba5dc" fill-opacity="{alpha:.3f}" stroke="#164a73" stroke-width="2"/>')
        cx = math.fsum(point[0] for point in points) / 3.0
        cy = math.fsum(point[1] for point in points) / 3.0
        sx, sy = screen((cx, cy))
        fragments.append(f'<text x="{sx:.2f}" y="{sy:.2f}" text-anchor="middle" dominant-baseline="middle" font-family="Arial,sans-serif" font-size="17" font-weight="bold" fill="#10344f">{face_id}</text>')
    label_point = screen(((min_x + max_x) / 2.0, (min_y + max_y) / 2.0))
    fragments.append(f'<text x="{label_point[0]:.2f}" y="{label_point[1]:.2f}" text-anchor="middle" transform="rotate(-90 {label_point[0]:.2f} {label_point[1]:.2f})" font-family="Arial,sans-serif" font-size="14" font-weight="bold" fill="#8f1e2b">EXCLUDED · CLOSED WALL</text>')
    fragments.extend([
        '<rect x="270" y="590" width="18" height="18" fill="#f5b8b8" stroke="#b32636"/>',
        '<text x="296" y="605" font-family="Arial,sans-serif" font-size="14" fill="#334e64">Excluded safety area</text>',
        '<rect x="520" y="590" width="18" height="18" fill="#6ba5dc" stroke="#164a73"/>',
        '<text x="546" y="605" font-family="Arial,sans-serif" font-size="14" fill="#334e64">Active native faces; shade uses final native depth</text>',
        '<text x="520" y="640" text-anchor="middle" font-family="Arial,sans-serif" font-size="12" fill="#52677a">Geometry and depths are read from the strict native source result; no interpolation or remeshing.</text>',
        '</svg>',
    ])
    target.write_text("\n".join(fragments) + "\n", encoding="utf-8")


def _readme_text(output_dir: Path, report: NativeBatchRunReport, summary: dict[str, Any]) -> str:
    return "\n".join([
        "# Native batch synthetic run",
        "",
        f"- Mode/data origin: `{report.mode}` / `{report.data_origin}`.",
        f"- Case/script ID: `{report.case_id}` / `{report.script_id}`.",
        f"- Native result: `{summary['result_file']}` (`{summary['native_solver_version']}`).",
        f"- Faces/times: {summary['face_count']} / {summary['time_s']} s; active/excluded area {summary['active_mesh_area_m2']:g}/{summary['security_excluded_area_m2']:g} m².",
        f"- Initial/final synchronous volume: {summary['initial_volume_m3']:.12g}/{summary['final_volume_m3']:.12g} m³; max absolute mass residual {summary['max_abs_mass_residual_m3']:.3g} m³.",
        f"- Rainfall accepted over active area: {summary['rainfall_accepted_active_total_m3']:.12g} m³; excluded-area unmodeled rain: {summary['rainfall_excluded_unmodeled_total_m3']:.12g} m³.",
        f"- Source flags: Manning applied={summary['manning_applied']}, nonzero effect={summary['nonzero_manning_effect_applied']}; rainfall applied={summary['rainfall_applied']}, nonzero effect={summary['rainfall_nonzero_effect_applied']}.",
        f"- Module source: `{report.module_source}`; Python: `{report.python_executable}`.",
        "- No base-none dynamic result was run, read, or required.",
        "",
        "## Reproduce",
        "",
        "From this run directory, set the project wheel install and execute the same stages:",
        "",
        "```powershell",
        f"$env:PYTHONPATH = '{Path(report.project_root) / 'work' / 'acceptance_native_source_shp_20261020' / 'installed'}'",
        f"$py = '{report.python_executable}'",
        "& $py -X utf8 -m hydrodynamics.mesh_preparation inputs/security_exclusion/mesh_preparation.yaml",
        "```",
        "",
        "The exact stage commands, exit codes, stdout/stderr logs, source identities, and validated package IDs are in `run_report.json` and `summary.json`.",
        "",
        "## Limits",
        "",
        "This is a four-face, 0.1 s synthetic closed-wall source demonstration. It is not an engineering prediction; open boundaries and risk/population/loss outputs are unsupported.",
        "",
        "![Native active faces and excluded safety area](preview.svg)",
        "",
    ])


def _run_synthetic_batch(
    project_root: Path, python_path: Path, config_path: Path, config: NativeBatchLaunchConfig,
    output_dir: Path, script_id: str,
) -> tuple[NativeBatchRunReport, int]:
    report = _new_report(
        project_root=project_root, output_dir=output_dir, script_id=script_id,
        mode="synthetic_demo", case_id=config.case_id, data_origin="SYNTHETIC_ONLY", config_path=config_path,
    )
    report.output_files = ["run_report.json"]
    _save_report(report, output_dir)
    started, clock = _now(), time.perf_counter()
    try:
        case_dir, copied = _copy_synthetic_inputs(
            project_root=project_root, config_path=config_path, config=config, output_dir=output_dir,
        )
        report.copied_inputs = copied
        _append_internal_stage(report, output_dir, "copy_synthetic_allowlist", ["copy", *[item.relative_path for item in copied]], "complete", 0, started, clock)
        stages = (
            ("p2_mesh_preparation", "hydrodynamics.mesh_preparation", ("mesh_preparation.yaml",)),
            ("p3_dynamic_business_preparation", "hydrodynamics.triangular_business", ("business_dynamic_preparation.yaml",)),
            ("combined_source_preparation", "hydrodynamics.triangular_sources", ("combined_source_preparation.yaml",)),
            ("combined_source_run", "hydrodynamics.triangular_source", ("combined_run.yaml",)),
            ("combined_max_epsg4490_export", "hydrodynamics.triangular_shp_export", ("export", "--request", "export_combined_v2.yaml")),
            ("combined_max_epsg4490_validate", "hydrodynamics.triangular_shp_export", ("validate", "--request", "export_combined_v2.yaml")),
            ("combined_frame_native_export", "hydrodynamics.triangular_shp_export", ("export", "--request", "export_frame_native_v2.yaml")),
            ("combined_frame_native_validate", "hydrodynamics.triangular_shp_export", ("validate", "--request", "export_frame_native_v2.yaml")),
        )
        for name, module, args in stages:
            if not _run_cli_stage(
                project_root=project_root, output_dir=output_dir, case_dir=case_dir, python_path=python_path,
                report=report, name=name, module=module, args=args,
            ):
                return report, 1
        base_none = case_dir / "base_none_dynamic.nc"
        if base_none.exists():
            return report, _finish_failure(
                report, output_dir, stage="source_only_contract",
                error="base_none_dynamic.nc was unexpectedly generated; source-only launcher must not run the base-none solver",
            )

        finalize_started, finalize_clock = _now(), time.perf_counter()
        try:
            summary = _summary_and_preview(
                project_root=project_root, case_dir=case_dir, output_dir=output_dir, report=report, config=config,
            )
            (output_dir / "README.md").write_text(_readme_text(output_dir, report, summary), encoding="utf-8")
            report.output_files = [
                "run_report.json", "summary.json", "README.md", "preview.svg",
                "inputs/security_exclusion/prepared_mesh.nc",
                "inputs/security_exclusion/business_dynamic.json",
                "inputs/security_exclusion/combined_sources.json",
                "inputs/security_exclusion/combined.nc",
                "inputs/security_exclusion/combined_native_shp_v2/manifest.json",
                "inputs/security_exclusion/combined_frame_native/manifest.json",
            ]
            _append_internal_stage(report, output_dir, "summary_readme_preview", ["finalize native summary and SVG from validated result"], "complete", 0, finalize_started, finalize_clock)
        except Exception as exc:
            return report, _finish_failure(report, output_dir, stage="summary_readme_preview", error=str(exc))
        report.preflight_only = False
        report.execution_available = True
        report.finished_at = _now()
        report.elapsed_seconds = max(0.0, time.perf_counter() - clock)
        report.status = "complete"
        _save_report(report, output_dir)
        return report, 0
    except Exception as exc:
        return report, _finish_failure(report, output_dir, stage="input_preflight_or_copy", error=str(exc))


def _engineering_value(config: NativeEngineeringPreflightConfig, field_path: str) -> Any:
    value: Any = config
    for part in field_path.split("."):
        value = getattr(value, part)
    return value


def _engineering_input_paths(config: NativeEngineeringPreflightConfig) -> list[tuple[str, str | None]]:
    paths: list[tuple[str, str | None]] = [
        (name, getattr(config, name)) for name in (
            "original_domain_geometry_file", "dem_source_file", "dem_nodata_mask_file", "dem_coverage_evidence_file",
            "native_preparation_config_file", "native_mesh_artifact_file", "p3_business_artifact_file",
            "native_run_config_file", "native_source_config_file", "native_capability_validation_file",
        )
    ]
    for nested, field_names in (
        ("initial_condition", ("source_file",)),
        ("roughness", ("source_file",)),
        ("rainfall", ("source_file", "intervals_file")),
        ("boundary", ("inflow_file", "outflow_file", "group_mapping_file", "provenance_file")),
    ):
        model = getattr(config, nested)
        paths.extend((f"{nested}.{name}", getattr(model, name)) for name in field_names)
    if config.security_exclusion.domain_geometry_file is not None:
        paths.append(("security_exclusion.domain_geometry_file", config.security_exclusion.domain_geometry_file))
    return paths


def _engineering_preflight(
    project_root: Path, python_path: Path, config_path: Path, config: NativeEngineeringPreflightConfig,
    output_dir: Path, script_id: str,
) -> tuple[NativeBatchRunReport, int]:
    report = _new_report(
        project_root=project_root, output_dir=output_dir, script_id=script_id,
        mode="engineering_preflight", case_id=config.case_id, data_origin="ENGINEERING_PRECHECK", config_path=config_path,
    )
    report.preflight_only = True
    report.execution_available = False
    report.output_files = ["run_report.json", "inputs/native_engineering_preflight.yaml"]
    _save_report(report, output_dir)
    stage_started, stage_clock = _now(), time.perf_counter()
    blockers: list[NativeBatchBlocker] = []
    path_checks: list[NativeBatchInputPathCheck] = []
    missing_specs = (
        ("original_domain_geometry_file", "MISSING_ORIGINAL_DOMAIN_D", "Supply trusted polygon geometry for original D."),
        ("original_domain_crs", "MISSING_DOMAIN_CRS", "Declare the original-domain CRS."),
        ("mesh_coordinate_system", "MISSING_NATIVE_MESH_CRS", "Declare the projected native mesh CRS."),
        ("dem_source_file", "MISSING_DEM_SOURCE", "Provide the authoritative DEM path; its pixels will not be opened."),
        ("dem_source_crs", "MISSING_DEM_SOURCE_CRS", "Declare the DEM source CRS."),
        ("dem_vertical_unit", "MISSING_DEM_VERTICAL_UNIT", "Declare DEM vertical unit in metres."),
        ("dem_vertical_datum", "MISSING_DEM_VERTICAL_DATUM", "Provide authoritative DEM vertical-datum provenance."),
        ("dem_nodata_mask_file", "MISSING_DEM_MASK", "Provide the source NoData/active mask contract."),
        ("dem_coverage_evidence_file", "MISSING_DEM_COVERAGE", "Provide coverage evidence for the native active domain."),
        ("initial_condition.source_file", "MISSING_INITIAL_CONDITION", "Prepare an initial field on the exact native active faces."),
        ("initial_condition.quantity", "MISSING_INITIAL_QUANTITY", "Declare initial depth or level semantics."),
        ("initial_condition.unit", "MISSING_INITIAL_UNIT", "Declare the initial field unit in metres."),
        ("initial_condition.vertical_datum", "MISSING_INITIAL_DATUM", "Declare the initial water-level datum or its depth-only semantics."),
        ("roughness.source_file", "MISSING_ROUGHNESS_SOURCE", "Provide active-face roughness provenance."),
        ("roughness.source_unit", "MISSING_ROUGHNESS_SOURCE_UNIT", "Declare the original roughness unit."),
        ("roughness.physical_unit", "MISSING_ROUGHNESS_PHYSICAL_UNIT", "Declare Manning physical unit s/m^(1/3)."),
        ("roughness.interpretation_policy", "MISSING_ROUGHNESS_SI_POLICY", "Declare the explicit SI Manning interpretation policy."),
        ("roughness.rationale", "MISSING_ROUGHNESS_RATIONALE", "Provide rationale for the source-to-SI roughness interpretation."),
        ("rainfall.source_file", "MISSING_RAINFALL_SOURCE", "Provide the native rainfall source path."),
        ("rainfall.input_unit", "MISSING_RAINFALL_UNIT", "Declare rainfall unit mm/h or m/s."),
        ("rainfall.intervals_file", "MISSING_RAINFALL_INTERVALS", "Provide explicit native rainfall intervals."),
        ("boundary.inflow_file", "MISSING_NATIVE_INFLOW", "Provide native inflow boundary data."),
        ("boundary.outflow_file", "MISSING_NATIVE_OUTFLOW", "Provide native outflow boundary data."),
        ("boundary.group_mapping_file", "MISSING_BOUNDARY_GROUPS", "Map each boundary series to native mesh groups."),
        ("boundary.provenance_file", "MISSING_BOUNDARY_PROVENANCE", "Provide boundary-source provenance."),
        ("boundary.time_origin", "MISSING_BOUNDARY_TIME_ORIGIN", "Provide timezone-aware boundary time origin."),
        ("boundary.time_unit", "MISSING_BOUNDARY_TIME_UNIT", "Declare boundary time unit seconds."),
        ("boundary.direction", "MISSING_BOUNDARY_DIRECTION", "Declare boundary inflow/outflow direction."),
        ("boundary.discharge_unit", "MISSING_BOUNDARY_DISCHARGE_UNIT", "Declare boundary discharge unit m3/s."),
        ("boundary.vertical_datum", "MISSING_BOUNDARY_VERTICAL_DATUM", "Declare boundary level datum where applicable."),
        ("native_preparation_config_file", "MISSING_NATIVE_PREPARATION_CONFIG", "Provide the native P2 preparation config."),
        ("native_mesh_artifact_file", "MISSING_NATIVE_MESH_ARTIFACT", "Provide a native P2 mesh/terrain artifact."),
        ("p3_business_artifact_file", "MISSING_P3_BUSINESS_ARTIFACT", "Provide P3 initial/roughness fields on the exact P2 face set."),
        ("native_run_config_file", "MISSING_NATIVE_RUN_CONFIG", "Provide the top-level native run config."),
        ("native_source_config_file", "MISSING_NATIVE_SOURCE_CONFIG", "Provide the strict native source config."),
        ("native_capability_validation_file", "MISSING_NATIVE_VALIDATION_EVIDENCE", "Provide native capability validation evidence."),
    )
    for field_path, code, next_action in missing_specs:
        if _engineering_value(config, field_path) is None:
            blockers.append(NativeBatchBlocker(
                code=code, field=field_path, message=f"{field_path} is explicitly unprovided in the engineering preflight template.",
                next_action=next_action,
            ))
    if config.boundary.direction in {"inflow", "outflow", "inflow_and_outflow"}:
        blockers.append(NativeBatchBlocker(
            code="UNSUPPORTED_OPEN_BOUNDARY_EXECUTION", field="boundary.direction",
            message="P4 native source execution currently supports closed_wall only; case_001 requires inflow/outflow boundary execution.",
            next_action="Obtain a separate approved native open-boundary design and implementation before any engineering run.",
        ))
    blockers.append(NativeBatchBlocker(
        code="ENGINEERING_EXECUTION_NOT_IN_SCOPE", field="mode",
        message="This launcher only performs engineering preflight and will not execute an engineering solver, even when template fields are filled.",
        next_action="Use the synthetic demo for this launcher; request a separate engineering execution design before running real inputs.",
    ))

    for field_path, declared in _engineering_input_paths(config):
        if declared is None:
            continue
        candidate = Path(declared).expanduser()
        source_lexical = candidate if candidate.is_absolute() else config_path.parent / candidate
        try:
            _reject_reparse_components(source_lexical)
            source_path = Path(os.path.abspath(source_lexical))
            exists = source_path.is_file()
        except NativeBatchError:
            source_path = Path(os.path.abspath(source_lexical))
            exists = False
        path_checks.append(NativeBatchInputPathCheck(field=field_path, path=str(source_path), exists=exists, content_read=False))
        if not exists:
            blockers.append(NativeBatchBlocker(
                code="MISSING_INPUT_FILE", field=field_path, message=f"Declared input path is not an existing regular file: {source_path}",
                next_action="Supply an authorized source file; this preflight checks presence only and does not open raster/vector/data records.",
            ))
        # This preflight intentionally stops at file existence. No DEM/NPZ/CSV/DBF
        # payload or raster/vector geometry is opened here.

    security_source_lexical = config_path.parent / config.security_exclusion.source_file
    try:
        _reject_reparse_components(security_source_lexical)
        security_source = Path(os.path.abspath(security_source_lexical))
        security_exists = security_source.is_file()
    except NativeBatchError:
        security_source = Path(os.path.abspath(security_source_lexical))
        security_exists = False
    path_checks.append(NativeBatchInputPathCheck(
        field="security_exclusion.source_file", path=str(security_source), exists=security_exists, content_read=False,
    ))
    if not config.security_exclusion.enabled:
        blockers.append(NativeBatchBlocker(
            code="SECURITY_EXCLUSION_DISABLED", field="security_exclusion.enabled",
            message="The engineering template must keep the protected-area exclusion enabled.",
            next_action="Retain enabled: true and the declared CRS/source before a later approved engineering design.",
        ))
    if not security_exists:
        blockers.append(NativeBatchBlocker(
            code="MISSING_SECURITY_GEOMETRY", field="security_exclusion.source_file",
            message=f"Declared security geometry file is not present: {security_source}",
            next_action="Restore the authorized geometry source; no geometry or DBF is read by this preflight.",
        ))

    template_copy = output_dir / "inputs" / "native_engineering_preflight.yaml"
    template_copy.parent.mkdir(exist_ok=False)
    shutil.copyfile(config_path, template_copy)
    report.input_path_checks = path_checks
    report.blockers = blockers
    report.output_files = ["run_report.json", "inputs/native_engineering_preflight.yaml"]
    _append_internal_stage(
        report, output_dir, "engineering_preflight",
        ["native_batch_launcher", "--engineering", "presence-only native input checks"],
        "blocked", 3, stage_started, stage_clock,
        error="engineering mode is preflight_only; native open-boundary execution is unsupported",
    )
    report.status = "blocked"
    report.preflight_only = True
    report.execution_available = False
    report.failed_stage = "engineering_preflight"
    report.error = "UNSUPPORTED_OPEN_BOUNDARY_EXECUTION; engineering execution is not in this batch"
    report.finished_at = _now()
    report.elapsed_seconds = max(0.0, time.perf_counter() - stage_clock)
    _save_report(report, output_dir)
    return report, 3


def launcher_schema() -> dict[str, Any]:
    return {
        "format": "native-batch-launcher-schema-set-v1",
        "synthetic_launch_config_schema": NativeBatchLaunchConfig.model_json_schema(),
        "engineering_preflight_config_schema": NativeEngineeringPreflightConfig.model_json_schema(),
        "run_report_schema": NativeBatchRunReport.model_json_schema(),
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hydrodynamics-native-batch",
        description="Run the approved SYNTHETIC_ONLY native triangle example or an engineering preflight-only report.",
    )
    parser.add_argument("--id", dest="run_id", help="explicit 1-96 character run ID")
    parser.add_argument("--script-id", dest="script_id", help="alias for --id; explicit value is preserved")
    parser.add_argument("--out-dir", help="new output directory; relative paths are anchored at the project root")
    parser.add_argument("--config", help="strict synthetic launcher YAML, relative to project root")
    parser.add_argument("--engineering", action="store_true", help="run native engineering preflight only; never starts a solver")
    parser.add_argument("--engineering-config", help="engineering preflight template YAML, relative to project root")
    parser.add_argument("--schema", action="store_true", help="print strict launch, engineering preflight, and run-report schemas")
    return parser.parse_args(argv)


def _load_profile_config(project_root: Path, path: str | None) -> tuple[Path, NativeBatchLaunchConfig]:
    config_path = _resolve_project_path(project_root, path or "examples/native_batch/synthetic_demo.yaml")
    config, _ = _load_model(config_path, NativeBatchLaunchConfig, "synthetic launcher config")
    source_case_lexical = config_path.parent / config.source_case_directory
    _reject_reparse_components(source_case_lexical)
    source_case = source_case_lexical.resolve()
    expected_case = (project_root / "examples" / "security_exclusion").resolve()
    if source_case != expected_case:
        raise NativeBatchError("synthetic launcher config may use only the approved examples/security_exclusion profile")
    if config.case_id != "security-exclusion-synthetic-dynamic-v1" or config.data_origin != "SYNTHETIC_ONLY":
        raise NativeBatchError("synthetic launcher must retain the approved case_id and data_origin: SYNTHETIC_ONLY")
    return config_path, config


def _load_engineering_config(project_root: Path, path: str | None) -> tuple[Path, NativeEngineeringPreflightConfig]:
    config_path = _resolve_project_path(
        project_root, path or "examples/case_001/native_engineering_preflight.yaml",
    )
    config, _ = _load_model(config_path, NativeEngineeringPreflightConfig, "native engineering preflight config")
    if config.data_origin != "ENGINEERING_PRECHECK" or not config.security_exclusion.enabled:
        raise NativeBatchError("engineering template must declare ENGINEERING_PRECHECK and keep security exclusion enabled")
    return config_path, config


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        if args.schema:
            print(json.dumps(launcher_schema(), ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.run_id and args.script_id and args.run_id != args.script_id:
            raise NativeBatchError("--id and --script-id may both be supplied only when their values match")
        explicit_id = args.run_id if args.run_id is not None else args.script_id
        script_id = resolve_script_id(explicit_id)
        project_root = _find_project_root()
        python_path = _project_python(project_root)
        if args.engineering:
            if args.config is not None:
                raise NativeBatchError("--config is for the synthetic profile; use --engineering-config with --engineering")
            config_path, config = _load_engineering_config(project_root, args.engineering_config)
            output_dir = _output_directory(project_root, args.out_dir, script_id)
            report, exit_code = _engineering_preflight(
                project_root, python_path, config_path, config, output_dir, script_id,
            )
        else:
            if args.engineering_config is not None:
                raise NativeBatchError("--engineering-config requires --engineering")
            config_path, config = _load_profile_config(project_root, args.config)
            output_dir = _output_directory(project_root, args.out_dir, script_id)
            report, exit_code = _run_synthetic_batch(
                project_root, python_path, config_path, config, output_dir, script_id,
            )
        print(json.dumps({
            "status": report.status, "mode": report.mode, "data_origin": report.data_origin,
            "case_id": report.case_id, "script_id": report.script_id,
            "output_directory": report.output_directory, "run_report": str(output_dir / "run_report.json"),
            "failed_stage": report.failed_stage, "error": report.error,
            "completed_stages": sum(stage.status == "complete" for stage in report.stages),
            "total_stages": len(report.stages),
        }, ensure_ascii=False, indent=2, allow_nan=False))
        return exit_code
    except (NativeBatchError, ValueError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


__all__ = [
    "NativeBatchError", "NativeBatchLaunchConfig", "NativeEngineeringPreflightConfig",
    "NativeBatchRunReport", "SYNTHETIC_INPUT_ALLOWLIST", "launcher_schema", "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
