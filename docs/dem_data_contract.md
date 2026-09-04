# DEM 数据契约 V1.0

## 1. 范围与数据流

本契约定义原始 DEM、内存数据集、模型地形字段之间的边界。当前实现包括可选
单波段 GeoTIFF Reader，以及不依赖栅格库的 DEMDataset → TerrainField 映射。
仍不实现 CRS 重投影、NoData 填补、求解器或结果写出。

```
原始 DEM 文件
    ↓
DEM Reader（读取原始值、掩码、元数据）
    ↓
DEMDataset / DEMMetadata
    ↓
TerrainMapper.map_dataset
    ↓
TerrainField
    ↓
未来 Solver
```

Solver 只接收 `TerrainField`，不应依赖 GeoTIFF、GDAL、Rasterio、CRS、原始
DEM 分辨率或原始栅格尺寸。

## 2. DEM 输入文件与 Reader

用户通过 `terrain.file` 指定 DEM 路径。配置加载器只校验非空路径字符串；
需要读取文件时显式调用 `GeoTIFFDEMReader`。V1 Reader 只接受 `.tif`/`.tiff`
并拒绝多波段文件，不猜测高程波段。Rasterio 是可选依赖，不属于核心运行时依赖。

Reader 只读取格式、几何、CRS、NoData、原始高程和掩码；不重投影、不重采样、
不填补 NoData。缺少 Rasterio 时抛出 `DEMReaderDependencyError`。

仓库中的真实样本 `data/dem/DEMn_1m.tif` 仅用于本地只读兼容性检查，不属于
源代码提交对象。体检报告见 [dem_inspection_report.md](dem_inspection_report.md)。

## 3. DEMMetadata

`hydrodynamics.dem_contract.DEMMetadata` 是依赖无关的不可变元数据结构：

| 字段 | 类型 | 含义 |
|---|---|---|
| width / height | int | 栅格列/行数，必须 > 0 |
| band_count | int | 波段数，必须 >= 1；V1 Reader/Mapper 要求 1 |
| dtype | string | 像元类型，不能为空 |
| xmin / xmax / ymin / ymax | number | 栅格外边界 |
| pixel_size_x / pixel_size_y | number | x/y 像元尺寸，必须 > 0 |
| transform | 6 或 9 个有限数值 | 仿射/齐次变换 |
| crs | string 或 None | 水平 CRS；缺失时映射失败 |
| horizontal_unit | string 或 None | 水平坐标单位 |
| nodata_value | number 或 None | 源 NoData sentinel；None 不代表可猜测默认值 |
| vertical_unit | string 或 None | 垂直单位 |
| vertical_datum | string 或 None | 垂直基准；未知时保持 None |
| elevation_type | string 或 None | DEM/DTM/DSM 类型；未知时保持 None |
| area_or_point | string 或 None | 源文件标记 |
| valid_pixel_count | int 或 None | 有效像元统计 |
| nodata_pixel_count | int 或 None | NoData 像元统计 |
| nonfinite_pixel_count | int 或 None | NaN/Inf 像元统计 |
| valid_coverage_ratio | number 或 None | 数据集级有效像元比例 |

`vertical_datum` 和 `elevation_type` 不得根据文件名、位置或高程范围猜测。
数据集级 `valid_coverage_ratio` 不能替代映射阶段逐单元的面积覆盖率。

## 4. DEMDataset 与掩码

`DEMDataset` 保存 `DEMMetadata`、未修改的二维 `elevation` 以及：

- `valid_mask`：有限且不是 NoData 的有效像元；
- `nodata_mask`：源 mask 或 NoData sentinel；
- `nonfinite_mask`：NaN/Inf。

Reader 对非有限值优先归入 `nonfinite_mask`，三类统计互斥；原始 sentinel、
NaN、Inf 仍原样保留。容器和映射层都不把 NoData 变成 0、边缘值或任何其他高程。

## 5. DEMValidator

`DEMValidator` 只验证元数据，不打开文件、不修改值：

- width/height 为正，band_count 至少为 1；
- 像元大小为正；
- 边界和 transform 为有限且有序；
- `require_projected_crs=True` 时 CRS 不能为空；
- 默认要求明确 `nodata_value`，不假设 sentinel；
- 可选检查有效/NoData/非有限统计和比例；
- `require_vertical_datum=True` 时必须显式提供垂直基准。

`validate_for_model` 是进入模型前的严格元数据入口。映射器另外检查单波段、
北向上几何、CRS 相等和模型域边界。

## 6. CRS 与高程基准

模型 CRS 与 DEM CRS 独立记录。映射只接受相同的平面/投影 CRS；CRS 缺失、
不一致或为角度地理坐标时明确失败，不在本层转换。若数据供应方提供不同 CRS，
应在外部数据准备阶段完成重投影并重新生成符合契约的 DEMDataset。

