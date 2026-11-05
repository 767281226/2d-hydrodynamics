"""Solver-independent triangular mesh and face-terrain contracts.

Coordinates are projected metres. Connectivity is zero-based and oriented
counter-clockwise. Topology indices are local to this mesh; ``face_id`` is a
stable external identifier and is kept when serializing the mesh.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from math import atan2, degrees, hypot, isfinite, sqrt
from numbers import Integral, Real
from typing import Mapping, Sequence


class TriangularMeshError(ValueError):
    """Raised when a triangular mesh violates its geometry contract."""


_METRE_UNIT_ALIASES = {"m", "meter", "meters", "metre", "metres"}


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(float(value)):
        raise TriangularMeshError(f"{name} must be a finite number")
    return float(value)


def _triangle_area(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    # Translate before taking the determinant so projected eastings northings
    # around 10^6 do not lose the small area in cancellation.
    return 0.5 * ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))


def _cross(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _signed_polygon_area(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    origin = points[0]
    return 0.5 * sum(
        (points[i][0] - origin[0]) * (points[i + 1][1] - origin[1])
        - (points[i + 1][0] - origin[0]) * (points[i][1] - origin[1])
        for i in range(1, len(points) - 1)
    )


def _triangle_intersection_area(
    subject: tuple[tuple[float, float], ...],
    clip: tuple[tuple[float, float], ...],
) -> float:
    """Return the convex intersection area, including boundary-contained cases."""
    polygon = list(subject)
    coordinate_scale = max(1.0, *(abs(value) for point in (*subject, *clip) for value in point))
    coordinate_epsilon = max(1e-14, math.ulp(coordinate_scale) * 8.0)
    for edge_index, start in enumerate(clip):
        end = clip[(edge_index + 1) % 3]
        edge_length = hypot(end[0] - start[0], end[1] - start[1])
        cross_epsilon = coordinate_epsilon * edge_length
        if not polygon:
            return 0.0
        output: list[tuple[float, float]] = []
        previous = polygon[-1]
        previous_side = _cross(start, end, previous)
        previous_inside = previous_side >= -cross_epsilon
        for current in polygon:
            current_side = _cross(start, end, current)
            current_inside = current_side >= -cross_epsilon
            if current_inside != previous_inside:
                direction_x, direction_y = current[0] - previous[0], current[1] - previous[1]
                clip_x, clip_y = end[0] - start[0], end[1] - start[1]
                denominator = direction_x * clip_y - direction_y * clip_x
                if denominator != 0.0:
                    fraction = ((start[0] - previous[0]) * clip_y - (start[1] - previous[1]) * clip_x) / denominator
                    output.append((previous[0] + fraction * direction_x, previous[1] + fraction * direction_y))
            if current_inside:
                output.append(current)
            previous, previous_inside = current, current_inside
        polygon = output
    return abs(_signed_polygon_area(polygon))


def _overlapping_faces(
    nodes: tuple[tuple[float, float], ...],
    faces: tuple[tuple[int, int, int], ...],
    areas: tuple[float, ...],
    geometry_tolerance_m: float,
) -> tuple[int, int] | None:
    """Reject positive-area overlaps using a uniform spatial bucket index.

    Faces spanning too many buckets are kept in an overflow list and compared
    with every subsequent face, so the index cannot omit long, narrow cells.
    """
    if len(faces) < 2:
        return None
    min_x = min(point[0] for point in nodes)
    max_x = max(point[0] for point in nodes)
    min_y = min(point[1] for point in nodes)
    max_y = max(point[1] for point in nodes)
    extent_area = (max_x - min_x) * (max_y - min_y)
    if not isfinite(extent_area):
        raise TriangularMeshError("node coordinate extent exceeds finite geometry limits")
    side = max(sqrt(extent_area / max(len(faces), 1)), 1e-9)
    buckets: dict[tuple[int, int], list[int]] = {}
    oversized: list[int] = []
    for face_index, face in enumerate(faces):
        points = tuple(nodes[node] for node in face)
        xs = tuple(point[0] for point in points)
        ys = tuple(point[1] for point in points)
        ix0, ix1 = int((min(xs) - min_x) / side), int((max(xs) - min_x) / side)
        iy0, iy1 = int((min(ys) - min_y) / side), int((max(ys) - min_y) / side)
        cells = (ix1 - ix0 + 1) * (iy1 - iy0 + 1)
        if cells > 4096:
            candidates = range(face_index)
        else:
            candidates = set(oversized)
            for iy in range(iy0, iy1 + 1):
                for ix in range(ix0, ix1 + 1):
                    candidates.update(buckets.get((ix, iy), ()))
        for other_index in candidates:
            other = faces[other_index]
            if len(set(face).intersection(other)) >= 2:
                continue  # Shared full edge is legal; orientation was checked separately.
            other_points = tuple(nodes[node] for node in other)
            intersection_area = _triangle_intersection_area(points, other_points)
            numerical_floor = max(geometry_tolerance_m**2, math.ulp(max(areas[face_index], areas[other_index], 1.0)) * 32.0)
            if intersection_area > numerical_floor:
                return other_index, face_index
        if cells > 4096:
            oversized.append(face_index)
        else:
            for iy in range(iy0, iy1 + 1):
                for ix in range(ix0, ix1 + 1):
                    buckets.setdefault((ix, iy), []).append(face_index)
    return None


@dataclass(frozen=True)
class TriangularMesh:
    """Validated triangular topology with derived edges and geometric values."""

    node_xy: Sequence[Sequence[float]]
    face_nodes: Sequence[Sequence[int]]
    coordinate_system: str
    mesh_id: str | None = None
    face_id: Sequence[int] | None = None
    boundary_groups: Mapping[str, Sequence[Sequence[int]]] | None = None
    constraint_groups: Mapping[str, Sequence[Sequence[int]]] | None = None
    geometry_tolerance_m: float = 1e-8
    minimum_face_area_m2: float = 1e-12

    def __post_init__(self) -> None:
        if not isinstance(self.coordinate_system, str) or not self.coordinate_system.strip():
            raise TriangularMeshError("coordinate_system (projected CRS) is required")
        object.__setattr__(self, "coordinate_system", self.coordinate_system.strip())
        tolerance = _finite(self.geometry_tolerance_m, "geometry_tolerance_m")
        minimum_area = _finite(self.minimum_face_area_m2, "minimum_face_area_m2")
        if not 0.0 < tolerance <= 1.0:
            raise TriangularMeshError("geometry_tolerance_m must be greater than 0 and at most 1 m")
        if not 0.0 < minimum_area <= 100.0:
            raise TriangularMeshError("minimum_face_area_m2 must be greater than 0 and at most 100 m2")
        object.__setattr__(self, "geometry_tolerance_m", tolerance)
        object.__setattr__(self, "minimum_face_area_m2", minimum_area)

        try:
            nodes = tuple(tuple(_finite(value, f"node_xy[{i}][{j}]") for j, value in enumerate(point)) for i, point in enumerate(self.node_xy))
        except TypeError as exc:
            raise TriangularMeshError("node_xy must be a sequence of 2-coordinate points") from exc
        if not nodes:
            raise TriangularMeshError("node_xy must contain at least 3 nodes")
        for index, point in enumerate(nodes):
            if len(point) != 2:
                raise TriangularMeshError(f"node_xy[{index}] must contain exactly two coordinates")
        coordinate_buckets: dict[tuple[int, int], list[tuple[int, tuple[float, float]]]] = {}
        for index, point in enumerate(nodes):
            scaled = (point[0] / tolerance, point[1] / tolerance)
            if not all(isfinite(value) for value in scaled):
                raise TriangularMeshError(f"node_xy[{index}] is outside the numerical range for geometry_tolerance_m")
            bucket = (math.floor(scaled[0]), math.floor(scaled[1]))
            for delta_x in (-1, 0, 1):
                for delta_y in (-1, 0, 1):
                    for previous_index, previous in coordinate_buckets.get((bucket[0] + delta_x, bucket[1] + delta_y), ()):
                        if hypot(point[0] - previous[0], point[1] - previous[1]) <= tolerance:
                            raise TriangularMeshError(f"node_xy[{index}] is a duplicate or within geometry_tolerance_m of node_xy[{previous_index}]")
            coordinate_buckets.setdefault(bucket, []).append((index, point))

        try:
            raw_faces = tuple(tuple(face) for face in self.face_nodes)
        except TypeError as exc:
            raise TriangularMeshError("face_nodes must be a sequence of 3-node faces") from exc
        if not raw_faces:
            raise TriangularMeshError("face_nodes must contain at least one triangle")
        faces: list[tuple[int, int, int]] = []
        seen_faces: set[tuple[int, int, int]] = set()
        face_areas: list[float] = []
        for index, face in enumerate(raw_faces):
            if len(face) != 3:
                raise TriangularMeshError(f"face_nodes[{index}] must contain exactly 3 node indices")
            if any(isinstance(value, bool) or not isinstance(value, Integral) for value in face):
                raise TriangularMeshError(f"face_nodes[{index}] node indices must be integers")
            normalized = tuple(int(value) for value in face)
            if len(set(normalized)) != 3:
                raise TriangularMeshError(f"face_nodes[{index}] contains repeated node indices")
            if any(value < 0 or value >= len(nodes) for value in normalized):
                raise TriangularMeshError(f"face_nodes[{index}] contains an out-of-range node index")
            area = _triangle_area(*(nodes[value] for value in normalized))
            if not isfinite(area):
                raise TriangularMeshError(f"face_nodes[{index}] produces a non-finite area")
            if area <= minimum_area:
                if abs(area) <= minimum_area:
                    raise TriangularMeshError(f"face_nodes[{index}] has zero or near-zero area (m2)")
                raise TriangularMeshError(f"face_nodes[{index}] must be counter-clockwise with positive area")
            canonical = tuple(sorted(normalized))
            if canonical in seen_faces:
                raise TriangularMeshError(f"duplicate triangle at face_nodes[{index}]")
            seen_faces.add(canonical)
            faces.append(normalized)
            face_areas.append(area)
        normalized_faces = tuple(faces)

        mesh_payload = json.dumps({"crs": self.coordinate_system, "nodes": nodes, "faces": [tuple(sorted(face)) for face in normalized_faces]}, separators=(",", ":"), sort_keys=True).encode("utf-8")
        resolved_mesh_id = self.mesh_id or sha256(mesh_payload).hexdigest()[:24]
        if not isinstance(resolved_mesh_id, str) or not resolved_mesh_id.strip():
            raise TriangularMeshError("mesh_id must be a non-empty string")
        object.__setattr__(self, "mesh_id", resolved_mesh_id.strip())

        if self.face_id is None:
            face_ids = tuple(int.from_bytes(sha256((resolved_mesh_id + ":" + ",".join(map(str, sorted(face)))).encode("ascii")).digest()[:8], "big") & ((1 << 63) - 1) for face in normalized_faces)
        else:
            try:
                face_ids = tuple(self.face_id)
            except TypeError as exc:
                raise TriangularMeshError("face_id must contain one integer identifier per face") from exc
        if len(face_ids) != len(normalized_faces) or any(isinstance(value, bool) or not isinstance(value, Integral) or value < 0 or value > (1 << 63) - 1 for value in face_ids):
            raise TriangularMeshError("face_id must contain one non-negative int64 identifier per face")
        face_ids = tuple(int(value) for value in face_ids)
        if len(set(face_ids)) != len(face_ids):
            raise TriangularMeshError("face_id values must be unique")

        edge_to_faces: dict[tuple[int, int], list[tuple[int, tuple[int, int]]]] = {}
        face_edges: list[tuple[int, int, int]] = []
        for face_index, face in enumerate(normalized_faces):
            local_edges: list[int] = []
            for first, second in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
                key = (min(first, second), max(first, second))
                incidences = edge_to_faces.setdefault(key, [])
                incidences.append((face_index, (first, second)))
                local_edges.append(-1)
            face_edges.append(tuple(local_edges))
        for edge, incidences in edge_to_faces.items():
            if len(incidences) > 2:
                raise TriangularMeshError(f"non-manifold edge {edge} is shared by {len(incidences)} faces")
            if len(incidences) == 2 and incidences[0][1] == incidences[1][1]:
                raise TriangularMeshError(f"adjacent faces have the same orientation along edge {edge}")
        overlap = _overlapping_faces(nodes, normalized_faces, tuple(face_areas), tolerance)
        if overlap is not None:
            raise TriangularMeshError(f"faces {overlap[0]} and {overlap[1]} overlap in positive area")

        edge_nodes = tuple(sorted(edge_to_faces))
        edge_lookup = {edge: index for index, edge in enumerate(edge_nodes)}
        edge_faces = tuple(
            tuple([incidences[0][0], incidences[1][0]] if len(incidences) == 2 else [incidences[0][0], -1])
            for edge in edge_nodes
            for incidences in [edge_to_faces[edge]]
        )
        boundary_degree: dict[int, int] = {}
        for edge_index, (left_face, right_face) in enumerate(edge_faces):
            if right_face == -1:
                for node in edge_nodes[edge_index]:
                    boundary_degree[node] = boundary_degree.get(node, 0) + 1
        dangling = [node for node, degree in boundary_degree.items() if degree != 2]
        if dangling:
            raise TriangularMeshError(f"boundary topology is open or branched at node indices {dangling[:8]}; each boundary node must meet exactly two boundary edges")
        final_face_edges = tuple(
            tuple(edge_lookup[(min(a, b), max(a, b))] for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])))
            for face in normalized_faces
        )
        edge_lengths = tuple(hypot(nodes[a][0] - nodes[b][0], nodes[a][1] - nodes[b][1]) for a, b in edge_nodes)
        if any(not isfinite(length) for length in edge_lengths):
            raise TriangularMeshError("edge lengths exceed finite geometry limits")
        short_edges = [index for index, length in enumerate(edge_lengths) if length <= tolerance]
        if short_edges:
            raise TriangularMeshError(f"mesh edges {short_edges[:8]} have length at or below geometry_tolerance_m ({tolerance:g} m)")
        centroids = tuple(
            tuple(nodes[face[0]][axis] + ((nodes[face[1]][axis] - nodes[face[0]][axis]) + (nodes[face[2]][axis] - nodes[face[0]][axis])) / 3.0 for axis in (0, 1))
            for face in normalized_faces
        )
        if any(not isfinite(value) for point in centroids for value in point):
            raise TriangularMeshError("face centroids exceed finite geometry limits")

        def normalize_groups(groups: Mapping[str, Sequence[Sequence[int]]] | None, label: str, *, boundary_only: bool) -> dict[str, tuple[int, ...]]:
            normalized_groups: dict[str, tuple[int, ...]] = {}
            if groups is not None and not isinstance(groups, Mapping):
                raise TriangularMeshError(f"{label}_groups must be a mapping of names to node-pair lists")
            for name, pairs in (groups or {}).items():
                if not isinstance(name, str) or not name.strip():
                    raise TriangularMeshError(f"{label} group names must be non-empty strings")
                indices: list[int] = []
                normalized_name = name.strip()
                if normalized_name in normalized_groups:
                    raise TriangularMeshError(f"duplicate {label} group name '{normalized_name}' after trimming whitespace")
                try:
                    pair_list = tuple(pairs)
                except TypeError as exc:
                    raise TriangularMeshError(f"{label} group '{name}' must contain node-pair lists") from exc
                for pair in pair_list:
                    try:
                        normalized_pair = tuple(pair)
                    except TypeError as exc:
                        raise TriangularMeshError(f"{label} group '{name}' entries must be pairs of integer node indices") from exc
                    if len(normalized_pair) != 2 or any(isinstance(node, bool) or not isinstance(node, Integral) for node in normalized_pair):
                        raise TriangularMeshError(f"{label} group '{name}' entries must be pairs of integer node indices")
                    edge = (min(int(normalized_pair[0]), int(normalized_pair[1])), max(int(normalized_pair[0]), int(normalized_pair[1])))
                    if edge not in edge_lookup:
                        raise TriangularMeshError(f"{label} group '{name}' refers to non-mesh edge {edge}")
                    edge_index = edge_lookup[edge]
                    if boundary_only and edge_faces[edge_index][1] != -1:
                        raise TriangularMeshError(f"boundary group '{name}' refers to internal edge {edge}")
                    indices.append(edge_index)
                if not indices or len(set(indices)) != len(indices):
                    raise TriangularMeshError(f"{label} group '{name}' must contain unique mesh edges")
                normalized_groups[normalized_name] = tuple(indices)
            return normalized_groups

        boundary = normalize_groups(self.boundary_groups, "boundary", boundary_only=True)
        constraints = normalize_groups(self.constraint_groups, "constraint", boundary_only=False)

        qualities: list[float] = []
        min_angles: list[float] = []
        for face, area in zip(normalized_faces, face_areas):
            lengths = tuple(hypot(nodes[face[(i + 1) % 3]][0] - nodes[face[i]][0], nodes[face[(i + 1) % 3]][1] - nodes[face[i]][1]) for i in range(3))
            scale = max(lengths)
            scaled_lengths = tuple(length / scale for length in lengths)
            qualities.append(4.0 * sqrt(3.0) * (area / scale / scale) / sum(length * length for length in scaled_lengths))
            angles = []
            for i in range(3):
                opposite = scaled_lengths[i]
                adjacent_a, adjacent_b = scaled_lengths[(i + 1) % 3], scaled_lengths[(i + 2) % 3]
                cosine = max(-1.0, min(1.0, (adjacent_a * adjacent_a + adjacent_b * adjacent_b - opposite * opposite) / (2 * adjacent_a * adjacent_b)))
                angles.append(degrees(atan2(sqrt(max(0.0, 1.0 - cosine * cosine)), cosine)))
            min_angles.append(min(angles))

        object.__setattr__(self, "node_xy", nodes)
        object.__setattr__(self, "face_nodes", normalized_faces)
        object.__setattr__(self, "face_id", face_ids)
        object.__setattr__(self, "edge_nodes", edge_nodes)
        object.__setattr__(self, "edge_faces", edge_faces)
        object.__setattr__(self, "face_edges", final_face_edges)
        object.__setattr__(self, "face_area", tuple(face_areas))
        object.__setattr__(self, "face_centroid", centroids)
        object.__setattr__(self, "edge_length", edge_lengths)
        object.__setattr__(self, "boundary_groups", boundary)
        object.__setattr__(self, "constraint_groups", constraints)
        object.__setattr__(self, "face_quality", tuple(qualities))
        object.__setattr__(self, "minimum_angle_deg", tuple(min_angles))

    @property
    def n_node(self) -> int:
        return len(self.node_xy)

    @property
    def n_face(self) -> int:
        return len(self.face_nodes)

    @property
    def n_edge(self) -> int:
        return len(self.edge_nodes)

    def quality_report(self, *, minimum_quality: float = 0.0) -> dict[str, object]:
        """Return descriptive geometry/topology statistics; no solver claims."""
        threshold = _finite(minimum_quality, "minimum_quality")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("minimum_quality must be between 0 and 1")
        bad = [index for index, value in enumerate(self.face_quality) if value < threshold]
        return {
            "mesh_id": self.mesh_id,
            "preparation_only": True,
            "node_count": self.n_node,
            "face_count": self.n_face,
            "edge_count": self.n_edge,
            "boundary_edge_count": sum(right == -1 for _, right in self.edge_faces),
            "internal_edge_count": sum(right != -1 for _, right in self.edge_faces),
            "total_area_m2": sum(self.face_area),
            "measured_minimum_face_area_m2": min(self.face_area),
            "minimum_face_area_threshold_m2": self.minimum_face_area_m2,
            "measured_minimum_quality": min(self.face_quality),
            "minimum_quality_threshold": threshold,
            "minimum_angle_deg": min(self.minimum_angle_deg),
            "geometry_tolerance_m": self.geometry_tolerance_m,
            "faces_below_quality_threshold": bad,
            "boundary_groups": {name: len(edges) for name, edges in self.boundary_groups.items()},
            "constraint_groups": {name: len(edges) for name, edges in self.constraint_groups.items()},
        }


@dataclass(frozen=True)
class FaceTerrainField:
    """Terrain values and overlap coverage aligned to one triangular mesh."""

    mesh_id: str
    elevation_m: Sequence[float | None]
    valid_mask: Sequence[bool]
    covered_area_m2: Sequence[float]
    valid_area_m2: Sequence[float]
    coverage_ratio: Sequence[float]
    coordinate_system: str
    vertical_datum: str | None = None
    vertical_unit: str = "m"
    vertical_unit_source: str = "config_declaration"
    vertical_datum_source: str | None = None
    source_vertical_unit: str | None = None
    source_vertical_datum: str | None = None

    def validate_for(self, mesh: TriangularMesh) -> "FaceTerrainField":
        if self.mesh_id != mesh.mesh_id:
            raise TriangularMeshError(f"terrain mesh_id '{self.mesh_id}' does not match mesh '{mesh.mesh_id}'")
        if self.coordinate_system.strip() != mesh.coordinate_system:
            raise TriangularMeshError("terrain CRS does not match mesh CRS")
        if self.vertical_unit != "m":
            raise TriangularMeshError("face terrain elevation_m must be in metres")
        if self.vertical_unit_source not in {"dem_metadata", "config_declaration"}:
            raise TriangularMeshError("vertical_unit_source must identify DEM metadata or an explicit config declaration")
        if self.vertical_datum_source not in {None, "dem_metadata", "config_declaration"}:
            raise TriangularMeshError("vertical_datum_source must identify DEM metadata or an explicit config declaration")
        if self.source_vertical_unit is not None and (not isinstance(self.source_vertical_unit, str) or not self.source_vertical_unit.strip()):
            raise TriangularMeshError("source_vertical_unit must be a non-empty source metadata value or None")
        if self.source_vertical_datum is not None and (not isinstance(self.source_vertical_datum, str) or not self.source_vertical_datum.strip()):
            raise TriangularMeshError("source_vertical_datum must be a non-empty source metadata value or None")
        if self.vertical_unit_source == "dem_metadata":
            if self.source_vertical_unit is None or self.source_vertical_unit.strip().casefold() not in _METRE_UNIT_ALIASES:
                raise TriangularMeshError("DEM-metadata unit provenance requires a source unit equivalent to metres")
        elif self.source_vertical_unit is not None:
            raise TriangularMeshError("config-declared metre units cannot also claim a source unit in DEM metadata")
        if self.vertical_datum_source == "dem_metadata":
            if self.source_vertical_datum is None or self.vertical_datum != self.source_vertical_datum.strip():
                raise TriangularMeshError("DEM-metadata datum provenance must retain the same source vertical datum")
        elif self.vertical_datum_source == "config_declaration":
            if self.source_vertical_datum is not None or self.vertical_datum is None:
                raise TriangularMeshError("config-declared datum provenance must supplement missing source metadata")
        elif self.source_vertical_datum is not None or self.vertical_datum is not None:
            raise TriangularMeshError("vertical datum values require matching metadata or config-declaration provenance")
        for name in ("elevation_m", "valid_mask", "covered_area_m2", "valid_area_m2", "coverage_ratio"):
            if len(getattr(self, name)) != mesh.n_face:
                raise TriangularMeshError(f"terrain {name} must have one value per face ({mesh.n_face})")
        for index, (value, valid, ratio, area, covered) in enumerate(zip(self.elevation_m, self.valid_mask, self.coverage_ratio, self.valid_area_m2, self.covered_area_m2)):
            if not isinstance(valid, bool):
                raise TriangularMeshError(f"terrain valid_mask[{index}] must be boolean")
            if valid != (value is not None):
                raise TriangularMeshError(f"terrain elevation_m[{index}] must be present exactly when valid_mask is true")
            if valid:
                _finite(value, f"terrain elevation_m[{index}]")
            for name, number in (("coverage_ratio", ratio), ("valid_area_m2", area), ("covered_area_m2", covered)):
                _finite(number, f"terrain {name}[{index}]")
            if not 0.0 <= float(ratio) <= 1.0 + 1e-9:
                raise TriangularMeshError(f"terrain coverage_ratio[{index}] must be between 0 and 1")
            if area < -1e-9 or covered < -1e-9 or area > covered + 1e-8:
                raise TriangularMeshError(f"terrain areas are inconsistent for face {index}")
            face_area = mesh.face_area[index]
            if covered > face_area + max(1e-8, face_area * 1e-9):
                raise TriangularMeshError(f"terrain covered_area_m2[{index}] exceeds its triangle area")
            expected_coverage = min(1.0, float(area) / face_area)
            if abs(float(ratio) - expected_coverage) > 1e-8:
                raise TriangularMeshError(f"terrain coverage_ratio[{index}] does not equal valid_area_m2 / face_area")
            if valid != (float(area) > 0.0):
                raise TriangularMeshError(f"terrain valid_mask[{index}] must match whether valid_area_m2 is positive")
        return self


__all__ = ["FaceTerrainField", "TriangularMesh", "TriangularMeshError"]
