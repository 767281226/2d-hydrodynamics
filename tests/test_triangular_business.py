from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError

from hydrodynamics.mesh_preparation import (
    MeshPreparationError,
    load_mesh_json,
    map_dem_to_triangular_mesh,
    prepare_mesh,
    read_dem_json,
)
from hydrodynamics.triangular_business import (
    ConstantBoundaryInput,
    ConstantInitialInput,
    ConstantRoughnessInput,
    FaceAssignment,
    FaceInitialInput,
    FaceRoughnessInput,
    TimeSeriesBoundaryInput,
    TriangularBoundaryBinding,
    TriangularBusinessConfig,
    TriangularBusinessError,
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    _build_boundary_binding,
    load_triangular_business_config,
    prepare_triangular_business,
    read_triangular_business_package,
    topology_fingerprint,
    terrain_fingerprint,
    write_triangular_business_package,
)
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh, TriangularMeshError
from hydrodynamics.mesh_preparation import write_ugrid_preparation


def prepared_fixture() -> tuple[TriangularMesh, object]:
    pytest.importorskip("pyproj")
    example_dir = Path(__file__).parents[1] / "examples" / "triangular_mesh"
    mesh = load_mesh_json(example_dir / "mesh.json", coordinate_system="EPSG:4548")
    dem = read_dem_json(example_dir / "dem.json")
    terrain = map_dem_to_triangular_mesh(dem, mesh)
    return mesh, terrain


def write_constant_business_fixture(tmp_path):
    pytest.importorskip("netCDF4")
    pytest.importorskip("pyproj")
    mesh, terrain = prepared_fixture()
    roughness = TriangularRoughnessField.from_constant(mesh, 0.035, source="constant acceptance fixture")
    initial = TriangularInitialConditionField.from_constant(mesh, terrain, "depth", 0.2, source="constant acceptance fixture")
    package = TriangularBusinessPackage(mesh, terrain, roughness, initial, ())
    mesh_path = tmp_path / "mesh.nc"
    package_path = tmp_path / "business.json"
    write_ugrid_preparation(mesh_path, mesh, terrain)
    write_triangular_business_package(package_path, package, mesh_artifact=mesh_path)
    return mesh, terrain, roughness, initial, package, mesh_path, package_path


def test_topology_fingerprint_tracks_geometry_connectivity_ids_and_groups():
    first = TriangularMesh(
        ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)),
        ((0, 1, 2), (0, 2, 3)), "EPSG:4548", mesh_id="same-mesh",
        face_id=(10, 11), boundary_groups={"outer": ((0, 1), (1, 2), (2, 3), (3, 0))},
    )
    changed_id = TriangularMesh(
        first.node_xy, first.face_nodes, first.coordinate_system, mesh_id="same-mesh",
        face_id=(10, 12), boundary_groups={"outer": ((0, 1), (1, 2), (2, 3), (3, 0))},
    )
    changed_groups = TriangularMesh(
        first.node_xy, first.face_nodes, first.coordinate_system, mesh_id="same-mesh",
        face_id=(10, 11), boundary_groups={"perimeter": ((0, 1), (1, 2), (2, 3), (3, 0))},
    )
    assert len(topology_fingerprint(first)) == 64
    assert topology_fingerprint(first) != topology_fingerprint(changed_id)
    assert topology_fingerprint(first) != topology_fingerprint(changed_groups)


def test_roughness_constant_and_face_id_assignments_keep_masks_and_units():
    mesh, _ = prepared_fixture()
    constant = TriangularRoughnessField.from_constant(mesh, 0.035, source="synthetic lookup")
    assert constant.unit == "1"
    assert constant.manning_n == pytest.approx((0.035,) * mesh.n_face)
    assert all(constant.valid_mask)

    by_id = TriangularRoughnessField.from_assignments(
        mesh, ((1003, 0.05), (1001, 0.03)), source="two-face synthetic field"
    )
    assert by_id.face_id == tuple(mesh.face_id)
    assert by_id.manning_n[:3] == pytest.approx((0.03, None, 0.05))
    assert by_id.valid_mask[:3] == (True, False, True)
    assert sum(by_id.valid_mask) == 2
    with pytest.raises(TriangularBusinessError, match="unknown face_id"):
        TriangularRoughnessField.from_assignments(mesh, ((9999, 0.03),), source="bad")
    with pytest.raises(TriangularBusinessError, match="repeat face_id"):
        TriangularRoughnessField.from_assignments(mesh, ((1001, 0.03), (1001, 0.04)), source="bad")
    with pytest.raises(TriangularBusinessError, match=r"range \(0, 1\]"):
        TriangularRoughnessField.from_constant(mesh, 0.0, source="bad")


