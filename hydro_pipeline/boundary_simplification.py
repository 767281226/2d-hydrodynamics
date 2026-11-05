"""Opt-in, topology-preserving short-edge collapse for D−S mesh-only boundaries.

This module changes only the configured polygon geometry that Gmsh meshes.
It never reads DEM data and never repairs invalid input.  The rule:

* Every ring edge shorter than ``minimum_vertex_spacing_m`` is a collapse
  candidate, shortest first.
* A collapse drops one endpoint (the new edge joins its neighbours).  On the
  security area S the collapse must keep the simplified S containing the
  source S: only reflex/collinear S vertices may be dropped; a convex short
  S edge may instead be merged into the single outward corner point where its
  two neighbouring edges meet (never inside S).
* A collapse is accepted only when every source vertex it represents stays
  within ``minimum_vertex_spacing_m`` of the new edge, the new edges do not
  intersect or touch any other ring edge, the removed/added triangle contains
  no other ring vertex, protected vertices (open boundary-group and
  constraint anchors) are kept, and every ring keeps at least three vertices.
  Edges that cannot be collapsed safely are kept and reported.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

BOUNDARY_SIMPLIFICATION_FIELDS = frozenset({"enabled", "minimum_vertex_spacing_m"})
SPACING_MAXIMUM_M = 50.0
BOUNDARY_SIMPLIFICATION_AUDIT_FORMAT = "domain-boundary-simplification-audit-v1"
COLLAPSE_RULE = "shortest_edge_first_endpoint_drop_with_source_vertex_offset_bound"
SECURITY_POLICY = "simplified_S_contains_source_S_reflex_drop_or_outward_corner_merge"

Point = tuple[float, float]


class BoundarySimplificationError(ValueError):
    """The requested simplification would break validity or S exclusion."""


def validate_boundary_simplification_shape(value: Any, *, label: str = "boundary_simplification") -> dict[str, Any]:
    """Validate the complete object; no field has an implicit default."""
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object with enabled and minimum_vertex_spacing_m")
    if set(value) != BOUNDARY_SIMPLIFICATION_FIELDS:
        raise ValueError(f"{label} fields must be exactly enabled and minimum_vertex_spacing_m")
    if type(value["enabled"]) is not bool:
        raise ValueError(f"{label}.enabled must be a JSON boolean")
    spacing = value["minimum_vertex_spacing_m"]
    if (isinstance(spacing, bool) or not isinstance(spacing, (int, float)) or not math.isfinite(float(spacing))
            or not 0.0 < float(spacing) <= SPACING_MAXIMUM_M):
        raise ValueError(f"{label}.minimum_vertex_spacing_m must be a finite number in (0, {SPACING_MAXIMUM_M:g}] m")
    return {"enabled": value["enabled"], "minimum_vertex_spacing_m": float(spacing)}


def _cross(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _signed_area(ring: Sequence[Point]) -> float:
    origin = ring[0]
    return 0.5 * math.fsum(
        (ring[i][0] - origin[0]) * (ring[i + 1][1] - origin[1]) - (ring[i + 1][0] - origin[0]) * (ring[i][1] - origin[1])
        for i in range(1, len(ring) - 1)
    )


def _point_segment_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 == 0.0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _segments_conflict(a: Point, b: Point, c: Point, d: Point, tolerance: float) -> bool:
    """True when segments touch, overlap or cross (shared endpoints are excluded by callers)."""
    if (max(a[0], b[0]) + tolerance < min(c[0], d[0]) or max(c[0], d[0]) + tolerance < min(a[0], b[0])
            or max(a[1], b[1]) + tolerance < min(c[1], d[1]) or max(c[1], d[1]) + tolerance < min(a[1], b[1])):
        return False
    d1, d2, d3, d4 = _cross(c, d, a), _cross(c, d, b), _cross(a, b, c), _cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0:
        return True
    return min(_point_segment_distance(a, c, d), _point_segment_distance(b, c, d),
               _point_segment_distance(c, a, b), _point_segment_distance(d, a, b)) <= tolerance


def _point_in_triangle(p: Point, a: Point, b: Point, c: Point) -> bool:
    """Closed triangle test; the bounding box keeps degenerate (collinear) triangles finite."""
    if (p[0] < min(a[0], b[0], c[0]) or p[0] > max(a[0], b[0], c[0])
            or p[1] < min(a[1], b[1], c[1]) or p[1] > max(a[1], b[1], c[1])):
        return False
    d1, d2, d3 = _cross(a, b, p), _cross(b, c, p), _cross(c, a, p)
    has_negative = d1 < 0 or d2 < 0 or d3 < 0
    has_positive = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_negative and has_positive)


def _line_intersection(a: Point, b: Point, c: Point, d: Point) -> tuple[Point, float, float] | None:
    """Intersection X = b + t*(b-a)/|b-a| = c + u*(c-d)/|c-d| with t, u in metres."""
    ab = math.dist(a, b)
    dc = math.dist(d, c)
    if ab == 0.0 or dc == 0.0:
        return None
    r = ((b[0] - a[0]) / ab, (b[1] - a[1]) / ab)
    s = ((c[0] - d[0]) / dc, (c[1] - d[1]) / dc)
    denominator = r[0] * s[1] - r[1] * s[0]
    if abs(denominator) < 1e-12:
        return None
    qp = (c[0] - b[0], c[1] - b[1])
    t = (qp[0] * s[1] - qp[1] * s[0]) / denominator
    u = (qp[0] * r[1] - qp[1] * r[0]) / denominator
    return (b[0] + t * r[0], b[1] + t * r[1]), t, u


@dataclass
class _Ring:
    role: str
    points: list[Point]
    # represented[i]: source vertices removed from the edge points[i] -> points[i+1].
    represented: list[list[Point]]
    security: bool
    allow_merge: bool


@dataclass(frozen=True)
class SimplificationResult:
    domain_geometry: Any
    security_geometry: Any
    exterior: tuple[Point, ...]
    domain_holes: tuple[tuple[Point, ...], ...]
    security_rings: tuple[tuple[Point, ...], ...]
    kept_source_points: frozenset[Point]
    audit: dict[str, Any]


def _ring_points(ring: Any) -> list[Point]:
    points = [(float(x), float(y)) for x, y, *_ in ring.coords]
    if points and points[0] == points[-1]:
        points.pop()
    return points


def _all_edges(rings: Sequence[_Ring]) -> Iterable[tuple[int, int, Point, Point]]:
    for ring_index, ring in enumerate(rings):
        count = len(ring.points)
        for index in range(count):
            yield ring_index, index, ring.points[index], ring.points[(index + 1) % count]


def _candidate_valid(rings: Sequence[_Ring], ring_index: int, removed: Sequence[Point],
                     new_edges: Sequence[tuple[Point, Point]], triangle: tuple[Point, Point, Point],
                     tolerance: float) -> bool:
    removed_set = set(removed)
    for other_ring_index, _edge_index, c, d in _all_edges(rings):
        if c in removed_set or d in removed_set:
            continue
        for a, b in new_edges:
            if {a, b} & {c, d}:
                # Adjacent edges may only share that endpoint; reject a fold-back overlap.
                shared = a if a in (c, d) else b
                other_new = b if shared == a else a
                other_old = d if shared == c else c
                if _point_segment_distance(other_old, a, b) <= tolerance or _point_segment_distance(other_new, c, d) <= tolerance:
                    return False
                continue
            if _segments_conflict(a, b, c, d, tolerance):
                return False
    corners = set(triangle)
    for ring in rings:
        for point in ring.points:
            if point in corners or point in removed_set:
                continue
            if _point_in_triangle(point, *triangle):
                return False
    return True


def _try_collapse(rings: list[_Ring], ring_index: int, edge_index: int, spacing: float, tolerance: float,
                  protected: frozenset[Point]) -> tuple[str, float] | None:
    ring = rings[ring_index]
    count = len(ring.points)
    if count <= 3:
        return None
    i_b, i_c = edge_index, (edge_index + 1) % count
    i_a, i_d = (edge_index - 1) % count, (edge_index + 2) % count
    a, b, c, d = ring.points[i_a], ring.points[i_b], ring.points[i_c], ring.points[i_d]
    rep_ab, rep_bc, rep_cd = ring.represented[i_a], ring.represented[i_b], ring.represented[i_c]
    orientation = 1.0 if _signed_area(ring.points) > 0 else -1.0
    candidates: list[tuple[float, str, Any]] = []

    def deviation(represented: Sequence[Point], start: Point, end: Point) -> float:
        return max((_point_segment_distance(p, start, end) for p in represented), default=0.0)

    # Drop b: new edge a -> c.
    if b not in protected and (not ring.security or orientation * _cross(a, b, c) <= 0.0):
        represented = [*rep_ab, b, *rep_bc]
        offset = deviation(represented, a, c)
        if offset <= spacing and count > 3:
            candidates.append((offset, "drop_start", (i_b, represented, (a, c), (a, b, c))))
    # Drop c: new edge b -> d.
    if c not in protected and (not ring.security or orientation * _cross(b, c, d) <= 0.0):
        represented = [*rep_bc, c, *rep_cd]
        offset = deviation(represented, b, d)
        if offset <= spacing and count > 3:
            candidates.append((offset, "drop_end", (i_c, represented, (b, d), (b, c, d))))
    # Security-only outward corner merge of a convex short edge.
    if ring.security and ring.allow_merge and b not in protected and c not in protected and count > 3:
        hit = _line_intersection(a, b, c, d)
        if hit is not None:
            x, t, u = hit
            if t > tolerance and u > tolerance:
                represented_left = [*rep_ab, b]
                represented_right = [*rep_bc, c, *rep_cd]
                offset = max(_point_segment_distance(x, b, c),
                             deviation(represented_left, a, x), deviation(represented_right, x, d))
                # x must lie on the exterior side of edge b->c so that triangle b-x-c is added to S.
                if offset <= spacing and orientation * _cross(b, c, x) < 0.0:
                    candidates.append((offset, "merge_outward", (x, represented_left, represented_right)))
    for offset, kind, payload in sorted(candidates, key=lambda item: (item[0], item[1])):
        if kind in ("drop_start", "drop_end"):
            drop_index, represented, new_edge, triangle = payload
            dropped = ring.points[drop_index]
            if not _candidate_valid(rings, ring_index, [dropped], [new_edge], triangle, tolerance):
                continue
            previous = (drop_index - 1) % count
            ring.represented[previous] = represented
            del ring.points[drop_index]
            del ring.represented[drop_index]
            return kind, offset
        x, represented_left, represented_right = payload
        if not _candidate_valid(rings, ring_index, [b, c], [(a, x), (x, d)], (b, x, c), tolerance):
            continue
        # Replace b and c by x; edge a->x represents rep_ab+b, edge x->d represents rep_bc+c+rep_cd.
        ring.points[i_b] = x
        ring.represented[i_a] = represented_left
        ring.represented[i_b] = represented_right
        del ring.points[i_c]
        del ring.represented[i_c]
        return kind, offset
    return None


def _simplify_rings(rings: list[_Ring], spacing: float, tolerance: float, protected: frozenset[Point]) -> dict[str, int]:
    counts = {"drop_start": 0, "drop_end": 0, "merge_outward": 0}
    blocked: set[tuple[Point, Point]] = set()
    while True:
        best: tuple[float, int, int] | None = None
        for ring_index, edge_index, start, end in _all_edges(rings):
            length = math.dist(start, end)
            if length < spacing and (start, end) not in blocked and len(rings[ring_index].points) > 3:
                key = (length, ring_index, edge_index)
                if best is None or key < best:
                    best = key
        if best is None:
            return counts
        _length, ring_index, edge_index = best
        ring = rings[ring_index]
        edge = (ring.points[edge_index], ring.points[(edge_index + 1) % len(ring.points)])
        outcome = _try_collapse(rings, ring_index, edge_index, spacing, tolerance, protected)
        if outcome is None:
            blocked.add(edge)
        else:
            counts[outcome[0]] += 1
            blocked.clear()


def _geometry_sha256(geometry: Any) -> str:
    return hashlib.sha256(geometry.wkb).hexdigest()


def _segment_stats(points: Sequence[Point], spacing: float) -> dict[str, Any]:
    lengths = [math.dist(points[i], points[(i + 1) % len(points)]) for i in range(len(points))]
    return {"vertex_count": len(points), "minimum_segment_m": min(lengths),
            "segments_below_spacing": sum(length < spacing for length in lengths)}


def simplify_domain_and_security(
    domain_geometry: Any,
    security_geometry: Any,
    *,
    minimum_vertex_spacing_m: float,
    geometry_tolerance_m: float,
    area_tolerance_m2: float,
    protected_points: Iterable[Point] = (),
    whole_ring_groups: Iterable[Sequence[Point]] = (),
) -> SimplificationResult:
    """Collapse short D/S ring edges; S stays a subset of the simplified S."""
    import shapely
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    spacing = float(minimum_vertex_spacing_m)
    if spacing <= geometry_tolerance_m:
        raise BoundarySimplificationError("minimum_vertex_spacing_m must exceed geometry_tolerance_m")
    if domain_geometry.geom_type != "Polygon":
        raise BoundarySimplificationError("boundary simplification requires one D Polygon")
    security_parts = list(getattr(security_geometry, "geoms", [security_geometry]))
    if any(part.geom_type != "Polygon" or len(part.interiors) for part in security_parts):
        raise BoundarySimplificationError(
            "UNSUPPORTED_SIMPLIFICATION_TOPOLOGY: S parts must be Polygons without holes")
    if (not security_geometry.within(domain_geometry)
            or security_geometry.distance(domain_geometry.exterior) <= geometry_tolerance_m
            or any(security_geometry.distance(ring) <= geometry_tolerance_m for ring in domain_geometry.interiors)):
        raise BoundarySimplificationError(
            "UNSUPPORTED_SIMPLIFICATION_TOPOLOGY: S must lie strictly inside D without touching any D ring")
    whole_ring_sets = [frozenset((float(p[0]), float(p[1])) for p in group) for group in whole_ring_groups]
    rings: list[_Ring] = []
    sources: list[list[Point]] = []
    for role, ring_geometry, security in (
        ("domain_D_exterior", domain_geometry.exterior, False),
        *((f"domain_D_hole_{i}", ring, False) for i, ring in enumerate(domain_geometry.interiors)),
        *((f"security_S_part_{i}", part.exterior, True) for i, part in enumerate(security_parts)),
    ):
        points = _ring_points(ring_geometry)
        sources.append(list(points))
        allow_merge = frozenset(points) not in whole_ring_sets
        rings.append(_Ring(role, points, [[] for _ in points], security, allow_merge))
    protected = frozenset((float(p[0]), float(p[1])) for p in protected_points)
    counts = _simplify_rings(rings, spacing, float(geometry_tolerance_m), protected)

    exterior = tuple(rings[0].points)
    domain_hole_count = len(domain_geometry.interiors)
    domain_holes = tuple(tuple(ring.points) for ring in rings[1:1 + domain_hole_count])
    security_rings = tuple(tuple(ring.points) for ring in rings[1 + domain_hole_count:])
    simplified_domain = Polygon(exterior, domain_holes)
    simplified_security = unary_union([Polygon(ring) for ring in security_rings])
    if not simplified_domain.is_valid or simplified_domain.area <= 0.0:
        raise BoundarySimplificationError("simplified D is invalid or empty; no repair is applied")
    if not simplified_security.is_valid or len(getattr(simplified_security, "geoms", [simplified_security])) != len(security_parts):
        raise BoundarySimplificationError("simplified S is invalid or changed its part count")
    if (not simplified_security.within(simplified_domain)
            or simplified_security.distance(simplified_domain.exterior) <= geometry_tolerance_m):
        raise BoundarySimplificationError("simplified S no longer lies strictly inside simplified D")
    source_active = domain_geometry.difference(security_geometry)
    simplified_active = simplified_domain.difference(simplified_security)
    if simplified_active.geom_type != "Polygon" or len(simplified_active.interiors) != len(source_active.interiors):
        raise BoundarySimplificationError("simplified D−S changed its connected-surface or hole topology")
    source_s_not_covered = float(security_geometry.difference(simplified_security).area)
    active_inside_source_s = float(simplified_active.intersection(security_geometry).area)
    if source_s_not_covered > area_tolerance_m2 or active_inside_source_s > area_tolerance_m2:
        raise BoundarySimplificationError(
            f"SIMPLIFIED_S_EXCLUSION_VIOLATED: simplified D−S overlaps source S by {active_inside_source_s:.12g} m2")

    ring_records = []
    max_offset = 0.0
    for ring, source in zip(rings, sources):
        offset = float(shapely.hausdorff_distance(LineString([*source, source[0]]),
            LineString([*ring.points, ring.points[0]]), densify=0.25))
        max_offset = max(max_offset, offset)
        before = _segment_stats(source, spacing)
        after = _segment_stats(ring.points, spacing)
        ring_records.append({
            "role": ring.role,
            "vertices_before": before["vertex_count"], "vertices_after": after["vertex_count"],
            "minimum_segment_before_m": before["minimum_segment_m"],
            "minimum_segment_after_m": after["minimum_segment_m"],
            "segments_below_spacing_before": before["segments_below_spacing"],
            "segments_below_spacing_after": after["segments_below_spacing"],
            "area_before_m2": abs(_signed_area(source)), "area_after_m2": abs(_signed_area(ring.points)),
            "hausdorff_offset_m": offset,
            "new_vertex_count": len(set(ring.points) - set(source)),
        })
    source_active_area = float(source_active.area)
    simplified_active_area = float(simplified_active.area)
    kept = frozenset(point for ring in rings for point in ring.points)
    audit = {
        "format": BOUNDARY_SIMPLIFICATION_AUDIT_FORMAT,
        "enabled": True,
        "minimum_vertex_spacing_m": spacing,
        "collapse_rule": COLLAPSE_RULE,
        "security_policy": SECURITY_POLICY,
        "collapse_counts": counts,
        "rings": ring_records,
        "maximum_hausdorff_offset_m": max_offset,
        "source_D_area_m2": float(domain_geometry.area),
        "simplified_D_area_m2": float(simplified_domain.area),
        "source_S_area_m2": float(security_geometry.area),
        "simplified_S_area_m2": float(simplified_security.area),
        "source_active_area_m2": source_active_area,
        "simplified_active_area_m2": simplified_active_area,
        "active_area_change_m2": simplified_active_area - source_active_area,
        "active_area_relative_change": (simplified_active_area - source_active_area) / source_active_area,
        "active_symmetric_difference_m2": float(simplified_active.symmetric_difference(source_active).area),
        "simplified_active_outside_source_D_m2": float(simplified_active.difference(domain_geometry).area),
        "simplified_active_inside_source_S_m2": active_inside_source_s,
        "source_S_outside_simplified_S_m2": source_s_not_covered,
        "simplified_D_sha256": _geometry_sha256(simplified_domain),
        "simplified_S_sha256": _geometry_sha256(simplified_security),
    }
    json.dumps(audit, allow_nan=False)
    return SimplificationResult(simplified_domain, simplified_security, exterior, domain_holes, security_rings, kept, audit)


__all__ = [
    "BOUNDARY_SIMPLIFICATION_AUDIT_FORMAT", "BOUNDARY_SIMPLIFICATION_FIELDS", "BoundarySimplificationError",
    "SPACING_MAXIMUM_M", "SimplificationResult", "simplify_domain_and_security",
    "validate_boundary_simplification_shape",
]
