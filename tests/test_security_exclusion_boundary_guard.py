from dataclasses import replace

import pytest
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from hydrodynamics.mesh_preparation import GmshLineGroup, GmshMeshConfig
from hydrodynamics.security_exclusion import (
    SecurityDomainAssessment,
    SecurityExclusionError,
    gmsh_config_for_active_domain,
)


LARGE_D = [
    (500107.82673730346, 4320001.409697845),
    (500014.72684347664, 4320007.906189589),
    (499892.0164162751, 4320005.875615648),
    (499940.12702815374, 4319994.328496062),
    (500045.30297479103, 4319990.480000857),
]
LARGE_S = [
    (499999.0, 4319999.0),
    (500001.0, 4319999.0),
    (500001.0, 4320001.0),
    (499999.0, 4320001.0),
]
SQUARE_D = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


def _assessment(domain: Polygon, security: Polygon) -> SecurityDomainAssessment:
    inside = security.intersection(domain)
    outside = security.difference(domain)
    active = domain.difference(security)
    return SecurityDomainAssessment(
        domain_geometry=domain,
        source_union_geometry=security,
        security_inside_geometry=inside,
        security_outside_geometry=outside,
        active_geometry=active,
        original_domain_area_m2=float(domain.area),
        security_union_area_m2=float(security.area),
        security_inside_area_m2=float(inside.area),
        security_outside_area_m2=float(outside.area),
        active_domain_area_m2=float(active.area),
    )


def _config(
    outer: list[tuple[float, float]],
    boundary_group: GmshLineGroup,
    *,
    constraints: list[GmshLineGroup] | None = None,
) -> GmshMeshConfig:
    return GmshMeshConfig(
        type="gmsh",
        outer_boundary=outer,
        holes=[],
        boundary_groups=[boundary_group],
        constraints=constraints or [],
        default_size_m=10.0,
        minimum_size_m=1.0,
    )


def _closed_line(points: list[tuple[float, float]]) -> LineString:
    coords = list(points)
    if coords[0] == coords[-1]:
        coords.pop()
    return LineString([*coords, coords[0]])


@pytest.mark.parametrize("explicit_closure", [False, True])
def test_exact_large_coordinate_closed_exterior_passes_when_tiny_buffer_fails(explicit_closure: bool) -> None:
    domain = Polygon(LARGE_D)
    security = Polygon(LARGE_S)
    assessment = _assessment(domain, security)
    group_points = [*LARGE_D, LARGE_D[0]] if explicit_closure else list(LARGE_D)
    group = GmshLineGroup(name="D_exterior_geometry", points=group_points, closed=True)
    config = _config(list(LARGE_D), group)
    full_line = _closed_line(group_points)
    active_boundary = assessment.active_geometry.boundary

    assert full_line.difference(active_boundary).is_empty
    assert full_line.difference(active_boundary.buffer(1e-8)).length > 1.0

    adapted = gmsh_config_for_active_domain(config, assessment, tolerance_m=1e-8)

    assert adapted.boundary_groups[0].points == group.points
    assert adapted.boundary_groups[0].closed is True


def test_local_translation_remains_a_diagnostic_control() -> None:
    dx, dy = 500000.0, 4320000.0
    domain_points = [(x - dx, y - dy) for x, y in LARGE_D]
    safe_points = [(x - dx, y - dy) for x, y in LARGE_S]
    assessment = _assessment(Polygon(domain_points), Polygon(safe_points))
    group = GmshLineGroup(name="D_exterior_geometry", points=domain_points, closed=True)
    config = _config(domain_points, group)
    line = _closed_line(domain_points)

    assert line.difference(assessment.active_geometry.boundary.buffer(1e-8)).length == pytest.approx(0.0)
    adapted = gmsh_config_for_active_domain(config, assessment, tolerance_m=1e-8)
    assert adapted.boundary_groups[0].points == group.points


@pytest.mark.parametrize(("offset", "accepted"), [(5e-9, True), (2e-8, False)])
def test_nonexact_open_boundary_keeps_original_tolerance_fallback(offset: float, accepted: bool) -> None:
    domain = Polygon(SQUARE_D)
    security = Polygon([(4, 4), (6, 4), (6, 6), (4, 6)])
    assessment = _assessment(domain, security)
    group = GmshLineGroup(name="near_outer_boundary", points=[(1, offset), (9, offset)], closed=False)
    config = _config(SQUARE_D, group)

    assert not LineString(group.points).difference(assessment.active_geometry.boundary).is_empty
    if accepted:
        adapted = gmsh_config_for_active_domain(config, assessment, tolerance_m=1e-8)
        assert adapted.boundary_groups[0].points == group.points
        assert adapted.boundary_groups[0].closed is False
    else:
        with pytest.raises(SecurityExclusionError, match="UNSUPPORTED_SECURITY_BOUNDARY_GROUP"):
            gmsh_config_for_active_domain(config, assessment, tolerance_m=1e-8)


