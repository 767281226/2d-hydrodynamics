"""R2A offline triangular result-package export and source-graph validation.

This adapter only reads a small, synthetic, closed-wall ``sources=none`` native
result and its actual run/P2/P3-A source graph. It never advances a solver,
rewrites G1 evidence, connects to a database, or publishes an operational result.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from importlib import resources
import json
import math
import os
from pathlib import Path, PureWindowsPath, PurePosixPath
import shutil
import stat
import tempfile
from typing import Any, Literal, Mapping, Sequence
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator
import yaml

from .config import Number, Text
from .gate_dispatch import gate_dispatch_paths, validate_gate_dispatch_exports
from .rehearsal_contract import (
    ExecutionEnvelope,
    ProductCoverage,
    ProductRequest,
    ResultProductSelector,
    ResultSetManifest,
    ScopeDescriptor,
    _digest_json,
    resolve_verified_synthetic_profile,
    validate_execution_identity,
    validate_selector_against_manifest,
)
from .triangular_business import (
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    terrain_fingerprint,
    topology_fingerprint,
)
from .triangular_mesh import FaceTerrainField, TriangularMesh
from .triangular_solver_contract import TriangularRunCapability, TriangularSolverRunConfig, validate_triangular_run_request
from .triangular_solver_netcdf import read_triangular_result
from .triangular_solver_contract import TriangularResultContract


class RehearsalPackageError(ValueError):
    """Raised when a local R2A package or its native source graph is invalid."""


class _PackageStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, allow_inf_nan=False)


class R2AExportRequest(_PackageStrictModel):
    format: Literal["rehearsal-result-package-export-request-v1"]
    schema_version: Literal["1.0"]
    run_config_file: Text
    dispatch_assignment_file: Text
    execution_envelope_file: Text
    output_directory: Text
    fixture_profile_id: Literal["r2a_flat_bed_closed_v1"] = "r2a_flat_bed_closed_v1"
    engine_mode: Literal["triangular_closed_experimental"] = "triangular_closed_experimental"
    scope_id: Literal["R2A_SYNTHETIC_CLOSED_SCOPE"]
    metric_policy_version: Literal["r2a-static-flat-bed-v1"]
    requested_products: list[ProductRequest] = Field(min_length=1, max_length=100)
    require_complete: StrictBool = False

    @field_validator("run_config_file", "dispatch_assignment_file", "execution_envelope_file", "output_directory")
    @classmethod
    def relative_paths(cls, value: str) -> str:
        if (not value.strip() or Path(value).is_absolute() or PureWindowsPath(value).is_absolute()
                or PureWindowsPath(value).drive):
            raise ValueError("package request paths must be non-empty paths relative to the request file")
        return value

    @field_validator("output_directory")
    @classmethod
    def local_output_directory(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if any(part in {".", ".."} for part in normalized.split("/")):
            raise ValueError("output_directory must be a new child path without '.' or '..' components")
        return value

    @model_validator(mode="after")
    def request_paths_are_distinct(self) -> "R2AExportRequest":
        paths = [self.run_config_file, self.dispatch_assignment_file, self.execution_envelope_file, self.output_directory]
        normalized = [Path(value).as_posix().casefold() for value in paths]
        if len(set(normalized)) != len(normalized):
            raise ValueError("run config, identity files and package output directory must have distinct paths")
        if len({item.product_code for item in self.requested_products}) != len(self.requested_products):
            raise ValueError("R2A request may contain each product_code at most once")
        return self


class LocalDispatchAssignment(_PackageStrictModel):
    """Separate local expected identity context; it is not authenticated platform proof."""

    format: Literal["rehearsal-local-dispatch-assignment-v1"]
    assignment_basis: Literal["LOCAL_SYNTHETIC_TEST_CONTEXT"]
    tenant_id: Text
    project_id: Text
    scenario_id: Text
    scenario_version_id: Text
    platform_run_id: Text
    attempt_id: Text
    attempt_no: StrictInt = Field(ge=1)
    script_id: Text
    local_run_id: Text
    model_case_id: Text
    scope_id: Literal["R2A_SYNTHETIC_CLOSED_SCOPE"]

    @field_validator("script_id", "local_run_id")
    @classmethod
    def valid_local_ids(cls, value: str) -> str:
        from .workflow_config import validate_run_id
        try:
            return validate_run_id(value)
        except ValueError as exc:
            raise ValueError(f"script/local run ID is invalid: {exc}") from exc

    @model_validator(mode="after")
    def local_ids_are_distinct_and_compatible(self) -> "LocalDispatchAssignment":
        if self.script_id != self.local_run_id:
            raise ValueError("local_run_id must equal script_id")
        if self.platform_run_id == self.script_id:
            raise ValueError("platform_run_id must not be reused as local_run_id/script_id")
        return self


class R2AMeshProfile(_PackageStrictModel):
    mesh_id: Text
    coordinate_system: Text
    node_xy_m: list[list[Number]] = Field(min_length=3)
    face_nodes: list[list[StrictInt]] = Field(min_length=1)
    face_id: list[StrictInt] = Field(min_length=1)
    boundary_groups: dict[str, list[list[StrictInt]]]
    constraint_groups: dict[str, list[list[StrictInt]]]


class R2ATerrainProfile(_PackageStrictModel):
    elevation_m: list[Number] = Field(min_length=1)
    vertical_datum: Text
    vertical_datum_source: Literal["config_declaration"]


class R2AInitialProfile(_PackageStrictModel):
    quantity: Literal["depth"]
    values_m: list[Number] = Field(min_length=1)


class R2AClosedProfile(_PackageStrictModel):
    format: Literal["rehearsal-r2a-synthetic-profile-v1"]
    profile_id: Literal["r2a_flat_bed_closed_v1"]
    profile_version: Literal["1.0"]
    classification: Literal["SYNTHETIC_ONLY"]
    model_case_id: Text
    engine_mode: Literal["triangular_closed_experimental"]
    scope_id: Literal["R2A_SYNTHETIC_CLOSED_SCOPE"]
    region_id: Text
    mesh: R2AMeshProfile
    terrain: R2ATerrainProfile
    initial_condition: R2AInitialProfile
    roughness_manning_n: Number = Field(gt=0.0, le=1.0)
    boundary_policy: Literal["closed_wall"]
    initial_momentum: Literal["zero"]
    sources: Literal["none"]


@dataclass(frozen=True)
class _ResolvedR2AProfile:
    profile: R2AClosedProfile
    profile_sha256: str
    topology_fingerprint: str
    terrain_fingerprint: str
    scope_hash: str
    face_area_m2: tuple[float, ...]
    face_id: tuple[int, ...]
    total_area_m2: float


class PackageFileRecord(_PackageStrictModel):
    key: Text
    sha256: Text
    byte_count: StrictInt = Field(ge=0)
    media_type: Text

    @field_validator("key")
    @classmethod
    def safe_relative_key(cls, value: str) -> str:
        path = PurePosixPath(value)
        if "\\" in value or path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")) or PureWindowsPath(value).drive:
            raise ValueError("package file key must be a normalized relative path without traversal")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value


class PackageFrame(_PackageStrictModel):
    frame_index: StrictInt = Field(ge=0)
    time_s: Number = Field(ge=0.0, le=1.0)
    maximum_depth_m: Number = Field(ge=0.0)
    maximum_depth_face_id: StrictInt = Field(ge=0)
    water_volume_m3: Number = Field(ge=0.0)
    static_reference_volume_m3: Number = Field(ge=0.0)
    mass_budget_final_volume_m3: Number = Field(ge=0.0)
    mass_budget_residual_m3: Number


class PackageFrames(_PackageStrictModel):
    format: Literal["rehearsal-package-frames-v1"]
    frame_times_s: list[Number] = Field(min_length=2, max_length=8)
    frames: list[PackageFrame] = Field(min_length=2, max_length=8)
    overall_maximum_depth_m: Number = Field(ge=0.0)
    overall_maximum_depth_time_s: Number = Field(ge=0.0, le=1.0)
    overall_maximum_depth_face_id: StrictInt = Field(ge=0)
    maximum_depth_tie_policy: Literal["EARLIEST_TIME_THEN_LOWEST_FACE_ID"]
    overall_maximum_water_volume_m3: Number = Field(ge=0.0)
    overall_maximum_water_volume_time_s: Number = Field(ge=0.0, le=1.0)
    water_volume_tie_policy: Literal["EARLIEST_TIME"]

    @model_validator(mode="after")
    def exact_frame_index_and_time_axis(self) -> "PackageFrames":
        if len(self.frame_times_s) != len(self.frames):
            raise ValueError("frame_times_s and frames must have equal lengths")
        if self.frame_times_s[0] != 0.0 or self.frame_times_s[-1] > 1.0:
            raise ValueError("R2A frames must start at zero and span no more than one second")
        if any(right <= left for left, right in zip(self.frame_times_s, self.frame_times_s[1:])):
            raise ValueError("frame_times_s must be strictly increasing")
        for index, frame in enumerate(self.frames):
            if frame.frame_index != index or frame.time_s != self.frame_times_s[index]:
                raise ValueError("frame_index/time_s must match the native result time coordinate exactly")
        peak = max(frame.maximum_depth_m for frame in self.frames)
        peak_indices = [index for index, frame in enumerate(self.frames) if frame.maximum_depth_m == peak]
        peak_frame = self.frames[peak_indices[0]]
        if (self.overall_maximum_depth_m != peak or self.overall_maximum_depth_time_s != peak_frame.time_s
                or self.overall_maximum_depth_face_id != peak_frame.maximum_depth_face_id):
            raise ValueError("overall maximum depth/time/face must be derived from frames using the declared tie policy")
        max_volume = max(frame.water_volume_m3 for frame in self.frames)
        volume_index = next(index for index, frame in enumerate(self.frames) if frame.water_volume_m3 == max_volume)
        if (self.overall_maximum_water_volume_m3 != max_volume
                or self.overall_maximum_water_volume_time_s != self.frames[volume_index].time_s):
            raise ValueError("overall maximum water volume/time must be derived from frames using the declared tie policy")
        return self


class PackageSummary(_PackageStrictModel):
    format: Literal["rehearsal-package-summary-v1"]
    total_face_area_m2: Number = Field(gt=0.0)
    initial_water_volume_m3: Number = Field(ge=0.0)
    final_water_volume_m3: Number = Field(ge=0.0)
    maximum_water_volume_m3: Number = Field(ge=0.0)
    maximum_water_volume_time_s: Number = Field(ge=0.0, le=1.0)
    maximum_water_volume_tie_policy: Literal["EARLIEST_TIME"]
    maximum_depth_m: Number = Field(ge=0.0)
    maximum_depth_time_s: Number = Field(ge=0.0, le=1.0)
    maximum_depth_face_id: StrictInt = Field(ge=0)
    maximum_depth_tie_policy: Literal["EARLIEST_TIME_THEN_LOWEST_FACE_ID"]
    static_reference_depth_m: Literal[0.2] = 0.2
    static_reference_volume_m3: Number = Field(ge=0.0)
    maximum_depth_error_abs_m: Number = Field(ge=0.0)
    frame_volume_error_abs_max_m3: Number = Field(ge=0.0)


class PackageCheck(_PackageStrictModel):
    check_id: Text
    status: Literal["PASS", "INFO"]
    evidence: Text


class PackageAuditEvidence(_PackageStrictModel):
    status: Literal["VERIFIED_PAIR", "MISSING_INPUT", "MISSING_IDENTITY_BINDING"]
    source_result_file_text: Text | None = None
    consistency_id: Text | None = None
    csv_file_key: Text | None = None
    json_file_key: Text | None = None
    csv_sha256: Text | None = None
    json_sha256: Text | None = None
    record_count: StrictInt | None = Field(default=None, ge=0)
    snapshot_count: StrictInt | None = Field(default=None, ge=0)
    evaluation_count: StrictInt | None = Field(default=None, ge=0)
    script_id_binding_status: Literal["MATCHED_OUTER_CONTEXT", "MISSING"]
    note: Text

    @model_validator(mode="after")
    def evidence_fields_match_status(self) -> "PackageAuditEvidence":
        if self.status == "VERIFIED_PAIR":
            if (not self.source_result_file_text or not self.consistency_id or not self.csv_file_key or not self.json_file_key
                    or not self.csv_sha256 or not self.json_sha256 or self.record_count is None
                    or self.snapshot_count is None or self.evaluation_count is None
                    or self.script_id_binding_status != "MATCHED_OUTER_CONTEXT"):
                raise ValueError("VERIFIED_PAIR requires both raw files, hashes, counts, consistency ID and a matched outer script binding")
        for digest in (self.csv_sha256, self.json_sha256):
            if digest is not None and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
                raise ValueError("G1 file digests must be lowercase SHA-256 values")
        if (self.csv_file_key is None) != (self.csv_sha256 is None) or (self.json_file_key is None) != (self.json_sha256 is None):
            raise ValueError("G1 file keys and hashes must appear in pairs")
        if self.consistency_id is not None and (len(self.consistency_id) != 64 or any(char not in "0123456789abcdef" for char in self.consistency_id)):
            raise ValueError("G1 consistency_id must be a lowercase SHA-256 digest")
        return self


class PackageIdentity(_PackageStrictModel):
    tenant_id: Text
    project_id: Text
    scenario_id: Text
    scenario_version_id: Text
    platform_run_id: Text
    attempt_id: Text
    attempt_no: StrictInt = Field(ge=1)
    script_id: Text
    local_run_id: Text
    model_case_id: Text
    input_sha256: Text
    execution_envelope_sha256: Text
    dispatch_assignment_sha256: Text
    identity_validation_status: Literal["LOCAL_CONTEXT_MATCHED"]
    identity_authentication_status: Literal["NOT_ASSESSED"]
    native_identity_basis: Literal["OUTER_EXECUTION_CONTEXT_ONLY"]
    native_identity_fields_missing: list[Literal["platform_run_id", "attempt_id", "script_id", "local_run_id"]]

    @field_validator("script_id", "local_run_id")
    @classmethod
    def valid_script_ids(cls, value: str) -> str:
        from .workflow_config import validate_run_id
        try:
            return validate_run_id(value)
        except ValueError as exc:
            raise ValueError(f"script/local_run_id is invalid: {exc}") from exc

    @field_validator("input_sha256", "execution_envelope_sha256", "dispatch_assignment_sha256")
    @classmethod
    def valid_input_digest(cls, value: str) -> str:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("input_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def local_identity_pair(self) -> "PackageIdentity":
        if self.script_id != self.local_run_id:
            raise ValueError("legacy local_run_id must equal script_id")
        expected_missing = ["platform_run_id", "attempt_id", "script_id", "local_run_id"]
        if self.native_identity_fields_missing != expected_missing:
            raise ValueError("native_identity_fields_missing must list the four outer-only IDs in their canonical order")
        return self


class PackageSourceGraph(_PackageStrictModel):
    validation_basis: Literal["LOCAL_SOURCE_GRAPH_V1"]
    request_path: Text
    run_config_path: Text
    business_package_path: Text
    mesh_path: Text
    native_result_path: Text
    request_sha256: Text
    run_config_sha256: Text
    business_package_sha256: Text
    mesh_sha256: Text
    native_result_sha256: Text
    topology_fingerprint: Text
    terrain_fingerprint: Text
    source_graph_sha256: Text
    portability: Literal["LOCAL_ONLY_REQUIRES_ORIGINAL_SOURCE_GRAPH"]

    @field_validator("request_sha256", "run_config_sha256", "business_package_sha256", "mesh_sha256", "native_result_sha256", "topology_fingerprint", "terrain_fingerprint", "source_graph_sha256")
    @classmethod
    def all_hashes_are_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("source graph identity values must be lowercase SHA-256 digests")
        return value

    @field_validator("request_path", "run_config_path", "business_package_path", "mesh_path", "native_result_path")
    @classmethod
    def source_paths_are_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute() and not PureWindowsPath(value).is_absolute():
            raise ValueError("LOCAL_SOURCE_GRAPH_V1 source paths must record the original absolute paths")
        return value


class PackageProductStatus(_PackageStrictModel):
    product_code: Literal[
        "water_depth", "storage_volume", "maximum_depth", "wet_area", "gate_audit", "wet_duration", "first_arrival",
        "peak_inundation_area", "inundation_footprint", "velocity", "water_level", "depth_band_stat",
        "affected_population", "flood_loss", "evacuation_plan", "scene_3d", "boundary_series",
    ]
    required: StrictBool
    status: Literal["COMPLETE", "MISSING_INPUT", "UNSUPPORTED", "NOT_COMPUTED"]
    reason: Text | None = None


class RehearsalResultPackageManifest(_PackageStrictModel):
    format: Literal["rehearsal-result-package-v1"]
    schema_version: Literal["1.0"]
    product_type: Literal["offline_rehearsal_result_package"]
    result_set_id: Text
    package_status: Literal["COMPLETE_OFFLINE_REVIEW", "INCOMPLETE_REVIEW"]
    publication_state: Literal["OFFLINE_REVIEW_STAGING"]
    can_publish: Literal[False] = False
    engineering_validated: Literal[False] = False
    production_approved: Literal[False] = False
    validation_basis: Literal["LOCAL_SOURCE_GRAPH_V1"]
    identity: PackageIdentity
    source_graph: PackageSourceGraph
    engine_mode: Literal["triangular_closed_experimental"]
    fixture_profile_id: Literal["r2a_flat_bed_closed_v1"]
    fixture_profile_version: Literal["1.0"]
    fixture_profile_sha256: Text
    scope_id: Literal["R2A_SYNTHETIC_CLOSED_SCOPE"]
    scope_hash: Text
    metric_policy_version: Literal["r2a-static-flat-bed-v1"]
    frame_times_s: list[Number] = Field(min_length=2, max_length=8)
    products: list[PackageProductStatus] = Field(min_length=1)
    gate_audit: PackageAuditEvidence
    native_identity_limit: Text
    static_reference: PackageSummary
    quality_checks: list[PackageCheck] = Field(min_length=1)
    limitations: list[Text] = Field(min_length=1)
    frames_file: Literal["frames.json"] = "frames.json"
    summary_file: Literal["summary.json"] = "summary.json"
    selectors_file: Literal["selectors.json"] = "selectors.json"
    quality_file: Literal["quality.json"] = "quality.json"
    files: list[PackageFileRecord] = Field(min_length=4)

    @field_validator("scope_hash")
    @classmethod
    def scope_hash_is_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("scope_hash must be a lowercase SHA-256 digest")
        return value

    @field_validator("fixture_profile_sha256")
    @classmethod
    def profile_digest_is_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("fixture_profile_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def package_flags_and_file_manifest(self) -> "RehearsalResultPackageManifest":
        keys = [item.key for item in self.files]
        if len(set(keys)) != len(keys):
            raise ValueError("package file keys must be unique")
        if "manifest.json" in keys:
            raise ValueError("manifest.json cannot include a self-referential file digest")
        if len(self.frame_times_s) > 8 or self.frame_times_s[-1] > 1.0:
            raise ValueError("R2A package frame axis must stay within 8 points and one second")
        if self.frame_times_s[0] != 0.0 or any(right <= left for left, right in zip(self.frame_times_s, self.frame_times_s[1:])):
            raise ValueError("R2A package frame axis must start at zero and be strictly increasing")
        required_keys = {"native/result.nc", "frames.json", "summary.json", "selectors.json", "quality.json",
                         "identity/execution_envelope.json", "identity/dispatch_assignment.json", "identity/export_request.yaml"}
        if not required_keys.issubset(set(keys)):
            raise ValueError(f"package file manifest is missing required files: {sorted(required_keys - set(keys))}")
        product_codes = [item.product_code for item in self.products]
        if len(product_codes) != len(set(product_codes)):
            raise ValueError("package product status codes must be unique")
        check_ids = [item.check_id for item in self.quality_checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("quality check IDs must be unique")
        if self.identity.input_sha256 != self.source_graph.source_graph_sha256:
            raise ValueError("execution identity input_sha256 must equal the validated local source graph digest")
        should_be_complete = self.gate_audit.status == "VERIFIED_PAIR" and all(
            not item.required or item.status == "COMPLETE" for item in self.products
        )
        if (self.package_status == "COMPLETE_OFFLINE_REVIEW") != should_be_complete:
            raise ValueError("package_status must match G1 evidence and required-product statuses exactly")
        if self.can_publish or self.engineering_validated or self.production_approved:
            raise ValueError("R2A packages are offline review artifacts and cannot claim publication/engineering approval")
        return self


class PackageValidationReport(_PackageStrictModel):
    valid: StrictBool
    complete: StrictBool
    can_publish: Literal[False] = False
    package_status: Literal["COMPLETE_OFFLINE_REVIEW", "INCOMPLETE_REVIEW"]
    result_set_id: Text
    checks: list[PackageCheck]
    blockers: list[Text]


@dataclass(frozen=True)
class _R2ASourceContext:
    request_path: Path
    request: R2AExportRequest
    run_config_path: Path
    native_result_path: Path
    envelope_path: Path
    assignment_path: Path
    output_path: Path
    capability: TriangularRunCapability
    envelope: ExecutionEnvelope | None
    assignment: LocalDispatchAssignment
    input_sha256: str
    scope_hash: str
    profile_sha256: str
    source_paths: dict[str, Path]
    source_hashes: dict[str, str]


class R2APackageSourceError(RehearsalPackageError):
    """Raised when native result/source graph inputs are invalid or inconsistent."""


_R2A_PROFILE_SHA256 = "52589d5056c5e3b246c95c7affbc8ca3a0e43f63b3d8ac8db05dd9e43e21ea8b"


def _load_r2a_profile() -> _ResolvedR2AProfile:
    try:
        raw = resources.files("hydrodynamics").joinpath("rehearsal_data/r2a_flat_bed_closed_v1.json").read_bytes()
    except OSError as exc:
        raise R2APackageSourceError(f"packaged R2A synthetic fixture is unavailable: {exc}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != _R2A_PROFILE_SHA256:
        raise R2APackageSourceError("packaged R2A synthetic fixture digest differs from the reviewed profile")
    try:
        profile = R2AClosedProfile.model_validate_json(raw)
        mesh_data = profile.mesh
        mesh = TriangularMesh(
            node_xy=mesh_data.node_xy_m, face_nodes=mesh_data.face_nodes,
            coordinate_system=mesh_data.coordinate_system, mesh_id=mesh_data.mesh_id,
            face_id=mesh_data.face_id, boundary_groups=mesh_data.boundary_groups,
            constraint_groups=mesh_data.constraint_groups,
        )
        terrain_data = profile.terrain
        terrain = FaceTerrainField(
            mesh_id=mesh.mesh_id or "", elevation_m=tuple(terrain_data.elevation_m),
            valid_mask=(True,) * mesh.n_face,
            covered_area_m2=tuple(mesh.face_area), valid_area_m2=tuple(mesh.face_area),
            coverage_ratio=(1.0,) * mesh.n_face, coordinate_system=mesh.coordinate_system,
            vertical_datum=terrain_data.vertical_datum,
            vertical_datum_source=terrain_data.vertical_datum_source,
        )
        roughness = TriangularRoughnessField.from_constant(mesh, float(profile.roughness_manning_n), source="R2A pinned synthetic profile")
        initial = TriangularInitialConditionField.from_assignments(
            mesh, terrain, profile.initial_condition.quantity,
            tuple(zip(mesh.face_id, profile.initial_condition.values_m)), source="R2A pinned static depth profile",
        )
        business = TriangularBusinessPackage(mesh, terrain, roughness, initial, ())
        top = topology_fingerprint(business.mesh)
        terrain_hash = terrain_fingerprint(business.mesh, business.terrain)
    except (ValidationError, ValueError, TypeError) as exc:
        raise R2APackageSourceError(f"packaged R2A synthetic fixture is invalid: {exc}") from exc
    scope_hash = _digest_json({"profile_sha256": digest, "scope_id": profile.scope_id,
                               "topology_fingerprint": top, "terrain_fingerprint": terrain_hash})
    return _ResolvedR2AProfile(
        profile=profile, profile_sha256=digest, topology_fingerprint=top,
        terrain_fingerprint=terrain_hash, scope_hash=scope_hash,
        face_area_m2=tuple(float(value) for value in mesh.face_area),
        face_id=tuple(int(value) for value in mesh.face_id), total_area_m2=math.fsum(mesh.face_area),
    )


_PACKAGE_REQUEST_FORMAT = "rehearsal-result-package-export-request-v1"
_PACKAGE_FORMAT = "rehearsal-result-package-v1"
_SUPPORTED_PRODUCTS = frozenset({"water_depth", "storage_volume", "maximum_depth", "gate_audit"})


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RehearsalPackageError(f"value cannot be encoded as canonical finite JSON: {exc}") from exc


def _json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RehearsalPackageError(f"value cannot be encoded as finite JSON: {exc}") from exc


def _strict_json_loads(raw: bytes | str, label: str) -> Any:
    def pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value!r} is not permitted")

    try:
        return json.loads(raw, object_pairs_hook=pairs_no_duplicates, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RehearsalPackageError(f"{label} is not strict JSON: {exc}") from exc


def _strict_yaml_loads(raw: bytes | str, label: str) -> Any:
    class UniqueKeyLoader(yaml.SafeLoader):
        def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
            self.flatten_mapping(node)
            mapping: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in mapping
                except TypeError as exc:
                    raise ValueError("YAML mapping keys must be scalar/hashable") from exc
                if duplicate:
                    raise ValueError(f"duplicate YAML mapping key {key!r}")
                mapping[key] = self.construct_object(value_node, deep=deep)
            return mapping

    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return yaml.load(text, Loader=UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        raise RehearsalPackageError(f"{label} is not strict YAML: {exc}") from exc


def _read_regular_file(path: Path, label: str) -> bytes:
    if path.is_symlink():
        raise RehearsalPackageError(f"{label} must not be a symbolic link: {path}")
    if not path.is_file():
        raise RehearsalPackageError(f"{label} must be an existing regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RehearsalPackageError(f"cannot read {label} {path}: {exc}") from exc


def _has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _is_reparse_point(path: Path) -> bool:
    """Detect symlinks and Windows directory reparse points on Python 3.11+."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RehearsalPackageError(f"cannot inspect path component {path}: {exc}") from exc
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(info, "st_file_attributes", 0))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _has_reparse_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _is_reparse_point(current):
            return True
    return False


