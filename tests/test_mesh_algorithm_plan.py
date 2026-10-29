"""Strict plan, version compatibility, boundary guard and analytic topology gold."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import jsonschema
import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from hydrodynamics.domain_mesh_only import validate_domain_mesh_only_config_shape
from hydrodynamics.mesh_algorithm_plan import validate_mesh_algorithm_plan_shape
from hydrodynamics.mesh_generation import _fixed_boundary_snapshot_unchanged
from hydrodynamics.triangular_mesh import TriangularMesh


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "triangular_mesh"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _triangle_quality(points: list[tuple[float, float]], face: tuple[int, int, int]) -> float:
    a, b, c = (points[index] for index in face)
    area = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2.0
    edges = (math.dist(a, b), math.dist(b, c), math.dist(c, a))
    return 4.0 * math.sqrt(3.0) * area / sum(length * length for length in edges)


def test_plan_and_v1_1_schemas_are_strict_and_valid():
    plan_schema = _read_json(EXAMPLES / "mesh_algorithm_plan_v1.schema.json")
    config_v1 = _read_json(EXAMPLES / "domain_mesh_only_config.schema.json")
    config_v1_1 = _read_json(EXAMPLES / "domain_mesh_only_config_v1_1.schema.json")
    diagnostic_v1 = _read_json(ROOT / "hydrodynamics/schemas/domain_mesh_quality_failure_diagnostic_v1.schema.json")
    diagnostic_v2 = _read_json(ROOT / "hydrodynamics/schemas/domain_mesh_quality_failure_diagnostic_v2.schema.json")

    for schema in (plan_schema, config_v1, config_v1_1, diagnostic_v1, diagnostic_v2):
        jsonschema.Draft202012Validator.check_schema(schema)
    assert plan_schema["properties"]["algorithm_2d"]["enum"] == [5, 6]
    assert "default" not in plan_schema["properties"]["algorithm_2d"]
    assert "mesh_algorithm_plan" not in config_v1["required"]
    assert "mesh_algorithm_plan" in config_v1_1["required"]
    assert diagnostic_v1["properties"]["format"]["const"] == "triangle-mesh-quality-failure-diagnostic-v1"
    assert diagnostic_v2["properties"]["format"]["const"] == "triangle-mesh-quality-failure-diagnostic-v2"
    v1_path = ROOT / "hydrodynamics/schemas/domain_mesh_quality_failure_diagnostic_v1.schema.json"
    assert hashlib.sha256(v1_path.read_bytes()).hexdigest() == "2a87b59d9e4603930d5ceca1658429d645e8a298a46d420d769b8a2344ea8ee8"
    node_provenance_ref = diagnostic_v2["properties"]["sampled_node_provenance"]["$ref"]
    node_provenance = diagnostic_v2["$defs"][node_provenance_ref.rsplit("/", 1)[1]]
    assert node_provenance["properties"]["node_limit"]["const"] == 48
    assert node_provenance["properties"]["nodes"]["maxItems"] == 48
    assert diagnostic_v2["$defs"]["worstFace"]["properties"]["gmsh_node_tags_decimal"]["maxItems"] == 3
    assert diagnostic_v2["properties"]["worst_face_selection"]["properties"]["faces"]["maxItems"] == 16


def test_example_plan_and_v1_1_config_validate_without_changing_v1():
    plan_schema = _read_json(EXAMPLES / "mesh_algorithm_plan_v1.schema.json")
    plans = [
        _read_json(EXAMPLES / "mesh_algorithm_plan_v1.example.json"),
        _read_json(EXAMPLES / "mesh_algorithm_plan_v1_delaunay.example.json"),
        _read_json(EXAMPLES / "mesh_algorithm_plan_v1_relocate2d.example.json"),
    ]
    plan = plans[0]
    config = _read_json(EXAMPLES / "domain_mesh_only_config.example.json")
    config_v1_schema = _read_json(EXAMPLES / "domain_mesh_only_config.schema.json")
    config_v1_1_schema = _read_json(EXAMPLES / "domain_mesh_only_config_v1_1.schema.json")

    for sample_plan in plans:
        jsonschema.Draft202012Validator(plan_schema).validate(sample_plan)
        assert validate_mesh_algorithm_plan_shape(sample_plan) == sample_plan
    jsonschema.Draft202012Validator(config_v1_schema).validate(config)
    assert validate_domain_mesh_only_config_shape(config) == config

    config_v1_1 = copy.deepcopy(config)
    config_v1_1["schema_version"] = "1.1"
    config_v1_1["mesh_algorithm_plan"] = plan
    jsonschema.Draft202012Validator(config_v1_1_schema).validate(config_v1_1)
    assert validate_domain_mesh_only_config_shape(config_v1_1) == config_v1_1

    missing_plan = copy.deepcopy(config_v1_1)
    missing_plan.pop("mesh_algorithm_plan")
    with pytest.raises((jsonschema.ValidationError, ValueError)):
        jsonschema.Draft202012Validator(config_v1_1_schema).validate(missing_plan)
    with pytest.raises(ValueError, match="fields differ"):
        validate_domain_mesh_only_config_shape(missing_plan)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("algorithm_2d", True),
        ("algorithm_2d", 5.0),
        ("algorithm_2d", 3),
        ("smoothing", True),
        ("algorithm_switch_on_failure", 1),
        ("optimizer", "Laplace2D"),
        ("iterations", True),
        ("iterations", 0.0),
        ("force", True),
        ("entity_scope", "caller_entity_tags"),
    ],
)
def test_plan_loader_rejects_bad_types_values_and_entities(field, value):
    plan = _read_json(EXAMPLES / "mesh_algorithm_plan_v1.example.json")
    plan[field] = value
    with pytest.raises(ValueError):
        validate_mesh_algorithm_plan_shape(plan)


def test_plan_requires_exact_iterations_and_rejects_unknown_keys():
    plan = _read_json(EXAMPLES / "mesh_algorithm_plan_v1.example.json")
    plan["optimizer"] = "Relocate2D"
    with pytest.raises(ValueError, match="iterations"):
        validate_mesh_algorithm_plan_shape(plan)
    plan["iterations"] = 1
    assert validate_mesh_algorithm_plan_shape(plan)["iterations"] == 1
    plan["caller_dim_tags"] = [[2, 99]]
    with pytest.raises(ValueError, match="fields"):
        validate_mesh_algorithm_plan_shape(plan)


def test_fixed_boundary_guard_requires_exact_0d_1d_identity():
    before = {
        "sha256": "a" * 64, "node_count": 12, "element_count_0d_1d": 16,
        "entity_count_0d": 8, "entity_count_1d": 8,
        "physical_group_count": 0, "logical_boundary_group_count": 2,
        "logical_constraint_group_count": 1,
    }
    assert _fixed_boundary_snapshot_unchanged(before, copy.deepcopy(before))
    changed = {**before, "sha256": "b" * 64}
    assert not _fixed_boundary_snapshot_unchanged(before, changed)
    changed = {**before, "node_count": before["node_count"] + 1}
    assert not _fixed_boundary_snapshot_unchanged(before, changed)


def test_fixed_boundary_quad_topology_gold_preserves_domain_but_changes_quality():
    points = [(0.0, 0.0), (1.0, 0.08), (2.0, 0.0), (1.0, -1.0)]
    triangulation_a = [(0, 1, 2), (0, 2, 3)]
    triangulation_b = [(0, 1, 3), (1, 2, 3)]
    polygon_a = unary_union([Polygon([points[index] for index in face]) for face in triangulation_a])
    polygon_b = unary_union([Polygon([points[index] for index in face]) for face in triangulation_b])

    assert polygon_a.symmetric_difference(polygon_b).area == 0.0
    assert min(_triangle_quality(points, face) for face in triangulation_a) < 0.1
    assert min(_triangle_quality(points, face) for face in triangulation_b) > 0.1
    assert points == [(0.0, 0.0), (1.0, 0.08), (2.0, 0.0), (1.0, -1.0)]


def test_quality_threshold_neighbours_keep_strict_lt_semantics():
    height_at_threshold = 0.3 / (2.0 * math.sqrt(3.0) + math.sqrt(11.88))
    below = TriangularMesh(
        [(0.0, 0.0), (1.0, 0.0), (0.5, height_at_threshold * (1 - 1e-6))],
        [(0, 1, 2)], "EPSG:4548", minimum_face_area_m2=1e-12,
    )
    above = TriangularMesh(
        [(0.0, 0.0), (1.0, 0.0), (0.5, height_at_threshold * (1 + 1e-6))],
        [(0, 1, 2)], "EPSG:4548", minimum_face_area_m2=1e-12,
    )
    assert below.face_quality[0] < 0.1
    assert above.face_quality[0] > 0.1
    assert len(below.quality_report(minimum_quality=0.1)["faces_below_quality_threshold"]) == 1
    assert len(above.quality_report(minimum_quality=0.1)["faces_below_quality_threshold"]) == 0
