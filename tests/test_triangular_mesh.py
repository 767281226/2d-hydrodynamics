from __future__ import annotations

import json
import math
from pathlib import Path
import shutil

import pytest

from hydrodynamics.dem_contract import DEMDataset, DEMMetadata
from hydrodynamics.mesh_preparation import (
    GmshMeshConfig,
    MeshPreparationConfig,
    MeshPreparationError,
    MeshImportConfig,
    TerrainSourceConfig,
    load_mesh_json,
    map_dem_to_triangular_mesh,
    read_dem_json,
    read_ugrid_preparation,
    write_ugrid_preparation,
)
from hydrodynamics.mesh_generation import _validate_geometry, generate_gmsh_mesh
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh, TriangularMeshError


def square_mesh(*, offset: float = 0.0) -> TriangularMesh:
    nodes = ((offset, offset), (offset + 2.0, offset), (offset + 2.0, offset + 2.0), (offset, offset + 2.0))
    return TriangularMesh(
        nodes, ((0, 1, 2), (0, 2, 3)), "EPSG:4548", mesh_id="square-v1",
        face_id=(81, 82), boundary_groups={"perimeter": ((0, 1), (1, 2), (2, 3), (3, 0))},
        constraint_groups={"diagonal": ((0, 2),)},
    )


def synthetic_dem(*, crs: str = "EPSG:4548", mask=None) -> DEMDataset:
    values = ((1.0, 2.0), (3.0, 4.0))
    valid = mask or ((True, True), (True, True))
    metadata = DEMMetadata(
        width=2, height=2, band_count=1, dtype="float64", xmin=0, xmax=2,
        ymin=0, ymax=2, pixel_size_x=1, pixel_size_y=1,
        transform=(1, 0, 0, 0, 1, 0), crs=crs, nodata_value=-9999,
        vertical_unit="m", vertical_datum="1985国家高程基准",
    )
    return DEMDataset(
        metadata, values, valid,
        tuple(tuple(not value for value in row) for row in valid),
        ((False, False), (False, False)),
    )


def clone_netcdf_with_variable_dtype(source: Path, target: Path, variable_to_change: str, dtype: str) -> None:
    netcdf4 = pytest.importorskip("netCDF4")
    with netcdf4.Dataset(source, "r") as original, netcdf4.Dataset(target, "w", format="NETCDF4") as clone:
        for name in original.ncattrs():
            clone.setncattr(name, original.getncattr(name))
        for name, dimension in original.dimensions.items():
            clone.createDimension(name, None if dimension.isunlimited() else len(dimension))
        for name, variable in original.variables.items():
            fill_value = variable.getncattr("_FillValue") if "_FillValue" in variable.ncattrs() else None
            variable_dtype = dtype if name == variable_to_change else variable.datatype
            copied = clone.createVariable(name, variable_dtype, variable.dimensions, fill_value=fill_value)
            for attribute in variable.ncattrs():
                if attribute != "_FillValue":
                    copied.setncattr(attribute, variable.getncattr(attribute))
            if variable.ndim == 0:
                copied.assignValue(variable.getValue())
            else:
                copied[:] = variable[:]


def test_topology_builds_unique_edges_adjacency_and_metrics():
    mesh = square_mesh()
    assert mesh.n_node == 4
    assert mesh.n_face == 2
    assert mesh.n_edge == 5
    assert mesh.face_area == (2.0, 2.0)
    assert mesh.face_centroid == ((4 / 3, 2 / 3), (2 / 3, 4 / 3))
    assert sum(right == -1 for _, right in mesh.edge_faces) == 4
    internal = next(index for index, pair in enumerate(mesh.edge_faces) if pair[1] != -1)
    assert mesh.edge_nodes[internal] == (0, 2)
    assert set(mesh.edge_faces[internal]) == {0, 1}
    assert len(mesh.boundary_groups["perimeter"]) == 4
    assert len(mesh.constraint_groups["diagonal"]) == 1
    assert mesh.quality_report()["preparation_only"] is True
    report = mesh.quality_report(minimum_quality=0.9)
    assert report["measured_minimum_face_area_m2"] == 2.0
    assert report["minimum_face_area_threshold_m2"] == mesh.minimum_face_area_m2
    assert report["measured_minimum_quality"] == pytest.approx(min(mesh.face_quality))
    assert report["minimum_quality_threshold"] == 0.9
    assert "minimum_face_area_m2" not in report


