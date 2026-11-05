"""Opt-in boundary-adaptive Gmsh size control for constrained 2-D meshing.

The rule is deterministic and only changes target edge sizes; it never moves,
adds or removes configured boundary/constraint vertices:

* every ring vertex (outer boundary and holes, cyclic) and every open
  constraint-path vertex gets a local size equal to its shortest adjacent
  configured segment, clamped to ``[minimum_size_m, default_size_m]``;
* vertices whose local size is below ``default_size_m`` are grouped into
  power-of-two bins (``2**floor(log2(size))``, clamped to ``minimum_size_m``);
* each bin becomes one Gmsh ``Distance`` + ``Threshold`` field whose target
  size grows linearly from the bin's smallest member local size at the vertex
  to ``default_size_m`` at distance ``(default_size_m - size_min) / grading_ratio``.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

BOUNDARY_ADAPTIVE_SIZING_FIELDS = frozenset({"enabled", "grading_ratio"})
GRADING_RATIO_MINIMUM = 0.05
GRADING_RATIO_MAXIMUM = 1.0
BOUNDARY_ADAPTIVE_SIZING_RECEIPT_FORMAT = "gmsh-boundary-adaptive-sizing-execution-v1"
LOCAL_SIZE_RULE = "min_adjacent_configured_segment_length_clamped_to_minimum_and_default_size"
BIN_RULE = "power_of_two_floor_groups_threshold_at_smallest_member_size"

Point = tuple[float, float]


def validate_boundary_adaptive_sizing_shape(value: Any, *, label: str = "boundary_adaptive_sizing") -> dict[str, Any]:
    """Validate the complete object; no field has an implicit default."""
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object with enabled and grading_ratio")
    if set(value) != BOUNDARY_ADAPTIVE_SIZING_FIELDS:
        raise ValueError(f"{label} fields must be exactly enabled and grading_ratio")
    if type(value["enabled"]) is not bool:
        raise ValueError(f"{label}.enabled must be a JSON boolean")
    ratio = value["grading_ratio"]
    if (isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(float(ratio))
            or not GRADING_RATIO_MINIMUM <= float(ratio) <= GRADING_RATIO_MAXIMUM):
        raise ValueError(
            f"{label}.grading_ratio must be a finite number in "
            f"[{GRADING_RATIO_MINIMUM:g}, {GRADING_RATIO_MAXIMUM:g}] (unit m/m: size increase per metre of distance)"
        )
    return {"enabled": value["enabled"], "grading_ratio": float(ratio)}


def boundary_local_sizes(
    rings: Iterable[Sequence[Point]],
    open_paths: Iterable[Sequence[Point]],
    *,
    default_size_m: float,
    minimum_size_m: float,
) -> dict[Point, float]:
    """Return each configured vertex's clamped shortest-adjacent-segment size, m."""
    sizes: dict[Point, float] = {}

    def visit(points: Sequence[Point], *, cyclic: bool) -> None:
        count = len(points)
        for index, point in enumerate(points):
            neighbours = []
            if cyclic or index > 0:
                neighbours.append(points[index - 1])
            if cyclic or index < count - 1:
                neighbours.append(points[(index + 1) % count])
            local = min([default_size_m, *(math.hypot(point[0] - other[0], point[1] - other[1]) for other in neighbours)])
            local = max(minimum_size_m, local)
            sizes[point] = min(sizes.get(point, default_size_m), local)

    for ring in rings:
        visit([(float(p[0]), float(p[1])) for p in ring], cyclic=True)
    for path in open_paths:
        visit([(float(p[0]), float(p[1])) for p in path], cyclic=False)
    return sizes


def size_bins(local_sizes: Mapping[Point, float], *, default_size_m: float, minimum_size_m: float) -> dict[float, list[Point]]:
    """Group vertices whose local size is below default_size_m into power-of-two bins."""
    bins: dict[float, list[Point]] = {}
    for point, size in local_sizes.items():
        if size >= default_size_m:
            continue
        bin_size = max(minimum_size_m, 2.0 ** math.floor(math.log2(size)))
        bins.setdefault(bin_size, []).append(point)
    return {key: bins[key] for key in sorted(bins)}


def threshold_dist_max_m(bin_size_m: float, *, default_size_m: float, grading_ratio: float) -> float:
    return (default_size_m - bin_size_m) / grading_ratio


__all__ = [
    "BOUNDARY_ADAPTIVE_SIZING_FIELDS", "BOUNDARY_ADAPTIVE_SIZING_RECEIPT_FORMAT", "GRADING_RATIO_MAXIMUM",
    "GRADING_RATIO_MINIMUM", "boundary_local_sizes", "size_bins", "threshold_dist_max_m",
    "validate_boundary_adaptive_sizing_shape",
]