def test_integer_terrain_values_keep_fingerprint_across_ugrid_round_trip(tmp_path):
    pytest.importorskip("netCDF4")
    mesh, terrain = prepared_fixture()
    terrain = replace(terrain, elevation_m=(10,) * mesh.n_face)
    numeric_fields = ("elevation_m", "covered_area_m2", "valid_area_m2", "coverage_ratio")
    integer_values = {
        name: tuple(int(value) if value is not None and float(value).is_integer() else value for value in getattr(terrain, name))
        for name in numeric_fields
    }
    integer_terrain = replace(terrain, **integer_values)
    float_terrain = replace(integer_terrain, **{
        name: tuple(None if value is None else float(value) for value in getattr(integer_terrain, name))
        for name in numeric_fields
    })
    assert terrain_fingerprint(mesh, integer_terrain) == terrain_fingerprint(mesh, float_terrain)
    roughness = TriangularRoughnessField.from_constant(mesh, 0.035, source="numeric storage fixture")
    initial = TriangularInitialConditionField.from_constant(mesh, integer_terrain, "depth", 0.2, source="numeric storage fixture")
    package = TriangularBusinessPackage(mesh, integer_terrain, roughness, initial, ())
    mesh_path, package_path = tmp_path / "mesh.nc", tmp_path / "business.json"
    write_ugrid_preparation(mesh_path, mesh, integer_terrain)
    write_triangular_business_package(package_path, package, mesh_artifact=mesh_path)
    restored = read_triangular_business_package(package_path)
    assert restored.terrain.elevation_m == integer_terrain.elevation_m
    assert terrain_fingerprint(restored.mesh, restored.terrain) == terrain_fingerprint(mesh, integer_terrain)


def test_initial_depth_and_absolute_level_reuse_face_and_vertical_datum_semantics():
    mesh, terrain = prepared_fixture()
    depth = TriangularInitialConditionField.from_constant(
        mesh, terrain, "depth", 0.2, source="synthetic initial state"
    )
    assert depth.values_m == pytest.approx((0.2,) * mesh.n_face)
    assert depth.quantity.value == "depth"
    by_id = TriangularInitialConditionField.from_assignments(
        mesh, terrain, "depth", ((1001, 0.0), (1002, 0.3)), source="partial initial fixture"
    )
    assert by_id.values_m[:3] == (0.0, 0.3, None)
    assert by_id.valid_mask[:3] == (True, True, False)
    level = TriangularInitialConditionField.from_assignments(
        mesh, terrain, "level", ((1001, 103.0),),
        source="water level observation", vertical_datum="1985国家高程基准",
    )
    assert level.quantity.value == "level"
    assert level.vertical_datum == terrain.vertical_datum
    assert level.vertical_datum_source == "config_declaration"
    inherited_level = TriangularInitialConditionField.from_constant(
        mesh, terrain, "level", 103.0, source="terrain-datum inheritance"
    )
    assert inherited_level.vertical_datum == terrain.vertical_datum
    assert inherited_level.vertical_datum_source == "terrain_metadata"
    with pytest.raises(TriangularBusinessError, match="greater than or equal to 0 m"):
        TriangularInitialConditionField.from_constant(mesh, terrain, "depth", -0.01, source="bad")
    with pytest.raises(TriangularBusinessError, match="does not match terrain vertical_datum"):
        TriangularInitialConditionField.from_constant(
            mesh, terrain, "level", 103.0, source="bad", vertical_datum="other datum"
        )
    unknown_datum_terrain = replace(terrain, vertical_datum=None, vertical_datum_source=None, source_vertical_datum=None)
    with pytest.raises(TriangularBusinessError, match="known terrain vertical_datum"):
        TriangularInitialConditionField.from_constant(
            mesh, unknown_datum_terrain, "level", 103.0, source="bad", vertical_datum="1985国家高程基准"
        )