def test_projected_large_coordinates_keep_area_stable():
    mesh = TriangularMesh(
        ((500000.0, 4400000.0), (500002.0, 4400000.0), (500002.0, 4400002.0)),
        ((0, 1, 2),), "EPSG:4548",
    )
    assert mesh.face_area == (2.0,)


def test_coordinate_and_area_tolerances_are_explicit_and_enforced():
    with pytest.raises(TriangularMeshError, match="within geometry_tolerance_m"):
        TriangularMesh(((0.0, 0.0), (5e-9, 0.0), (0.0, 1.0)), ((0, 1, 2),), "EPSG:4548")
    with pytest.raises(TriangularMeshError, match="zero or near-zero area"):
        TriangularMesh(((0, 0), (1, 0), (0, 1)), ((0, 1, 2),), "EPSG:4548", minimum_face_area_m2=1.0)


@pytest.mark.parametrize(
    "nodes, faces, message",
    [
        (((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)), ((0, 1, 1),), "repeated node"),
        (((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)), ((0, 1, 3),), "out-of-range"),
        (((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)), ((0, 2, 1),), "counter-clockwise"),
        (((0.0, 0.0), (1.0, 0.0), (2.0, 0.0)), ((0, 1, 2),), "zero or near-zero"),
        (((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)), ((0, 1, 2.0),), "integers"),
    ],
)
def test_invalid_face_topology_has_clear_errors(nodes, faces, message):
    with pytest.raises(TriangularMeshError, match=message):
        TriangularMesh(nodes, faces, "EPSG:4548")


def test_rejects_nonmanifold_duplicate_overlap_and_bad_groups():
    with pytest.raises(TriangularMeshError, match="duplicate or within geometry_tolerance_m"):
        TriangularMesh(((0, 0), (1, 0), (0, 1), (0, 1)), ((0, 1, 2),), "EPSG:4548")
    with pytest.raises(TriangularMeshError, match="non-manifold"):
        TriangularMesh(
            ((0, 0), (1, 0), (0, 1), (1, 1), (0.5, -1)),
            ((0, 1, 2), (0, 1, 3), (1, 0, 4)), "EPSG:4548",
        )
    with pytest.raises(TriangularMeshError, match="overlap"):
        TriangularMesh(
            ((0, 0), (2, 0), (0, 2), (0.2, 0.2), (0.5, 0.2), (0.2, 0.5)),
            ((0, 1, 2), (3, 4, 5)), "EPSG:4548",
        )
    with pytest.raises(TriangularMeshError, match="internal edge"):
        TriangularMesh(
            ((0, 0), (2, 0), (2, 2), (0, 2)), ((0, 1, 2), (0, 2, 3)), "EPSG:4548",
            boundary_groups={"bad": ((0, 2),)},
        )
    with pytest.raises(TriangularMeshError, match="boundary topology is open or branched"):
        TriangularMesh(
            ((0, 0), (1, 0), (0, 1), (-1, 0), (0, -1)),
            ((0, 1, 2), (0, 3, 4)), "EPSG:4548",
        )


def test_contained_triangle_overlap_is_rejected_when_vertices_lie_on_outer_edges():
    nodes = ((0, 0), (4, 0), (0, 4), (0, 2), (2, 0), (2, 2))
    for faces in (((0, 1, 2), (3, 4, 5)), ((3, 4, 5), (0, 1, 2))):
        with pytest.raises(TriangularMeshError, match="overlap"):
            TriangularMesh(nodes, faces, "EPSG:4548")


@pytest.mark.parametrize("nodes, faces", [
    (((0.0, 0.0), (1_000_000.0, 0.0), (0.0, 0.01), (1.0, 0.001), (2.0, 0.001), (1.0, 0.002)), ((0, 1, 2), (3, 4, 5))),
    (((0.0, 0.0), (1_000_000.0, 0.0), (0.0, 0.01), (1.0, 0.001), (2.0, 0.001), (1.0, 0.002)), ((3, 4, 5), (0, 1, 2))),
])
def test_spatial_index_does_not_miss_long_thin_face_overlaps(nodes, faces):
    with pytest.raises(TriangularMeshError, match="overlap"):
        TriangularMesh(nodes, faces, "EPSG:4548")


