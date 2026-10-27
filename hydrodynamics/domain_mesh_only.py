"""Restricted, preparation-only D−S mesh generation from trusted geometry sources.

The module has no terrain/DEM imports. Heavy geometry parsing, Gmsh loading,
generation, auditing, writing, and semantic readback are entered only from the
restricted worker after its Job Object membership has been checked.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
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
from typing import Any, Mapping


DOMAIN_MESH_ONLY_FORMAT = "triangle-domain-mesh-only-config-v1"
DOMAIN_MESH_ONLY_VERSION = "1.0"
DOMAIN_MESH_ONLY_ROUTE = "triangle-domain-mesh-only-generate-v1"
DOMAIN_SOURCE_MANIFEST_FORMAT = "triangle-domain-mesh-source-manifest-v1"
DOMAIN_SOURCE_MANIFEST_VERSION = "1.0"
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
    fields = {"format", "schema_version", "run_id", "output_root", "resources",
              "source_manifest", "target_coordinate_system", "mesh_generation",
              "expected_gmsh_version", "expected_gmsh_library_sha256"}
    if set(config) != fields:
        raise ValueError("domain mesh-only config fields differ from its frozen contract")
    if config["format"] != DOMAIN_MESH_ONLY_FORMAT or config["schema_version"] != DOMAIN_MESH_ONLY_VERSION:
        raise ValueError("unsupported domain mesh-only config format/version")
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
    if manifest["format"] != DOMAIN_SOURCE_MANIFEST_FORMAT or manifest["schema_version"] != DOMAIN_SOURCE_MANIFEST_VERSION:
        raise ValueError("unsupported domain mesh-only source manifest format/version")
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
    for index, component in enumerate(components):
        label = f"components[{index}]"
        if not isinstance(component, dict) or set(component) != {
                "component_id", "role", "format", "source_crs", "files", "group_name", "closed"}:
            raise ValueError(f"{label} fields differ from the frozen contract")
        component_id = component["component_id"]
        if (not isinstance(component_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", component_id) is None
                or component_id in ids):
            raise ValueError(f"{label}.component_id must be unique and 1–64 safe characters")
        ids.add(component_id)
        role, fmt = component["role"], component["format"]
        allowed = {"domain_D": {"polygon_geojson", "polygon_shapefile"},
                   "security_S": {"polygon_geojson", "polygon_shapefile"},
                   "boundary_line": {"line_geojson", "line_shapefile"},
                   "constraint_line": {"line_geojson", "line_shapefile"}}
        if not isinstance(role, str) or not isinstance(fmt, str) or role not in allowed or fmt not in allowed[role]:
            raise ValueError(f"{label} role/format pair is unsupported")
        if not isinstance(component["source_crs"], str) or not component["source_crs"].strip():
            raise ValueError(f"{label}.source_crs is required")
        is_line = role in {"boundary_line", "constraint_line"}
        if is_line:
            line_count += 1
            name = component["group_name"]
            if (not isinstance(name, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) is None
                    or name in line_names):
                raise ValueError(f"{label}.group_name must be a unique 1–64 character safe ID")
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
            if not isinstance(file_record, dict) or set(file_record) != {"path", "sha256", "expected_stat_identity"}:
                raise ValueError(f"{file_label} fields differ from the frozen file identity contract")
            _safe_relative(file_record["path"], f"{file_label}.path")
            _validate_sha256(file_record["sha256"], f"{file_label}.sha256")
            _validate_stat_identity(file_record["expected_stat_identity"], f"{file_label}.expected_stat_identity")
    if domain_count != 1 or security_count != 1 or line_count < 1:
        raise ValueError("source manifest requires exactly one D, one S, and at least one source boundary/constraint line")
    if len({x["component_id"] for x in components}) != len(components):
        raise ValueError("source manifest component IDs must be unique")
    return manifest


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
    file_stats, total_bytes = _component_file_records(root, path, manifest, verify_sha=False)
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


def _load_polygon_component(component: Mapping[str, Any], manifest_path: Path,
                            root: Path, target_crs_text: str) -> _PolygonComponent:
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
    source_crs = CRS.from_user_input(component["source_crs"])
    target_crs = CRS.from_user_input(target_crs_text)
    if component["format"] == "polygon_shapefile":
        geometries, prj_text, source_file_sha = _read_shapefile_geometries(path)
        if not _crs_equivalent(CRS.from_wkt(prj_text), source_crs):
            raise DomainMeshOnlyError(f"{component['component_id']} explicit source_crs differs from its PRJ")
    else:
        geometries, source_file_sha, declared_crs = _read_geojson_geometries(path)
        if (declared_crs is not None
                and not _crs_equivalent(CRS.from_user_input(declared_crs), source_crs)):
            raise DomainMeshOnlyError(f"{component['component_id']} GeoJSON CRS differs from its explicit source_crs")
    if _crs_equivalent(source_crs, target_crs):
        projected = tuple(geometries)
    else:
        transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
        projected = tuple(transform_geometry(transformer.transform, geometry) for geometry in geometries)
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
    before_records, _ = _component_file_records(root, manifest_path, manifest, verify_sha=verify_component_sha)
    target_crs_text = config["target_coordinate_system"]
    _validate_projected_metric_crs(target_crs_text)
    target_crs = CRS.from_user_input(target_crs_text)
    by_role = {component["role"]: component for component in manifest["components"] if component["role"] in {"domain_D", "security_S"}}
    if set(by_role) != {"domain_D", "security_S"}:
        raise DomainMeshOnlyError("source manifest must provide exactly one domain_D and one security_S component")

    loaded_areas: dict[str, _PolygonComponent] = {}
    for role, component in by_role.items():
        loaded_areas[role] = _load_polygon_component(component, manifest_path, root, target_crs_text)
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
        line=_read_line_geometry(component,manifest_path,root,target_crs)
        points=[tuple(map(float,point[:2])) for point in line.coords]
        group=GmshLineGroup(name=component["group_name"],points=points,closed=component["closed"])
        (boundary_groups if component["role"]=="boundary_line" else constraint_groups).append(group)
        line_components.append({"component_id":component["component_id"],"role":component["role"],
                                "group_name":component["group_name"],"geometry_sha256":_geometry_sha256(line),
                                "feature_count":1,"source_crs":component["source_crs"],
                                "target_crs":target_crs_text,"coordinate_transform_applied":not _crs_equivalent(CRS.from_user_input(component["source_crs"]),target_crs)})
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
    after_records,total_bytes=_component_file_records(root,manifest_path,manifest,verify_sha=True)
    before_hashes={key:value["sha256"] for key,value in before_records.items()}
    after_hashes={key:value["sha256"] for key,value in after_records.items()}
    if before_hashes!=after_hashes or total_bytes>MAX_COMPONENT_TOTAL_BYTES:
        raise SourceIdentityError("one or more source components changed during geometry loading")
    from .restricted_worker import _sha256_file
    if _stat_identity(manifest_path)!=identity["manifest_stat_identity"] or _sha256_file(manifest_path,maximum_bytes=MAX_SOURCE_MANIFEST_BYTES)[0]!=identity["manifest_sha256"]:
        raise SourceIdentityError("source manifest changed during geometry loading")
    polygon_metadata={}
    for role,component in by_role.items():
        loaded=loaded_areas[role]
        polygon_metadata[role]={"component_id":component["component_id"],"source_format":component["format"],
                                "source_crs":component["source_crs"],"target_crs":target_crs_text,
                                "source_feature_count":len(loaded.source_feature_indices),
                                "source_geometry_sha256":loaded.source_geometry_sha256,
                                "source_union_geometry_sha256":loaded.source_union_geometry_sha256,
                                "coordinate_transform_applied":not _crs_equivalent(loaded.source_crs,loaded.target_crs)}
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
        files_by_id.setdefault(item["component_id"], []).append({
            "role": item["file_role"], "path": item["path"], "sha256": item["sha256"],
            "bytes": item["bytes"], "stat_identity": dict(item["stat_identity"]),
        })
    component_meta: dict[str, dict[str, Any]] = {}
    for component in context.manifest["components"]:
        component_meta[component["component_id"]] = {
            "component_id": component["component_id"], "role": component["role"],
            "format": component["format"], "source_crs": component["source_crs"],
            "group_name": component["group_name"], "closed": component["closed"],
            "files": sorted(files_by_id[component["component_id"]], key=lambda x: x["role"]),
        }
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
    for line in context.line_components:
        component_meta[line["component_id"]].update(line)
    return [component_meta[x["component_id"]] for x in context.manifest["components"]]


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
        "format": DOMAIN_MESH_AUDIT_FORMAT,
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
        "format": DOMAIN_MESH_AUDIT_FORMAT,
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
    started=time.perf_counter()
    mesh=mesh_generation.generate_gmsh_mesh(
        context.gmsh_config,coordinate_system=config["target_coordinate_system"],
        geometry_tolerance_m=float(settings["geometry_tolerance_m"]),
        minimum_face_area_m2=float(settings["minimum_face_area_m2"]),
    )
    gmsh_seconds=time.perf_counter()-started
    gmsh_identity_after = _gmsh_runtime_identity()
    if any(gmsh_identity_after[key] != gmsh_identity_before[key] for key in (
            "python_binding_path", "python_binding_version", "native_library_path", "native_library_sha256", "native_library_bytes")):
        from .restricted_worker import SourceIdentityError
        raise SourceIdentityError("Gmsh binding/DLL identity changed during mesh generation")
    gmsh_identity=_seal_gmsh_worker_identity(gmsh_identity_after)
    stage=Path(control["attempt_root"]); scratch=stage/"scratch"; artifacts=stage/"artifacts"
    resources=config["resources"]
    mesh_payload=_mesh_payload(mesh,full=True)
    mesh_payload["format"]=DOMAIN_MESH_ARTIFACT_FORMAT
    mesh_payload["mesh_only"]=True
    mesh_bytes=_canonical_json_bytes(mesh_payload); mesh_sha=hashlib.sha256(mesh_bytes).hexdigest()
    from .restricted_worker import _write_no_clobber_bytes
    _write_no_clobber_bytes(scratch/"domain-mesh-generated.json",
        _canonical_json_bytes({"format":"triangle-domain-mesh-only-generated-v1","worker_pid":os.getpid(),
                               "process_in_job":True,"gmsh_native_library_loaded":gmsh_identity["native_library_loaded_in_worker"],
                               "mesh_sha256":mesh_sha,"node_count":mesh.n_node,"face_count":mesh.n_face}),
        output_budget=resources["scratch_budget_bytes"],root=scratch)
    if mesh.n_node>settings["maximum_nodes"] or mesh.n_face>settings["maximum_faces"]:
        raise DomainMeshOnlyError(f"generated mesh exceeds configured cap: {mesh.n_node} nodes/{mesh.n_face} faces")
    quality=mesh.quality_report(minimum_quality=float(settings["minimum_face_quality"]))
    if quality["faces_below_quality_threshold"]:
        raise DomainMeshOnlyError(f"generated mesh has faces below minimum_face_quality={settings['minimum_face_quality']}")
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
    mesh_bytes=_canonical_json_bytes(mesh_payload);mesh_sha=hashlib.sha256(mesh_bytes).hexdigest()
    audit_bytes=_canonical_json_bytes(audit_record);audit_sha=hashlib.sha256(audit_bytes).hexdigest()
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