def test_face_fields_reject_stale_mesh_identity_topology_and_face_order():
    mesh, _ = prepared_fixture()
    other = TriangularMesh(mesh.node_xy, mesh.face_nodes, mesh.coordinate_system, mesh_id="other", face_id=mesh.face_id,
                           boundary_groups={name: [mesh.edge_nodes[i] for i in ids] for name, ids in mesh.boundary_groups.items()},
                           constraint_groups={name: [mesh.edge_nodes[i] for i in ids] for name, ids in mesh.constraint_groups.items()})
    field = TriangularRoughnessField.from_constant(mesh, 0.035, source="fixture")
    with pytest.raises(TriangularBusinessError, match="mesh_id"):
        replace(field, mesh_id="other").validate_for(mesh)
    with pytest.raises(TriangularBusinessError, match="topology_fingerprint"):
        replace(field, topology_fingerprint=topology_fingerprint(other)).validate_for(mesh)
    with pytest.raises(TriangularBusinessError, match="face_id ordering"):
        replace(field, face_id=tuple(reversed(field.face_id))).validate_for(mesh)


def test_boundary_binding_uses_real_edges_group_totals_and_time_series():
    mesh, terrain = prepared_fixture()
    inflow = TimeSeriesBoundaryInput.model_validate({
        "type": "timeseries", "group_name": "outer_boundary", "quantity": "discharge",
        "time_s": [0.0, 3600.0, 7200.0], "values": [0.0, 1.5, 1.5], "source": "synthetic inflow",
    })
    bound = _build_boundary_binding(mesh, terrain, inflow)
    assert bound.quantity.value == "discharge"
    assert bound.unit == "m3/s"
    assert bound.interpretation == "total_group_discharge"
    assert bound.time_s == (0.0, 3600.0, 7200.0)
    assert bound.edge_ids == tuple(mesh.boundary_groups["outer_boundary"])
    assert bound.edge_nodes == tuple(mesh.edge_nodes[index] for index in bound.edge_ids)

    level = ConstantBoundaryInput.model_validate({
        "type": "constant", "group_name": "hole_0", "quantity": "water_level",
        "value": 102.0, "vertical_datum": "1985国家高程基准", "source": "synthetic hole level",
    })
    level_binding = _build_boundary_binding(mesh, terrain, level)
    assert level_binding.unit == "m"
    assert level_binding.interpretation == "absolute_water_level"
    assert level_binding.vertical_datum == terrain.vertical_datum
    with pytest.raises(TriangularBusinessError, match="internal constraint group"):
        _build_boundary_binding(mesh, terrain, ConstantBoundaryInput.model_validate({
            "type": "constant", "group_name": "internal_breakline", "quantity": "discharge",
            "value": 1.0, "source": "invalid",
        }))
    with pytest.raises(TriangularBusinessError, match="unknown exterior boundary group"):
        _build_boundary_binding(mesh, terrain, ConstantBoundaryInput.model_validate({
            "type": "constant", "group_name": "missing_group", "quantity": "discharge",
            "value": 1.0, "source": "invalid",
        }))
    with pytest.raises(TriangularBusinessError, match="vertical_datum must match terrain vertical_datum"):
        _build_boundary_binding(mesh, terrain, ConstantBoundaryInput.model_validate({
            "type": "constant", "group_name": "outer_boundary", "quantity": "water_level",
            "value": 102.0, "vertical_datum": "other datum", "source": "invalid",
        }))
    with pytest.raises(ValidationError, match="strictly increasing"):
        TimeSeriesBoundaryInput.model_validate({
            "type": "timeseries", "group_name": "outer_boundary", "quantity": "discharge",
            "time_s": [0.0, 0.0], "values": [1.0, 2.0], "source": "invalid",
        })
    with pytest.raises(ValidationError, match="same non-zero length"):
        TimeSeriesBoundaryInput.model_validate({
            "type": "timeseries", "group_name": "outer_boundary", "quantity": "discharge",
            "time_s": [0.0, 10.0], "values": [1.0], "source": "invalid",
        })


