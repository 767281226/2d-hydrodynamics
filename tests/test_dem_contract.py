from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

from hydrodynamics import DEMMetadata, DEMValidationError, DEMValidator
from hydrodynamics.config import DomainConfig, TerrainField


def make_metadata(**overrides: object) -> DEMMetadata:
    values: dict[str, object] = {
        "width": 10,
        "height": 20,
        "band_count": 1,
        "dtype": "float32",
        "xmin": 0.0,
        "xmax": 10.0,
        "ymin": 0.0,
        "ymax": 20.0,
        "pixel_size_x": 1.0,
        "pixel_size_y": 1.0,
        "transform": (1.0, 0.0, 0.0, 0.0, -1.0, 20.0),
        "crs": "EPSG:4548",
        "horizontal_unit": "metre",
        "nodata_value": -32767.0,
        "vertical_unit": "metre",
        "vertical_datum": None,
        "elevation_type": None,
        "area_or_point": "Point",
        "valid_pixel_count": 100,
        "nodata_pixel_count": 100,
        "valid_coverage_ratio": 0.5,
    }
    values.update(overrides)
    return DEMMetadata(**values)


class DEMContractTests(unittest.TestCase):
    def test_valid_metadata_and_unknown_fields(self) -> None:
        metadata = make_metadata()
        self.assertEqual(metadata.pixel_count, 200)
        self.assertIsNone(metadata.vertical_datum)
        self.assertIsNone(metadata.elevation_type)
        self.assertEqual(metadata.valid_ratio, 0.5)
        self.assertNotEqual(metadata.nodata_value, 0)

    def test_valid_ratio_is_derived_when_ratio_is_not_supplied(self) -> None:
        metadata = make_metadata(valid_coverage_ratio=None)
        self.assertEqual(metadata.valid_ratio, 0.5)

    def test_invalid_dimensions_and_pixel_size_fail(self) -> None:
        with self.assertRaises(ValueError):
            make_metadata(width=0)
        with self.assertRaises(ValueError):
            make_metadata(pixel_size_x=0)
        with self.assertRaises(ValueError):
            make_metadata(pixel_size_y=-1)

    def test_missing_crs_fails_when_model_requires_projected_crs(self) -> None:
        metadata = make_metadata(crs=None)
        with self.assertRaises(DEMValidationError) as raised:
            DEMValidator().validate(metadata, require_projected_crs=True)
        self.assertIn("crs is required", str(raised.exception))

    def test_vertical_datum_requirement_is_explicit(self) -> None:
        metadata = make_metadata()
        with self.assertRaises(DEMValidationError) as raised:
            DEMValidator().validate(metadata, require_vertical_datum=True)
        self.assertIn("vertical_datum is required", str(raised.exception))
        confirmed = make_metadata(vertical_datum="confirmed datum")
        self.assertIs(DEMValidator().validate(confirmed, require_vertical_datum=True), confirmed)

    def test_missing_nodata_is_not_assumed(self) -> None:
        metadata = make_metadata(nodata_value=None)
        with self.assertRaises(DEMValidationError) as raised:
            DEMValidator().validate(metadata)
        self.assertIn("no default is assumed", str(raised.exception))
        self.assertIs(DEMValidator().validate(metadata, require_nodata=False), metadata)

    def test_valid_coverage_ratios_zero_half_and_full(self) -> None:
        for ratio, valid, nodata in ((0.0, 0, 200), (0.5, 100, 100), (1.0, 200, 0)):
            metadata = make_metadata(
                valid_pixel_count=valid,
                nodata_pixel_count=nodata,
                valid_coverage_ratio=ratio,
            )
            self.assertIs(DEMValidator().validate(metadata, require_statistics=True), metadata)

    def test_invalid_coverage_ratio_fails(self) -> None:
        with self.assertRaises(ValueError):
            make_metadata(valid_coverage_ratio=1.1)

    def test_terrain_field_quality_information_and_zero_elevation(self) -> None:
        domain = DomainConfig(xmin=0, xmax=100, ymin=0, ymax=100, dx=50, dy=50)
        field = TerrainField.from_domain(
            domain,
            [[0.0, 1.0], [2.0, 3.0]],
            valid_mask=[[True, False], [True, False]],
            coverage_ratio=[[1.0, 0.5], [0.0, 1.0]],
        )
        self.assertEqual(field.valid_mask[0, 0], True)
        self.assertEqual(field.nodata_mask[0, 0], False)
        self.assertEqual(field.nodata_mask[0, 1], True)
        self.assertEqual(field.coverage_ratio[0, 1], 0.5)
        self.assertEqual(field.elevation[0, 0], 0.0)

    def test_terrain_field_valid_area_is_derived_from_coverage(self) -> None:
        domain = DomainConfig(xmin=0, xmax=100, ymin=0, ymax=100, dx=50, dy=50)
        field = TerrainField.from_domain(
            domain, [[1.0, 2.0], [3.0, 4.0]], coverage_ratio=[[1.0, 0.5], [0.0, 1.0]]
        )
        self.assertEqual(field.valid_area[0, 0], 2500.0)
        self.assertEqual(field.valid_area[0, 1], 1250.0)
        self.assertEqual(field.valid_area[1, 0], 0.0)

    def test_terrain_field_rejects_invalid_coverage_shape_or_range(self) -> None:
        domain = DomainConfig(xmin=0, xmax=100, ymin=0, ymax=100, dx=50, dy=50)
        with self.assertRaises(ValueError):
            TerrainField.from_domain(domain, [[1.0, 2.0], [3.0, 4.0]], coverage_ratio=[[1.0]])
        with self.assertRaises(ValueError):
            TerrainField.from_domain(
                domain, [[1.0, 2.0], [3.0, 4.0]], coverage_ratio=[[1.0, 2.0], [0.0, 1.0]]
            )

    @unittest.skipUnless(
        importlib.util.find_spec("rasterio") and os.environ.get("RUN_REAL_DEM_TEST") == "1",
        "real DEM test is opt-in; use a small temporary fixture by default",
    )
    def test_real_dem_metadata_is_compatible(self) -> None:
        import rasterio

        path = Path(__file__).parents[1] / "data" / "dem" / "DEMn_1m.tif"
        if not path.exists():
            self.skipTest("real DEM is not present in this checkout")
        with rasterio.open(path) as dataset:
            metadata = DEMMetadata(
                width=dataset.width,
                height=dataset.height,
                band_count=dataset.count,
                dtype=dataset.dtypes[0],
                xmin=dataset.bounds.left,
                xmax=dataset.bounds.right,
                ymin=dataset.bounds.bottom,
                ymax=dataset.bounds.top,
                pixel_size_x=dataset.res[0],
                pixel_size_y=dataset.res[1],
                transform=tuple(dataset.transform),
                crs=dataset.crs.to_wkt() if dataset.crs else None,
                horizontal_unit=dataset.crs.linear_units if dataset.crs else None,
                nodata_value=dataset.nodata,
                vertical_unit="metre",
                vertical_datum=None,
                elevation_type=None,
                area_or_point=dataset.tags().get("AREA_OR_POINT"),
                valid_pixel_count=209366213,
                nodata_pixel_count=772694188,
                valid_coverage_ratio=209366213 / (dataset.width * dataset.height),
            )
        self.assertIs(DEMValidator().validate_for_model(metadata), metadata)
        self.assertIs(DEMValidator().validate(metadata, require_statistics=True), metadata)
        self.assertEqual(metadata.pixel_size_x, 1.0)
        self.assertEqual(metadata.nodata_value, -32767.0)


if __name__ == "__main__":
    unittest.main()
