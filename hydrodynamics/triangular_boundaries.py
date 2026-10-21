"""P4-3A open-boundary request preparation; this module never evaluates fluxes.

The adapter binds P3-A group requests to actual P2 exterior edges, records
geometry-derived outward normals, and freezes explicit request allocations.
Its output is not a boundary state, numerical flux, solver input, or result.
"""

from __future__ import annotations

import argparse
import ctypes
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import stat
import tempfile
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator
import yaml

from .config import Number, Text
from .triangular_business import (
    BoundaryQuantity,
    TriangularBoundaryBinding,
    TriangularBusinessError,
    TriangularBusinessPackage,
    read_triangular_business_package,
    terrain_fingerprint,
    topology_fingerprint,
)
from .triangular_mesh import TriangularMeshError
from .triangular_solver_contract import _step_tolerance_s


class TriangularBoundaryPreparationError(ValueError):
    """Raised when a boundary preparation request or artifact is invalid."""


class _BoundaryStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, allow_inf_nan=False)


class BoundaryGroupRequest(_BoundaryStrictModel):
    """Explicit adapter choices for one existing P3-A group binding."""

    group_name: Text
    direction: Literal["inflow", "outflow"] | None = None
    allocation_policy: Literal["uniform_per_unit_length"] | None = None
    temporal_policy: Literal["piecewise_constant"] | None = None


class TriangularBoundaryPreparationConfig(_BoundaryStrictModel):
    """Versioned preparation config independent of every solver run config."""

    format: Literal["triangular-boundary-preparation-v1"]
    schema_version: Literal["1.0"]
    case_id: Text
    business_file: Text
    output_file: Text
    output_times_s: list[Number] = Field(min_length=2, max_length=100_000)
    time_origin: Text | None
    unbound_exterior_policy: Literal["closed_wall"]
    boundary_bindings: list[BoundaryGroupRequest] = Field(default_factory=list, max_length=100_000)
    overwrite_existing_output: StrictBool = False

    @field_validator("business_file", "output_file")
    @classmethod
    def relative_paths(cls, value: str) -> str:
        if not value.strip() or Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("path must be a non-empty path relative to the boundary-preparation config")
        return value

    @field_validator("time_origin")
    @classmethod
    def timezone_aware_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("time_origin must be an ISO8601 timestamp with timezone") from exc
        if parsed.utcoffset() is None:
            raise ValueError("time_origin must include a timezone")
        return value

    @model_validator(mode="after")
    def valid_axis_and_paths(self) -> "TriangularBoundaryPreparationConfig":
        if self.business_file == self.output_file:
            raise ValueError("output_file must be distinct from business_file")
        if self.output_times_s[0] != 0.0:
            raise ValueError("output_times_s must start at 0 s")
        if any(value < 0.0 or value > 31_536_000.0 for value in self.output_times_s):
            raise ValueError("output_times_s must be within [0, 31,536,000] s")
        if any(right <= left for left, right in zip(self.output_times_s, self.output_times_s[1:])):
            raise ValueError("output_times_s must be strictly increasing and unique")
        names = [item.group_name for item in self.boundary_bindings]
        if len(set(names)) != len(names):
            raise ValueError("boundary_bindings group_name values must be unique")
        return self


class PreparedBoundaryEdge(_BoundaryStrictModel):
    edge_id: StrictInt = Field(ge=0)
    node_ids: list[StrictInt] = Field(min_length=2, max_length=2)
    length_m: Number = Field(gt=0.0)
    outward_normal_x: Number
    outward_normal_y: Number
    length_weight: Number = Field(gt=0.0, le=1.0)


class PreparedBoundaryEdgeRequest(_BoundaryStrictModel):
    edge_id: StrictInt = Field(ge=0)
    requested_edge_discharge_m3_s: Number
    requested_normal_discharge_m2_s: Number


class PreparedBoundaryTerminalSample(_BoundaryStrictModel):
    raw_time_s: Number
    effective_time_s: Number
    output_index: StrictInt = Field(ge=0)
    value: Number
    unit: Literal["m3/s", "m"]


