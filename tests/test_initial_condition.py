from __future__ import annotations

import math
import unittest

from hydrodynamics import (
    InitialConditionError,
    InitialConditionField,
    InitialConditionQuantity,
    InitialWaterField,
)
from hydrodynamics.config import DomainConfig, TerrainField, TerrainMapper
from hydrodynamics.dem_contract import DEMDataset, DEMMetadata
from hydrodynamics.terrain_mapping import TerrainMappingError


CRS = "EPSG:4548"


def make_dataset(
    values: list[list[float]],
    *,
    pixel_size: float = 10.0,
    crs: str | None = CRS,
    vertical_datum: str | None = None,
    invalid: set[tuple[int, int]] | None = None,
) -> DEMDataset:
    height = len(values)
    width = len(values[0])
    invalid = invalid or set()
    top = height * pixel_size
    metadata = DEMMetadata(
        width=width,
        height=height,
        band_count=1,
        dtype="float64",
        xmin=0.0,
        xmax=width * pixel_size,
        ymin=0.0,
        ymax=top,
        pixel_size_x=pixel_size,
        pixel_size_y=pixel_size,
        transform=(pixel_size, 0.0, 0.0, 0.0, -pixel_size, top),
        crs=crs,
        horizontal_unit="metre",
        nodata_value=-9999.0,
        vertical_datum=vertical_datum,
    )
    valid = []
    nodata = []
    nonfinite = []
    for row_index, row in enumerate(values):
        valid_row = []
        nodata_row = []
        nonfinite_row = []
        for column_index, value in enumerate(row):
            bad = (row_index, column_index) in invalid or value == -9999.0
            finite = math.isfinite(float(value))
            valid_row.append(not bad and finite)
            nodata_row.append(bad and finite)
            nonfinite_row.append(not finite)
        valid.append(valid_row)
        nodata.append(nodata_row)
        nonfinite.append(nonfinite_row)
    return DEMDataset(
        metadata=metadata,
        elevation=values,
        valid_mask=valid,
        nodata_mask=nodata,
        nonfinite_mask=nonfinite,
    )


class InitialConditionFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.domain = DomainConfig(xmin=0, xmax=60, ymin=0, ymax=60, dx=30, dy=30)

    def terrain(self, *, datum: str | None = None) -> TerrainField:
        dataset = make_dataset([[10.0] * 6 for _ in range(6)], vertical_datum=datum)
        return TerrainMapper().map_dataset(
            dataset, self.domain, coordinate_system=CRS, strategy="area_weighted_mean"
        )

    def test_constant_depth_zero_is_valid_and_relative(self) -> None:
        terrain = self.terrain()
        field = InitialConditionField.from_constant_depth(terrain, 0.0)
        self.assertIs(field.quantity, InitialConditionQuantity.DEPTH)
        self.assertEqual(field.kind, "depth")
        self.assertEqual(field.shape, (self.domain.ny, self.domain.nx))
        self.assertTrue(all(value == 0.0 for row in field.values for value in row))
        self.assertTrue(all(value for row in field.valid_mask for value in row))
        self.assertTrue(all(value == 1.0 for row in field.coverage_ratio for value in row))
        self.assertEqual(field.water_surface_elevation[0, 0], 10.0)

    def test_constant_depth_rejects_negative(self) -> None:
        with self.assertRaises(InitialConditionError):
            InitialConditionField.from_constant_depth(self.terrain(), -0.1)

    def test_constant_depth_preserves_terrain_quality_masks(self) -> None:
        domain = self.domain
        terrain = TerrainField.from_domain(
            domain, [[1.0, 1.0], [1.0, 1.0]],
            valid_mask=[[True, False], [True, True]],
            coverage_ratio=[[1.0, 0.5], [1.0, 1.0]],
        )
        field = InitialConditionField.from_constant_depth(terrain, 2.0)
        self.assertTrue(field.valid_mask[0, 0])
        self.assertFalse(field.valid_mask[0, 1])
        self.assertEqual(field.coverage_ratio[0, 1], 0.5)

    def test_constant_level_requires_and_preserves_confirmed_datum(self) -> None:
        terrain = self.terrain(datum="D")
        field = InitialConditionField.from_constant_level(terrain, -5.0)
        self.assertIs(field.quantity, InitialConditionQuantity.LEVEL)
        self.assertEqual(field.values[0, 0], -5.0)
        self.assertEqual(field.vertical_datum, "D")
        self.assertNotEqual(field.values[0, 0], terrain.elevation[0, 0] - 5.0)

    def test_constant_level_missing_or_mismatched_datum_fails(self) -> None:
        with self.assertRaises(InitialConditionError):
            InitialConditionField.from_constant_level(self.terrain(), 5.0, vertical_datum="D")
        terrain = self.terrain(datum="D")
        with self.assertRaises(InitialConditionError):
            InitialConditionField.from_constant_level(terrain, 5.0, vertical_datum="OTHER")
        field = InitialConditionField.from_constant_level(self.domain, 5.0, vertical_datum="D")
        self.assertEqual(field.vertical_datum, "D")

    def test_constant_level_zero_is_not_nodata(self) -> None:
        field = InitialConditionField.from_constant_level(self.domain, 0.0, vertical_datum="D")
        self.assertEqual(field.values[0, 0], 0.0)
        self.assertFalse(field.nodata_mask[0, 0])

    def test_raster_depth_reuses_mapping_and_preserves_zero(self) -> None:
        dataset = make_dataset([[0.0] * 6 for _ in range(6)])
        field = InitialConditionField.from_dataset(
            dataset, self.domain, quantity="depth", coordinate_system=CRS, strategy="area_weighted_mean"
        )
        self.assertIs(field.quantity, InitialConditionQuantity.DEPTH)
        self.assertEqual(field.values[0, 0], 0.0)
        self.assertEqual(field.shape, (2, 2))

    def test_mapper_metadata_is_available_for_datum_checks(self) -> None:
        dataset = make_dataset([[1.0] * 6 for _ in range(6)], vertical_datum="D")
        terrain = TerrainMapper().map_dataset(
            dataset, self.domain, coordinate_system=CRS, strategy="area_weighted_mean"
        )
        self.assertEqual(terrain.coordinate_system, CRS)
        self.assertEqual(terrain.vertical_datum, "D")

    def test_raster_depth_nodata_and_negative_fail(self) -> None:
        invalid = {(row, column) for row in (3, 4, 5) for column in (0, 1, 2)}
        with self.assertRaises(TerrainMappingError):
            InitialConditionField.from_dataset(
                make_dataset([[1.0] * 6 for _ in range(6)], invalid=invalid),
                self.domain, coordinate_system=CRS, quantity="depth", strategy="area_weighted_mean"
            )
        values = [[1.0] * 6 for _ in range(6)]
        values[0][0] = -1.0
        with self.assertRaises(InitialConditionError):
            InitialConditionField.from_dataset(
                make_dataset(values), self.domain, coordinate_system=CRS, quantity="depth"
            )

    def test_raster_level_is_absolute_and_checks_datum(self) -> None:
        values = [[-5.0] * 6 for _ in range(6)]
        dataset = make_dataset(values, vertical_datum="D")
        terrain = self.terrain(datum="D")
        field = InitialConditionField.from_dataset(
            dataset, self.domain, quantity="level", terrain_field=terrain,
            coordinate_system=CRS, vertical_datum="D", strategy="area_weighted_mean"
        )
        self.assertIs(field.quantity, InitialConditionQuantity.LEVEL)
        self.assertEqual(field.values[0, 0], -5.0)
        self.assertEqual(field.vertical_datum, "D")
        with self.assertRaises(InitialConditionError):
            InitialConditionField.from_dataset(
                dataset, self.domain, quantity="level", coordinate_system=CRS, vertical_datum="OTHER"
            )
        with self.assertRaises(InitialConditionError):
            InitialConditionField.from_dataset(
                make_dataset(values), self.domain, quantity="level", coordinate_system=CRS, vertical_datum="D"
            )

    def test_raster_alignment_crs_and_quantity_errors(self) -> None:
        terrain = self.terrain(datum="D")
        wrong_domain = DomainConfig(xmin=0, xmax=100, ymin=0, ymax=100, dx=50, dy=50)
        with self.assertRaises(InitialConditionError):
            InitialConditionField.from_dataset(
                make_dataset([[1.0] * 6 for _ in range(6)]), wrong_domain,
                terrain_field=terrain, coordinate_system=CRS
            )
        with self.assertRaises(InitialConditionError):
            InitialConditionField.from_dataset(
                make_dataset([[1.0] * 6 for _ in range(6)]), self.domain,
                terrain_field=terrain, coordinate_system="EPSG:3857"
            )
        with self.assertRaises(InitialConditionError):
            InitialConditionField.from_dataset(
                make_dataset([[1.0] * 6 for _ in range(6)]), self.domain,
                coordinate_system=CRS, quantity="unknown"
            )
        with self.assertRaises(TerrainMappingError):
            InitialConditionField.from_dataset(
                make_dataset([[1.0] * 6 for _ in range(6)], crs="EPSG:3857"),
                self.domain, coordinate_system=CRS, quantity="depth"
            )

    def test_from_config_builds_depth_and_infers_raster_quantity(self) -> None:
        from hydrodynamics.config import InitialConditionConfig
        terrain = self.terrain()
        depth_config = InitialConditionConfig(type="constant_depth", depth=0.0)
        depth = InitialConditionField.from_config(depth_config, self.domain, terrain_field=terrain)
        self.assertEqual(depth.quantity, "depth")
        raster_config = InitialConditionConfig(type="raster", depth_file="data/depth.tif")
        raster = InitialConditionField.from_config(
            raster_config, self.domain, dataset=make_dataset([[0.0] * 6 for _ in range(6)]),
            terrain_field=terrain, coordinate_system=CRS, strategy="area_weighted_mean"
        )
        self.assertEqual(raster.quantity, "depth")

    def test_from_config_does_not_silently_drop_reserved_velocity(self) -> None:
        from hydrodynamics.config import InitialConditionConfig
        config = InitialConditionConfig(
            type="constant_depth", depth=0.0, velocity={"type": "constant", "x": 0.0, "y": 0.0}
        )
        with self.assertRaises(NotImplementedError):
            InitialConditionField.from_config(config, self.domain, terrain_field=self.terrain())

    def test_masks_must_not_overlap_and_alias_is_exported(self) -> None:
        with self.assertRaises(InitialConditionError):
            InitialConditionField(
                quantity="depth", values=[[0.0]], nx=1, ny=1, dx=1, dy=1, xmin=0, ymin=0,
                valid_mask=[[True]], nodata_mask=[[True]]
            )
        self.assertIs(InitialWaterField, InitialConditionField)


if __name__ == "__main__":
    unittest.main()
