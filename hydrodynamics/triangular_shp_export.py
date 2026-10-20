"""Read-only GIS export for validated native triangular P4 result arrays.

This module never runs a solver. It refuses structured NC, preparation-only
UGRID, source-enabled runs, and results that fail the existing P4 reader.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import tempfile
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator
import yaml

from .config import Number, Text
from .triangular_business import terrain_fingerprint, topology_fingerprint
from .triangular_solver_contract import TriangularRunCapability, TriangularSolverRunConfig, validate_triangular_run_request
from .triangular_solver_netcdf import TriangularResultContract, read_triangular_result
from .security_exclusion import SecurityExclusionEvidence, security_evidence_sha256


class NativeTriangleSHPError(ValueError):
    """Raised for invalid native triangular SHP export requests or packages."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, allow_inf_nan=False)


class NativeTriangleSHPSelector(_StrictModel):
    aggregation: Literal["FRAME", "MAX_OVER_WINDOW"]
    frame_index: StrictInt | None = Field(default=None, ge=0)
    time_s: Number | None = Field(default=None, ge=0.0)
    window_start_s: Number | None = Field(default=None, ge=0.0)
    window_end_s: Number | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def selector_shape_matches_aggregation(self) -> "NativeTriangleSHPSelector":
        if self.aggregation == "FRAME":
            if self.frame_index is None or self.time_s is None or self.window_start_s is not None or self.window_end_s is not None:
                raise ValueError("FRAME selector requires frame_index/time_s and forbids a window")
        else:
            if self.frame_index is not None or self.time_s is not None or self.window_start_s is None or self.window_end_s is None:
                raise ValueError("MAX_OVER_WINDOW selector requires window_start_s/window_end_s and forbids a frame")
            if self.window_end_s <= self.window_start_s:
                raise ValueError("window_end_s must be greater than window_start_s")
        return self


class NativeTriangleSHPExportRequest(_StrictModel):
    format: Literal["native-triangle-result-shp-export-request-v1"]
    schema_version: Literal["1.0"]
    run_config_file: Text
    output_directory: Text
    selector: NativeTriangleSHPSelector
    scope_selection: Literal["FULL_DOMAIN", "MAXIMUM_WET_SURFACE"]
    wet_depth_threshold_m: Number = Field(default=0.0, ge=0.0)
    target_crs: Literal["NATIVE", "EPSG:4490"] = "NATIVE"

    @field_validator("run_config_file", "output_directory")
    @classmethod
    def normalized_relative_path(cls, value: str) -> str:
        win = PureWindowsPath(value)
        posix = PurePosixPath(value.replace("\\", "/"))
        if (not value.strip() or Path(value).is_absolute() or win.is_absolute() or win.drive
                or posix.is_absolute() or any(part in {"", ".", ".."} for part in value.replace("\\", "/").split("/"))):
            raise ValueError("run_config_file/output_directory must be normalized relative paths without traversal")
        return value

    @model_validator(mode="after")
    def distinct_input_and_output(self) -> "NativeTriangleSHPExportRequest":
        if Path(self.run_config_file.replace("\\", "/")).as_posix().casefold() == Path(self.output_directory.replace("\\", "/")).as_posix().casefold():
            raise ValueError("output_directory must differ from run_config_file")
        return self


class NativeSourceTriangleSHPExportRequest(_StrictModel):
    """Version-2 request for a strictly read Manning/rainfall native result."""

    format: Literal["native-triangle-result-shp-export-request-v2"]
    schema_version: Literal["2.0"]
    result_kind: Literal["manning", "rainfall", "manning_and_rainfall"]
    run_config_file: Text
    output_directory: Text
    selector: NativeTriangleSHPSelector
    scope_selection: Literal["FULL_DOMAIN", "MAXIMUM_WET_SURFACE"]
    wet_depth_threshold_m: Number = Field(default=0.0, ge=0.0)
    target_crs: Literal["NATIVE", "EPSG:4490"] = "NATIVE"

    @field_validator("run_config_file", "output_directory")
    @classmethod
    def normalized_relative_path(cls, value: str) -> str:
        win = PureWindowsPath(value)
        posix = PurePosixPath(value.replace("\\", "/"))
        if (not value.strip() or Path(value).is_absolute() or win.is_absolute() or win.drive
                or posix.is_absolute() or any(part in {"", ".", ".."} for part in value.replace("\\", "/").split("/"))):
            raise ValueError("run_config_file/output_directory must be normalized relative paths without traversal")
        return value

    @model_validator(mode="after")
    def distinct_input_and_output(self) -> "NativeSourceTriangleSHPExportRequest":
        if Path(self.run_config_file.replace("\\", "/")).as_posix().casefold() == Path(self.output_directory.replace("\\", "/")).as_posix().casefold():
            raise ValueError("output_directory must differ from run_config_file")
        return self


class NativeTriangleSHPFile(_StrictModel):
    key: Text
    sha256: Text
    byte_count: StrictInt = Field(ge=0)
    media_type: Text

    @field_validator("key")
    @classmethod
    def safe_relative_key(cls, value: str) -> str:
        if ("\\" in value or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
                or any(part in {"", ".", ".."} for part in value.split("/"))):
            raise ValueError("file key must be a normalized relative path without traversal")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value


class NativeTriangleSHPField(_StrictModel):
    dbf_name: Text
    semantic_name: Text
    dbf_type: Literal["C", "N"]
    unit: Text | None
    meaning: Text


class NativeTriangleSHPManifest(_StrictModel):
    format: Literal["native-triangular-result-shapefile-v1"]
    schema_version: Literal["1.0"]
    package_type: Literal["native_triangular_result_shapefile"]
    result_set_id: Text
    product_type: Literal["experimental_native_triangular_p4_result"]
    experimental: Literal[True]
    engineering_validated: Literal[False]
    production_approved: Literal[False]
    can_publish: Literal[False]
    validation_basis: Literal["P4_1_NATIVE_RESULT_READER_AND_SOURCE_GRAPH"]
    engine_mode: Literal["triangular_closed_experimental"]
    grid_topology: Literal["native_triangular_mesh"]
    case_id: Text
    mesh_id: Text
    topology_fingerprint: Text
    terrain_fingerprint: Text
    input_graph_sha256: Text
    source_coordinate_system: Text
    target_crs: Text
    vertical_datum: Text
    scope_selection: Literal["FULL_DOMAIN", "MAXIMUM_WET_SURFACE"]
    scope_id: Literal["COMPUTATIONAL_DOMAIN", "MAXIMUM_WET_SURFACE"]
    scope_hash: Text
    scope_area_m2: Number = Field(ge=0.0)
    wet_depth_threshold_m: Number = Field(ge=0.0)
    selector: NativeTriangleSHPSelector
    frame_times_s: list[Number] = Field(min_length=2)
    selected_frame_indices: list[StrictInt] = Field(min_length=1)
    native_face_count: StrictInt = Field(ge=1)
    feature_count: StrictInt = Field(ge=0)
    native_face_id_sha256: Text
    security_exclusion_sha256: Text | None = None
    security_active_face_id_sha256: Text | None = None
    security_active_domain_area_m2: Number | None = Field(default=None, ge=0.0)
    security_excluded_area_m2: Number | None = Field(default=None, ge=0.0)
    exported_face_id_sha256: Text
    run_config_sha256: Text
    business_package_sha256: Text
    mesh_sha256: Text
    native_result_sha256: Text
    request_sha256: Text
    run_config_path: Text
    business_package_path: Text
    mesh_path: Text
    native_result_path: Text
    request_path: Text
    native_identity_limit: Text
    face_id_encoding: Literal["exact_decimal_text"]
    area_rule: Literal["native_projected_face_area_m2"]
    window_endpoints_inclusive: Literal[True]
    maximum_tie_policy: Literal["EARLIEST_NATIVE_TIME"]
    velocity_component_frame: Text
    geometry_type: Literal["POLYGON_WITH_THREE_DISTINCT_VERTICES"]
    layer_key: Literal["faces.shp"]
    field_mapping: list[NativeTriangleSHPField] = Field(min_length=1)
    files: list[NativeTriangleSHPFile] = Field(min_length=5)
    limitations: list[Text] = Field(min_length=1)

    @field_validator("topology_fingerprint", "terrain_fingerprint", "scope_hash", "native_face_id_sha256",
                     "input_graph_sha256", "exported_face_id_sha256", "run_config_sha256", "business_package_sha256", "mesh_sha256",
                     "native_result_sha256", "request_sha256", "security_exclusion_sha256", "security_active_face_id_sha256")
    @classmethod
    def digest_fields_are_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("identity digests must be lowercase SHA-256 values")
        return value

    @model_validator(mode="after")
    def unique_files_and_time_selection(self) -> "NativeTriangleSHPManifest":
        keys = [item.key for item in self.files]
        if len(keys) != len(set(keys)) or "manifest.json" in keys:
            raise ValueError("SHP package file keys must be unique and must not hash manifest.json itself")
        if len(set(self.selected_frame_indices)) != len(self.selected_frame_indices):
            raise ValueError("selected_frame_indices must be unique")
        if any(index >= len(self.frame_times_s) for index in self.selected_frame_indices):
            raise ValueError("selected_frame_indices must lie on the native time axis")
        security_fields = (
            self.security_exclusion_sha256, self.security_active_face_id_sha256,
            self.security_active_domain_area_m2, self.security_excluded_area_m2,
        )
        if all(value is None for value in security_fields):
            pass
        elif any(value is None for value in security_fields):
            raise ValueError("security manifest identity, active face set and area values must appear together")
        elif self.security_active_face_id_sha256 != self.native_face_id_sha256:
            raise ValueError("security active face_id digest must match the full native P4 face set")
        return self


class NativeTriangleSHPSourceArtifact(_StrictModel):
    role: Literal[
        "request", "top_level_run_config", "base_run_config", "source_config",
        "source_preparation", "p3_business", "p2_mesh", "native_result",
        "security_input_config", "security_geometry", "security_shx", "security_prj",
        "security_domain",
    ]
    path: Text
    sha256: Text
    byte_count: StrictInt = Field(ge=0)

    @field_validator("sha256")
    @classmethod
    def valid_source_digest(cls, value: str) -> str:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("source graph sha256 must be a lowercase SHA-256 digest")
        return value


class NativeTriangleSHPFrameBudget(_StrictModel):
    frame_index: StrictInt = Field(ge=0)
    time_s: Number = Field(ge=0.0)
    integrated_volume_m3: Number = Field(ge=0.0)
    ledger_initial_volume_m3: Number = Field(ge=0.0)
    ledger_boundary_net_volume_m3: Number
    ledger_source_net_volume_m3: Number
    ledger_correction_volume_m3: Number = Field(ge=0.0)
    ledger_final_volume_m3: Number = Field(ge=0.0)
    ledger_residual_m3: Number


class NativeTriangleSHPSourcesSummary(_StrictModel):
    result_kind: Literal["manning", "rainfall", "manning_and_rainfall"]
    sources_applied: Literal["manning", "rainfall", "manning_and_rainfall"]
    manning_enabled: StrictBool
    manning_applied: StrictBool
    nonzero_manning_effect_applied: StrictBool
    manning_source_unit: Literal["1"] | None
    manning_physical_unit: Literal["s/m^(1/3)"] | None
    manning_interpretation_policy: Literal["legacy_unit1_as_si_manning"] | None
    manning_interpretation_rationale: Text | None
    manning_face_count: StrictInt = Field(ge=0)
    manning_face_id_sha256: Text | None
    manning_n_min: Number | None = Field(default=None, gt=0.0, le=1.0)
    manning_n_max: Number | None = Field(default=None, gt=0.0, le=1.0)
    source_cfl_fraction: Number | None = Field(default=None, gt=0.0, le=1.0)
    manning_nonzero_face_stage_evaluation_count: StrictInt = Field(ge=0)
    manning_maximum_gamma_per_s: Number = Field(ge=0.0)
    manning_maximum_source_magnitude_m2_s2: Number = Field(ge=0.0)
    manning_source_cfl_limited_step_count: StrictInt = Field(ge=0)
    manning_source_cfl_rejected_step_count: StrictInt = Field(ge=0)
    rainfall_enabled: StrictBool
    rainfall_applied: StrictBool
    rainfall_nonzero_effect_applied: StrictBool
    rainfall_input_unit: Literal["mm/h", "m/s"] | None
    rainfall_stage_evaluation_count: StrictInt = Field(ge=0)
    rainfall_nonzero_source_stage_evaluation_count: StrictInt = Field(ge=0)
    rainfall_rate_m_s_by_interval: list[Number]
    rainfall_accepted_active_volume_m3_by_interval: list[Number]
    rainfall_accepted_active_volume_m3: Number = Field(ge=0.0)
    rainfall_total_depth_m: Number = Field(ge=0.0)
    rainfall_total_volume_m3: Number = Field(ge=0.0)
    rainfall_excluded_unmodeled_volume_m3_by_interval: list[Number]
    rainfall_excluded_unmodeled_volume_m3: Number = Field(ge=0.0)
    excluded_rain_is_accepted_budget_loss: Literal[False] = False
    automatic_flow_transfer_from_excluded_area: Literal[False] = False

    @field_validator("manning_face_id_sha256")
    @classmethod
    def manning_face_digest(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)):
            raise ValueError("manning_face_id_sha256 must be a lowercase SHA-256 digest")
        return value


