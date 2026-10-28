"""Restricted synthetic P0/G0 terrain-preparation pilot route.

The supervisor imports this module without loading rasterio, Shapely, Gmsh or
its native library. Worker-only imports happen after the Job Object is active.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from importlib import metadata, util
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping


PILOT_FORMAT = "triangle-b1-synthetic-pilot-config-v1"
PILOT_VERSION = "1.0"
PILOT_EVIDENCE_FORMAT = "triangle-b1-synthetic-pilot-evidence-v1"
GMSH_RECIPE_FORMAT = "triangle-b1-synthetic-gmsh-recipe-v1"
GMSH_MESH_FORMAT = "triangle-b1-generated-gmsh-mesh-v1"
P0_CASE_ID_MAX_LENGTH = 64
P0_FACE_SIZE_M = 50.0
G0_AREA_ABS_TOLERANCE_MIN_M2 = 1e-12
G0_AREA_ABS_TOLERANCE_MAX_M2 = 1.0
G0_AREA_REL_TOLERANCE_MIN = 1e-12
G0_AREA_REL_TOLERANCE_MAX = 1e-3
GMSH_VERSION_PATTERN = r"^4\.(?:1[1-9]|[2-9][0-9]|[1-9][0-9]{2,})\.(?:0|[1-9][0-9]*)$"
_AUDIT_FLOAT_REL_TOLERANCE = 1e-12
_AUDIT_FLOAT_ABS_TOLERANCE = 1e-12


def validate_pilot_config_shape(config: Any) -> dict[str, Any]:
    """Validate the pilot envelope while keeping the published v1 contract strict."""
    from .restricted_worker import FORMAT, validate_config_shape

    if not isinstance(config, dict):
        raise ValueError("pilot config must be an object")
    expected = {"format", "schema_version", "run_id", "output_root", "resources", "source",
                "mesh", "mapping_config", "crs_alias", "action", "pilot_case", "mesh_generation"}
    missing, extra = sorted(expected - config.keys()), sorted(config.keys() - expected)
    if missing or extra:
        raise ValueError(f"pilot config fields differ from the frozen contract; missing={missing}, unknown={extra}")
    if config.get("format") != PILOT_FORMAT or config.get("schema_version") != PILOT_VERSION:
        raise ValueError("unsupported synthetic pilot config format/version")
    action = config.get("action")
    if action not in {"p0_windowed_mapping", "g0_gmsh_windowed_mapping"}:
        raise ValueError("pilot action must be p0_windowed_mapping or g0_gmsh_windowed_mapping")

    base = {key: config[key] for key in expected if key in {
        "schema_version", "run_id", "output_root", "resources", "source", "mesh",
        "mapping_config", "crs_alias"}}
    base["format"] = FORMAT
    validate_config_shape(base)
    expected_resources = {"max_workers": 1, "job_memory_limit_mib": 2048,
                          "wall_clock_limit_s": 900.0, "output_budget_bytes": 1_073_741_824,
                          "scratch_budget_bytes": 16_777_216,
                          "child_process_policy": "job_object_inheritance_without_breakaway"}
    if config["resources"] != expected_resources:
        raise ValueError("pilot resources are frozen at one worker/2048 MiB/900 s/1 GiB output/16 MiB scratch")
    mapping = config["mapping_config"]
    fixed_mapping = {"max_source_pixels": 262_144, "tile_rows": 256, "tile_cols": 256,
                     "max_inflight_tiles": 1, "gdal_cache_mib": 64, "max_faces": 500_000,
                     "max_nodes": 2_000_000, "max_candidate_overlap_checks": 1_000_000,
                     "coverage_policy": "diagnostic_only", "minimum_valid_coverage": None,
                     "coverage_policy_evidence_path": None, "coverage_policy_evidence_sha256": None,
                     "source_vertical_unit_declaration": None, "source_vertical_unit_evidence_path": None,
                     "source_vertical_unit_evidence_sha256": None, "source_vertical_datum_declaration": None,
                     "source_vertical_datum_evidence_path": None, "source_vertical_datum_evidence_sha256": None,
                     "area_abs_tolerance_m2": 1e-8, "area_rel_tolerance": 1e-10,
                     "elevation_abs_tolerance_m": 1e-8, "elevation_rel_tolerance": 1e-10}
    for key, expected_value in fixed_mapping.items():
        if mapping.get(key) != expected_value:
            raise ValueError(f"pilot mapping_config.{key} differs from its frozen synthetic measurement setting")

    if action == "p0_windowed_mapping":
        case = config.get("pilot_case")
        if not isinstance(case, dict) or set(case) != {
                "case_id", "seed", "face_count", "face_size_m", "repeat_index", "source_pattern"}:
            raise ValueError("P0 pilot_case fields differ from the frozen case contract")
        if (not isinstance(case["case_id"], str)
                or not 1 <= len(case["case_id"]) <= P0_CASE_ID_MAX_LENGTH):
            raise ValueError(f"P0 pilot_case.case_id must contain 1 to {P0_CASE_ID_MAX_LENGTH} characters")
        if type(case["seed"]) is not int or not 0 <= case["seed"] <= 2**32 - 1:
            raise ValueError("P0 pilot_case.seed must be an unsigned 32-bit integer")
        if type(case["face_count"]) is not int or case["face_count"] not in {2, 8, 32}:
            raise ValueError("P0 pilot_case.face_count must be 2, 8 or 32")
        if (isinstance(case["face_size_m"], bool) or not isinstance(case["face_size_m"], (int, float))
                or not math.isfinite(float(case["face_size_m"])) or float(case["face_size_m"]) != P0_FACE_SIZE_M):
            raise ValueError("P0 pilot_case.face_size_m must be exactly 50 m")
        if type(case["repeat_index"]) is not int or case["repeat_index"] not in {1, 2}:
            raise ValueError("P0 pilot_case.repeat_index must be 1 or 2")
        if case["source_pattern"] not in {"planar_all_valid", "planar_nodata_nonfinite"}:
            raise ValueError("P0 pilot source_pattern is unsupported")
        if config.get("mesh_generation") is not None:
            raise ValueError("P0 does not accept mesh_generation fields")
    else:
        if config.get("pilot_case") is not None:
            raise ValueError("G0 does not accept a P0 pilot_case")
        generation = config.get("mesh_generation")
        fields = {"format", "maximum_faces", "maximum_nodes", "minimum_face_quality",
                  "area_abs_tolerance_m2", "area_rel_tolerance", "area_roles",
                  "expected_gmsh_version", "expected_gmsh_library_sha256"}
        if not isinstance(generation, dict) or set(generation) != fields:
            raise ValueError("G0 mesh_generation fields differ from the frozen contract")
        if generation.get("format") != "triangle-b1-gmsh-generation-v1":
            raise ValueError("G0 mesh_generation format is unsupported")
        gmsh_version = generation["expected_gmsh_version"]
        gmsh_version_match = re.fullmatch(GMSH_VERSION_PATTERN, gmsh_version) if isinstance(gmsh_version, str) else None
        if (gmsh_version_match is None
                or not isinstance(generation["expected_gmsh_library_sha256"], str)
                or len(generation["expected_gmsh_library_sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in generation["expected_gmsh_library_sha256"])):
            raise ValueError("G0 expected Gmsh version/native library SHA-256 identity is invalid")
        if type(generation["maximum_faces"]) is not int or not 1 <= generation["maximum_faces"] <= 500_000:
            raise ValueError("G0 maximum_faces must be in [1, 500000]")
        if type(generation["maximum_nodes"]) is not int or not 3 <= generation["maximum_nodes"] <= 2_000_000:
            raise ValueError("G0 maximum_nodes must be in [3, 2000000]")
        for key, low, high in (("minimum_face_quality", 0.0, 1.0),
                                ("area_abs_tolerance_m2", G0_AREA_ABS_TOLERANCE_MIN_M2, G0_AREA_ABS_TOLERANCE_MAX_M2),
                                ("area_rel_tolerance", G0_AREA_REL_TOLERANCE_MIN, G0_AREA_REL_TOLERANCE_MAX)):
            value = generation[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not low <= float(value) <= high:
                raise ValueError(f"G0 {key} must be a finite number in [{low}, {high}]")
        roles = generation["area_roles"]
        if not isinstance(roles, dict) or set(roles) != {"outer_boundary", "holes", "constraints"}:
            raise ValueError("G0 area_roles fields differ from the frozen role contract")
        if not isinstance(roles["outer_boundary"], str) or not roles["outer_boundary"].strip():
            raise ValueError("G0 outer_boundary role must be a non-empty string")
        if not isinstance(roles["holes"], dict) or any(
                not isinstance(key, str) or re.fullmatch(r"hole_[0-9]+", key) is None
                or not isinstance(value, str) or not value.strip()
                for key, value in roles["holes"].items()):
            raise ValueError("G0 hole roles must map hole names to non-empty area roles")
        if not isinstance(roles["constraints"], dict) or any(
                not isinstance(key, str) or not isinstance(value, str) or not value.strip()
                for key, value in roles["constraints"].items()):
            raise ValueError("G0 constraint roles must map group names to non-empty roles")
    return config


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _ring_area(points: list[list[float]] | tuple[tuple[float, float], ...]) -> float:
    if len(points) < 3:
        return 0.0
    return 0.5 * math.fsum(
        float(points[index][0]) * float(points[(index + 1) % len(points)][1])
        - float(points[(index + 1) % len(points)][0]) * float(points[index][1])
        for index in range(len(points)))


def _stable_ring_area(points: list[list[float]] | tuple[tuple[float, float], ...]) -> float:
    """Compute signed shoelace area after translating the ring to a local origin.

    Translation preserves the input coordinates and winding while avoiding
    cancellation between products formed from large absolute map coordinates.
    """
    if len(points) < 3:
        return 0.0
    origin_x = float(points[0][0])
    origin_y = float(points[0][1])
    return 0.5 * math.fsum(
        (float(points[index][0]) - origin_x) * (float(points[(index + 1) % len(points)][1]) - origin_y)
        - (float(points[(index + 1) % len(points)][0]) - origin_x) * (float(points[index][1]) - origin_y)
        for index in range(len(points))
    )


def _mesh_payload(mesh: Any, *, full: bool) -> dict[str, Any]:
    payload = {
        "format": GMSH_MESH_FORMAT if full else "triangle-mesh-xy-json-v1",
        "mesh_id": mesh.mesh_id,
        "coordinate_system": mesh.coordinate_system,
        "node_xy": [list(point) for point in mesh.node_xy],
        "face_nodes": [list(face) for face in mesh.face_nodes],
        "face_id": list(mesh.face_id),
        "geometry_tolerance_m": mesh.geometry_tolerance_m,
        "minimum_face_area_m2": mesh.minimum_face_area_m2,
    }
    if full:
        payload["boundary_groups"] = {
            name: [list(mesh.edge_nodes[index]) for index in indices]
            for name, indices in mesh.boundary_groups.items()
        }
        payload["constraint_groups"] = {
            name: [list(mesh.edge_nodes[index]) for index in indices]
            for name, indices in mesh.constraint_groups.items()
        }
    return payload


def _line_segments(points: Any, *, closed: bool) -> tuple[tuple[tuple[float, float], tuple[float, float]], ...]:
    coordinates = tuple((float(point[0]), float(point[1])) for point in points)
    if closed and len(coordinates) > 1 and coordinates[0] == coordinates[-1]:
        coordinates = coordinates[:-1]
    pairs = list(zip(coordinates, coordinates[1:]))
    if closed and coordinates:
        pairs.append((coordinates[-1], coordinates[0]))
    return tuple(pairs)


def _audit_mesh_line_group(
    mesh: Any,
    *,
    category: str,
    group_name: str,
    source_segments: tuple[tuple[tuple[float, float], tuple[float, float]], ...],
    edge_indices: Any,
    tolerance_m: float,
) -> dict[str, float]:
    if not source_segments:
        raise ValueError(f"G0 {category} group {group_name!r} has no source segments")
    segment_lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in source_segments]
    if any(length <= tolerance_m for length in segment_lengths):
        raise ValueError(f"G0 {category} group {group_name!r} contains a degenerate source segment")
    intervals: list[list[tuple[float, float]]] = [[] for _ in source_segments]
    expected_length = math.fsum(segment_lengths)
    observed_edge_length = 0.0
    maximum_deviation = 0.0
    outside_length = 0.0
    for edge_index in edge_indices:
        node_a, node_b = mesh.edge_nodes[edge_index]
        edge_a, edge_b = mesh.node_xy[node_a], mesh.node_xy[node_b]
        edge_length = math.hypot(edge_b[0] - edge_a[0], edge_b[1] - edge_a[1])
        observed_edge_length += edge_length
        matches: list[tuple[float, float, int, float, float]] = []
        for segment_index, ((source_a, source_b), length) in enumerate(zip(source_segments, segment_lengths)):
            dx, dy = source_b[0] - source_a[0], source_b[1] - source_a[1]
            length_squared = dx * dx + dy * dy
            projections = [((point[0] - source_a[0]) * dx + (point[1] - source_a[1]) * dy) / length_squared
                           for point in (edge_a, edge_b)]
            deviations = [abs(dx * (point[1] - source_a[1]) - dy * (point[0] - source_a[0])) / length
                          for point in (edge_a, edge_b)]
            low, high = min(projections), max(projections)
            outside = (max(0.0, -low) + max(0.0, high - 1.0)) * length
            projected_length = max(0.0, min(1.0, high) - max(0.0, low)) * length
            deviation = max(deviations)
            if (deviation <= tolerance_m and outside <= tolerance_m
                    and abs(edge_length - projected_length) <= max(tolerance_m, edge_length * 1e-12)):
                matches.append((deviation, outside, segment_index, max(0.0, low), min(1.0, high)))
        if not matches:
            raise ValueError(f"G0 {category} group {group_name!r} contains an edge outside its trusted recipe lines")
        deviation, outside, segment_index, low, high = min(matches, key=lambda item: (item[0], item[1], item[2]))
        maximum_deviation = max(maximum_deviation, deviation)
        outside_length += outside
        intervals[segment_index].append((low, high))

    gap_length = 0.0
    overlap_length = 0.0
    covered_length = 0.0
    for segment_index, segment_intervals in enumerate(intervals):
        length = segment_lengths[segment_index]
        cursor = 0.0
        for low, high in sorted(segment_intervals):
            if low > cursor:
                gap_length += (low - cursor) * length
            elif low < cursor:
                overlap_length += (min(cursor, high) - low) * length
            covered_length += max(0.0, high - max(cursor, low)) * length
            cursor = max(cursor, high)
        if cursor < 1.0:
            gap_length += (1.0 - cursor) * length

    allowed_length_error = max(tolerance_m * len(source_segments), expected_length * 1e-12)
    if (gap_length > allowed_length_error or outside_length > allowed_length_error
            or overlap_length > allowed_length_error
            or abs(observed_edge_length - expected_length) > allowed_length_error
            or maximum_deviation > tolerance_m):
        raise ValueError(
            f"G0 {category} group {group_name!r} does not cover its recipe line exactly "
            f"(gap={gap_length:.12g} m, outside={outside_length:.12g} m, overlap={overlap_length:.12g} m)"
        )
    return {
        "expected_length_m": expected_length,
        "observed_edge_length_m": observed_edge_length,
        "covered_length_m": covered_length,
        "gap_length_m": gap_length,
        "outside_length_m": outside_length,
        "overlap_length_m": overlap_length,
        "maximum_deviation_m": maximum_deviation,
    }


def _audit_g0_mesh(mesh: Any, gmsh_config: Any, generation_config: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute recipe roles, mesh-line coverage and set/area coverage without terrain I/O."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    roles = generation_config["area_roles"]
    expected_holes = {f"hole_{index}" for index in range(len(gmsh_config.holes))}
    expected_constraints = {group.name for group in gmsh_config.constraints}
    if set(roles["holes"]) != expected_holes or set(roles["constraints"]) != expected_constraints:
        raise ValueError("G0 semantic role keys do not match the trusted recipe holes/constraints")

    generated_holes = {f"hole_{index}": _line_segments(ring, closed=True)
                       for index, ring in enumerate(gmsh_config.holes)}
    auto_boundary_names = {"outer_boundary", *generated_holes}
    explicit_boundary_names = {group.name for group in gmsh_config.boundary_groups}
    constraint_names = {group.name for group in gmsh_config.constraints}
    if ((auto_boundary_names & explicit_boundary_names)
            or (auto_boundary_names & constraint_names)
            or (explicit_boundary_names & constraint_names)):
        raise ValueError("G0 recipe boundary/constraint group names collide with generated semantic groups")
    boundary_segments = {"outer_boundary": _line_segments(gmsh_config.outer_boundary, closed=True), **generated_holes}
    for group in gmsh_config.boundary_groups:
        boundary_segments[group.name] = _line_segments(group.points, closed=group.closed)
    constraint_segments = {
        group.name: _line_segments(group.points, closed=False)
        for group in gmsh_config.constraints
    }
    if set(mesh.boundary_groups) != set(boundary_segments):
        raise ValueError("G0 generated boundary groups differ from the trusted recipe group set")
    if set(mesh.constraint_groups) != set(constraint_segments):
        raise ValueError("G0 generated constraint groups differ from the trusted recipe group set")
    tolerance_m = float(gmsh_config.geometry_tolerance_m) if hasattr(gmsh_config, "geometry_tolerance_m") else float(mesh.geometry_tolerance_m)
    boundary_coverage = {
        name: _audit_mesh_line_group(mesh, category="boundary", group_name=name,
                                     source_segments=segments, edge_indices=mesh.boundary_groups[name],
                                     tolerance_m=tolerance_m)
        for name, segments in boundary_segments.items()
    }
    constraint_coverage = {
        name: _audit_mesh_line_group(mesh, category="constraint", group_name=name,
                                     source_segments=segments, edge_indices=mesh.constraint_groups[name],
                                     tolerance_m=tolerance_m)
        for name, segments in constraint_segments.items()
    }

    outer = [tuple(map(float, point)) for point in gmsh_config.outer_boundary]
    holes = [[tuple(map(float, point)) for point in ring] for ring in gmsh_config.holes]
    domain = Polygon(outer, holes)
    if not domain.is_valid or not math.isfinite(float(domain.area)) or domain.area <= 0:
        raise ValueError("G0 trusted outer-minus-hole recipe does not form a valid positive-area domain")
    recipe_expected_area = abs(_stable_ring_area(outer)) - math.fsum(
        abs(_stable_ring_area(ring)) for ring in holes
    )
    if not math.isclose(float(domain.area), recipe_expected_area, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError("G0 polygon domain area differs from its ring-area calculation")
    triangles = [Polygon([mesh.node_xy[index] for index in face]) for face in mesh.face_nodes]
    if any(not polygon.is_valid or polygon.area <= 0 for polygon in triangles):
        raise ValueError("G0 generated mesh contains an invalid or zero-area triangle")
    face_union = unary_union(triangles)
    if not face_union.is_valid:
        raise ValueError("G0 generated face union is invalid")
    expected_area = float(domain.area)
    observed_area = math.fsum(mesh.face_area)
    difference = observed_area - expected_area
    tolerance = max(float(generation_config["area_abs_tolerance_m2"]),
                    expected_area * float(generation_config["area_rel_tolerance"]))
    gap = float(domain.difference(face_union).area)
    outside = float(face_union.difference(domain).area)
    overlap = max(0.0, observed_area - float(face_union.area))
    maximum_hole_intersection = max(
        (float(triangle.intersection(Polygon(hole)).area) for triangle in triangles for hole in holes),
        default=0.0,
    )
    if (abs(difference) > tolerance or gap > tolerance or outside > tolerance or overlap > tolerance
            or maximum_hole_intersection > float(generation_config["area_abs_tolerance_m2"])):
        raise ValueError(
            f"G0 mesh/domain coverage failed (difference={difference:.12g} m2, gap={gap:.12g} m2, "
            f"outside={outside:.12g} m2, overlap={overlap:.12g} m2)"
        )
    return {
        "area_audit": {
            "expected_outer_minus_holes_m2": expected_area,
            "observed_face_total_m2": observed_area,
            "difference_m2": difference,
            "tolerance_m2": tolerance,
            "coverage_gap_m2": gap,
            "outside_domain_m2": outside,
            "overlap_m2": overlap,
            "maximum_face_hole_intersection_m2": maximum_hole_intersection,
        },
        "line_coverage": {"boundary_groups": boundary_coverage,
                          "constraint_groups": constraint_coverage},
    }


def _assert_audit_record_matches(recorded: Any, expected: Any, *, label: str) -> None:
    if isinstance(expected, Mapping):
        if not isinstance(recorded, Mapping) or set(recorded) != set(expected):
            raise ValueError(f"G0 stored {label} fields differ from the recomputed audit")
        for key, value in expected.items():
            _assert_audit_record_matches(recorded[key], value, label=f"{label}.{key}")
        return
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if (isinstance(recorded, bool) or not isinstance(recorded, (int, float))
                or not math.isfinite(float(recorded))
                or not math.isclose(float(recorded), float(expected),
                                    rel_tol=_AUDIT_FLOAT_REL_TOLERANCE,
                                    abs_tol=_AUDIT_FLOAT_ABS_TOLERANCE)):
            raise ValueError(f"G0 stored {label} differs from its recomputed value")
        return
    if recorded != expected:
        raise ValueError(f"G0 stored {label} differs from its trusted value")


def _validate_g0_stored_audits(
    mesh_generation: Mapping[str, Any], config: Mapping[str, Any], recomputed: Mapping[str, Any]
) -> None:
    if mesh_generation.get("area_roles") != config["mesh_generation"]["area_roles"]:
        raise ValueError("G0 stored semantic roles differ from the trusted config")
    _assert_audit_record_matches(mesh_generation.get("area_audit"), recomputed["area_audit"], label="area_audit")
    _assert_audit_record_matches(mesh_generation.get("line_coverage_audit"), recomputed["line_coverage"],
                                 label="line_coverage_audit")


def _process_is_in_job() -> bool:
    if os.name != "nt":
        return False
    from .restricted_worker import _win_api
    kernel32, _ = _win_api()
    membership = wintypes.BOOL()
    if not kernel32.IsProcessInJob(wintypes.HANDLE(-1), None, ctypes.byref(membership)):
        raise OSError(ctypes.get_last_error(), "IsProcessInJob(current process) failed before Gmsh load")
    return bool(membership.value)


def _gmsh_runtime_identity() -> dict[str, Any]:
    from .mesh_generation import _gmsh_native_library_path
    from .restricted_worker import _sha256_file, _stat_identity
    spec = util.find_spec("gmsh")
    if spec is None or spec.origin is None:
        raise ImportError("Gmsh Python binding is unavailable; install with pip install '.[gmsh]'")
    library = _gmsh_native_library_path()
    if library is None:
        raise ImportError("Gmsh Python binding is installed but its native SDK DLL is unavailable")
    before = _stat_identity(library)
    library_sha, library_bytes = _sha256_file(library, maximum_bytes=256 * 1024 * 1024)
    after = _stat_identity(library)
    if before != after:
        raise OSError("Gmsh native SDK DLL changed while its identity was captured")
    try:
        gmsh_version = metadata.version("gmsh")
    except metadata.PackageNotFoundError as exc:
        raise ImportError("Gmsh binding has no installed distribution metadata") from exc
    return {"python_binding_path": str(Path(spec.origin).resolve()), "python_binding_version": gmsh_version,
            "native_library_path": str(library.resolve()), "native_library_sha256": library_sha,
            "native_library_bytes": library_bytes, "native_library_stat": before,
            "native_library_loaded_in_worker": False}


def _loaded_gmsh_library(path: str) -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HANDLE
    return bool(kernel32.GetModuleHandleW(Path(path).name))


def _read_recipe(path: Path, expected_sha256: str) -> tuple[dict[str, Any], dict[str, int]]:
    from .restricted_worker import SourceIdentityError, _sha256_file, _stat_identity
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"G0 Gmsh recipe is missing or not a regular file: {path}")
    before = _stat_identity(path)
    digest, size = _sha256_file(path, maximum_bytes=1024 * 1024)
    if digest != expected_sha256:
        raise SourceIdentityError("G0 Gmsh recipe SHA-256 differs from its declared source identity")
    try:
        recipe = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"G0 Gmsh recipe is malformed: {exc}") from exc
    after = _stat_identity(path)
    if before != after:
        raise SourceIdentityError("G0 Gmsh recipe changed while it was read")
    if (not isinstance(recipe, dict) or set(recipe) != {
            "format", "schema_version", "coordinate_system", "geometry_tolerance_m",
            "minimum_face_area_m2", "gmsh_config"}
            or recipe.get("format") != GMSH_RECIPE_FORMAT or recipe.get("schema_version") != "1.0"):
        raise ValueError("G0 Gmsh recipe format/fields differ from the frozen contract")
    return recipe, before


def _profiled_mapping(control: dict[str, Any], config: dict[str, Any], root: Path, *,
                      pilot_metadata: dict[str, Any], route_version: str,
                      body_path_overrides: Mapping[str, str] | None = None,
                      prepared_body_validator: Any | None = None) -> dict[str, Any]:
    from unittest.mock import patch
    from . import prepared_terrain, terrain_windowed_mapping
    from .dem_contract import GeoTIFFDEMReader
    from .restricted_worker import _worker_run_mapping
    import numpy
    import platform
    import pydantic
    import pyproj
    import rasterio
    import shapely

    pilot_metadata["runtime_fingerprint"] = {
        "python": sys.version.splitlines()[0],
        "platform": platform.platform(),
        "numpy": numpy.__version__,
        "pydantic": pydantic.__version__,
        "rasterio": rasterio.__version__,
        "gdal": rasterio.__gdal_version__,
        "shapely": shapely.__version__,
        "geos": shapely.geos_version_string,
        "pyproj": pyproj.__version__,
        "proj": pyproj.proj_version_str,
    }

    timings = {"source_metadata_io_seconds": 0.0, "raw_window_io_seconds": 0.0,
               "candidate_search_seconds": 0.0, "clip_seconds": 0.0, "mapper_seconds": 0.0}
    profile_counts = {"metadata_reads": 0, "raw_windows_read": 0, "candidate_search_calls": 0, "clip_calls": 0}
    writer_time = {"seconds": 0.0}
    original_metadata = GeoTIFFDEMReader.read_metadata
    original_window = GeoTIFFDEMReader._read_open_window
    original_candidates = terrain_windowed_mapping._geometry_candidates
    original_clip = terrain_windowed_mapping._clip_convex
    original_mapper = terrain_windowed_mapping.map_triangular_mesh_from_windows
    original_writer = prepared_terrain.write_prepared_face_artifacts

    def timed_metadata(reader: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_metadata(reader, *args, **kwargs)
        finally:
            timings["source_metadata_io_seconds"] += time.perf_counter() - started
            profile_counts["metadata_reads"] += 1

    def timed_window(reader: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_window(reader, *args, **kwargs)
        finally:
            timings["raw_window_io_seconds"] += time.perf_counter() - started
            profile_counts["raw_windows_read"] += 1

    def timed_candidates(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_candidates(*args, **kwargs)
        finally:
            timings["candidate_search_seconds"] += time.perf_counter() - started
            profile_counts["candidate_search_calls"] += 1

    def timed_clip(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_clip(*args, **kwargs)
        finally:
            timings["clip_seconds"] += time.perf_counter() - started
            profile_counts["clip_calls"] += 1

    def timed_mapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            terrain, report = original_mapper(*args, **kwargs)
        finally:
            timings["mapper_seconds"] += time.perf_counter() - started
        pilot_metadata["timing_protocol"] = {
            "clock": "time.perf_counter",
            "mapper_interval": "around the actual map_triangular_mesh_from_windows call",
            "component_intervals": "metadata/raw-window reads, _geometry_candidates, and _clip_convex wrappers inside that call",
            "residual": "mapper interval minus measured disjoint components; includes index construction, accumulation and Python overhead",
            "instrumentation_overhead": "wrapper call overhead is included in each measured component and total",
        }
        pilot_metadata["mapping_timing_seconds"] = {
            **timings,
            "unclassified_mapper_seconds": timings["mapper_seconds"] - sum(
                timings[name] for name in ("source_metadata_io_seconds", "raw_window_io_seconds",
                                           "candidate_search_seconds", "clip_seconds")),
        }
        pilot_metadata["mapping_profile_counts"] = dict(profile_counts)
        pilot_metadata["mapping_stats"] = dict(report.get("stats", {}))
        return terrain, report

    def timed_writer(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_writer(*args, **kwargs)
        finally:
            writer_time["seconds"] += time.perf_counter() - started

    with (patch.object(GeoTIFFDEMReader, "read_metadata", timed_metadata),
          patch.object(GeoTIFFDEMReader, "_read_open_window", timed_window),
          patch.object(terrain_windowed_mapping, "_geometry_candidates", timed_candidates),
          patch.object(terrain_windowed_mapping, "_clip_convex", timed_clip),
          patch.object(terrain_windowed_mapping, "map_triangular_mesh_from_windows", timed_mapper),
          patch.object(prepared_terrain, "write_prepared_face_artifacts", timed_writer)):
        result = _worker_run_mapping(control, config, root,
                                     body_metadata={"synthetic_pilot": pilot_metadata},
                                     route_version=route_version,
                                     body_path_overrides=body_path_overrides,
                                     prepared_body_validator=prepared_body_validator)
    result["pilot"] = json.loads(json.dumps(pilot_metadata, ensure_ascii=False, allow_nan=False))
    result["prepared_writer_seconds"] = writer_time["seconds"]
    return result


def _run_g0(control: dict[str, Any], config: dict[str, Any], root: Path) -> dict[str, Any]:
    from . import mesh_generation
    from .mesh_preparation import GmshMeshConfig, MeshPreparationError
    from .restricted_worker import (FORMAT, SourceIdentityError,
                                    _resolve_project_path, _sha256_file, _stat_identity,
                                    _verify_source_manifest_bindings, _write_no_clobber_bytes)
    from .triangular_mesh import TriangularMesh
    from .terrain_windowed_mapping import WindowedTriangleMappingConfig

    if not _process_is_in_job():
        raise RuntimeError("G0 refused to load Gmsh: current worker is not in its assigned Job Object")
    source = config["source"]
    raster_path = _resolve_project_path(root, source["raster_path"], "source.raster_path")
    if raster_path.is_symlink() or not raster_path.is_file():
        raise FileNotFoundError(f"G0 synthetic source raster is missing: {source['raster_path']}")
    raster_before = _stat_identity(raster_path)
    if source.get("expected_stat_identity") is not None and raster_before != source["expected_stat_identity"]:
        raise SourceIdentityError("G0 synthetic raster stat identity differs before Gmsh generation")
    manifest_path = _resolve_project_path(root, source["source_manifest_path"], "source.source_manifest_path")
    manifest_sha, _ = _sha256_file(manifest_path, maximum_bytes=16 * 1024 * 1024)
    if manifest_sha != source["source_manifest_sha256"]:
        raise SourceIdentityError("G0 input source manifest identity mismatch")
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    recipe_path = _resolve_project_path(root, config["mesh"]["path"], "mesh.path")
    recipe, recipe_stat = _read_recipe(recipe_path, config["mesh"]["sha256"])
    _verify_source_manifest_bindings(manifest_payload, source, config["mesh"])
    from .restricted_worker import _source_component_snapshot
    source_components_before = _source_component_snapshot(config, root)
    generation_config = config["mesh_generation"]
    gmsh_config = GmshMeshConfig.model_validate(recipe["gmsh_config"])
    mapping_limits = WindowedTriangleMappingConfig.model_validate(config["mapping_config"])
    if (generation_config["maximum_faces"] > mapping_limits.max_faces
            or generation_config["maximum_nodes"] > mapping_limits.max_nodes):
        raise MeshPreparationError("G0 Gmsh generation caps may not exceed the route mapping mesh caps")
    if gmsh_config.maximum_nodes > generation_config["maximum_nodes"]:
        raise MeshPreparationError("Gmsh recipe maximum_nodes exceeds the frozen pilot cap")
    vertical_evidence_path = root / "docs/external_evidence_contract.md"
    if _sha256_file(vertical_evidence_path, maximum_bytes=4 * 1024 * 1024)[0] != control["vertical_evidence_sha256"]:
        raise SourceIdentityError("G0 vertical relation evidence differs before native generation")
    from .dem_contract import GeoTIFFDEMReader
    from .restricted_worker import _CRSAliasReader
    metadata_reader = _CRSAliasReader(GeoTIFFDEMReader(), config["crs_alias"])
    source_metadata = metadata_reader.read_metadata(raster_path)
    if source_metadata.crs != recipe["coordinate_system"]:
        raise SourceIdentityError("G0 Gmsh recipe coordinate system differs from the raster or verified alias")
    if not _process_is_in_job():
        raise RuntimeError("G0 worker left its Job Object before native Gmsh load")
    gmsh_identity = _gmsh_runtime_identity()
    if (gmsh_identity["python_binding_version"] != generation_config["expected_gmsh_version"]
            or gmsh_identity["native_library_sha256"] != generation_config["expected_gmsh_library_sha256"]):
        raise SourceIdentityError("G0 installed Gmsh binding/DLL identity differs from the frozen pilot config")
    generation_started = time.perf_counter()
    mesh = mesh_generation.generate_gmsh_mesh(
        gmsh_config, coordinate_system=recipe["coordinate_system"],
        geometry_tolerance_m=recipe["geometry_tolerance_m"],
        minimum_face_area_m2=recipe["minimum_face_area_m2"],
    )
    gmsh_generation_seconds = time.perf_counter() - generation_started
    gmsh_identity["native_library_loaded_in_worker"] = _loaded_gmsh_library(gmsh_identity["native_library_path"])
    gmsh_identity["python_binding_loaded_in_worker"] = "gmsh" in sys.modules
    gmsh_identity["worker_pid"] = os.getpid()
    if not gmsh_identity["native_library_loaded_in_worker"]:
        raise MeshPreparationError("Gmsh generation completed but the expected SDK DLL is not loaded in the worker process")
    if mesh.n_node > generation_config["maximum_nodes"] or mesh.n_face > generation_config["maximum_faces"]:
        raise MeshPreparationError(
            f"Gmsh mesh {mesh.n_node} nodes/{mesh.n_face} faces exceeds the frozen {generation_config['maximum_nodes']} node/{generation_config['maximum_faces']} face cap"
        )
    quality_report = mesh.quality_report(minimum_quality=float(generation_config["minimum_face_quality"]))
    if quality_report["faces_below_quality_threshold"]:
        raise MeshPreparationError(
            f"Gmsh mesh has {len(quality_report['faces_below_quality_threshold'])} faces below minimum_face_quality={generation_config['minimum_face_quality']}"
        )
    roles = generation_config["area_roles"]
    try:
        geometry_audit = _audit_g0_mesh(mesh, gmsh_config, generation_config)
    except (TypeError, ValueError) as exc:
        raise MeshPreparationError(f"G0 generated mesh failed trusted recipe audit: {exc}") from exc

    stage = Path(control["attempt_root"])
    artifacts, scratch = stage / "artifacts", stage / "scratch"
    resource = config["resources"]
    limits = {"output_budget_bytes": resource["output_budget_bytes"], "scratch_budget_bytes": resource["scratch_budget_bytes"]}
    mapping_payload = _mesh_payload(mesh, full=False)
    full_payload = _mesh_payload(mesh, full=True)
    mapping_bytes, full_bytes = _canonical_json_bytes(mapping_payload), _canonical_json_bytes(full_payload)
    mapping_sha, full_sha = hashlib.sha256(mapping_bytes).hexdigest(), hashlib.sha256(full_bytes).hexdigest()
    mapping_path, full_path = artifacts / "gmsh_mapping_mesh.json", artifacts / "gmsh_mesh.json"
    for path, payload in ((full_path, full_bytes), (mapping_path, mapping_bytes)):
        _write_no_clobber_bytes(path, payload, output_budget=limits["output_budget_bytes"],
                                root=stage, reserve_bytes=96 * 1024)
    mapping_relative = mapping_path.relative_to(root).as_posix()
    final_artifact_root = Path(config["output_root"]) / config["run_id"] / "artifacts"
    derived_manifest = {
        "format": "b1-synthetic-derived-source-manifest-v1", "synthetic_only": True,
        "raster_path": source["raster_path"], "raster_sha256": source["sha256"],
        "raster_bytes": raster_before["size_bytes"], "raster_mtime_ns": raster_before["mtime_ns"],
        "mesh_path": (final_artifact_root / "gmsh_mapping_mesh.json").as_posix(), "mesh_sha256": mapping_sha,
    }
    derived_manifest_bytes = _canonical_json_bytes(derived_manifest)
    derived_manifest_path = artifacts / "g0_source_manifest.json"
    _write_no_clobber_bytes(derived_manifest_path, derived_manifest_bytes,
                            output_budget=limits["output_budget_bytes"], root=stage,
                            reserve_bytes=96 * 1024)
    derived_source = dict(source)
    derived_source["source_manifest_path"] = derived_manifest_path.relative_to(root).as_posix()
    derived_source["source_manifest_sha256"] = hashlib.sha256(derived_manifest_bytes).hexdigest()
    derived_mesh = {"path": mapping_relative, "sha256": mapping_sha}
    derived_config = dict(config)
    derived_config["format"] = FORMAT
    derived_config["source"] = derived_source
    derived_config["mesh"] = derived_mesh
    derived_code_hashes = control["code_hashes"]
    _write_no_clobber_bytes(
        scratch / "gmsh-generated.json",
        _canonical_json_bytes({"format": "b1-synthetic-gmsh-generated-v1", "worker_pid": os.getpid(),
                               "native_library_loaded_in_worker": gmsh_identity["native_library_loaded_in_worker"],
                               "mesh_sha256": full_sha, "node_count": mesh.n_node,
                               "face_count": mesh.n_face, "recorded_at_ns": time.time_ns()}),
        output_budget=limits["scratch_budget_bytes"], root=scratch,
    )
    g0_metadata = {
        "format": PILOT_EVIDENCE_FORMAT, "action": "g0_gmsh_windowed_mapping",
        "config_sha256": control["config_sha256"], "source_sha256": source["sha256"],
        "input_source_manifest_sha256": source["source_manifest_sha256"],
        "route_code_sha256": control["code_hashes"],
        "preparation_only": True, "simulation_result": False,
        "mesh_generation": {
            "recipe_path": config["mesh"]["path"], "recipe_sha256": config["mesh"]["sha256"],
            "generated_mesh": {"name": full_path.name, "sha256": full_sha, "bytes": len(full_bytes)},
            "mapping_mesh": {"name": mapping_path.name, "sha256": mapping_sha, "bytes": len(mapping_bytes)},
            "derived_source_manifest": {"name": derived_manifest_path.name,
                                         "sha256": hashlib.sha256(derived_manifest_bytes).hexdigest(),
                                         "bytes": len(derived_manifest_bytes)},
            "gmsh": gmsh_identity, "gmsh_generation_seconds": gmsh_generation_seconds,
            "mesh_quality": quality_report,
            "area_audit": geometry_audit["area_audit"],
            "line_coverage_audit": geometry_audit["line_coverage"],
            "recipe_hole_count": len(gmsh_config.holes),
            "area_roles": roles,
            "source_components_before": source_components_before,
            "route_code_sha256": derived_code_hashes,
            "process_in_job_before_gmsh": True,
        },
    }

    def validate_prepared_body(body: Mapping[str, Any]) -> None:
        stored_pilot = body.get("synthetic_pilot")
        stored_generation = stored_pilot.get("mesh_generation") if isinstance(stored_pilot, Mapping) else None
        if not isinstance(stored_generation, Mapping):
            raise ValueError("G0 prepared writer body lacks the trusted mesh-generation audit")
        try:
            _validate_g0_stored_audits(stored_generation, config, geometry_audit)
        except ValueError as exc:
            raise ValueError(f"G0 prepared writer body failed its trusted recipe/config audit: {exc}") from exc

    result = _profiled_mapping(control, derived_config, root, pilot_metadata=g0_metadata,
                               route_version="triangle-b1-g0-gmsh-preparation-v1",
                               prepared_body_validator=validate_prepared_body,
                               body_path_overrides={
                                   "mesh_path": (final_artifact_root / "gmsh_mapping_mesh.json").as_posix(),
                                   "source_manifest_path": (final_artifact_root / "g0_source_manifest.json").as_posix(),
                               })
    recipe_after = _stat_identity(recipe_path)
    recipe_hash_after = _sha256_file(recipe_path, maximum_bytes=1024 * 1024)[0]
    raster_after = _stat_identity(raster_path)
    components_after = _source_component_snapshot(config, root)
    if recipe_stat != recipe_after or recipe_hash_after != config["mesh"]["sha256"]:
        raise SourceIdentityError("G0 Gmsh recipe changed during generation/mapping")
    if raster_before != raster_after or source_components_before != components_after:
        raise SourceIdentityError("G0 source raster/component identity changed during generation/mapping")
    result["pilot"] = json.loads(json.dumps(g0_metadata, ensure_ascii=False, allow_nan=False))
    return result


def run_pilot_worker(control: dict[str, Any], config: dict[str, Any], root: Path) -> dict[str, Any]:
    validate_pilot_config_shape(config)
    if config["action"] == "g0_gmsh_windowed_mapping":
        return _run_g0(control, config, root)
    from .restricted_worker import FORMAT
    p0_config = dict(config)
    p0_config["format"] = FORMAT
    case = dict(config["pilot_case"])
    pilot = {
        "format": PILOT_EVIDENCE_FORMAT, "action": config["action"],
        "config_sha256": control["config_sha256"], "source_sha256": config["source"]["sha256"],
        "input_source_manifest_sha256": config["source"]["source_manifest_sha256"],
        "mesh_path": config["mesh"]["path"], "mesh_sha256": config["mesh"]["sha256"],
        "case": case, "route_code_sha256": control["code_hashes"],
        "preparation_only": True, "simulation_result": False,
    }
    result = _profiled_mapping(control, p0_config, root, pilot_metadata=pilot,
                               route_version="triangle-b1-p0-windowed-mapping-v1")
    return result


def _valid_nonnegative_seconds(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value)) and value >= 0


def validate_pilot_prepared_bundle(bundle_directory: str | Path, manifest: Mapping[str, Any],
                                   config: dict[str, Any], project_root: str | Path) -> dict[str, Any]:
    """Read route-specific pilot evidence without opening the raster or remapping."""
    from .prepared_terrain import PreparedTerrainError, face_id_sequence_sha256
    from .restricted_worker import _resolve_project_path, _sha256_file, _stat_identity
    from .mesh_preparation import GmshMeshConfig
    from .terrain_windowed_mapping import mesh_fingerprint_sha256
    from .triangular_mesh import TriangularMesh

    validate_pilot_config_shape(config)
    root = Path(bundle_directory)
    project = Path(project_root).resolve()
    raster_path = _resolve_project_path(project, config["source"]["raster_path"], "source.raster_path")
    if (raster_path.is_symlink()
            or (config["source"].get("expected_stat_identity") is not None
                and _stat_identity(raster_path) != config["source"]["expected_stat_identity"])):
        raise PreparedTerrainError("synthetic raster identity changed between worker completion and prepared readback")
    source_manifest_path = _resolve_project_path(project, config["source"]["source_manifest_path"], "source.source_manifest_path")
    if source_manifest_path.is_symlink() or _sha256_file(source_manifest_path)[0] != config["source"]["source_manifest_sha256"]:
        raise PreparedTerrainError("synthetic input source manifest identity changed before readback")
    expected_route = ("triangle-b1-p0-windowed-mapping-v1" if config.get("action") == "p0_windowed_mapping"
                      else "triangle-b1-g0-gmsh-preparation-v1")
    if manifest.get("route_version") != expected_route:
        raise PreparedTerrainError("prepared pilot route version differs from the configured action")
    pilot = manifest.get("synthetic_pilot")
    if (not isinstance(pilot, dict) or pilot.get("format") != PILOT_EVIDENCE_FORMAT
            or pilot.get("action") != config.get("action")
            or pilot.get("config_sha256") != manifest.get("bindings", {}).get("config_sha256")
            or pilot.get("source_sha256") != manifest.get("bindings", {}).get("source_sha256")
            or pilot.get("input_source_manifest_sha256") != config.get("source", {}).get("source_manifest_sha256")
            or pilot.get("route_code_sha256") != manifest.get("code", {}).get("route_files_sha256")
            or pilot.get("preparation_only") is not True or pilot.get("simulation_result") is not False):
        raise PreparedTerrainError("synthetic pilot evidence is missing or differs from its bound route identity")
    result_path = root / "artifacts" / "worker-result.json"
    try:
        worker_result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedTerrainError(f"synthetic pilot worker result is malformed: {exc}") from exc
    if worker_result.get("pilot") != pilot or not _valid_nonnegative_seconds(worker_result.get("prepared_writer_seconds")):
        raise PreparedTerrainError("synthetic pilot worker timing/result differs from the prepared body")
    timing = pilot.get("mapping_timing_seconds")
    profile_counts = pilot.get("mapping_profile_counts")
    stats = pilot.get("mapping_stats")
    if (not isinstance(timing, dict)
            or not all(_valid_nonnegative_seconds(timing.get(key)) for key in (
                "source_metadata_io_seconds", "raw_window_io_seconds", "candidate_search_seconds",
                "clip_seconds", "mapper_seconds", "unclassified_mapper_seconds"))
            or not isinstance(profile_counts, dict) or not isinstance(stats, dict)
            or stats != manifest.get("mapping", {}).get("statistics")):
        raise PreparedTerrainError("synthetic pilot mapping timing/count evidence is incomplete")
    if profile_counts.get("clip_calls") != stats.get("clip_calls"):
        raise PreparedTerrainError("synthetic pilot clip-call instrumentation differs from mapper statistics")
    if config["action"] == "p0_windowed_mapping":
        if (pilot.get("mesh_path") != config["mesh"]["path"]
                or pilot.get("mesh_sha256") != config["mesh"]["sha256"]
                or pilot.get("case") != config["pilot_case"]
                or manifest["mesh"]["face_count"] != config["pilot_case"]["face_count"]):
            raise PreparedTerrainError("P0 case/mesh identity differs from the frozen configuration")
        mesh_path = _resolve_project_path(project, config["mesh"]["path"], "mesh.path")
        if mesh_path.is_symlink() or _sha256_file(mesh_path)[0] != config["mesh"]["sha256"]:
            raise PreparedTerrainError("P0 mesh source changed after mapping")
        return {"action": pilot["action"], "mesh_face_count": manifest["mesh"]["face_count"],
                "mapper_stats": stats, "mapping_timing_seconds": timing,
                "prepared_writer_seconds": worker_result["prepared_writer_seconds"]}

    if config["action"] != "g0_gmsh_windowed_mapping":
        raise PreparedTerrainError("synthetic pilot action is unsupported by the reader")
    mesh_generation = pilot.get("mesh_generation")
    if (not isinstance(mesh_generation, dict)
            or mesh_generation.get("recipe_path") != config["mesh"]["path"]
            or mesh_generation.get("recipe_sha256") != config["mesh"]["sha256"]):
        raise PreparedTerrainError("G0 generated mesh evidence is missing")
    recipe_path = _resolve_project_path(project, config["mesh"]["path"], "G0 mesh recipe")
    recipe, _ = _read_recipe(recipe_path, config["mesh"]["sha256"])
    try:
        gmsh_config = GmshMeshConfig.model_validate(recipe["gmsh_config"])
    except Exception as exc:
        raise PreparedTerrainError(f"G0 trusted recipe Gmsh config is invalid: {exc}") from exc
    generated_record = mesh_generation.get("generated_mesh")
    mapping_record = mesh_generation.get("mapping_mesh")
    derived_manifest_record = mesh_generation.get("derived_source_manifest")
    artifacts = root / "artifacts"
    generated_path = artifacts / "gmsh_mesh.json"
    mapping_path = artifacts / "gmsh_mapping_mesh.json"
    derived_manifest_path = artifacts / "g0_source_manifest.json"
    for path, record, expected_name in ((generated_path, generated_record, "gmsh_mesh.json"),
                                        (mapping_path, mapping_record, "gmsh_mapping_mesh.json"),
                                        (derived_manifest_path, derived_manifest_record, "g0_source_manifest.json")):
        if (not isinstance(record, dict) or record.get("name") != expected_name
                or path.is_symlink() or not path.is_file()):
            raise PreparedTerrainError(f"G0 generated mesh artifact record is missing: {expected_name}")
        digest, size = _sha256_file(path)
        if digest != record.get("sha256") or size != record.get("bytes"):
            raise PreparedTerrainError(f"G0 generated mesh artifact identity mismatch: {expected_name}")
    try:
        full_payload = json.loads(generated_path.read_text(encoding="utf-8"))
        mapping_payload = json.loads(mapping_path.read_text(encoding="utf-8"))
        derived_manifest = json.loads(derived_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedTerrainError(f"G0 mesh/source artifact JSON is malformed: {exc}") from exc
    final_artifact_relative = (Path(config["output_root"]) / config["run_id"] / "artifacts").as_posix()
    expected_mapping_path = f"{final_artifact_relative}/gmsh_mapping_mesh.json"
    expected_derived_manifest_path = f"{final_artifact_relative}/g0_source_manifest.json"
    mapping_source_path = root / "artifacts" / "gmsh_mapping_mesh.json"
    if (manifest.get("mesh", {}).get("path") != expected_mapping_path
            or _stat_identity(mapping_source_path) != manifest.get("mesh", {}).get("stat_identity")
            or manifest.get("source", {}).get("source_manifest_path") != expected_derived_manifest_path
            or manifest.get("mesh", {}).get("sha256") != mapping_record.get("sha256")
            or manifest.get("bindings", {}).get("mesh_sha256") != mapping_record.get("sha256")
            or manifest.get("source", {}).get("source_manifest_sha256_actual") != derived_manifest_record.get("sha256")
            or not isinstance(derived_manifest, dict)
            or derived_manifest.get("format") != "b1-synthetic-derived-source-manifest-v1"
            or derived_manifest.get("synthetic_only") is not True
            or derived_manifest.get("raster_path") != config["source"]["raster_path"]
            or derived_manifest.get("raster_sha256") != config["source"]["sha256"]
            or derived_manifest.get("mesh_path") != expected_mapping_path
            or derived_manifest.get("mesh_sha256") != mapping_record.get("sha256")):
        raise PreparedTerrainError("G0 derived source manifest does not bind the final raster/mesh artifact paths")
    expected_full_keys = {"format", "mesh_id", "coordinate_system", "node_xy", "face_nodes", "face_id",
                          "boundary_groups", "constraint_groups", "geometry_tolerance_m", "minimum_face_area_m2"}
    if not isinstance(full_payload, dict) or set(full_payload) != expected_full_keys or full_payload.get("format") != GMSH_MESH_FORMAT:
        raise PreparedTerrainError("G0 generated Gmsh mesh format/fields differ from the frozen contract")
    expected_mapping_keys = {"format", "mesh_id", "coordinate_system", "node_xy", "face_nodes", "face_id",
                             "geometry_tolerance_m", "minimum_face_area_m2"}
    if (not isinstance(mapping_payload, dict) or set(mapping_payload) != expected_mapping_keys
            or mapping_payload.get("format") != "triangle-mesh-xy-json-v1"):
        raise PreparedTerrainError("G0 B0 mapping-mesh format/fields differ from the frozen contract")
    try:
        full_mesh = TriangularMesh(
            node_xy=full_payload["node_xy"], face_nodes=full_payload["face_nodes"],
            face_id=full_payload["face_id"], coordinate_system=full_payload["coordinate_system"],
            mesh_id=full_payload["mesh_id"], boundary_groups=full_payload["boundary_groups"],
            constraint_groups=full_payload["constraint_groups"],
            geometry_tolerance_m=full_payload["geometry_tolerance_m"],
            minimum_face_area_m2=full_payload["minimum_face_area_m2"],
        )
        mapping_mesh = TriangularMesh(
            node_xy=mapping_payload["node_xy"], face_nodes=mapping_payload["face_nodes"],
            face_id=mapping_payload["face_id"], coordinate_system=mapping_payload["coordinate_system"],
            mesh_id=mapping_payload["mesh_id"], geometry_tolerance_m=mapping_payload["geometry_tolerance_m"],
            minimum_face_area_m2=mapping_payload["minimum_face_area_m2"],
        )
    except Exception as exc:
        raise PreparedTerrainError(f"G0 generated mesh topology/quality input is invalid: {exc}") from exc
    if (full_mesh.mesh_id != manifest["mesh"]["mesh_id"]
            or full_mesh.n_node != manifest["mesh"]["node_count"]
            or full_mesh.n_face != manifest["mesh"]["face_count"]
            or full_mesh.coordinate_system != recipe["coordinate_system"]
            or full_mesh.geometry_tolerance_m != float(recipe["geometry_tolerance_m"])
            or full_mesh.minimum_face_area_m2 != float(recipe["minimum_face_area_m2"])
            or full_mesh.node_xy != mapping_mesh.node_xy
            or full_mesh.face_nodes != mapping_mesh.face_nodes
            or full_mesh.face_id != mapping_mesh.face_id
            or mesh_fingerprint_sha256(full_mesh) != manifest["mesh"]["mesh_fingerprint_sha256"]
            or face_id_sequence_sha256(full_mesh.face_id) != manifest["mesh"]["face_id_sequence_sha256"]):
        raise PreparedTerrainError("G0 full/mapping mesh geometry differs from the prepared mesh identity")
    if mesh_generation.get("process_in_job_before_gmsh") is not True:
        raise PreparedTerrainError("G0 did not prove Job membership before Gmsh generation")
    gmsh = mesh_generation.get("gmsh")
    if (not isinstance(gmsh, dict) or gmsh.get("native_library_loaded_in_worker") is not True
            or gmsh.get("python_binding_loaded_in_worker") is not True
            or gmsh.get("python_binding_version") != config["mesh_generation"]["expected_gmsh_version"]
            or gmsh.get("native_library_sha256") != config["mesh_generation"]["expected_gmsh_library_sha256"]
            or gmsh.get("worker_pid") not in manifest.get("worker", {}).get("job_process_ids_observed", [])):
        raise PreparedTerrainError("G0 Gmsh binding/DLL execution is not bound to the restricted worker tree")
    quality = full_mesh.quality_report(minimum_quality=float(config["mesh_generation"]["minimum_face_quality"]))
    if quality != mesh_generation.get("mesh_quality") or quality["faces_below_quality_threshold"]:
        raise PreparedTerrainError("G0 generated mesh quality differs from its accepted quality report")
    if mesh_generation.get("recipe_hole_count") != len(gmsh_config.holes):
        raise PreparedTerrainError("G0 stored recipe hole count differs from the trusted recipe")
    try:
        recomputed_audit = _audit_g0_mesh(full_mesh, gmsh_config, config["mesh_generation"])
        _validate_g0_stored_audits(mesh_generation, config, recomputed_audit)
    except (KeyError, TypeError, ValueError) as exc:
        raise PreparedTerrainError(f"G0 prepared geometry/role audit differs from the trusted recipe/config: {exc}") from exc
    return {"action": pilot["action"], "mesh_face_count": full_mesh.n_face,
            "quality": quality, "area_audit": recomputed_audit["area_audit"],
            "line_coverage_audit": recomputed_audit["line_coverage"],
            "mapping_timing_seconds": timing, "prepared_writer_seconds": worker_result["prepared_writer_seconds"]}
