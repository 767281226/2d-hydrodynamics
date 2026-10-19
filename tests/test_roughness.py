from __future__ import annotations

import math
import unittest

from hydrodynamics import RoughnessError, RoughnessField, ResistanceField
from hydrodynamics.config import DomainConfig, RoughnessConfig, TerrainMapper
from hydrodynamics.dem_contract import DEMDataset, DEMMetadata
from hydrodynamics.terrain_mapping import TerrainMappingError

CRS = "EPSG:4548"


def make_dataset(values, *, crs=CRS, invalid=None):
    invalid = invalid or set()
    height, width = len(values), len(values[0])
    metadata = DEMMetadata(width=width, height=height, band_count=1, dtype="float64",
        xmin=0.0, xmax=width * 10.0, ymin=0.0, ymax=height * 10.0,
        pixel_size_x=10.0, pixel_size_y=10.0,
        transform=(10.0, 0.0, 0.0, 0.0, -10.0, height * 10.0), crs=crs,
        horizontal_unit="metre", nodata_value=-9999.0)
    valid=[]; nodata=[]; nonfinite=[]
    for r, row in enumerate(values):
        vr=[]; nr=[]; fr=[]
        for c, value in enumerate(row):
            bad=(r,c) in invalid or value == -9999.0
            finite=math.isfinite(float(value))
            vr.append(not bad and finite); nr.append(bad and finite); fr.append(not finite)
        valid.append(vr); nodata.append(nr); nonfinite.append(fr)
    return DEMDataset(metadata=metadata, elevation=values, valid_mask=valid,
        nodata_mask=nodata, nonfinite_mask=nonfinite)


class RoughnessFieldTests(unittest.TestCase):
    def setUp(self):
        self.domain = DomainConfig(xmin=0, xmax=60, ymin=0, ymax=60, dx=30, dy=30)

    def terrain(self):
        return TerrainMapper().map_dataset(make_dataset([[1.0]*6 for _ in range(6)]),
            self.domain, coordinate_system=CRS, strategy="area_weighted_mean")

    def test_constant_field_is_positive_and_aligned(self):
        field = RoughnessField.from_constant(self.domain, 0.035, coordinate_system=CRS)
        self.assertEqual(field.quantity, "manning_n")
        self.assertEqual(field.shape, (2, 2))
        self.assertEqual(field.manning_n[0, 0], 0.035)
        self.assertEqual(field.crs, CRS)
        self.assertTrue(all(v for row in field.valid_mask for v in row))

    def test_constant_inherits_terrain_crs_and_allows_different_dx_dy(self):
        domain = DomainConfig(xmin=0, xmax=60, ymin=0, ymax=60, dx=30, dy=30)
        terrain = TerrainMapper().map_dataset(make_dataset([[1.0]*6 for _ in range(6)]),
            domain, coordinate_system=CRS, strategy="area_weighted_mean")
        field = RoughnessField.from_constant(terrain, 0.04)
        self.assertEqual((field.dx, field.dy, field.crs), (30.0, 30.0, CRS))

    def test_constant_rejects_nonpositive_or_nonfinite(self):
        for value in (0.0, -0.1, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(RoughnessError):
                RoughnessField.from_constant(self.domain, value)

    def test_raster_maps_positive_values_and_preserves_metadata(self):
        field = RoughnessField.from_dataset(make_dataset([[0.03]*6 for _ in range(6)]),
            self.domain, coordinate_system=CRS, strategy="area_weighted_mean")
        self.assertEqual(field.shape, (2, 2))
        self.assertAlmostEqual(field.values[0, 0], 0.03)
        self.assertEqual(field.coverage_ratio[0, 0], 1.0)

    def test_raster_rejects_nonpositive_valid_values(self):
        for value in (0.0, -0.01):
            values=[[0.03]*6 for _ in range(6)]; values[0][0]=value
            with self.subTest(value=value), self.assertRaises(RoughnessError):
                RoughnessField.from_dataset(make_dataset(values), self.domain,
                    coordinate_system=CRS)

    def test_raster_nodata_error_and_reserved_strategies(self):
        invalid={(r,c) for r in range(3,6) for c in range(3)}
        with self.assertRaises(TerrainMappingError):
            RoughnessField.from_dataset(make_dataset([[0.03]*6 for _ in range(6)], invalid=invalid),
                self.domain, coordinate_system=CRS, strategy="area_weighted_mean")
        for strategy in ("nearest", "interpolate"):
            with self.subTest(strategy=strategy), self.assertRaises(NotImplementedError):
                RoughnessField.from_dataset(make_dataset([[0.03]*6 for _ in range(6)]),
                    self.domain, coordinate_system=CRS, nodata_strategy=strategy)

    def test_raster_requires_compatible_crs(self):
        with self.assertRaises(TerrainMappingError):
            RoughnessField.from_dataset(make_dataset([[0.03]*6 for _ in range(6)], crs="EPSG:4326"),
                self.domain, coordinate_system=CRS)

    def test_terrain_alignment_is_checked(self):
        other = DomainConfig(xmin=0, xmax=30, ymin=0, ymax=30, dx=30, dy=30)
        terrain = TerrainMapper().map_dataset(make_dataset([[1.0]*3 for _ in range(3)]),
            other, coordinate_system=CRS, strategy="area_weighted_mean")
        with self.assertRaises(RoughnessError):
            RoughnessField.from_dataset(make_dataset([[0.03]*6 for _ in range(6)]),
                self.domain, terrain_field=terrain, coordinate_system=CRS)

    def test_from_config_constant_and_raster(self):
        constant = RoughnessConfig.model_validate({"type":"constant", "manning_n":0.035})
        self.assertEqual(RoughnessField.from_config(constant, self.domain).values[0,0], 0.035)
        raster = RoughnessConfig.model_validate({"type":"raster", "file":"data/manning.tif"})
        with self.assertRaises(RoughnessError):
            RoughnessField.from_config(raster, self.domain)
        field = RoughnessField.from_config(raster, self.domain,
            dataset=make_dataset([[0.04]*6 for _ in range(6)]), coordinate_system=CRS)
        self.assertAlmostEqual(field.values[0,0], 0.04)

    def test_alias(self):
        self.assertIs(ResistanceField, RoughnessField)


if __name__ == "__main__":
    unittest.main()