def _resolve_request_file(base: Path, relative: str, label: str) -> Path:
    candidate = base / relative
    resolved = candidate.resolve()
    if _has_symlink_component(candidate):
        raise RehearsalPackageError(f"{label} must not traverse a symbolic link: {candidate}")
    if not resolved.is_file():
        raise RehearsalPackageError(f"{label} is not an existing file: {resolved}")
    return resolved


def _resolve_output_directory(request_path: Path, relative: str) -> Path:
    request_parent = request_path.parent.resolve()
    candidate = Path(os.path.abspath(request_parent / relative))
    if _has_reparse_component(candidate):
        raise RehearsalPackageError(f"output_directory must not traverse a symbolic link or directory reparse point: {candidate}")
    normalized = candidate.resolve(strict=False)
    try:
        relative_to_request = normalized.relative_to(request_parent)
    except ValueError as exc:
        raise RehearsalPackageError("normalized output_directory must remain a new child path under the request directory") from exc
    if not relative_to_request.parts or any(part in {"", ".", ".."} for part in relative_to_request.parts):
        raise RehearsalPackageError("normalized output_directory must be a new child path under the request directory")
    return normalized


def _resolve_package_request(path: str | Path) -> tuple[Path, R2AExportRequest, bytes]:
    request_candidate = Path(os.path.abspath(path))
    if _has_symlink_component(request_candidate):
        raise RehearsalPackageError(f"package request path must not traverse a symbolic link: {request_candidate}")
    request_path = request_candidate.resolve()
    raw = _read_regular_file(request_path, "package export request")
    try:
        data = _strict_yaml_loads(raw, "package export request")
        request = R2AExportRequest.model_validate(data)
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise RehearsalPackageError(f"invalid R2A package export request: {exc}") from exc
    if request.format != _PACKAGE_REQUEST_FORMAT:
        raise RehearsalPackageError(f"package request format must be {_PACKAGE_REQUEST_FORMAT}")
    product_keys = [(item.product_code, item.scope_id, item.aggregation) for item in request.requested_products]
    if len(product_keys) != len(set(product_keys)):
        raise RehearsalPackageError("requested_products must not contain duplicate product/scope/aggregation selectors")
    if request.scope_id != "R2A_SYNTHETIC_CLOSED_SCOPE":
        raise RehearsalPackageError("R2A scope_id must be R2A_SYNTHETIC_CLOSED_SCOPE")
    for item in request.requested_products:
        expected_scope = "AUDIT_RUN_GLOBAL" if item.product_code == "gate_audit" else request.scope_id
        if item.scope_id != expected_scope:
            raise RehearsalPackageError(f"requested product {item.product_code} must use scope {expected_scope}")
    return request_path, request, raw