@pytest.mark.parametrize("explicit_closure", [False, True])
def test_closed_group_rejects_security_cut_on_implicit_or_explicit_closing_edge(
    explicit_closure: bool,
) -> None:
    domain = Polygon(SQUARE_D)
    security = Polygon([(0, 4), (2, 4), (2, 6), (0, 6)])
    assessment = _assessment(domain, security)
    points = [*SQUARE_D, SQUARE_D[0]] if explicit_closure else list(SQUARE_D)
    group = GmshLineGroup(name="D_exterior_geometry", points=points, closed=True)
    config = _config(SQUARE_D, group)

    assert _closed_line(points).difference(assessment.active_geometry.boundary).length == pytest.approx(2.0)
    with pytest.raises(SecurityExclusionError, match="UNSUPPORTED_SECURITY_BOUNDARY_GROUP"):
        gmsh_config_for_active_domain(config, assessment, tolerance_m=1e-8)


def test_closed_group_rejects_security_cut_on_a_nonclosing_edge() -> None:
    domain = Polygon(SQUARE_D)
    security = Polygon([(4, 0), (6, 0), (6, 2), (4, 2)])
    assessment = _assessment(domain, security)
    group = GmshLineGroup(name="D_exterior_geometry", points=list(SQUARE_D), closed=True)
    config = _config(SQUARE_D, group)

    with pytest.raises(SecurityExclusionError, match="UNSUPPORTED_SECURITY_BOUNDARY_GROUP"):
        gmsh_config_for_active_domain(config, assessment, tolerance_m=1e-8)


def test_open_group_on_exact_active_boundary_remains_compatible() -> None:
    domain = Polygon(SQUARE_D)
    security = Polygon([(4, 4), (6, 4), (6, 6), (4, 6)])
    assessment = _assessment(domain, security)
    group = GmshLineGroup(name="open_outer_segment", points=[(1, 0), (9, 0)], closed=False)

    adapted = gmsh_config_for_active_domain(_config(SQUARE_D, group), assessment, tolerance_m=1e-8)

    assert adapted.boundary_groups[0].points == group.points
    assert adapted.boundary_groups[0].closed is False


def test_exact_boundary_match_does_not_construct_a_tolerance_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    domain = Polygon(SQUARE_D)
    security = Polygon([(4, 4), (6, 4), (6, 6), (4, 6)])
    assessment = _assessment(domain, security)
    group = GmshLineGroup(name="open_outer_segment", points=[(1, 0), (9, 0)], closed=False)
    config = _config(SQUARE_D, group)

    def unexpected_buffer(*args: object, **kwargs: object) -> None:
        raise AssertionError("exact boundary matches must not construct a tolerance buffer")

    monkeypatch.setattr(BaseGeometry, "buffer", unexpected_buffer)
    adapted = gmsh_config_for_active_domain(config, assessment, tolerance_m=1e-8)

    assert adapted.boundary_groups[0].points == group.points


def test_open_group_crossing_security_hole_is_still_rejected() -> None:
    domain = Polygon(SQUARE_D)
    security = Polygon([(4, 4), (6, 4), (6, 6), (4, 6)])
    assessment = _assessment(domain, security)
    group = GmshLineGroup(name="crosses_exclusion", points=[(3, 5), (7, 5)], closed=False)

    with pytest.raises(SecurityExclusionError, match="UNSUPPORTED_SECURITY_BOUNDARY_GROUP"):
        gmsh_config_for_active_domain(_config(SQUARE_D, group), assessment, tolerance_m=1e-8)


def test_exact_boundary_shortcut_does_not_bypass_active_type_or_constraint_checks() -> None:
    domain = Polygon(SQUARE_D)
    security = Polygon([(4, 4), (6, 4), (6, 6), (4, 6)])
    assessment = _assessment(domain, security)
    group = GmshLineGroup(name="exact_outer", points=[*SQUARE_D, SQUARE_D[0]], closed=True)
    crossing_constraint = GmshLineGroup(name="crosses_security", points=[(3, 5), (7, 5)], closed=False)
    config = _config(SQUARE_D, group, constraints=[crossing_constraint])

    with pytest.raises(SecurityExclusionError, match="UNSUPPORTED_CONSTRAINT_CROSSES_EXCLUSION"):
        gmsh_config_for_active_domain(config, assessment, tolerance_m=1e-8)

    multipart = replace(
        assessment,
        active_geometry=MultiPolygon([Polygon([(0, 0), (4, 0), (4, 4), (0, 4)])]),
    )
    with pytest.raises(SecurityExclusionError, match="UNSUPPORTED_SECURITY_DOMAIN_MULTICOMPONENT"):
        gmsh_config_for_active_domain(_config(SQUARE_D, group), multipart, tolerance_m=1e-8)
