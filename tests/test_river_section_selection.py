"""Assumed river section selection: pure rule, synthetic DEM selection, validation, schemas, CLI."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from hydrodynamics.river_section_selection import (
    AUDIT_FORMAT,
    REQUEST_FORMAT,
    SECTION_STATUS,
    ChannelRuleParameters,
    RiverSectionSelectionError,
    RiverSectionsRequest,
    load_mesh_artifact,
    main,
    outer_boundary_loop,
    select_channel_run,
    select_river_sections,
)
from hydrodynamics.triangular_mesh import TriangularMesh

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "river_sections"
HAS_RASTERIO = importlib.util.find_spec("rasterio") is not None
HAS_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None
needs_rasterio = pytest.mark.skipif(not HAS_RASTERIO, reason="rasterio is required to write/read the synthetic GeoTIFF")

# Synthetic domain: 400 m x 100 m rectangle, 10 m regular triangulation (800 faces, 100 exterior edges).
NX, NY, DX = 40, 10, 10.0
CHANNEL = (150.0, 210.0)   # notch across the south boundary (y = 0): 6 edges, 60 m
BUMP = (170.0, 180.0)      # one-edge above-threshold bump inside the notch
NODATA_BAND = (300.0, 320.0)


def _rectangle_mesh(crs: str = "EPSG:4548") -> TriangularMesh:
    nodes = [(i * DX, j * DX) for j in range(NY + 1) for i in range(NX + 1)]
    faces = []
    for j in range(NY):
        for i in range(NX):
            a = j * (NX + 1) + i
            b, c, d = a + 1, a + NX + 2, a + NX + 1
            faces.extend([(a, b, c), (a, c, d)])
    return TriangularMesh(nodes, faces, crs, mesh_id="synthetic-rect")


def _write_dem(path: Path, *, bump: bool = False, nodata_band: bool = False) -> Path:
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    x0, x1, y0, y1 = -20.0, 420.0, -20.0, 120.0
    width, height = int(x1 - x0), int(y1 - y0)
    xs = x0 + np.arange(width) + 0.5
    ys = y1 - np.arange(height) - 0.5
    X, Y = np.meshgrid(xs, ys)
    z = np.full(X.shape, 5.0, dtype=np.float32)
    z[(X >= CHANNEL[0]) & (X < CHANNEL[1])] = 1.0
    if bump:
        z[(X >= BUMP[0]) & (X < BUMP[1])] = 5.0
    if nodata_band:
        z[(X >= NODATA_BAND[0]) & (X < NODATA_BAND[1])] = -9999.0
    with rasterio.open(path, "w", driver="GTiff", height=height, width=width, count=1, dtype="float32",
                       crs="EPSG:4548", transform=from_origin(x0, y1, 1.0, 1.0), nodata=-9999.0) as dst:
        dst.write(z, 1)
    return path


def _request(**overrides) -> dict:
    payload = {
        "format": REQUEST_FORMAT,
        "crs": "EPSG:4548",
        "rule": {"window_m": 200.0, "thalweg_search_m": 50.0},
        "sections": [{"name": "S", "role": "inflow", "xy": [180.0, -5.0],
                      "discharge_series_column": "inflow_m3s", "name_zh": "测试断面"}],
    }
    payload.update(overrides)
    return payload


def _south_edge_nodes(x_lo: float, x_hi: float) -> set[tuple[int, int]]:
    xs = np.arange(x_lo, x_hi + DX / 2, DX)
    return {(int(a / DX), int(b / DX)) for a, b in zip(xs[:-1], xs[1:])}


def _assert_exterior_contiguous(mesh: TriangularMesh, edge_indices: list[int]) -> None:
    for e in edge_indices:
        assert -1 in mesh.edge_faces[e], f"edge {e} is not exterior"
    for left, right in zip(edge_indices[:-1], edge_indices[1:]):
        assert set(mesh.edge_nodes[left]) & set(mesh.edge_nodes[right]), f"edges {left} and {right} do not touch"


# --------------------------------------------------------------------------- pure rule


def test_rule_on_hand_written_profile():
    rel = (np.arange(11) - 5) * 10.0
    length = np.full(11, 10.0)
    z = np.array([5, 5, 5, 2, 1, 2, 5, 5, 5, 5, 5], dtype=float)
    rule = ChannelRuleParameters(window_m=100.0, thalweg_search_m=30.0)
    run = select_channel_run(rel, length, z, rule)
    assert run.thalweg_index == 4 and run.thalweg_elevation_m == 1.0
    assert run.bank_level_m == 5.0 and run.threshold_m == 3.0
    assert run.run_indices.tolist() == [3, 4, 5]
    assert (run.lo, run.hi) == (3, 5)


def test_rule_gap_tolerance_merges_one_edge_bump():
    rel = (np.arange(11) - 5) * 10.0
    length = np.full(11, 10.0)
    z = np.array([5, 5, 2, 1, 5, 2, 2, 5, 5, 5, 5], dtype=float)
    merged = select_channel_run(rel, length, z, ChannelRuleParameters(window_m=100.0, thalweg_search_m=30.0, gap_tolerance_m=15.0))
    assert merged.run_indices.tolist() == [2, 3, 4, 5, 6]
    split = select_channel_run(rel, length, z, ChannelRuleParameters(window_m=100.0, thalweg_search_m=30.0, gap_tolerance_m=5.0))
    assert split.run_indices.tolist() == [2, 3]


def test_rule_nan_edges_never_thalweg_and_count_as_gap():
    rel = (np.arange(11) - 5) * 10.0
    length = np.full(11, 10.0)
    z = np.array([5, 5, 2, 1, np.nan, 2, 5, 5, 5, 5, 5], dtype=float)
    rule = ChannelRuleParameters(window_m=100.0, thalweg_search_m=30.0, gap_tolerance_m=15.0)
    run = select_channel_run(rel, length, z, rule)
    assert run.thalweg_index == 3
    assert run.run_indices.tolist() == [2, 3, 4, 5]
    with pytest.raises(RiverSectionSelectionError, match="thalweg_undefined"):
        select_channel_run(rel, length, np.where(np.abs(rel) < 30, np.nan, z), rule)


def test_rule_rejects_run_covering_whole_loop():
    rel = (np.arange(8) - 4) * 10.0
    with pytest.raises(RiverSectionSelectionError, match="run_covers_loop"):
        select_channel_run(rel, np.full(8, 10.0), np.zeros(8), ChannelRuleParameters(window_m=100.0, thalweg_search_m=10.0))


def test_rule_parameter_ranges():
    with pytest.raises(RiverSectionSelectionError, match="parameter_range"):
        ChannelRuleParameters(window_m=-1.0)
    with pytest.raises(RiverSectionSelectionError, match="parameter_range"):
        ChannelRuleParameters(bank_percentile=120)
    with pytest.raises(RiverSectionSelectionError, match="thalweg_search_m must not exceed"):
        ChannelRuleParameters(window_m=100.0, thalweg_search_m=200.0)
    with pytest.raises(RiverSectionSelectionError, match="parameter_type"):
        ChannelRuleParameters(dem_samples_per_edge=2.5)
    assert ChannelRuleParameters().sample_fractions == (0.1, 0.3, 0.5, 0.7, 0.9)
    assert ChannelRuleParameters().audit_record()["edge_elevation"] == "mean of 5 DEM samples at 0.1..0.9 of the edge"


# --------------------------------------------------------------------------- outer loop


def test_outer_loop_is_oriented_and_exterior():
    mesh = _rectangle_mesh()
    loop = outer_boundary_loop(mesh)
    assert loop.loop_lengths == (2 * (NX + NY),)
    assert loop.perimeter_m == pytest.approx(2 * (NX + NY) * DX)
    for e in loop.edge_indices:
        assert -1 in mesh.edge_faces[e]
    assert np.all(loop.oriented_nodes[1:, 0] == loop.oriented_nodes[:-1, 1])
    assert loop.oriented_nodes[-1, 1] == loop.oriented_nodes[0, 0]
    rel = loop.relative_arclength(0)
    assert rel[0] == 0.0 and rel[-1] == pytest.approx(-DX)
    assert np.abs(rel).max() <= loop.perimeter_m / 2


# --------------------------------------------------------------------------- request validation


def test_request_validation_errors():
    with pytest.raises(RiverSectionSelectionError, match="section_role"):
        RiverSectionsRequest.from_mapping(_request(sections=[{"name": "S", "role": "source", "xy": [1, 2], "discharge_series_column": "q", "name_zh": "x"}]))
    with pytest.raises(RiverSectionSelectionError, match="missing_field"):
        RiverSectionsRequest.from_mapping(_request(sections=[{"name": "S", "role": "inflow", "xy": [1, 2], "name_zh": "x"}]))
    with pytest.raises(RiverSectionSelectionError, match="parameter_range"):
        RiverSectionsRequest.from_mapping(_request(rule={"window_m": 10.0}))
    with pytest.raises(RiverSectionSelectionError, match="parameter_range"):
        RiverSectionsRequest.from_mapping(_request(max_point_distance_m=0))
    with pytest.raises(RiverSectionSelectionError, match="duplicate_section"):
        RiverSectionsRequest.from_mapping(_request(sections=_request()["sections"] * 2))
    with pytest.raises(RiverSectionSelectionError, match="unknown_field"):
        RiverSectionsRequest.from_mapping(_request(mesh="x"))
    with pytest.raises(RiverSectionSelectionError, match="format"):
        RiverSectionsRequest.from_mapping(_request(format="other"))
    request = RiverSectionsRequest.from_mapping(_request())
    assert request.max_point_distance_m == 100.0 and request.rule.gap_tolerance_m == 60.0


def test_crs_mismatch_is_rejected_before_dem_access(tmp_path):
    request = RiverSectionsRequest.from_mapping(_request(crs="EPSG:4547"))
    with pytest.raises(RiverSectionSelectionError, match="crs_mismatch"):
        select_river_sections(request, _rectangle_mesh(), tmp_path / "missing.tif")


def test_example_request_matches_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((EXAMPLES / "assumed_river_sections_request.schema.json").read_text(encoding="utf-8"))
    example = json.loads((EXAMPLES / "assumed_river_sections_request.example.json").read_text(encoding="utf-8"))
    jsonschema.validate(example, schema)
    request = RiverSectionsRequest.from_mapping(example)
    assert [s.name for s in request.sections] == ["A", "B", "C"]
    assert request.rule == ChannelRuleParameters()


# --------------------------------------------------------------------------- synthetic DEM selection


@needs_rasterio
def test_selection_matches_channel_notch(tmp_path):
    mesh = _rectangle_mesh()
    dem = _write_dem(tmp_path / "dem.tif")
    audit = select_river_sections(RiverSectionsRequest.from_mapping(_request()), mesh, dem)
    assert audit["format"] == AUDIT_FORMAT and audit["section_status"] == SECTION_STATUS
    section = audit["sections"]["S"]
    assert section["edge_count"] == 6 and section["width_m"] == pytest.approx(CHANNEL[1] - CHANNEL[0])
    assert {tuple(p) for p in section["edge_nodes"]} == _south_edge_nodes(*CHANNEL)
    _assert_exterior_contiguous(mesh, section["edge_indices"])
    assert section["thalweg_elevation_m"] == 1.0 and section["bank_level_p75_m"] == 5.0 and section["threshold_m"] == 3.0
    assert section["edges_above_threshold_included"] == 0 and section["nodata_edges_in_window"] == 0
    assert section["nearest_edge_distance_m"] == pytest.approx(np.hypot(5.0, 5.0))
    assert len(section["profile"]) == 6 and len(section["window_profile"]) == section["window_edge_count"]
    assert audit["rule"]["window_m"] == 200.0 and audit["rule"]["dem_samples_per_edge"] == 5
    assert audit["dem_sha256"] and audit["mesh_id"] == "synthetic-rect"


@needs_rasterio
def test_gap_tolerance_merges_one_edge_bump(tmp_path):
    mesh = _rectangle_mesh()
    dem = _write_dem(tmp_path / "dem_bump.tif", bump=True)
    merged = select_river_sections(RiverSectionsRequest.from_mapping(_request()), mesh, dem)["sections"]["S"]
    assert merged["edge_count"] == 6 and merged["edges_above_threshold_included"] == 1
    assert {tuple(p) for p in merged["edge_nodes"]} == _south_edge_nodes(*CHANNEL)
    split_request = _request(rule={"window_m": 200.0, "thalweg_search_m": 50.0, "gap_tolerance_m": 5.0})
    split = select_river_sections(RiverSectionsRequest.from_mapping(split_request), mesh, dem)["sections"]["S"]
    assert split["edge_count"] < 6 and split["edges_above_threshold_included"] == 0
    assert not ({tuple(p) for p in split["edge_nodes"]} & _south_edge_nodes(*BUMP))
    _assert_exterior_contiguous(mesh, split["edge_indices"])


@needs_rasterio
def test_nodata_edges_are_reported_not_filled(tmp_path):
    mesh = _rectangle_mesh()
    dem = _write_dem(tmp_path / "dem_nodata.tif", nodata_band=True)
    section = select_river_sections(RiverSectionsRequest.from_mapping(_request()), mesh, dem)["sections"]["S"]
    assert section["nodata_edges_in_window"] == 2
    assert sum(row["z_mean_m"] is None for row in section["window_profile"]) == 2
    assert section["edge_count"] == 6


@needs_rasterio
def test_point_far_from_boundary_and_dem_checks(tmp_path):
    mesh = _rectangle_mesh()
    dem = _write_dem(tmp_path / "dem.tif")
    far = _request(sections=[dict(_request()["sections"][0], xy=[180.0, -150.0])])
    with pytest.raises(RiverSectionSelectionError, match="point_far_from_boundary"):
        select_river_sections(RiverSectionsRequest.from_mapping(far), mesh, dem)
    tight = _request(max_point_distance_m=5.0)
    with pytest.raises(RiverSectionSelectionError, match="point_far_from_boundary"):
        select_river_sections(RiverSectionsRequest.from_mapping(tight), mesh, dem)
    with pytest.raises(RiverSectionSelectionError, match="dem_sha256_mismatch"):
        select_river_sections(RiverSectionsRequest.from_mapping(_request(dem_sha256="0" * 64)), mesh, dem)
    wide = _request(rule={"window_m": 500.0, "thalweg_search_m": 50.0})
    with pytest.raises(RiverSectionSelectionError, match="half the outer loop perimeter"):
        select_river_sections(RiverSectionsRequest.from_mapping(wide), mesh, dem)
    with pytest.raises(RiverSectionSelectionError, match="dem_crs_mismatch"):
        select_river_sections(RiverSectionsRequest.from_mapping(_request(crs="EPSG:4547")), _rectangle_mesh("EPSG:4547"), dem)


@needs_rasterio
def test_audit_matches_schema_and_cli_round_trip(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    mesh = _rectangle_mesh()
    dem = _write_dem(tmp_path / "dem.tif")
    artifact = tmp_path / "domain-mesh.json"
    artifact.write_text(json.dumps({
        "format": "triangle-domain-mesh-only-artifact-v1", "mesh_only": True, "mesh_id": mesh.mesh_id,
        "coordinate_system": mesh.coordinate_system, "node_xy": [list(p) for p in mesh.node_xy],
        "face_nodes": [list(f) for f in mesh.face_nodes], "face_id": list(mesh.face_id),
        "boundary_groups": {}, "constraint_groups": {}, "geometry_tolerance_m": 1e-8, "minimum_face_area_m2": 1e-12,
    }), encoding="utf-8")
    loaded, sha = load_mesh_artifact(artifact)
    assert loaded.n_face == mesh.n_face and len(sha) == 64
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_request(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "audit.json"
    argv = [str(request_path), "--mesh", str(artifact), "--dem", str(dem), "--out", str(out)]
    figure = tmp_path / "sections.png"
    if HAS_MATPLOTLIB:
        argv += ["--figure", str(figure)]
    assert main(argv) == 0
    audit = json.loads(out.read_text(encoding="utf-8"))
    schema = json.loads((EXAMPLES / "assumed_river_sections_audit.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(audit, schema)
    assert audit["mesh_artifact_sha256"] == sha and audit["sections"]["S"]["edge_count"] == 6
    if HAS_MATPLOTLIB:
        assert figure.is_file()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_request(crs="EPSG:4547")), encoding="utf-8")
    assert main([str(bad), "--mesh", str(artifact), "--dem", str(dem), "--out", str(tmp_path / "x.json")]) == 2
