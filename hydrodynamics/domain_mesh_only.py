"""Restricted, preparation-only D−S mesh generation from trusted geometry sources.

The module has no terrain/DEM imports. Heavy geometry parsing, Gmsh loading,
generation, auditing, writing, and semantic readback are entered only from the
restricted worker after its Job Object membership has been checked.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import heapq
import importlib.metadata
import json
import math
import os
from pathlib import Path, PureWindowsPath
import platform
import re
import struct
import sys
import time
from typing import Any, Mapping, Sequence


DOMAIN_MESH_ONLY_FORMAT = "triangle-domain-mesh-only-config-v1"
DOMAIN_MESH_ONLY_VERSION = "1.0"
DOMAIN_MESH_ONLY_ALGORITHM_PLAN_VERSION = "1.1"
DOMAIN_MESH_ONLY_ROUTE = "triangle-domain-mesh-only-generate-v1"
DOMAIN_SOURCE_MANIFEST_FORMAT = "triangle-domain-mesh-source-manifest-v1"
DOMAIN_SOURCE_MANIFEST_VERSION = "1.0"
DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION = "1.1"
DOMAIN_D_EXTERIOR_LINE_FORMAT = "derived_domain_D_exterior_line_v1"
DOMAIN_D_EXTERIOR_DERIVATION_RULE = "exact_parent_D_union_exterior_xy_v1"
DOMAIN_D_EXTERIOR_Q_BINDING = "NONE"
DOMAIN_D_EXTERIOR_GEOMETRY_HASH_FORMAT = "triangle-domain-D-exterior-xy-m-f64le-v1"
DOMAIN_D_EXTERIOR_PROVENANCE_FORMAT = "triangle-domain-D-exterior-provenance-v1"
DOMAIN_D_EXTERIOR_AUDIT_FORMAT = "triangle-domain-mesh-only-source-audit-v2"
MAX_DERIVED_LINE_POINTS = 250_000
DOMAIN_MESH_ARTIFACT_FORMAT = "triangle-domain-mesh-only-artifact-v1"
DOMAIN_MESH_AUDIT_FORMAT = "triangle-domain-mesh-only-source-audit-v1"
DOMAIN_MESH_BUNDLE_FORMAT = "triangle-domain-mesh-only-bundle-v1"
DOMAIN_MESH_READY_FORMAT = "triangle-domain-mesh-only-ready-v1"
CHILD_POLICY = "job_object_inheritance_without_breakaway"
MAX_SOURCE_MANIFEST_BYTES = 1024 * 1024
MAX_COMPONENT_FILE_BYTES = 4 * 1024 * 1024
MAX_COMPONENT_TOTAL_BYTES = 16 * 1024 * 1024
MAX_COMPONENT_COUNT = 8
MAX_SOURCE_FILE_COUNT = 40
QUALITY_FAILURE_PRIMARY_FORMAT = "triangle-mesh-quality-failure-primary-v1"
QUALITY_FAILURE_DIAGNOSTIC_FORMAT = "triangle-mesh-quality-failure-diagnostic-v1"
QUALITY_FAILURE_DIAGNOSTIC_SCHEMA_VERSION = "1.0"
QUALITY_FAILURE_DIAGNOSTIC_RELATIVE_PATH = "diagnostics/domain-mesh-quality-failure.json"
QUALITY_FAILURE_DIAGNOSTIC_V2_FORMAT = "triangle-mesh-quality-failure-diagnostic-v2"
QUALITY_FAILURE_DIAGNOSTIC_V2_SCHEMA_VERSION = "2.0"
QUALITY_FAILURE_DIAGNOSTIC_V2_RELATIVE_PATH = "diagnostics/domain-mesh-quality-failure-v2.json"
QUALITY_FAILURE_DIAGNOSTIC_MAX_BYTES = 64 * 1024
QUALITY_FAILURE_DIAGNOSTIC_TOP_K = 16
QUALITY_FAILURE_HISTOGRAM_BIN_COUNT = 1024
QUALITY_FAILURE_QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90, 0.99)
QUALITY_FAILURE_PENDING_RELATIVE_PATH = "scratch/quality-failure-pending.json"
QUALITY_FAILURE_PENDING_MAX_BYTES = 4096
QUALITY_FAILURE_METRIC_ID = "triangle-face-quality-q4sqrt3-area-over-edge-square-sum-v1"
MAX_OUTPUT_RECEIPT_BYTES = 64 * 1024
OUTPUT_RESERVE_BYTES = 96 * 1024
GMSH_VERSION_PATTERN = r"^4\.(?:1[1-9]|[2-9][0-9]|[1-9][0-9]{2,})\.(?:0|[1-9][0-9]*)$"
RESOURCE_LIMITS = {
    "max_workers": 1,
    "job_memory_limit_mib": 2048,
    "wall_clock_limit_s": 900.0,
    "output_budget_bytes": 1_073_741_824,
    "scratch_budget_bytes": 16_777_216,
    "child_process_policy": CHILD_POLICY,
}
_PROJECTED_PARAMETER_CONTRACTS = {
    ("EPSG", "9807"): ("transverse mercator", {
        "latitude of natural origin": "angular",
        "longitude of natural origin": "angular",
        "scale factor at natural origin": "scale",
        "false easting": "linear",
        "false northing": "linear",
    }),
}


class DomainMeshOnlyError(ValueError):
    """The synthetic geometry mesh-only input or diagnostic is invalid."""


_SEMANTIC_CONTEXT_SEAL = object()


@dataclass(frozen=True)
class _WorkerSemanticContext:
    seal: object
    process_id: int
    bundle_path: Path
    config_sha256: str
    source_manifest_sha256: str


@dataclass(frozen=True)
class _GeometryContext:
    manifest_path: Path
    manifest_sha256: str
    manifest_stat: Mapping[str, int]
    manifest: Mapping[str, Any]
    components_before: tuple[Mapping[str, Any], ...]
    components_after: tuple[Mapping[str, Any], ...]
    domain_component: Mapping[str, Any]
    security_component: Mapping[str, Any]
    line_components: tuple[Mapping[str, Any], ...]
    domain_geometry: Any
    domain_load: Any
    security_geometry: Any
    security_load: Any
    security_assessment: Any
    gmsh_config: Any
    generation_settings: Mapping[str, Any]


@dataclass(frozen=True)
class _PolygonComponent:
    source_union: Any
    source_feature_indices: tuple[int, ...]
    source_geometry_sha256: str
    source_union_geometry_sha256: str
    source_crs: Any
    target_crs: Any
    source_file_sha256: str
    source_crs_raw_text: str
    declared_source_crs_text: str
    operation_identity: Mapping[str, Any] | None
    source_dimension_identity: Mapping[str, Any] | None = None


def _is_strict_int(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _is_finite_number(value: Any, low: float, high: float, *, low_inclusive: bool = True) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return False
    number = float(value)
    return (low <= number if low_inclusive else low < number) and number <= high


def _safe_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError(f"{label} must be a non-empty project-relative path")
    path, win = Path(value), PureWindowsPath(value)
    if (path.is_absolute() or win.is_absolute() or win.drive or win.root
            or ".." in path.parts or ".." in win.parts):
        raise ValueError(f"{label} must remain under the project root")
    return value


def _validate_sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _validate_stat_identity(value: Any, label: str) -> None:
    expected = {"size_bytes", "mtime_ns", "ctime_ns", "device", "file_id"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} must contain the frozen file-stat identity fields")
    if any(type(item) is not int or item < 0 for item in value.values()):
        raise ValueError(f"{label} fields must be strict non-negative integers")


def _complete_projected_parameter_signature(crs: Any) -> tuple[Any, ...] | None:
    """Require the complete, unit-checked parameter contract for a supported method."""
    if (not getattr(crs, "is_projected", False)
            or getattr(crs, "is_bound", False)
            or getattr(crs, "is_compound", False)):
        return None
    operation = getattr(crs, "coordinate_operation", None)
    if operation is None or getattr(operation, "type_name", None) != "Conversion":
        return None
    authority = getattr(operation, "method_auth_name", None)
    code = getattr(operation, "method_code", None)
    if not isinstance(authority, str) or code is None:
        return None
    contract = _PROJECTED_PARAMETER_CONTRACTS.get((authority.upper(), str(code)))
    if contract is None:
        return None
    expected_method, expected_parameters = contract
    if (not isinstance(getattr(operation, "method_name", None), str)
            or operation.method_name.casefold() != expected_method):
        return None
    parameters = tuple(getattr(operation, "params", ()))
    if len(parameters) != len(expected_parameters):
        return None
    normalized: dict[str, float] = {}
    for parameter in parameters:
        name = getattr(parameter, "name", None)
        category = getattr(parameter, "unit_category", None)
        unit_name = getattr(parameter, "unit_name", None)
        factor = getattr(parameter, "unit_conversion_factor", None)
        value = getattr(parameter, "value", None)
        if (not isinstance(name, str) or not name.strip()
                or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not isinstance(unit_name, str) or not unit_name.strip()
                or isinstance(factor, bool) or not isinstance(factor, (int, float))
                or not math.isfinite(float(factor)) or float(factor) <= 0):
            return None
        key = name.strip().casefold()
        expected_category = expected_parameters.get(key)
        if (expected_category is None or category != expected_category or key in normalized):
            return None
        normalized[key] = float(value) * float(factor)
    if set(normalized) != set(expected_parameters):
        return None
    return (authority.upper(), str(code), expected_method, tuple(sorted(normalized.items())))


def _crs_equivalent(left: Any, right: Any) -> bool:
    """Compare CRS semantics under this adapter's fixed XY coordinate-array policy.

    Axis order is ignored because vector coordinate arrays are interpreted as XY
    and every source-to-target operation uses ``always_xy=True``. All CRS
    parameters remain significant; authority identifiers and EPSG guesses are
    not evidence of equivalence. The explicit projected-CRS comparison covers
    lossless Shapefile WKT1 versus WKT2 metadata differences by comparing the
    geodetic CRS, projection method, every parameter with its unit, and the
    projected axes under the same fixed XY policy. A projected method's full
    required parameter set and units are checked before PyProj's equality fast
    path; omitted parameters never inherit operation defaults.
    """
    left_projected = bool(getattr(left, "is_projected", False))
    right_projected = bool(getattr(right, "is_projected", False))
    left_parameters = _complete_projected_parameter_signature(left) if left_projected else None
    right_parameters = _complete_projected_parameter_signature(right) if right_projected else None
    if ((left_projected and left_parameters is None)
            or (right_projected and right_parameters is None)):
        return False
    if left_projected and right_projected and left_parameters != right_parameters:
        return False
    if left.equals(right, ignore_axis_order=True):
        return True
    if not (left_projected and right_projected):
        return False

    left_geodetic, right_geodetic = left.geodetic_crs, right.geodetic_crs
    if (left_geodetic is None or right_geodetic is None
            or not left_geodetic.equals(right_geodetic, ignore_axis_order=True)):
        return False

    left_axes, right_axes = left.axis_info, right.axis_info
    if len(left_axes) != 2 or len(right_axes) != 2:
        return False

    def axis_signature(axis: Any) -> tuple[str, str, float] | None:
        direction = getattr(axis, "direction", None)
        unit_name = getattr(axis, "unit_name", None)
        factor = getattr(axis, "unit_conversion_factor", None)
        if (not isinstance(direction, str) or not isinstance(unit_name, str)
                or isinstance(factor, bool) or not isinstance(factor, (int, float))
                or not math.isfinite(float(factor)) or float(factor) <= 0):
            return None
        return direction.casefold(), unit_name.casefold(), float(factor)

    left_axis_items = [axis_signature(axis) for axis in left_axes]
    right_axis_items = [axis_signature(axis) for axis in right_axes]
    if any(item is None for item in left_axis_items + right_axis_items):
        return False
    left_axis_signature = tuple(sorted(left_axis_items))
    right_axis_signature = tuple(sorted(right_axis_items))
    if left_axis_signature != right_axis_signature:
        return False

    return left_parameters is not None and left_parameters == right_parameters


def validate_domain_mesh_only_config_shape(config: Any) -> dict[str, Any]:
    """Strict input gate for the separate D−S generate-only route."""
    if not isinstance(config, dict):
        raise ValueError("domain mesh-only config root must be an object")
    base_fields = {"format", "schema_version", "run_id", "output_root", "resources",
                   "source_manifest", "target_coordinate_system", "mesh_generation",
                   "expected_gmsh_version", "expected_gmsh_library_sha256"}
    schema_version = config.get("schema_version")
    if schema_version == DOMAIN_MESH_ONLY_VERSION:
        fields = base_fields
    elif schema_version == DOMAIN_MESH_ONLY_ALGORITHM_PLAN_VERSION:
        fields = base_fields | {"mesh_algorithm_plan"}
    else:
        raise ValueError("unsupported domain mesh-only config schema_version")
    if set(config) != fields:
        raise ValueError("domain mesh-only config fields differ from its frozen contract")
    if config["format"] != DOMAIN_MESH_ONLY_FORMAT:
        raise ValueError("unsupported domain mesh-only config format/version")
    if schema_version == DOMAIN_MESH_ONLY_ALGORITHM_PLAN_VERSION:
        from .mesh_algorithm_plan import validate_mesh_algorithm_plan_shape
        validate_mesh_algorithm_plan_shape(config["mesh_algorithm_plan"])
    if (not isinstance(config["run_id"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", config["run_id"]) is None):
        raise ValueError("run_id must be a 1–64 character safe identifier")
    _safe_relative(config["output_root"], "output_root")
    if config["resources"] != RESOURCE_LIMITS:
        raise ValueError("domain mesh-only resources are fixed at 1 worker/2048 MiB/900 s/1 GiB output/16 MiB scratch")
    source = config["source_manifest"]
    if not isinstance(source, dict) or set(source) != {"path", "sha256", "expected_stat_identity"}:
        raise ValueError("source_manifest fields differ from the frozen contract")
    _safe_relative(source["path"], "source_manifest.path")
    _validate_sha256(source["sha256"], "source_manifest.sha256")
    _validate_stat_identity(source["expected_stat_identity"], "source_manifest.expected_stat_identity")
    target_crs = config["target_coordinate_system"]
    if not isinstance(target_crs, str) or not target_crs.strip():
        raise ValueError("target_coordinate_system must be a non-empty CRS string")
    gmsh_version = config["expected_gmsh_version"]
    if not isinstance(gmsh_version, str) or re.fullmatch(GMSH_VERSION_PATTERN, gmsh_version) is None:
        raise ValueError("expected_gmsh_version must be canonical 4.minor.patch with minor >= 11 and no suffix")
    _validate_sha256(config["expected_gmsh_library_sha256"], "expected_gmsh_library_sha256")
    mesh = config["mesh_generation"]
    mesh_fields = {"maximum_faces", "maximum_nodes", "minimum_face_quality", "geometry_tolerance_m",
                   "minimum_face_area_m2", "default_size_m", "minimum_size_m",
                   "area_abs_tolerance_m2", "area_rel_tolerance"}
    if not isinstance(mesh, dict) or set(mesh) != mesh_fields:
        raise ValueError("mesh_generation fields differ from the frozen domain mesh-only contract")
    if not _is_strict_int(mesh["maximum_faces"], 1, 500_000):
        raise ValueError("mesh_generation.maximum_faces must be an integer in [1, 500000]")
    if not _is_strict_int(mesh["maximum_nodes"], 3, 2_000_000):
        raise ValueError("mesh_generation.maximum_nodes must be an integer in [3, 2000000]")
    if not _is_finite_number(mesh["minimum_face_quality"], 0.0, 1.0):
        raise ValueError("mesh_generation.minimum_face_quality must be a finite number in [0,1]")
    if not _is_finite_number(mesh["geometry_tolerance_m"], 0.0, 1.0, low_inclusive=False):
        raise ValueError("mesh_generation.geometry_tolerance_m must be a finite number in (0,1] m")
    if not _is_finite_number(mesh["minimum_face_area_m2"], 0.0, 100.0, low_inclusive=False):
        raise ValueError("mesh_generation.minimum_face_area_m2 must be a finite number in (0,100] m2")
    if not _is_finite_number(mesh["default_size_m"], 0.0, 10_000.0, low_inclusive=False):
        raise ValueError("mesh_generation.default_size_m must be a finite number in (0,10000] m")
    if not _is_finite_number(mesh["minimum_size_m"], 0.0, 10_000.0, low_inclusive=False):
        raise ValueError("mesh_generation.minimum_size_m must be a finite number in (0,10000] m")
    if float(mesh["minimum_size_m"]) <= float(mesh["geometry_tolerance_m"]):
        raise ValueError("mesh_generation.minimum_size_m must exceed geometry_tolerance_m")
    if float(mesh["minimum_size_m"]) > float(mesh["default_size_m"]):
        raise ValueError("mesh_generation.minimum_size_m must not exceed default_size_m")
    if not _is_finite_number(mesh["area_abs_tolerance_m2"], 1e-12, 1.0):
        raise ValueError("mesh_generation.area_abs_tolerance_m2 must be in [1e-12,1] m2")
    if not _is_finite_number(mesh["area_rel_tolerance"], 1e-12, 1e-3):
        raise ValueError("mesh_generation.area_rel_tolerance must be in [1e-12,1e-3]")
    return config


def validate_source_manifest_shape(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != {
            "format", "schema_version", "source_set_id", "target_coordinate_system", "components"}:
        raise ValueError("source manifest root fields differ from the frozen contract")
    schema_version = manifest["schema_version"]
    if (manifest["format"] != DOMAIN_SOURCE_MANIFEST_FORMAT
            or not isinstance(schema_version, str)
            or schema_version not in {DOMAIN_SOURCE_MANIFEST_VERSION, DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION}):
        raise ValueError("unsupported domain mesh-only source manifest format/version")
    derived_version = schema_version == DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION
    if (not isinstance(manifest["source_set_id"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", manifest["source_set_id"]) is None):
        raise ValueError("source_set_id must be a 1–64 character safe identifier")
    if (not isinstance(manifest["target_coordinate_system"], str)
            or not manifest["target_coordinate_system"].strip()
            or manifest["target_coordinate_system"] != manifest["target_coordinate_system"].strip()):
        raise ValueError("source manifest target_coordinate_system must be a non-empty CRS string")
    components = manifest["components"]
    if not isinstance(components, list) or not 3 <= len(components) <= MAX_COMPONENT_COUNT:
        raise ValueError(f"source manifest components must contain 3–{MAX_COMPONENT_COUNT} records")
    ids: set[str] = set()
    line_names: set[str] = set()
    domain_count = security_count = line_count = 0
    shapefile_roles = {"shp", "shx", "prj", "dbf", "cpg"}
    components_by_id: dict[str, Mapping[str, Any]] = {}
    derived_components: list[Mapping[str, Any]] = []
    for index, component in enumerate(components):
        label = f"components[{index}]"
        if not isinstance(component, dict):
            raise ValueError(f"{label} must be an object")
        source_kind = component.get("source_kind") if derived_version else "file"
        file_fields = {"component_id", "role", "format", "source_crs", "files", "group_name", "closed"}
        if source_kind == "file":
            expected_fields = file_fields | ({"source_kind", "source_version"} if derived_version else set())
        elif source_kind == "derived":
            expected_fields = {
                "component_id", "source_kind", "role", "format", "parent_component_id",
                "group_name", "closed", "geometry_only", "q_binding",
            }
        else:
            raise ValueError(f"{label}.source_kind must select file or derived")
        if set(component) != expected_fields:
            raise ValueError(f"{label} fields differ from the frozen contract")
        component_id = component["component_id"]
        if (not isinstance(component_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", component_id) is None
                or component_id in ids):
            raise ValueError(f"{label}.component_id must be unique and 1–64 safe characters")
        ids.add(component_id)
        components_by_id[component_id] = component
        role, fmt = component["role"], component["format"]
        if not isinstance(role, str) or not isinstance(fmt, str):
            raise ValueError(f"{label}.role and format must be strings")
        is_line = role in {"boundary_line", "constraint_line"}
        if source_kind == "derived":
            derived_components.append(component)
            if role != "boundary_line" or fmt != DOMAIN_D_EXTERIOR_LINE_FORMAT:
                raise ValueError(f"{label} derived source only supports the versioned domain-D exterior boundary_line")
            parent_id = component["parent_component_id"]
            if (not isinstance(parent_id, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", parent_id) is None):
                raise ValueError(f"{label}.parent_component_id must be a safe component ID")
            if not isinstance(component["group_name"], str) or re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", component["group_name"]) is None:
                raise ValueError(f"{label}.group_name must be a unique 1–64 character safe ID")
            if type(component["closed"]) is not bool or component["closed"] is not True:
                raise ValueError(f"{label}.closed must be true for the derived D exterior")
            if type(component["geometry_only"]) is not bool or component["geometry_only"] is not True:
                raise ValueError(f"{label}.geometry_only must be true")
            if component["q_binding"] != DOMAIN_D_EXTERIOR_Q_BINDING:
                raise ValueError(f"{label}.q_binding must be the literal NONE; geometry lines carry no Q")
            if (component["group_name"] in {"outer_boundary", "security_exclusion_boundary"}
                    or re.fullmatch(r"hole_[0-9]+", component["group_name"]) is not None):
                raise ValueError(f"{label}.group_name conflicts with a reserved generated mesh group")
            line_count += 1
            if component["group_name"] in line_names:
                raise ValueError(f"{label}.group_name must be unique among line components")
            line_names.add(component["group_name"])
        else:
            allowed = {"domain_D": {"polygon_geojson", "polygon_shapefile"},
                       "security_S": {"polygon_geojson", "polygon_shapefile"},
                       "boundary_line": {"line_geojson", "line_shapefile"},
                       "constraint_line": {"line_geojson", "line_shapefile"}}
            if not isinstance(role, str) or not isinstance(fmt, str) or role not in allowed or fmt not in allowed[role]:
                raise ValueError(f"{label} role/format pair is unsupported")
            if not isinstance(component["source_crs"], str) or not component["source_crs"].strip():
                raise ValueError(f"{label}.source_crs is required")
            if derived_version:
                source_version = component["source_version"]
                if (not isinstance(source_version, str)
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", source_version) is None):
                    raise ValueError(f"{label}.source_version must be a required 1–64 character safe source-version ID")
            if is_line:
                line_count += 1
                name = component["group_name"]
                if (not isinstance(name, str)
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) is None
                        or name in line_names):
                    raise ValueError(f"{label}.group_name must be a unique 1–64 character safe ID")
                if derived_version and (name in {"outer_boundary", "security_exclusion_boundary"}
                                        or re.fullmatch(r"hole_[0-9]+", name) is not None):
                    raise ValueError(f"{label}.group_name conflicts with a reserved generated mesh group")
                line_names.add(name)
                if type(component["closed"]) is not bool:
                    raise ValueError(f"{label}.closed must be boolean")
                if role == "constraint_line" and component["closed"]:
                    raise ValueError(f"{label} constraint lines must be open paths")
            elif component["group_name"] is not None or component["closed"] is not None:
                raise ValueError(f"{label} polygon components cannot declare line-group fields")
        if role == "domain_D":
            domain_count += 1
        elif role == "security_S":
            security_count += 1
        if source_kind == "file":
            files = component["files"]
            if not isinstance(files, dict) or not files:
                raise ValueError(f"{label}.files must be a non-empty file-identity object")
            if fmt.endswith("shapefile"):
                if not {"shp", "shx", "prj"}.issubset(files) or set(files) - shapefile_roles:
                    raise ValueError(f"{label} Shapefile files require shp/shx/prj and allow only optional dbf/cpg")
                for file_role in files:
                    if not isinstance(file_role, str):
                        raise ValueError(f"{label}.files roles must be strings")
            elif set(files) != {"geometry"}:
                raise ValueError(f"{label} GeoJSON component requires exactly one geometry file")
            for file_role, file_record in files.items():
                file_label = f"{label}.files.{file_role}"
                expected_file_fields = {"path", "sha256", "expected_stat_identity"}
                if derived_version:
                    expected_file_fields.add("origin")
                if not isinstance(file_record, dict) or set(file_record) != expected_file_fields:
                    raise ValueError(f"{file_label} fields differ from the frozen file identity contract")
                _safe_relative(file_record["path"], f"{file_label}.path")
                _validate_sha256(file_record["sha256"], f"{file_label}.sha256")
                _validate_stat_identity(file_record["expected_stat_identity"], f"{file_label}.expected_stat_identity")
                if derived_version:
                    origin = file_record["origin"]
                    if not isinstance(origin, dict) or set(origin) != {"path", "sha256", "expected_stat_identity"}:
                        raise ValueError(f"{file_label}.origin fields differ from the frozen source identity contract")
                    _safe_relative(origin["path"], f"{file_label}.origin.path")
                    _validate_sha256(origin["sha256"], f"{file_label}.origin.sha256")
                    _validate_stat_identity(origin["expected_stat_identity"], f"{file_label}.origin.expected_stat_identity")
                    if (origin["path"].casefold() == file_record["path"].casefold()
                            or origin["sha256"] != file_record["sha256"]
                            or origin["expected_stat_identity"]["size_bytes"]
                            != file_record["expected_stat_identity"]["size_bytes"]):
                        raise ValueError(f"{file_label} must bind a distinct byte-identical original-to-staged source chain")
    if domain_count != 1 or security_count != 1 or line_count < 1:
        raise ValueError("source manifest requires exactly one D, one S, and at least one source boundary/constraint line")
    if len({x["component_id"] for x in components}) != len(components):
        raise ValueError("source manifest component IDs must be unique")
    if len(derived_components) > 1:
        raise ValueError("source manifest supports at most one derived D-exterior boundary component")
    if derived_components:
        if not derived_version:
            raise ValueError("derived components require source manifest schema_version 1.1")
        parent = components_by_id.get(derived_components[0]["parent_component_id"])
        if (parent is None or parent.get("role") != "domain_D"
                or (derived_version and parent.get("source_kind") != "file")):
            raise ValueError("derived D-exterior parent_component_id must identify the unique file-backed domain_D component")
    return manifest


def _component_source_kind(manifest: Mapping[str, Any], component: Mapping[str, Any]) -> str:
    if manifest.get("schema_version") == DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION:
        return str(component.get("source_kind", ""))
    return "file"


def _resolve_component_file(root: Path, manifest_path: Path, value: str, label: str) -> Path:
    from .restricted_worker import _resolve_project_path
    base_relative = manifest_path.parent.relative_to(root)
    relative = (base_relative / Path(_safe_relative(value, label))).as_posix()
    return _resolve_project_path(root, relative, label)


def _component_file_records(root: Path, manifest_path: Path, manifest: Mapping[str, Any], *,
                            verify_sha: bool) -> tuple[dict[str, dict[str, Any]], int]:
    from .restricted_worker import ResourceBudgetError, SourceIdentityError, _is_reparse_or_symlink, _sha256_file, _stat_identity
    records: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for component in manifest["components"]:
        files = component["files"]
        if component["format"].endswith("shapefile"):
            shp = files["shp"]["path"]
            shp_base = Path(shp)
            if shp_base.suffix.lower() != ".shp":
                raise SourceIdentityError(f"{component['component_id']} SHP component path must end in .shp")
            for extension in ("shx", "prj"):
                if Path(files[extension]["path"]).as_posix().lower() != shp_base.with_suffix(f".{extension}").as_posix().lower():
                    raise SourceIdentityError(f"{component['component_id']} {extension.upper()} sidecar must share the SHP basename")
            for extension in ("dbf", "cpg"):
                expected_path = shp_base.with_suffix(f".{extension}")
                actual_path = (manifest_path.parent / expected_path)
                if actual_path.exists() != (extension in files):
                    raise SourceIdentityError(
                        f"{component['component_id']} {extension.upper()} presence must match its declared file identity"
                    )
                if extension in files and Path(files[extension]["path"]).as_posix().lower() != expected_path.as_posix().lower():
                    raise SourceIdentityError(f"{component['component_id']} {extension.upper()} sidecar must share the SHP basename")
        for file_role, record in files.items():
            label = f"{component['component_id']}.{file_role}"
            path = _resolve_component_file(root, manifest_path, record["path"], label)
            if _is_reparse_or_symlink(path) or not path.is_file():
                raise SourceIdentityError(f"source component file must be a regular non-symlink file: {label}")
            before = _stat_identity(path)
            if before != record["expected_stat_identity"]:
                from .restricted_worker import SourceIdentityError
                raise SourceIdentityError(f"source component stat identity differs: {label}")
            if before["size_bytes"] > MAX_COMPONENT_FILE_BYTES:
                raise ResourceBudgetError(f"source component exceeds the fixed {MAX_COMPONENT_FILE_BYTES}-byte cap: {label}")
            total_bytes += before["size_bytes"]
            if total_bytes > MAX_COMPONENT_TOTAL_BYTES:
                raise ResourceBudgetError(f"source components exceed the fixed {MAX_COMPONENT_TOTAL_BYTES}-byte input cap")
            digest = record["sha256"]
            if verify_sha:
                digest, size = _sha256_file(path, maximum_bytes=MAX_COMPONENT_FILE_BYTES)
                if digest != record["sha256"] or size != before["size_bytes"]:
                    from .restricted_worker import SourceIdentityError
                    raise SourceIdentityError(f"source component bytes differ from their manifest identity: {label}")
            after = _stat_identity(path)
            if before != after:
                from .restricted_worker import SourceIdentityError
                raise SourceIdentityError(f"source component changed while its identity was captured: {label}")
            records[f"{component['component_id']}:{file_role}"] = {
                "component_id": component["component_id"], "component_role": component["role"],
                "file_role": file_role, "path": record["path"], "sha256": digest,
                "bytes": before["size_bytes"], "stat_identity": before,
            }
    if len(records) > MAX_SOURCE_FILE_COUNT:
        raise ResourceBudgetError(f"source manifest has more than the fixed {MAX_SOURCE_FILE_COUNT}-file cap")
    return records, total_bytes


def _component_file_records_v11(root: Path, manifest_path: Path, manifest: Mapping[str, Any], *,
                                verify_sha: bool) -> tuple[dict[str, dict[str, Any]], int]:
    """Validate both original-source and staged-copy identities for manifest 1.1."""
    from .restricted_worker import (
        ResourceBudgetError, SourceIdentityError, _is_reparse_or_symlink,
        _sha256_file, _stat_identity,
    )
    records: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    total_files = 0
    seen_paths: set[str] = set()
    seen_physical_files: dict[tuple[int, int], str] = {}

    def capture(component: Mapping[str, Any], file_role: str, record: Mapping[str, Any],
                identity_kind: str) -> dict[str, Any]:
        nonlocal total_bytes, total_files
        label = f"{component['component_id']}.{file_role} {identity_kind}"
        path = _resolve_component_file(root, manifest_path, record["path"], label)
        if _is_reparse_or_symlink(path) or not path.is_file():
            raise SourceIdentityError(f"source component file must be a regular non-symlink file: {label}")
        resolved_key = str(path).casefold()
        if resolved_key in seen_paths:
            raise SourceIdentityError(f"source identity chain must not reuse a physical file: {label}")
        seen_paths.add(resolved_key)
        before = _stat_identity(path)
        device = before.get("device")
        file_id = before.get("file_id")
        if (type(device) is not int or device < 0
                or type(file_id) is not int or file_id <= 0):
            raise SourceIdentityError(
                f"SOURCE_PHYSICAL_IDENTITY_UNAVAILABLE: {label} requires a non-negative device and positive file_id"
            )
        if before != record["expected_stat_identity"]:
            raise SourceIdentityError(f"source component stat identity differs: {label}")
        physical_key = (device, file_id)
        prior_label = seen_physical_files.get(physical_key)
        if prior_label is not None:
            raise SourceIdentityError(
                f"SOURCE_PHYSICAL_IDENTITY_REUSED: {label} aliases the same physical file as {prior_label}"
            )
        seen_physical_files[physical_key] = label
        if before["size_bytes"] > MAX_COMPONENT_FILE_BYTES:
            raise ResourceBudgetError(f"source component exceeds the fixed {MAX_COMPONENT_FILE_BYTES}-byte cap: {label}")
        total_bytes += before["size_bytes"]
        total_files += 1
        if total_bytes > MAX_COMPONENT_TOTAL_BYTES:
            raise ResourceBudgetError(f"original plus staged source components exceed the fixed {MAX_COMPONENT_TOTAL_BYTES}-byte input cap")
        if total_files > MAX_SOURCE_FILE_COUNT:
            raise ResourceBudgetError(f"source manifest has more than the fixed {MAX_SOURCE_FILE_COUNT}-file cap")
        digest = record["sha256"]
        if verify_sha:
            digest, size = _sha256_file(path, maximum_bytes=MAX_COMPONENT_FILE_BYTES)
            if digest != record["sha256"] or size != before["size_bytes"]:
                raise SourceIdentityError(f"source component bytes differ from their manifest identity: {label}")
        after = _stat_identity(path)
        if before != after:
            raise SourceIdentityError(f"source component changed while its identity was captured: {label}")
        return {
            "component_id": component["component_id"], "component_role": component["role"],
            "file_role": file_role, "identity_kind": identity_kind,
            "path": record["path"], "sha256": digest,
            "bytes": before["size_bytes"], "stat_identity": before,
        }

    for component in manifest["components"]:
        if component["source_kind"] == "derived":
            continue
        files = component["files"]
        if component["format"].endswith("shapefile"):
            staged_shp = Path(files["shp"]["path"])
            origin_shp = Path(files["shp"]["origin"]["path"])
            if staged_shp.suffix.lower() != ".shp" or origin_shp.suffix.lower() != ".shp":
                raise SourceIdentityError(f"{component['component_id']} staged and original SHP paths must end in .shp")
            for base, is_origin in ((staged_shp, False), (origin_shp, True)):
                for extension in ("shx", "prj"):
                    actual_record = files[extension]["origin"] if is_origin else files[extension]
                    if Path(actual_record["path"]).as_posix().lower() != base.with_suffix(f".{extension}").as_posix().lower():
                        kind = "original " if is_origin else "staged "
                        raise SourceIdentityError(f"{component['component_id']} {kind}{extension.upper()} sidecar must share the SHP basename")
                for extension in ("dbf", "cpg"):
                    expected_path = base.with_suffix(f".{extension}")
                    actual_path = manifest_path.parent / expected_path
                    if actual_path.exists() != (extension in files):
                        kind = "original " if is_origin else "staged "
                        raise SourceIdentityError(
                            f"{component['component_id']} {kind}{extension.upper()} presence must match its declared file identity"
                        )
                    actual_record = files[extension]["origin"] if is_origin and extension in files else files.get(extension)
                    if extension in files and Path(actual_record["path"]).as_posix().lower() != base.with_suffix(f".{extension}").as_posix().lower():
                        kind = "original " if is_origin else "staged "
                        raise SourceIdentityError(f"{component['component_id']} {kind}{extension.upper()} sidecar must share the SHP basename")
            for file_role, record in files.items():
                if Path(record["path"]).name.casefold() != Path(record["origin"]["path"]).name.casefold():
                    raise SourceIdentityError(f"{component['component_id']} original and staged sidecar basenames differ: {file_role}")

        for file_role, record in files.items():
            origin = record["origin"]
            if (origin["sha256"] != record["sha256"]
                    or origin["expected_stat_identity"]["size_bytes"]
                    != record["expected_stat_identity"]["size_bytes"]):
                raise SourceIdentityError(f"{component['component_id']}.{file_role} original and staged identities are not byte-equivalent")
            original_record = capture(component, file_role, origin, "original_source")
            staged_record = capture(component, file_role, record, "staged_copy")
            records[f"{component['component_id']}:origin:{file_role}"] = original_record
            records[f"{component['component_id']}:{file_role}"] = staged_record
    return records, total_bytes


def _component_file_records_for_manifest(root: Path, manifest_path: Path,
                                         manifest: Mapping[str, Any], *,
                                         verify_sha: bool) -> tuple[dict[str, dict[str, Any]], int]:
    if manifest["schema_version"] == DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION:
        return _component_file_records_v11(root, manifest_path, manifest, verify_sha=verify_sha)
    return _component_file_records(root, manifest_path, manifest, verify_sha=verify_sha)


def inspect_source_manifest_identity(config: Mapping[str, Any], project_root: str | Path) -> dict[str, Any]:
    """Supervisor-safe identity preflight; reads only the bounded manifest and file stats."""
    from .restricted_worker import SourceIdentityError, _is_reparse_or_symlink, _resolve_project_path, _sha256_file, _stat_identity
    root = Path(project_root).resolve()
    validate_domain_mesh_only_config_shape(dict(config))
    source = config["source_manifest"]
    path = _resolve_project_path(root, source["path"], "source_manifest.path")
    if _is_reparse_or_symlink(path) or not path.is_file() or path.stat().st_size > MAX_SOURCE_MANIFEST_BYTES:
        raise SourceIdentityError("source manifest must be a regular file within the 1 MiB identity cap")
    before = _stat_identity(path)
    if before != source["expected_stat_identity"]:
        raise SourceIdentityError("source manifest stat identity differs from trusted config")
    digest, size = _sha256_file(path, maximum_bytes=MAX_SOURCE_MANIFEST_BYTES)
    if digest != source["sha256"] or size != before["size_bytes"]:
        raise SourceIdentityError("source manifest SHA-256 differs from trusted config")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainMeshOnlyError(f"source manifest JSON is malformed: {exc}") from exc
    try:
        validate_source_manifest_shape(manifest)
    except (TypeError, ValueError, KeyError) as exc:
        raise SourceIdentityError(f"source manifest contract is invalid: {exc}") from exc
    if manifest["target_coordinate_system"] != config["target_coordinate_system"]:
        raise DomainMeshOnlyError("source manifest target CRS differs from trusted config")
    file_stats, total_bytes = _component_file_records_for_manifest(root, path, manifest, verify_sha=False)
    after = _stat_identity(path)
    if before != after:
        raise SourceIdentityError("source manifest changed while its identity was captured")
    return {"manifest_path": path, "manifest_sha256": digest, "manifest_stat_identity": before,
            "manifest": manifest, "component_file_records": file_stats, "component_total_bytes": total_bytes}


def _read_line_geometry(component: Mapping[str, Any], manifest_path: Path, root: Path, target_crs: Any) -> Any:
    from pyproj import CRS, Transformer
    from shapely.geometry import shape
    from shapely.ops import transform as transform_geometry
    from .restricted_worker import _resolve_project_path

    files = component["files"]
    if component["format"] == "line_geojson":
        geometry_record = files["geometry"]
        manifest_base = manifest_path.parent.relative_to(root)
        geometry_path = _resolve_project_path(root, (manifest_base / geometry_record["path"]).as_posix(),
                                              f"{component['component_id']}.geometry")
        try:
            payload = json.loads(geometry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainMeshOnlyError(f"{component['component_id']} line GeoJSON is malformed: {exc}") from exc
        if not isinstance(payload, dict):
            raise DomainMeshOnlyError(f"{component['component_id']} line GeoJSON root must be an object")
        declared_crs = None
        if "crs" in payload:
            crs_obj = payload["crs"]
            properties = crs_obj.get("properties") if isinstance(crs_obj, dict) else None
            if not isinstance(crs_obj, dict) or not isinstance(properties, dict):
                raise DomainMeshOnlyError(f"{component['component_id']} GeoJSON CRS declaration is malformed")
            ctype = str(crs_obj.get("type", "")).strip().lower()
            if ctype == "name" and isinstance(properties.get("name"), str):
                declared_crs = properties["name"]
            elif ctype == "epsg" and isinstance(properties.get("code"), (str, int)) and not isinstance(properties.get("code"), bool):
                declared_crs = f"EPSG:{properties['code']}"
            else:
                raise DomainMeshOnlyError(f"{component['component_id']} GeoJSON CRS declaration is unsupported")
        geometry_payload = payload
        if payload.get("type") == "FeatureCollection":
            features = payload.get("features")
            if not isinstance(features, list) or len(features) != 1 or not isinstance(features[0], dict):
                raise DomainMeshOnlyError(f"{component['component_id']} line GeoJSON must contain exactly one feature")
            geometry_payload = features[0].get("geometry")
        elif payload.get("type") == "Feature":
            geometry_payload = payload.get("geometry")
        if not isinstance(geometry_payload, dict):
            raise DomainMeshOnlyError(f"{component['component_id']} GeoJSON feature must contain a geometry")
        geometry = shape(geometry_payload)
        source_crs = CRS.from_user_input(component["source_crs"])
        if declared_crs is not None and not _crs_equivalent(CRS.from_user_input(declared_crs), source_crs):
            raise DomainMeshOnlyError(f"{component['component_id']} GeoJSON CRS differs from its explicit source_crs")
    else:
        import shapefile
        from shapely.geometry import shape as shape_geometry
        manifest_base = manifest_path.parent.relative_to(root)
        shp_path = _resolve_project_path(root, (manifest_base / files["shp"]["path"]).as_posix(),
                                         f"{component['component_id']}.shp")
        shx_path = _resolve_project_path(root, (manifest_base / files["shx"]["path"]).as_posix(),
                                         f"{component['component_id']}.shx")
        prj_path = _resolve_project_path(root, (manifest_base / files["prj"]["path"]).as_posix(),
                                         f"{component['component_id']}.prj")
        try:
            with shp_path.open("rb") as shp_stream, shx_path.open("rb") as shx_stream:
                reader = shapefile.Reader(shp=shp_stream, shx=shx_stream)
                try:
                    if reader.shapeType not in {shapefile.POLYLINE, getattr(shapefile, "POLYLINEM", -1),
                                                getattr(shapefile, "POLYLINEZ", -1)}:
                        raise DomainMeshOnlyError(f"{component['component_id']} SHP shapeType must be a line")
                    records = reader.shapes()
                finally:
                    reader.close()
            if len(records) != 1:
                raise DomainMeshOnlyError(f"{component['component_id']} line Shapefile must contain exactly one shape record")
            geometry = shape_geometry(records[0].__geo_interface__)
            prj_crs = CRS.from_wkt(prj_path.read_text(encoding="utf-8"))
            source_crs = CRS.from_user_input(component["source_crs"])
            if not _crs_equivalent(prj_crs, source_crs):
                raise DomainMeshOnlyError(f"{component['component_id']} explicit source_crs differs from its PRJ")
        except DomainMeshOnlyError:
            raise
        except Exception as exc:
            raise DomainMeshOnlyError(f"{component['component_id']} line Shapefile could not be read: {exc}") from exc
    if geometry.geom_type != "LineString" or geometry.is_empty or not geometry.is_valid:
        raise DomainMeshOnlyError(
            f"UNSUPPORTED_MULTIPART_LINE: {component['component_id']} requires exactly one valid LineString; got {geometry.geom_type}"
        )
    points = tuple((float(x), float(y)) for x, y, *_rest in geometry.coords)
    if len(points) < 2 or any(not math.isfinite(v) for point in points for v in point):
        raise DomainMeshOnlyError(f"{component['component_id']} line coordinates must be finite with at least two points")
    if len(set(points)) < 2 or float(geometry.length) <= 0:
        raise DomainMeshOnlyError(f"{component['component_id']} line must have positive length")
    if not _crs_equivalent(source_crs, target_crs):
        transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
        geometry = transform_geometry(transformer.transform, geometry)
    if geometry.geom_type != "LineString" or geometry.is_empty or not geometry.is_valid:
        raise DomainMeshOnlyError(f"{component['component_id']} projected line geometry is invalid")
    return geometry


def _crs_runtime_identity() -> dict[str, Any]:
    import pyproj
    from pyproj import database, network

    network.set_network_enabled(False)
    try:
        pyshp_version = importlib.metadata.version("pyshp")
    except importlib.metadata.PackageNotFoundError as exc:
        raise DomainMeshOnlyError("CRS_OPERATION_EVIDENCE_UNAVAILABLE: PyShp dependency identity is missing") from exc
    metadata = {
        "epsg_database_version": database.get_database_metadata("EPSG.VERSION"),
        "epsg_database_date": database.get_database_metadata("EPSG.DATE"),
    }
    if (not isinstance(pyproj.__version__, str) or not pyproj.__version__
            or not isinstance(pyshp_version, str) or not pyshp_version
            or not isinstance(pyproj.proj_version_str, str) or not pyproj.proj_version_str
            or any(not isinstance(value, str) or not value for value in metadata.values())):
        raise DomainMeshOnlyError("CRS_OPERATION_EVIDENCE_UNAVAILABLE: PyProj/PROJ/database identity is incomplete")
    return {
        "pyproj_version": pyproj.__version__, "proj_version": pyproj.proj_version_str,
        "pyshp_version": pyshp_version, "python_version": platform.python_version(),
        **metadata, "proj_network_enabled": bool(network.is_network_enabled()),
    }


def _coordinate_operation_chain(used_operation: Any) -> list[dict[str, Any]]:
    operations = getattr(used_operation, "operations", None)
    if not isinstance(operations, (tuple, list)) or not operations:
        raise DomainMeshOnlyError("CRS_OPERATION_EVIDENCE_UNAVAILABLE: selected operation chain has no CoordinateOperation records")
    chain: list[dict[str, Any]] = []
    for index, operation in enumerate(operations):
        ballpark = getattr(operation, "has_ballpark_transformation", None)
        if type(ballpark) is not bool:
            raise DomainMeshOnlyError(
                f"CRS_OPERATION_EVIDENCE_UNAVAILABLE: operation-chain item {index} has no explicit ballpark flag"
            )
        if ballpark:
            raise DomainMeshOnlyError(f"BALLPARK_TRANSFORMATION_REJECTED: operation-chain item {index} is ballpark")
        try:
            operation_json = operation.to_json_dict()
            operation_wkt = operation.to_wkt(pretty=False)
        except Exception as exc:
            raise DomainMeshOnlyError(f"CRS_OPERATION_EVIDENCE_UNAVAILABLE: operation-chain item {index} cannot be serialized") from exc
        if not isinstance(operation_json, dict) or not isinstance(operation_wkt, str) or not operation_wkt:
            raise DomainMeshOnlyError(f"CRS_OPERATION_EVIDENCE_UNAVAILABLE: operation-chain item {index} is incomplete")
        accuracy = getattr(operation, "accuracy", None)
        if isinstance(accuracy, bool) or not isinstance(accuracy, (int, float)) or not math.isfinite(float(accuracy)):
            raise DomainMeshOnlyError(f"CRS_OPERATION_EVIDENCE_UNAVAILABLE: operation-chain item {index} has invalid accuracy metadata")
        chain.append({
            "name": getattr(operation, "name", None),
            "method_name": getattr(operation, "method_name", None),
            "method_auth_name": getattr(operation, "method_auth_name", None),
            "method_code": getattr(operation, "method_code", None),
            "accuracy_m": float(accuracy),
            "has_ballpark_transformation": ballpark,
            "definition": getattr(operation, "definition", None),
            "wkt": operation_wkt,
            "projjson": operation_json,
        })
    return chain


def _project_polygon_features(geometries: tuple[Any, ...], source_crs: Any, target_crs: Any, *,
                              source_crs_raw_text: str,
                              declared_source_crs_text: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
    from pyproj import Transformer
    from pyproj.transformer import TransformerGroup
    import shapely
    from pyproj import network
    from shapely.ops import transform as transform_geometry

    runtime = _crs_runtime_identity()
    base: dict[str, Any] = {
        "source_crs_raw_text": source_crs_raw_text,
        "declared_source_crs_text": declared_source_crs_text,
        "source_crs_wkt2": source_crs.to_wkt(version="WKT2_2019", pretty=False),
        "target_crs": target_crs.to_string(),
        "target_crs_wkt2": target_crs.to_wkt(version="WKT2_2019", pretty=False),
        "coordinate_array_axis_policy": "always_xy",
        "allow_ballpark": False,
        "only_best": True,
        "shapely_version": shapely.__version__,
        **runtime,
    }
    if _crs_equivalent(source_crs, target_crs):
        projected = tuple(geometries)
        base.update({
            "operation_status": "identity_no_transform_required",
            "coordinate_transform_applied": False,
            "best_available": None,
            "selected_operation": None,
            "operation_chain": [],
            "ballpark_flags": [],
            "ballpark_flag": None,
        })
    else:
        network.set_network_enabled(False)
        try:
            group = TransformerGroup(source_crs, target_crs, always_xy=True, allow_ballpark=False)
            best_available = group.best_available
            candidates = tuple(group.transformers)
            if type(best_available) is not bool or not best_available or not candidates:
                raise DomainMeshOnlyError("CRS_OPERATION_UNAVAILABLE: no best available non-ballpark operation")
            transformer = Transformer.from_crs(
                source_crs, target_crs, always_xy=True, allow_ballpark=False, only_best=True
            )
            projected = tuple(transform_geometry(transformer.transform, geometry) for geometry in geometries)
            used = transformer.get_last_used_operation()
        except DomainMeshOnlyError:
            raise
        except Exception as exc:
            raise DomainMeshOnlyError(f"CRS_OPERATION_UNAVAILABLE: source-to-target operation could not be selected: {exc}") from exc
        if used is None or not isinstance(getattr(used, "definition", None), str):
            raise DomainMeshOnlyError("CRS_OPERATION_EVIDENCE_UNAVAILABLE: the transformed coordinates have no selected operation")
        candidate_definitions = {item.definition for item in candidates if isinstance(getattr(item, "definition", None), str)}
        if used.definition not in candidate_definitions:
            raise DomainMeshOnlyError("CRS_OPERATION_EVIDENCE_UNAVAILABLE: selected operation is not in the best available operation group")
        chain = _coordinate_operation_chain(used)
        flags = [item["has_ballpark_transformation"] for item in chain]
        if not flags or any(type(value) is not bool for value in flags):
            raise DomainMeshOnlyError("CRS_OPERATION_EVIDENCE_UNAVAILABLE: actual operation-chain flags are incomplete")
        selected_accuracy = getattr(used, "accuracy", None)
        if (isinstance(selected_accuracy, bool) or not isinstance(selected_accuracy, (int, float))
                or not math.isfinite(float(selected_accuracy))):
            raise DomainMeshOnlyError("CRS_OPERATION_EVIDENCE_UNAVAILABLE: selected operation accuracy is invalid")
        transformer_accuracy = getattr(transformer, "accuracy", None)
        if (isinstance(transformer_accuracy, bool) or not isinstance(transformer_accuracy, (int, float))
                or not math.isfinite(float(transformer_accuracy))):
            raise DomainMeshOnlyError("CRS_OPERATION_EVIDENCE_UNAVAILABLE: transformer accuracy is invalid")
        base.update({
            "operation_status": "transformed_with_best_available_operation",
            "coordinate_transform_applied": True,
            "best_available": best_available,
            "candidate_operation_count": len(candidates),
            "transformer_description": transformer.description,
            "transformer_definition": transformer.definition,
            "transformer_accuracy_m": float(transformer_accuracy),
            "selected_operation_description": used.description,
            "selected_operation_definition": used.definition,
            "selected_operation_accuracy_m": float(selected_accuracy),
            "operation_chain": chain,
            "ballpark_flags": flags,
            "ballpark_flag": any(flags),
        })
    identity_bytes = _canonical_json_bytes(base)
    return projected, {**base, "operation_identity_sha256": hashlib.sha256(identity_bytes).hexdigest()}


def _inspect_parent_d_shapefile_xy_dimension(path: Path) -> dict[str, Any]:
    """Read raw SHP headers/record type words before any GeoJSON interface can drop Z/M."""
    if not path.is_file() or path.stat().st_size > MAX_COMPONENT_FILE_BYTES:
        raise DomainMeshOnlyError("PARENT_D_SHP_IDENTITY_INVALID: bounded regular SHP file is required")
    size_bytes = path.stat().st_size
    try:
        with path.open("rb") as stream:
            header = stream.read(100)
            if len(header) != 100:
                raise DomainMeshOnlyError("PARENT_D_SHP_HEADER_TRUNCATED: 100-byte SHP header is required")
            file_code = struct.unpack_from(">i", header, 0)[0]
            declared_size = struct.unpack_from(">i", header, 24)[0] * 2
            version = struct.unpack_from("<i", header, 28)[0]
            header_shape_type = struct.unpack_from("<i", header, 32)[0]
            if file_code != 9994 or version != 1000 or declared_size != size_bytes:
                raise DomainMeshOnlyError(
                    "PARENT_D_SHP_HEADER_INVALID: file code, version, or declared byte length is invalid"
                )
            offset = 100
            record_types: list[int] = []
            while offset < size_bytes:
                stream.seek(offset)
                record_header = stream.read(8)
                if len(record_header) != 8:
                    raise DomainMeshOnlyError("PARENT_D_SHP_RECORD_HEADER_TRUNCATED: 8-byte record header is required")
                record_number, content_length_words = struct.unpack(">ii", record_header)
                record_content_bytes = content_length_words * 2
                if (record_number != len(record_types) + 1 or record_content_bytes < 4
                        or offset + 8 + record_content_bytes > size_bytes):
                    raise DomainMeshOnlyError(
                        f"PARENT_D_SHP_RECORD_INVALID: record {len(record_types)} number or content length is invalid"
                    )
                shape_type_bytes = stream.read(4)
                if len(shape_type_bytes) != 4:
                    raise DomainMeshOnlyError("PARENT_D_SHP_RECORD_TYPE_TRUNCATED: record shape type is missing")
                record_shape_type = struct.unpack("<i", shape_type_bytes)[0]
                record_types.append(record_shape_type)
                if record_shape_type == 5:
                    if record_content_bytes < 44:
                        raise DomainMeshOnlyError(
                            f"PARENT_D_SHP_RECORD_TRUNCATED: XY Polygon record {len(record_types) - 1} is shorter than its fixed header"
                        )
                    xy_header = stream.read(40)
                    if len(xy_header) != 40:
                        raise DomainMeshOnlyError(
                            f"PARENT_D_SHP_RECORD_TRUNCATED: XY Polygon record {len(record_types) - 1} is incomplete"
                        )
                    part_count, point_count = struct.unpack_from("<ii", xy_header, 32)
                    if part_count < 1 or point_count < 4:
                        raise DomainMeshOnlyError(
                            f"PARENT_D_SHP_RECORD_INVALID: XY Polygon record {len(record_types) - 1} has invalid counts"
                        )
                    expected_xy_bytes = 44 + part_count * 4 + point_count * 16
                    if record_content_bytes != expected_xy_bytes:
                        raise DomainMeshOnlyError(
                            "UNSUPPORTED_PARENT_D_SHAPE_DIMENSION: "
                            f"Polygon record {len(record_types) - 1} content length does not match XY-only layout"
                        )
                offset += 8 + record_content_bytes
            if offset != size_bytes or not record_types:
                raise DomainMeshOnlyError("PARENT_D_SHP_RECORD_INVALID: record sequence is empty or does not end at EOF")
    except DomainMeshOnlyError:
        raise
    except (OSError, struct.error) as exc:
        raise DomainMeshOnlyError(f"PARENT_D_SHP_HEADER_INVALID: raw shape type inspection failed: {exc}") from exc

    mixed_records = [index for index, shape_type in enumerate(record_types) if shape_type != header_shape_type]
    if mixed_records:
        first = mixed_records[0]
        raise DomainMeshOnlyError(
            "PARENT_D_SHP_HEADER_RECORD_TYPE_MISMATCH: "
            f"header shape type {header_shape_type} differs from record {first} type {record_types[first]}"
        )
    if header_shape_type != 5 or any(shape_type != 5 for shape_type in record_types):
        raise DomainMeshOnlyError(
            "UNSUPPORTED_PARENT_D_SHAPE_DIMENSION: schema 1.1 parent D supports only XY Polygon shape type 5; "
            f"header={header_shape_type}, record_types={sorted(set(record_types))}"
        )
    return {
        "source_encoding": "shapefile",
        "header_shape_type": header_shape_type,
        "record_count": len(record_types),
        "record_shape_type_counts": {str(shape_type): record_types.count(shape_type) for shape_type in sorted(set(record_types))},
        "coordinate_dimension": 2,
    }


def _inspect_parent_d_geojson_xy_dimension(path: Path) -> dict[str, Any]:
    """Verify raw parent-D GeoJSON positions have exactly XY before Shapely decoding."""
    if not path.is_file() or path.stat().st_size > MAX_COMPONENT_FILE_BYTES:
        raise DomainMeshOnlyError("PARENT_D_GEOJSON_IDENTITY_INVALID: bounded regular GeoJSON file is required")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainMeshOnlyError(f"PARENT_D_GEOJSON_INVALID: cannot inspect raw coordinates: {exc}") from exc
    if not isinstance(payload, dict):
        raise DomainMeshOnlyError("PARENT_D_GEOJSON_INVALID: root must be an object")
    if payload.get("type") == "FeatureCollection":
        features = payload.get("features")
        if not isinstance(features, list) or not features:
            raise DomainMeshOnlyError("PARENT_D_GEOJSON_INVALID: FeatureCollection must contain features")
        geometries = [feature.get("geometry") if isinstance(feature, dict) else None for feature in features]
    elif payload.get("type") == "Feature":
        geometries = [payload.get("geometry")]
    else:
        geometries = [payload]
    coordinate_position_count = 0

    def visit(node: Any, feature_index: int) -> None:
        nonlocal coordinate_position_count
        if not isinstance(node, list) or not node:
            raise DomainMeshOnlyError(f"PARENT_D_GEOJSON_INVALID: feature {feature_index} has an empty coordinate array")
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in node):
            if len(node) != 2:
                raise DomainMeshOnlyError(
                    "UNSUPPORTED_PARENT_D_GEOJSON_DIMENSION: schema 1.1 parent D supports XY positions only; "
                    f"feature {feature_index} has a {len(node)}-value position"
                )
            if any(not math.isfinite(float(value)) for value in node):
                raise DomainMeshOnlyError(f"PARENT_D_GEOJSON_NONFINITE: feature {feature_index} has a non-finite XY position")
            coordinate_position_count += 1
            return
        for child in node:
            visit(child, feature_index)

    for feature_index, geometry in enumerate(geometries):
        if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise DomainMeshOnlyError(f"PARENT_D_GEOJSON_INVALID: feature {feature_index} must be Polygon/MultiPolygon")
        visit(geometry.get("coordinates"), feature_index)
    return {
        "source_encoding": "geojson",
        "feature_count": len(geometries),
        "coordinate_position_count": coordinate_position_count,
        "coordinate_dimension": 2,
    }


def _load_polygon_component(component: Mapping[str, Any], manifest_path: Path,
                            root: Path, target_crs_text: str, *,
                            source_schema_version: str) -> _PolygonComponent:
    from pyproj import CRS, Transformer
    from shapely.ops import transform as transform_geometry, unary_union
    from .restricted_worker import _resolve_project_path
    from .security_exclusion import (
        SecurityExclusionError, _canonical_geometry_bytes, _geometry_sha256,
        _read_geojson_geometries, _read_shapefile_geometries, _validate_polygon_geometry,
    )

    file_role = "shp" if component["format"] == "polygon_shapefile" else "geometry"
    manifest_base = manifest_path.parent.relative_to(root)
    path = _resolve_project_path(
        root, (manifest_base / component["files"][file_role]["path"]).as_posix(),
        f"{component['component_id']}.{file_role}",
    )
    declared_source_crs = CRS.from_user_input(component["source_crs"])
    target_crs = CRS.from_user_input(target_crs_text)
    dimension_identity: Mapping[str, Any] | None = None
    strict_parent_d_dimension = (
        source_schema_version == DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION
        and component["role"] == "domain_D"
    )
    if component["format"] == "polygon_shapefile":
        if strict_parent_d_dimension:
            dimension_identity = _inspect_parent_d_shapefile_xy_dimension(path)
        geometries, prj_text, source_file_sha = _read_shapefile_geometries(path)
        raw_source_crs = CRS.from_wkt(prj_text)
        if not _crs_equivalent(raw_source_crs, declared_source_crs):
            raise DomainMeshOnlyError(f"{component['component_id']} explicit source_crs differs from its PRJ")
        source_crs_raw_text = prj_text
        source_crs = raw_source_crs if source_schema_version == DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION else declared_source_crs
    else:
        if strict_parent_d_dimension:
            dimension_identity = _inspect_parent_d_geojson_xy_dimension(path)
        geometries, source_file_sha, declared_crs = _read_geojson_geometries(path)
        if (declared_crs is not None
                and not _crs_equivalent(CRS.from_user_input(declared_crs), declared_source_crs)):
            raise DomainMeshOnlyError(f"{component['component_id']} GeoJSON CRS differs from its explicit source_crs")
        source_crs_raw_text = declared_crs if declared_crs is not None else component["source_crs"]
        source_crs = (CRS.from_user_input(source_crs_raw_text)
                      if source_schema_version == DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION
                      else declared_source_crs)
    if source_schema_version == DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION:
        projected, operation_identity = _project_polygon_features(
            tuple(geometries), source_crs, target_crs,
            source_crs_raw_text=source_crs_raw_text,
            declared_source_crs_text=component["source_crs"],
        )
    else:
        if _crs_equivalent(source_crs, target_crs):
            projected = tuple(geometries)
        else:
            transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
            projected = tuple(transform_geometry(transformer.transform, geometry) for geometry in geometries)
        operation_identity = None
    for index, geometry in enumerate(projected):
        try:
            _validate_polygon_geometry(geometry, f"{component['component_id']} feature {index}")
        except SecurityExclusionError as exc:
            raise DomainMeshOnlyError(str(exc)) from exc
    union = unary_union(projected)
    try:
        _validate_polygon_geometry(union, f"{component['component_id']} feature union")
    except SecurityExclusionError as exc:
        raise DomainMeshOnlyError(str(exc)) from exc
    source_geometry = b"".join(_canonical_geometry_bytes(geometry) for geometry in projected)
    return _PolygonComponent(
        source_union=union, source_feature_indices=tuple(range(len(projected))),
        source_geometry_sha256=hashlib.sha256(source_geometry).hexdigest(),
        source_union_geometry_sha256=_geometry_sha256(union),
        source_crs=source_crs, target_crs=target_crs, source_file_sha256=source_file_sha,
        source_crs_raw_text=source_crs_raw_text,
        declared_source_crs_text=component["source_crs"],
        operation_identity=operation_identity,
        source_dimension_identity=dimension_identity,
    )


def _load_geometry_context(config: Mapping[str, Any], project_root: str | Path, *, verify_component_sha: bool) -> _GeometryContext:
    from pyproj import CRS
    from .mesh_preparation import GmshLineGroup, GmshMeshConfig, _validate_projected_metric_crs
    from .restricted_worker import SourceIdentityError, _stat_identity
    from .security_exclusion import (
        SecurityExclusionError, _geometry_sha256, assess_security_domain, gmsh_config_for_active_domain,
    )

    root = Path(project_root).resolve()
    identity = inspect_source_manifest_identity(config, root)
    manifest_path = identity["manifest_path"]
    manifest = identity["manifest"]
    before_records, _ = _component_file_records_for_manifest(root, manifest_path, manifest, verify_sha=verify_component_sha)
    target_crs_text = config["target_coordinate_system"]
    _validate_projected_metric_crs(target_crs_text)
    target_crs = CRS.from_user_input(target_crs_text)
    by_role = {component["role"]: component for component in manifest["components"] if component["role"] in {"domain_D", "security_S"}}
    if set(by_role) != {"domain_D", "security_S"}:
        raise DomainMeshOnlyError("source manifest must provide exactly one domain_D and one security_S component")

    loaded_areas: dict[str, _PolygonComponent] = {}
    for role, component in by_role.items():
        loaded_areas[role] = _load_polygon_component(
            component, manifest_path, root, target_crs_text,
            source_schema_version=manifest["schema_version"],
        )
    domain_geometry = loaded_areas["domain_D"].source_union
    security_load = loaded_areas["security_S"]
    security_geometry = security_load.source_union
    if domain_geometry.geom_type != "Polygon":
        raise DomainMeshOnlyError(
            f"UNSUPPORTED_MULTIPOLYGON_DOMAIN_D: current Gmsh adapter requires one D Polygon; got {domain_geometry.geom_type}"
        )
    mesh_settings = config["mesh_generation"]
    base_config = GmshMeshConfig.model_validate({
        "type": "gmsh",
        "outer_boundary": [tuple(map(float, point[:2])) for point in list(domain_geometry.exterior.coords)[:-1]],
        "holes": [[tuple(map(float, point[:2])) for point in list(ring.coords)[:-1]] for ring in domain_geometry.interiors],
        "boundary_groups": [], "constraints": [],
        "default_size_m": mesh_settings["default_size_m"],
        "minimum_size_m": mesh_settings["minimum_size_m"],
        "refinement_points": [], "maximum_nodes": mesh_settings["maximum_nodes"],
    })
    boundary_groups=[]; constraint_groups=[]; line_components=[]
    for component in manifest["components"]:
        if component["role"] not in {"boundary_line", "constraint_line"}:
            continue
        if _component_source_kind(manifest, component) == "derived":
            line, line_identity = _derived_domain_d_exterior_line(
                component, by_role["domain_D"], loaded_areas["domain_D"],
                domain_geometry, tuple(before_records.values()), target_crs_text,
            )
        else:
            line=_read_line_geometry(component,manifest_path,root,target_crs)
            line_identity={"component_id":component["component_id"],"role":component["role"],
                           "group_name":component["group_name"],"geometry_sha256":_geometry_sha256(line),
                           "feature_count":1,"source_crs":component["source_crs"],
                           "target_crs":target_crs_text,
                           "coordinate_transform_applied":not _crs_equivalent(CRS.from_user_input(component["source_crs"]),target_crs)}
            if manifest["schema_version"] == DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION:
                line_identity.update({"source_kind":"file","source_version":component["source_version"]})
        points=[tuple(map(float,point[:2])) for point in line.coords]
        group=GmshLineGroup(name=component["group_name"],points=points,closed=component["closed"])
        (boundary_groups if component["role"]=="boundary_line" else constraint_groups).append(group)
        line_components.append(line_identity)
    base_config=GmshMeshConfig.model_validate({**base_config.model_dump(mode="python"),
                                               "boundary_groups":boundary_groups,"constraints":constraint_groups})
    assessment=assess_security_domain(domain_geometry,security_load)
    area_tolerance=max(float(mesh_settings["area_abs_tolerance_m2"]),
                       float(domain_geometry.area)*float(mesh_settings["area_rel_tolerance"]))
    if assessment.security_outside_area_m2 > area_tolerance:
        raise DomainMeshOnlyError(
            f"SECURITY_OUTSIDE_DOMAIN: S has {assessment.security_outside_area_m2:.12g} m2 outside D "
            f"(tolerance {area_tolerance:.12g} m2)"
        )
    if assessment.active_geometry.geom_type != "Polygon":
        raise DomainMeshOnlyError(
            f"UNSUPPORTED_MULTIPOLYGON_D_MINUS_S: current adapter requires one active Polygon; got {assessment.active_geometry.geom_type}"
        )
    try:
        gmsh_config=gmsh_config_for_active_domain(base_config,assessment,tolerance_m=float(mesh_settings["geometry_tolerance_m"]))
    except SecurityExclusionError as exc:
        raise DomainMeshOnlyError(f"D−S/line geometry is not representable by the current Gmsh adapter: {exc}") from exc
    if gmsh_config is None:
        raise DomainMeshOnlyError("D−S geometry adapter returned no Gmsh config")
    after_records,total_bytes=_component_file_records_for_manifest(root,manifest_path,manifest,verify_sha=True)
    before_hashes={key:value["sha256"] for key,value in before_records.items()}
    after_hashes={key:value["sha256"] for key,value in after_records.items()}
    if before_hashes!=after_hashes or total_bytes>MAX_COMPONENT_TOTAL_BYTES:
        raise SourceIdentityError("one or more source components changed during geometry loading")
    from .restricted_worker import _sha256_file
    if _stat_identity(manifest_path)!=identity["manifest_stat_identity"] or _sha256_file(manifest_path,maximum_bytes=MAX_SOURCE_MANIFEST_BYTES)[0]!=identity["manifest_sha256"]:
        raise SourceIdentityError("source manifest changed during geometry loading")
    return _GeometryContext(
        manifest_path=manifest_path,manifest_sha256=identity["manifest_sha256"],
        manifest_stat=identity["manifest_stat_identity"],manifest=manifest,
        components_before=tuple(before_records.values()),components_after=tuple(after_records.values()),
        domain_component=by_role["domain_D"],security_component=by_role["security_S"],
        line_components=tuple(line_components),domain_geometry=domain_geometry,
        domain_load=loaded_areas["domain_D"],
        security_geometry=security_geometry,security_load=security_load,
        security_assessment=assessment,gmsh_config=gmsh_config,generation_settings=mesh_settings,
    )


def _current_process_in_job() -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    from .restricted_worker import _win_api
    kernel32, _ = _win_api()
    value = wintypes.BOOL()
    if not kernel32.IsProcessInJob(wintypes.HANDLE(-1), None, ctypes.byref(value)):
        raise OSError(ctypes.get_last_error(), "IsProcessInJob(current process) failed")
    return bool(value.value)


def _new_worker_semantic_context(bundle_path: Path, config_sha256: str,
                                 source_manifest_sha256: str) -> _WorkerSemanticContext:
    if not _current_process_in_job():
        raise DomainMeshOnlyError("worker semantic context can only be created inside the enforced Job")
    return _WorkerSemanticContext(_SEMANTIC_CONTEXT_SEAL, os.getpid(), bundle_path.resolve(),
                                  config_sha256, source_manifest_sha256)


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _persist_mesh_algorithm_execution_receipt(
    stage: Path,
    receipt: Mapping[str, Any],
    *,
    scratch_budget_bytes: int,
) -> dict[str, Any]:
    payload = _canonical_json_bytes(dict(receipt))
    if len(payload) > MAX_OUTPUT_RECEIPT_BYTES:
        raise DomainMeshOnlyError(
            f"MESH_ALGORITHM_EXECUTION_RECEIPT_CAP: receipt exceeds {MAX_OUTPUT_RECEIPT_BYTES} bytes"
        )
    from .restricted_worker import _write_no_clobber_bytes

    relative = "scratch/mesh-algorithm-execution.json"
    path = stage / relative
    _write_no_clobber_bytes(
        path, payload, output_budget=scratch_budget_bytes,
        root=stage / "scratch", reserve_bytes=4096,
    )
    return {
        "path": relative,
        "format": str(receipt.get("format", "gmsh-mesh-algorithm-plan-execution-v1")),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _canonical_domain_exterior_sha256(points: Sequence[tuple[float, float]]) -> str:
    point_count = len(points)
    if not 4 <= point_count <= MAX_DERIVED_LINE_POINTS:
        raise DomainMeshOnlyError(
            f"DERIVED_LINE_POINT_COUNT_LIMIT: D exterior point count must be in [4,{MAX_DERIVED_LINE_POINTS}]"
        )
    digest = hashlib.sha256()
    digest.update((DOMAIN_D_EXTERIOR_GEOMETRY_HASH_FORMAT + "\0").encode("ascii"))
    digest.update(struct.pack("<Q", point_count))
    for index, point in enumerate(points):
        if len(point) != 2 or not all(math.isfinite(float(value)) for value in point):
            raise DomainMeshOnlyError(f"DERIVED_LINE_NONFINITE_COORDINATE: exterior point {index} must be finite XY")
        x, y = float(point[0]), float(point[1])
        # Normalize negative zero while preserving every other IEEE-754 XY value.
        digest.update(struct.pack("<dd", 0.0 if x == 0.0 else x, 0.0 if y == 0.0 else y))
    return digest.hexdigest()


def _derived_domain_d_exterior_line(
    component: Mapping[str, Any],
    parent_component: Mapping[str, Any],
    parent_load: _PolygonComponent,
    domain_geometry: Any,
    parent_file_records: tuple[Mapping[str, Any], ...],
    target_crs_text: str,
) -> tuple[Any, dict[str, Any]]:
    from shapely.geometry import LineString

    if component["format"] != DOMAIN_D_EXTERIOR_LINE_FORMAT:
        raise DomainMeshOnlyError("DERIVED_LINE_RULE_UNSUPPORTED: unsupported derived boundary rule version")
    if component["parent_component_id"] != parent_component["component_id"] or parent_component["role"] != "domain_D":
        raise DomainMeshOnlyError("DERIVED_LINE_PARENT_MISMATCH: parent ID must resolve to the loaded domain_D component")
    if domain_geometry.geom_type != "Polygon" or domain_geometry.is_empty or not domain_geometry.is_valid:
        raise DomainMeshOnlyError("DERIVED_LINE_PARENT_UNSUPPORTED: parent D union must be one valid Polygon")
    if parent_load.operation_identity is None:
        raise DomainMeshOnlyError("DERIVED_LINE_OPERATION_UNAVAILABLE: parent D has no versioned CRS operation identity")
    if parent_load.source_dimension_identity is None:
        raise DomainMeshOnlyError("DERIVED_LINE_SOURCE_DIMENSION_UNAVAILABLE: parent D raw XY dimension proof is missing")
    raw_coordinates = tuple(domain_geometry.exterior.coords)
    if not 4 <= len(raw_coordinates) <= MAX_DERIVED_LINE_POINTS:
        raise DomainMeshOnlyError(
            f"DERIVED_LINE_POINT_COUNT_LIMIT: D exterior point count must be in [4,{MAX_DERIVED_LINE_POINTS}]"
        )
    points: list[tuple[float, float]] = []
    for index, coordinate in enumerate(raw_coordinates):
        if len(coordinate) != 2:
            raise DomainMeshOnlyError("DERIVED_LINE_COORDINATE_DIMENSION: parent D exterior must use XY coordinates only")
        point = (float(coordinate[0]), float(coordinate[1]))
        if not all(math.isfinite(value) for value in point):
            raise DomainMeshOnlyError(f"DERIVED_LINE_NONFINITE_COORDINATE: exterior point {index} is not finite")
        points.append(point)
    ordered_points = tuple(points)
    if ordered_points[0] != ordered_points[-1]:
        raise DomainMeshOnlyError("DERIVED_LINE_OPEN_RING: parent D exterior is not explicitly closed")
    if len(set(ordered_points[:-1])) < 3 or any(a == b for a, b in zip(ordered_points, ordered_points[1:])):
        raise DomainMeshOnlyError("DERIVED_LINE_DEGENERATE_RING: parent D exterior has repeated consecutive or insufficient vertices")
    line = LineString(ordered_points)
    if (line.geom_type != "LineString" or line.is_empty or not line.is_valid
            or tuple((float(x), float(y)) for x, y in line.coords) != ordered_points):
        raise DomainMeshOnlyError("DERIVED_LINE_SEQUENCE_CHANGED: LineString construction changed the parent D exterior sequence")

    geometry_sha256 = _canonical_domain_exterior_sha256(ordered_points)
    parent_records = sorted(
        (dict(item) for item in parent_file_records if item.get("component_id") == parent_component["component_id"]),
        key=lambda item: (item.get("file_role", ""), item.get("identity_kind", "staged"), item.get("path", "")),
    )
    if not parent_records or any(item.get("identity_kind") not in {"original_source", "staged_copy"} for item in parent_records):
        raise DomainMeshOnlyError("DERIVED_LINE_PARENT_IDENTITY_INCOMPLETE: D original and staged sidecar identities are required")
    provenance = {
        "format": DOMAIN_D_EXTERIOR_PROVENANCE_FORMAT,
        "derivation_rule": DOMAIN_D_EXTERIOR_DERIVATION_RULE,
        "derived_component_id": component["component_id"],
        "role": "boundary_line",
        "parent_component_id": parent_component["component_id"],
        "parent_component_role": parent_component["role"],
        "parent_source_version": parent_component["source_version"],
        "parent_file_identity_chain": parent_records,
        "parent_source_crs_raw_text": parent_load.source_crs_raw_text,
        "parent_declared_source_crs_text": parent_load.declared_source_crs_text,
        "parent_source_dimension_identity": dict(parent_load.source_dimension_identity),
        "target_crs": target_crs_text,
        "coordinate_operation": dict(parent_load.operation_identity),
        "parent_geometry_stage": "all_valid_transformed_D_features_then_unary_union_then_exterior",
        "parent_feature_count": len(parent_load.source_feature_indices),
        "parent_union_geometry_sha256": parent_load.source_union_geometry_sha256,
        "geometry_hash_format": DOMAIN_D_EXTERIOR_GEOMETRY_HASH_FORMAT,
        "geometry_sha256": geometry_sha256,
        "ordered_point_count": len(ordered_points),
        "closed": True,
        "group_name": component["group_name"],
        "geometry_only": True,
        "q_binding": DOMAIN_D_EXTERIOR_Q_BINDING,
    }
    provenance_sha256 = hashlib.sha256(_canonical_json_bytes(provenance)).hexdigest()
    line_identity = {
        "component_id": component["component_id"], "role": "boundary_line",
        "source_kind": "derived", "format": DOMAIN_D_EXTERIOR_LINE_FORMAT,
        "parent_component_id": parent_component["component_id"],
        "group_name": component["group_name"], "closed": True,
        "geometry_only": True, "q_binding": DOMAIN_D_EXTERIOR_Q_BINDING,
        "geometry_type": "LineString", "geometry_hash_format": DOMAIN_D_EXTERIOR_GEOMETRY_HASH_FORMAT,
        "geometry_sha256": geometry_sha256, "provenance_format": DOMAIN_D_EXTERIOR_PROVENANCE_FORMAT,
        "provenance_sha256": provenance_sha256, "derivation_provenance": provenance,
        "ordered_point_count": len(ordered_points), "feature_count": 1,
        "source_crs": parent_load.declared_source_crs_text,
        "source_crs_raw_text": parent_load.source_crs_raw_text,
        "target_crs": target_crs_text,
        "coordinate_transform_applied": parent_load.operation_identity["coordinate_transform_applied"],
        "coordinate_operation": dict(parent_load.operation_identity),
    }
    return line, line_identity


def _mesh_payload(mesh: Any) -> dict[str, Any]:
    from .b1_synthetic_pilot import _mesh_payload as pilot_mesh_payload
    payload = pilot_mesh_payload(mesh, full=True)
    payload["format"] = DOMAIN_MESH_ARTIFACT_FORMAT
    payload["mesh_only"] = True
    return payload


def _face_id_sha256(face_ids: Any) -> str:
    digest = hashlib.sha256()
    for face_id in face_ids:
        digest.update(struct.pack(">q", int(face_id)))
    return digest.hexdigest()


def _generation_audit_settings(config: Mapping[str, Any], gmsh_config: Any) -> dict[str, Any]:
    components = config.get("_trusted_source_components")
    if isinstance(components, list):
        constraint_roles = {item["group_name"]: item["role"] for item in components
                            if isinstance(item, Mapping) and item.get("role") == "constraint_line"}
    else:
        constraint_roles = {group.name: "constraint_line" for group in gmsh_config.constraints}
    return {
        "area_abs_tolerance_m2": float(config["mesh_generation"]["area_abs_tolerance_m2"]),
        "area_rel_tolerance": float(config["mesh_generation"]["area_rel_tolerance"]),
        "area_roles": {
            "outer_boundary": "active_domain_D_minus_S",
            "holes": {f"hole_{index}": "active_domain_exclusion_boundary"
                      for index, _hole in enumerate(gmsh_config.holes)},
            "constraints": constraint_roles,
        },
    }


def _prepare_geometry_context(config: Mapping[str, Any], project_root: Path, *, verify_component_sha: bool) -> tuple[_GeometryContext, dict[str, Any]]:
    context = _load_geometry_context(config, project_root, verify_component_sha=verify_component_sha)
    mesh_settings = config["mesh_generation"]
    return context, dict(mesh_settings)


def _audit_generated_mesh(mesh: Any, context: _GeometryContext, config: Mapping[str, Any]) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    from .b1_synthetic_pilot import _audit_g0_mesh
    from .security_exclusion import SecurityExclusionError, check_mesh_against_security
    generation = _generation_audit_settings(config, context.gmsh_config)
    try:
        geometry_audit = _audit_g0_mesh(mesh, context.gmsh_config, generation)
        security_check = check_mesh_against_security(
            mesh, context.security_assessment,
            tolerance_m=float(config["mesh_generation"]["geometry_tolerance_m"]),
        )
    except (SecurityExclusionError, ValueError, TypeError) as exc:
        raise DomainMeshOnlyError(f"generated D−S mesh failed geometry/security audit: {exc}") from exc
    return security_check.mesh, geometry_audit, {
        "original_domain_area_m2": context.security_assessment.original_domain_area_m2,
        "security_union_area_m2": context.security_assessment.security_union_area_m2,
        "security_inside_area_m2": context.security_assessment.security_inside_area_m2,
        "security_outside_area_m2": context.security_assessment.security_outside_area_m2,
        "active_domain_area_m2": context.security_assessment.active_domain_area_m2,
        "active_mesh_area_m2": security_check.active_mesh_area_m2,
        "coverage_residual_m2": security_check.coverage_residual_m2,
        "security_boundary_edge_count": len(security_check.security_boundary_edge_indices),
        "security_boundary_edge_sha256": security_check.security_boundary_edge_sha256,
        "security_boundary_group_name": "security_exclusion_boundary",
    }


def _component_audit(context: _GeometryContext) -> list[dict[str, Any]]:
    files_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in context.components_after:
        file_identity = {
            "role": item["file_role"], "path": item["path"], "sha256": item["sha256"],
            "bytes": item["bytes"], "stat_identity": dict(item["stat_identity"]),
        }
        if "identity_kind" in item:
            file_identity["identity_kind"] = item["identity_kind"]
        files_by_id.setdefault(item["component_id"], []).append(file_identity)
    component_meta: dict[str, dict[str, Any]] = {}
    for component in context.manifest["components"]:
        source_kind = _component_source_kind(context.manifest, component)
        metadata = {
            "component_id": component["component_id"], "role": component["role"],
            "format": component["format"],
            "group_name": component["group_name"], "closed": component["closed"],
            "files": sorted(files_by_id.get(component["component_id"], []),
                             key=lambda x: (x["role"], x.get("identity_kind", ""), x["path"])),
        }
        if source_kind == "file":
            metadata["source_crs"] = component["source_crs"]
            if context.manifest["schema_version"] == DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION:
                metadata["source_kind"] = "file"
                metadata["source_version"] = component["source_version"]
        else:
            metadata.update({
                "source_kind": "derived", "parent_component_id": component["parent_component_id"],
                "geometry_only": component["geometry_only"], "q_binding": component["q_binding"],
            })
        component_meta[component["component_id"]] = metadata
    domain = context.domain_load
    security = context.security_load
    for role, loaded in (("domain_D", domain), ("security_S", security)):
        component_id = context.domain_component["component_id"] if role == "domain_D" else context.security_component["component_id"]
        item = component_meta[component_id]
        item.update({
            "source_feature_count": len(loaded.source_feature_indices),
            "source_geometry_sha256": loaded.source_geometry_sha256,
            "source_union_geometry_sha256": loaded.source_union_geometry_sha256,
            "coordinate_transform_applied": not _crs_equivalent(loaded.source_crs, loaded.target_crs),
            "target_crs": loaded.target_crs.to_string(),
        })
        if context.manifest["schema_version"] == DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION:
            source_component = context.domain_component if role == "domain_D" else context.security_component
            if loaded.operation_identity is None:
                raise DomainMeshOnlyError(f"{role} CRS operation identity is missing from schema 1.1 audit")
            item.update({
                "source_version": source_component["source_version"],
                "source_crs_raw_text": loaded.source_crs_raw_text,
                "declared_source_crs_text": loaded.declared_source_crs_text,
                "coordinate_operation": dict(loaded.operation_identity),
            })
            if role == "domain_D":
                if loaded.source_dimension_identity is None:
                    raise DomainMeshOnlyError("domain_D raw XY dimension proof is missing from schema 1.1 audit")
                item["source_dimension_identity"] = dict(loaded.source_dimension_identity)
    for line in context.line_components:
        component_meta[line["component_id"]].update(line)
    return [component_meta[x["component_id"]] for x in context.manifest["components"]]


def _source_audit_format(context: _GeometryContext) -> str:
    if any(_component_source_kind(context.manifest, component) == "derived"
           for component in context.manifest["components"]):
        return DOMAIN_D_EXTERIOR_AUDIT_FORMAT
    return DOMAIN_MESH_AUDIT_FORMAT


def _build_source_audit(
    context: _GeometryContext,
    mesh: Any,
    config: Mapping[str, Any],
    geometry_audit: Mapping[str, Any],
    security_audit: Mapping[str, Any],
    gmsh_identity: Mapping[str, Any],
    code_hashes: Mapping[str, str],
) -> dict[str, Any]:
    payload = _mesh_payload(mesh)
    payload_sha = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return {
        "format": _source_audit_format(context),
        "mesh_only": True,
        "preparation_only": True,
        "simulation_result": False,
        "production_approved": False,
        "engineering_ready": False,
        "source_manifest_sha256": context.manifest_sha256,
        "source_set_id": context.manifest["source_set_id"],
        "target_coordinate_system": config["target_coordinate_system"],
        "source_components": _component_audit(context),
        "domain_D_area_m2": float(context.domain_geometry.area),
        "security_S_geometry_sha256": context.security_load.source_union_geometry_sha256,
        "d_minus_s_area_m2": float(context.security_assessment.active_domain_area_m2),
        "area_audit": geometry_audit["area_audit"],
        "line_coverage_audit": geometry_audit["line_coverage"],
        "security_mesh_audit": dict(security_audit),
        "mesh": {
            "mesh_id": mesh.mesh_id,
            "coordinate_system": mesh.coordinate_system,
            "node_count": mesh.n_node,
            "face_count": mesh.n_face,
            "face_id_sequence_sha256": _face_id_sha256(mesh.face_id),
            "mesh_sha256": payload_sha,
            "minimum_quality": min(mesh.face_quality),
            "minimum_face_area_m2": min(mesh.face_area),
            "geometry_tolerance_m": mesh.geometry_tolerance_m,
        },
        "gmsh": dict(gmsh_identity),
        "route_code_sha256": dict(code_hashes),
    }


def _write_route_file(path: Path, payload: bytes, *, output_budget: int, root: Path) -> dict[str, Any]:
    from .restricted_worker import _write_no_clobber_bytes
    _write_no_clobber_bytes(path, payload, output_budget=output_budget, root=root, reserve_bytes=OUTPUT_RESERVE_BYTES)
    return {"name": path.name, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


def _load_saved_mesh(path: Path, *, expected_sha256: str, expected_bytes: int) -> Any:
    from .triangular_mesh import TriangularMesh
    if path.is_symlink() or not path.is_file():
        raise DomainMeshOnlyError("mesh-only artifact is missing or a symlink")
    raw = path.read_bytes()
    if len(raw) != expected_bytes or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise DomainMeshOnlyError("mesh-only artifact bytes differ from their recorded identity")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainMeshOnlyError(f"mesh-only JSON artifact is malformed: {exc}") from exc
    keys = {"format", "mesh_only", "mesh_id", "coordinate_system", "node_xy", "face_nodes", "face_id",
            "boundary_groups", "constraint_groups", "geometry_tolerance_m", "minimum_face_area_m2"}
    if not isinstance(payload, dict) or set(payload) != keys or payload.get("format") != DOMAIN_MESH_ARTIFACT_FORMAT or payload.get("mesh_only") is not True:
        raise DomainMeshOnlyError("mesh-only artifact format/fields differ from the frozen contract")
    if _canonical_json_bytes(payload) != raw:
        raise DomainMeshOnlyError("mesh-only artifact is not canonical JSON")
    try:
        mesh = TriangularMesh(
            node_xy=payload["node_xy"], face_nodes=payload["face_nodes"], face_id=payload["face_id"],
            coordinate_system=payload["coordinate_system"], mesh_id=payload["mesh_id"],
            boundary_groups=payload["boundary_groups"], constraint_groups=payload["constraint_groups"],
            geometry_tolerance_m=payload["geometry_tolerance_m"], minimum_face_area_m2=payload["minimum_face_area_m2"],
        )
    except Exception as exc:
        raise DomainMeshOnlyError(f"mesh-only topology is invalid: {exc}") from exc
    return mesh


def _mesh_without_security_boundary_group(mesh: Any) -> Any:
    from .triangular_mesh import TriangularMesh
    groups = {
        name: [mesh.edge_nodes[index] for index in indices]
        for name, indices in mesh.boundary_groups.items()
        if name != "security_exclusion_boundary"
    }
    constraints = {
        name: [mesh.edge_nodes[index] for index in indices]
        for name, indices in mesh.constraint_groups.items()
    }
    return TriangularMesh(
        node_xy=mesh.node_xy, face_nodes=mesh.face_nodes, face_id=mesh.face_id,
        coordinate_system=mesh.coordinate_system, mesh_id=mesh.mesh_id,
        boundary_groups=groups, constraint_groups=constraints,
        geometry_tolerance_m=mesh.geometry_tolerance_m, minimum_face_area_m2=mesh.minimum_face_area_m2,
    )


def _audit_security_mesh(mesh: Any, context: _GeometryContext, config: Mapping[str, Any]) -> dict[str, Any]:
    from .security_exclusion import SecurityExclusionError, check_mesh_against_security
    try:
        check = check_mesh_against_security(
            mesh, context.security_assessment,
            tolerance_m=float(config["mesh_generation"]["geometry_tolerance_m"]),
        )
    except SecurityExclusionError as exc:
        raise DomainMeshOnlyError(f"mesh does not satisfy trusted D−S/security audit: {exc}") from exc
    if check.mesh.boundary_groups != mesh.boundary_groups or check.mesh.constraint_groups != mesh.constraint_groups:
        raise DomainMeshOnlyError("security reader recomputed a different closed-wall boundary/constraint group set")
    return {
        "original_domain_area_m2": context.security_assessment.original_domain_area_m2,
        "security_union_area_m2": context.security_assessment.security_union_area_m2,
        "security_inside_area_m2": context.security_assessment.security_inside_area_m2,
        "security_outside_area_m2": context.security_assessment.security_outside_area_m2,
        "active_domain_area_m2": context.security_assessment.active_domain_area_m2,
        "active_mesh_area_m2": check.active_mesh_area_m2,
        "coverage_residual_m2": check.coverage_residual_m2,
        "security_boundary_edge_count": len(check.security_boundary_edge_indices),
        "security_boundary_edge_sha256": check.security_boundary_edge_sha256,
        "security_boundary_group_name": "security_exclusion_boundary",
    }


def _gmsh_identity_for_worker(config: Mapping[str, Any]) -> dict[str, Any]:
    from .b1_synthetic_pilot import _gmsh_runtime_identity, _loaded_gmsh_library
    if not _current_process_in_job():
        raise DomainMeshOnlyError("domain mesh-only route refused Gmsh before confirmed Job membership")
    identity = _gmsh_runtime_identity()
    if (identity.get("python_binding_version") != config["expected_gmsh_version"]
            or identity.get("native_library_sha256") != config["expected_gmsh_library_sha256"]):
        from .restricted_worker import SourceIdentityError
        raise SourceIdentityError("installed Gmsh binding/DLL differs from the trusted mesh-only config")
    return identity


def _seal_gmsh_worker_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    from .b1_synthetic_pilot import _loaded_gmsh_library
    value = dict(identity)
    value["native_library_loaded_in_worker"] = _loaded_gmsh_library(value["native_library_path"])
    value["python_binding_loaded_in_worker"] = "gmsh" in sys.modules
    value["worker_pid"] = os.getpid()
    if not _current_process_in_job() or value["native_library_loaded_in_worker"] is not True or value["python_binding_loaded_in_worker"] is not True:
        raise DomainMeshOnlyError("Gmsh binding/DLL was not loaded in the Job-bound worker")
    return value


def _domain_mesh_fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(dict(payload))).hexdigest()


def _runtime_dependency_fingerprint(gmsh_identity: Mapping[str, Any]) -> dict[str, Any]:
    from pyproj import __version__ as pyproj_version
    from pyproj import proj_version_str
    import shapely
    from shapely import geos_version_string
    try:
        pyshp_version = importlib.metadata.version("pyshp")
    except importlib.metadata.PackageNotFoundError:
        pyshp_version = None
    return {
        "python": sys.version.splitlines()[0],
        "platform": platform.platform(),
        "pyshp": pyshp_version,
        "shapely": shapely.__version__,
        "geos": geos_version_string,
        "pyproj": pyproj_version,
        "proj": proj_version_str,
        "gmsh_python_binding": gmsh_identity.get("python_binding_version"),
        "gmsh_native_library_sha256": gmsh_identity.get("native_library_sha256"),
    }


def _make_mesh_only_payload(mesh: Any) -> dict[str, Any]:
    from .b1_synthetic_pilot import _mesh_payload
    payload = _mesh_payload(mesh, full=True)
    payload["format"] = DOMAIN_MESH_ARTIFACT_FORMAT
    payload["mesh_only"] = True
    return payload


def _source_audit_record(context: _GeometryContext, mesh: Any, config: Mapping[str, Any],
                         geometry_audit: Mapping[str, Any], security_audit: Mapping[str, Any],
                         gmsh_identity: Mapping[str, Any], code_hashes: Mapping[str, str],
                         config_sha256: str) -> dict[str, Any]:
    payload = _make_mesh_only_payload(mesh)
    domain_area = float(context.domain_geometry.area)
    return {
        "format": _source_audit_format(context),
        "mesh_only": True,
        "preparation_only": True,
        "simulation_result": False,
        "production_approved": False,
        "engineering_ready": False,
        "config_sha256": config_sha256,
        "source_manifest_path": config["source_manifest"]["path"],
        "source_manifest_sha256": context.manifest_sha256,
        "source_set_id": context.manifest["source_set_id"],
        "target_coordinate_system": config["target_coordinate_system"],
        "source_components": _component_audit(context),
        "domain_D": {
            "geometry_sha256": context.domain_load.source_union_geometry_sha256,
            "source_geometry_sha256": context.domain_load.source_geometry_sha256,
            "area_m2": domain_area,
            "feature_count": len(context.domain_load.source_feature_indices),
            "supported_topology": "one Polygon with zero or more holes",
        },
        "security_S": {
            "geometry_sha256": context.security_load.source_union_geometry_sha256,
            "source_geometry_sha256": context.security_load.source_geometry_sha256,
            "area_m2": float(context.security_geometry.area),
            "active_inside_area_m2": context.security_assessment.security_inside_area_m2,
            "outside_D_area_m2": context.security_assessment.security_outside_area_m2,
            "active_D_minus_S_area_m2": context.security_assessment.active_domain_area_m2,
            "supported_topology": "Polygon/MultiPolygon union when D−S remains one Polygon",
        },
        "boundary_and_constraint_components": [dict(x) for x in context.line_components],
        "area_audit": dict(geometry_audit["area_audit"]),
        "line_coverage_audit": dict(geometry_audit["line_coverage"]),
        "security_mesh_audit": dict(security_audit),
        "mesh": {
            "mesh_id": mesh.mesh_id,
            "node_count": mesh.n_node,
            "face_count": mesh.n_face,
            "face_id_sequence_sha256": _face_id_sha256(mesh.face_id),
            "geometry_fingerprint_sha256": _domain_mesh_fingerprint(payload),
            "quality_report": mesh.quality_report(minimum_quality=float(config["mesh_generation"]["minimum_face_quality"])),
            "minimum_face_quality": min(mesh.face_quality),
            "minimum_face_area_m2": min(mesh.face_area),
            "mesh_area_m2": math.fsum(float(x) for x in mesh.face_area),
        },
        "gmsh": dict(gmsh_identity),
        "runtime_fingerprint": _runtime_dependency_fingerprint(gmsh_identity),
        "route_code_sha256": dict(code_hashes),
        "dem_pixel_reader_calls": 0,
        "terrain_mapper_calls": 0,
        "solver_calls": 0,
    }


def _compare_exact(label: str, stored: Any, expected: Any) -> None:
    if stored != expected:
        raise DomainMeshOnlyError(f"mesh-only {label} differs from the trusted source/config recomputation")


def validate_domain_mesh_bundle_semantics(
    bundle_directory: str | Path,
    *,
    config_path: str | Path,
    expected_config_sha256: str,
    project_root: str | Path,
    expected_mesh_sha256: str,
    expected_source_audit_sha256: str,
    code_hashes: Mapping[str, str],
    worker_context: _WorkerSemanticContext | None = None,
) -> dict[str, Any]:
    """Recompute mesh semantics only in an enforced worker, with external config/source context."""
    directory = Path(bundle_directory).resolve()
    if (not isinstance(worker_context, _WorkerSemanticContext)
            or worker_context.seal is not _SEMANTIC_CONTEXT_SEAL
            or worker_context.process_id != os.getpid()
            or worker_context.bundle_path != directory
            or worker_context.config_sha256 != expected_config_sha256
            or not _current_process_in_job()):
        raise DomainMeshOnlyError("trusted-context semantic mesh-only readback requires its dispatcher-bound Job context")
    from .b1_synthetic_pilot import _audit_g0_mesh
    from .mesh_preparation import GmshMeshConfig
    from .restricted_worker import _sha256_file
    from .security_exclusion import SecurityExclusionError, check_mesh_against_security
    from .triangular_mesh import TriangularMesh

    root = Path(project_root).resolve()
    config_file = Path(config_path)
    if config_file.is_symlink() or not config_file.is_file() or config_file.stat().st_size > 4 * 1024 * 1024:
        raise DomainMeshOnlyError("trusted mesh-only config is missing, symlinked or oversized")
    config_bytes = config_file.read_bytes()
    if hashlib.sha256(config_bytes).hexdigest() != expected_config_sha256:
        from .restricted_worker import SourceIdentityError
        raise SourceIdentityError("trusted mesh-only config bytes changed before semantic readback")
    try:
        config = validate_domain_mesh_only_config_shape(json.loads(config_bytes))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DomainMeshOnlyError(f"trusted mesh-only config is invalid during readback: {exc}") from exc
    if worker_context.source_manifest_sha256 != config["source_manifest"]["sha256"]:
        raise DomainMeshOnlyError("dispatcher-bound worker context has a different source-manifest binding")
    context = _load_geometry_context(config, root, verify_component_sha=True)
    if context.manifest_sha256 != config["source_manifest"]["sha256"]:
        raise DomainMeshOnlyError("trusted source manifest SHA differs during semantic readback")
    mesh_path = directory / "artifacts" / "domain-mesh.json"
    audit_path = directory / "artifacts" / "domain-source-audit.json"
    for path, expected_digest in ((mesh_path, expected_mesh_sha256), (audit_path, expected_source_audit_sha256)):
        if path.is_symlink() or not path.is_file():
            raise DomainMeshOnlyError(f"mesh-only readback artifact is missing or a symlink: {path.name}")
        if _sha256_file(path, maximum_bytes=MAX_COMPONENT_TOTAL_BYTES)[0] != expected_digest:
            raise DomainMeshOnlyError(f"mesh-only readback artifact hash differs: {path.name}")
    try:
        payload = json.loads(mesh_path.read_text(encoding="utf-8"))
        stored_audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainMeshOnlyError(f"mesh-only readback JSON is malformed: {exc}") from exc
    mesh_keys = {"format", "mesh_only", "mesh_id", "coordinate_system", "node_xy", "face_nodes", "face_id",
                 "boundary_groups", "constraint_groups", "geometry_tolerance_m", "minimum_face_area_m2"}
    if not isinstance(payload, dict) or set(payload) != mesh_keys or payload.get("format") != DOMAIN_MESH_ARTIFACT_FORMAT or payload.get("mesh_only") is not True:
        raise DomainMeshOnlyError("saved mesh-only artifact format/fields differ from its schema")
    if _canonical_json_bytes(payload) != mesh_path.read_bytes():
        raise DomainMeshOnlyError("saved mesh-only artifact is not canonical JSON")
    try:
        mesh = TriangularMesh(
            node_xy=payload["node_xy"], face_nodes=payload["face_nodes"], face_id=payload["face_id"],
            coordinate_system=payload["coordinate_system"], mesh_id=payload["mesh_id"],
            boundary_groups=payload["boundary_groups"], constraint_groups=payload["constraint_groups"],
            geometry_tolerance_m=payload["geometry_tolerance_m"],
            minimum_face_area_m2=payload["minimum_face_area_m2"],
        )
    except Exception as exc:
        raise DomainMeshOnlyError(f"saved domain mesh topology/IDs are invalid: {exc}") from exc
    settings = config["mesh_generation"]
    if (mesh.coordinate_system != config["target_coordinate_system"]
            or mesh.geometry_tolerance_m != float(settings["geometry_tolerance_m"])
            or mesh.minimum_face_area_m2 != float(settings["minimum_face_area_m2"])
            or mesh.n_node > settings["maximum_nodes"] or mesh.n_face > settings["maximum_faces"]):
        raise DomainMeshOnlyError("saved mesh CRS, geometry thresholds or count caps differ from trusted config")
    quality = mesh.quality_report(minimum_quality=float(settings["minimum_face_quality"]))
    if quality["faces_below_quality_threshold"]:
        raise DomainMeshOnlyError("saved mesh contains faces below trusted minimum_face_quality")
    if "security_exclusion_boundary" not in mesh.boundary_groups:
        raise DomainMeshOnlyError("saved mesh lacks the derived S exclusion boundary group")
    base_mesh = _mesh_without_security_boundary_group(mesh)
    try:
        generation = _generation_audit_settings(config, context.gmsh_config)
        geometry_audit = _audit_g0_mesh(base_mesh, context.gmsh_config, generation)
        security_audit = _audit_security_mesh(mesh, context, config)
    except (DomainMeshOnlyError, ValueError, TypeError, SecurityExclusionError) as exc:
        raise DomainMeshOnlyError(f"saved mesh failed trusted D/S/line/coverage audit: {exc}") from exc
    from .b1_synthetic_pilot import _gmsh_runtime_identity, _loaded_gmsh_library
    gmsh_identity = _gmsh_runtime_identity()
    gmsh_identity["native_library_loaded_in_worker"] = _loaded_gmsh_library(gmsh_identity["native_library_path"])
    gmsh_identity["python_binding_loaded_in_worker"] = "gmsh" in sys.modules
    gmsh_identity["worker_pid"] = os.getpid()
    if (not _current_process_in_job() or gmsh_identity["native_library_loaded_in_worker"] is not True
            or gmsh_identity["python_binding_loaded_in_worker"] is not True):
        raise DomainMeshOnlyError("Gmsh native library/binding is not loaded in the semantic reader Job member")
    expected_audit = _source_audit_record(context, mesh, config, geometry_audit, security_audit,
                                          gmsh_identity, code_hashes, expected_config_sha256)
    _compare_exact("source audit", stored_audit, expected_audit)
    return {"semantic_readback_passed": True, "mesh_face_count": mesh.n_face,
            "mesh_node_count": mesh.n_node, "geometry_audit": geometry_audit,
            "security_audit": security_audit}


class _QualityFailureDiagnosticError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _quality_failure_code_identity_sha256(code_hashes: Mapping[str, str]) -> str:
    return hashlib.sha256(_canonical_json_bytes(dict(code_hashes))).hexdigest()


def _quality_failure_primary_record(
    mesh: Any,
    quality: Mapping[str, Any],
    *,
    run_id: str,
    config_sha256: str,
    source_manifest_sha256: str,
    route_code_sha256: str,
    mesh_sha256: str,
    canonical_mesh_bytes: int,
) -> dict[str, Any]:
    bad_rows = quality.get("faces_below_quality_threshold")
    if not isinstance(bad_rows, list) or not bad_rows:
        raise _QualityFailureDiagnosticError(
            "QUALITY_FAILURE_PRIMARY_RECORD_INVALID",
            "quality failure receipt requires a non-empty strict-below-threshold face row list",
        )
    threshold = float(quality["minimum_quality_threshold"])
    return {
        "format": QUALITY_FAILURE_PRIMARY_FORMAT,
        "attempt_id": run_id,
        "run_id": run_id,
        "worker_exit_code": 53,
        "failure_code": "MINIMUM_FACE_QUALITY_FAILED",
        "message": f"generated mesh has faces below minimum_face_quality={threshold}",
        "diagnostic_only": True,
        "config_sha256": config_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "route_code_sha256": route_code_sha256,
        "metric_id": QUALITY_FAILURE_METRIC_ID,
        "quality_threshold": threshold,
        "comparison": "strict_lt",
        "node_count": int(mesh.n_node),
        "face_count": int(mesh.n_face),
        "bad_face_count": len(bad_rows),
        "mesh_sha256": mesh_sha256,
        "canonical_mesh_bytes": canonical_mesh_bytes,
    }


def _quality_failure_quantile_intervals(
    histogram: Sequence[int],
    *,
    face_count: int,
    underflow_count: int,
    minimum_quality: float,
    maximum_quality: float,
) -> list[dict[str, Any]]:
    bins = QUALITY_FAILURE_HISTOGRAM_BIN_COUNT
    intervals: list[dict[str, Any]] = []
    for percentile in QUALITY_FAILURE_QUANTILES:
        rank = max(1, math.ceil(percentile * face_count))
        if rank <= underflow_count:
            lower, upper = minimum_quality, 0.0
        else:
            cumulative = underflow_count
            lower = upper = 0.0
            found = False
            for index, count in enumerate(histogram):
                cumulative += count
                if cumulative >= rank:
                    lower = index / bins
                    upper = (index + 1) / bins
                    found = True
                    break
            if not found:
                lower, upper = 1.0, maximum_quality
        intervals.append({
            "percentile": percentile,
            "rank_one_based": rank,
            "lower_bound": float(lower),
            "upper_bound": float(upper),
            "interval_width": float(upper - lower),
            "exact_value_reported": False,
        })
    return intervals


def _build_quality_failure_diagnostic(
    mesh: Any,
    quality: Mapping[str, Any],
    primary: Mapping[str, Any],
    *,
    node_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded failure report from the in-Job mesh without full sorting/copying."""
    face_quality = mesh.face_quality
    face_count = int(mesh.n_face)
    if face_count < 1 or len(face_quality) != face_count:
        raise _QualityFailureDiagnosticError(
            "QUALITY_DIAGNOSTIC_MESH_SHAPE_INVALID",
            "face quality count differs from mesh face count",
        )
    histogram = [0] * QUALITY_FAILURE_HISTOGRAM_BIN_COUNT
    underflow_count = 0
    overflow_count = 0
    measured_bad_face_count = 0
    minimum_quality = math.inf
    maximum_quality = -math.inf
    worst_heap: list[tuple[float, int, int, int]] = []
    threshold = float(quality["minimum_quality_threshold"])
    for row, raw_quality in enumerate(face_quality):
        value = float(raw_quality)
        if not math.isfinite(value):
            raise _QualityFailureDiagnosticError(
                "QUALITY_DIAGNOSTIC_NONFINITE_VALUE",
                f"face_quality[{row}] is not finite",
            )
        if value < threshold:
            measured_bad_face_count += 1
        minimum_quality = min(minimum_quality, value)
        maximum_quality = max(maximum_quality, value)
        if value < 0.0:
            underflow_count += 1
        elif value > 1.0:
            overflow_count += 1
        else:
            index = min(QUALITY_FAILURE_HISTOGRAM_BIN_COUNT - 1,
                        int(value * QUALITY_FAILURE_HISTOGRAM_BIN_COUNT))
            histogram[index] += 1
        face_id = int(mesh.face_id[row])
        entry = (-value, -face_id, -row, row)
        if len(worst_heap) < QUALITY_FAILURE_DIAGNOSTIC_TOP_K:
            heapq.heappush(worst_heap, entry)
        elif entry > worst_heap[0]:
            heapq.heapreplace(worst_heap, entry)

    bad_rows = quality.get("faces_below_quality_threshold")
    if (not isinstance(bad_rows, list)
            or len(bad_rows) != int(primary["bad_face_count"])
            or measured_bad_face_count != int(primary["bad_face_count"])):
        raise _QualityFailureDiagnosticError(
            "QUALITY_DIAGNOSTIC_BAD_FACE_COUNT_MISMATCH",
            "strict-below-threshold rows differ from the primary quality failure count",
        )
    worst_rows = sorted(
        (item[3] for item in worst_heap),
        key=lambda row: (float(face_quality[row]), int(mesh.face_id[row]), row),
    )
    edge_to_worst_rows: dict[int, list[int]] = {}
    for row in worst_rows:
        for edge_index in mesh.face_edges[row]:
            edge_to_worst_rows.setdefault(int(edge_index), []).append(row)
    groups_by_row = {
        row: {"boundary": set(), "constraint": set()}
        for row in worst_rows
    }
    for group_name, edge_indices in sorted(mesh.boundary_groups.items()):
        for edge_index in edge_indices:
            for row in edge_to_worst_rows.get(int(edge_index), ()):
                groups_by_row[row]["boundary"].add(group_name)
    for group_name, edge_indices in sorted(mesh.constraint_groups.items()):
        for edge_index in edge_indices:
            for row in edge_to_worst_rows.get(int(edge_index), ()):
                groups_by_row[row]["constraint"].add(group_name)

    worst_faces: list[dict[str, Any]] = []
    for row in worst_rows:
        node_indices = tuple(int(index) for index in mesh.face_nodes[row])
        points = tuple(mesh.node_xy[index] for index in node_indices)
        xs = tuple(float(point[0]) for point in points)
        ys = tuple(float(point[1]) for point in points)
        edge_indices = tuple(int(index) for index in mesh.face_edges[row])
        worst_faces.append({
            "face_row_zero_based": row,
            "face_id_decimal": str(int(mesh.face_id[row])),
            "quality": float(face_quality[row]),
            "minimum_angle_deg": float(mesh.minimum_angle_deg[row]),
            "face_area_m2": float(mesh.face_area[row]),
            "centroid_xy_m": [float(value) for value in mesh.face_centroid[row]],
            "bounds_xy_m": [min(xs), min(ys), max(xs), max(ys)],
            "node_indices_zero_based": list(node_indices),
            "edge_indices_zero_based": list(edge_indices),
            "edge_node_indices_zero_based": [
                [int(node) for node in mesh.edge_nodes[index]]
                for index in edge_indices
            ],
            "edge_lengths_m": [float(mesh.edge_length[index]) for index in edge_indices],
            "boundary_group_names": sorted(groups_by_row[row]["boundary"]),
            "constraint_group_names": sorted(groups_by_row[row]["constraint"]),
        })

    diagnostic_format = QUALITY_FAILURE_DIAGNOSTIC_FORMAT
    diagnostic_schema_version = QUALITY_FAILURE_DIAGNOSTIC_SCHEMA_VERSION
    if node_provenance is not None:
        nodes = node_provenance.get("nodes")
        faces = node_provenance.get("faces")
        if (not isinstance(nodes, list) or not isinstance(faces, list)
                or node_provenance.get("node_limit") != 48
                or node_provenance.get("selected_node_count") != len(nodes)
                or node_provenance.get("selected_face_limit") != 16
                or node_provenance.get("selected_face_count") != len(faces)
                or len(nodes) > 48 or len(faces) != len(worst_faces)
                or node_provenance.get("node_classification_source") != "gmsh.model.mesh.getNode before gmsh.finalize"):
            raise _QualityFailureDiagnosticError(
                "QUALITY_DIAGNOSTIC_GMSH_NODE_PROVENANCE_INVALID",
                "bounded Gmsh node classification does not match the top-face selection",
            )
        nodes_by_index = {
            int(item["mesh_node_index_zero_based"]): item for item in nodes
            if isinstance(item, Mapping) and isinstance(item.get("mesh_node_index_zero_based"), int)
        }
        if len(nodes_by_index) != len(nodes):
            raise _QualityFailureDiagnosticError(
                "QUALITY_DIAGNOSTIC_GMSH_NODE_PROVENANCE_INVALID",
                "sampled Gmsh node indices must be unique",
            )
        node_tags_by_index: dict[int, str] = {}
        for index, item in nodes_by_index.items():
            tag = item.get("gmsh_node_tag_decimal")
            xyz = item.get("xyz_m")
            entity_dim = item.get("entity_dim")
            entity_tag = item.get("entity_tag_decimal")
            if (not isinstance(tag, str) or re.fullmatch(r"[1-9][0-9]{0,18}", tag) is None
                    or not isinstance(xyz, list) or len(xyz) != 3
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(float(value)) for value in xyz)
                    or type(entity_dim) is not int or entity_dim not in (0, 1, 2)
                    or not isinstance(entity_tag, str)
                    or re.fullmatch(r"[1-9][0-9]{0,18}", entity_tag) is None):
                raise _QualityFailureDiagnosticError(
                    "QUALITY_DIAGNOSTIC_GMSH_NODE_PROVENANCE_INVALID",
                    "sampled Gmsh node has malformed tag, coordinate, or entity classification",
                )
            node_tags_by_index[index] = tag
        if len(set(node_tags_by_index.values())) != len(node_tags_by_index):
            raise _QualityFailureDiagnosticError(
                "QUALITY_DIAGNOSTIC_GMSH_NODE_PROVENANCE_INVALID",
                "sampled Gmsh node tags must be unique",
            )
        faces_by_row = {
            int(item["face_row_zero_based"]): item for item in faces
            if isinstance(item, Mapping) and isinstance(item.get("face_row_zero_based"), int)
        }
        if len(faces_by_row) != len(faces):
            raise _QualityFailureDiagnosticError(
                "QUALITY_DIAGNOSTIC_GMSH_NODE_PROVENANCE_INVALID",
                "sampled Gmsh face rows must be unique",
            )
        for face in worst_faces:
            sampled = faces_by_row.get(face["face_row_zero_based"])
            if sampled is None or sampled.get("face_id_decimal") != face["face_id_decimal"]:
                raise _QualityFailureDiagnosticError(
                    "QUALITY_DIAGNOSTIC_GMSH_NODE_PROVENANCE_INVALID",
                    "sampled Gmsh face identity differs from the trusted quality selection",
                )
            expected_indices = face["node_indices_zero_based"]
            indices = sampled.get("mesh_node_indices_zero_based")
            tags = sampled.get("gmsh_node_tags_decimal")
            if (indices != expected_indices or not isinstance(tags, list) or len(tags) != 3
                    or any(index not in nodes_by_index for index in expected_indices)
                    or tags != [node_tags_by_index[index] for index in expected_indices]):
                raise _QualityFailureDiagnosticError(
                    "QUALITY_DIAGNOSTIC_GMSH_NODE_PROVENANCE_INVALID",
                    "sampled Gmsh node references do not cover a selected face",
                )
            face["gmsh_node_tags_decimal"] = tags
        diagnostic_format = QUALITY_FAILURE_DIAGNOSTIC_V2_FORMAT
        diagnostic_schema_version = QUALITY_FAILURE_DIAGNOSTIC_V2_SCHEMA_VERSION

    quantile_intervals = _quality_failure_quantile_intervals(
        histogram,
        face_count=face_count,
        underflow_count=underflow_count,
        minimum_quality=minimum_quality,
        maximum_quality=maximum_quality,
    )
    diagnostic = {
        "format": diagnostic_format,
        "schema_version": diagnostic_schema_version,
        "diagnostic_only": True,
        "preparation_only": True,
        "mesh_ready": False,
        "ready": False,
        "latest_published": False,
        "simulation_result": False,
        "production_approved": False,
        "engineering_ready": False,
        "attempt_id": primary["attempt_id"],
        "run_id": primary["run_id"],
        "config_sha256": primary["config_sha256"],
        "source_manifest_sha256": primary["source_manifest_sha256"],
        "route_code_sha256": primary["route_code_sha256"],
        "failure_code": primary["failure_code"],
        "worker_exit_code": int(primary["worker_exit_code"]),
        "failure_message": primary["message"],
        "metric": {
            "id": QUALITY_FAILURE_METRIC_ID,
            "version": QUALITY_FAILURE_METRIC_ID,
            "definition": "q = 4*sqrt(3)*A/(a^2+b^2+c^2)",
            "unit": "1",
            "area_source": "validated positive mesh.face_area in m2",
            "edge_length_source": "validated mesh edge lengths in m",
            "threshold": float(primary["quality_threshold"]),
            "comparison": "strict_lt",
        },
        "mesh": {
            "mesh_id": str(mesh.mesh_id),
            "mesh_sha256": primary["mesh_sha256"],
            "canonical_mesh_bytes": int(primary["canonical_mesh_bytes"]),
            "node_count": int(mesh.n_node),
            "face_count": face_count,
        },
        "quality_summary": {
            "minimum_quality_exact": float(minimum_quality),
            "maximum_quality_exact": float(maximum_quality),
            "bad_face_count_exact": int(primary["bad_face_count"]),
            "comparison": "strict_lt",
            "histogram": {
                "method": "fixed_equal_width_histogram_approximate_v1",
                "domain_lower": 0.0,
                "domain_upper": 1.0,
                "bin_count": QUALITY_FAILURE_HISTOGRAM_BIN_COUNT,
                "bin_width": 1.0 / QUALITY_FAILURE_HISTOGRAM_BIN_COUNT,
                "counts": histogram,
                "underflow_count": underflow_count,
                "overflow_count": overflow_count,
            },
            "quantile_rule": "nearest-rank: rank=ceil(p*N), one-based; return the containing histogram bin interval; equal values share a bin and are not ordered for quantiles",
            "quantiles_approximate": True,
            "maximum_quantile_interval_width": max(
                (float(item["interval_width"]) for item in quantile_intervals), default=0.0,
            ),
            "quantile_intervals": quantile_intervals,
        },
        "worst_face_selection": {
            "population": "all faces",
            "limit": QUALITY_FAILURE_DIAGNOSTIC_TOP_K,
            "selected_count": len(worst_faces),
            "exact": True,
            "order": "quality ascending, then numeric face_id ascending, then zero-based row ascending",
            "faces": worst_faces,
        },
    }
    if node_provenance is not None:
        diagnostic["sampled_node_provenance"] = {
            "mesh_sha256": primary["mesh_sha256"],
            "route_code_sha256": primary["route_code_sha256"],
            "node_classification_source": node_provenance["node_classification_source"],
            "node_limit": 48,
            "selected_node_count": len(node_provenance["nodes"]),
            "nodes": [dict(item) for item in node_provenance["nodes"]],
        }
    return diagnostic