def _source_paths_and_hashes(
    request_path: Path, request: R2AExportRequest, request_bytes: bytes, *, require_envelope: bool,
) -> tuple[dict[str, Path], dict[str, bytes], LocalDispatchAssignment, ExecutionEnvelope | None, Path]:
    base = request_path.parent
    run_config_path = _resolve_request_file(base, request.run_config_file, "run_config_file")
    assignment_path = _resolve_request_file(base, request.dispatch_assignment_file, "dispatch_assignment_file")
    envelope_candidate = base / request.execution_envelope_file
    if _has_symlink_component(envelope_candidate):
        raise RehearsalPackageError(f"execution envelope path must not traverse a symbolic link: {envelope_candidate}")
    envelope_path = envelope_candidate.resolve()
    if require_envelope and not envelope_path.is_file():
        raise RehearsalPackageError(f"execution envelope file is missing: {envelope_path}")
    assignment_bytes = _read_regular_file(assignment_path, "local dispatch assignment")
    try:
        assignment = LocalDispatchAssignment.model_validate(_strict_json_loads(assignment_bytes, "local dispatch assignment"))
    except ValidationError as exc:
        raise RehearsalPackageError(f"invalid local dispatch assignment: {exc}") from exc
    envelope: ExecutionEnvelope | None = None
    envelope_bytes = b""
    if require_envelope:
        envelope_bytes = _read_regular_file(envelope_path, "execution envelope")
        try:
            envelope = ExecutionEnvelope.model_validate(_strict_json_loads(envelope_bytes, "execution envelope"))
        except ValidationError as exc:
            raise RehearsalPackageError(f"invalid R1 execution envelope: {exc}") from exc
    run_config_bytes = _read_regular_file(run_config_path, "triangular run config")
    run_config_data = _strict_yaml_loads(run_config_bytes, "triangular run config")
    try:
        strict_run_config = TriangularSolverRunConfig.model_validate(run_config_data)
    except ValidationError as exc:
        raise RehearsalPackageError(f"triangular run config failed strict schema validation: {exc}") from exc
    try:
        capability = validate_triangular_run_request(run_config_path, allow_existing_result=True)
    except (ValueError, OSError, ImportError) as exc:
        raise RehearsalPackageError(f"actual triangular run/P3-A/P2 source graph is invalid or unavailable: {exc}") from exc
    # Reject symlinked input dependencies before trusting the canonical paths returned by the run validator.
    business_lexical = run_config_path.parent / strict_run_config.business_file
    result_lexical = run_config_path.parent / strict_run_config.result_file
    try:
        business_bytes = _read_regular_file(capability.business_path, "P3-A business package")
        raw_business = _strict_json_loads(business_bytes, "P3-A business package")
        mesh_lexical = business_lexical.parent / raw_business["mesh_artifact"]
    except (OSError, KeyError, TypeError) as exc:
        raise RehearsalPackageError(f"cannot resolve the P3-A/P2 source graph: {exc}") from exc
    if (_has_symlink_component(business_lexical) or _has_symlink_component(mesh_lexical) or _has_symlink_component(result_lexical)
            or _has_symlink_component(capability.mesh_path) or _has_symlink_component(capability.result_path)):
        raise RehearsalPackageError("run config, P3-A package, and P2 mesh source paths must not traverse symbolic links")
    source_paths = {
        "request": request_path,
        "run_config": run_config_path,
        "business_package": capability.business_path,
        "mesh": capability.mesh_path,
        "assignment": assignment_path,
        "envelope": envelope_path,
        "native_result": capability.result_path,
    }
    source_bytes = {
        "request": request_bytes,
        "run_config": run_config_bytes,
        "business_package": business_bytes,
        "mesh": _read_regular_file(source_paths["mesh"], "P2 mesh artifact"),
        "assignment": assignment_bytes,
    }
    if require_envelope:
        source_bytes["envelope"] = envelope_bytes
    if capability.config.case_id != assignment.model_case_id:
        raise RehearsalPackageError("dispatch assignment model_case_id differs from triangular run config case_id")
    if assignment.model_case_id != _load_r2a_profile().profile.model_case_id:
        raise RehearsalPackageError("dispatch assignment model_case_id does not match the pinned R2A synthetic case")
    if capability.config.boundary_policy != "closed_wall" or capability.config.initial_momentum != "zero" or capability.config.sources != "none":
        raise RehearsalPackageError("R2A accepts only boundary_policy=closed_wall, initial_momentum=zero, sources=none")
    if len(capability.config.output_times_s) > 8 or capability.config.output_times_s[-1] > 1.0:
        raise RehearsalPackageError("R2A accepts at most 8 output frames over a duration of at most 1 second")
    if len(capability.config.output_times_s) < 2 or capability.config.output_times_s[0] != 0.0:
        raise RehearsalPackageError("R2A requires at least two output times starting at 0 seconds")
    if capability.business.mesh.n_face > 64:
        raise RehearsalPackageError("R2A accepts no more than 64 triangular faces")
    if capability.business.boundary_bindings:
        raise RehearsalPackageError("R2A closed fixture does not accept P3-A boundary bindings")
    if assignment.scope_id != request.scope_id:
        raise RehearsalPackageError("dispatch assignment scope_id differs from package request")
    return source_paths, source_bytes, assignment, envelope, envelope_path


