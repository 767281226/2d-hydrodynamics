# Configuration validation

Validation is performed before any future solver is called.

## Layers

1. YAML syntax is parsed with `yaml.safe_load`.
2. Pydantic models enforce types, enums, required fields, ranges, and rejection
   of unknown keys (`extra="forbid"`).
3. Model validators enforce cross-field rules such as selected source fields,
   unique boundary IDs, time ordering, and matching output intervals.
4. The YAML loader does not open referenced files. When explicitly invoked,
   `GeoTIFFDEMReader` checks extension, existence, decodability, and single-band
   structure.
5. `TerrainField` validates `(ny, nx)` elevation/mask shape. The in-memory
   `TerrainMapper.map_dataset` additionally validates CRS, north-up geometry,
   domain containment, strategy conditions, NoData, and coverage thresholds.

Use `ConfigLoadError` for missing/unreadable/invalid-YAML files and
`ConfigValidationError` for a syntactically valid YAML document that violates
the schema. The latter includes a list of Pydantic error dictionaries in
`.errors` and a readable message such as:

```text
Invalid simulation configuration:
- terrain.file: file is required when terrain.type is 'raster'
```

No numerical or physical validation is claimed by this package yet.

## DEM contract validation

`DEMMetadata` 在构造时检查正尺寸、正像元大小、有限 extent/transform 和 0～1
的数据集有效率；`DEMValidator` 可按模型要求检查 CRS、显式 NoData 和统计字段。
缺失 `vertical_datum` 不会被替换为默认值；当调用方传入
`require_vertical_datum=True` 时会明确失败。

`TerrainField` 检查 elevation、mask 和 coverage 的 `(ny, nx)` 形状以及 coverage
范围。映射器对每个目标单元计算有效覆盖率；在 `error` 策略下无有效值或低于
显式 `min_valid_coverage` 时失败。

## Reader validation

`GeoTIFFDEMReader` 在读取前检查扩展名和文件存在性，打开后要求恰好一个波段，
并将 NoData sentinel、Rasterio mask 和 NaN/Inf 分开记录；非有限值优先归入
`nonfinite` 类别。原始值不会被替换为 0、边缘值或其他高程。

## Mapping validation boundary

`map_dataset` 只接受与模型相同的平面 CRS 和北向上、轴对齐 DEM。它不会执行 CRS
转换、外推或 NoData 填补；`nearest`/`interpolate` 会明确抛出
`NotImplementedError`。旧的 `TerrainMapper.map` 仍是配置占位接口。
