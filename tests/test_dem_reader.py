from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hydrodynamics.dem_contract import (
    DEMBandCountError,
    DEMDataset,
    DEMFormatError,
    DEMReaderDependencyError,
    DEMReaderError,
    GeoTIFFDEMReader,
)


try:
    import rasterio as _rasterio_probe
except Exception:
    RASTERIO_AVAILABLE = False
else:
    RASTERIO_AVAILABLE = True


@unittest.skipUnless(RASTERIO_AVAILABLE, "rasterio is optional")
class GeoTIFFDEMReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        self.np = np
        self.rasterio = rasterio
        self.transform = from_origin(100.0, 206.0, 2.0, 3.0)

    def _write_fixture(self, directory: str, *, suffix: str = ".tif", count: int = 1) -> Path:
        path = Path(directory) / f"fixture{suffix}"
        data = self.np.array([[1.0, -9999.0], [self.np.nan, self.np.inf]], dtype="float32")
        with self.rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=2,
            height=2,
            count=count,
            dtype="float32",
            crs="EPSG:4326",
            transform=self.transform,
            nodata=-9999.0,
        ) as dataset:
            for band in range(1, count + 1):
                dataset.write(data if band == 1 else data + band, band)
            dataset.set_band_unit(1, "metre")
            dataset.update_tags(AREA_OR_POINT="Point")
        return path

    def test_tif_metadata_and_tiff_extension(self) -> None:
        reader = GeoTIFFDEMReader()
        with tempfile.TemporaryDirectory() as directory:
            tif = self._write_fixture(directory)
            tiff = self._write_fixture(directory, suffix=".tiff")
            for path in (tif, tiff):
                metadata = reader.read_metadata(path)
                self.assertEqual((metadata.width, metadata.height, metadata.band_count), (2, 2, 1))
                self.assertEqual(metadata.dtype, "float32")
                self.assertEqual((metadata.pixel_size_x, metadata.pixel_size_y), (2.0, 3.0))
                self.assertEqual((metadata.xmin, metadata.xmax, metadata.ymin, metadata.ymax), (100.0, 104.0, 200.0, 206.0))
                self.assertIsNotNone(metadata.crs)
                self.assertEqual(metadata.nodata_value, -9999.0)
                self.assertEqual(metadata.vertical_unit, "metre")
                self.assertIsNone(metadata.vertical_datum)
                self.assertIsNone(metadata.elevation_type)
                self.assertEqual(metadata.area_or_point, "Point")

    def test_multiband_is_rejected_without_guessing(self) -> None:
        reader = GeoTIFFDEMReader()
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_fixture(directory, count=2)
            with self.assertRaises(DEMBandCountError) as raised:
                reader.read_metadata(path)
            self.assertIn("exactly one band", str(raised.exception))
            with self.assertRaises(DEMBandCountError):
                reader.read_elevation(path)

    def test_raw_values_and_masks_preserve_nodata_and_nonfinite_values(self) -> None:
        reader = GeoTIFFDEMReader()
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_fixture(directory)
            dataset = reader.read_dataset(path)
            self.assertIsInstance(dataset, DEMDataset)
            self.assertEqual(dataset.shape, (2, 2))
            self.assertEqual(dataset.elevation[0, 1], -9999.0)
            self.assertTrue(math.isnan(dataset.elevation[1, 0]))
            self.assertTrue(math.isinf(dataset.elevation[1, 1]))
            self.assertTrue(dataset.valid_mask[0, 0])
            self.assertFalse(dataset.valid_mask[0, 1])
            self.assertTrue(dataset.nodata_mask[0, 1])
            self.assertTrue(dataset.nonfinite_mask[1, 0])
            self.assertTrue(dataset.nonfinite_mask[1, 1])
            self.assertEqual(dataset.metadata.valid_pixel_count, 1)
            self.assertEqual(dataset.metadata.nodata_pixel_count, 1)
            self.assertEqual(dataset.metadata.nonfinite_pixel_count, 2)
            self.assertNotEqual(dataset.elevation[0, 1], 0.0)

    def test_missing_file_and_unsupported_extension_fail_clearly(self) -> None:
        reader = GeoTIFFDEMReader()
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.tif"
            with self.assertRaises(DEMReaderError):
                reader.read_metadata(missing)
            unsupported = Path(directory) / "input.asc"
            unsupported.write_text("not a raster", encoding="utf-8")
            with self.assertRaises(DEMFormatError):
                reader.read_metadata(unsupported)

    def test_legacy_read_elevation_returns_raw_sequence(self) -> None:
        reader = GeoTIFFDEMReader()
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_fixture(directory)
            values = reader.read_elevation(path)
            self.assertEqual(values[0, 1], -9999.0)
            self.assertTrue(math.isnan(values[1, 0]))

    def test_missing_optional_dependency_has_clear_error(self) -> None:
        reader = GeoTIFFDEMReader()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.tif"
            path.write_bytes(b"not-used")
            def missing_dependency() -> object:
                raise DEMReaderDependencyError("rasterio is unavailable")
            with patch.object(GeoTIFFDEMReader, "_rasterio", staticmethod(missing_dependency)):
                with self.assertRaises(DEMReaderDependencyError):
                    reader.read_metadata(path)

    def test_reader_does_not_depend_on_model_mapping(self) -> None:
        reader = GeoTIFFDEMReader()
        self.assertEqual(reader.SUPPORTED_EXTENSIONS, frozenset({".tif", ".tiff"}))


class OptionalDependencyErrorTests(unittest.TestCase):
    def test_dependency_error_type_is_exported(self) -> None:
        self.assertTrue(issubclass(DEMReaderDependencyError, ImportError))


if __name__ == "__main__":
    unittest.main()