def _validate_actual_profile(capability: TriangularRunCapability, resolved: _ResolvedR2AProfile) -> None:
    expected = resolved.profile
    mesh = capability.business.mesh
    expected_mesh = TriangularMesh(
        node_xy=expected.mesh.node_xy_m, face_nodes=expected.mesh.face_nodes,
        coordinate_system=expected.mesh.coordinate_system, mesh_id=expected.mesh.mesh_id,
        face_id=expected.mesh.face_id, boundary_groups=expected.mesh.boundary_groups,
        constraint_groups=expected.mesh.constraint_groups,
    )
    terrain = capability.business.terrain
    roughness = capability.business.roughness
    initial = capability.business.initial_condition
    if mesh.mesh_id != expected.mesh.mesh_id or mesh.coordinate_system != expected.mesh.coordinate_system:
        raise RehearsalPackageError("actual P2 mesh ID/coordinate system does not match the pinned R2A fixture")
    if tuple(tuple(float(v) for v in row) for row in mesh.node_xy) != tuple(tuple(float(v) for v in row) for row in expected.mesh.node_xy_m):
        raise RehearsalPackageError("actual P2 node coordinates do not match the pinned R2A fixture")
    if tuple(tuple(int(v) for v in row) for row in mesh.face_nodes) != tuple(tuple(row) for row in expected.mesh.face_nodes):
        raise RehearsalPackageError("actual P2 connectivity does not match the pinned R2A fixture")
    if tuple(int(v) for v in mesh.face_id) != tuple(expected.mesh.face_id):
        raise RehearsalPackageError("actual P2 face IDs do not match the pinned R2A fixture")
    if dict(mesh.boundary_groups) != dict(expected_mesh.boundary_groups):
        raise RehearsalPackageError("actual P2 boundary groups do not match the pinned R2A fixture")
    if dict(mesh.constraint_groups) != dict(expected_mesh.constraint_groups):
        raise RehearsalPackageError("actual P2 constraint groups do not match the pinned R2A fixture")
    if tuple(float(v) for v in terrain.elevation_m) != tuple(float(v) for v in expected.terrain.elevation_m):
        raise RehearsalPackageError("actual P3-A terrain does not match the pinned flat-bed R2A fixture")
    if terrain.vertical_datum != expected.terrain.vertical_datum or terrain.vertical_datum_source != expected.terrain.vertical_datum_source:
        raise RehearsalPackageError("actual P3-A terrain datum does not match the pinned R2A fixture")
    if not all(terrain.valid_mask) or tuple(float(v) for v in terrain.coverage_ratio) != (1.0,) * mesh.n_face:
        raise RehearsalPackageError("actual P3-A terrain must be complete over every R2A face")
    if initial.quantity.value != "depth" or not all(initial.valid_mask) or tuple(float(v) for v in initial.values_m) != tuple(expected.initial_condition.values_m):
        raise RehearsalPackageError("actual P3-A initial depth must match the pinned 0.2 m R2A static reference")
    if tuple(int(v) for v in initial.face_id) != tuple(mesh.face_id):
        raise RehearsalPackageError("actual P3-A initial depth face order differs from the P2 mesh")
    if not all(roughness.valid_mask) or tuple(int(v) for v in roughness.face_id) != tuple(mesh.face_id):
        raise RehearsalPackageError("actual P3-A roughness must be complete and aligned to the P2 mesh")
    if tuple(float(v) for v in roughness.manning_n) != (float(expected.roughness_manning_n),) * mesh.n_face:
        raise RehearsalPackageError("actual P3-A Manning roughness does not match the pinned R2A fixture")
    if capability.config.case_id != expected.model_case_id or capability.config.boundary_policy != expected.boundary_policy:
        raise RehearsalPackageError("run config case/boundary policy does not match the pinned R2A fixture")


def _input_graph_digest(capability: TriangularRunCapability, source_bytes: Mapping[str, bytes], resolved: _ResolvedR2AProfile) -> str:
    values = {
        "format": "LOCAL_SOURCE_GRAPH_V1",
        "run_config_sha256": _sha256_bytes(source_bytes["run_config"]),
        "business_package_sha256": _sha256_bytes(source_bytes["business_package"]),
        "mesh_sha256": _sha256_bytes(source_bytes["mesh"]),
        "fixture_profile_sha256": resolved.profile_sha256,
        "topology_fingerprint": resolved.topology_fingerprint,
        "terrain_fingerprint": resolved.terrain_fingerprint,
        "scope_id": resolved.profile.scope_id,
        "scope_hash": resolved.scope_hash,
        "model_case_id": capability.config.case_id,
        "engine_mode": "triangular_closed_experimental",
        "output_times_s": [float(value) for value in capability.config.output_times_s],
        "boundary_policy": capability.config.boundary_policy,
        "initial_momentum": capability.config.initial_momentum,
        "sources": capability.config.sources,
        "initial_depth_m": [float(value) for value in capability.business.initial_condition.values_m],
        "manning_n": [float(value) for value in capability.business.roughness.manning_n],
    }
    return _sha256_bytes(_canonical_json(values))


def _load_source_context(request_path: str | Path, *, require_envelope: bool, require_native: bool) -> _R2ASourceContext:
    resolved_profile = _load_r2a_profile()
    request_path_resolved, request, request_raw = _resolve_package_request(request_path)
    paths, source_bytes, assignment, envelope, envelope_path = _source_paths_and_hashes(
        request_path_resolved, request, request_raw, require_envelope=require_envelope,
    )
    try:
        capability = validate_triangular_run_request(paths["run_config"], allow_existing_result=True)
    except (ValueError, OSError, ImportError) as exc:
        raise RehearsalPackageError(f"actual triangular run/P3-A/P2 source graph is invalid or unavailable: {exc}") from exc
    _validate_actual_profile(capability, resolved_profile)
    input_sha256 = _input_graph_digest(capability, source_bytes, resolved_profile)
    if envelope is not None:
        try:
            validate_execution_identity(
                envelope,
                trusted_tenant_id=assignment.tenant_id,
                trusted_project_id=assignment.project_id,
                trusted_scenario_id=assignment.scenario_id,
                trusted_scenario_version_id=assignment.scenario_version_id,
                trusted_platform_run_id=assignment.platform_run_id,
                trusted_attempt_id=assignment.attempt_id,
                trusted_attempt_no=assignment.attempt_no,
                trusted_script_id=assignment.script_id,
                trusted_model_case_id=assignment.model_case_id,
                trusted_input_sha256=input_sha256,
                trusted_engine_mode=request.engine_mode,
            )
        except ValueError as exc:
            raise RehearsalPackageError(f"execution envelope does not match the separate local dispatch assignment/source graph: {exc}") from exc
        if envelope.execution_origin != "PLATFORM_MODEL":
            raise RehearsalPackageError("execution envelope origin is not compatible with the R1 envelope contract")
    native_path = paths["native_result"]
    if require_native:
        _read_regular_file(native_path, "configured native triangular result")
        paths["native_result"] = native_path.resolve()
    source_hashes = {
        "request": _sha256_bytes(source_bytes["request"]),
        "run_config": _sha256_bytes(source_bytes["run_config"]),
        "business_package": _sha256_bytes(source_bytes["business_package"]),
        "mesh": _sha256_bytes(source_bytes["mesh"]),
        "assignment": _sha256_bytes(source_bytes["assignment"]),
    }
    if require_envelope:
        source_hashes["envelope"] = _sha256_bytes(source_bytes["envelope"])
    scope_hash = resolved_profile.scope_hash
    output_path = _resolve_output_directory(request_path_resolved, request.output_directory)
    return _R2ASourceContext(
        request_path=request_path_resolved, request=request, run_config_path=paths["run_config"],
        native_result_path=native_path, envelope_path=envelope_path,
        assignment_path=paths["assignment"], output_path=output_path,
        capability=capability, envelope=envelope, assignment=assignment, input_sha256=input_sha256,
        scope_hash=scope_hash, profile_sha256=resolved_profile.profile_sha256,
        source_paths=paths, source_hashes=source_hashes,
    )


def _assert_source_unchanged(context: _R2ASourceContext, result_sha256: str | None = None) -> None:
    expected = dict(context.source_hashes)
    for key, value in expected.items():
        if _sha256_bytes(_read_regular_file(context.source_paths[key], f"source {key}")) != value:
            raise RehearsalPackageError(f"source graph file changed while the R2A package was being processed: {context.source_paths[key]}")
    if result_sha256 is not None:
        current = _sha256_bytes(_read_regular_file(context.native_result_path, "native result"))
        if current != result_sha256:
            raise RehearsalPackageError("native result changed while the R2A package was being processed")


def _native_contract(context: _R2ASourceContext) -> tuple[TriangularResultContract, bytes, str, _ResolvedR2AProfile]:
    resolved = _load_r2a_profile()
    _validate_actual_profile(context.capability, resolved)
    native_raw = _read_regular_file(context.native_result_path, "native triangular result")
    native_sha = _sha256_bytes(native_raw)
    try:
        result = read_triangular_result(context.native_result_path, context.run_config_path, enforce_configured_result_path=True)
    except Exception as exc:
        raise RehearsalPackageError(f"existing native result failed the full P4-1 reader/source-graph checks: {exc}") from exc
    _assert_source_unchanged(context, native_sha)
    if result.case_id != context.assignment.model_case_id:
        raise RehearsalPackageError("native result case_id differs from the local dispatch assignment")
    if tuple(float(t) for t in result.time_s) != tuple(float(t) for t in context.capability.config.output_times_s):
        raise RehearsalPackageError("native result time axis differs from the actual run config output_times_s")
    if len(result.time_s) > 8 or float(result.time_s[-1]) > 1.0:
        raise RehearsalPackageError("native result exceeds the R2A 8-frame/1-second scope")
    return result, native_raw, native_sha, resolved