def test_generated_face_ids_are_stable_and_unique():
    first = TriangularMesh(((0, 0), (1, 0), (0, 1)), ((0, 1, 2),), "EPSG:4548")
    second = TriangularMesh(((0, 0), (1, 0), (0, 1)), ((0, 1, 2),), "EPSG:4548")
    assert first.mesh_id == second.mesh_id
    assert first.face_id == second.face_id
    assert 0 <= first.face_id[0] < (1 << 63)


def test_import_mesh_json_preserves_stable_ids_groups_and_hole_fixture():
    pytest.importorskip("pyproj")
    base = Path(__file__).parents[1] / "examples" / "triangular_mesh"
    mesh = load_mesh_json(base / "mesh.json", coordinate_system="EPSG:4548")
    assert mesh.mesh_id == "synthetic-annulus-v1"
    assert mesh.face_id == tuple(range(1001, 1011))
    assert len(mesh.boundary_groups["outer_boundary"]) == 5
    assert len(mesh.boundary_groups["hole_0"]) == 5
    assert len(mesh.constraint_groups["internal_breakline"]) == 1
    assert math.isclose(sum(mesh.face_area), 14.25)


def test_import_mesh_json_rejects_mismatched_crs_and_unknown_fields(tmp_path):
    pytest.importorskip("pyproj")
    source = tmp_path / "bad.json"
    source.write_text(json.dumps({"format": "triangular-mesh-json-v1", "coordinate_system": "EPSG:4326", "node_xy": [[0, 0], [1, 0], [0, 1]], "face_nodes": [[0, 1, 2]]}), encoding="utf-8")
    with pytest.raises(MeshPreparationError, match="does not match config"):
        load_mesh_json(source, coordinate_system="EPSG:4548")
    source.write_text(json.dumps({"format": "triangular-mesh-json-v1", "node_xy": [[0, 0], [1, 0], [0, 1]], "face_nodes": [[0, 1, 2]], "future_key": True}), encoding="utf-8")
    with pytest.raises(MeshPreparationError, match="unsupported fields"):
        load_mesh_json(source, coordinate_system="EPSG:4548")


def test_dem_json_reads_source_values_masks_and_transform(tmp_path):
    path = tmp_path / "dem.json"
    path.write_text(json.dumps({
        "format": "synthetic-dem-json-v1", "coordinate_system": "EPSG:4548",
        "transform": [1, 0, 0, 0, 1, 0], "nodata_value": -9999,
        "elevation": [[4, -9999], [float("nan"), 8]],
    }), encoding="utf-8")
    dataset = read_dem_json(path)
    assert dataset.elevation[0, 1] == -9999
    assert dataset.nodata_mask[0, 1] is True
    assert dataset.nonfinite_mask[1, 0] is True
    assert dataset.valid_mask[1, 1] is True


def test_exact_overlap_area_mapping_and_nodata_policy():
    pytest.importorskip("pyproj")
    mesh = square_mesh()
    mapped = map_dem_to_triangular_mesh(synthetic_dem(), mesh)
    assert mapped.elevation_m == pytest.approx((2.25, 2.75))
    assert mapped.valid_area_m2 == pytest.approx((2.0, 2.0))
    assert mapped.covered_area_m2 == pytest.approx((2.0, 2.0))
    masked = map_dem_to_triangular_mesh(synthetic_dem(mask=((False, True), (True, True))), mesh)
    assert masked.coverage_ratio == pytest.approx((0.75, 0.75))
    assert masked.elevation_m == pytest.approx((8 / 3, 10 / 3))
    with pytest.raises(MeshPreparationError, match="below minimum"):
        map_dem_to_triangular_mesh(synthetic_dem(mask=((False, True), (True, True))), mesh, minimum_valid_coverage=0.8)
    with pytest.raises(MeshPreparationError, match="zero valid"):
        map_dem_to_triangular_mesh(synthetic_dem(mask=((False, False), (False, False))), mesh)
    with pytest.raises(MeshPreparationError, match="maximum_overlap_checks"):
        map_dem_to_triangular_mesh(synthetic_dem(), mesh, maximum_overlap_checks=7)