def test_business_package_rejects_distinct_groups_that_bind_the_same_edge_twice():
    mesh = TriangularMesh(
        ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        ((0, 1, 2), (0, 2, 3)), "EPSG:4548", mesh_id="overlapping-groups",
        face_id=(1, 2), boundary_groups={"first": ((0, 1), (1, 2)), "second": ((0, 1), (2, 3))},
    )
    terrain = FaceTerrainField(
        mesh.mesh_id, (10.0, 10.0), (True, True), (0.5, 0.5), (0.5, 0.5), (1.0, 1.0),
        "EPSG:4548", "synthetic datum", vertical_datum_source="config_declaration",
    )
    roughness = TriangularRoughnessField.from_constant(mesh, 0.035, source="fixture")
    initial = TriangularInitialConditionField.from_constant(mesh, terrain, "depth", 0.0, source="fixture")
    first = _build_boundary_binding(mesh, terrain, ConstantBoundaryInput.model_validate({
        "type": "constant", "group_name": "first", "quantity": "discharge", "value": 1.0, "source": "a",
    }))
    second = _build_boundary_binding(mesh, terrain, ConstantBoundaryInput.model_validate({
        "type": "constant", "group_name": "second", "quantity": "discharge", "value": 1.0, "source": "b",
    }))
    with pytest.raises(TriangularBusinessError, match="bound more than once"):
        TriangularBusinessPackage(mesh, terrain, roughness, initial, (first, second))


def test_negative_absolute_water_levels_are_valid_in_constant_series_and_package_readback(tmp_path):
    pytest.importorskip("netCDF4")
    pytest.importorskip("pyproj")
    mesh, terrain, roughness, initial, _, mesh_path, _ = write_constant_business_fixture(tmp_path)
    constant_config = ConstantBoundaryInput.model_validate({
        "type": "constant", "group_name": "outer_boundary", "quantity": "water_level",
        "value": -0.5, "vertical_datum": terrain.vertical_datum, "source": "negative datum-relative level",
    })
    series_config = TimeSeriesBoundaryInput.model_validate({
        "type": "timeseries", "group_name": "hole_0", "quantity": "water_level",
        "time_s": [0.0, 10.0], "values": [-0.5, 0.2],
        "vertical_datum": terrain.vertical_datum, "source": "negative level series",
    })
    constant = _build_boundary_binding(mesh, terrain, constant_config)
    series = _build_boundary_binding(mesh, terrain, series_config)
    assert constant.values == (-0.5,)
    assert series.values == (-0.5, 0.2)
    package = TriangularBusinessPackage(mesh, terrain, roughness, initial, (constant, series))
    package_path = tmp_path / "negative-levels.json"
    write_triangular_business_package(package_path, package, mesh_artifact=mesh_path)
    loaded = read_triangular_business_package(package_path)
    assert loaded.boundary_bindings[0].values == (-0.5,)
    assert loaded.boundary_bindings[1].values == (-0.5, 0.2)


def test_discharge_nonnegative_contract_is_unchanged_for_constant_and_series():
    with pytest.raises(ValidationError, match="discharge value must be greater than or equal to 0"):
        ConstantBoundaryInput.model_validate({
            "type": "constant", "group_name": "outer_boundary", "quantity": "discharge",
            "value": -0.5, "source": "invalid discharge",
        })
    with pytest.raises(ValidationError, match="boundary values must be greater than or equal to 0"):
        TimeSeriesBoundaryInput.model_validate({
            "type": "timeseries", "group_name": "outer_boundary", "quantity": "discharge",
            "time_s": [0.0, 10.0], "values": [-0.5, 0.2], "source": "invalid discharge series",
        })