def _compute_static_metrics(context: _R2ASourceContext, result: TriangularResultContract, resolved: _ResolvedR2AProfile) -> tuple[PackageFrames, PackageSummary, list[PackageCheck]]:
    if tuple(int(face) for face in result.face_id) != resolved.face_id:
        raise RehearsalPackageError("native result face ID order differs from the pinned R2A fixture")
    areas = tuple(float(value) for value in result.face_area_m2)
    if len(areas) != len(result.face_id) or any(not math.isfinite(value) or value <= 0.0 for value in areas):
        raise RehearsalPackageError("native result face areas must be finite, positive and aligned to face IDs")
    if any(not math.isclose(area, expected, rel_tol=1e-12, abs_tol=1e-12) for area, expected in zip(areas, resolved.face_area_m2)):
        raise RehearsalPackageError("native result face areas differ from the actual P2 geometry")
    total_area = math.fsum(areas)
    static_volume = total_area * 0.2
    frame_records: list[PackageFrame] = []
    global_max = -math.inf
    global_time = 0.0
    global_face = min(resolved.face_id)
    depth_error = 0.0
    volume_error_max = 0.0
    for frame_index, (time_s, depth_values, budget) in enumerate(zip(result.time_s, result.water_depth_m, result.mass_budget)):
        depths = tuple(float(value) for value in depth_values)
        if len(depths) != len(areas) or any(not math.isfinite(value) or value < 0.0 for value in depths):
            raise RehearsalPackageError(f"native water depth frame {frame_index} is not finite/non-negative or face aligned")
        frame_error = max((abs(value - 0.2) for value in depths), default=0.0)
        depth_error = max(depth_error, frame_error)
        if frame_error > 1e-12:
            raise RehearsalPackageError(f"native static-reference depth error exceeds 1e-12 m at frame {frame_index}")
        volume = math.fsum(area * depth for area, depth in zip(areas, depths))
        volume_error = abs(volume - static_volume)
        volume_error_max = max(volume_error_max, volume_error)
        if volume_error > 1e-12:
            raise RehearsalPackageError(f"native static-reference volume error exceeds 1e-12 m3 at frame {frame_index}")
        if abs(volume - float(budget.final_volume_m3)) > 1e-12:
            raise RehearsalPackageError(f"area-integrated volume differs from P4-1 mass budget by more than 1e-12 m3 at frame {frame_index}")
        frame_max = max(depths)
        frame_max_face = min(int(result.face_id[i]) for i, value in enumerate(depths) if value == frame_max)
        if frame_max > global_max:
            global_max, global_time, global_face = frame_max, float(time_s), frame_max_face
        frame_records.append(PackageFrame(
            frame_index=frame_index, time_s=float(time_s), maximum_depth_m=frame_max,
            maximum_depth_face_id=frame_max_face, water_volume_m3=volume,
            static_reference_volume_m3=static_volume, mass_budget_final_volume_m3=float(budget.final_volume_m3),
            mass_budget_residual_m3=float(budget.residual_m3),
        ))
    maximum_water_volume = max(frame.water_volume_m3 for frame in frame_records)
    maximum_water_volume_time = next(frame.time_s for frame in frame_records if frame.water_volume_m3 == maximum_water_volume)
    frames = PackageFrames(
        format="rehearsal-package-frames-v1", frame_times_s=[float(v) for v in result.time_s], frames=frame_records,
        overall_maximum_depth_m=global_max, overall_maximum_depth_time_s=global_time,
        overall_maximum_depth_face_id=global_face,
        maximum_depth_tie_policy="EARLIEST_TIME_THEN_LOWEST_FACE_ID",
        overall_maximum_water_volume_m3=maximum_water_volume,
        overall_maximum_water_volume_time_s=maximum_water_volume_time,
        water_volume_tie_policy="EARLIEST_TIME",
    )
    summary = PackageSummary(
        format="rehearsal-package-summary-v1", total_face_area_m2=total_area,
        initial_water_volume_m3=frame_records[0].water_volume_m3,
        final_water_volume_m3=frame_records[-1].water_volume_m3,
        maximum_water_volume_m3=maximum_water_volume,
        maximum_water_volume_time_s=maximum_water_volume_time,
        maximum_water_volume_tie_policy="EARLIEST_TIME",
        maximum_depth_m=global_max, maximum_depth_time_s=global_time,
        maximum_depth_face_id=global_face, maximum_depth_tie_policy="EARLIEST_TIME_THEN_LOWEST_FACE_ID",
        static_reference_volume_m3=static_volume, maximum_depth_error_abs_m=depth_error,
        frame_volume_error_abs_max_m3=volume_error_max,
    )
    checks = [
        PackageCheck(check_id="native_result_bound_to_current_p2_p3_run", status="PASS", evidence="Existing native result read by the unchanged P4-1 reader with current run config, P3-A package and P2 mesh."),
        PackageCheck(check_id="profile_topology_and_terrain_match", status="PASS", evidence=f"Pinned profile {resolved.profile.profile_id}; topology and terrain fingerprints match actual source graph."),
        PackageCheck(check_id="static_depth_reference_abs_error_le_1e-12_m", status="PASS", evidence=f"Maximum absolute depth error = {depth_error:.17g} m across all faces and frames."),
        PackageCheck(check_id="static_volume_reference_abs_error_le_1e-12_m3", status="PASS", evidence=f"Maximum absolute volume error = {volume_error_max:.17g} m3 across all frames; reference uses actual face areas."),
        PackageCheck(check_id="area_integral_matches_mass_budget", status="PASS", evidence="Each frame's fsum(face_area_m2 * water_depth_m) matches the P4-1 final volume within 1e-12 m3."),
        PackageCheck(check_id="identity_authentication", status="INFO", evidence="Identity matches a separate local synthetic assignment; tenant/platform authorization is NOT_ASSESSED."),
    ]
    return frames, summary, checks


def _read_gate_evidence(context: _R2ASourceContext, result: TriangularResultContract) -> tuple[PackageAuditEvidence, dict[str, bytes]]:
    csv_path, json_path = gate_dispatch_paths(context.native_result_path)
    present_csv = csv_path.exists()
    present_json = json_path.exists()
    raw_files: dict[str, bytes] = {}
    if not present_csv and not present_json:
        return PackageAuditEvidence(
            status="MISSING_INPUT", script_id_binding_status="MISSING",
            note="G1 companion CSV/JSON pair is absent; gate_audit is MISSING_INPUT and this package remains incomplete.",
        ), raw_files
    if present_csv:
        raw_files["audit/gate_dispatch.csv"] = _read_regular_file(csv_path, "G1 gate dispatch CSV")
    if present_json:
        raw_files["audit/gate_dispatch.json"] = _read_regular_file(json_path, "G1 gate dispatch JSON")
    if not (present_csv and present_json):
        return PackageAuditEvidence(
            status="MISSING_INPUT", csv_file_key="audit/gate_dispatch.csv" if present_csv else None,
            json_file_key="audit/gate_dispatch.json" if present_json else None,
            csv_sha256=_sha256_bytes(raw_files["audit/gate_dispatch.csv"]) if present_csv else None,
            json_sha256=_sha256_bytes(raw_files["audit/gate_dispatch.json"]) if present_json else None,
            script_id_binding_status="MISSING",
            note="Only one G1 companion file is present; raw available bytes are preserved and gate_audit remains MISSING_INPUT.",
        ), raw_files
    try:
        pair_report = validate_gate_dispatch_exports(context.native_result_path)
    except (OSError, ValueError) as exc:
        raise RehearsalPackageError(f"G1 CSV/JSON companion pair failed the existing validator: {exc}") from exc
    payload = _strict_json_loads(raw_files["audit/gate_dispatch.json"], "G1 JSON")
    if not isinstance(payload, dict):
        raise RehearsalPackageError("G1 JSON root must be an object")
    if payload.get("time_unit") != "s":
        raise RehearsalPackageError("G1 time_unit must be seconds (s)")
    actual_path = payload.get("result_file")
    if not isinstance(actual_path, str) or Path(actual_path).resolve() != context.native_result_path.resolve():
        raise RehearsalPackageError("G1 JSON result_file must resolve to the original configured native result; paths are never rewritten")
    result_context = payload.get("result_context")
    if not isinstance(result_context, dict):
        raise RehearsalPackageError("G1 JSON must contain the native result_context used for source verification")
    case_id = result_context.get("case_id") or result_context.get("case")
    if case_id is not None and case_id != result.case_id:
        raise RehearsalPackageError("G1 result_context case_id/case differs from the native result case")
    if result_context.get("format") != result.format:
        raise RehearsalPackageError("G1 result_context format differs from the validated native result artifact")
    if payload.get("time_origin") != result.time_origin:
        raise RehearsalPackageError("G1 time_origin differs from the validated native result")
    if payload.get("window_start_s") != float(result.time_s[0]) or payload.get("window_end_s") != float(result.time_s[-1]):
        raise RehearsalPackageError("G1 time window differs from the native result time axis")
    if payload.get("output_sample_count") != len(result.time_s):
        raise RehearsalPackageError("G1 output_sample_count differs from the native result time axis")
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, list) or [item.get("time_s") for item in snapshots if isinstance(item, dict)] != [float(t) for t in result.time_s]:
        raise RehearsalPackageError("G1 snapshot time axis differs from the full native result time axis")
    if len(snapshots) != len(result.time_s) or payload.get("snapshot_count") != len(result.time_s):
        raise RehearsalPackageError("G1 snapshot count differs from the native result time axis")
    script_bindings = [result_context[key] for key in ("script_id", "run_id") if result_context.get(key) is not None]
    if not script_bindings:
        return PackageAuditEvidence(
            status="MISSING_IDENTITY_BINDING", source_result_file_text=actual_path,
            consistency_id=pair_report["consistency_id"], csv_file_key="audit/gate_dispatch.csv",
            json_file_key="audit/gate_dispatch.json", csv_sha256=_sha256_bytes(raw_files["audit/gate_dispatch.csv"]),
            json_sha256=_sha256_bytes(raw_files["audit/gate_dispatch.json"]),
            record_count=int(pair_report["record_count"]), snapshot_count=int(payload.get("snapshot_count", 0)),
            evaluation_count=int(payload.get("evaluation_count", 0)), script_id_binding_status="MISSING",
            note="G1 pair is internally consistent and matches source result path/native format/case/time, but has no script/run binding to the outer context.",
        ), raw_files
    if any(script_id != context.assignment.script_id for script_id in script_bindings):
        raise RehearsalPackageError("G1 result_context script_id/run_id differs from the separate local dispatch assignment")
    return PackageAuditEvidence(
        status="VERIFIED_PAIR", source_result_file_text=actual_path,
        consistency_id=pair_report["consistency_id"], csv_file_key="audit/gate_dispatch.csv",
        json_file_key="audit/gate_dispatch.json", csv_sha256=_sha256_bytes(raw_files["audit/gate_dispatch.csv"]),
        json_sha256=_sha256_bytes(raw_files["audit/gate_dispatch.json"]),
        record_count=int(pair_report["record_count"]), snapshot_count=int(payload.get("snapshot_count", 0)),
        evaluation_count=int(payload.get("evaluation_count", 0)), script_id_binding_status="MATCHED_OUTER_CONTEXT",
        note="Existing G1 pair passed its validator and matches the original result path, native format and full time axis; the P4-1 reader supplies the case identity through that exact native path. Local script context matches; authorization remains NOT_ASSESSED.",
    ), raw_files


def _selector_for_request(item: ProductRequest, context: _R2ASourceContext, result_set_id: str, duration_s: float, frame_index: int | None = None) -> ResultProductSelector:
    common: dict[str, Any] = {
        "result_set_id": result_set_id, "product_code": item.product_code,
        "scope_id": item.scope_id,
        "scope_hash": None if item.product_code == "gate_audit" else context.scope_hash,
        "aggregation": item.aggregation, "metric_policy_version": context.request.metric_policy_version,
    }
    if item.aggregation == "FRAME":
        assert frame_index is not None
        common.update(frame_index=frame_index, time_s=float(context.capability.config.output_times_s[frame_index]))
    elif item.aggregation in {"MAX_OVER_WINDOW", "PEAK_AREA_OVER_WINDOW", "UNION_FOOTPRINT", "FIRST_ARRIVAL", "WET_DURATION", "EVENT_TIMELINE"}:
        common.update(window_start_s=0.0, window_end_s=duration_s)
    elif item.aggregation == "SERIES":
        # R2A does not expose object-scoped series products. This selector is deliberately not fabricated.
        raise RehearsalPackageError("R2A cannot construct an exact object-scoped SERIES selector without a registered object ID")
    return ResultProductSelector.model_validate(common)