def test_mapping_rejects_crs_mismatch_and_uncovered_face():
    pytest.importorskip("pyproj")
    mesh = square_mesh()
    with pytest.raises(MeshPreparationError, match="does not match mesh CRS"):
        map_dem_to_triangular_mesh(synthetic_dem(crs="EPSG:4326"), mesh)
    metadata = DEMMetadata(
        width=1, height=1, band_count=1, dtype="float64", xmin=10, xmax=11,
        ymin=10, ymax=11, pixel_size_x=1, pixel_size_y=1,
        transform=(1, 0, 10, 0, 1, 10), crs="EPSG:4548", vertical_unit="m",
    )
    outside = DEMDataset(metadata, ((1.0,),), ((True,),), ((False,),), ((False,),))
    with pytest.raises(MeshPreparationError, match="zero valid"):
        map_dem_to_triangular_mesh(outside, mesh)


def test_mapping_rejects_vertical_unit_and_datum_conflicts_but_tracks_declarations():
    pytest.importorskip("pyproj")
    from dataclasses import replace
    mesh = square_mesh()
    base = synthetic_dem()
    wrong_unit_metadata = replace(base.metadata, vertical_unit="ft")
    wrong_unit = DEMDataset(wrong_unit_metadata, base.elevation, base.valid_mask, base.nodata_mask, base.nonfinite_mask)
    with pytest.raises(MeshPreparationError, match="does not convert elevations"):
        map_dem_to_triangular_mesh(wrong_unit, mesh)
    with pytest.raises(MeshPreparationError, match="conflicts with DEM metadata"):
        map_dem_to_triangular_mesh(base, mesh, vertical_datum="local datum")

    missing_unit_metadata = replace(base.metadata, vertical_unit=None, vertical_datum=None)
    missing_unit = DEMDataset(missing_unit_metadata, base.elevation, base.valid_mask, base.nodata_mask, base.nonfinite_mask)
    with pytest.raises(MeshPreparationError, match="vertical_unit is missing"):
        map_dem_to_triangular_mesh(missing_unit, mesh)
    declared = map_dem_to_triangular_mesh(missing_unit, mesh, vertical_unit="m", vertical_datum="source declaration")
    assert declared.vertical_unit == "m"
    assert declared.vertical_unit_source == "config_declaration"
    assert declared.vertical_datum == "source declaration"
    assert declared.vertical_datum_source == "config_declaration"
    assert declared.source_vertical_unit is None
    assert declared.source_vertical_datum is None


def test_face_terrain_rejects_wrong_mesh_identity_and_field_length():
    mesh = square_mesh()
    with pytest.raises(TriangularMeshError, match="does not match mesh"):
        FaceTerrainField("other", (1.0, 2.0), (True, True), (2.0, 2.0), (2.0, 2.0), (1.0, 1.0), "EPSG:4548").validate_for(mesh)
    with pytest.raises(TriangularMeshError, match="one value per face"):
        FaceTerrainField("square-v1", (1.0,), (True,), (2.0,), (2.0,), (1.0,), "EPSG:4548").validate_for(mesh)


