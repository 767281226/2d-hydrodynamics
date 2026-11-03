"""Opt-in D/S boundary simplification: contract, S containment and validity guards."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from hydrodynamics.boundary_simplification import (
    BoundarySimplificationError,
    simplify_domain_and_security,
    validate_boundary_simplification_shape,
)
from hydrodynamics.domain_mesh_only import validate_domain_mesh_only_config_shape


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "triangular_mesh"


def _example() -> dict:
    return json.loads((EXAMPLES / "domain_mesh_only_config_v1_2.example.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"enabled": True},
        {"enabled": True, "minimum_vertex_spacing_m": 5.0, "extra": 1},
        {"enabled": 1, "minimum_vertex_spacing_m": 5.0},
        {"enabled": True, "minimum_vertex_spacing_m": True},
        {"enabled": True, "minimum_vertex_spacing_m": "5"},
        {"enabled": True, "minimum_vertex_spacing_m": 0.0},
        {"enabled": True, "minimum_vertex_spacing_m": -1.0},
        {"enabled": True, "minimum_vertex_spacing_m": 50.01},
        {"enabled": True, "minimum_vertex_spacing_m": math.nan},
    ],
)
def test_simplification_loader_rejects_bad_shapes_types_and_ranges(value):
    with pytest.raises(ValueError, match="boundary_simplification"):
        validate_boundary_simplification_shape(value)
    config = _example()
    config["mesh_generation"]["boundary_simplification"] = value
    with pytest.raises(ValueError, match="mesh_generation.boundary_simplification"):
        validate_domain_mesh_only_config_shape(config)


def test_simplification_is_required_in_v1_2_and_must_exceed_geometry_tolerance():
    config = _example()
    assert validate_domain_mesh_only_config_shape(config) is config
    assert validate_boundary_simplification_shape({"enabled": False, "minimum_vertex_spacing_m": 50}) == {
        "enabled": False, "minimum_vertex_spacing_m": 50.0}
    missing = copy.deepcopy(config)
    missing["mesh_generation"].pop("boundary_simplification")
    with pytest.raises(ValueError, match="boundary_simplification"):
        validate_domain_mesh_only_config_shape(missing)
    tiny = copy.deepcopy(config)
    tiny["mesh_generation"]["boundary_simplification"]["minimum_vertex_spacing_m"] = 1e-9
    with pytest.raises(ValueError, match="must exceed geometry_tolerance_m"):
        validate_domain_mesh_only_config_shape(tiny)


def _segments(ring):
    return [math.dist(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring))]


def test_short_d_edges_drop_and_convex_short_s_edge_merges_outward():
    # D: 200 m square with a jagged top edge (1 m and 2 m steps).
    domain = Polygon([(0, 0), (200, 0), (200, 200), (120, 200), (119, 200.4), (117, 200.2), (0, 200)])
    # S: 40 m square with one 1 m chamfer at its north-east corner (convex short edge).
    security = Polygon([(80, 80), (120, 80), (120, 119.3), (119.3, 120), (80, 120)])
    result = simplify_domain_and_security(
        domain, security, minimum_vertex_spacing_m=5.0, geometry_tolerance_m=1e-8, area_tolerance_m2=1e-6)
    audit = result.audit
    assert all(length >= 5.0 for length in _segments(result.exterior))
    assert all(length >= 5.0 for ring in result.security_rings for length in _segments(ring))
    assert audit["collapse_counts"]["merge_outward"] == 1
    # The outward corner merge restores the S corner exactly; S stays fully excluded.
    assert (120.0, 120.0) in result.security_rings[0]
    assert result.security_geometry.contains(security)
    assert audit["simplified_active_inside_source_S_m2"] == 0.0
    assert audit["source_S_outside_simplified_S_m2"] == 0.0
    assert 0.0 < audit["maximum_hausdorff_offset_m"] <= 5.0
    rings = {ring["role"]: ring for ring in audit["rings"]}
    assert rings["domain_D_exterior"]["vertices_before"] == 7
    assert rings["domain_D_exterior"]["segments_below_spacing_after"] == 0
    assert rings["security_S_part_0"]["new_vertex_count"] == 1
    assert audit["active_area_change_m2"] == pytest.approx(
        result.domain_geometry.difference(result.security_geometry).area - domain.difference(security).area)
    assert result.domain_geometry.is_valid and result.security_geometry.within(result.domain_geometry)


def test_reflex_s_vertex_drop_grows_s_and_unsafe_s_edges_are_kept():
    domain = Polygon([(0, 0), (300, 0), (300, 300), (0, 300)])
    # A 1 m notch into S makes reflex vertices that may be dropped (S grows).
    notched = Polygon([(100, 100), (150, 100), (150.5, 101), (151, 100), (200, 100), (200, 200), (100, 200)])
    result = simplify_domain_and_security(
        domain, notched, minimum_vertex_spacing_m=5.0, geometry_tolerance_m=1e-8, area_tolerance_m2=1e-6)
    assert result.security_geometry.contains(notched)
    assert all(length >= 5.0 for length in _segments(result.security_rings[0]))
    # A 1 m S rectangle cannot be simplified without shrinking S; it is kept unchanged.
    sliver = Polygon([(100, 100), (200, 100), (200, 101), (100, 101)])
    kept = simplify_domain_and_security(
        domain, sliver, minimum_vertex_spacing_m=5.0, geometry_tolerance_m=1e-8, area_tolerance_m2=1e-6)
    assert sorted(kept.security_rings[0]) == sorted(tuple(map(float, p)) for p in sliver.exterior.coords[:-1])
    ring = next(item for item in kept.audit["rings"] if item["role"] == "security_S_part_0")
    assert ring["segments_below_spacing_after"] == 2 and ring["vertices_after"] == 4


def test_collapse_that_would_cut_off_s_is_rejected_and_edge_kept():
    domain = Polygon([(0, 1), (1, 0), (100, 0), (100, 100), (0, 100)])
    security = Polygon([(0.6, 0.6), (0.7, 0.6), (0.7, 0.7), (0.6, 0.7)])
    result = simplify_domain_and_security(
        domain, security, minimum_vertex_spacing_m=5.0, geometry_tolerance_m=1e-8, area_tolerance_m2=1e-9)
    assert (0.0, 1.0) in result.exterior and (1.0, 0.0) in result.exterior
    assert result.security_geometry.within(result.domain_geometry)
    exterior = next(item for item in result.audit["rings"] if item["role"] == "domain_D_exterior")
    assert exterior["segments_below_spacing_after"] == 1


def test_protected_points_are_kept_and_unsupported_topology_is_rejected():
    domain = Polygon([(0, 0), (100, 0), (100, 100), (51, 100), (50, 100), (0, 100)])
    security = Polygon([(40, 40), (60, 40), (60, 60), (40, 60)])
    result = simplify_domain_and_security(
        domain, security, minimum_vertex_spacing_m=5.0, geometry_tolerance_m=1e-8, area_tolerance_m2=1e-6,
        protected_points=[(51.0, 100.0), (50.0, 100.0)])
    assert (51.0, 100.0) in result.exterior and (50.0, 100.0) in result.exterior
    touching = Polygon([(40, 0), (60, 0), (60, 20), (40, 20)])
    with pytest.raises(BoundarySimplificationError, match="UNSUPPORTED_SIMPLIFICATION_TOPOLOGY"):
        simplify_domain_and_security(domain, touching, minimum_vertex_spacing_m=5.0,
                                     geometry_tolerance_m=1e-8, area_tolerance_m2=1e-6)