def test_business_package_json_round_trip_is_bound_to_p2_mesh_and_terrain(tmp_path):
    pytest.importorskip("netCDF4")
    pytest.importorskip("pyproj")
    sample = Path(__file__).parents[1] / "examples" / "triangular_mesh"
    for name in ("mesh.json", "dem.json", "preparation.yaml", "business_preparation.yaml"):
        shutil.copy(sample / name, tmp_path / name)
    prepared = prepare_mesh(tmp_path / "preparation.yaml")
    result = prepare_triangular_business(tmp_path / "business_preparation.yaml")
    assert result.package.preparation_only is True
    assert result.package.mesh.mesh_id == prepared.mesh.mesh_id
    assert result.report["preparation_only"] is True
    assert result.report["simulation_result"] is False
    assert result.report["roughness_valid_faces"] == prepared.mesh.n_face
    assert result.report["initial_condition_valid_faces"] == 3
    assert result.report["boundary_bindings"] == 2
    package = read_triangular_business_package(result.artifact_path)
    assert package.mesh.mesh_id == prepared.mesh.mesh_id
    assert package.roughness.face_id == tuple(prepared.mesh.face_id)
    assert package.roughness.manning_n == pytest.approx((0.035,) * prepared.mesh.n_face)
    assert package.initial_condition.values_m[:4] == (0.2, 0.1, 0.0, None)
    assert [binding.group_name for binding in package.boundary_bindings] == ["outer_boundary", "hole_0"]
    assert package.boundary_bindings[0].interpretation == "total_group_discharge"
    assert package.boundary_bindings[1].interpretation == "absolute_water_level"
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    assert payload["preparation_only"] is True and payload["simulation_result"] is False


@pytest.mark.parametrize(
    "field,key,value,message",
    [
        ("roughness", "values", 0.04, "constant roughness values must be identical"),
        ("initial_condition", "values_m", 0.3, "constant initial-condition values_m must be identical"),
    ],
)
def test_package_reader_rejects_changed_values_in_constant_mode(tmp_path, field, key, value, message):
    _, _, _, _, _, _, package_path = write_constant_business_fixture(tmp_path)
    payload = json.loads(package_path.read_text(encoding="utf-8"))
    assert payload[field]["assignment_mode"] == "constant"
    payload[field][key][0] = value
    package_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TriangularBusinessError, match=message):
        read_triangular_business_package(package_path)


def test_business_package_reader_rejects_linked_terrain_content_changes(tmp_path):
    netcdf4 = pytest.importorskip("netCDF4")
    _, _, _, _, _, mesh_path, package_path = write_constant_business_fixture(tmp_path)
    with netcdf4.Dataset(mesh_path, "r+") as dataset:
        variable = dataset.variables["terrain_elevation"]
        variable[0] = float(variable[0]) + 10.0
    with pytest.raises(TriangularBusinessError, match="terrain_fingerprint"):
        read_triangular_business_package(package_path)


@pytest.mark.parametrize("component", ["validity", "coverage_area", "source_unit", "source_datum"])
def test_terrain_fingerprint_covers_masks_areas_coverage_and_source_provenance(tmp_path, component):
    netcdf4 = pytest.importorskip("netCDF4")
    _, _, _, _, _, mesh_path, package_path = write_constant_business_fixture(tmp_path)
    with netcdf4.Dataset(mesh_path, "r+") as dataset:
        if component == "validity":
            dataset.variables["terrain_valid"][0] = 0
            dataset.variables["terrain_elevation"][0] = float("nan")
            dataset.variables["terrain_valid_area"][0] = 0.0
            dataset.variables["terrain_coverage_ratio"][0] = 0.0
        elif component == "coverage_area":
            area = float(dataset.variables["face_area"][0])
            new_valid_area = float(dataset.variables["terrain_valid_area"][0]) * 0.5
            dataset.variables["terrain_valid_area"][0] = new_valid_area
            dataset.variables["terrain_coverage_ratio"][0] = new_valid_area / area
        elif component == "source_unit":
            dataset.source_vertical_unit = "metre"
        else:
            dataset.vertical_datum = "changed source datum"
            dataset.source_vertical_datum = "changed source datum"
    with pytest.raises(TriangularBusinessError, match="terrain_fingerprint"):
        read_triangular_business_package(package_path)