def test_ugrid_preparation_round_trip_preserves_topology_ids_masks_and_groups(tmp_path):
    netcdf4 = pytest.importorskip("netCDF4")
    pytest.importorskip("pyproj")
    from pyproj import CRS
    mesh = square_mesh()
    terrain = map_dem_to_triangular_mesh(synthetic_dem(), mesh)
    path = tmp_path / "mesh.nc"
    write_ugrid_preparation(path, mesh, terrain, minimum_quality_threshold=0.25)
    with netcdf4.Dataset(path) as dataset:
        assert dataset.product_type == "mesh_and_terrain_preparation"
        assert dataset.preparation_only == 1
        assert dataset.simulation_result == 0
        assert dataset.variables["face_nodes"].start_index == 0
        assert dataset.variables["edge_length"].units == "m"
        assert dataset.variables["face_quality"].units == "1"
        assert "time" not in dataset.dimensions
        parsed = CRS.from_wkt(dataset.variables["crs"].crs_wkt)
        assert parsed.is_projected
        assert parsed.equals(CRS.from_user_input("EPSG:4548"), ignore_axis_order=True)
        assert dataset.variables["crs"].crs_wkt.startswith("PROJCRS[")
        assert dataset.variables["crs"].grid_mapping_name == parsed.to_cf()["grid_mapping_name"]
        assert dataset.vertical_unit == "m"
        assert dataset.vertical_unit_source == "dem_metadata"
        assert dataset.vertical_datum == "1985国家高程基准"
        assert dataset.vertical_datum_source == "dem_metadata"
        assert dataset.source_vertical_unit == "m"
        assert dataset.source_vertical_datum == "1985国家高程基准"
        assert dataset.minimum_quality_threshold == pytest.approx(0.25)
    loaded_mesh, loaded_terrain = read_ugrid_preparation(path)
    assert loaded_mesh.mesh_id == mesh.mesh_id
    assert loaded_mesh.node_xy == mesh.node_xy
    assert loaded_mesh.face_nodes == mesh.face_nodes
    assert loaded_mesh.face_id == mesh.face_id
    assert loaded_mesh.edge_nodes == mesh.edge_nodes
    assert loaded_mesh.edge_faces == mesh.edge_faces
    assert loaded_mesh.geometry_tolerance_m == mesh.geometry_tolerance_m
    assert loaded_mesh.minimum_face_area_m2 == mesh.minimum_face_area_m2
    assert loaded_mesh.boundary_groups == mesh.boundary_groups
    assert loaded_mesh.constraint_groups == mesh.constraint_groups
    assert loaded_terrain.elevation_m == pytest.approx(terrain.elevation_m)
    assert loaded_terrain.coverage_ratio == pytest.approx(terrain.coverage_ratio)


def test_ugrid_reader_rejects_noninteger_connectivity_and_face_ids(tmp_path):
    pytest.importorskip("netCDF4")
    pytest.importorskip("pyproj")
    mesh = square_mesh()
    terrain = map_dem_to_triangular_mesh(synthetic_dem(), mesh)
    source = tmp_path / "source.nc"
    write_ugrid_preparation(source, mesh, terrain)
    for name in ("face_nodes", "face_id"):
        malformed = tmp_path / f"float_{name}.nc"
        clone_netcdf_with_variable_dtype(source, malformed, name, "f8")
        with pytest.raises(MeshPreparationError, match=f"{name} must use an integer data type"):
            read_ugrid_preparation(malformed)


@pytest.mark.parametrize("attribute, value", [("add_offset", 0.25), ("scale_factor", 0.5)])
@pytest.mark.parametrize("variable", ["face_nodes", "edge_nodes", "edge_faces", "face_edges", "face_id", "terrain_valid"])
def test_ugrid_reader_rejects_packed_integer_variables(tmp_path, variable, attribute, value):
    netcdf4 = pytest.importorskip("netCDF4")
    pytest.importorskip("pyproj")
    mesh = square_mesh()
    terrain = map_dem_to_triangular_mesh(synthetic_dem(), mesh)
    path = tmp_path / "packed_integer.nc"
    write_ugrid_preparation(path, mesh, terrain)
    with netcdf4.Dataset(path, "r+") as dataset:
        setattr(dataset.variables[variable], attribute, value)
    with pytest.raises(MeshPreparationError, match=f"{variable} must not use packing attribute {attribute}"):
        read_ugrid_preparation(path)


@pytest.mark.parametrize(
    "variable, attribute, value, message",
    [
        ("terrain_valid", None, 2, "terrain_valid values must be exactly 0 or 1"),
        ("terrain_elevation", "units", "ft", "terrain_elevation.units must be 'm'"),
        ("terrain_elevation", "location", "edge", "terrain_elevation.location must be 'face'"),
        ("terrain_elevation", "grid_mapping", "unknown_crs", "terrain_elevation.grid_mapping must be 'crs'"),
    ],
)
def test_ugrid_reader_rejects_invalid_terrain_flags_units_and_location(tmp_path, variable, attribute, value, message):
    netcdf4 = pytest.importorskip("netCDF4")
    pytest.importorskip("pyproj")
    mesh = square_mesh()
    terrain = map_dem_to_triangular_mesh(synthetic_dem(), mesh)
    path = tmp_path / "bad_metadata.nc"
    write_ugrid_preparation(path, mesh, terrain)
    with netcdf4.Dataset(path, "r+") as dataset:
        if attribute is None:
            dataset.variables[variable][0] = value
        else:
            setattr(dataset.variables[variable], attribute, value)
    with pytest.raises(MeshPreparationError, match=message):
        read_ugrid_preparation(path)