def _quality_failure_error_record(exc: Exception, *, phase: str) -> dict[str, str]:
    if isinstance(exc, InterruptedError):
        code = "DIAGNOSTIC_INTERRUPTED"
    elif isinstance(exc, FileExistsError):
        code = "DIAGNOSTIC_NO_CLOBBER_CONFLICT"
    elif type(exc).__name__ == "ResourceBudgetError":
        code = "DIAGNOSTIC_BUDGET_REJECTED"
    elif isinstance(exc, _QualityFailureDiagnosticError):
        code = exc.code
    else:
        code = "DIAGNOSTIC_WRITE_FAILED"
    return {
        "phase": phase,
        "code": code,
        "type": type(exc).__name__,
        "message": str(exc)[:512],
    }


def _persist_quality_failure_primary(
    stage: Path,
    primary: Mapping[str, Any],
    *,
    scratch_budget_bytes: int,
) -> dict[str, str] | None:
    try:
        payload = _canonical_json_bytes(dict(primary))
        if len(payload) > QUALITY_FAILURE_PENDING_MAX_BYTES:
            raise _QualityFailureDiagnosticError(
                "QUALITY_FAILURE_PRIMARY_RECEIPT_CAP_EXCEEDED",
                f"quality failure primary record exceeds {QUALITY_FAILURE_PENDING_MAX_BYTES} bytes",
            )
        from .restricted_worker import _write_no_clobber_bytes

        scratch = stage / "scratch"
        path = stage / QUALITY_FAILURE_PENDING_RELATIVE_PATH
        _write_no_clobber_bytes(
            path,
            payload,
            output_budget=scratch_budget_bytes,
            root=scratch,
            reserve_bytes=4096,
        )
        return None
    except Exception as exc:
        return _quality_failure_error_record(exc, phase="quality_failure_primary_receipt")