def _build_selectors_and_products(
    context: _R2ASourceContext, frames: PackageFrames, summary: PackageSummary,
    result_set_id: str, audit: PackageAuditEvidence,
) -> tuple[dict[str, Any], list[PackageProductStatus]]:
    duration = float(frames.frame_times_s[-1])
    coverage: list[ProductCoverage] = []
    selectors: list[ResultProductSelector] = []
    product_status: list[PackageProductStatus] = []
    profile = _load_r2a_profile()
    requested_codes = {item.product_code for item in context.request.requested_products}
    requests = list(context.request.requested_products)
    if "gate_audit" not in requested_codes:
        # G1 completeness is a frozen R2A gate even when not requested as a display product.
        requests.append(ProductRequest(product_code="gate_audit", required=False, scope_id="AUDIT_RUN_GLOBAL", aggregation="EVENT_TIMELINE"))
    for item in requests:
        supported = item.product_code in _SUPPORTED_PRODUCTS
        if item.product_code == "water_depth" and item.aggregation == "FRAME":
            supported = True
        elif item.product_code == "storage_volume" and item.aggregation in {"FRAME", "MAX_OVER_WINDOW"}:
            supported = True
        elif item.product_code == "maximum_depth" and item.aggregation == "MAX_OVER_WINDOW":
            supported = True
        elif item.product_code == "gate_audit" and item.aggregation == "EVENT_TIMELINE":
            supported = True
        else:
            supported = False
        current_status = "COMPLETE" if supported else "UNSUPPORTED"
        reason = None if supported else "R2A only exports actual depth arrays, area-integrated storage volume, the window maximum depth, and validated G1 audit evidence."
        if item.product_code == "gate_audit" and audit.status != "VERIFIED_PAIR":
            current_status = "MISSING_INPUT"
            reason = audit.note
        if item.product_code == "water_depth" and item.aggregation == "FRAME":
            for frame_index, time_s in enumerate(frames.frame_times_s):
                status = "COMPLETE" if supported else "UNSUPPORTED"
                coverage.append(ProductCoverage(
                    product_code=item.product_code, scope_id=item.scope_id, scope_hash=context.scope_hash,
                    aggregation=item.aggregation, frame_index=frame_index, time_s=float(time_s),
                    metric_policy_version=context.request.metric_policy_version, status=status,
                    expected_count=len(profile.face_id) if supported else None,
                    valid_count=len(profile.face_id) if supported else None,
                ))
                if supported:
                    selectors.append(_selector_for_request(item, context, result_set_id, duration, frame_index))
        elif item.product_code == "storage_volume" and item.aggregation == "FRAME":
            for frame_index, time_s in enumerate(frames.frame_times_s):
                status = "COMPLETE" if supported else "UNSUPPORTED"
                coverage.append(ProductCoverage(
                    product_code=item.product_code, scope_id=item.scope_id, scope_hash=context.scope_hash,
                    aggregation=item.aggregation, frame_index=frame_index, time_s=float(time_s),
                    metric_policy_version=context.request.metric_policy_version, status=status,
                    expected_count=1 if supported else None, valid_count=1 if supported else None,
                ))
                if supported:
                    selectors.append(_selector_for_request(item, context, result_set_id, duration, frame_index))
        elif item.product_code == "gate_audit" and item.aggregation == "EVENT_TIMELINE":
            status = "COMPLETE" if audit.status == "VERIFIED_PAIR" else "MISSING_INPUT"
            coverage.append(ProductCoverage(
                product_code=item.product_code, scope_id="AUDIT_RUN_GLOBAL", scope_hash=None,
                aggregation=item.aggregation, window_start_s=0.0, window_end_s=duration,
                metric_policy_version=context.request.metric_policy_version, status=status,
            ))
            if status == "COMPLETE":
                selectors.append(_selector_for_request(item, context, result_set_id, duration))
        elif item.product_code in {"storage_volume", "maximum_depth"} and item.aggregation == "MAX_OVER_WINDOW":
            status = "COMPLETE" if supported else "UNSUPPORTED"
            coverage.append(ProductCoverage(
                product_code=item.product_code, scope_id=item.scope_id, scope_hash=context.scope_hash,
                aggregation=item.aggregation, window_start_s=0.0, window_end_s=duration,
                metric_policy_version=context.request.metric_policy_version, status=status,
                expected_count=len(profile.face_id) if item.product_code == "maximum_depth" and supported else (1 if supported else None),
                valid_count=len(profile.face_id) if item.product_code == "maximum_depth" and supported else (1 if supported else None),
            ))
            if supported:
                selectors.append(_selector_for_request(item, context, result_set_id, duration))
        if item.product_code == "gate_audit" and not any(p.product_code == "gate_audit" for p in product_status):
            product_status.append(PackageProductStatus(product_code="gate_audit", required=item.required, status=current_status, reason=reason))
        elif item.product_code != "gate_audit":
            product_status.append(PackageProductStatus(product_code=item.product_code, required=item.required, status=current_status, reason=reason))
    manifest = ResultSetManifest(
        result_set_id=result_set_id, metric_policy_version=context.request.metric_policy_version,
        frame_times_s=list(frames.frame_times_s), duration_s=duration,
        scopes=[ScopeDescriptor(scope_id=context.request.scope_id, scope_hash=context.scope_hash)],
        product_coverage=coverage,
    )
    for selector in selectors:
        validation = validate_selector_against_manifest(selector, manifest)
        if not validation.available:
            raise RehearsalPackageError(f"generated selector failed the unchanged R1 selector validator: {selector.product_code}: {validation.reason}")
    return {
        "format": "rehearsal-package-selectors-v1",
        "result_set_manifest": manifest.model_dump(mode="json"),
        "selectors": [item.model_dump(mode="json") for item in selectors],
        "selector_validation": "validated_by_rehearsal_contract.validate_selector_against_manifest",
    }, product_status


def _package_material(context: _R2ASourceContext) -> tuple[RehearsalResultPackageManifest, dict[str, bytes], bool]:
    if context.envelope is None:
        raise RehearsalPackageError("execution envelope is required for R2A package export/validation")
    result, native_raw, native_sha, resolved = _native_contract(context)
    frames, summary, checks = _compute_static_metrics(context, result, resolved)
    audit, audit_files = _read_gate_evidence(context, result)
    result_set_id = "r2a-local-" + _sha256_bytes(_canonical_json({
        "input_sha256": context.input_sha256, "native_result_sha256": native_sha,
        "platform_run_id": context.assignment.platform_run_id, "attempt_id": context.assignment.attempt_id,
        "script_id": context.assignment.script_id,
    }))[:24]
    selectors, product_status = _build_selectors_and_products(context, frames, summary, result_set_id, audit)
    completed_codes = {item.product_code for item in product_status if item.status == "COMPLETE"}
    data_bindings: dict[str, Any] = {}
    if "water_depth" in completed_codes:
        data_bindings["water_depth"] = {
            "artifact_key": "native/result.nc", "source_kind": "NETCDF_VARIABLE",
            "time_variable": "time", "face_id_variable": "face_id", "variable_name": "water_depth", "unit": "m",
        }
    if "storage_volume" in completed_codes:
        storage_request = next(item for item in context.request.requested_products if item.product_code == "storage_volume")
        if storage_request.aggregation == "MAX_OVER_WINDOW":
            data_bindings["storage_volume"] = {
                "artifact_key": "summary.json", "source_kind": "SUMMARY_FIELD",
                "time_field": "maximum_water_volume_time_s", "value_field": "maximum_water_volume_m3",
                "unit": "m3",
            }
        else:
            data_bindings["storage_volume"] = {
                "artifact_key": "frames.json", "source_kind": "FRAME_FIELD",
                "time_field": "time_s", "value_field": "water_volume_m3", "unit": "m3",
            }
    if "maximum_depth" in completed_codes:
        data_bindings["maximum_depth"] = {
            "artifact_key": "summary.json", "source_kind": "SUMMARY_FIELD",
            "time_field": "maximum_depth_time_s", "value_field": "maximum_depth_m",
            "face_id_field": "maximum_depth_face_id", "unit": "m",
        }
    if "gate_audit" in completed_codes:
        data_bindings["gate_audit"] = {
            "artifact_keys": ["audit/gate_dispatch.csv", "audit/gate_dispatch.json"],
            "source_kind": "VERIFIED_G1_PAIR", "consistency_id": audit.consistency_id,
        }
    selectors["data_bindings"] = data_bindings
    package_status = "COMPLETE_OFFLINE_REVIEW" if audit.status == "VERIFIED_PAIR" and all(
        (not item.required) or item.status == "COMPLETE" for item in product_status
    ) else "INCOMPLETE_REVIEW"
    if audit.status == "VERIFIED_PAIR":
        checks.append(PackageCheck(check_id="g1_companion_pair", status="PASS", evidence="Existing G1 CSV/JSON pair is internally consistent and source-bound."))
    else:
        checks.append(PackageCheck(check_id="g1_companion_pair", status="INFO", evidence=audit.note))
    artifacts: dict[str, bytes] = {
        "native/result.nc": native_raw,
        "frames.json": _json_bytes(frames.model_dump(mode="json")),
        "summary.json": _json_bytes(summary.model_dump(mode="json")),
        "selectors.json": _json_bytes(selectors),
        "quality.json": _json_bytes({"format": "rehearsal-package-quality-v1", "checks": [item.model_dump(mode="json") for item in checks]}),
        "identity/execution_envelope.json": _read_regular_file(context.envelope_path, "execution envelope"),
        "identity/dispatch_assignment.json": _read_regular_file(context.assignment_path, "local dispatch assignment"),
        "identity/export_request.yaml": _read_regular_file(context.request_path, "package export request"),
        **audit_files,
    }
    source_graph = PackageSourceGraph(
        validation_basis="LOCAL_SOURCE_GRAPH_V1", request_path=str(context.request_path),
        run_config_path=str(context.run_config_path), business_package_path=str(context.capability.business_path),
        mesh_path=str(context.capability.mesh_path), native_result_path=str(context.native_result_path),
        request_sha256=context.source_hashes["request"], run_config_sha256=context.source_hashes["run_config"],
        business_package_sha256=context.source_hashes["business_package"], mesh_sha256=context.source_hashes["mesh"],
        native_result_sha256=native_sha, topology_fingerprint=resolved.topology_fingerprint,
        terrain_fingerprint=resolved.terrain_fingerprint, source_graph_sha256=context.input_sha256,
        portability="LOCAL_ONLY_REQUIRES_ORIGINAL_SOURCE_GRAPH",
    )
    identity = PackageIdentity(
        tenant_id=context.assignment.tenant_id, project_id=context.assignment.project_id,
        scenario_id=context.assignment.scenario_id, scenario_version_id=context.assignment.scenario_version_id,
        platform_run_id=context.assignment.platform_run_id, attempt_id=context.assignment.attempt_id,
        attempt_no=context.assignment.attempt_no, script_id=context.assignment.script_id,
        local_run_id=context.assignment.local_run_id, model_case_id=context.assignment.model_case_id,
        input_sha256=context.input_sha256,
        execution_envelope_sha256=context.source_hashes["envelope"],
        dispatch_assignment_sha256=context.source_hashes["assignment"],
        identity_validation_status="LOCAL_CONTEXT_MATCHED", identity_authentication_status="NOT_ASSESSED",
        native_identity_basis="OUTER_EXECUTION_CONTEXT_ONLY",
        native_identity_fields_missing=["platform_run_id", "attempt_id", "script_id", "local_run_id"],
    )
    media_types = {
        "native/result.nc": "application/x-netcdf", "frames.json": "application/json",
        "summary.json": "application/json", "selectors.json": "application/json", "quality.json": "application/json",
        "identity/execution_envelope.json": "application/json", "identity/dispatch_assignment.json": "application/json",
        "identity/export_request.yaml": "application/yaml",
        "audit/gate_dispatch.csv": "text/csv", "audit/gate_dispatch.json": "application/json",
    }
    file_records = [PackageFileRecord(key=key, sha256=_sha256_bytes(raw), byte_count=len(raw), media_type=media_types[key]) for key, raw in sorted(artifacts.items())]
    manifest = RehearsalResultPackageManifest(
        format=_PACKAGE_FORMAT, schema_version="1.0", product_type="offline_rehearsal_result_package",
        result_set_id=result_set_id, package_status=package_status, publication_state="OFFLINE_REVIEW_STAGING",
        validation_basis="LOCAL_SOURCE_GRAPH_V1", identity=identity, source_graph=source_graph,
        engine_mode=context.request.engine_mode, fixture_profile_id=resolved.profile.profile_id,
        fixture_profile_version=resolved.profile.profile_version, fixture_profile_sha256=resolved.profile_sha256,
        scope_id=context.request.scope_id, scope_hash=context.scope_hash,
        metric_policy_version=context.request.metric_policy_version, frame_times_s=list(frames.frame_times_s),
        products=product_status, gate_audit=audit,
        native_identity_limit="P4-1 native NetCDF does not carry platform_run_id, attempt_id, script_id or local_run_id; those values are bound only through the separately checked outer execution context.",
        static_reference=summary, quality_checks=checks,
        limitations=[
            "Synthetic-only closed-wall sources=none fixture; not a real flood case or calibrated model.",
            "Identity matching uses a local synthetic dispatch assignment; tenant authorization and platform authenticity are NOT_ASSESSED.",
            "Package is offline review staging only; publication and engineering validation remain false.",
            "Validation is LOCAL_SOURCE_GRAPH_V1 and requires original request, run config, P3-A package, P2 mesh and native result paths.",
            "R2A does not compute Manning/rainfall/open boundaries, wet duration, arrival, exposure, loss, evacuation or 3-D products.",
        ], files=file_records,
    )
    _assert_source_unchanged(context, native_sha)
    complete = package_status == "COMPLETE_OFFLINE_REVIEW"
    return manifest, artifacts, complete