def test_small_import_example_completes_prepare_only_workflow(tmp_path):
    pytest.importorskip("pyproj")
    pytest.importorskip("netCDF4")
    pytest.importorskip("yaml")
    from hydrodynamics.mesh_preparation import prepare_mesh
    sample = Path(__file__).parents[1] / "examples" / "triangular_mesh"
    for name in ("mesh.json", "dem.json"):
        shutil.copy(sample / name, tmp_path / name)
    (tmp_path / "preparation.yaml").write_text((sample / "preparation.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    result = prepare_mesh(tmp_path / "preparation.yaml")
    assert result.artifact_path == tmp_path / "prepared_mesh.nc"
    assert result.preparation_only is True
    assert result.mesh.face_id == tuple(range(1001, 1011))
    assert result.terrain.mesh_id == result.mesh.mesh_id
    assert result.artifact_path.is_file()


def test_ugrid_reader_rejects_topology_metric_mismatch(tmp_path):
    netcdf4 = pytest.importorskip("netCDF4")
    pytest.importorskip("pyproj")
    mesh = square_mesh()
    terrain = map_dem_to_triangular_mesh(synthetic_dem(), mesh)
    path = tmp_path / "corrupt.nc"
    write_ugrid_preparation(path, mesh, terrain)
    with netcdf4.Dataset(path, "r+") as dataset:
        dataset.variables["edge_length"][0] = float(dataset.variables["edge_length"][0]) * 2
    with pytest.raises(MeshPreparationError, match="edge_length is inconsistent"):
        read_ugrid_preparation(path)


def test_pydantic_model_exports_a_strict_json_schema():
    schema = MeshPreparationConfig.model_json_schema()
    assert schema["additionalProperties"] is False
    assert {"coordinate_system", "mesh", "terrain", "output_file"}.issubset(schema["required"])
    assert "minimum_valid_coverage" in schema["$defs"]["TerrainSourceConfig"]["properties"]
    assert "geometry_tolerance_m" in schema["properties"]


def test_gmsh_rejects_invalid_hole_and_boundary_crossing_constraint():
    bad_hole = GmshMeshConfig.model_validate({
        "type": "gmsh", "outer_boundary": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
        "holes": [[[5.0, 5.0], [6.0, 5.0], [6.0, 6.0], [5.0, 6.0]]], "default_size_m": 1.0,
    })
    with pytest.raises(MeshPreparationError, match="strictly inside"):
        generate_gmsh_mesh(bad_hole, coordinate_system="EPSG:4548")
    crossing_hole = GmshMeshConfig.model_validate({
        "type": "gmsh", "outer_boundary": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
        "holes": [[[1.0, 1.0], [3.0, 1.0], [3.0, 3.0], [1.0, 3.0]]],
        "constraints": [{"name": "crossing", "points": [[0.5, 2.0], [3.5, 2.0]]}],
        "default_size_m": 1.0,
    })
    with pytest.raises(MeshPreparationError, match="intersects, touches, or overlaps a region boundary"):
        generate_gmsh_mesh(crossing_hole, coordinate_system="EPSG:4548")


def test_boundary_group_open_path_and_closed_ring_semantics_are_explicit():
    open_path = GmshMeshConfig.model_validate({
        "type": "gmsh", "outer_boundary": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
        "boundary_groups": [{"name": "two_sides", "points": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0]]}],
        "default_size_m": 1.0,
    })
    closed_ring = GmshMeshConfig.model_validate({
        "type": "gmsh", "outer_boundary": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
        "boundary_groups": [{"name": "whole_ring", "points": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]], "closed": True}],
        "default_size_m": 1.0,
    })
    _validate_geometry(open_path, geometry_tolerance_m=1e-8, minimum_face_area_m2=1e-12)
    _validate_geometry(closed_ring, geometry_tolerance_m=1e-8, minimum_face_area_m2=1e-12)


