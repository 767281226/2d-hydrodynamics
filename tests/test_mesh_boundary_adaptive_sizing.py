"""Opt-in boundary-adaptive Gmsh sizing: contract, rule and short-segment quality gold."""
from __future__ import annotations

import copy
import importlib.util
import json
import math
import os
import tempfile
from pathlib import Path

import jsonschema
import pytest

from hydrodynamics.domain_mesh_only import validate_domain_mesh_only_config_shape
from hydrodynamics.mesh_boundary_sizing import (
    boundary_local_sizes,
    size_bins,
    threshold_dist_max_m,
    validate_boundary_adaptive_sizing_shape,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "triangular_mesh"
PLAN = {
    "format": "triangle-mesh-algorithm-plan-v1", "schema_version": "1.0", "algorithm_2d": 5,
    "smoothing": 1, "algorithm_switch_on_failure": 0, "optimizer": "none", "iterations": 0,
    "force": False, "entity_scope": "active_surface",
}
# 500 m square whose top edge carries one 1 m segment between two collinear vertices.
SHORT_SEGMENT_SQUARE = [(0.0, 0.0), (500.0, 0.0), (500.0, 500.0), (251.0, 500.0), (250.0, 500.0), (0.0, 500.0)]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_v1_2_schema_and_example_are_strict_and_leave_v1_1_closed():
    schema = _read_json(EXAMPLES / "domain_mesh_only_config_v1_2.schema.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    example = _read_json(EXAMPLES / "domain_mesh_only_config_v1_2.example.json")
    jsonschema.Draft202012Validator(schema).validate(example)
    assert validate_domain_mesh_only_config_shape(example) is example
    sizing_schema = schema["$defs"]["boundaryAdaptiveSizing"]
    assert sizing_schema["required"] == ["enabled", "grading_ratio"]
    assert "default" not in sizing_schema["properties"]["grading_ratio"]
    assert "boundary_adaptive_sizing" in schema["$defs"]["meshGeneration"]["required"]

    missing = copy.deepcopy(example)
    missing["mesh_generation"].pop("boundary_adaptive_sizing")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(missing)
    with pytest.raises(ValueError, match="requires mesh_generation.boundary_adaptive_sizing"):
        validate_domain_mesh_only_config_shape(missing)

    as_v1_1 = copy.deepcopy(example)
    as_v1_1["schema_version"] = "1.1"
    with pytest.raises(ValueError, match="fields differ"):
        validate_domain_mesh_only_config_shape(as_v1_1)
    v1_1_schema = _read_json(EXAMPLES / "domain_mesh_only_config_v1_1.schema.json")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(v1_1_schema).validate(as_v1_1)

    no_plan = copy.deepcopy(example)
    no_plan.pop("mesh_algorithm_plan")
    with pytest.raises(ValueError, match="fields differ"):
        validate_domain_mesh_only_config_shape(no_plan)


@pytest.mark.parametrize(
    "value",
    [
        {"enabled": True},
        {"enabled": True, "grading_ratio": 0.3, "extra": 1},
        {"enabled": 1, "grading_ratio": 0.3},
        {"enabled": "true", "grading_ratio": 0.3},
        {"enabled": True, "grading_ratio": True},
        {"enabled": True, "grading_ratio": "0.3"},
        {"enabled": True, "grading_ratio": 0.0},
        {"enabled": True, "grading_ratio": 0.049},
        {"enabled": True, "grading_ratio": 1.01},
        {"enabled": True, "grading_ratio": math.nan},
        {"enabled": True, "grading_ratio": math.inf},
        None,
    ],
)
def test_sizing_loader_rejects_bad_shapes_types_and_ranges(value):
    with pytest.raises(ValueError, match="boundary_adaptive_sizing"):
        validate_boundary_adaptive_sizing_shape(value)
    example = _read_json(EXAMPLES / "domain_mesh_only_config_v1_2.example.json")
    example["mesh_generation"]["boundary_adaptive_sizing"] = value
    with pytest.raises(ValueError, match="mesh_generation.boundary_adaptive_sizing"):
        validate_domain_mesh_only_config_shape(example)


@pytest.mark.parametrize("ratio", [0.05, 0.3, 1, 1.0])
def test_sizing_loader_accepts_range_endpoints(ratio):
    assert validate_boundary_adaptive_sizing_shape({"enabled": False, "grading_ratio": ratio}) == {
        "enabled": False, "grading_ratio": float(ratio)}


def test_local_size_rule_is_cyclic_clamped_and_binned_by_power_of_two():
    sizes = boundary_local_sizes([SHORT_SEGMENT_SQUARE], [[(100.0, 100.0), (100.0, 103.0)]],
                                 default_size_m=50.0, minimum_size_m=0.5)
    assert sizes[(251.0, 500.0)] == sizes[(250.0, 500.0)] == 1.0
    # The closing segment (0,500)->(0,0) is 500 m, so cyclic neighbours never under-refine.
    assert sizes[(0.0, 0.0)] == sizes[(0.0, 500.0)] == 50.0
    assert sizes[(100.0, 100.0)] == sizes[(100.0, 103.0)] == 3.0
    clamped = boundary_local_sizes([[(0.0, 0.0), (0.2, 0.0), (0.2, 10.0)]], [],
                                   default_size_m=50.0, minimum_size_m=0.5)
    assert clamped[(0.0, 0.0)] == 0.5
    bins = size_bins(sizes, default_size_m=50.0, minimum_size_m=0.5)
    assert list(bins) == [1.0, 2.0]
    assert sorted(bins[1.0]) == [(250.0, 500.0), (251.0, 500.0)]
    assert sorted(bins[2.0]) == [(100.0, 100.0), (100.0, 103.0)]
    assert threshold_dist_max_m(1.0, default_size_m=50.0, grading_ratio=0.25) == 196.0


def _gmsh_or_skip():
    if importlib.util.find_spec("gmsh") is None:
        pytest.skip("optional gmsh package is not installed")
    from hydrodynamics.mesh_generation import _gmsh_native_library_path
    if _gmsh_native_library_path() is None:
        pytest.skip("Gmsh native SDK library is unavailable")


_ISOLATION_ENV = "HYDRO_TEST_IN_PROCESS_GMSH_CHILD"


def _run_in_child_process(request) -> bool:
    """Run in-process Gmsh tests in a child pytest so the parent never imports gmsh.

    Other suites assert that the supervisor process has not imported the Gmsh
    binding before launching its Job; an in-process import here would leak.
    Returns True when the caller is the child and should execute the body.
    """
    if os.environ.get(_ISOLATION_ENV) == "1":
        return True
    import subprocess
    import sys
    env = {**os.environ, _ISOLATION_ENV: "1"}
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", request.node.nodeid],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]
    return False


def _generate(sizing):
    from hydrodynamics.mesh_generation import generate_gmsh_mesh
    from hydrodynamics.mesh_preparation import GmshMeshConfig
    config = GmshMeshConfig.model_validate({
        "type": "gmsh", "outer_boundary": SHORT_SEGMENT_SQUARE, "default_size_m": 50.0, "minimum_size_m": 0.5,
    })
    receipt: dict = {}
    mesh = generate_gmsh_mesh(config, coordinate_system="EPSG:4548", mesh_algorithm_plan=PLAN,
                              generation_receipt=receipt, boundary_adaptive_sizing=sizing)
    return mesh, receipt


def test_short_boundary_segment_fails_quality_without_sizing_and_passes_with_it(request):
    _gmsh_or_skip()
    if not _run_in_child_process(request):
        return
    baseline, baseline_receipt = _generate(None)
    disabled, disabled_receipt = _generate({"enabled": False, "grading_ratio": 0.3})
    adaptive, adaptive_receipt = _generate({"enabled": True, "grading_ratio": 0.3})

    assert min(baseline.face_quality) < 0.1
    assert baseline.quality_report(minimum_quality=0.1)["faces_below_quality_threshold"]
    assert "boundary_adaptive_sizing" not in baseline_receipt
    # Disabled sizing is generation-identical to the previous behavior.
    assert disabled.node_xy == baseline.node_xy and disabled.face_nodes == baseline.face_nodes
    assert disabled_receipt["boundary_adaptive_sizing"]["enabled"] is False
    assert "bins" not in disabled_receipt["boundary_adaptive_sizing"]

    assert min(adaptive.face_quality) >= 0.1
    assert adaptive.quality_report(minimum_quality=0.1)["faces_below_quality_threshold"] == []
    assert math.isclose(math.fsum(adaptive.face_area), 250000.0, rel_tol=1e-12)
    node_set = set(adaptive.node_xy)
    assert all(point in node_set for point in SHORT_SEGMENT_SQUARE)
    record = adaptive_receipt["boundary_adaptive_sizing"]
    assert record["format"] == "gmsh-boundary-adaptive-sizing-execution-v1"
    assert record["grading_ratio"] == 0.3 and record["threshold_field_count"] == 1
    assert record["bins"] == [{"bin_floor_m": 1.0, "size_min_m": 1.0, "vertex_count": 2, "dist_min_m": 0.0,
                               "dist_max_m": pytest.approx(49.0 / 0.3)}]
    assert adaptive_receipt["sdk_options_before_generate"]["Mesh.MeshSizeExtendFromBoundary"] == 0.0
    assert adaptive_receipt["fixed_boundary_guard"]["status"] == "passed"


def test_invalid_sizing_is_rejected_before_gmsh():
    from hydrodynamics.mesh_generation import generate_gmsh_mesh
    from hydrodynamics.mesh_preparation import GmshMeshConfig, MeshPreparationError
    config = GmshMeshConfig.model_validate({"type": "gmsh", "outer_boundary": SHORT_SEGMENT_SQUARE, "default_size_m": 50.0})
    with pytest.raises(MeshPreparationError, match="invalid boundary_adaptive_sizing"):
        generate_gmsh_mesh(config, coordinate_system="EPSG:4548",
                           boundary_adaptive_sizing={"enabled": True, "grading_ratio": 2.0})


@pytest.mark.skipif(os.name != "nt", reason="domain mesh-only route requires Windows Job Objects")
def test_domain_route_short_segment_v1_1_fails_v1_2_sizing_and_simplification_pass():
    _gmsh_or_skip()
    from hydrodynamics.restricted_worker import run_restricted_preparation
    runner_spec = importlib.util.spec_from_file_location(
        "domain_mesh_only_synthetic_sizing", EXAMPLES / "run_domain_mesh_only_synthetic.py")
    runner = importlib.util.module_from_spec(runner_spec)
    runner_spec.loader.exec_module(runner)
    # Insert a 1 m collinear segment into the synthetic 100 m D exterior.
    outer = [list(point) for point in runner.OUTER]
    outer.insert(3, [outer[3][0] + 1.0, outer[3][1]])
    sizing = {"enabled": True, "grading_ratio": 0.3}
    cases = (
        ("v1-1", None, None),
        ("v1-2-size", sizing, {"enabled": False, "minimum_vertex_spacing_m": 5.0}),
        ("v1-2-both", sizing, {"enabled": True, "minimum_vertex_spacing_m": 5.0}),
    )
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mesh-sizing-test-", dir=work) as temp:
        experiment = Path(temp).relative_to(ROOT)
        results = {}
        for label, size_option, simplify_option in cases:
            config_path, config, _manifest_path, _manifest = runner.create_synthetic_domain_mesh_case(
                ROOT, experiment / label, run_id=f"short-segment-{label}", d_polygons=[[outer]],
                derived_domain_exterior=True, include_constraint=False, mesh_algorithm_plan=PLAN,
                boundary_adaptive_sizing=size_option, boundary_simplification=simplify_option)
            results[label] = (run_restricted_preparation(config_path, project_root=ROOT), config)
        failed, _ = results["v1-1"]
        assert failed.status == "worker_failed" and failed.exit_code == 53
        assert "minimum_face_quality" in failed.message
        bundles = {}
        for label in ("v1-2-size", "v1-2-both"):
            passed, config = results[label]
            assert config["schema_version"] == "1.2"
            assert passed.status == "mesh_generation_diagnostic_complete", passed
            bundle = passed.bundle_path
            assert (bundle / "MESH_READY").is_file()
            worker = _read_json(bundle / "artifacts" / "worker-result.json")
            audit = _read_json(bundle / "artifacts" / "domain-source-audit.json")
            mesh = _read_json(bundle / "artifacts" / "domain-mesh.json")
            assert worker["quality_report"]["faces_below_quality_threshold"] == []
            assert audit["boundary_adaptive_sizing"] == sizing
            bundles[label] = (worker, audit, mesh)
        worker, audit, mesh = bundles["v1-2-size"]
        assert worker["mesh_algorithm_execution"]["boundary_adaptive_sizing"]["bins"][0]["size_min_m"] == 1.0
        assert audit["boundary_simplification"] == {"enabled": False, "minimum_vertex_spacing_m": 5.0}
        short_vertex = outer[3]
        assert short_vertex in mesh["node_xy"]

        worker, audit, mesh = bundles["v1-2-both"]
        record = audit["boundary_simplification"]
        assert record["enabled"] is True and record["minimum_vertex_spacing_m"] == 5.0
        exterior = next(ring for ring in record["rings"] if ring["role"] == "domain_D_exterior")
        assert (exterior["vertices_before"], exterior["vertices_after"]) == (5, 4)
        assert exterior["segments_below_spacing_after"] == 0
        assert record["maximum_hausdorff_offset_m"] == pytest.approx(0.0, abs=1e-9)
        assert record["simplified_active_inside_source_S_m2"] == 0.0
        assert record["active_area_change_m2"] == pytest.approx(0.0, abs=1e-6)
        assert short_vertex not in mesh["node_xy"]
        assert audit["security_mesh_audit"]["security_boundary_edge_count"] > 0
        # The 1 m segment is gone: sizing grades no lower than the shortest kept segment (10 m S edges).
        bins = worker["mesh_algorithm_execution"]["boundary_adaptive_sizing"]["bins"]
        assert min(item["size_min_m"] for item in bins) >= 5.0


def test_closed_wall_audit_matches_oblique_edges_at_real_projected_coordinates(request):
    """Regression: nanometre buffers of multi-segment walls collapse at ~4e6 m coordinates."""
    _gmsh_or_skip()
    if not _run_in_child_process(request):
        return
    from types import SimpleNamespace
    from shapely.geometry import Polygon
    from hydrodynamics.mesh_generation import generate_gmsh_mesh
    from hydrodynamics.mesh_preparation import GmshMeshConfig
    from hydrodynamics.security_exclusion import assess_security_domain, check_mesh_against_security

    def rotated(points, origin=(473500.123, 4335900.456), angle=0.37):
        c, s = math.cos(angle), math.sin(angle)
        return [(origin[0] + x * c - y * s, origin[1] + x * s + y * c) for x, y in points]

    outer = rotated([(0.0, 0.0), (400.0, 0.0), (400.0, 300.0), (0.0, 300.0)])
    hole = rotated([(150.0, 100.0), (260.0, 110.0), (240.0, 200.0), (160.0, 190.0)])
    assessment = assess_security_domain(Polygon(outer), SimpleNamespace(source_union=Polygon(hole)))
    config = GmshMeshConfig.model_validate({
        "type": "gmsh", "outer_boundary": list(assessment.active_geometry.exterior.coords)[:-1],
        "holes": [list(assessment.active_geometry.interiors[0].coords)[:-1]], "default_size_m": 25.0,
    })
    mesh = generate_gmsh_mesh(config, coordinate_system="EPSG:4548")
    check = check_mesh_against_security(mesh, assessment, tolerance_m=1e-8)
    assert len(check.security_boundary_edge_indices) == len(mesh.boundary_groups["hole_0"])
    assert "security_exclusion_boundary" in check.mesh.boundary_groups