class PreparedBoundarySegment(_BoundaryStrictModel):
    raw_start_time_s: Number
    raw_end_time_s: Number
    effective_start_time_s: Number
    effective_end_time_s: Number
    start_output_index: StrictInt = Field(ge=0)
    end_output_index: StrictInt = Field(ge=1)
    requested_group_discharge_m3_s: Number | None = None
    requested_absolute_water_level_m: Number | None = None
    edge_requests: list[PreparedBoundaryEdgeRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def positive_interval_and_single_quantity(self) -> "PreparedBoundarySegment":
        if self.raw_end_time_s <= self.raw_start_time_s or self.effective_end_time_s <= self.effective_start_time_s:
            raise ValueError("prepared boundary segment must span positive time")
        if (self.requested_group_discharge_m3_s is None) == (self.requested_absolute_water_level_m is None):
            raise ValueError("prepared segment must contain exactly one requested boundary quantity")
        if self.requested_group_discharge_m3_s is None and self.edge_requests:
            raise ValueError("water-level segments must not claim discharge allocations")
        return self


class PreparedBoundaryGroup(_BoundaryStrictModel):
    group_name: Text
    quantity: Literal["discharge", "water_level"]
    mode: Literal["constant", "timeseries"]
    unit: Literal["m3/s", "m"]
    interpretation: Literal["total_group_discharge", "absolute_water_level"]
    source: Text
    vertical_datum: Text | None = None
    direction: Literal["inflow", "outflow"] | None = None
    allocation_policy: Literal["uniform_per_unit_length"] | None = None
    temporal_policy: Literal["piecewise_constant"] | None = None
    edges: list[PreparedBoundaryEdge] = Field(min_length=1)
    segments: list[PreparedBoundarySegment] = Field(min_length=1)
    terminal_sample: PreparedBoundaryTerminalSample
    requested_inflow_volume_m3: Number = Field(ge=0.0)
    requested_outflow_volume_m3: Number = Field(ge=0.0)
    requested_net_inflow_volume_m3: Number

    @model_validator(mode="after")
    def binding_semantics_are_explicit(self) -> "PreparedBoundaryGroup":
        if self.quantity == "discharge":
            if self.unit != "m3/s" or self.interpretation != "total_group_discharge":
                raise ValueError("discharge preparation must retain non-negative total group m3/s semantics")
            if self.direction is None or self.allocation_policy != "uniform_per_unit_length":
                raise ValueError("discharge preparation requires explicit direction and uniform_per_unit_length allocation")
            if self.vertical_datum is not None or self.requested_inflow_volume_m3 * self.requested_outflow_volume_m3 != 0.0:
                raise ValueError("discharge preparation cannot carry a datum or simultaneous in/out request volumes")
            if self.temporal_policy != ("piecewise_constant" if self.mode == "timeseries" else None):
                raise ValueError("discharge temporal_policy must be piecewise_constant only for a time series")
            if any(segment.requested_group_discharge_m3_s is None for segment in self.segments):
                raise ValueError("discharge segments must retain total group request values")
            if any(len(segment.edge_requests) != len(self.edges) for segment in self.segments):
                raise ValueError("each discharge segment must allocate every actual group edge")
            total_length = math.fsum(edge.length_m for edge in self.edges)
            if not math.isclose(math.fsum(edge.length_weight for edge in self.edges), 1.0, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("discharge edge length_weight values must sum to one")
            expected_sign = -1.0 if self.direction == "inflow" else 1.0
            derived_inflow: list[float] = []
            derived_outflow: list[float] = []
            for segment in self.segments:
                request_q = float(segment.requested_group_discharge_m3_s)
                requests_by_edge = {item.edge_id: item for item in segment.edge_requests}
                if len(requests_by_edge) != len(segment.edge_requests) or set(requests_by_edge) != {edge.edge_id for edge in self.edges}:
                    raise ValueError("each segment edge request must match one unique actual group edge")
                signed_q = expected_sign * request_q
                if not math.isclose(math.fsum(item.requested_edge_discharge_m3_s for item in segment.edge_requests), signed_q,
                                    rel_tol=1e-12, abs_tol=1e-12 * max(1.0, abs(request_q))):
                    raise ValueError("edge request discharges must sum to the signed group request")
                for edge in self.edges:
                    item = requests_by_edge[edge.edge_id]
                    if not math.isclose(item.requested_edge_discharge_m3_s, signed_q * edge.length_weight,
                                        rel_tol=1e-12, abs_tol=1e-12 * max(1.0, abs(request_q))):
                        raise ValueError("edge request discharge must use its actual length weight")
                    if not math.isclose(item.requested_normal_discharge_m2_s, signed_q / total_length,
                                        rel_tol=1e-12, abs_tol=1e-12 * max(1.0, abs(request_q / total_length))):
                        raise ValueError("unit-width request must equal signed group discharge divided by group length")
                duration = math.fsum((segment.effective_end_time_s, -segment.effective_start_time_s))
                (derived_inflow if self.direction == "inflow" else derived_outflow).append(request_q * duration)
            if not math.isclose(self.requested_inflow_volume_m3, math.fsum(derived_inflow), rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("requested_inflow_volume_m3 must match the declared inflow intervals")
            if not math.isclose(self.requested_outflow_volume_m3, math.fsum(derived_outflow), rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("requested_outflow_volume_m3 must match the declared outflow intervals")
            if not math.isclose(self.requested_net_inflow_volume_m3,
                                self.requested_inflow_volume_m3 - self.requested_outflow_volume_m3,
                                rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("requested_net_inflow_volume_m3 must equal inflow minus outflow requests")
        else:
            if self.unit != "m" or self.interpretation != "absolute_water_level" or not self.vertical_datum:
                raise ValueError("water-level preparation must retain absolute metres and a known datum")
            if self.direction is not None or self.allocation_policy is not None:
                raise ValueError("water-level preparation must not invent discharge direction or allocation")
            if self.temporal_policy != ("piecewise_constant" if self.mode == "timeseries" else None):
                raise ValueError("water-level temporal_policy must be piecewise_constant only for a time series")
            if self.requested_inflow_volume_m3 != 0.0 or self.requested_outflow_volume_m3 != 0.0 or self.requested_net_inflow_volume_m3 != 0.0:
                raise ValueError("water-level requests do not establish a boundary flux or exchange volume")
            if any(segment.requested_absolute_water_level_m is None for segment in self.segments):
                raise ValueError("water-level segments must retain absolute stage request values")
        for edge in self.edges:
            if not math.isclose(math.hypot(edge.outward_normal_x, edge.outward_normal_y), 1.0, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"boundary edge {edge.edge_id} outward normal must be a unit vector")
        if len({edge.edge_id for edge in self.edges}) != len(self.edges):
            raise ValueError("prepared boundary edge IDs must be unique within a group")
        for segment in self.segments:
            if segment.end_output_index <= segment.start_output_index:
                raise ValueError("prepared boundary segments must span at least one output interval")
        return self


class PreparedUnboundExteriorEdge(_BoundaryStrictModel):
    edge_id: StrictInt = Field(ge=0)
    node_ids: list[StrictInt] = Field(min_length=2, max_length=2)


class TriangularBoundaryPreparationResult(_BoundaryStrictModel):
    format: Literal["triangular-boundary-preparation-result-v1"]
    product_type: Literal["triangular_boundary_preparation"]
    preparation_only: Literal[True]
    simulation_result: Literal[False]
    solver_completed: Literal[False]
    production_approved: Literal[False]
    boundary_applied: Literal[False]
    status: Literal["prepared_not_executable"]
    execution_available: Literal[False]
    case_id: Text
    time_origin: Text | None
    output_times_s: list[Number] = Field(min_length=2)
    coordinate_system: Text
    mesh_id: Text
    topology_fingerprint: Text
    terrain_fingerprint: Text
    preparation_config_sha256: Text
    business_package_sha256: Text
    mesh_sha256: Text
    preparation_config_artifact: Text
    business_package_artifact: Text
    mesh_artifact: Text
    unbound_exterior_policy: Literal["closed_wall"]
    exterior_edge_ids: list[StrictInt]
    boundary_groups: list[PreparedBoundaryGroup]
    unbound_exterior_edges: list[PreparedUnboundExteriorEdge]

    @field_validator(
        "preparation_only", "simulation_result", "solver_completed", "production_approved",
        "boundary_applied", "execution_available", mode="before",
    )
    @classmethod
    def fixed_markers_require_json_booleans(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("fixed preparation/result markers must be JSON booleans, not integers or other scalar types")
        return value

    @model_validator(mode="after")
    def prep_flags_and_edge_identity(self) -> "TriangularBoundaryPreparationResult":
        if self.output_times_s[0] != 0.0 or any(right <= left for left, right in zip(self.output_times_s, self.output_times_s[1:])):
            raise ValueError("prepared boundary output_times_s must start at 0 and increase strictly")
        if len(set(self.exterior_edge_ids)) != len(self.exterior_edge_ids):
            raise ValueError("exterior_edge_ids must contain unique P2 edge indices")
        group_names = [group.group_name for group in self.boundary_groups]
        if len(set(group_names)) != len(group_names):
            raise ValueError("prepared boundary group names must be unique")
        assigned = [edge.edge_id for group in self.boundary_groups for edge in group.edges]
        unbound = [edge.edge_id for edge in self.unbound_exterior_edges]
        if len(assigned) != len(set(assigned)) or set(assigned).intersection(unbound):
            raise ValueError("prepared boundary assignments and closed-wall edges may not overlap")
        if set(assigned).union(unbound) != set(self.exterior_edge_ids):
            raise ValueError("prepared group and unbound edge IDs must partition actual exterior_edge_ids")
        for group in self.boundary_groups:
            for segment in group.segments:
                if segment.end_output_index >= len(self.output_times_s):
                    raise ValueError("prepared boundary output index is outside output_times_s")
                start = float(self.output_times_s[segment.start_output_index])
                end = float(self.output_times_s[segment.end_output_index])
                if abs(segment.effective_start_time_s - start) > _step_tolerance_s(max(abs(segment.effective_start_time_s), abs(start))):
                    raise ValueError("prepared boundary segment start no longer matches its output index")
                if abs(segment.effective_end_time_s - end) > _step_tolerance_s(max(abs(segment.effective_end_time_s), abs(end))):
                    raise ValueError("prepared boundary segment end no longer matches its output index")
            if group.terminal_sample.output_index != len(self.output_times_s) - 1 or \
                    abs(group.terminal_sample.effective_time_s - self.output_times_s[-1]) > _step_tolerance_s(max(abs(group.terminal_sample.effective_time_s), abs(self.output_times_s[-1]))):
                raise ValueError("prepared boundary terminal sample must match the final output time")
        for field in ("preparation_config_sha256", "business_package_sha256", "mesh_sha256", "topology_fingerprint", "terrain_fingerprint"):
            digest = getattr(self, field)
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"{field} must be a lowercase SHA-256 digest")
        return self


class _BoundaryCapability:
    def __init__(self, config_path: Path, business_path: Path, mesh_path: Path, output_path: Path,
                 config: TriangularBoundaryPreparationConfig, business: TriangularBusinessPackage,
                 existing_output_identity: tuple[int, int, int, int, int] | None = None,
                 existing_output_sha256: str | None = None):
        self.config_path = config_path
        self.business_path = business_path
        self.mesh_path = mesh_path
        self.output_path = output_path
        self.config = config
        self.business = business
        self.existing_output_identity = existing_output_identity
        self.existing_output_sha256 = existing_output_sha256


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode)
            or int(getattr(info, "st_file_attributes", 0)) & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))):
        raise OSError(f"boundary-preparation output is no longer a regular unaliased file: {path}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _validated_existing_output_snapshot(
    path: Path, expected_case_id: str,
) -> tuple[tuple[int, int, int, int, int], str]:
    before = _file_identity(path)
    raw = path.read_bytes()
    after = _file_identity(path)
    if before != after or len(raw) != before[2]:
        raise TriangularBoundaryPreparationError(
            f"existing boundary-preparation output changed while it was being validated: {path}"
        )
    try:
        existing = TriangularBoundaryPreparationResult.model_validate(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise TriangularBoundaryPreparationError(
            "explicit overwrite is limited to an existing valid boundary-preparation artifact"
        ) from exc
    if existing.format != "triangular-boundary-preparation-result-v1":
        raise TriangularBoundaryPreparationError(
            "explicit overwrite is limited to an existing valid boundary-preparation artifact"
        )
    if existing.case_id != expected_case_id:
        raise TriangularBoundaryPreparationError(
            "explicit overwrite cannot replace a boundary-preparation artifact for a different case_id"
        )
    return after, hashlib.sha256(raw).hexdigest()


def _output_snapshot(path: Path) -> tuple[tuple[int, int, int, int, int], str]:
    before = _file_identity(path)
    digest = _sha256(path)
    after = _file_identity(path)
    if before != after:
        raise TriangularBoundaryPreparationError(
            f"boundary-preparation output changed while publication ownership was checked: {path}"
        )
    return after, digest


@contextmanager
def _boundary_output_mutex(path: Path):
    """Serialize boundary-preparation publishers that use this module."""
    if os.name == "nt":
        normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
        digest = hashlib.sha256(normalized.encode("utf-8", "surrogatepass")).hexdigest()
        mutex_name = f"Local\\HydrodynamicsBoundaryPreparation-{digest}"
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        create_mutex.restype = ctypes.c_void_p
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        wait_for_single_object.restype = ctypes.c_uint32
        release_mutex = kernel32.ReleaseMutex
        release_mutex.argtypes = (ctypes.c_void_p,)
        release_mutex.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int

        handle = create_mutex(None, 0, mutex_name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = False
        try:
            result = wait_for_single_object(handle, 30_000)
            if result in {0, 0x80}:  # WAIT_OBJECT_0 or WAIT_ABANDONED
                acquired = True
            elif result == 0x102:  # WAIT_TIMEOUT
                raise TimeoutError(f"timed out waiting for boundary output lock: {path}")
            else:
                raise ctypes.WinError(ctypes.get_last_error())
            yield
        finally:
            release_error = None
            if acquired and not release_mutex(handle):
                release_error = ctypes.WinError(ctypes.get_last_error())
            close_handle(handle)
            if release_error is not None:
                raise release_error
        return

    # POSIX directory locks require no persistent sidecar file. They serialize
    # publishers using this module; external uncooperative writers are outside
    # the overwrite contract and are still checked by the file snapshot.
    import fcntl
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _publish_without_replacing(temporary: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(temporary, destination)
        return
    os.link(temporary, destination)
    temporary.unlink()


def _relative(base: Path, value: str, label: str) -> Path:
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
        raise TriangularBoundaryPreparationError(f"{label} must be relative to the boundary-preparation config")
    return (base / value).resolve()


def load_triangular_boundary_preparation_config(path: str | Path) -> TriangularBoundaryPreparationConfig:
    source = Path(path).resolve()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TriangularBoundaryPreparationError(f"boundary-preparation config not found: {source}") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise TriangularBoundaryPreparationError(f"invalid boundary-preparation YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TriangularBoundaryPreparationError("boundary-preparation config root must be a YAML mapping")
    try:
        return TriangularBoundaryPreparationConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}" for issue in exc.errors())
        raise TriangularBoundaryPreparationError(f"invalid triangular boundary-preparation config: {details}") from exc


def _linked_mesh_path(business_path: Path) -> Path:
    try:
        raw = json.loads(business_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TriangularBoundaryPreparationError(f"cannot inspect linked P3-A package: {exc}") from exc
    mesh_ref = raw.get("mesh_artifact") if isinstance(raw, dict) else None
    if not isinstance(mesh_ref, str) or not mesh_ref.strip() or Path(mesh_ref).is_absolute() or PureWindowsPath(mesh_ref).is_absolute() or PureWindowsPath(mesh_ref).drive:
        raise TriangularBoundaryPreparationError("P3-A mesh_artifact must be a non-empty relative path")
    return (business_path.parent / mesh_ref).resolve()


def _load_capability(path: str | Path, *, allow_existing_output: bool = False) -> _BoundaryCapability:
    config_path = Path(path).resolve()
    config = load_triangular_boundary_preparation_config(config_path)
    business_path = _relative(config_path.parent, config.business_file, "business_file")
    lexical_output = config_path.parent / config.output_file
    if lexical_output.is_symlink():
        raise TriangularBoundaryPreparationError("output_file must not be a symbolic link")
    output_path = _relative(config_path.parent, config.output_file, "output_file")
    if output_path.parent != config_path.parent and not output_path.parent.is_dir():
        raise TriangularBoundaryPreparationError(f"output directory does not exist: {output_path.parent}")
    try:
        business = read_triangular_business_package(business_path)
    except (TriangularBusinessError, TriangularMeshError) as exc:
        raise TriangularBoundaryPreparationError(f"linked P3-A/P2 input is invalid: {exc}") from exc
    mesh_path = _linked_mesh_path(business_path)
    protected = {config_path, business_path, mesh_path}
    if output_path in protected:
        raise TriangularBoundaryPreparationError("output_file must not overwrite the boundary config, P3-A package or linked P2 mesh")
    existing_output_identity = None
    existing_output_sha256 = None
    if output_path.exists() and not output_path.is_file():
        raise TriangularBoundaryPreparationError("output_file must identify a regular file")
    if output_path.exists():
        try:
            if any(item.exists() and os.path.samefile(output_path, item) for item in protected):
                raise TriangularBoundaryPreparationError("output_file must not be a hard link to a protected input")
        except OSError as exc:
            raise TriangularBoundaryPreparationError(f"cannot safely inspect existing output identity: {exc}") from exc
        if not config.overwrite_existing_output and not allow_existing_output:
            raise TriangularBoundaryPreparationError(f"boundary-preparation output already exists: {output_path}")
        if config.overwrite_existing_output and not allow_existing_output:
            try:
                existing_output_identity, existing_output_sha256 = _validated_existing_output_snapshot(
                    output_path, config.case_id,
                )
            except TriangularBoundaryPreparationError:
                raise
            except OSError as exc:
                raise TriangularBoundaryPreparationError(
                    f"cannot safely validate prior boundary-preparation output identity: {exc}"
                ) from exc
    return _BoundaryCapability(
        config_path, business_path, mesh_path, output_path, config, business,
        existing_output_identity, existing_output_sha256,
    )


def _matching_output_index(time_s: float, output_times_s: Sequence[float], label: str) -> int:
    matches = [index for index, output_time in enumerate(output_times_s)
               if abs(float(time_s) - float(output_time)) <= _step_tolerance_s(max(abs(float(time_s)), abs(float(output_time))))]
    if len(matches) != 1:
        problem = "does not match" if not matches else "ambiguously matches"
        raise TriangularBoundaryPreparationError(f"{label}={time_s:.17g} s {problem} exactly one output time within the shared tolerance")
    return matches[0]


def _edge_geometry(mesh, edge_id: int, *, total_length: float) -> PreparedBoundaryEdge:
    if not 0 <= edge_id < mesh.n_edge:
        raise TriangularBoundaryPreparationError(f"boundary edge index {edge_id} is outside the P2 mesh")
    adjacent = [index for index in mesh.edge_faces[edge_id] if index >= 0]
    if len(adjacent) != 1:
        raise TriangularBoundaryPreparationError(f"boundary edge {edge_id} is not an exterior edge")
    first, second = mesh.edge_nodes[edge_id]
    ax, ay = mesh.node_xy[first]
    bx, by = mesh.node_xy[second]
    length = float(mesh.edge_length[edge_id])
    if not math.isfinite(length) or length <= 0.0:
        raise TriangularBoundaryPreparationError(f"boundary edge {edge_id} length must be finite and positive")
    # Pick one of the two edge-normal directions using the actual adjacent
    # triangle centroid. This does not rely on stored endpoint orientation.
    candidate = ((by - ay) / length, -(bx - ax) / length)
    midpoint = ((ax + bx) * 0.5, (ay + by) * 0.5)
    centroid = mesh.face_centroid[adjacent[0]]
    outward = (midpoint[0] - centroid[0], midpoint[1] - centroid[1])
    dot = candidate[0] * outward[0] + candidate[1] * outward[1]
    if not math.isfinite(dot) or abs(dot) <= 0.0:
        raise TriangularBoundaryPreparationError(f"cannot derive a unique outward normal for boundary edge {edge_id}")
    nx, ny = candidate if dot > 0.0 else (-candidate[0], -candidate[1])
    if not all(math.isfinite(value) for value in (nx, ny)) or abs(math.hypot(nx, ny) - 1.0) > 1e-12:
        raise TriangularBoundaryPreparationError(f"boundary edge {edge_id} has an invalid unit outward normal")
    return PreparedBoundaryEdge(
        edge_id=edge_id, node_ids=[int(first), int(second)], length_m=length,
        outward_normal_x=nx, outward_normal_y=ny, length_weight=length / total_length,
    )


def _validate_config_bindings(config: TriangularBoundaryPreparationConfig,
                               business: TriangularBusinessPackage) -> dict[str, BoundaryGroupRequest]:
    protected_security_edges = set(business.security_exclusion_boundary_edge_ids)
    for binding in business.boundary_bindings:
        overlap = sorted(set(binding.edge_ids).intersection(protected_security_edges))
        if overlap:
            raise TriangularBoundaryPreparationError(
                f"P3-A boundary group '{binding.group_name}' includes security-exclusion closed-wall P2 edge IDs {overlap}"
            )
    actual = {binding.group_name: binding for binding in business.boundary_bindings}
    requested = {item.group_name: item for item in config.boundary_bindings}
    unknown = sorted(set(requested) - set(actual))
    missing = sorted(set(actual) - set(requested))
    if unknown:
        group = unknown[0]
        if group in business.mesh.constraint_groups:
            raise TriangularBoundaryPreparationError(f"'{group}' is an internal constraint group and cannot receive boundary forcing")
        raise TriangularBoundaryPreparationError(f"unknown exterior boundary group '{group}'")
    if missing:
        raise TriangularBoundaryPreparationError(f"P3-A boundary group '{missing[0]}' requires an explicit boundary-preparation mapping")
    for name, binding in actual.items():
        choice = requested[name]
        if binding.quantity is BoundaryQuantity.DISCHARGE:
            if choice.direction not in {"inflow", "outflow"}:
                raise TriangularBoundaryPreparationError(f"discharge group '{name}' requires explicit direction: inflow or outflow")
            if choice.allocation_policy != "uniform_per_unit_length":
                raise TriangularBoundaryPreparationError(f"discharge group '{name}' requires allocation_policy: uniform_per_unit_length")
            expected_temporal = "piecewise_constant" if binding.mode == "timeseries" else None
            if choice.temporal_policy != expected_temporal:
                raise TriangularBoundaryPreparationError(f"group '{name}' temporal_policy must be {expected_temporal!r} for mode {binding.mode}")
        else:
            if choice.direction is not None or choice.allocation_policy is not None:
                raise TriangularBoundaryPreparationError(f"water_level group '{name}' must not specify discharge direction/allocation")
            expected_temporal = "piecewise_constant" if binding.mode == "timeseries" else None
            if choice.temporal_policy != expected_temporal:
                raise TriangularBoundaryPreparationError(f"group '{name}' temporal_policy must be {expected_temporal!r} for mode {binding.mode}")
            if business.terrain.vertical_datum is None or binding.vertical_datum != business.terrain.vertical_datum:
                raise TriangularBoundaryPreparationError(f"water_level group '{name}' datum must exactly match the known P2 terrain vertical_datum")
    return requested


def _binding_segments(binding: TriangularBoundaryBinding, output_times: Sequence[float],
                      temporal_policy: str | None, edge_geometry: Sequence[PreparedBoundaryEdge],
                      direction: str | None) -> tuple[list[PreparedBoundarySegment], PreparedBoundaryTerminalSample, float, float, float]:
    total_time = float(output_times[-1])
    if binding.mode == "constant":
        raw_times = (0.0, total_time)
        effective_times = (float(output_times[0]), float(output_times[-1]))
        indices = (0, len(output_times) - 1)
        values = (float(binding.values[0]),)
        terminal_raw = total_time
        terminal_value = float(binding.values[0])
        segments_src = [(raw_times[0], raw_times[1], effective_times[0], effective_times[1], indices[0], indices[1], values[0])]
    else:
        if temporal_policy != "piecewise_constant":
            raise TriangularBoundaryPreparationError(f"time-series group '{binding.group_name}' requires piecewise_constant temporal_policy")
        assert binding.time_s is not None
        raw_times = tuple(float(value) for value in binding.time_s)
        indices = tuple(_matching_output_index(value, output_times, f"{binding.group_name}.time_s[{i}]") for i, value in enumerate(raw_times))
        if indices[0] != 0 or indices[-1] != len(output_times) - 1:
            raise TriangularBoundaryPreparationError(f"time-series group '{binding.group_name}' must explicitly cover [0, {total_time:g}] s without extrapolation")
        if any(right <= left for left, right in zip(indices, indices[1:])):
            raise TriangularBoundaryPreparationError(f"time-series group '{binding.group_name}' breakpoints must match strictly increasing output times")
        effective_times = tuple(float(output_times[index]) for index in indices)
        segments_src = [
            (raw_times[i], raw_times[i + 1], effective_times[i], effective_times[i + 1], indices[i], indices[i + 1], float(binding.values[i]))
            for i in range(len(raw_times) - 1)
        ]
        terminal_raw = raw_times[-1]
        terminal_value = float(binding.values[-1])
    sign = -1.0 if direction == "inflow" else 1.0
    segments: list[PreparedBoundarySegment] = []
    inflow = outflow = 0.0
    for raw_start, raw_end, effective_start, effective_end, start_index, end_index, value in segments_src:
        if binding.quantity is BoundaryQuantity.DISCHARGE:
            group_q = value
            duration = math.fsum((effective_end, -effective_start))
            signed_outward = sign * group_q
            group_length = math.fsum(float(item.length_m) for item in edge_geometry)
            requested_normal = signed_outward / group_length
            if not math.isfinite(requested_normal):
                raise TriangularBoundaryPreparationError(f"group '{binding.group_name}' produces a non-finite unit-width request")
            edge_requests = []
            for edge in edge_geometry:
                edge_q = signed_outward * float(edge.length_weight)
                if not math.isfinite(edge_q):
                    raise TriangularBoundaryPreparationError(f"group '{binding.group_name}' produces a non-finite edge request")
                edge_requests.append(PreparedBoundaryEdgeRequest(
                    edge_id=edge.edge_id,
                    requested_edge_discharge_m3_s=edge_q,
                    requested_normal_discharge_m2_s=requested_normal,
                ))
            if direction == "inflow":
                inflow = math.fsum((inflow, group_q * duration))
            else:
                outflow = math.fsum((outflow, group_q * duration))
            segments.append(PreparedBoundarySegment(
                raw_start_time_s=raw_start, raw_end_time_s=raw_end,
                effective_start_time_s=effective_start, effective_end_time_s=effective_end,
                start_output_index=start_index, end_output_index=end_index,
                requested_group_discharge_m3_s=group_q, edge_requests=edge_requests,
            ))
        else:
            segments.append(PreparedBoundarySegment(
                raw_start_time_s=raw_start, raw_end_time_s=raw_end,
                effective_start_time_s=effective_start, effective_end_time_s=effective_end,
                start_output_index=start_index, end_output_index=end_index,
                requested_absolute_water_level_m=value,
            ))
    terminal_idx = len(output_times) - 1
    terminal_effective = float(output_times[terminal_idx])
    terminal = PreparedBoundaryTerminalSample(
        raw_time_s=terminal_raw, effective_time_s=terminal_effective, output_index=terminal_idx,
        value=terminal_value, unit="m3/s" if binding.quantity is BoundaryQuantity.DISCHARGE else "m",
    )
    net_inflow = math.fsum((inflow, -outflow))
    if not all(math.isfinite(value) for value in (inflow, outflow, net_inflow)):
        raise TriangularBoundaryPreparationError(f"group '{binding.group_name}' request volume exceeds finite numeric range")
    return segments, terminal, inflow, outflow, net_inflow


def _build_result(capability: _BoundaryCapability) -> TriangularBoundaryPreparationResult:
    config, business = capability.config, capability.business
    mesh, terrain = business.mesh, business.terrain
    choices = _validate_config_bindings(config, business)
    exterior_ids = [edge_id for edge_id, (_, right) in enumerate(mesh.edge_faces) if right == -1]
    assigned: set[int] = set()
    groups: list[PreparedBoundaryGroup] = []
    for binding in business.boundary_bindings:
        choice = choices[binding.group_name]
        ids = tuple(int(edge_id) for edge_id in binding.edge_ids)
        if not ids or len(set(ids)) != len(ids):
            raise TriangularBoundaryPreparationError(f"boundary group '{binding.group_name}' must contain unique exterior edges")
        overlap = assigned.intersection(ids)
        if overlap:
            raise TriangularBoundaryPreparationError(f"exterior mesh edge IDs are bound more than once: {sorted(overlap)}")
        for edge_id in ids:
            if edge_id not in exterior_ids or mesh.edge_faces[edge_id][1] != -1:
                raise TriangularBoundaryPreparationError(f"boundary group '{binding.group_name}' contains non-exterior P2 edge {edge_id}")
        assigned.update(ids)
        lengths = [float(mesh.edge_length[edge_id]) for edge_id in ids]
        try:
            total_length = math.fsum(lengths)
        except OverflowError as exc:
            raise TriangularBoundaryPreparationError(f"boundary group '{binding.group_name}' total edge length exceeds finite geometry limits") from exc
        if not math.isfinite(total_length) or total_length <= 0.0:
            raise TriangularBoundaryPreparationError(f"boundary group '{binding.group_name}' total edge length must be finite and positive")
        edges = [_edge_geometry(mesh, edge_id, total_length=total_length) for edge_id in ids]
        if binding.quantity is BoundaryQuantity.WATER_LEVEL:
            if terrain.vertical_datum is None or binding.vertical_datum != terrain.vertical_datum:
                raise TriangularBoundaryPreparationError(f"water_level group '{binding.group_name}' datum must match actual known terrain datum")
        segments, terminal, inflow, outflow, net_inflow = _binding_segments(
            binding, config.output_times_s, choice.temporal_policy, edges, choice.direction,
        )
        groups.append(PreparedBoundaryGroup(
            group_name=binding.group_name, quantity=binding.quantity.value, mode=binding.mode,
            unit=binding.unit, interpretation=binding.interpretation, source=binding.source,
            vertical_datum=binding.vertical_datum, direction=choice.direction,
            allocation_policy=choice.allocation_policy, temporal_policy=choice.temporal_policy,
            edges=edges, segments=segments, terminal_sample=terminal,
            requested_inflow_volume_m3=inflow, requested_outflow_volume_m3=outflow,
            requested_net_inflow_volume_m3=net_inflow,
        ))
    unbound_ids = sorted(set(exterior_ids) - assigned)
    refs = {
        "preparation_config_artifact": Path(os.path.relpath(capability.config_path, capability.output_path.parent)).as_posix(),
        "business_package_artifact": Path(os.path.relpath(capability.business_path, capability.output_path.parent)).as_posix(),
        "mesh_artifact": Path(os.path.relpath(capability.mesh_path, capability.output_path.parent)).as_posix(),
    }
    return TriangularBoundaryPreparationResult(
        format="triangular-boundary-preparation-result-v1",
        product_type="triangular_boundary_preparation",
        preparation_only=True, simulation_result=False, solver_completed=False,
        production_approved=False, boundary_applied=False, status="prepared_not_executable",
        execution_available=False, case_id=config.case_id, time_origin=config.time_origin,
        output_times_s=list(config.output_times_s), coordinate_system=mesh.coordinate_system,
        mesh_id=mesh.mesh_id or "", topology_fingerprint=topology_fingerprint(mesh),
        terrain_fingerprint=terrain_fingerprint(mesh, terrain),
        preparation_config_sha256=_sha256(capability.config_path),
        business_package_sha256=_sha256(capability.business_path), mesh_sha256=_sha256(capability.mesh_path),
        **refs, unbound_exterior_policy=config.unbound_exterior_policy,
        exterior_edge_ids=exterior_ids, boundary_groups=groups,
        unbound_exterior_edges=[PreparedUnboundExteriorEdge(edge_id=edge_id, node_ids=list(mesh.edge_nodes[edge_id])) for edge_id in unbound_ids],
    )


def _safe_build_result(capability: _BoundaryCapability) -> TriangularBoundaryPreparationResult:
    try:
        return _build_result(capability)
    except TriangularBoundaryPreparationError:
        raise
    except (ValidationError, OverflowError) as exc:
        raise TriangularBoundaryPreparationError(f"prepared boundary values exceed the strict finite result contract: {exc}") from exc


def _atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    overwrite: bool,
    expected_existing_identity: tuple[int, int, int, int, int] | None,
    expected_existing_sha256: str | None,
    verify: Callable[[Path], None],
) -> None:
    if not path.parent.is_dir():
        raise TriangularBoundaryPreparationError(f"output directory does not exist: {path.parent}")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp.json", dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        verify(temporary)

        if not overwrite or expected_existing_identity is None:
            try:
                _publish_without_replacing(temporary, path)
            except OSError as exc:
                if path.exists():
                    raise TriangularBoundaryPreparationError(f"output appeared before publication; refusing to overwrite: {path}") from exc
                raise TriangularBoundaryPreparationError(f"cannot atomically publish boundary preparation: {exc}") from exc

        else:
            with _boundary_output_mutex(path):
                try:
                    current_identity, current_sha256 = _output_snapshot(path)
                except OSError as exc:
                    raise TriangularBoundaryPreparationError(
                        f"existing boundary-preparation output disappeared or changed before publication: {path}: {exc}"
                    ) from exc
                if (current_identity != expected_existing_identity
                        or current_sha256 != expected_existing_sha256):
                    raise TriangularBoundaryPreparationError(
                        f"existing boundary-preparation output changed during staging; refusing to replace it: {path}"
                    )
                try:
                    os.replace(temporary, path)
                except OSError as exc:
                    raise TriangularBoundaryPreparationError(
                        f"cannot atomically replace the verified prior preparation output: {path}: {exc}"
                    ) from exc
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def read_triangular_boundary_preparation(path: str | Path, config_path: str | Path,
                                         *, enforce_configured_output_path: bool = True) -> TriangularBoundaryPreparationResult:
    """Re-read actual config/P3-A/P2 and compare the full prepared artifact."""
    artifact_path = Path(path).resolve()
    capability = _load_capability(config_path, allow_existing_output=True)
    if enforce_configured_output_path and artifact_path != capability.output_path:
        raise TriangularBoundaryPreparationError("boundary-preparation artifact path does not match configured output_file")
    try:
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
        actual = TriangularBoundaryPreparationResult.model_validate(raw)
    except FileNotFoundError as exc:
        raise TriangularBoundaryPreparationError(f"boundary-preparation artifact not found: {artifact_path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise TriangularBoundaryPreparationError(f"boundary-preparation artifact is invalid: {exc}") from exc
    expected = _safe_build_result(capability)
    if actual.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise TriangularBoundaryPreparationError("boundary-preparation artifact does not match re-read config, P2 geometry or P3-A bindings")
    return actual


def prepare_triangular_boundaries(config_path: str | Path) -> TriangularBoundaryPreparationResult:
    """Prepare edge geometry/request allocations without applying a boundary."""
    capability = _load_capability(config_path)
    result = _safe_build_result(capability)

    def verify(path: Path) -> None:
        read_triangular_boundary_preparation(path, capability.config_path, enforce_configured_output_path=False)

    try:
        _atomic_write_json(capability.output_path, result.model_dump(mode="json"),
                           overwrite=capability.config.overwrite_existing_output,
                           expected_existing_identity=capability.existing_output_identity,
                           expected_existing_sha256=capability.existing_output_sha256,
                           verify=verify)
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, TriangularBoundaryPreparationError):
            raise
        raise TriangularBoundaryPreparationError(f"could not write boundary-preparation artifact: {exc}") from exc
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare explicit triangular boundary request allocations; does not apply a boundary or run a solver.")
    parser.add_argument("config", help="YAML triangular boundary-preparation config")
    args = parser.parse_args(argv)
    try:
        result = prepare_triangular_boundaries(args.config)
    except (TriangularBoundaryPreparationError, TriangularBusinessError, TriangularMeshError) as exc:
        parser.error(str(exc))
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "BoundaryGroupRequest", "PreparedBoundaryEdge", "PreparedBoundaryGroup", "TriangularBoundaryPreparationConfig",
    "TriangularBoundaryPreparationError", "TriangularBoundaryPreparationResult", "load_triangular_boundary_preparation_config",
    "main", "prepare_triangular_boundaries", "read_triangular_boundary_preparation",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