def _walk_package_files(package_dir: Path) -> dict[str, Path]:
    if _is_reparse_point(package_dir) or not package_dir.is_dir():
        raise RehearsalPackageError(f"package directory must exist and must not be a symbolic link or reparse point: {package_dir}")
    files: dict[str, Path] = {}
    for root, dirs, filenames in os.walk(package_dir, followlinks=False):
        root_path = Path(root)
        for directory in dirs:
            if _is_reparse_point(root_path / directory):
                raise RehearsalPackageError(f"package directory must not contain symbolic links or reparse points: {root_path / directory}")
        for filename in filenames:
            path = root_path / filename
            if _is_reparse_point(path) or not path.is_file():
                raise RehearsalPackageError(f"package must contain only regular files: {path}")
            key = path.relative_to(package_dir).as_posix()
            if key in files:
                raise RehearsalPackageError(f"duplicate package file key: {key}")
            files[key] = path
    return files


def _filesystem_identity(path: Path, *, directory: bool) -> tuple[int, int]:
    """Return the device/file identity used to distinguish this export's entries from replacements."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise RehearsalPackageError(f"cannot inspect package transaction path {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise RehearsalPackageError(f"package transaction path must not be a symbolic link or reparse point: {path}")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise RehearsalPackageError(f"package transaction path must be a directory: {path}")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise RehearsalPackageError(f"package transaction path must be a regular file: {path}")
    if not info.st_ino:
        raise RehearsalPackageError(f"filesystem does not expose a stable identity for safe package rollback: {path}")
    return int(info.st_dev), int(info.st_ino)


def _capture_export_tree(stage: Path, expected_files: Mapping[str, bytes]) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    """Capture the exact staged tree before rename so rollback can remove only entries this export created."""
    actual_files = _walk_package_files(stage)
    if set(actual_files) != set(expected_files):
        raise RehearsalPackageError("staged package file set changed before publication")
    file_identities: dict[str, tuple[int, int]] = {}
    for key, expected_bytes in expected_files.items():
        path = actual_files[key]
        if _read_regular_file(path, f"staged package file {key}") != expected_bytes:
            raise RehearsalPackageError(f"staged package file changed before publication: {key}")
        file_identities[key] = _filesystem_identity(path, directory=False)
    directory_identities: dict[str, tuple[int, int]] = {"": _filesystem_identity(stage, directory=True)}
    for root, dirs, _ in os.walk(stage, followlinks=False):
        root_path = Path(root)
        for name in dirs:
            path = root_path / name
            key = path.relative_to(stage).as_posix()
            directory_identities[key] = _filesystem_identity(path, directory=True)
    return file_identities, directory_identities


def _owned_directory_chain_matches(
    destination: Path, relative_directory: str, directory_identities: Mapping[str, tuple[int, int]],
) -> bool:
    parts = PurePosixPath(relative_directory).parts if relative_directory else ()
    prefixes = [""] + ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
    for key in prefixes:
        expected = directory_identities.get(key)
        if expected is None:
            return False
        path = destination if not key else destination.joinpath(*PurePosixPath(key).parts)
        try:
            if _filesystem_identity(path, directory=True) != expected:
                return False
        except RehearsalPackageError:
            return False
    return True


def _write_manifest_recovery_copy(destination: Path, payload: bytes) -> Path:
    """Write modified manifest bytes to a unique, no-clobber recovery file."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(20):
        recovery = destination / f"manifest.recovered.{uuid.uuid4().hex}.json"
        try:
            descriptor = os.open(recovery, flags, 0o600)
        except FileExistsError:
            continue
        created_identity: tuple[int, int] | None = None
        try:
            info = os.fstat(descriptor)
            if not info.st_ino:
                raise RehearsalPackageError(f"filesystem does not expose a stable identity for recovery file: {recovery}")
            created_identity = (int(info.st_dev), int(info.st_ino))
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if _filesystem_identity(recovery, directory=False) != created_identity or _read_regular_file(recovery, "recovered package manifest") != payload:
                raise RehearsalPackageError(f"manifest recovery file changed while being written: {recovery}")
            return recovery
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                if created_identity is not None and _filesystem_identity(recovery, directory=False) == created_identity:
                    recovery.unlink()
            except (OSError, RehearsalPackageError):
                pass
            raise
    raise RehearsalPackageError("could not allocate a unique manifest recovery file without overwriting existing content")


def _retire_export_manifest(
    destination: Path,
    expected_manifest: bytes,
    expected_identity: tuple[int, int],
    directory_identities: Mapping[str, tuple[int, int]],
) -> list[str]:
    """Retire the marker, preserving any concurrent manifest bytes under a unique recovery name."""
    manifest_path = destination / "manifest.json"
    issues: list[str] = []
    for _ in range(3):
        if not _owned_directory_chain_matches(destination, "", directory_identities):
            return issues + [f"left manifest at an unowned/replaced parent path: {manifest_path}"]
        try:
            current_identity = _filesystem_identity(manifest_path, directory=False)
            current_bytes = _read_regular_file(manifest_path, "published package manifest")
        except RehearsalPackageError as exc:
            if manifest_path.exists() or manifest_path.is_symlink():
                return issues + [f"preserved non-regular or unreadable manifest at {manifest_path}: {exc}"]
            return issues

        if current_identity == expected_identity and current_bytes == expected_manifest:
            try:
                if (_owned_directory_chain_matches(destination, "", directory_identities)
                        and _filesystem_identity(manifest_path, directory=False) == current_identity
                        and _read_regular_file(manifest_path, "published package manifest") == current_bytes):
                    manifest_path.unlink()
                    if manifest_path.exists() or manifest_path.is_symlink():
                        return issues + [f"a concurrent writer recreated the success marker at: {manifest_path}"]
                    return issues
            except (OSError, RehearsalPackageError) as exc:
                return issues + [f"could not safely remove transaction-owned manifest {manifest_path}: {exc}"]
            continue

        try:
            recovery = _write_manifest_recovery_copy(destination, current_bytes)
        except (OSError, RehearsalPackageError) as exc:
            return issues + [f"could not preserve modified manifest; left it at {manifest_path}: {exc}"]
        reason = "modified" if current_identity == expected_identity else "replaced"
        issues.append(f"recovered {reason} manifest bytes at: {recovery}")
        try:
            if not _owned_directory_chain_matches(destination, "", directory_identities):
                return issues + [f"left manifest in place because its parent identity changed: {manifest_path}"]
            if _filesystem_identity(manifest_path, directory=False) != current_identity:
                continue
            if _read_regular_file(manifest_path, "published package manifest") != current_bytes:
                continue
            manifest_path.unlink()
            if manifest_path.exists() or manifest_path.is_symlink():
                return issues + [f"a concurrent writer recreated the success marker at: {manifest_path}"]
            return issues
        except (OSError, RehearsalPackageError) as exc:
            return issues + [f"preserved manifest recovery at {recovery}; could not retire marker {manifest_path}: {exc}"]
    return issues + [f"left concurrently changing manifest at: {manifest_path}"]


def _rollback_published_export(
    destination: Path,
    expected_files: Mapping[str, bytes],
    file_identities: Mapping[str, tuple[int, int]],
    directory_identities: Mapping[str, tuple[int, int]],
) -> list[str]:
    """Remove only still-identical transaction-owned files; leave replaced or concurrent entries untouched."""
    issues: list[str] = []
    if not _owned_directory_chain_matches(destination, "", directory_identities):
        return [f"preserved package directory whose filesystem identity changed: {destination}"]

    # Retire the success marker before cleaning data. If another writer changed
    # this transaction-owned file in place or replaces it, save those exact
    # bytes under a unique no-clobber name before unlinking the marker path.
    issues.extend(_retire_export_manifest(
        destination, expected_files["manifest.json"], file_identities["manifest.json"], directory_identities,
    ))

    keys = sorted(key for key in expected_files if key != "manifest.json")
    for key in keys:
        path = destination.joinpath(*PurePosixPath(key).parts)
        parent_key = "/".join(PurePosixPath(key).parts[:-1])
        if not _owned_directory_chain_matches(destination, parent_key, directory_identities):
            issues.append(f"preserved package file beneath a replaced parent directory: {path}")
            continue
        try:
            identity = _filesystem_identity(path, directory=False)
        except RehearsalPackageError:
            if path.exists() or path.is_symlink():
                issues.append(f"preserved replaced or non-regular package path: {path}")
            continue
        if identity != file_identities.get(key):
            issues.append(f"preserved package file with a different filesystem identity: {path}")
            continue
        if key != "manifest.json":
            try:
                if _read_regular_file(path, f"rollback package file {key}") != expected_files[key]:
                    issues.append(f"preserved modified package file: {path}")
                    continue
            except RehearsalPackageError as exc:
                issues.append(str(exc))
                continue
        try:
            path.unlink()
        except OSError as exc:
            issues.append(f"could not remove transaction-owned package file {path}: {exc}")

    # rmdir is deliberately non-recursive: any concurrent file or directory
    # keeps its parent in place and is never removed by rollback.
    directories = sorted(directory_identities, key=lambda key: (key.count("/"), len(key)), reverse=True)
    for key in directories:
        path = destination if not key else destination.joinpath(*PurePosixPath(key).parts)
        if not _owned_directory_chain_matches(destination, key, directory_identities):
            issues.append(f"preserved package directory with a different filesystem identity: {path}")
            continue
        try:
            path.rmdir()
        except OSError:
            # Non-empty directories may now contain files not owned by this export.
            pass
    return issues