水平 CRS 已知不等于垂直基准已知。DEM 高程、初始水位和边界水位必须使用一致的
垂直基准；`vertical_datum=None` 时禁止自动加减常数或依据数值猜测。
`model.vertical_datum_required` 默认 true 只表达运行前置要求，不指定具体基准。

## 7. 覆盖率定义

必须区分两种覆盖：

### 7.1 DEM 几何范围对模型区域的覆盖

模型区域必须完全位于 DEM 的几何边界内。越界、真实空间缺失、需要截断或外推
都会失败；只容忍约定的极小浮点误差。

### 7.2 单个计算单元的有效覆盖率

```text
coverage_ratio = 有效 DEM 与单元的重叠面积 / 单元面积
valid_area = 有效重叠面积（m²）
```

数据集级 `valid_coverage_ratio` 与单元级 `coverage_ratio` 不可混用。对于
`area_weighted_mean`，只用有效重叠面积计算高程；无有效面积时在 `error`
策略下失败。显式 `terrain.min_valid_coverage` 可设置 0～1 的单元阈值，默认
`null` 表示阈值尚未冻结。

## 8. DEM → Model Grid 映射

V1.0 网格由 `xmin/xmax/ymin/ymax`、`dx/dy` 定义，`nx/ny` 由程序计算，
数组方向为 `[j][i]`，`j=0` 在南侧。

### 8.1 area_weighted_mean（已实现）

DEM 在两轴不粗于模型网格且至少一轴更细时，按每个 DEM 像元与模型单元的真实
重叠面积计算 `Σ(Ak×zk)/Σ(Ak)`，并填充目标 `coverage_ratio`/ `valid_area`。
NoData、NaN、Inf 被跳过但不被替换；无有效面积或低于显式阈值时报错。

### 8.2 direct（已实现）

要求两轴分辨率、范围、像元边界和轴方向完全一致。源北向上栅格行序与目标数组
方向相反，因此输出行反转；不进行二次重采样。

### 8.3 bilinear（已实现）

DEM 在两轴不细于模型网格且至少一轴更粗时，以目标单元中心的四个源像元中心
计算双线性值。四邻域必须完整位于 DEM 内且全部有效；禁止外推。粗 DEM 插值到
细网格不产生新的真实精度。

### 8.4 auto（已实现）

`auto` 按两轴分辨率选择上述策略；同分辨率未完全对齐、混合细/粗方向时
显式报错，不猜测。

公开入口：

```python
from hydrodynamics import TerrainMapper
field = TerrainMapper().map_dataset(
    dataset, domain, coordinate_system="EPSG:4548", strategy="auto",
    nodata_strategy="error", min_valid_coverage=None,
)
```

`map_dataset_to_field` 是等价函数入口。两者都只接收内存 DEMDataset；现有
`TerrainMapper.map(domain, terrain)` 仍是兼容的配置占位接口。

## 9. TerrainField

`TerrainField` 是不可变、依赖无关的模型网格地形容器，保存 `elevation`、
`nx/ny`、`dx/dy`、`xmin/ymin`、`valid_mask`、`nodata_mask`、
`coverage_ratio`，并以 `valid_area = coverage_ratio × dx × dy` 提供有效面积。
所有二维形状都必须是 `(ny, nx)`。

## 10. 错误语义

- 配置错误：`ConfigValidationError`，包含字段路径和清晰原因；
- DEM 元数据错误：`DEMValidationError`；
- 文件/格式错误：`DEMReaderError`、`DEMFormatError`、
  `DEMBandCountError` 或缺依赖错误；
- 映射几何、CRS、覆盖或策略错误：`TerrainMappingError`；
- `nearest`/`interpolate`：声明为未来策略，调用抛 `NotImplementedError`。

## 11. 当前已实现

- 严格的 `DEMMetadata`、`DEMDataset` 和 `DEMValidator`；
- 可选 Rasterio 单波段 GeoTIFF Reader，保留原始值及三类掩码；
- `TerrainField` 形状、质量掩码和覆盖率校验；
- 纯 Python `area_weighted_mean`、`direct`、`bilinear` 和 `auto` 内存映射；
- 域边界、北向上几何、同 CRS、NoData/非有限值和显式覆盖阈值检查；
- 所有接口的清晰异常。

## 12. 当前未实现

- CRS 重投影、不同 CRS 的自动配准；
- nearest/interpolate NoData 填补；
- 大型栅格分块/窗口读取优化；
- DEM 路径自动解析和配置加载时文件存在性检查；
- 任何二维水动力 Solver、控制方程、离散格式、数值通量、时间积分和物理过程。

## 13. 后续扩展

- 在保持 `TerrainField` 契约不变的前提下增加其他 Reader；
- 评估分块读取和更低内存映射；
- 验证 `min_valid_coverage` 默认阈值及与未来填补策略的协同；
- 确认垂直基准元数据和运行前置条件；
- 增加独立结果写出和质量报告适配器。

\n
