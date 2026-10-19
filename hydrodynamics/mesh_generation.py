"""Optional Gmsh constrained-triangulation adapter."""

from __future__ import annotations

import math
import os
import platform
from importlib import metadata, util
from pathlib import Path
import shutil
from typing import Sequence

from .mesh_preparation import GmshMeshConfig, MeshPreparationError
from .triangular_mesh import TriangularMesh


def _ring_area(ring: Sequence[tuple[float, float]]) -> float:
    if len(ring) < 3:
        return 0.0
    origin = ring[0]
    return 0.5 * sum(
        (ring[i][0] - origin[0]) * (ring[i + 1][1] - origin[1])
        - (ring[i + 1][0] - origin[0]) * (ring[i][1] - origin[1])
        for i in range(1, len(ring) - 1)
    )


def _cross(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _point_on_segment(
    point: tuple[float, float], a: tuple[float, float], b: tuple[float, float], tolerance_m: float
) -> bool:
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    return (
        abs(_cross(a, b, point)) <= tolerance_m * max(length, tolerance_m)
        and min(a[0], b[0]) - tolerance_m <= point[0] <= max(a[0], b[0]) + tolerance_m
        and min(a[1], b[1]) - tolerance_m <= point[1] <= max(a[1], b[1]) + tolerance_m
    )


def _segment_relation(
    a: tuple[float, float], b: tuple[float, float],
    c: tuple[float, float], d: tuple[float, float],
    tolerance_m: float,
) -> str:
    """Classify finite-segment contact as none, touch, proper, or overlap."""
    ab_c, ab_d = _cross(a, b, c), _cross(a, b, d)
    cd_a, cd_b = _cross(c, d, a), _cross(c, d, b)
    epsilon_ab = tolerance_m * max(math.hypot(b[0] - a[0], b[1] - a[1]), tolerance_m)
    epsilon_cd = tolerance_m * max(math.hypot(d[0] - c[0], d[1] - c[1]), tolerance_m)
    signs_ab = (1 if ab_c > epsilon_ab else -1 if ab_c < -epsilon_ab else 0, 1 if ab_d > epsilon_ab else -1 if ab_d < -epsilon_ab else 0)
    signs_cd = (1 if cd_a > epsilon_cd else -1 if cd_a < -epsilon_cd else 0, 1 if cd_b > epsilon_cd else -1 if cd_b < -epsilon_cd else 0)
    if signs_ab[0] * signs_ab[1] < 0 and signs_cd[0] * signs_cd[1] < 0:
        return "proper"
    if all(sign == 0 for sign in (*signs_ab, *signs_cd)):
        axis = 0 if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else 1
        overlap = min(max(a[axis], b[axis]), max(c[axis], d[axis])) - max(min(a[axis], b[axis]), min(c[axis], d[axis]))
        if overlap > tolerance_m:
            return "overlap"
        if overlap >= -tolerance_m:
            return "touch"
        return "none"
    if (
        (signs_ab[0] == 0 and _point_on_segment(c, a, b, tolerance_m))
        or (signs_ab[1] == 0 and _point_on_segment(d, a, b, tolerance_m))
        or (signs_cd[0] == 0 and _point_on_segment(a, c, d, tolerance_m))
        or (signs_cd[1] == 0 and _point_on_segment(b, c, d, tolerance_m))
    ):
        return "touch"
    return "none"


def _validate_ring(
    ring: Sequence[tuple[float, float]], name: str, *, geometry_tolerance_m: float, minimum_area_m2: float
) -> tuple[tuple[float, float], ...]:
    points = tuple((float(point[0]), float(point[1])) for point in ring)
    if len(points) < 3:
        raise MeshPreparationError(f"{name} must contain at least three unique vertices without a repeated closing point")
    if any(not math.isfinite(value) for point in points for value in point):
        raise MeshPreparationError(f"{name} coordinates must be finite metres")
    for left in range(len(points)):
        for right in range(left + 1, len(points)):
            if math.hypot(points[left][0] - points[right][0], points[left][1] - points[right][1]) <= geometry_tolerance_m:
                raise MeshPreparationError(f"{name} repeats a vertex within geometry_tolerance_m={geometry_tolerance_m:g}")
    if abs(_ring_area(points)) <= minimum_area_m2:
        raise MeshPreparationError(f"{name} must enclose a positive area")
    for i in range(len(points)):
        a, b = points[i], points[(i + 1) % len(points)]
        if math.hypot(b[0] - a[0], b[1] - a[1]) <= geometry_tolerance_m:
            raise MeshPreparationError(f"{name} contains a segment at or below geometry_tolerance_m")
        for j in range(i + 1, len(points)):
            relation = _segment_relation(a, b, points[j], points[(j + 1) % len(points)], geometry_tolerance_m)
            adjacent = j == i + 1 or (i == 0 and j == len(points) - 1)
            if adjacent and relation == "touch":
                continue
            if relation != "none":
                raise MeshPreparationError(f"{name} self-intersects or self-touches at segments {i} and {j}")
    return points


def _inside(point: tuple[float, float], ring: Sequence[tuple[float, float]]) -> bool:
    inside = False
    x, y = point
    for i, start in enumerate(ring):
        end = ring[(i + 1) % len(ring)]
        if (start[1] > y) != (end[1] > y) and x < (end[0] - start[0]) * (y - start[1]) / (end[1] - start[1]) + start[0]:
            inside = not inside
    return inside


def _on_segment(point: tuple[float, float], a: tuple[float, float], b: tuple[float, float], tolerance: float) -> bool:
    edge_length = math.hypot(b[0] - a[0], b[1] - a[1])
    if abs(_cross(a, b, point)) > tolerance * edge_length:
        return False
    return min(a[0], b[0]) - tolerance <= point[0] <= max(a[0], b[0]) + tolerance and min(a[1], b[1]) - tolerance <= point[1] <= max(a[1], b[1]) + tolerance


def _on_ring(point: tuple[float, float], ring: Sequence[tuple[float, float]], tolerance: float) -> bool:
    return any(_on_segment(point, ring[i], ring[(i + 1) % len(ring)], tolerance) for i in range(len(ring)))


def _validate_geometry(
    config: GmshMeshConfig, *, geometry_tolerance_m: float, minimum_face_area_m2: float
) -> tuple[tuple[float, float], ...]:
    validate = lambda ring, name: _validate_ring(ring, name, geometry_tolerance_m=geometry_tolerance_m, minimum_area_m2=minimum_face_area_m2)
    outer = validate(config.outer_boundary, "outer_boundary")
    holes = [validate(ring, f"holes[{index}]") for index, ring in enumerate(config.holes)]
    for index, hole in enumerate(holes):
        if not _inside(hole[0], outer):
            raise MeshPreparationError(f"holes[{index}] must lie strictly inside outer_boundary")
        if any(_on_ring(point, outer, geometry_tolerance_m) for point in hole):
            raise MeshPreparationError(f"holes[{index}] must not touch outer_boundary")
        if any(_segment_relation(hole[i], hole[(i + 1) % len(hole)], outer[j], outer[(j + 1) % len(outer)], geometry_tolerance_m) != "none" for i in range(len(hole)) for j in range(len(outer))):
            raise MeshPreparationError(f"holes[{index}] intersects outer_boundary")
        for other_index, other in enumerate(holes[:index]):
            if _inside(hole[0], other) or _inside(other[0], hole) or any(_on_ring(point, other, geometry_tolerance_m) for point in hole) or any(
                _segment_relation(hole[i], hole[(i + 1) % len(hole)], other[j], other[(j + 1) % len(other)], geometry_tolerance_m) != "none"
                for i in range(len(hole)) for j in range(len(other))
            ):
                raise MeshPreparationError(f"holes[{index}] overlaps holes[{other_index}]")

    all_rings = [outer, *holes]
    for group in config.boundary_groups:
        points = tuple((float(point[0]), float(point[1])) for point in group.points)
        if points[0] == points[-1] and not group.closed:
            raise MeshPreparationError(f"boundary group '{group.name}' repeats its first point; set closed: true or remove the duplicate")
        if group.closed and points[0] == points[-1]:
            points = points[:-1]
        if not points or any(not any(point in ring for ring in all_rings) for point in points):
            raise MeshPreparationError(f"boundary group '{group.name}' points must be vertices of the configured outer boundary or a hole")
        pairs = list(zip(points, points[1:]))
        if group.closed:
            if len(points) < 3:
                raise MeshPreparationError(f"closed boundary group '{group.name}' requires at least three points")
            pairs.append((points[-1], points[0]))
        for point, next_point in pairs:
            if not any((point == ring[j] and next_point == ring[(j + 1) % len(ring)]) or (next_point == ring[j] and point == ring[(j + 1) % len(ring)]) for ring in all_rings for j in range(len(ring))):
                raise MeshPreparationError(f"boundary group '{group.name}' includes a segment that is not one configured boundary edge")

    constraint_segments: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    for group in config.constraints:
        if group.closed:
            raise MeshPreparationError(f"constraint group '{group.name}' must be an open path")
        points = tuple((float(point[0]), float(point[1])) for point in group.points)
        if len(set(points)) != len(points):
            raise MeshPreparationError(f"constraint group '{group.name}' repeats a vertex")
        for point in points:
            if not _inside(point, outer) and not _on_ring(point, outer, geometry_tolerance_m):
                raise MeshPreparationError(f"constraint group '{group.name}' must lie inside outer_boundary")
            if _on_ring(point, outer, geometry_tolerance_m) and point not in outer:
                raise MeshPreparationError(f"constraint group '{group.name}' touches outer_boundary away from a configured vertex; split the boundary there first")
            if any(_inside(point, hole) or _on_ring(point, hole, geometry_tolerance_m) for hole in holes):
                raise MeshPreparationError(f"constraint group '{group.name}' crosses the interior of a hole")
        for segment_index in range(len(points) - 1):
            a, b = points[segment_index], points[segment_index + 1]
            if math.hypot(b[0] - a[0], b[1] - a[1]) <= geometry_tolerance_m:
                raise MeshPreparationError(f"constraint group '{group.name}' has a segment at or below geometry_tolerance_m")
            for ring in all_rings:
                for edge_index, c in enumerate(ring):
                    d = ring[(edge_index + 1) % len(ring)]
                    relation = _segment_relation(a, b, c, d, geometry_tolerance_m)
                    if relation == "none":
                        continue
                    shared_endpoint = set((a, b)).intersection((c, d))
                    anchored_endpoint = relation == "touch" and bool(shared_endpoint) and any(point in ring for point in shared_endpoint for ring in (outer,))
                    if ring is not outer or not anchored_endpoint:
                        raise MeshPreparationError(f"constraint group '{group.name}' intersects, touches, or overlaps a region boundary; split the geometry explicitly")
            for previous_name, c, d in constraint_segments:
                relation = _segment_relation(a, b, c, d, geometry_tolerance_m)
                if relation == "none":
                    continue
                is_adjacent = (
                    relation == "touch" and previous_name == group.name and segment_index > 0
                    and c == points[segment_index - 1] and d == points[segment_index]
                    and a == points[segment_index]
                )
                if not is_adjacent:
                    raise MeshPreparationError(f"constraint group '{group.name}' intersects or self-touches constraint group '{previous_name}'; split all lines at the shared vertex")
            constraint_segments.append((group.name, a, b))
    return outer


def _gmsh_native_library_path() -> Path | None:
    """Locate the native Gmsh SDK library before importing its ctypes wrapper."""
    module_spec = util.find_spec("gmsh")
    if module_spec is None or module_spec.origin is None:
        return None
    try:
        version = metadata.version("gmsh")
        major_minor = ".".join(version.split(".")[:2])
    except metadata.PackageNotFoundError:
        major_minor = "4.15"
    if platform.system() == "Windows":
        library_names = (f"gmsh-{major_minor}.dll", "gmsh.dll")
    elif platform.system() == "Darwin":
        library_names = (f"libgmsh.{major_minor}.dylib", "libgmsh.dylib")
    else:
        library_names = (f"libgmsh.so.{major_minor}", "libgmsh.so")
    module_dir = Path(module_spec.origin).resolve().parent
    search_dirs = [
        module_dir, module_dir / "lib", module_dir / "Lib", module_dir / "bin",
        module_dir.parent, module_dir.parent / "lib", module_dir.parent / "Lib", module_dir.parent / "bin",
        module_dir.parent.parent, module_dir.parent.parent / "lib", module_dir.parent.parent / "Lib",
        module_dir.parent.parent / "bin", module_dir.parent.parent / "Library" / "bin",
    ]
    search_dirs.extend(Path(entry) for entry in os.environ.get("PATH", "").split(os.pathsep) if entry)
    for directory in search_dirs:
        for name in library_names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    # The command-line launcher may be on PATH with the corresponding DLL.
    executable = shutil.which("gmsh")
    if executable:
        directory = Path(executable).resolve().parent
        for name in library_names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def generate_gmsh_mesh(
    config: GmshMeshConfig,
    *,
    coordinate_system: str,
    geometry_tolerance_m: float = 1e-8,
    minimum_face_area_m2: float = 1e-12,
) -> TriangularMesh:
    """Generate a constrained triangle mesh; raises a clear error if Gmsh is absent."""
    if isinstance(geometry_tolerance_m, bool) or not isinstance(geometry_tolerance_m, (int, float)) or not math.isfinite(geometry_tolerance_m) or not 0.0 < geometry_tolerance_m <= 1.0:
        raise MeshPreparationError("geometry_tolerance_m must be greater than 0 and at most 1 m")
    if isinstance(minimum_face_area_m2, bool) or not isinstance(minimum_face_area_m2, (int, float)) or not math.isfinite(minimum_face_area_m2) or not 0.0 < minimum_face_area_m2 <= 100.0:
        raise MeshPreparationError("minimum_face_area_m2 must be greater than 0 and at most 100 m2")
    if config.minimum_size_m <= geometry_tolerance_m or any(not config.minimum_size_m <= point.size_m <= config.default_size_m for point in config.refinement_points):
        raise MeshPreparationError("Gmsh refinement sizes must be between minimum_size_m and default_size_m, and above geometry_tolerance_m")
    _validate_geometry(config, geometry_tolerance_m=geometry_tolerance_m, minimum_face_area_m2=minimum_face_area_m2)
    from .mesh_preparation import _validate_projected_metric_crs
    _validate_projected_metric_crs(coordinate_system)
    if util.find_spec("gmsh") is None:
        raise MeshPreparationError("Gmsh constrained generation requires the optional Python package 'gmsh'; install it with pip install '.[gmsh]'")
    native_library = _gmsh_native_library_path()
    if native_library is None:
        raise MeshPreparationError(
            "The Gmsh Python package is installed but its native SDK library is missing. "
            "Install the matching Gmsh SDK and put its shared library beside gmsh.py or on PATH."
        )
    dll_directory_handle = None
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        # The PyPI wheel places the SDK DLL under the environment's Lib folder.
        # Keep this handle alive for the full API call so Windows can resolve
        # Gmsh's adjacent runtime dependencies without a global PATH mutation.
        try:
            dll_directory_handle = os.add_dll_directory(str(native_library.parent))
        except OSError as exc:
            raise MeshPreparationError(f"Could not add the native Gmsh DLL directory '{native_library.parent}' to the Windows loader: {exc}") from exc
    try:
        import gmsh
    except (ImportError, OSError) as exc:  # pragma: no cover - depends on optional environment
        raise MeshPreparationError("Gmsh constrained generation requires optional dependency 'gmsh'; install it with pip install '.[gmsh]'") from exc

    initialized_here = not bool(gmsh.isInitialized())
    if initialized_here:
        gmsh.initialize([])
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.option.setNumber("Mesh.MeshSizeMin", config.minimum_size_m)
        gmsh.option.setNumber("Mesh.MeshSizeMax", config.default_size_m)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 1)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        gmsh.option.setNumber("Mesh.ElementOrder", 1)
        gmsh.option.setNumber("Mesh.RecombineAll", 0)
        gmsh.model.add("triangular_mesh_preparation")

        point_tags: dict[tuple[float, float], int] = {}
        def point_tag(point: Sequence[float], size: float | None = None) -> int:
            key = (float(point[0]), float(point[1]))
            if key not in point_tags:
                point_tags[key] = gmsh.model.geo.addPoint(key[0], key[1], 0.0, size or config.default_size_m)
            return point_tags[key]

        curve_tags: dict[tuple[tuple[float, float], tuple[float, float]], int] = {}
        def add_ring(ring: Sequence[tuple[float, float]], *, clockwise: bool = False) -> tuple[int, list[int]]:
            points = list(ring)
            if (not clockwise and _ring_area(points) < 0) or (clockwise and _ring_area(points) > 0):
                points.reverse()
            tags: list[int] = []
            for index, start in enumerate(points):
                end = points[(index + 1) % len(points)]
                tag = gmsh.model.geo.addLine(point_tag(start), point_tag(end))
                curve_tags[(start, end)] = tag
                curve_tags[(end, start)] = tag
                tags.append(tag)
            return gmsh.model.geo.addCurveLoop(tags), tags

        outer = _validate_ring(config.outer_boundary, "outer_boundary", geometry_tolerance_m=geometry_tolerance_m, minimum_area_m2=minimum_face_area_m2)
        outer_loop, outer_curves = add_ring(outer)
        loops = [outer_loop]
        boundary_curve_by_name: dict[str, list[int]] = {"outer_boundary": outer_curves}
        for ring_index, ring in enumerate(config.holes):
            hole_points = _validate_ring(ring, f"holes[{ring_index}]", geometry_tolerance_m=geometry_tolerance_m, minimum_area_m2=minimum_face_area_m2)
            hole_loop, hole_curves = add_ring(hole_points, clockwise=True)
            loops.append(hole_loop)
            boundary_curve_by_name[f"hole_{ring_index}"] = hole_curves
        surface = gmsh.model.geo.addPlaneSurface(loops)

        for group in config.boundary_groups:
            coordinates = [(float(point[0]), float(point[1])) for point in group.points]
            if group.closed and coordinates[0] == coordinates[-1]:
                coordinates.pop()
            pairs = list(zip(coordinates, coordinates[1:]))
            if group.closed:
                pairs.append((coordinates[-1], coordinates[0]))
            try:
                boundary_curve_by_name[group.name] = [curve_tags[pair] for pair in pairs]
            except KeyError as exc:
                raise MeshPreparationError(f"boundary group '{group.name}' cannot be resolved to configured boundary curves") from exc

        constraint_curve_by_name: dict[str, list[int]] = {}
        embedded_curves: list[int] = []
        for group in config.constraints:
            coords = [(float(point[0]), float(point[1])) for point in group.points]
            tags = [gmsh.model.geo.addLine(point_tag(coords[index]), point_tag(coords[index + 1])) for index in range(len(coords) - 1)]
            for index, tag in enumerate(tags):
                curve_tags[(coords[index], coords[index + 1])] = tag
                curve_tags[(coords[index + 1], coords[index])] = tag
            constraint_curve_by_name[group.name] = tags
            embedded_curves.extend(tags)
        gmsh.model.geo.synchronize()
        if embedded_curves:
            gmsh.model.mesh.embed(1, embedded_curves, 2, surface)

        refinement_point_tags: list[int] = []
        for refinement in config.refinement_points:
            tag = point_tag(refinement.point, refinement.size_m)
            refinement_point_tags.append(tag)
        gmsh.model.geo.synchronize()
        if refinement_point_tags:
            gmsh.model.mesh.embed(0, refinement_point_tags, 2, surface)

        fields: list[int] = []
        for refinement, tag in zip(config.refinement_points, refinement_point_tags):
            distance = gmsh.model.mesh.field.add("Distance")
            gmsh.model.mesh.field.setNumbers(distance, "PointsList", [tag])
            threshold = gmsh.model.mesh.field.add("Threshold")
            gmsh.model.mesh.field.setNumber(threshold, "InField", distance)
            gmsh.model.mesh.field.setNumber(threshold, "SizeMin", refinement.size_m)
            gmsh.model.mesh.field.setNumber(threshold, "SizeMax", config.default_size_m)
            gmsh.model.mesh.field.setNumber(threshold, "DistMin", 0.0)
            gmsh.model.mesh.field.setNumber(threshold, "DistMax", refinement.radius_m)
            fields.append(threshold)
        if fields:
            minimum = gmsh.model.mesh.field.add("Min")
            gmsh.model.mesh.field.setNumbers(minimum, "FieldsList", fields)
            gmsh.model.mesh.field.setAsBackgroundMesh(minimum)

        gmsh.model.mesh.generate(2)
        node_tags, xyz, _ = gmsh.model.mesh.getNodes()
        if len(node_tags) > config.maximum_nodes:
            raise MeshPreparationError(f"Gmsh generated {len(node_tags)} nodes, exceeding maximum_nodes={config.maximum_nodes}")
        tag_to_index = {int(tag): index for index, tag in enumerate(node_tags)}
        nodes = tuple((float(xyz[index * 3]), float(xyz[index * 3 + 1])) for index in range(len(node_tags)))
        element_types, _, element_nodes = gmsh.model.mesh.getElements(2, surface)
        raw_faces: list[tuple[int, int, int]] = []
        for element_type, tags in zip(element_types, element_nodes):
            properties = gmsh.model.mesh.getElementProperties(element_type)
            if int(properties[1]) != 2:
                continue
            if int(properties[3]) != 3 or int(properties[2]) != 1:
                raise MeshPreparationError("Gmsh generated a non-linear or non-triangular surface element; only first-order triangles are supported")
            connectivity = list(tags)
            for offset in range(0, len(connectivity), 3):
                face = tuple(tag_to_index[int(value)] for value in connectivity[offset:offset + 3])
                a, b, c = (nodes[index] for index in face)
                if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) < 0:
                    face = (face[0], face[2], face[1])
                raw_faces.append(face)
        if not raw_faces:
            raise MeshPreparationError("Gmsh produced no first-order triangular faces")

        def line_edges(groups: dict[str, list[int]]) -> dict[str, list[tuple[int, int]]]:
            output: dict[str, list[tuple[int, int]]] = {}
            for name, curves in groups.items():
                pairs: list[tuple[int, int]] = []
                for curve in curves:
                    line_types, _, line_nodes = gmsh.model.mesh.getElements(1, curve)
                    for element_type, connectivity in zip(line_types, line_nodes):
                        properties = gmsh.model.mesh.getElementProperties(element_type)
                        count = int(properties[3])
                        if int(properties[1]) != 1 or count != 2:
                            continue
                        for offset in range(0, len(connectivity), 2):
                            pairs.append((tag_to_index[int(connectivity[offset])], tag_to_index[int(connectivity[offset + 1])]))
                if pairs:
                    output[name] = pairs
            return output

        boundary_groups = line_edges(boundary_curve_by_name)
        constraint_groups = line_edges(constraint_curve_by_name)
        missing_boundary = set(boundary_curve_by_name) - set(boundary_groups)
        missing_constraints = set(constraint_curve_by_name) - set(constraint_groups)
        if missing_boundary:
            raise MeshPreparationError(f"Gmsh did not preserve boundary groups: {', '.join(sorted(missing_boundary))}")
        if missing_constraints:
            raise MeshPreparationError(f"Gmsh did not preserve constraint groups: {', '.join(sorted(missing_constraints))}")
        return TriangularMesh(nodes, tuple(raw_faces), coordinate_system, boundary_groups=boundary_groups, constraint_groups=constraint_groups, geometry_tolerance_m=geometry_tolerance_m, minimum_face_area_m2=minimum_face_area_m2)
    except MeshPreparationError:
        raise
    except Exception as exc:
        raise MeshPreparationError(f"Gmsh constrained triangulation failed: {exc}") from exc
    finally:
        if initialized_here and gmsh.isInitialized():
            gmsh.finalize()


__all__ = ["generate_gmsh_mesh"]