def _persist_quality_failure_diagnostic(
    stage: Path,
    diagnostic: Mapping[str, Any],
    *,
    output_budget_bytes: int,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    try:
        payload = _canonical_json_bytes(dict(diagnostic))
        if len(payload) > QUALITY_FAILURE_DIAGNOSTIC_MAX_BYTES:
            raise _QualityFailureDiagnosticError(
                "DIAGNOSTIC_BYTE_CAP_EXCEEDED",
                f"quality failure diagnostic is {len(payload)} bytes; cap is {QUALITY_FAILURE_DIAGNOSTIC_MAX_BYTES}",
            )
        if diagnostic.get("format") == QUALITY_FAILURE_DIAGNOSTIC_V2_FORMAT:
            relative_path = QUALITY_FAILURE_DIAGNOSTIC_V2_RELATIVE_PATH
        else:
            relative_path = QUALITY_FAILURE_DIAGNOSTIC_RELATIVE_PATH
        path = stage / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        from .restricted_worker import _write_no_clobber_bytes

        _write_no_clobber_bytes(
            path,
            payload,
            output_budget=output_budget_bytes,
            root=stage,
            reserve_bytes=OUTPUT_RESERVE_BYTES,
        )
        return {
            "path": relative_path,
            "format": diagnostic["format"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "diagnostic_only": True,
        }, None
    except Exception as exc:
        return None, _quality_failure_error_record(exc, phase="quality_failure_diagnostic")


def _make_quality_failure_exception(
    primary: Mapping[str, Any],
    diagnostic_reference: Mapping[str, Any] | None,
    diagnostic_errors: Sequence[Mapping[str, str]],
) -> DomainMeshOnlyError:
    error = DomainMeshOnlyError(str(primary["message"]))
    error.quality_failure = dict(primary)
    error.failure_diagnostic = None if diagnostic_reference is None else dict(diagnostic_reference)
    error.failure_diagnostic_error = (
        {"errors": [dict(item) for item in diagnostic_errors]} if diagnostic_errors else None
    )
    return error


def run_domain_mesh_only_worker(control: Mapping[str, Any], config: dict[str, Any], root: Path) -> dict[str, Any]:
    """Generate and semantically read back one D−S mesh; terrain APIs are never imported."""
    if not _current_process_in_job():
        raise DomainMeshOnlyError("domain mesh-only worker is not in its assigned Job Object")
    validate_domain_mesh_only_config_shape(config)
    context = _load_geometry_context(config, root, verify_component_sha=True)
    if not _current_process_in_job():
        raise DomainMeshOnlyError("worker left its Job before Gmsh identity check")
    from . import mesh_generation
    from .b1_synthetic_pilot import (_audit_g0_mesh, _gmsh_runtime_identity, _loaded_gmsh_library,
                                    _mesh_payload)
    from .restricted_worker import _tree_bytes, _write_no_clobber_bytes
    settings = config["mesh_generation"]
    gmsh_identity_before = _gmsh_runtime_identity()
    if (gmsh_identity_before["python_binding_version"] != config["expected_gmsh_version"]
            or gmsh_identity_before["native_library_sha256"] != config["expected_gmsh_library_sha256"]):
        from .restricted_worker import SourceIdentityError
        raise SourceIdentityError("Gmsh binding/DLL differs from the trusted mesh-only config")
    stage=Path(control["attempt_root"]); scratch=stage/"scratch"; artifacts=stage/"artifacts"
    resources=config["resources"]
    mesh_algorithm_plan=config.get("mesh_algorithm_plan")
    generation_receipt: dict[str, Any] | None = {} if mesh_algorithm_plan is not None else None
    started=time.perf_counter()
    try:
        mesh=mesh_generation.generate_gmsh_mesh(
            context.gmsh_config,coordinate_system=config["target_coordinate_system"],
            geometry_tolerance_m=float(settings["geometry_tolerance_m"]),
            minimum_face_area_m2=float(settings["minimum_face_area_m2"]),
            mesh_algorithm_plan=mesh_algorithm_plan,
            generation_receipt=generation_receipt,
        )
    except Exception as exc:
        if generation_receipt is not None:
            generation_receipt.update({
                "attempt_id": str(config["run_id"]),
                "config_sha256": str(control["config_sha256"]),
                "source_manifest_sha256": context.manifest_sha256,
                "route_code_sha256": _quality_failure_code_identity_sha256(control["code_hashes"]),
                "gmsh_identity": dict(gmsh_identity_before),
                "generation_error": {"type": type(exc).__name__, "message": str(exc)[:512]},
            })
            try:
                _persist_mesh_algorithm_execution_receipt(
                    stage, generation_receipt,
                    scratch_budget_bytes=int(resources["scratch_budget_bytes"]),
                )
            except Exception as receipt_exc:
                raise DomainMeshOnlyError(
                    f"{exc}; mesh algorithm execution receipt also failed: {type(receipt_exc).__name__}: {receipt_exc}"
                ) from exc
        from .mesh_preparation import MeshPreparationError
        if isinstance(exc, MeshPreparationError):
            raise DomainMeshOnlyError(str(exc)) from exc
        raise
    gmsh_seconds=time.perf_counter()-started
    gmsh_identity_after = _gmsh_runtime_identity()
    if any(gmsh_identity_after[key] != gmsh_identity_before[key] for key in (
            "python_binding_path", "python_binding_version", "native_library_path", "native_library_sha256", "native_library_bytes")):
        from .restricted_worker import SourceIdentityError
        raise SourceIdentityError("Gmsh binding/DLL identity changed during mesh generation")
    gmsh_identity=_seal_gmsh_worker_identity(gmsh_identity_after)
    mesh_payload=_mesh_payload(mesh,full=True)
    mesh_payload["format"]=DOMAIN_MESH_ARTIFACT_FORMAT
    mesh_payload["mesh_only"]=True
    mesh_bytes=_canonical_json_bytes(mesh_payload); mesh_sha=hashlib.sha256(mesh_bytes).hexdigest()
    algorithm_execution_reference = None
    if generation_receipt is not None:
        generation_receipt.update({
            "attempt_id": str(config["run_id"]),
            "config_sha256": str(control["config_sha256"]),
            "source_manifest_sha256": context.manifest_sha256,
            "route_code_sha256": _quality_failure_code_identity_sha256(control["code_hashes"]),
            "gmsh_identity": dict(gmsh_identity),
            "gmsh_generation_seconds": gmsh_seconds,
            "mesh_identity": {
                "mesh_id": str(mesh.mesh_id), "mesh_sha256": mesh_sha,
                "canonical_mesh_bytes": len(mesh_bytes),
                "node_count": mesh.n_node, "face_count": mesh.n_face,
            },
        })
        algorithm_execution_reference = _persist_mesh_algorithm_execution_receipt(
            stage, generation_receipt,
            scratch_budget_bytes=int(resources["scratch_budget_bytes"]),
        )
    from .restricted_worker import _write_no_clobber_bytes
    generated_record={"format":"triangle-domain-mesh-only-generated-v1","worker_pid":os.getpid(),
                      "process_in_job":True,"gmsh_native_library_loaded":gmsh_identity["native_library_loaded_in_worker"],
                      "mesh_sha256":mesh_sha,"node_count":mesh.n_node,"face_count":mesh.n_face}
    if algorithm_execution_reference is not None:
        generated_record["mesh_algorithm_execution"] = algorithm_execution_reference
    _write_no_clobber_bytes(scratch/"domain-mesh-generated.json",
        _canonical_json_bytes(generated_record),
        output_budget=resources["scratch_budget_bytes"],root=scratch)
    if mesh.n_node>settings["maximum_nodes"] or mesh.n_face>settings["maximum_faces"]:
        raise DomainMeshOnlyError(f"generated mesh exceeds configured cap: {mesh.n_node} nodes/{mesh.n_face} faces")
    quality=mesh.quality_report(minimum_quality=float(settings["minimum_face_quality"]))
    if quality["faces_below_quality_threshold"]:
        route_code_sha256 = _quality_failure_code_identity_sha256(control["code_hashes"])
        quality_failure = _quality_failure_primary_record(
            mesh,
            quality,
            run_id=str(config["run_id"]),
            config_sha256=str(control["config_sha256"]),
            source_manifest_sha256=context.manifest_sha256,
            route_code_sha256=route_code_sha256,
            mesh_sha256=mesh_sha,
            canonical_mesh_bytes=len(mesh_bytes),
        )
        diagnostic_errors: list[dict[str, str]] = []
        pending_error = _persist_quality_failure_primary(
            stage, quality_failure,
            scratch_budget_bytes=int(resources["scratch_budget_bytes"]),
        )
        if pending_error is not None:
            diagnostic_errors.append(pending_error)
        diagnostic_reference: dict[str, Any] | None = None
        try:
            diagnostic = _build_quality_failure_diagnostic(
                mesh, quality, quality_failure,
                node_provenance=(generation_receipt or {}).get("quality_node_provenance"),
            )
            diagnostic_reference, diagnostic_error = _persist_quality_failure_diagnostic(
                stage, diagnostic,
                output_budget_bytes=int(resources["output_budget_bytes"]),
            )
        except Exception as exc:
            diagnostic_error = _quality_failure_error_record(exc, phase="quality_failure_diagnostic_build")
        if diagnostic_error is not None:
            diagnostic_errors.append(diagnostic_error)
        raise _make_quality_failure_exception(
            quality_failure, diagnostic_reference, diagnostic_errors,
        )
    mesh_without_security=mesh
    from .b1_synthetic_pilot import _audit_g0_mesh
    generation=_generation_audit_settings(config,context.gmsh_config)
    geometry_audit=_audit_g0_mesh(mesh_without_security,context.gmsh_config,generation)
    from .security_exclusion import SecurityExclusionError, check_mesh_against_security
    try:
        security_check=check_mesh_against_security(mesh_without_security,context.security_assessment,
                                                  tolerance_m=float(settings["geometry_tolerance_m"]))
    except SecurityExclusionError as exc:
        raise DomainMeshOnlyError(f"generated mesh failed D−S/S exclusion checks: {exc}") from exc
    secured_mesh=security_check.mesh
    audit_record=_source_audit_record(context,secured_mesh,config,geometry_audit,
        {"original_domain_area_m2":context.security_assessment.original_domain_area_m2,
         "security_union_area_m2":context.security_assessment.security_union_area_m2,
         "security_inside_area_m2":context.security_assessment.security_inside_area_m2,
         "security_outside_area_m2":context.security_assessment.security_outside_area_m2,
         "active_domain_area_m2":context.security_assessment.active_domain_area_m2,
         "active_mesh_area_m2":security_check.active_mesh_area_m2,
         "coverage_residual_m2":security_check.coverage_residual_m2,
         "security_boundary_edge_count":len(security_check.security_boundary_edge_indices),
         "security_boundary_edge_sha256":security_check.security_boundary_edge_sha256,
         "security_boundary_group_name":"security_exclusion_boundary"},
        gmsh_identity,control["code_hashes"],control["config_sha256"])
    mesh_payload=_mesh_payload(secured_mesh,full=True);mesh_payload["format"]=DOMAIN_MESH_ARTIFACT_FORMAT;mesh_payload["mesh_only"]=True
    mesh_bytes=_canonical_json_bytes(mesh_payload)
    if len(mesh_bytes) > MAX_COMPONENT_TOTAL_BYTES:
        raise DomainMeshOnlyError(f"MESH_ARTIFACT_BYTE_CAP: mesh artifact exceeds {MAX_COMPONENT_TOTAL_BYTES} bytes")
    mesh_sha=hashlib.sha256(mesh_bytes).hexdigest()
    audit_bytes=_canonical_json_bytes(audit_record)
    if len(audit_bytes) > MAX_COMPONENT_TOTAL_BYTES:
        raise DomainMeshOnlyError(f"AUDIT_ARTIFACT_BYTE_CAP: source audit exceeds {MAX_COMPONENT_TOTAL_BYTES} bytes")
    audit_sha=hashlib.sha256(audit_bytes).hexdigest()
    mesh_file=_write_route_file(artifacts/"domain-mesh.json",mesh_bytes,output_budget=resources["output_budget_bytes"],root=stage)
    audit_file=_write_route_file(artifacts/"domain-source-audit.json",audit_bytes,output_budget=resources["output_budget_bytes"],root=stage)
    semantic_context=_new_worker_semantic_context(stage,control["config_sha256"],context.manifest_sha256)
    semantic=validate_domain_mesh_bundle_semantics(
        stage,config_path=control["config_path"],expected_config_sha256=control["config_sha256"],
        project_root=root,expected_mesh_sha256=mesh_sha,expected_source_audit_sha256=audit_sha,
        code_hashes=control["code_hashes"],worker_context=semantic_context)
    if _tree_bytes(stage,skip_names={"scratch"})>resources["output_budget_bytes"] or _tree_bytes(scratch)>resources["scratch_budget_bytes"]:
        raise DomainMeshOnlyError("mesh-only output or private scratch exceeds its configured byte budget")
    return {
        "status":"success","category":"mesh_generation_diagnostic_complete",
        "message":"restricted synthetic D−S mesh generated and trusted-context readback passed",
        "route":"triangle-domain-mesh-only-generate-v1","mesh_only":True,
        "preparation_only":True,"simulation_result":False,"production_approved":False,"engineering_ready":False,
        "config_sha256":control["config_sha256"],"source_manifest_sha256":context.manifest_sha256,
        "route_code_sha256":dict(control["code_hashes"]),"mesh_file":mesh_file,"source_audit_file":audit_file,
        "mesh_summary":{"mesh_id":secured_mesh.mesh_id,"node_count":secured_mesh.n_node,"face_count":secured_mesh.n_face,
                        "face_id_sequence_sha256":_face_id_sha256(secured_mesh.face_id),
                        "geometry_fingerprint_sha256":_domain_mesh_fingerprint(mesh_payload),
                        "mesh_area_m2":math.fsum(float(x) for x in secured_mesh.face_area)},
        "quality_report":quality,"geometry_audit":geometry_audit,
        "security_audit":audit_record["security_mesh_audit"],"gmsh":gmsh_identity,
        "gmsh_generation_seconds":gmsh_seconds,"semantic_readback":semantic,
        "source_components_before":list(context.components_before),"source_components_after":list(context.components_after),
        "source_manifest_stat_identity":dict(context.manifest_stat),
        "output_bytes_written":mesh_file["bytes"]+audit_file["bytes"],
        **({"mesh_algorithm_execution": dict(generation_receipt)}
           if generation_receipt is not None else {}),
    }


def read_domain_mesh_bundle_identity(
    bundle_directory: str | Path,
    *,
    expected_config_sha256: str,
    expected_source_manifest_sha256: str,
) -> dict[str, Any]:
    """Read only the mesh-only envelope and byte identities; this is not semantic acceptance."""
    from .restricted_worker import _sha256_file

    _validate_sha256(expected_config_sha256, "expected_config_sha256")
    _validate_sha256(expected_source_manifest_sha256, "expected_source_manifest_sha256")
    root = Path(bundle_directory)
    if root.is_symlink() or not root.is_dir():
        raise DomainMeshOnlyError("mesh-only bundle must be a regular directory")
    if any((root / name).exists() for name in ("READY", "READY.pending", "latest")):
        raise DomainMeshOnlyError("mesh-only bundle must not contain terrain/engineering READY or latest markers")
    marker_path = root / "MESH_READY"
    manifest_path = root / "manifest.json"
    status_path = root / "status.json"
    if any(path.is_symlink() or not path.is_file() for path in (marker_path, manifest_path, status_path)):
        raise DomainMeshOnlyError("mesh-only marker, manifest and status must be regular files")
    if manifest_path.stat().st_size > 1024 * 1024 or status_path.stat().st_size > 64 * 1024:
        raise DomainMeshOnlyError("mesh-only manifest/status exceeds its fixed reader size limit")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainMeshOnlyError(f"mesh-only envelope JSON is malformed: {exc}") from exc
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if (not isinstance(manifest, dict) or manifest.get("format") != DOMAIN_MESH_BUNDLE_FORMAT
            or manifest.get("route") != DOMAIN_MESH_ONLY_ROUTE
            or root.name != manifest.get("run_id")
            or manifest.get("mesh_only") is not True or manifest.get("preparation_only") is not True
            or manifest.get("simulation_result") is not False or manifest.get("production_approved") is not False
            or manifest.get("engineering_ready") is not False or manifest.get("latest_published") is not False
            or manifest.get("mesh_ready") is not True
            or manifest.get("resources") != RESOURCE_LIMITS
            or manifest.get("output_budget_bytes") != RESOURCE_LIMITS["output_budget_bytes"]
            or manifest.get("scratch_budget_bytes") != RESOURCE_LIMITS["scratch_budget_bytes"]
            or manifest.get("config_sha256") != expected_config_sha256
            or manifest.get("source_manifest_sha256") != expected_source_manifest_sha256):
        raise DomainMeshOnlyError("mesh-only manifest flags or trusted config/source bindings are invalid")
    if (not isinstance(marker, dict) or marker.get("format") != DOMAIN_MESH_READY_FORMAT
            or marker.get("manifest_sha256") != manifest_sha or marker.get("run_id") != manifest.get("run_id")
            or marker.get("mesh_only") is not True or marker.get("simulation_result") is not False
            or marker.get("production_approved") is not False or marker.get("engineering_ready") is not False):
        raise DomainMeshOnlyError("MESH_READY does not bind the mesh-only diagnostic manifest")
    if (not isinstance(status, dict) or status.get("format") != "triangle-domain-mesh-only-status-v1"
            or status.get("status") != "mesh_generation_diagnostic_complete"
            or status.get("exit_code") != 0 or status.get("run_id") != manifest.get("run_id")
            or status.get("mesh_only") is not True or status.get("mesh_ready") is not True
            or status.get("ready") is not False or status.get("simulation_result") is not False
            or status.get("production_approved") is not False or status.get("engineering_ready") is not False
            or status.get("latest_published") is not False):
        raise DomainMeshOnlyError("mesh-only status is not a completed non-engineering diagnostic")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"mesh_file", "source_audit_file", "worker_result_file"}:
        raise DomainMeshOnlyError("mesh-only manifest must bind the mesh, source audit and worker receipt")
    artifacts = root / "artifacts"
    if artifacts.is_symlink() or not artifacts.is_dir():
        raise DomainMeshOnlyError("mesh-only artifacts must be a regular directory")
    for key, expected_name in (("mesh_file", "domain-mesh.json"),
                               ("source_audit_file", "domain-source-audit.json"),
                               ("worker_result_file", "worker-result.json")):
        record = files[key]
        if not isinstance(record, dict) or set(record) != {"name", "sha256", "bytes"} or record.get("name") != expected_name:
            raise DomainMeshOnlyError(f"mesh-only {key} identity record is invalid")
        path = artifacts / expected_name
        if path.is_symlink() or not path.is_file():
            raise DomainMeshOnlyError(f"mesh-only artifact is missing or a symlink: {expected_name}")
        maximum = MAX_OUTPUT_RECEIPT_BYTES if key == "worker_result_file" else manifest.get("output_budget_bytes")
        digest, size = _sha256_file(path, maximum_bytes=maximum)
        if digest != record.get("sha256") or size != record.get("bytes"):
            raise DomainMeshOnlyError(f"mesh-only artifact bytes differ from manifest: {expected_name}")
    forbidden = {"prepared_faces.jsonl", "prepared_body.json", "simulation.nc", "result.nc"}
    actual = {p.name for p in artifacts.iterdir()}
    if actual != {"domain-mesh.json", "domain-source-audit.json", "worker-result.json"} or actual & forbidden:
        raise DomainMeshOnlyError("mesh-only artifact directory contains unexpected terrain/result files")
    return {"manifest": manifest, "status": status, "mesh_path": artifacts / "domain-mesh.json",
            "source_audit_path": artifacts / "domain-source-audit.json", "manifest_sha256": manifest_sha,
            "identity_only": True, "semantic_validation_performed": False}
