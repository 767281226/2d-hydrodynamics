from __future__ import annotations

import math
import unittest

from hydrodynamics.config import DomainConfig, NoDataStrategy, ResamplingStrategy
from hydrodynamics.dem_contract import DEMDataset, DEMMetadata
from hydrodynamics.terrain_mapping import TerrainMapper, TerrainMappingError


CRS = "EPSG:4548"


def make_dataset(
    values: list[list[float]],
    *,
    pixel_size_x: float,
    pixel_size_y: float | None = None,
    xmin: float = 0.0,
    ymin: float = 0.0,
    crs: str | None = CRS,
    nodata_value: float | None = -9999.0,
    invalid: set[tuple[int, int]] | None = None,
    nonfinite: set[tuple[int, int]] | None = None,
) -> DEMDataset:
    pixel_size_y = pixel_size_y if pixel_size_y is not None else pixel_size_x
    height = len(values)
    width = len(values[0])
    invalid = invalid or set()
    nonfinite = nonfinite or set()
    top = ymin + height * pixel_size_y
    metadata = DEMMetadata(
        width=width,
        height=height,
        band_count=1,
        dtype="float64",
        xmin=xmin,
        xmax=xmin + width * pixel_size_x,
        ymin=ymin,
        ymax=top,
        pixel_size_x=pixel_size_x,
        pixel_size_y=pixel_size_y,
        transform=(pixel_size_x, 0.0, xmin, 0.0, -pixel_size_y, top),
        crs=crs,
        horizontal_unit="metre",
        nodata_value=nodata_value,
    )
    valid_mask: list[list[bool]] = []
    nodata_mask: list[list[bool]] = []
    nonfinite_mask: list[list[bool]] = []
    for row_index, row in enumerate(values):
        valid_row: list[bool] = []
        nodata_row: list[bool] = []
        nonfinite_row: list[bool] = []
        for column_index, value in enumerate(row):
            is_nonfinite = (row_index, column_index) in nonfinite or not math.isfinite(float(value))
            is_nodata = (row_index, column_index) in invalid
            if nodata_value is not None and not is_nonfinite and value == nodata_value:
                is_nodata = True
            nonfinite_row.append(is_nonfinite)
            nodata_row.append(is_nodata)
            valid_row.append(not is_nonfinite and not is_nodata)
        valid_mask.append(valid_row)
        nodata_mask.append(nodata_row)
        nonfinite_mask.append(nonfinite_row)
    return DEMDataset(
        metadata=metadata,
        elevation=values,
        valid_mask=valid_mask,
        nodata_mask=nodata_mask,
        nonfinite_mask=nonfinite_mask,
    )


def fine_dataset() -> DEMDataset:
    values = [[float(row * 100 + column) for column in range(6)] for row in range(6)]
    return make_dataset(values, pixel_size_x=10.0)


class TerrainMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapper = TerrainMapper()
        self.domain = DomainConfig(xmin=0, xmax=60, ymin=0, ymax=60, dx=30, dy=30)

    def test_area_weighted_mean_uses_overlap_and_north_up_orientation(self) -> None:
        field = self.mapper.map_dataset(
            fine_dataset(), self.domain, coordinate_system=CRS, strategy="area_weighted_mean"
        )
        self.assertEqual(field.shape, (2, 2))
        self.assertEqual(field.elevation[0, 0], 401.0)
        self.assertEqual(field.elevation[0, 1], 404.0)
        self.assertEqual(field.elevation[1, 0], 101.0)
        self.assertEqual(field.elevation[1, 1], 104.0)
        self.assertEqual(field.coverage_ratio[0, 0], 1.0)
        self.assertEqual(field.valid_area[0, 0], 900.0)

    def test_area_weighted_mean_records_partial_coverage(self) -> None:
        dataset = make_dataset(
            [[float(row * 100 + column) for column in range(6)] for row in range(6)],
            pixel_size_x=10.0,
            invalid={(5, 0)},
        )
        field = self.mapper.map_dataset(dataset, self.domain, coordinate_system=CRS, strategy="area_weighted_mean")
        self.assertAlmostEqual(field.coverage_ratio[0, 0], 8 / 9)
        self.assertAlmostEqual(field.valid_area[0, 0], 800.0)
        self.assertAlmostEqual(field.elevation[0, 0], (401 * 9 - 500) / 8)

    def test_area_weighted_mean_threshold_and_empty_cell_fail(self) -> None:
        partial = make_dataset(
            [[float(row * 100 + column) for column in range(6)] for row in range(6)],
            pixel_size_x=10.0,
            invalid={(5, 0)},
        )
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(
                partial,
                self.domain,
                coordinate_system=CRS,
                strategy=ResamplingStrategy.AREA_WEIGHTED_MEAN,
                min_valid_coverage=0.9,
            )
        empty = make_dataset(
            [[float(row * 100 + column) for column in range(6)] for row in range(6)],
            pixel_size_x=10.0,
            invalid={(row, column) for row in (3, 4, 5) for column in (0, 1, 2)},
        )
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(empty, self.domain, coordinate_system=CRS, strategy="area_weighted_mean")

    def test_direct_reverses_north_up_source_rows(self) -> None:
        dataset = make_dataset([[1.0, 2.0], [3.0, 4.0]], pixel_size_x=30.0)
        field = self.mapper.map_dataset(dataset, self.domain, coordinate_system=CRS, strategy="direct")
        self.assertEqual(field.elevation, ((3.0, 4.0), (1.0, 2.0)))
        self.assertEqual(field.coverage_ratio, ((1.0, 1.0), (1.0, 1.0)))

    def test_direct_requires_exact_grid_alignment(self) -> None:
        dataset = make_dataset([[1.0, 2.0], [3.0, 4.0]], pixel_size_x=30.0, xmin=1.0)
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(dataset, self.domain, coordinate_system=CRS, strategy="direct")

    def test_bilinear_uses_four_source_centers_without_extrapolation(self) -> None:
        values = []
        for row in range(4):
            y = 240.0 - (row + 0.5) * 60.0
            values.append([x + 2.0 * y for x in (30.0, 90.0, 150.0, 210.0)])
        dataset = make_dataset(values, pixel_size_x=60.0, xmin=0.0, ymin=0.0)
        domain = DomainConfig(xmin=60, xmax=180, ymin=60, ymax=180, dx=30, dy=30)
        field = self.mapper.map_dataset(dataset, domain, coordinate_system=CRS, strategy="bilinear")
        self.assertEqual(field.elevation[0, 0], 225.0)
        self.assertEqual(field.elevation[0, 3], 315.0)
        self.assertEqual(field.elevation[3, 0], 405.0)
        self.assertTrue(all(value == 1.0 for row in field.coverage_ratio for value in row))
        edge_domain = DomainConfig(xmin=0, xmax=120, ymin=60, ymax=180, dx=30, dy=30)
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(dataset, edge_domain, coordinate_system=CRS, strategy="bilinear")

    def test_bilinear_requires_all_four_neighbors_valid(self) -> None:
        values = [[float(row * 10 + column) for column in range(4)] for row in range(4)]
        dataset = make_dataset(values, pixel_size_x=60.0, invalid={(1, 1)})
        domain = DomainConfig(xmin=60, xmax=180, ymin=60, ymax=180, dx=30, dy=30)
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(dataset, domain, coordinate_system=CRS, strategy="bilinear")

    def test_auto_dispatches_and_rejects_undefined_cases(self) -> None:
        fine = self.mapper.map_dataset(fine_dataset(), self.domain, coordinate_system=CRS)
        self.assertEqual(fine.elevation[0, 0], 401.0)
        coarse_values = [[float(row * 10 + column) for column in range(4)] for row in range(4)]
        coarse = make_dataset(coarse_values, pixel_size_x=60.0)
        coarse_domain = DomainConfig(xmin=60, xmax=180, ymin=60, ymax=180, dx=30, dy=30)
        self.assertEqual(
            self.mapper.map_dataset(coarse, coarse_domain, coordinate_system=CRS).shape,
            (4, 4),
        )
        direct = make_dataset([[1.0, 2.0], [3.0, 4.0]], pixel_size_x=30.0)
        self.assertEqual(self.mapper.map_dataset(direct, self.domain, coordinate_system=CRS).elevation[0, 0], 3.0)
        self.assertEqual(
            self.mapper.map_dataset(direct, self.domain, coordinate_system=CRS, strategy="auto").elevation[0, 0],
            3.0,
        )
        mixed = make_dataset([[1.0] * 4 for _ in range(4)], pixel_size_x=10.0, pixel_size_y=60.0)
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(mixed, coarse_domain, coordinate_system=CRS, strategy="auto")

    def test_common_crs_geometry_and_strategy_errors(self) -> None:
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(fine_dataset(), self.domain, coordinate_system=None)
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(
                make_dataset([[1.0] * 6 for _ in range(6)], pixel_size_x=10.0, crs="EPSG:4326"),
                self.domain,
                coordinate_system=CRS,
            )
        rotated = fine_dataset()
        rotated_metadata = rotated.metadata.__class__(**{**rotated.metadata.__dict__, "transform": (10.0, 0.1, 0.0, 0.0, -10.0, 60.0)})
        rotated = DEMDataset(
            metadata=rotated_metadata,
            elevation=rotated.elevation,
            valid_mask=rotated.valid_mask,
            nodata_mask=rotated.nodata_mask,
            nonfinite_mask=rotated.nonfinite_mask,
        )
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(rotated, self.domain, coordinate_system=CRS)
        outside = DomainConfig(xmin=-30, xmax=30, ymin=0, ymax=60, dx=30, dy=30)
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(fine_dataset(), outside, coordinate_system=CRS)
        with self.assertRaises(NotImplementedError):
            self.mapper.map_dataset(
                fine_dataset(), self.domain, coordinate_system=CRS, nodata_strategy=NoDataStrategy.NEAREST
            )

    def test_zero_is_a_valid_elevation_and_alias_works(self) -> None:
        dataset = make_dataset([[0.0] * 6 for _ in range(6)], pixel_size_x=10.0)
        field = self.mapper.map_field(dataset, self.domain, coordinate_system=CRS, strategy="area_weighted_mean")
        self.assertEqual(field.elevation[0, 0], 0.0)



    def test_exact_nodata_sentinel_does_not_discard_nearby_value(self) -> None:
        dataset = make_dataset([[-9998.999] * 6 for _ in range(6)], pixel_size_x=10.0)
        field = self.mapper.map_dataset(dataset, self.domain, coordinate_system=CRS, strategy='area_weighted_mean')
        self.assertAlmostEqual(field.elevation[0, 0], -9998.999)

    def test_crs_matching_uses_outer_authority_not_nested_unit(self) -> None:
        wkt = 'PROJCS["demo",AUTHORITY["EPSG","4548"],UNIT["metre",1,AUTHORITY["EPSG","9001"]]]'
        dataset = make_dataset([[1.0] * 6 for _ in range(6)], pixel_size_x=10.0, crs=wkt)
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(dataset, self.domain, coordinate_system='EPSG:9001')
        field = self.mapper.map_dataset(dataset, self.domain, coordinate_system='EPSG:4548', strategy='area_weighted_mean')
        self.assertEqual(field.elevation[0, 0], 1.0)

    def test_geographic_crs_is_rejected_for_metric_mapping(self) -> None:
        dataset = make_dataset([[1.0] * 6 for _ in range(6)], pixel_size_x=10.0, crs='EPSG:4326')
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(dataset, self.domain, coordinate_system='EPSG:4326')

    def test_non_string_model_crs_fails_clearly(self) -> None:
        with self.assertRaises(TerrainMappingError):
            self.mapper.map_dataset(fine_dataset(), self.domain, coordinate_system=4548)


if __name__ == "__main__":
    unittest.main()