def test_gmsh_refuses_self_touching_rings_constraint_t_junctions_and_foldbacks():
    touching_ring = GmshMeshConfig.model_validate({
        "type": "gmsh", "outer_boundary": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [2.0, 0.0], [0.0, 4.0]],
        "default_size_m": 1.0,
    })
    with pytest.raises(MeshPreparationError, match="self-intersects or self-touches"):
        generate_gmsh_mesh(touching_ring, coordinate_system="EPSG:4548")

    t_junction = GmshMeshConfig.model_validate({
        "type": "gmsh", "outer_boundary": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
        "constraints": [
            {"name": "horizontal", "points": [[1.0, 2.0], [3.0, 2.0]]},
            {"name": "vertical", "points": [[2.0, 2.0], [2.0, 3.0]]},
        ], "default_size_m": 1.0,
    })
    with pytest.raises(MeshPreparationError, match="intersects or self-touches"):
        generate_gmsh_mesh(t_junction, coordinate_system="EPSG:4548")

    foldback = GmshMeshConfig.model_validate({
        "type": "gmsh", "outer_boundary": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
        "constraints": [{"name": "foldback", "points": [[1.0, 1.0], [3.0, 1.0], [2.0, 1.0]]}],
        "default_size_m": 1.0,
    })
    with pytest.raises(MeshPreparationError, match="intersects or self-touches"):
        generate_gmsh_mesh(foldback, coordinate_system="EPSG:4548")


def test_gmsh_missing_optional_dependency_has_install_hint(monkeypatch):
    pytest.importorskip("pyproj")
    import builtins
    config = GmshMeshConfig.model_validate({
        "type": "gmsh", "outer_boundary": [[0.0, 0.0], [2.0, 0.0], [0.0, 2.0]],
        "default_size_m": 1.0,
    })
    actual_import = builtins.__import__
    monkeypatch.setattr("hydrodynamics.mesh_generation._gmsh_native_library_path", lambda: Path(__file__).resolve())
    def without_gmsh(name, *args, **kwargs):
        if name == "gmsh":
            raise ImportError("optional gmsh package hidden by test")
        return actual_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_gmsh)
    with pytest.raises(MeshPreparationError, match=r"pip install '\.\[gmsh\]'|'gmsh'"):
        generate_gmsh_mesh(config, coordinate_system="EPSG:4548")


def test_gmsh_python_binding_without_native_sdk_fails_before_ctypes_load(monkeypatch):
    import importlib.util
    if importlib.util.find_spec("gmsh") is None:
        pytest.skip("Gmsh Python binding is not installed")
    config = GmshMeshConfig.model_validate({
        "type": "gmsh", "outer_boundary": [[0.0, 0.0], [2.0, 0.0], [0.0, 2.0]],
        "default_size_m": 1.0,
    })
    monkeypatch.setattr("hydrodynamics.mesh_generation._gmsh_native_library_path", lambda: None)
    with pytest.raises(MeshPreparationError, match="native SDK library is missing"):
        generate_gmsh_mesh(config, coordinate_system="EPSG:4548")


def test_preparation_yaml_strict_schema_and_ranges(tmp_path):
    pytest.importorskip("pyproj")
    path = tmp_path / "case.yaml"
    path.write_text("""version: 1
coordinate_system: EPSG:4326
mesh:
  type: json
  file: mesh.json
terrain:
  type: json
  file: dem.json
output_file: out.nc
""", encoding="utf-8")
    from hydrodynamics.mesh_preparation import load_mesh_preparation_config
    with pytest.raises(MeshPreparationError, match="must be projected"):
        load_mesh_preparation_config(path)
    path.write_text("""version: 1
coordinate_system: EPSG:4548
mesh:
  type: json
  file: mesh.json
  typo: true
terrain:
  type: json
  file: dem.json
output_file: out.nc
""", encoding="utf-8")
    with pytest.raises(MeshPreparationError, match="Extra inputs are not permitted"):
        load_mesh_preparation_config(path)