def test_business_package_writer_rejects_same_topology_with_different_terrain(tmp_path):
    netcdf4 = pytest.importorskip("netCDF4")
    mesh, terrain, roughness, initial, package, mesh_path, _ = write_constant_business_fixture(tmp_path)
    with netcdf4.Dataset(mesh_path, "r+") as dataset:
        variable = dataset.variables["terrain_elevation"]
        variable[0] = float(variable[0]) + 10.0
    with pytest.raises(TriangularBusinessError, match="terrain contents/provenance"):
        write_triangular_business_package(tmp_path / "other.json", package, mesh_artifact=mesh_path)


def test_business_reader_rejects_legacy_package_without_a_terrain_fingerprint(tmp_path):
    _, _, _, _, _, _, package_path = write_constant_business_fixture(tmp_path)
    payload = json.loads(package_path.read_text(encoding="utf-8"))
    assert payload["format"] == "triangular-business-preparation-v2"
    payload["format"] = "triangular-business-preparation-v1"
    payload.pop("terrain_fingerprint")
    package_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TriangularBusinessError, match="legacy v1.*regenerate"):
        read_triangular_business_package(package_path)


def test_business_package_reader_rejects_stale_topology_and_masks(tmp_path):
    pytest.importorskip("netCDF4")
    pytest.importorskip("pyproj")
    sample = Path(__file__).parents[1] / "examples" / "triangular_mesh"
    for name in ("mesh.json", "dem.json", "preparation.yaml", "business_preparation.yaml"):
        shutil.copy(sample / name, tmp_path / name)
    prepare_mesh(tmp_path / "preparation.yaml")
    result = prepare_triangular_business(tmp_path / "business_preparation.yaml")
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    payload["topology_fingerprint"] = "0" * 64
    result.artifact_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TriangularBusinessError, match="topology_fingerprint"):
        read_triangular_business_package(result.artifact_path)


@pytest.mark.parametrize("mutation, message", [
    (lambda payload: payload.update(mesh_id="stale-mesh"), "mesh_id/topology_fingerprint"),
    (lambda payload: payload["face_id"].reverse(), "face_id list"),
    (lambda payload: payload["roughness"]["face_id"].reverse(), "face_id ordering"),
    (lambda payload: payload["roughness"]["valid_mask"].__setitem__(0, 1), "valid_mask values must be boolean"),
    (lambda payload: payload["boundary_bindings"][0].update(mesh_id="stale-mesh"), "does not match mesh"),
])
def test_business_package_reader_rejects_mesh_and_field_identity_mismatches(tmp_path, mutation, message):
    pytest.importorskip("netCDF4")
    pytest.importorskip("pyproj")
    sample = Path(__file__).parents[1] / "examples" / "triangular_mesh"
    for name in ("mesh.json", "dem.json", "preparation.yaml", "business_preparation.yaml"):
        shutil.copy(sample / name, tmp_path / name)
    prepare_mesh(tmp_path / "preparation.yaml")
    result = prepare_triangular_business(tmp_path / "business_preparation.yaml")
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    mutation(payload)
    result.artifact_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TriangularBusinessError, match=message):
        read_triangular_business_package(result.artifact_path)


def test_business_config_json_schema_is_strict_and_rejects_duplicate_groups():
    schema = TriangularBusinessConfig.model_json_schema()
    assert schema["additionalProperties"] is False
    assert {"mesh_file", "output_file", "roughness", "initial_condition"}.issubset(schema["required"])
    with pytest.raises(ValidationError, match="must not repeat a group_name"):
        TriangularBusinessConfig.model_validate({
            "mesh_file": "prepared.nc", "output_file": "business.json",
            "roughness": {"type": "constant", "manning_n": 0.035, "source": "fixture"},
            "initial_condition": {"type": "constant", "quantity": "depth", "value_m": 0.0, "source": "fixture"},
            "boundary_bindings": [
                {"type": "constant", "group_name": "outer", "quantity": "discharge", "value": 1.0, "source": "a"},
                {"type": "constant", "group_name": "outer", "quantity": "discharge", "value": 2.0, "source": "b"},
            ],
        })