def validate_result_package(
    request_path: str | Path, package_directory: str | Path | None = None,
) -> PackageValidationReport:
    """Independently re-read the current source graph, native arrays and every package byte."""
    context = _load_source_context(request_path, require_envelope=True, require_native=True)
    expected_manifest, expected_files, expected_complete = _package_material(context)
    if package_directory is None:
        package_dir = context.output_path
    else:
        package_candidate = Path(os.path.abspath(package_directory))
        if _has_reparse_component(package_candidate):
            raise RehearsalPackageError(f"package directory must not traverse a symbolic link or reparse point: {package_candidate}")
        package_dir = package_candidate.resolve()
    if _has_reparse_component(package_dir):
        raise RehearsalPackageError(f"package directory must not traverse a symbolic link or reparse point: {package_dir}")
    actual_paths = _walk_package_files(package_dir)
    if "manifest.json" not in actual_paths:
        raise RehearsalPackageError("package manifest.json is missing")
    raw_manifest = _read_regular_file(actual_paths["manifest.json"], "package manifest")
    try:
        manifest_model = RehearsalResultPackageManifest.model_validate(_strict_json_loads(raw_manifest, "package manifest"))
    except ValidationError as exc:
        raise RehearsalPackageError(f"package manifest schema validation failed: {exc}") from exc
    expected_keys = set(expected_files) | {"manifest.json"}
    if set(actual_paths) != expected_keys:
        missing = sorted(expected_keys - set(actual_paths))
        extra = sorted(set(actual_paths) - expected_keys)
        raise RehearsalPackageError(f"package file set differs from independently rebuilt source package; missing={missing}, extra={extra}")
    for record in manifest_model.files:
        key = record.key
        if key not in actual_paths:
            raise RehearsalPackageError(f"manifest references missing package file {key}")
        raw = _read_regular_file(actual_paths[key], f"package file {key}")
        if len(raw) != record.byte_count or _sha256_bytes(raw) != record.sha256:
            raise RehearsalPackageError(f"package file byte count/SHA-256 mismatch: {key}")
        if raw != expected_files[key]:
            raise RehearsalPackageError(f"package file does not match independently rebuilt source evidence: {key}")
    if manifest_model.model_dump(mode="json") != expected_manifest.model_dump(mode="json"):
        raise RehearsalPackageError("package manifest differs from independently revalidated source graph, identities, products or metrics")
    quality_raw = _strict_json_loads(actual_paths["quality.json"].read_bytes(), "package quality")
    if not isinstance(quality_raw, dict) or quality_raw.get("format") != "rehearsal-package-quality-v1":
        raise RehearsalPackageError("quality.json has an unsupported format")
    checks = [PackageCheck.model_validate(item) for item in quality_raw.get("checks", [])]
    if checks != expected_manifest.quality_checks:
        raise RehearsalPackageError("quality.json checks differ from source-derived checks")
    report_checks = [
        PackageCheck(check_id="package_manifest_and_files", status="PASS", evidence="Strict manifest schema, exact file set, byte counts and hashes match source-derived evidence."),
        PackageCheck(check_id="source_graph_and_native_result", status="PASS", evidence="Original source graph and native result were freshly re-read; P4-1 reader and static metrics passed."),
        PackageCheck(check_id="selectors_and_product_coverage", status="PASS", evidence="R1 selectors and coverage were rebuilt and each emitted selector passed the R1 validator."),
    ]
    if expected_complete:
        report_checks.append(PackageCheck(check_id="package_completeness", status="PASS", evidence="Required R2A products and G1 audit pair are complete for offline review."))
    else:
        report_checks.append(PackageCheck(check_id="package_completeness", status="INFO", evidence="The package is valid but incomplete; see product statuses and G1 evidence."))
    _assert_source_unchanged(context, expected_manifest.source_graph.native_result_sha256)
    return PackageValidationReport(
        valid=True, complete=expected_complete, package_status=expected_manifest.package_status,
        result_set_id=expected_manifest.result_set_id, checks=report_checks,
        blockers=[] if expected_complete else [
            item.reason or f"{item.product_code} status is {item.status}"
            for item in expected_manifest.products if (item.required and item.status != "COMPLETE")
        ] + ([] if expected_manifest.gate_audit.status == "VERIFIED_PAIR" else ["G1 audit pair is missing or lacks a bound script identity"]),
    )


def export_result_package(request_path: str | Path) -> PackageValidationReport:
    """Write a new offline package directory atomically; never run or modify the solver/source graph."""
    context = _load_source_context(request_path, require_envelope=True, require_native=True)
    destination = context.output_path
    if _has_reparse_component(destination) or destination.exists() or destination.is_symlink():
        raise RehearsalPackageError(f"refusing to overwrite existing or aliased package output directory: {destination}")
    parent = destination.parent
    if not parent.is_dir():
        raise RehearsalPackageError(f"package output parent directory does not exist: {parent}")
    manifest, artifacts, _ = _package_material(context)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=parent))
    manifest_bytes = _json_bytes(manifest.model_dump(mode="json"))
    owned_files = {**artifacts, "manifest.json": manifest_bytes}
    file_identities: dict[str, tuple[int, int]] | None = None
    directory_identities: dict[str, tuple[int, int]] | None = None
    published = False
    try:
        for key, raw in artifacts.items():
            path = stage.joinpath(*PurePosixPath(key).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        (stage / "manifest.json").write_bytes(manifest_bytes)
        # Validate the staged bytes against freshly reread original inputs before exposing the directory.
        validate_result_package(request_path, stage)
        file_identities, directory_identities = _capture_export_tree(stage, owned_files)
        if destination.exists() or destination.is_symlink():
            raise RehearsalPackageError(f"package output directory appeared during export; refusing to replace it: {destination}")
        os.rename(stage, destination)
        published = True
        # Keep the post-rename fresh read inside the same transaction as staging.
        # It detects source changes in the stage/publish gap and reader failures.
        return validate_result_package(request_path, destination)
    except Exception as exc:
        # A rename wrapper or filesystem error can raise after the move has
        # happened. Confirm the published root by its captured identity before
        # deciding which location belongs to this transaction.
        if (not published and file_identities is not None and directory_identities is not None
                and not stage.exists() and destination.exists() and not destination.is_symlink()):
            try:
                published = _filesystem_identity(destination, directory=True) == directory_identities[""]
            except RehearsalPackageError:
                published = False
        rollback_issues: list[str] = []
        if published and file_identities is not None and directory_identities is not None:
            rollback_issues = _rollback_published_export(destination, owned_files, file_identities, directory_identities)
        elif stage.exists() and not stage.is_symlink():
            # The staging directory was created by this call. It is private and
            # has never been installed at the user-visible package path.
            shutil.rmtree(stage, ignore_errors=True)
        if rollback_issues:
            raise RehearsalPackageError(
                f"R2A export failed and rollback preserved concurrently changed content: {exc}; "
                + "; ".join(rollback_issues)
            ) from exc
        raise


def create_local_execution_envelope(request_path: str | Path) -> Path:
    """Create the separately labeled synthetic local identity envelope for this request."""
    context = _load_source_context(request_path, require_envelope=False, require_native=False)
    destination = context.envelope_path
    if destination.exists() or destination.is_symlink():
        raise RehearsalPackageError(f"refusing to overwrite existing execution envelope: {destination}")
    envelope = ExecutionEnvelope(
        format="rehearsal-execution-envelope-v1", tenant_id=context.assignment.tenant_id,
        project_id=context.assignment.project_id, scenario_id=context.assignment.scenario_id,
        scenario_version_id=context.assignment.scenario_version_id, platform_run_id=context.assignment.platform_run_id,
        attempt_id=context.assignment.attempt_id, attempt_no=context.assignment.attempt_no,
        script_id=context.assignment.script_id, local_run_id=context.assignment.local_run_id,
        model_case_id=context.assignment.model_case_id, input_sha256=context.input_sha256,
        engine_mode=context.request.engine_mode, execution_origin="PLATFORM_MODEL",
    )
    raw = _json_bytes(envelope.model_dump(mode="json"))
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists() or destination.is_symlink():
            raise RehearsalPackageError(f"execution envelope appeared during creation; refusing to replace it: {destination}")
        os.rename(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    _assert_source_unchanged(context)
    return destination


def package_schema() -> dict[str, Any]:
    return {
        "format": "rehearsal-result-package-schema-set-v1",
        "request_schema": R2AExportRequest.model_json_schema(),
        "manifest_schema": RehearsalResultPackageManifest.model_json_schema(),
        "frames_schema": PackageFrames.model_json_schema(),
        "summary_schema": PackageSummary.model_json_schema(),
        "validation_report_schema": PackageValidationReport.model_json_schema(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hydrodynamics-rehearsal-package", description="Export and independently validate small offline R2A result packages.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="print the versioned request/package JSON schemas")
    envelope = commands.add_parser("make-local-envelope", help="write a local synthetic execution envelope for a package request")
    envelope.add_argument("--request", required=True)
    export = commands.add_parser("export", help="export an existing native result and current source graph")
    export.add_argument("--request", required=True)
    export.add_argument("--require-complete", action="store_true", help="return exit code 3 for a valid but incomplete package")
    validate = commands.add_parser("validate", help="independently validate an existing package against its original source graph")
    validate.add_argument("--request", required=True)
    validate.add_argument("--package-directory")
    validate.add_argument("--require-complete", action="store_true", help="return exit code 3 for a valid but incomplete package")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "schema":
            print(json.dumps(package_schema(), ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.command == "make-local-envelope":
            path = create_local_execution_envelope(args.request)
            print(json.dumps({"status": "created_local_synthetic_envelope", "path": str(path)}, ensure_ascii=False))
            return 0
        if args.command == "export":
            report = export_result_package(args.request)
        else:
            report = validate_result_package(args.request, args.package_directory)
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, allow_nan=False))
        _, request, _ = _resolve_package_request(args.request)
        if (args.require_complete or request.require_complete) and not report.complete:
            return 3
        return 0 if report.valid else 2
    except (RehearsalPackageError, ValueError, OSError, ValidationError, RuntimeError, ImportError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