class NativeSourceTriangleSHPManifest(NativeTriangleSHPManifest):
    """Independent version-2 manifest; the version-1 manifest remains unchanged."""

    format: Literal["native-triangular-result-shapefile-v2"]
    schema_version: Literal["2.0"]
    package_type: Literal["native_triangular_source_result_shapefile"]
    product_type: Literal["experimental_native_triangular_p4_2_source_result"]
    validation_basis: Literal["P4_2_NATIVE_SOURCE_READER_AND_SOURCE_GRAPH"]
    engine_mode: Literal[
        "triangular_manning_experimental", "triangular_rainfall_experimental",
        "triangular_manning_and_rainfall_experimental",
    ]
    result_kind: Literal["manning", "rainfall", "manning_and_rainfall"]
    native_format: Literal["triangular-manning-result-v1", "triangular-source-result-v1"]
    native_product_type: Literal[
        "experimental_triangular_manning_shallow_water_result",
        "experimental_triangular_source_shallow_water_result",
    ]
    solver_version: Text
    sources_applied: Literal["manning", "rainfall", "manning_and_rainfall"]
    manning_enabled: StrictBool
    manning_applied: StrictBool
    rainfall_enabled: StrictBool
    rainfall_applied: StrictBool
    nonzero_manning_effect_applied: StrictBool
    rainfall_nonzero_effect_applied: StrictBool
    source_graph_sha256: Text
    source_graph: list[NativeTriangleSHPSourceArtifact] = Field(min_length=8)
    source_summary: NativeTriangleSHPSourcesSummary
    frame_budgets: list[NativeTriangleSHPFrameBudget] = Field(min_length=2)
    maximum_synchronous_water_volume_m3: Number = Field(ge=0.0)
    selected_window_maximum_synchronous_water_volume_m3: Number = Field(ge=0.0)
    security_exclusion_evidence: SecurityExclusionEvidence | None = None

    @field_validator("source_graph_sha256")
    @classmethod
    def source_manifest_hashes(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)):
            raise ValueError("source manifest hashes must be lowercase SHA-256 digests")
        return value

    @model_validator(mode="after")
    def source_identity_and_budget_are_consistent(self) -> "NativeSourceTriangleSHPManifest":
        expected_manning = self.result_kind in {"manning", "manning_and_rainfall"}
        expected_rainfall = self.result_kind in {"rainfall", "manning_and_rainfall"}
        if (self.sources_applied != self.result_kind or self.source_summary.result_kind != self.result_kind
                or self.source_summary.sources_applied != self.result_kind):
            raise ValueError("source manifest result_kind and sources_applied must match")
        if ((self.manning_enabled, self.manning_applied) != (expected_manning, expected_manning)
                or (self.rainfall_enabled, self.rainfall_applied) != (expected_rainfall, expected_rainfall)):
            raise ValueError("source manifest enabled/applied flags must match result_kind")
        if (self.source_summary.manning_enabled, self.source_summary.manning_applied) != (expected_manning, expected_manning):
            raise ValueError("source summary Manning flags must match result_kind")
        if (self.source_summary.rainfall_enabled, self.source_summary.rainfall_applied) != (expected_rainfall, expected_rainfall):
            raise ValueError("source summary rainfall flags must match result_kind")
        if len(self.frame_budgets) != len(self.frame_times_s):
            raise ValueError("frame_budgets must contain one row per native saved time")
        if any(row.frame_index != index or row.time_s != self.frame_times_s[index]
               for index, row in enumerate(self.frame_budgets)):
            raise ValueError("frame_budgets must be in exact native saved-time order")
        if any(not math.isclose(row.integrated_volume_m3, row.ledger_final_volume_m3, rel_tol=1e-12, abs_tol=1e-12)
               for row in self.frame_budgets):
            raise ValueError("integrated frame volumes must match native mass-budget final_volume_m3")
        if any(not math.isclose(
            row.ledger_residual_m3,
            row.ledger_final_volume_m3 - row.ledger_initial_volume_m3 - row.ledger_boundary_net_volume_m3
            - row.ledger_source_net_volume_m3 - row.ledger_correction_volume_m3,
            rel_tol=1e-12, abs_tol=1e-12,
        ) for row in self.frame_budgets):
            raise ValueError("frame budget rows must preserve the native mass-ledger equation")
        frame_volumes = [row.integrated_volume_m3 for row in self.frame_budgets]
        if not math.isclose(self.maximum_synchronous_water_volume_m3, max(frame_volumes), rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("maximum_synchronous_water_volume_m3 must be the maximum of saved-frame integrals")
        selected_volume = max(frame_volumes[index] for index in self.selected_frame_indices)
        if not math.isclose(self.selected_window_maximum_synchronous_water_volume_m3, selected_volume, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("selected-window water volume must use only selected saved frames")
        required_roles = {
            "request", "top_level_run_config", "base_run_config", "source_config", "source_preparation",
            "p3_business", "p2_mesh", "native_result",
        }
        roles = [item.role for item in self.source_graph]
        if len(roles) != len(set(roles)) or not required_roles.issubset(roles):
            raise ValueError("source_graph must contain each required role exactly once")
        graph_sha = _canonical_sha256([item.model_dump(mode="json") for item in self.source_graph])
        if graph_sha != self.source_graph_sha256:
            raise ValueError("source_graph_sha256 does not match the embedded source graph")
        source_result = next(item for item in self.source_graph if item.role == "native_result")
        if source_result.sha256 != self.native_result_sha256 or source_result.path != self.native_result_path:
            raise ValueError("native_result source-graph entry must match native_result identity fields")
        if self.security_exclusion_evidence is None:
            if self.security_exclusion_sha256 is not None or self.security_active_face_id_sha256 is not None:
                raise ValueError("security identity fields require security_exclusion_evidence")
        else:
            evidence = self.security_exclusion_evidence
            if (self.security_exclusion_sha256 != security_evidence_sha256(evidence)
                    or self.security_active_face_id_sha256 != evidence.active_face_id_sha256
                    or evidence.active_face_id_sha256 != self.native_face_id_sha256
                    or evidence.mesh_face_count != self.native_face_count):
                raise ValueError("security evidence must match the native active face set and manifest digest")
        if expected_rainfall:
            interval_count = len(self.frame_times_s) - 1
            if (len(self.source_summary.rainfall_rate_m_s_by_interval) != interval_count
                    or len(self.source_summary.rainfall_accepted_active_volume_m3_by_interval) != interval_count
                    or len(self.source_summary.rainfall_excluded_unmodeled_volume_m3_by_interval) != interval_count):
                raise ValueError("rainfall source summary arrays must align with native saved-time intervals")
        elif self.source_summary.rainfall_rate_m_s_by_interval or self.source_summary.rainfall_accepted_active_volume_m3_by_interval:
            raise ValueError("Manning-only manifest may not contain rainfall intervals")
        return self


class NativeTriangleSHPValidationReport(_StrictModel):
    valid: StrictBool
    complete: StrictBool
    can_publish: Literal[False] = False
    result_set_id: Text
    feature_count: StrictInt = Field(ge=0)
    checks: list[Text]


@dataclass(frozen=True)
class _SHPContext:
    request_path: Path
    request_bytes: bytes
    request: NativeTriangleSHPExportRequest
    run_config_path: Path
    run_config_bytes: bytes
    capability: TriangularRunCapability
    result: TriangularResultContract
    native_result_path: Path
    native_result_bytes: bytes
    source_hashes: dict[str, str]
    source_paths: dict[str, Path]


@dataclass(frozen=True)
class _NativeSourceSHPContext:
    request_path: Path
    request_bytes: bytes
    request: NativeSourceTriangleSHPExportRequest
    run_config_path: Path
    run_config_bytes: bytes
    capability: TriangularRunCapability
    source_capability: Any
    result: TriangularResultContract
    native_result_path: Path
    native_result_bytes: bytes
    source_hashes: dict[str, str]
    source_paths: dict[str, Path]
    source_graph: tuple[NativeTriangleSHPSourceArtifact, ...]


@dataclass(frozen=True)
class _SHPFeature:
    face_id: int
    polygon_points: tuple[tuple[float, float], ...]
    record_values: tuple[Any, ...]
    native_area_m2: float


_SHP_REQUEST_FORMAT = "native-triangle-result-shp-export-request-v1"
_SHP_FORMAT = "native-triangular-result-shapefile-v1"
_SOURCE_SHP_REQUEST_FORMAT = "native-triangle-result-shp-export-request-v2"
_SOURCE_SHP_FORMAT = "native-triangular-result-shapefile-v2"
_DBF_DECIMALS = 12
_FRAME_FIELDS = (
    ("face_id", "C", 32, 0), ("time_s", "N", 20, _DBF_DECIMALS),
    ("depth_m", "N", 20, _DBF_DECIMALS), ("water_lvl", "N", 20, _DBF_DECIMALS),
    ("ux_m_s", "N", 20, _DBF_DECIMALS), ("uy_m_s", "N", 20, _DBF_DECIMALS),
    ("area_m2", "N", 20, _DBF_DECIMALS),
)
_MAX_FIELDS = (
    ("face_id", "C", 32, 0), ("max_depth", "N", 20, _DBF_DECIMALS),
    ("max_time", "N", 20, _DBF_DECIMALS), ("area_m2", "N", 20, _DBF_DECIMALS),
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _sha256_bytes(raw)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _strict_json(raw: bytes, label: str) -> Any:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise ValueError(f"duplicate key {key!r}")
            output[key] = value
        return output

    def no_nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value!r}")

    try:
        return json.loads(raw, object_pairs_hook=no_duplicate_keys, parse_constant=no_nonfinite)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise NativeTriangleSHPError(f"{label} is not strict JSON: {exc}") from exc


def _strict_yaml(raw: bytes, label: str) -> Any:
    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
            self.flatten_mapping(node)
            output: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in output:
                    raise ValueError(f"duplicate YAML key {key!r}")
                output[key] = self.construct_object(value_node, deep=deep)
            return output

    try:
        return yaml.load(raw.decode("utf-8"), Loader=UniqueLoader)
    except (UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        raise NativeTriangleSHPError(f"{label} is not strict YAML: {exc}") from exc


def _is_reparse_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    for part in absolute.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NativeTriangleSHPError(f"cannot inspect path component {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & reparse_flag:
            return True
    return False


def _regular_bytes(path: Path, label: str) -> bytes:
    if _is_reparse_component(path):
        raise NativeTriangleSHPError(f"{label} path must not traverse a symlink or reparse point: {path}")
    if not path.is_file():
        raise NativeTriangleSHPError(f"{label} must be an existing regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise NativeTriangleSHPError(f"cannot read {label} {path}: {exc}") from exc


def _resolve_relative_file(base: Path, value: str, label: str) -> Path:
    candidate = base / value
    if _is_reparse_component(candidate):
        raise NativeTriangleSHPError(f"{label} path must not traverse a symlink or reparse point: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise NativeTriangleSHPError(f"{label} is not an existing file: {resolved}")
    return resolved


def _output_path(request_path: Path, relative: str) -> Path:
    parent = request_path.parent.resolve()
    candidate = Path(os.path.abspath(parent / relative))
    if _is_reparse_component(candidate):
        raise NativeTriangleSHPError(f"output_directory must not traverse a symlink or reparse point: {candidate}")
    normalized = candidate.resolve(strict=False)
    try:
        relative_child = normalized.relative_to(parent)
    except ValueError as exc:
        raise NativeTriangleSHPError("normalized output_directory must stay under the request directory") from exc
    if not relative_child.parts or any(part in {"", ".", ".."} for part in relative_child.parts):
        raise NativeTriangleSHPError("normalized output_directory must be a new child under the request directory")
    return normalized


def _load_request(request_path: str | Path) -> tuple[Path, NativeTriangleSHPExportRequest, bytes]:
    candidate = Path(os.path.abspath(request_path))
    if _is_reparse_component(candidate):
        raise NativeTriangleSHPError(f"request path must not traverse a symlink or reparse point: {candidate}")
    resolved = candidate.resolve()
    raw = _regular_bytes(resolved, "SHP export request")
    try:
        request = NativeTriangleSHPExportRequest.model_validate(_strict_yaml(raw, "SHP export request"))
    except ValidationError as exc:
        raise NativeTriangleSHPError(f"invalid native triangle SHP request: {exc}") from exc
    if request.format != _SHP_REQUEST_FORMAT:
        raise NativeTriangleSHPError(f"request format must be {_SHP_REQUEST_FORMAT}")
    return resolved, request, raw


def _read_result_context(request_path: str | Path) -> _SHPContext:
    request_file, request, request_raw = _load_request(request_path)
    run_config_path = _resolve_relative_file(request_file.parent, request.run_config_file, "run_config_file")
    run_config_bytes = _regular_bytes(run_config_path, "run config")
    try:
        declared_run_config = TriangularSolverRunConfig.model_validate(_strict_yaml(run_config_bytes, "triangular run config"))
    except ValidationError as exc:
        raise NativeTriangleSHPError(f"triangular run config failed strict contract validation: {exc}") from exc
    try:
        capability = validate_triangular_run_request(run_config_path, allow_existing_result=True)
    except (ValueError, OSError, ImportError) as exc:
        raise NativeTriangleSHPError(f"run config/P2/P3-A source graph is invalid: {exc}") from exc
    if capability.config != declared_run_config:
        raise NativeTriangleSHPError("run config changed while it was being validated")
    if capability.config.sources != "none" or capability.config.boundary_policy != "closed_wall" or capability.config.initial_momentum != "zero":
        raise NativeTriangleSHPError("this SHP adapter supports only sources=none, closed_wall, initial_momentum=zero")
    if capability.business.boundary_bindings:
        raise NativeTriangleSHPError("this SHP adapter does not support P3-A open-boundary bindings")
    source_paths = {
        "request": request_file, "run_config": run_config_path,
        "business_package": capability.business_path, "mesh": capability.mesh_path,
        "native_result": capability.result_path,
    }
    business_bytes = _regular_bytes(capability.business_path, "P3-A business package")
    _strict_json(business_bytes, "P3-A business package")
    source_bytes = {
        "request": request_raw,
        "run_config": run_config_bytes,
        "business_package": business_bytes,
        "mesh": _regular_bytes(capability.mesh_path, "P2 mesh"),
        "native_result": _regular_bytes(capability.result_path, "native P4 result"),
    }
    try:
        result = read_triangular_result(capability.result_path, run_config_path, enforce_configured_result_path=True)
    except Exception as exc:
        raise NativeTriangleSHPError(f"native P4 result failed the existing strict reader/source binding: {exc}") from exc
    if (result.sources_applied != "none" or result.boundary_policy != "closed_wall"
            or result.gate_hydraulics_enabled or result.manning_enabled or result.rainfall_enabled):
        raise NativeTriangleSHPError("native result is outside the closed-wall sources=none SHP scope")
    if result.case_id != capability.config.case_id:
        raise NativeTriangleSHPError("native result case_id differs from the validated run config")
    if not capability.business.terrain.vertical_datum:
        raise NativeTriangleSHPError("FRAME SHP requires the native P3-A vertical datum for water_level_m")
    for name, path in source_paths.items():
        if _sha256_bytes(_regular_bytes(path, f"source {name}")) != _sha256_bytes(source_bytes[name]):
            raise NativeTriangleSHPError(f"source graph changed while reading native result: {path}")
    hashes = {name: _sha256_bytes(raw) for name, raw in source_bytes.items()}
    return _SHPContext(
        request_path=request_file, request_bytes=request_raw, request=request,
        run_config_path=run_config_path, run_config_bytes=source_bytes["run_config"],
        capability=capability, result=result, native_result_path=capability.result_path,
        native_result_bytes=source_bytes["native_result"], source_hashes=hashes, source_paths=source_paths,
    )


def _load_source_request(
    request_path: str | Path,
) -> tuple[Path, NativeSourceTriangleSHPExportRequest, bytes]:
    candidate = Path(os.path.abspath(request_path))
    if _is_reparse_component(candidate):
        raise NativeTriangleSHPError(f"request path must not traverse a symlink or reparse point: {candidate}")
    resolved = candidate.resolve()
    raw = _regular_bytes(resolved, "native source SHP export request")
    try:
        request = NativeSourceTriangleSHPExportRequest.model_validate(_strict_yaml(raw, "native source SHP export request"))
    except ValidationError as exc:
        raise NativeTriangleSHPError(f"invalid native source triangle SHP request: {exc}") from exc
    if request.format != _SOURCE_SHP_REQUEST_FORMAT:
        raise NativeTriangleSHPError(f"request format must be {_SOURCE_SHP_REQUEST_FORMAT}")
    return resolved, request, raw


def _read_native_source_context(request_path: str | Path) -> _NativeSourceSHPContext:
    """Read only an existing Manning/rainfall native result and its strict source graph."""
    request_file, request, request_raw = _load_source_request(request_path)
    run_config_path = _resolve_relative_file(request_file.parent, request.run_config_file, "run_config_file")
    run_config_bytes = _regular_bytes(run_config_path, "native source run config")
    run_config_data = _strict_yaml(run_config_bytes, "native source run config")
    if not isinstance(run_config_data, dict):
        raise NativeTriangleSHPError("v2 run_config_file must be a strict YAML mapping")

    try:
        if request.result_kind == "manning":
            from .triangular_manning_contract import (
                load_triangular_manning_run_config,
                validate_triangular_manning_run_request,
            )
            from .triangular_manning_netcdf import read_triangular_manning_result

            if run_config_data.get("sources") != "manning":
                raise NativeTriangleSHPError("result_kind manning requires a top-level sources: manning config")
            declared = load_triangular_manning_run_config(run_config_path)
            if declared.format != "triangular-manning-run-v1":
                raise NativeTriangleSHPError("result_kind manning requires triangular-manning-run-v1")
            declared_base_path = _resolve_relative_file(run_config_path.parent, declared.base_run_config_file, "base_run_config_file")
            declared_source_path = _resolve_relative_file(run_config_path.parent, declared.source_config_file, "source_config_file")
            declared_result_path = _resolve_relative_file(run_config_path.parent, declared.result_file, "result_file")
            source_capability = validate_triangular_manning_run_request(run_config_path, allow_existing_result=True)
            if (source_capability.config != declared or declared_base_path != source_capability.base_run_config_path
                    or declared_source_path != source_capability.source_config_path
                    or declared_result_path != source_capability.result_path):
                raise NativeTriangleSHPError("Manning run config changed while it was being validated")
            base_capability = source_capability.base_run
            result = None
            native_result_path = source_capability.result_path
            reader = lambda: read_triangular_manning_result(
                native_result_path, run_config_path, enforce_configured_result_path=True,
            )
        else:
            from .triangular_source_contract import (
                load_triangular_source_run_config,
                validate_triangular_source_run_request,
            )
            from .triangular_source_netcdf import read_triangular_source_result

            if run_config_data.get("sources") != request.result_kind:
                raise NativeTriangleSHPError(
                    f"result_kind {request.result_kind} requires a matching top-level sources value"
                )
            declared = load_triangular_source_run_config(run_config_path)
            if declared.format != "triangular-source-run-v1":
                raise NativeTriangleSHPError("rainfall/combined result_kind requires triangular-source-run-v1")
            declared_base_path = _resolve_relative_file(run_config_path.parent, declared.base_run_config_file, "base_run_config_file")
            declared_source_path = _resolve_relative_file(run_config_path.parent, declared.source_config_file, "source_config_file")
            declared_result_path = _resolve_relative_file(run_config_path.parent, declared.result_file, "result_file")
            source_capability = validate_triangular_source_run_request(run_config_path, allow_existing_result=True)
            if (source_capability.config != declared or declared_base_path != source_capability.base_run_config_path
                    or declared_source_path != source_capability.source_config_path
                    or declared_result_path != source_capability.result_path):
                raise NativeTriangleSHPError("source run config changed while it was being validated")
            base_capability = source_capability.base_run
            result = None
            native_result_path = source_capability.result_path
            reader = lambda: read_triangular_source_result(
                native_result_path, run_config_path, enforce_configured_result_path=True,
            )
    except NativeTriangleSHPError:
        raise
    except (ValueError, OSError, ImportError) as exc:
        raise NativeTriangleSHPError(f"native source run/P2/P3-A graph failed strict validation: {exc}") from exc

    if base_capability.business.boundary_bindings:
        raise NativeTriangleSHPError("native source SHP does not support P3-A open-boundary bindings")

    # Snapshot the declared graph before the existing strict native reader runs;
    # it validates the embedded P2/P3/source preparation again without reading
    # or requiring the base-none result_file.
    graph_paths: dict[str, Path] = {}
    graph_bytes: dict[str, bytes] = {}

    def add_source(role: str, path: str | Path, label: str, expected_sha256: str | None = None) -> None:
        source = Path(path).resolve()
        raw = _regular_bytes(source, label)
        digest = _sha256_bytes(raw)
        if expected_sha256 is not None and digest != expected_sha256:
            raise NativeTriangleSHPError(f"{label} SHA-256 differs from the freshly validated native evidence")
        if role in graph_paths:
            raise NativeTriangleSHPError(f"duplicate source graph role {role}")
        graph_paths[role], graph_bytes[role] = source, raw

    base_config_data = _strict_yaml(
        _regular_bytes(source_capability.base_run_config_path, "base run config"), "base run config",
    )
    if not isinstance(base_config_data, dict) or not isinstance(base_config_data.get("business_file"), str):
        raise NativeTriangleSHPError("base run config must declare a relative P3 business_file")
    business_path = _resolve_relative_file(
        source_capability.base_run_config_path.parent, base_config_data["business_file"], "business_file",
    )
    business_data = _strict_json(_regular_bytes(business_path, "P3 business package"), "P3 business package")
    if not isinstance(business_data, dict) or not isinstance(business_data.get("mesh_artifact"), str):
        raise NativeTriangleSHPError("P3 business package must declare a relative P2 mesh_artifact")
    mesh_path = _resolve_relative_file(business_path.parent, business_data["mesh_artifact"], "mesh_artifact")
    source_preparation_path = _resolve_relative_file(
        source_capability.source_config_path.parent,
        source_capability.source_config.output_file,
        "source-preparation output_file",
    )
    if (business_path != base_capability.business_path or mesh_path != base_capability.mesh_path
            or source_preparation_path != source_capability.source_preparation_path):
        raise NativeTriangleSHPError("freshly resolved P2/P3/source-preparation paths differ from the validated capabilities")

    add_source("request", request_file, "v2 export request")
    add_source("top_level_run_config", run_config_path, "top-level source run config")
    add_source("base_run_config", source_capability.base_run_config_path, "base run config")
    add_source("source_config", source_capability.source_config_path, "source-preparation config")
    add_source("source_preparation", source_preparation_path, "source-preparation artifact")
    add_source("p3_business", business_path, "P3 business package")
    add_source("p2_mesh", mesh_path, "P2 mesh/terrain artifact")
    add_source("native_result", native_result_path, "native source result")

    evidence = base_capability.business.security_exclusion
    if evidence is not None:
        security_config_path = Path(evidence.security_input_config_path).resolve()
        security_config_data = _strict_yaml(
            _regular_bytes(security_config_path, "security input config"), "security input config",
        )
        security_policy = security_config_data.get("security_exclusion") if isinstance(security_config_data, dict) else None
        if not isinstance(security_policy, dict) or not isinstance(security_policy.get("source_file"), str):
            raise NativeTriangleSHPError("security input config does not contain a strict relative source_file")
        security_geometry_path = _resolve_relative_file(
            security_config_path.parent, security_policy["source_file"], "security geometry source_file",
        )
        if security_geometry_path != Path(evidence.source_file_path).resolve():
            raise NativeTriangleSHPError("security geometry path differs from the path recorded in P2 evidence")
        if evidence.domain_source_kind == "P2_GMSH_CONFIG":
            security_domain_path = security_config_path
        elif evidence.domain_source_kind == "DOMAIN_GEOJSON":
            domain_relative = security_policy.get("domain_geometry_file")
            if not isinstance(domain_relative, str):
                raise NativeTriangleSHPError("imported security mesh requires its explicit source-domain GeoJSON")
            security_domain_path = _resolve_relative_file(
                security_config_path.parent, domain_relative, "security domain_geometry_file",
            )
        else:
            raise NativeTriangleSHPError(f"unsupported security domain source kind {evidence.domain_source_kind}")
        if security_domain_path != Path(evidence.domain_source_path).resolve():
            raise NativeTriangleSHPError("original-domain path differs from the path recorded in P2 evidence")
        add_source("security_input_config", evidence.security_input_config_path, "security input config", evidence.security_input_config_sha256)
        add_source("security_geometry", security_geometry_path, "security geometry", evidence.source_file_sha256)
        if evidence.source_shx_sha256 is not None:
            shx_path = _resolve_relative_file(
                security_config_path.parent,
                str(PurePosixPath(security_policy["source_file"].replace("\\", "/")).with_suffix(".shx")),
                "security SHX sidecar",
            )
            add_source("security_shx", shx_path, "security SHX sidecar", evidence.source_shx_sha256)
        if evidence.source_prj_sha256 is not None:
            prj_path = _resolve_relative_file(
                security_config_path.parent,
                str(PurePosixPath(security_policy["source_file"].replace("\\", "/")).with_suffix(".prj")),
                "security PRJ sidecar",
            )
            add_source("security_prj", prj_path, "security PRJ sidecar", evidence.source_prj_sha256)
        add_source("security_domain", security_domain_path, "original domain geometry/config", evidence.domain_source_sha256)

    try:
        result = reader()
    except Exception as exc:
        raise NativeTriangleSHPError(
            f"native {request.result_kind} result failed its existing strict reader/source binding: {exc}"
        ) from exc
    if (result.sources_applied != request.result_kind or result.boundary_policy != "closed_wall"
            or result.gate_hydraulics_enabled or result.production_approved):
        raise NativeTriangleSHPError("native source result family/closed-wall flags differ from the v2 request")
    expected_manning = request.result_kind in {"manning", "manning_and_rainfall"}
    expected_rainfall = request.result_kind in {"rainfall", "manning_and_rainfall"}
    if (bool(result.manning_enabled) != expected_manning or bool(result.manning_applied) != expected_manning
            or bool(result.rainfall_enabled) != expected_rainfall or bool(result.rainfall_applied) != expected_rainfall):
        raise NativeTriangleSHPError("native result enabled/applied flags conflict with result_kind")
    result_security = result.security_exclusion
    if (result_security is None) != (evidence is None):
        raise NativeTriangleSHPError("native source result security evidence differs from the validated P2/P3 active domain")
    if result_security is not None and evidence is not None:
        if security_evidence_sha256(result_security) != security_evidence_sha256(evidence):
            raise NativeTriangleSHPError("native source result security evidence differs from the P2/P3 source graph")
    if not base_capability.business.terrain.vertical_datum:
        raise NativeTriangleSHPError("source SHP requires the native P3-A vertical datum for water_level_m")

    hashes = {role: _sha256_bytes(raw) for role, raw in graph_bytes.items()}
    hashes.update({
        "request": hashes["request"],
        "run_config": hashes["top_level_run_config"],
        "business_package": hashes["p3_business"],
        "mesh": hashes["p2_mesh"],
        "native_result": hashes["native_result"],
    })
    source_graph = tuple(
        NativeTriangleSHPSourceArtifact(
            role=role, path=str(graph_paths[role]), sha256=hashes[role], byte_count=len(graph_bytes[role]),
        )
        for role in graph_paths
    )
    context = _NativeSourceSHPContext(
        request_path=request_file, request_bytes=request_raw, request=request,
        run_config_path=run_config_path, run_config_bytes=run_config_bytes,
        capability=base_capability, source_capability=source_capability,
        result=result, native_result_path=native_result_path,
        native_result_bytes=graph_bytes["native_result"],
        source_hashes=hashes, source_paths=graph_paths, source_graph=source_graph,
    )
    _assert_sources_unchanged(context)
    return context


def _assert_sources_unchanged(context: _SHPContext | _NativeSourceSHPContext) -> None:
    for name, path in context.source_paths.items():
        if _sha256_bytes(_regular_bytes(path, f"source {name}")) != context.source_hashes[name]:
            raise NativeTriangleSHPError(f"source graph changed while the SHP package was being processed: {path}")


def _selected_frames(context: _SHPContext | _NativeSourceSHPContext) -> tuple[tuple[int, ...], dict[str, Any]]:
    result = context.result
    selector = context.request.selector
    times = tuple(float(value) for value in result.time_s)
    if selector.aggregation == "FRAME":
        index = int(selector.frame_index)
        if index >= len(times):
            raise NativeTriangleSHPError("FRAME frame_index is outside the native result time axis")
        if float(selector.time_s) != times[index]:
            raise NativeTriangleSHPError("FRAME time_s must exactly equal the native time_s[frame_index]")
        return (index,), {
            "aggregation": "FRAME", "frame_index": index, "time_s": times[index],
            "window_start_s": None, "window_end_s": None,
        }
    start, end = float(selector.window_start_s), float(selector.window_end_s)
    if start not in times or end not in times:
        raise NativeTriangleSHPError("MAX_OVER_WINDOW endpoints must exactly match saved native output times; interpolation is not performed")
    if start >= end:
        raise NativeTriangleSHPError("MAX_OVER_WINDOW requires window_start_s < window_end_s")
    indices = tuple(index for index, time_s in enumerate(times) if start <= time_s <= end)
    if not indices or times[indices[0]] != start or times[indices[-1]] != end:
        raise NativeTriangleSHPError("MAX_OVER_WINDOW must include both exact native endpoint frames")
    return indices, {
        "aggregation": "MAX_OVER_WINDOW", "frame_index": None, "time_s": None,
        "window_start_s": start, "window_end_s": end,
    }


def _signed_polygon_area(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    # Translate to a local origin before shoelace products. Absolute projected
    # coordinates can be millions of metres while a valid face is only metres
    # wide; subtracting the large x*y products loses the face area to rounding.
    origin_x, origin_y = points[0]
    return 0.5 * math.fsum(
        (points[index][0] - origin_x) * (points[(index + 1) % len(points)][1] - origin_y)
        - (points[(index + 1) % len(points)][0] - origin_x) * (points[index][1] - origin_y)
        for index in range(len(points))
    )


def _dbf_number(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise NativeTriangleSHPError(f"{name} must be finite before writing DBF")
    text = f"{value:.{_DBF_DECIMALS}f}"
    if len(text) > 20:
        raise NativeTriangleSHPError(f"{name} cannot be represented losslessly enough in the fixed-width DBF numeric field")
    rounded = float(text)
    if not math.isfinite(rounded):
        raise NativeTriangleSHPError(f"{name} overflows the DBF numeric field")
    return rounded


def _output_crs_and_transform(source_crs_text: str, target_crs_selector: str):
    try:
        from pyproj import CRS, Transformer
        source_crs = CRS.from_user_input(source_crs_text)
        if not source_crs.is_projected:
            raise NativeTriangleSHPError("native P4 SHP source CRS must be projected in metres")
        axis_info = source_crs.axis_info
        if len(axis_info) < 2 or any(not math.isclose(float(axis.unit_conversion_factor), 1.0, rel_tol=0.0, abs_tol=1e-12) for axis in axis_info[:2]):
            raise NativeTriangleSHPError("native P4 projected CRS axes must use metres so face_area_m2 matches geometry")
        target_crs = source_crs if target_crs_selector == "NATIVE" else CRS.from_epsg(4490)
        transformer = None if target_crs.equals(source_crs, ignore_axis_order=True) else Transformer.from_crs(source_crs, target_crs, always_xy=True)
        wkt = target_crs.to_wkt(version="WKT1_ESRI")
        return source_crs, target_crs, transformer, wkt
    except NativeTriangleSHPError:
        raise
    except ImportError as exc:
        raise NativeTriangleSHPError("native triangle SHP export requires pyproj; install the triangular extra") from exc
    except Exception as exc:
        raise NativeTriangleSHPError(f"cannot resolve output CRS: {exc}") from exc


def _build_features(context: _SHPContext | _NativeSourceSHPContext) -> dict[str, Any]:
    result = context.result
    mesh = context.capability.business.mesh
    terrain = context.capability.business.terrain
    selector_indices, selector = _selected_frames(context)
    source_crs, target_crs, transformer, target_wkt = _output_crs_and_transform(result.coordinate_system, context.request.target_crs)
    if tuple(int(value) for value in result.face_id) != tuple(int(value) for value in mesh.face_id):
        raise NativeTriangleSHPError("native face_id order differs from the validated P2 mesh")
    if len(set(int(value) for value in result.face_id)) != len(result.face_id):
        raise NativeTriangleSHPError("native face_id values must be unique")
    depths_by_time = tuple(tuple(float(value) for value in row) for row in result.water_depth_m)
    mx_by_time = tuple(tuple(float(value) for value in row) for row in result.momentum_x_m2_s)
    my_by_time = tuple(tuple(float(value) for value in row) for row in result.momentum_y_m2_s)
    bed = tuple(float(value) for value in terrain.elevation_m)
    ids = tuple(int(value) for value in result.face_id)
    areas = tuple(float(value) for value in result.face_area_m2)
    if not (len(ids) == len(areas) == len(bed) == len(result.face_nodes)):
        raise NativeTriangleSHPError("native face IDs/areas/terrain/connectivity are not aligned")

    rows: list[dict[str, Any]] = []
    selected_ids: list[str] = []
    source_geometry_areas: list[float] = []
    face_areas_by_id = dict(zip(ids, areas))
    threshold = float(context.request.wet_depth_threshold_m)
    for face_index, (face_id, connectivity) in enumerate(zip(ids, result.face_nodes)):
        if len(connectivity) != 3 or len(set(int(node) for node in connectivity)) != 3:
            raise NativeTriangleSHPError(f"native face_id {face_id} does not have exactly three distinct nodes")
        source_points = tuple(tuple(float(value) for value in result.node_xy_m[int(node)]) for node in connectivity)
        geom_area = abs(_signed_polygon_area(source_points))
        if not math.isfinite(geom_area) or geom_area <= 0.0:
            raise NativeTriangleSHPError(f"native face_id {face_id} has invalid projected geometry area")
        if not math.isclose(geom_area, areas[face_index], rel_tol=1e-10, abs_tol=1e-11):
            raise NativeTriangleSHPError(f"native face_id {face_id} projected geometry area differs from stored face_area_m2")
        source_geometry_areas.append(geom_area)

        if context.request.selector.aggregation == "FRAME":
            frame_index = selector_indices[0]
            depth = depths_by_time[frame_index][face_index]
            momentum_x = mx_by_time[frame_index][face_index]
            momentum_y = my_by_time[frame_index][face_index]
            if depth < 0.0 or not math.isfinite(depth):
                raise NativeTriangleSHPError(f"native depth is invalid at face_id {face_id}")
            if depth == 0.0:
                if momentum_x != 0.0 or momentum_y != 0.0:
                    raise NativeTriangleSHPError(f"dry face_id {face_id} has nonzero native momentum; velocity cannot be derived")
                ux = uy = 0.0
            else:
                ux, uy = momentum_x / depth, momentum_y / depth
            if not math.isfinite(ux) or not math.isfinite(uy):
                raise NativeTriangleSHPError(f"native velocity is non-finite at face_id {face_id}")
            selected_value = depth
            row = {
                "face_id": str(face_id), "time_s": _dbf_number(float(result.time_s[frame_index]), "time_s"),
                "depth_m": _dbf_number(depth, "depth_m"),
                "water_lvl": _dbf_number(bed[face_index] + depth, "water_level_m"),
                "ux_m_s": _dbf_number(ux, "velocity_x_m_s"),
                "uy_m_s": _dbf_number(uy, "velocity_y_m_s"),
                "area_m2": _dbf_number(areas[face_index], "area_m2"),
            }
        else:
            frame_values = [(depths_by_time[index][face_index], float(result.time_s[index])) for index in selector_indices]
            maximum = max(value for value, _ in frame_values)
            earliest_time = next(time_s for value, time_s in frame_values if value == maximum)
            selected_value = maximum
            row = {
                "face_id": str(face_id), "max_depth": _dbf_number(maximum, "max_depth_m"),
                "max_time": _dbf_number(earliest_time, "max_time_s"),
                "area_m2": _dbf_number(areas[face_index], "area_m2"),
            }
        if context.request.scope_selection == "MAXIMUM_WET_SURFACE" and not selected_value > threshold:
            continue

        output_points = source_points
        if transformer is not None:
            try:
                x_values, y_values = transformer.transform([point[0] for point in source_points], [point[1] for point in source_points])
                output_points = tuple((float(x), float(y)) for x, y in zip(x_values, y_values))
            except Exception as exc:
                raise NativeTriangleSHPError(f"coordinate export transform failed at face_id {face_id}: {exc}") from exc
        if any(not math.isfinite(value) for point in output_points for value in point):
            raise NativeTriangleSHPError(f"export coordinates are non-finite at face_id {face_id}")
        # ESRI polygon outer rings are clockwise. This only reorders the three
        # actual face vertices and never changes or subdivides the native face.
        if _signed_polygon_area(output_points) > 0.0:
            output_points = (output_points[0], output_points[2], output_points[1])
        ring = (*output_points, output_points[0])
        rows.append(_SHPFeature(face_id=face_id, polygon_points=ring,
                                record_values=tuple(row[name] for name, *_ in _field_specs(context.request.selector.aggregation)),
                                native_area_m2=areas[face_index]))
        selected_ids.append(str(face_id))

    face_id_digest = _canonical_sha256([str(value) for value in ids])
    exported_id_digest = _canonical_sha256(selected_ids)
    scope_hash = _canonical_sha256({
        "topology_fingerprint": topology_fingerprint(mesh),
        "terrain_fingerprint": terrain_fingerprint(mesh, terrain),
        "scope_selection": context.request.scope_selection,
        "wet_depth_threshold_m": threshold,
        "selector": selector,
        "exported_face_id_sha256": exported_id_digest,
    })
    return {
        "features": tuple(rows), "selected_face_ids": tuple(selected_ids),
        "selector_indices": selector_indices, "selector": selector,
        "source_crs": source_crs, "target_crs": target_crs, "target_wkt": target_wkt,
        "scope_hash": scope_hash, "native_face_id_sha256": face_id_digest,
        "exported_face_id_sha256": exported_id_digest,
        "scope_area_m2": math.fsum(face_areas_by_id[int(value)] for value in selected_ids),
        "source_geometry_areas": tuple(source_geometry_areas),
    }


def _field_specs(aggregation: str) -> tuple[tuple[str, str, int, int], ...]:
    return _FRAME_FIELDS if aggregation == "FRAME" else _MAX_FIELDS


def _field_mapping(aggregation: str, vertical_datum: str) -> list[NativeTriangleSHPField]:
    semantics = {
        "face_id": ("native_face_id", None, "Exact native P2/P4 face identifier stored as decimal text to avoid DBF numeric precision loss."),
        "time_s": ("frame_time_s", "s", "Exact selected native output frame time, represented to 12 DBF decimals."),
        "depth_m": ("water_depth_m", "m", "Native P4 water_depth array at the selected FRAME."),
        "water_lvl": ("water_level_m", "m", f"P3-A bed_elevation_m plus native depth_m; vertical datum {vertical_datum}."),
        "ux_m_s": ("velocity_x_m_s", "m/s", "Native momentum_x_m2_s divided by water_depth_m; components remain in source projected CRS axes."),
        "uy_m_s": ("velocity_y_m_s", "m/s", "Native momentum_y_m2_s divided by water_depth_m; components remain in source projected CRS axes."),
        "area_m2": ("native_face_area_m2", "m2", "Stored P4 face_area_m2, verified against source projected triangle geometry."),
        "max_depth": ("maximum_water_depth_m", "m", "Per-face maximum native water_depth_m over the inclusive saved-frame window."),
        "max_time": ("earliest_maximum_time_s", "s", "Earliest native saved time attaining this face's maximum depth."),
    }
    output = []
    for name, typ, _, _ in _field_specs(aggregation):
        semantic, unit, meaning = semantics[name]
        output.append(NativeTriangleSHPField(dbf_name=name, semantic_name=semantic, dbf_type="C" if typ == "C" else "N", unit=unit, meaning=meaning))
    return output


def _file_media_type(key: str) -> str:
    return {
        "faces.shp": "application/x-esri-shapefile",
        "faces.shx": "application/x-esri-shapefile-index",
        "faces.dbf": "application/dbase",
        "faces.prj": "text/plain",
        "faces.cpg": "text/plain",
    }[key]


def _write_shapefile(directory: Path, context: _SHPContext, built: Mapping[str, Any]) -> None:
    try:
        import shapefile
    except ImportError as exc:
        raise NativeTriangleSHPError("native triangle SHP export requires pyshp; install the 'shp' extra") from exc
    layer_path = directory / "faces"
    writer = shapefile.Writer(str(layer_path), shapeType=shapefile.POLYGON, encoding="utf-8")
    writer.autoBalance = 1
    try:
        specs = _field_specs(context.request.selector.aggregation)
        for name, kind, width, decimals in specs:
            writer.field(name, kind, size=width, decimal=decimals)
        for feature in built["features"]:
            writer.poly([list(feature.polygon_points)])
            writer.record(*feature.record_values)
    finally:
        writer.close()
    try:
        (directory / "faces.cpg").write_text("UTF-8", encoding="ascii")
        (directory / "faces.prj").write_text(built["target_wkt"], encoding="utf-8")
    except OSError as exc:
        raise NativeTriangleSHPError(f"cannot write SHP CRS/encoding sidecars: {exc}") from exc


def _collect_file_records(directory: Path) -> list[NativeTriangleSHPFile]:
    keys = ("faces.shp", "faces.shx", "faces.dbf", "faces.prj", "faces.cpg")
    records: list[NativeTriangleSHPFile] = []
    for key in keys:
        path = directory / key
        if _is_reparse_component(path) or not path.is_file():
            raise NativeTriangleSHPError(f"required SHP component is missing or aliased: {path}")
        raw = path.read_bytes()
        records.append(NativeTriangleSHPFile(key=key, sha256=_sha256_bytes(raw), byte_count=len(raw), media_type=_file_media_type(key)))
    return records


def _expected_manifest(context: _SHPContext, built: Mapping[str, Any], package_directory: Path) -> NativeTriangleSHPManifest:
    file_records = _collect_file_records(package_directory)
    return _build_manifest(context, built, file_records)


def _read_shapefile_features(package_directory: Path, context: _SHPContext, built: Mapping[str, Any]) -> None:
    try:
        import shapefile
    except ImportError as exc:
        raise NativeTriangleSHPError("validating native triangle SHP requires pyshp; install the 'shp' extra") from exc
    path = package_directory / "faces.shp"
    try:
        reader = shapefile.Reader(str(path), encoding="utf-8")
        try:
            shapes = reader.shapes()
            records = reader.records()
            fields = [(item[0], item[1], item[2], item[3]) for item in reader.fields[1:]]
        finally:
            reader.close()
    except Exception as exc:
        raise NativeTriangleSHPError(f"independent Shapefile reader failed: {exc}") from exc
    expected_fields = [(name, kind, width, decimals) for name, kind, width, decimals in _field_specs(context.request.selector.aggregation)]
    if reader.shapeType != shapefile.POLYGON or fields != expected_fields:
        raise NativeTriangleSHPError("SHP geometry type or DBF field layout differs from the native-triangle contract")
    features: Sequence[_SHPFeature] = built["features"]
    if len(shapes) != len(records) or len(shapes) != len(features):
        raise NativeTriangleSHPError("SHP/DBF feature counts differ from the independently selected native face set")
    coordinate_tolerance = 1e-10 if context.request.target_crs == "NATIVE" else 1e-9
    for index, (shape, record, expected) in enumerate(zip(shapes, records, features)):
        points = tuple((float(x), float(y)) for x, y in shape.points)
        if list(shape.parts) != [0] or len(points) != 4 or points[0] != points[-1] or len(set(points[:-1])) != 3:
            raise NativeTriangleSHPError(f"SHP feature {index} must contain exactly three distinct vertices and one closing point")
        if len(points) != len(expected.polygon_points) or any(
            not math.isclose(actual[axis], expected.polygon_points[point][axis], rel_tol=0.0, abs_tol=coordinate_tolerance)
            for point, actual in enumerate(points) for axis in (0, 1)
        ):
            raise NativeTriangleSHPError(f"SHP feature geometry differs from native face_id {expected.face_id}")
        values = list(record)
        if str(values[0]).strip() != str(expected.face_id):
            raise NativeTriangleSHPError(f"DBF face_id is missing, truncated or reordered at feature {index}")
        for field_index, (_, kind, _, _) in enumerate(expected_fields[1:], start=1):
            if kind == "N":
                actual = float(values[field_index])
                wanted = float(expected.record_values[field_index])
                if not math.isfinite(actual) or actual != wanted:
                    raise NativeTriangleSHPError(f"DBF numeric field {expected_fields[field_index][0]} differs at face_id {expected.face_id}")
            elif str(values[field_index]).strip() != str(expected.record_values[field_index]):
                raise NativeTriangleSHPError(f"DBF field {expected_fields[field_index][0]} differs at face_id {expected.face_id}")


def _walk_package(package_directory: Path) -> dict[str, Path]:
    if _is_reparse_component(package_directory) or not package_directory.is_dir():
        raise NativeTriangleSHPError(f"SHP output package directory is missing or aliased: {package_directory}")
    files: dict[str, Path] = {}
    for root, dirs, names in os.walk(package_directory, followlinks=False):
        base = Path(root)
        for name in dirs:
            if _is_reparse_component(base / name):
                raise NativeTriangleSHPError(f"SHP package contains a symlink/reparse directory: {base / name}")
        for name in names:
            path = base / name
            if _is_reparse_component(path) or not path.is_file():
                raise NativeTriangleSHPError(f"SHP package contains a non-regular file: {path}")
            key = path.relative_to(package_directory).as_posix()
            if key in files:
                raise NativeTriangleSHPError(f"duplicate SHP package path: {key}")
            files[key] = path
    return files


def _file_identity(path: Path, *, directory: bool) -> tuple[int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise NativeTriangleSHPError(f"cannot inspect SHP transaction path {path}: {exc}") from exc
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & reparse_flag:
        raise NativeTriangleSHPError(f"SHP transaction path must not be a symlink/reparse point: {path}")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise NativeTriangleSHPError(f"SHP transaction path is not a directory: {path}")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise NativeTriangleSHPError(f"SHP transaction path is not a regular file: {path}")
    if not info.st_ino:
        raise NativeTriangleSHPError(f"filesystem has no stable identity for safe SHP transaction rollback: {path}")
    return int(info.st_dev), int(info.st_ino)


def _capture_parent_chain(path: Path) -> tuple[tuple[Path, tuple[int, int]], ...]:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    chain: list[tuple[Path, tuple[int, int]]] = []
    for part in absolute.parts[1:]:
        current = current / part
        chain.append((current, _file_identity(current, directory=True)))
    return tuple(chain)


def _parent_chain_matches(chain: Sequence[tuple[Path, tuple[int, int]]]) -> bool:
    try:
        return all(_file_identity(path, directory=True) == identity for path, identity in chain)
    except NativeTriangleSHPError:
        return False


def _write_manifest_recovery_copy(directory: Path, contents: bytes) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(20):
        path = directory / f"manifest.recovered.{os.urandom(16).hex()}.json"
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            if _regular_bytes(path, "manifest recovery copy") != contents:
                raise NativeTriangleSHPError(f"manifest recovery copy changed while being written: {path}")
            return path
        except Exception:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    raise NativeTriangleSHPError("cannot allocate a unique manifest recovery filename without overwriting existing data")


def _retire_failed_manifest(
    destination: Path,
    manifest_bytes: bytes,
    root_identity: tuple[int, int],
    manifest_identity: tuple[int, int],
    parent_chain: Sequence[tuple[Path, tuple[int, int]]],
) -> list[str]:
    issues: list[str] = []
    try:
        if not _parent_chain_matches(parent_chain) or _file_identity(destination, directory=True) != root_identity:
            return [f"preserved SHP output root whose identity/parent chain changed: {destination}"]
    except NativeTriangleSHPError as exc:
        return [str(exc)]
    marker = destination / "manifest.json"
    for _ in range(3):
        try:
            current_identity = _file_identity(marker, directory=False)
            current_bytes = _regular_bytes(marker, "published SHP manifest")
        except NativeTriangleSHPError as exc:
            if marker.exists() or marker.is_symlink():
                return issues + [f"preserved non-regular/unreadable SHP marker at {marker}: {exc}"]
            return issues
        if current_identity != manifest_identity:
            return issues + [f"preserved replaced SHP marker at {marker}; its bytes were not modified by rollback"]
        if current_bytes != manifest_bytes:
            try:
                recovery = _write_manifest_recovery_copy(destination, current_bytes)
            except (OSError, NativeTriangleSHPError) as exc:
                return issues + [f"could not recover modified SHP marker; left it at {marker}: {exc}"]
            issues.append(f"recovered modified SHP manifest bytes at {recovery}")
        try:
            if not _parent_chain_matches(parent_chain) or _file_identity(destination, directory=True) != root_identity:
                return issues + [f"left SHP marker at {marker} because output root ownership changed"]
            if _file_identity(marker, directory=False) != current_identity or _regular_bytes(marker, "published SHP manifest") != current_bytes:
                continue
            marker.unlink()
            if marker.exists() or marker.is_symlink():
                return issues + [f"a concurrent writer recreated the SHP success marker at {marker}"]
            return issues
        except (OSError, NativeTriangleSHPError) as exc:
            return issues + [f"could not safely retire SHP marker {marker}: {exc}"]
    return issues + [f"left concurrently changing SHP marker at {marker}"]


def _manifest_base(context: _SHPContext, built: Mapping[str, Any]) -> dict[str, Any]:
    result = context.result
    mesh = context.capability.business.mesh
    terrain = context.capability.business.terrain
    security = result.security_exclusion
    security_digest = None if security is None else security_evidence_sha256(security)
    input_graph_sha = _canonical_sha256({
        "run_config_sha256": context.source_hashes["run_config"],
        "business_package_sha256": context.source_hashes["business_package"],
        "mesh_sha256": context.source_hashes["mesh"],
        "topology_fingerprint": topology_fingerprint(mesh),
        "terrain_fingerprint": terrain_fingerprint(mesh, terrain),
        "security_exclusion_sha256": security_digest,
        "case_id": result.case_id,
        "engine_mode": "triangular_closed_experimental",
    })
    result_set_id = "native-tri-shp-" + _canonical_sha256({
        "native_result_sha256": context.source_hashes["native_result"],
        "selector": built["selector"], "scope_hash": built["scope_hash"],
        "target_crs": built["target_crs"].to_string(),
        "input_graph_sha256": input_graph_sha,
    })[:24]
    return {
        "format": _SHP_FORMAT, "schema_version": "1.0", "package_type": "native_triangular_result_shapefile",
        "result_set_id": result_set_id, "product_type": "experimental_native_triangular_p4_result",
        "experimental": True, "engineering_validated": False, "production_approved": False, "can_publish": False,
        "validation_basis": "P4_1_NATIVE_RESULT_READER_AND_SOURCE_GRAPH",
        "engine_mode": "triangular_closed_experimental", "grid_topology": "native_triangular_mesh",
        "case_id": result.case_id, "mesh_id": result.mesh_id,
        "topology_fingerprint": topology_fingerprint(mesh), "terrain_fingerprint": terrain_fingerprint(mesh, terrain),
        "input_graph_sha256": input_graph_sha,
        "source_coordinate_system": result.coordinate_system, "target_crs": built["target_crs"].to_string(),
        "vertical_datum": str(terrain.vertical_datum), "scope_selection": context.request.scope_selection,
        "scope_id": "COMPUTATIONAL_DOMAIN" if context.request.scope_selection == "FULL_DOMAIN" else "MAXIMUM_WET_SURFACE",
        "scope_hash": built["scope_hash"], "scope_area_m2": built["scope_area_m2"],
        "wet_depth_threshold_m": float(context.request.wet_depth_threshold_m),
        "selector": built["selector"], "frame_times_s": [float(value) for value in result.time_s],
        "selected_frame_indices": list(built["selector_indices"]),
        "native_face_count": len(result.face_id), "feature_count": len(built["features"]),
        "native_face_id_sha256": built["native_face_id_sha256"],
        "security_exclusion_sha256": security_digest,
        "security_active_face_id_sha256": None if security is None else security.active_face_id_sha256,
        "security_active_domain_area_m2": None if security is None else security.active_domain_area_m2,
        "security_excluded_area_m2": None if security is None else security.security_inside_area_m2,
        "exported_face_id_sha256": built["exported_face_id_sha256"],
        "run_config_sha256": context.source_hashes["run_config"],
        "business_package_sha256": context.source_hashes["business_package"],
        "mesh_sha256": context.source_hashes["mesh"], "native_result_sha256": context.source_hashes["native_result"],
        "request_sha256": context.source_hashes["request"],
        "run_config_path": str(context.run_config_path),
        "business_package_path": str(context.capability.business_path),
        "mesh_path": str(context.capability.mesh_path),
        "native_result_path": str(context.native_result_path), "request_path": str(context.request_path),
        "native_identity_limit": "The P4-1 native result binds case_id and the current P2/P3-A source graph. It contains no platform_run_id, attempt_id, script_id or local_run_id; none are inferred.",
        "face_id_encoding": "exact_decimal_text", "area_rule": "native_projected_face_area_m2",
        "window_endpoints_inclusive": True, "maximum_tie_policy": "EARLIEST_NATIVE_TIME",
        "velocity_component_frame": f"Native projected CRS axes ({result.coordinate_system}); values are momentum/depth in the original calculation frame.",
        "geometry_type": "POLYGON_WITH_THREE_DISTINCT_VERTICES", "layer_key": "faces.shp",
        "field_mapping": [item.model_dump(mode="json") for item in _field_mapping(context.request.selector.aggregation, str(terrain.vertical_datum))],
        "limitations": [
            "Only an existing experimental P4-1 closed_wall, sources=none native result is supported; this adapter never calls the solver.",
            "SHP features correspond one-to-one with native P4 face_id values; no rectangle conversion, remeshing, subdivision, merging or resampling occurs.",
            "For EPSG:4490 output, geometry coordinates are transformed in the export layer; area_m2 remains the original projected P4 face_area and is not computed from longitude/latitude.",
            "Native NetCDF has no platform attempt/script identity; case_id and source graph are validated, other run identity is NOT_ASSESSED.",
            "The artifact is experimental GIS output only, not engineering validation or publication approval.",
        ],
    }


def _write_layer(directory: Path, context: _SHPContext | _NativeSourceSHPContext, built: Mapping[str, Any]) -> None:
    try:
        import shapefile
    except ImportError as exc:
        raise NativeTriangleSHPError("native triangle SHP export requires pyshp; install with python -m pip install -e \".[triangular,shp]\"") from exc
    writer = shapefile.Writer(str(directory / "faces"), shapeType=shapefile.POLYGON, encoding="utf-8")
    writer.autoBalance = 1
    try:
        specs = _field_specs(context.request.selector.aggregation)
        for name, kind, width, decimals in specs:
            writer.field(name, kind, size=width, decimal=decimals)
        for feature in built["features"]:
            writer.poly([list(feature.polygon_points)])
            writer.record(*feature.record_values)
    finally:
        writer.close()
    try:
        (directory / "faces.cpg").write_text("UTF-8", encoding="ascii")
        (directory / "faces.prj").write_text(built["target_wkt"], encoding="utf-8")
    except OSError as exc:
        raise NativeTriangleSHPError(f"cannot write SHP CRS/encoding sidecars: {exc}") from exc


def _file_records(directory: Path) -> list[NativeTriangleSHPFile]:
    keys = ("faces.shp", "faces.shx", "faces.dbf", "faces.prj", "faces.cpg")
    records: list[NativeTriangleSHPFile] = []
    for key in keys:
        path = directory / key
        if _is_reparse_component(path) or not path.is_file():
            raise NativeTriangleSHPError(f"required SHP component is missing or aliased: {path}")
        raw = path.read_bytes()
        records.append(NativeTriangleSHPFile(key=key, sha256=_sha256_bytes(raw), byte_count=len(raw), media_type=_file_media_type(key)))
    return records


def _validate_layer(package_directory: Path, context: _SHPContext | _NativeSourceSHPContext, built: Mapping[str, Any]) -> None:
    try:
        import shapefile
    except ImportError as exc:
        raise NativeTriangleSHPError("native triangle SHP validation requires pyshp; install the 'shp' extra") from exc
    path = package_directory / "faces.shp"
    try:
        reader = shapefile.Reader(str(path), encoding="utf-8")
        shapes = reader.shapes()
        records = reader.records()
        fields = [(item[0], item[1], item[2], item[3]) for item in reader.fields[1:]]
    except Exception as exc:
        raise NativeTriangleSHPError(f"independent Shapefile reader failed: {exc}") from exc
    expected_fields = list(_field_specs(context.request.selector.aggregation))
    if reader.shapeType != shapefile.POLYGON or fields != expected_fields:
        raise NativeTriangleSHPError("SHP geometry type or DBF field layout differs from the native-triangle contract")
    features: Sequence[_SHPFeature] = built["features"]
    if len(shapes) != len(records) or len(shapes) != len(features):
        raise NativeTriangleSHPError("SHP/DBF feature counts differ from the independently selected native face set")
    tolerance = 1e-12 if context.request.target_crs == "NATIVE" else 1e-9
    for index, (shape, record, expected) in enumerate(zip(shapes, records, features)):
        points = tuple((float(x), float(y)) for x, y in shape.points)
        if list(shape.parts) != [0] or len(points) != 4 or points[0] != points[-1] or len(set(points[:-1])) != 3:
            raise NativeTriangleSHPError(f"SHP feature {index} must have exactly three distinct vertices and one closing point")
        if len(points) != len(expected.polygon_points) or any(
            not math.isclose(actual[axis], expected.polygon_points[point][axis], rel_tol=0.0, abs_tol=tolerance)
            for point, actual in enumerate(points) for axis in (0, 1)
        ):
            raise NativeTriangleSHPError(f"SHP feature geometry differs from native face_id {expected.face_id}")
        values = list(record)
        if str(values[0]).strip() != str(expected.face_id):
            raise NativeTriangleSHPError(f"DBF face_id is missing, truncated or reordered at feature {index}")
        for field_index, (_, kind, _, _) in enumerate(expected_fields[1:], start=1):
            if kind == "N":
                actual = float(values[field_index])
                wanted = float(expected.record_values[field_index])
                if not math.isfinite(actual) or actual != wanted:
                    raise NativeTriangleSHPError(f"DBF numeric field {expected_fields[field_index][0]} differs at face_id {expected.face_id}")
            elif str(values[field_index]).strip() != str(expected.record_values[field_index]):
                raise NativeTriangleSHPError(f"DBF field {expected_fields[field_index][0]} differs at face_id {expected.face_id}")


def _manifest_for_package(context: _SHPContext, built: Mapping[str, Any], package_directory: Path) -> NativeTriangleSHPManifest:
    return NativeTriangleSHPManifest.model_validate({
        **_manifest_base(context, built), "files": [item.model_dump(mode="json") for item in _file_records(package_directory)],
    })


def _source_budget_and_summary(
    context: _NativeSourceSHPContext,
    built: Mapping[str, Any],
) -> tuple[list[NativeTriangleSHPFrameBudget], float, float, NativeTriangleSHPSourcesSummary]:
    result = context.result
    mesh = context.capability.business.mesh
    areas = tuple(float(value) for value in mesh.face_area)
    if len(result.water_depth_m) != len(result.time_s) or len(result.mass_budget) != len(result.time_s):
        raise NativeTriangleSHPError("native source water states, time axis, and mass budget must align")
    frame_volumes: list[float] = []
    budget_rows: list[NativeTriangleSHPFrameBudget] = []
    initial_volume = None
    rainfall_mode = context.request.result_kind in {"rainfall", "manning_and_rainfall"}
    rainfall_cumulative = tuple(float(value) for value in getattr(result, "cumulative_rainfall_volume_m3_by_time", ()))
    if rainfall_mode and len(rainfall_cumulative) != len(result.time_s):
        raise NativeTriangleSHPError("native rainfall cumulative-volume ledger must align with saved time")
    for frame_index, (time_s, depths, ledger) in enumerate(zip(result.time_s, result.water_depth_m, result.mass_budget)):
        if len(depths) != len(areas):
            raise NativeTriangleSHPError(f"native depth frame {frame_index} does not align with active face areas")
        volume = math.fsum(float(depth) * area for depth, area in zip(depths, areas))
        if not math.isfinite(volume):
            raise NativeTriangleSHPError(f"native integrated water volume is non-finite at frame {frame_index}")
        frame_volumes.append(volume)
        if initial_volume is None:
            initial_volume = float(ledger.initial_volume_m3)
        if not math.isclose(float(ledger.initial_volume_m3), initial_volume, rel_tol=1e-12, abs_tol=1e-12):
            raise NativeTriangleSHPError("native mass-budget initial volume must remain constant across saved frames")
        if float(ledger.boundary_net_volume_m3) != 0.0:
            raise NativeTriangleSHPError("native closed-wall source result must have zero boundary net volume")
        if not math.isclose(volume, float(ledger.final_volume_m3), rel_tol=1e-12, abs_tol=1e-12):
            raise NativeTriangleSHPError(f"depth-times-area volume disagrees with native mass budget at frame {frame_index}")
        source_volume = float(ledger.source_net_volume_m3)
        expected_source = rainfall_cumulative[frame_index] if rainfall_mode else 0.0
        if not math.isclose(source_volume, expected_source, rel_tol=1e-12, abs_tol=1e-12):
            raise NativeTriangleSHPError(f"native source ledger disagrees with applied source summary at frame {frame_index}")
        expected_residual = (
            float(ledger.final_volume_m3) - float(ledger.initial_volume_m3)
            - float(ledger.boundary_net_volume_m3) - source_volume - float(ledger.correction_volume_m3)
        )
        if not math.isclose(float(ledger.residual_m3), expected_residual, rel_tol=1e-12, abs_tol=1e-12):
            raise NativeTriangleSHPError(f"native mass-budget residual is inconsistent at frame {frame_index}")
        budget_rows.append(NativeTriangleSHPFrameBudget(
            frame_index=frame_index, time_s=float(time_s), integrated_volume_m3=volume,
            ledger_initial_volume_m3=float(ledger.initial_volume_m3),
            ledger_boundary_net_volume_m3=float(ledger.boundary_net_volume_m3),
            ledger_source_net_volume_m3=source_volume,
            ledger_correction_volume_m3=float(ledger.correction_volume_m3),
            ledger_final_volume_m3=float(ledger.final_volume_m3), ledger_residual_m3=float(ledger.residual_m3),
        ))

    selected_indices = tuple(int(value) for value in built["selector_indices"])
    if not selected_indices or any(index < 0 or index >= len(frame_volumes) for index in selected_indices):
        raise NativeTriangleSHPError("selected native frame indices are empty or outside the saved time axis")
    maximum_synchronous = max(frame_volumes)
    selected_maximum_synchronous = max(frame_volumes[index] for index in selected_indices)

    expected_manning = context.request.result_kind in {"manning", "manning_and_rainfall"}
    expected_rainfall = context.request.result_kind in {"rainfall", "manning_and_rainfall"}
    manning_ids = tuple(int(value) for value in getattr(result, "manning_face_id", ()))
    manning_values = tuple(float(value) for value in getattr(result, "manning_n", ()))
    if expected_manning:
        if manning_ids != tuple(int(value) for value in result.face_id) or len(manning_values) != mesh.n_face:
            raise NativeTriangleSHPError("native Manning values must align with the complete active face set")
        manning_unit = getattr(result, "manning_source_unit", None)
        manning_physical_unit = getattr(result, "manning_physical_unit", None)
        manning_policy = getattr(result, "manning_interpretation_policy", None)
        manning_rationale = getattr(result, "manning_interpretation_rationale", None)
        if (manning_unit != "1" or manning_physical_unit != "s/m^(1/3)"
                or manning_policy != "legacy_unit1_as_si_manning" or not manning_rationale):
            raise NativeTriangleSHPError("native Manning result is missing its strict legacy-unit SI interpretation evidence")
        manning_id_hash = _canonical_sha256([str(value) for value in manning_ids])
        manning_min, manning_max = min(manning_values), max(manning_values)
    else:
        manning_ids, manning_values = (), ()
        manning_unit = manning_physical_unit = manning_policy = manning_rationale = None
        manning_id_hash = None
        manning_min = manning_max = None

    rates: list[float] = []
    accepted_interval_volumes: list[float] = []
    excluded_interval_volumes: list[float] = []
    excluded_total = 0.0
    accepted_total = 0.0
    rainfall_depth = 0.0
    rainfall_total_volume = 0.0
    rainfall_input_unit = None
    rainfall_stage_count = 0
    rainfall_nonzero_stage_count = 0
    rainfall_nonzero_effect = False
    evidence = result.security_exclusion
    if expected_rainfall:
        rates = [float(value) for value in result.rainfall_rate_m_s_by_interval]
        accepted_native = [float(value) for value in result.rainfall_volume_m3_by_interval]
        if len(rates) != len(result.time_s) - 1 or len(accepted_native) != len(rates):
            raise NativeTriangleSHPError("native rainfall rates and accepted volumes must align with saved-time intervals")
        active_area = math.fsum(areas)
        excluded_area = 0.0 if evidence is None else float(evidence.security_inside_area_m2)
        durations = [math.fsum((float(end), -float(start))) for start, end in zip(result.time_s, result.time_s[1:])]
        accepted_interval_volumes = [rate * duration * active_area for rate, duration in zip(rates, durations)]
        excluded_interval_volumes = [rate * duration * excluded_area for rate, duration in zip(rates, durations)]
        for index, (actual, expected) in enumerate(zip(accepted_native, accepted_interval_volumes)):
            if not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-12):
                raise NativeTriangleSHPError(f"accepted rainfall interval {index} is not rate times active area and duration")
        if evidence is not None:
            accounting = getattr(result, "security_rainfall_accounting", None)
            if accounting is None:
                raise NativeTriangleSHPError("security-enabled rainfall result is missing excluded unmodeled rainfall accounting")
            reported = tuple(float(value) for value in accounting.security_excluded_unmodeled_rainfall_volume_m3_by_interval)
            if len(reported) != len(excluded_interval_volumes) or any(
                not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-12)
                for actual, expected in zip(reported, excluded_interval_volumes)
            ):
                raise NativeTriangleSHPError("excluded unmodeled rainfall does not match security area, rate, and interval duration")
            if accounting.excluded_rain_is_accepted_budget_loss or accounting.automatic_flow_transfer_from_excluded_area:
                raise NativeTriangleSHPError("excluded rainfall may not be recorded as accepted loss or automatic flow transfer")
        accepted_total = math.fsum(accepted_interval_volumes)
        excluded_total = math.fsum(excluded_interval_volumes)
        rainfall_total_volume = float(result.rainfall_total_volume_m3)
        rainfall_depth = float(result.rainfall_total_depth_m)
        if (not math.isclose(rainfall_total_volume, accepted_total, rel_tol=1e-10, abs_tol=1e-12)
                or not math.isclose(rainfall_depth * active_area, accepted_total, rel_tol=1e-10, abs_tol=1e-12)):
            raise NativeTriangleSHPError("native rainfall total depth/volume differs from active-area integration")
        if not math.isclose(accepted_total, math.fsum(accepted_native), rel_tol=1e-10, abs_tol=1e-12):
            raise NativeTriangleSHPError("native rainfall accepted volume differs from per-interval mass ledger")
        rainfall_input_unit = result.rainfall_input_unit
        rainfall_stage_count = int(result.rainfall_stage_evaluation_count)
        rainfall_nonzero_stage_count = int(result.rainfall_nonzero_source_stage_evaluation_count)
        rainfall_nonzero_effect = bool(result.rainfall_nonzero_effect_applied)
    summary = NativeTriangleSHPSourcesSummary(
        result_kind=context.request.result_kind, sources_applied=str(result.sources_applied),
        manning_enabled=bool(result.manning_enabled), manning_applied=bool(result.manning_applied),
        nonzero_manning_effect_applied=bool(getattr(result, "nonzero_manning_effect_applied", False)),
        manning_source_unit=manning_unit, manning_physical_unit=manning_physical_unit,
        manning_interpretation_policy=manning_policy, manning_interpretation_rationale=manning_rationale,
        manning_face_count=len(manning_ids), manning_face_id_sha256=manning_id_hash,
        manning_n_min=manning_min, manning_n_max=manning_max,
        source_cfl_fraction=getattr(result, "source_cfl_fraction", None),
        manning_nonzero_face_stage_evaluation_count=int(getattr(result, "manning_nonzero_face_stage_evaluation_count", 0)),
        manning_maximum_gamma_per_s=float(getattr(result, "maximum_manning_gamma_per_s", 0.0)),
        manning_maximum_source_magnitude_m2_s2=float(getattr(result, "maximum_manning_source_magnitude_m2_s2", 0.0)),
        manning_source_cfl_limited_step_count=int(getattr(result, "manning_source_cfl_limited_step_count", 0)),
        manning_source_cfl_rejected_step_count=int(getattr(result, "manning_source_cfl_rejected_step_count", 0)),
        rainfall_enabled=bool(result.rainfall_enabled), rainfall_applied=bool(result.rainfall_applied),
        rainfall_nonzero_effect_applied=rainfall_nonzero_effect, rainfall_input_unit=rainfall_input_unit,
        rainfall_stage_evaluation_count=rainfall_stage_count,
        rainfall_nonzero_source_stage_evaluation_count=rainfall_nonzero_stage_count,
        rainfall_rate_m_s_by_interval=rates,
        rainfall_accepted_active_volume_m3_by_interval=accepted_interval_volumes,
        rainfall_accepted_active_volume_m3=accepted_total,
        rainfall_total_depth_m=rainfall_depth, rainfall_total_volume_m3=rainfall_total_volume,
        rainfall_excluded_unmodeled_volume_m3_by_interval=excluded_interval_volumes,
        rainfall_excluded_unmodeled_volume_m3=excluded_total,
    )
    return budget_rows, maximum_synchronous, selected_maximum_synchronous, summary


def _manifest_for_source_package(
    context: _NativeSourceSHPContext,
    built: Mapping[str, Any],
    package_directory: Path,
) -> NativeSourceTriangleSHPManifest:
    result = context.result
    evidence = result.security_exclusion
    graph_items = [item.model_dump(mode="json") for item in context.source_graph]
    graph_sha = _canonical_sha256(graph_items)
    frame_budgets, max_synchronous, selected_maximum, sources = _source_budget_and_summary(context, built)
    common = _manifest_base(context, built)
    engine_modes = {
        "manning": "triangular_manning_experimental",
        "rainfall": "triangular_rainfall_experimental",
        "manning_and_rainfall": "triangular_manning_and_rainfall_experimental",
    }
    input_graph_sha = _canonical_sha256({
        "source_graph_sha256": graph_sha,
        "topology_fingerprint": common["topology_fingerprint"],
        "terrain_fingerprint": common["terrain_fingerprint"],
        "case_id": result.case_id,
        "result_kind": context.request.result_kind,
        "native_format": result.format,
        "native_product_type": result.product_type,
        "solver_version": result.solver_version,
    })
    result_set_id = "native-source-tri-shp-" + _canonical_sha256({
        "input_graph_sha256": input_graph_sha,
        "native_result_sha256": context.source_hashes["native_result"],
        "result_kind": context.request.result_kind,
        "selector": built["selector"], "scope_hash": built["scope_hash"],
        "target_crs": built["target_crs"].to_string(),
    })[:24]
    source_digest = context.source_hashes
    payload = {
        **common,
        "format": _SOURCE_SHP_FORMAT,
        "schema_version": "2.0",
        "package_type": "native_triangular_source_result_shapefile",
        "result_set_id": result_set_id,
        "product_type": "experimental_native_triangular_p4_2_source_result",
        "validation_basis": "P4_2_NATIVE_SOURCE_READER_AND_SOURCE_GRAPH",
        "engine_mode": engine_modes[context.request.result_kind],
        "input_graph_sha256": input_graph_sha,
        "result_kind": context.request.result_kind,
        "native_format": result.format,
        "native_product_type": result.product_type,
        "solver_version": result.solver_version,
        "sources_applied": result.sources_applied,
        "manning_enabled": bool(result.manning_enabled), "manning_applied": bool(result.manning_applied),
        "rainfall_enabled": bool(result.rainfall_enabled), "rainfall_applied": bool(result.rainfall_applied),
        "nonzero_manning_effect_applied": bool(getattr(result, "nonzero_manning_effect_applied", False)),
        "rainfall_nonzero_effect_applied": bool(getattr(result, "rainfall_nonzero_effect_applied", False)),
        "source_graph_sha256": graph_sha, "source_graph": graph_items,
        "source_summary": sources.model_dump(mode="json"),
        "frame_budgets": [item.model_dump(mode="json") for item in frame_budgets],
        "maximum_synchronous_water_volume_m3": max_synchronous,
        "selected_window_maximum_synchronous_water_volume_m3": selected_maximum,
        "security_exclusion_evidence": None if evidence is None else evidence.model_dump(mode="json"),
        # The v1-compatible hash/path fields continue to identify the source
        # result graph, while the role-indexed source_graph is authoritative.
        "run_config_sha256": source_digest["top_level_run_config"],
        "run_config_path": str(context.run_config_path),
        "request_sha256": source_digest["request"],
        "native_result_sha256": source_digest["native_result"],
        "business_package_sha256": source_digest["p3_business"],
        "mesh_sha256": source_digest["p2_mesh"],
        "request_path": str(context.request_path),
        "native_result_path": str(context.native_result_path),
        "business_package_path": str(context.capability.business_path),
        "mesh_path": str(context.capability.mesh_path),
        "native_identity_limit": "LOCAL_SOURCE_GRAPH only; no platform_run_id, attempt_id, script_id or local_run_id is inferred.",
        "limitations": [
            "Only an existing strict P4-2 Manning/rainfall/combined native result is supported; this adapter never calls a runner.",
            "SHP features are one-to-one with native active face_id values; no remeshing, subdivision, merging or interpolation occurs.",
            "Rainfall active-area acceptance and excluded-area unmodeled rainfall remain separately identified; excluded rain is not transferred or recorded as budget loss.",
            "Risk, population and loss aggregates are unsupported by this adapter and are not inferred as zero.",
        ],
        "files": [item.model_dump(mode="json") for item in _file_records(package_directory)],
    }
    return NativeSourceTriangleSHPManifest.model_validate(payload)


def validate_native_triangle_shp_package(
    request_path: str | Path, package_directory: str | Path | None = None,
) -> NativeTriangleSHPValidationReport:
    """Re-read the P2/P3-A graph and native NetCDF, then verify every SHP/DBF/PRJ byte and face."""
    context = _read_result_context(request_path)
    built = _build_features(context)
    if package_directory is None:
        directory = _output_path(context.request_path, context.request.output_directory)
    else:
        candidate = Path(os.path.abspath(package_directory))
        if _is_reparse_component(candidate):
            raise NativeTriangleSHPError(f"SHP package directory must not traverse a symlink or reparse point: {candidate}")
        directory = candidate.resolve()
    if _is_reparse_component(directory) or not directory.is_dir():
        raise NativeTriangleSHPError(f"native triangle SHP package directory is missing or aliased: {directory}")
    actual = _walk_package(directory)
    expected_keys = {"faces.shp", "faces.shx", "faces.dbf", "faces.prj", "faces.cpg", "manifest.json"}
    if set(actual) != expected_keys:
        raise NativeTriangleSHPError(f"SHP package file set differs from the contract; missing={sorted(expected_keys-set(actual))}, extra={sorted(set(actual)-expected_keys)}")
    manifest_raw = _regular_bytes(actual["manifest.json"], "native triangle SHP manifest")
    try:
        manifest = NativeTriangleSHPManifest.model_validate(_strict_json(manifest_raw, "native triangle SHP manifest"))
    except ValidationError as exc:
        raise NativeTriangleSHPError(f"native triangle SHP manifest schema validation failed: {exc}") from exc
    for record in manifest.files:
        raw = _regular_bytes(actual[record.key], f"SHP package file {record.key}")
        if len(raw) != record.byte_count or _sha256_bytes(raw) != record.sha256:
            raise NativeTriangleSHPError(f"SHP package file byte count/SHA-256 mismatch: {record.key}")
        if record.media_type != _file_media_type(record.key):
            raise NativeTriangleSHPError(f"SHP package file media type mismatch: {record.key}")
    expected_manifest = _manifest_for_package(context, built, directory)
    if manifest.model_dump(mode="json") != expected_manifest.model_dump(mode="json"):
        raise NativeTriangleSHPError("SHP manifest differs from the freshly validated native source graph and selector")
    if (actual["faces.cpg"].read_text(encoding="ascii").strip().upper() != "UTF-8"):
        raise NativeTriangleSHPError("faces.cpg must declare UTF-8 encoding")
    try:
        from pyproj import CRS
        actual_crs = CRS.from_wkt(actual["faces.prj"].read_text(encoding="utf-8"))
    except Exception as exc:
        raise NativeTriangleSHPError(f"faces.prj is missing or unreadable: {exc}") from exc
    expected_serialized_crs = CRS.from_wkt(built["target_wkt"])
    if not actual_crs.equals(expected_serialized_crs, ignore_axis_order=True):
        raise NativeTriangleSHPError("faces.prj CRS differs from requested native/EPSG:4490 CRS")
    _validate_layer(directory, context, built)
    _assert_sources_unchanged(context)
    return NativeTriangleSHPValidationReport(
        valid=True, complete=True, result_set_id=expected_manifest.result_set_id,
        feature_count=len(built["features"]),
        checks=["P4-1 native reader/source graph", "triangle geometry and exact face_id set", "DBF fields/units/values", "PRJ/CPG and component hashes"],
    )


def validate_native_source_triangle_shp_package(
    request_path: str | Path, package_directory: str | Path | None = None,
) -> NativeTriangleSHPValidationReport:
    """Re-read the matching strict source-result reader and complete local source graph."""
    context = _read_native_source_context(request_path)
    built = _build_features(context)
    if package_directory is None:
        directory = _output_path(context.request_path, context.request.output_directory)
    else:
        candidate = Path(os.path.abspath(package_directory))
        if _is_reparse_component(candidate):
            raise NativeTriangleSHPError(f"SHP package directory must not traverse a symlink or reparse point: {candidate}")
        directory = candidate.resolve()
    if _is_reparse_component(directory) or not directory.is_dir():
        raise NativeTriangleSHPError(f"native source SHP package directory is missing or aliased: {directory}")
    actual = _walk_package(directory)
    expected_keys = {"faces.shp", "faces.shx", "faces.dbf", "faces.prj", "faces.cpg", "manifest.json"}
    if set(actual) != expected_keys:
        raise NativeTriangleSHPError(
            f"source SHP package file set differs from the contract; "
            f"missing={sorted(expected_keys-set(actual))}, extra={sorted(set(actual)-expected_keys)}"
        )
    manifest_raw = _regular_bytes(actual["manifest.json"], "native source SHP manifest")
    try:
        manifest = NativeSourceTriangleSHPManifest.model_validate(_strict_json(manifest_raw, "native source SHP manifest"))
    except ValidationError as exc:
        raise NativeTriangleSHPError(f"native source SHP manifest schema validation failed: {exc}") from exc
    for record in manifest.files:
        raw = _regular_bytes(actual[record.key], f"source SHP package file {record.key}")
        if len(raw) != record.byte_count or _sha256_bytes(raw) != record.sha256:
            raise NativeTriangleSHPError(f"source SHP component byte count/SHA-256 mismatch: {record.key}")
        if record.media_type != _file_media_type(record.key):
            raise NativeTriangleSHPError(f"source SHP component media type mismatch: {record.key}")
    expected_manifest = _manifest_for_source_package(context, built, directory)
    if manifest.model_dump(mode="json") != expected_manifest.model_dump(mode="json"):
        raise NativeTriangleSHPError("source SHP manifest differs from freshly validated native source graph and selector")
    if actual["faces.cpg"].read_text(encoding="ascii").strip().upper() != "UTF-8":
        raise NativeTriangleSHPError("faces.cpg must declare UTF-8 encoding")
    try:
        from pyproj import CRS
        actual_crs = CRS.from_wkt(actual["faces.prj"].read_text(encoding="utf-8"))
        expected_crs = CRS.from_wkt(built["target_wkt"])
    except Exception as exc:
        raise NativeTriangleSHPError(f"faces.prj is missing or unreadable: {exc}") from exc
    if not actual_crs.equals(expected_crs, ignore_axis_order=True):
        raise NativeTriangleSHPError("faces.prj CRS differs from the requested native/EPSG:4490 CRS")
    _validate_layer(directory, context, built)
    _assert_sources_unchanged(context)
    return NativeTriangleSHPValidationReport(
        valid=True, complete=True, result_set_id=expected_manifest.result_set_id,
        feature_count=len(built["features"]),
        checks=[
            "P4-2 matching native Manning/source reader and complete source graph",
            "triangle geometry and exact active face_id set", "native source budget and saved-time alignment",
            "DBF fields/units/values", "PRJ/CPG and component hashes",
        ],
    )


def export_native_source_triangle_shp(request_path: str | Path) -> NativeTriangleSHPValidationReport:
    """Export an existing source-enabled native result; never calls a runner."""
    context = _read_native_source_context(request_path)
    destination = _output_path(context.request_path, context.request.output_directory)
    if _is_reparse_component(destination) or destination.exists():
        raise NativeTriangleSHPError(f"refusing to overwrite an existing or aliased source SHP package directory: {destination}")
    if not destination.parent.is_dir():
        raise NativeTriangleSHPError(f"source SHP output parent directory must already exist: {destination.parent}")
    built = _build_features(context)
    parent_chain = _capture_parent_chain(destination.parent)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=destination.parent))
    manifest_bytes = b""
    stage_identity = _file_identity(stage, directory=True)
    manifest_identity: tuple[int, int] | None = None
    try:
        _write_layer(stage, context, built)
        manifest = _manifest_for_source_package(context, built, stage)
        manifest_bytes = _json_bytes(manifest.model_dump(mode="json"))
        (stage / "manifest.json").write_bytes(manifest_bytes)
        manifest_identity = _file_identity(stage / "manifest.json", directory=False)
        validate_native_source_triangle_shp_package(request_path, stage)
        _assert_sources_unchanged(context)
        if destination.exists() or destination.is_symlink():
            raise NativeTriangleSHPError(f"source SHP output directory appeared during export; refusing to replace it: {destination}")
        os.rename(stage, destination)
        return validate_native_source_triangle_shp_package(request_path, destination)
    except Exception as exc:
        published = False
        if manifest_identity is not None and destination.exists() and not _is_reparse_component(destination):
            try:
                published = _file_identity(destination, directory=True) == stage_identity
            except NativeTriangleSHPError:
                published = False
        if published:
            rollback = _retire_failed_manifest(destination, manifest_bytes, stage_identity, manifest_identity, parent_chain)
            if rollback:
                raise NativeTriangleSHPError(
                    f"source SHP export failed: {exc}; rollback preserved concurrent/replaced content: {'; '.join(rollback)}"
                ) from exc
            raise NativeTriangleSHPError(
                f"source SHP export failed: {exc}; the owned success marker was safely retired from {destination}"
            ) from exc
        elif stage.exists() or stage.is_symlink():
            if stage.is_symlink() or _is_reparse_component(stage):
                location = f"failed source staging path is now a symlink/reparse point; no cleanup was attempted: {stage}"
            elif not _parent_chain_matches(parent_chain):
                location = f"failed source staging parent chain changed; stage was preserved at {stage}"
            else:
                try:
                    current_stage_identity = _file_identity(stage, directory=True)
                except NativeTriangleSHPError as identity_error:
                    location = f"failed source staging path could not be safely identified and was preserved at {stage}: {identity_error}"
                else:
                    if current_stage_identity == stage_identity:
                        marker_notes: list[str] = []
                        if manifest_identity is not None:
                            marker_notes = _retire_failed_manifest(
                                stage, manifest_bytes, stage_identity, manifest_identity, parent_chain,
                            )
                        if marker_notes:
                            location = f"source stage preserved at {stage}; rollback preserved concurrent marker/content: {'; '.join(marker_notes)}"
                        elif manifest_identity is not None:
                            location = f"source stage preserved at {stage}; its owned success marker was safely retired"
                        else:
                            location = f"failed source stage preserved for inspection at {stage}"
                    else:
                        location = f"source staging path now names a replaced directory; no cleanup was attempted: {stage}"
            raise NativeTriangleSHPError(f"source SHP export failed: {exc}; {location}") from exc
        else:
            if destination.exists():
                location = f"source output root with an unowned/replaced identity remains at {destination}"
            else:
                location = f"source output root disappeared or moved from {destination}; its location is unverified"
            raise NativeTriangleSHPError(f"source SHP export failed: {exc}; {location}") from exc


def export_native_triangle_shp(request_path: str | Path) -> NativeTriangleSHPValidationReport:
    """Export an existing native result to a new directory; never calls the solver."""
    context = _read_result_context(request_path)
    destination = _output_path(context.request_path, context.request.output_directory)
    if _is_reparse_component(destination) or destination.exists():
        raise NativeTriangleSHPError(f"refusing to overwrite an existing or aliased SHP package directory: {destination}")
    if not destination.parent.is_dir():
        raise NativeTriangleSHPError(f"SHP output parent directory must already exist: {destination.parent}")
    built = _build_features(context)
    parent_chain = _capture_parent_chain(destination.parent)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=destination.parent))
    manifest_bytes = b""
    stage_identity = _file_identity(stage, directory=True)
    manifest_identity: tuple[int, int] | None = None
    try:
        _write_layer(stage, context, built)
        manifest = _manifest_for_package(context, built, stage)
        manifest_bytes = _json_bytes(manifest.model_dump(mode="json"))
        (stage / "manifest.json").write_bytes(manifest_bytes)
        manifest_identity = _file_identity(stage / "manifest.json", directory=False)
        validate_native_triangle_shp_package(request_path, stage)
        if destination.exists() or destination.is_symlink():
            raise NativeTriangleSHPError(f"SHP output directory appeared during export; refusing to replace it: {destination}")
        os.rename(stage, destination)
        return validate_native_triangle_shp_package(request_path, destination)
    except Exception as exc:
        published = False
        if manifest_identity is not None and destination.exists() and not _is_reparse_component(destination):
            try:
                published = _file_identity(destination, directory=True) == stage_identity
            except NativeTriangleSHPError:
                published = False
        if published:
            rollback = _retire_failed_manifest(destination, manifest_bytes, stage_identity, manifest_identity, parent_chain)
            if rollback:
                raise NativeTriangleSHPError(
                    f"SHP export failed: {exc}; rollback preserved concurrent/replaced content: {'; '.join(rollback)}"
                ) from exc
            raise NativeTriangleSHPError(
                f"SHP export failed: {exc}; the owned success marker was safely retired from {destination}"
            ) from exc
        elif stage.exists() or stage.is_symlink():
            if stage.is_symlink() or _is_reparse_component(stage):
                location = f"failed staging path is now a symlink/reparse point; no cleanup was attempted: {stage}"
            elif not _parent_chain_matches(parent_chain):
                location = f"failed staging parent chain changed; no cleanup was attempted and the stage was preserved at {stage}"
            else:
                try:
                    current_stage_identity = _file_identity(stage, directory=True)
                except NativeTriangleSHPError as identity_error:
                    location = f"failed staging path could not be safely identified and was preserved at {stage}: {identity_error}"
                else:
                    if current_stage_identity == stage_identity:
                        marker_notes: list[str] = []
                        if manifest_identity is not None:
                            marker_notes = _retire_failed_manifest(
                                stage, manifest_bytes, stage_identity, manifest_identity, parent_chain,
                            )
                        if marker_notes:
                            location = (
                                f"failed staging directory was preserved at {stage}; "
                                f"rollback preserved concurrent marker/content: {'; '.join(marker_notes)}"
                            )
                        elif manifest_identity is not None:
                            location = f"failed staging directory was preserved at {stage}; its owned success marker was safely retired"
                        else:
                            location = f"failed staging directory was preserved for inspection at {stage}"
                    else:
                        location = f"staging path now names a replaced directory; no cleanup was attempted and its contents were preserved at {stage}"
            raise NativeTriangleSHPError(f"SHP export failed: {exc}; {location}") from exc
        else:
            if destination.exists():
                location = f"published output root with an unowned/replaced identity remains at {destination}"
            else:
                location = f"published output root disappeared or moved from {destination}; its current location is unverified"
            raise NativeTriangleSHPError(f"SHP export failed: {exc}; {location}") from exc


def package_schema() -> dict[str, Any]:
    return {
        "format": "native-triangle-shp-schema-set-v1",
        "request_schema": NativeTriangleSHPExportRequest.model_json_schema(),
        "manifest_schema": NativeTriangleSHPManifest.model_json_schema(),
        "validation_report_schema": NativeTriangleSHPValidationReport.model_json_schema(),
    }


def source_package_schema() -> dict[str, Any]:
    """Return the independent version-2 source-result request and manifest schema set."""
    return {
        "format": "native-triangle-shp-schema-set-v2",
        "request_schema": NativeSourceTriangleSHPExportRequest.model_json_schema(),
        "manifest_schema": NativeSourceTriangleSHPManifest.model_json_schema(),
        "validation_report_schema": NativeTriangleSHPValidationReport.model_json_schema(),
    }


def _request_format(request_path: str | Path) -> str:
    candidate = Path(os.path.abspath(request_path))
    if _is_reparse_component(candidate):
        raise NativeTriangleSHPError(f"request path must not traverse a symlink or reparse point: {candidate}")
    data = _strict_yaml(_regular_bytes(candidate.resolve(), "SHP export request"), "SHP export request")
    if not isinstance(data, dict) or not isinstance(data.get("format"), str):
        raise NativeTriangleSHPError("SHP export request must declare a versioned format")
    return data["format"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hydrodynamics-triangle-shp", description="Export or validate one feature per face from an existing validated native triangular P4 result.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="print versioned request, manifest, and validation schemas")
    commands.add_parser("schema-v2", help="print source-result v2 request, manifest, and validation schemas")
    export = commands.add_parser("export", help="export an existing validated native triangular result")
    export.add_argument("--request", required=True)
    validate = commands.add_parser("validate", help="independently validate an SHP package against its original P2/P3-A and P4 source graph")
    validate.add_argument("--request", required=True)
    validate.add_argument("--package-directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "schema":
            print(json.dumps(package_schema(), ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.command == "schema-v2":
            print(json.dumps(source_package_schema(), ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.command == "export":
            if _request_format(args.request) == _SOURCE_SHP_REQUEST_FORMAT:
                report = export_native_source_triangle_shp(args.request)
            else:
                report = export_native_triangle_shp(args.request)
        else:
            if _request_format(args.request) == _SOURCE_SHP_REQUEST_FORMAT:
                report = validate_native_source_triangle_shp_package(args.request, args.package_directory)
            else:
                report = validate_native_triangle_shp_package(args.request, args.package_directory)
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (NativeTriangleSHPError, ValueError, OSError, ValidationError, RuntimeError, ImportError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "NativeTriangleSHPError", "NativeTriangleSHPExportRequest", "NativeTriangleSHPManifest",
    "NativeTriangleSHPValidationReport", "NativeSourceTriangleSHPExportRequest",
    "NativeSourceTriangleSHPManifest", "NativeTriangleSHPSourcesSummary",
    "NativeTriangleSHPFrameBudget", "NativeTriangleSHPSourceArtifact",
    "export_native_triangle_shp", "export_native_source_triangle_shp", "main", "package_schema",
    "source_package_schema", "validate_native_triangle_shp_package",
    "validate_native_source_triangle_shp_package",
]
