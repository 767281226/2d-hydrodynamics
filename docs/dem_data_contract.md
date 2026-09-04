# DEM 数据契约 V1.0

## 1. 范围

本契约定义原始 DEM 到二维模型地形字段之间的数据边界。它不实现二维水动力
Solver、CRS 重投影、GeoTIFF 读取、NoData 填补或重采样算法。

数据流固定为：

~~~
原始 DEM 文件
    ↓
DEM Reader
    ↓
DEM Dataset / DEMMetadata
    ↓
TerrainMapper
    ↓
TerrainField
    ↓
Solver
~~~

Solver 只接收 TerrainField，不应依赖 GeoTIFF、GDAL、Rasterio、CRS、原始 DEM
分辨率或原始栅格尺寸。

## 2. DEM 输入文件

用户通过 terrain.file 指定 DEM 文件路径。路径是描述信息，当前配置加载器和
数据契约不会打开文件。未来 Reader 应支持并报告实际格式、波段、尺寸、仿射
变换、CRS、NoData 和高程统计。

当前真实样本为 data/dem/DEMn_1m.tif：GeoTIFF、26,677 × 36,813、单波段
float32、1 m × 1 m、EPSG:4548、NoData=-32767；完整体检见
dem_inspection_report.md。该 TIFF 不属于本轮提交对象。

## 3. DEMMetadata

hydrodynamics.dem_contract.DEMMetadata 是依赖无关的不可变元数据结构，至少包含：

| 字段 | 类型 | 含义 |
|---|---|---|
| width / height | int | 栅格列数/行数，必须 > 0 |
| band_count | int | 波段数，必须 >= 1 |
| dtype | string | 像元数据类型，不能为空 |
| xmin / xmax / ymin / ymax | number | 栅格空间外边界 |
| pixel_size_x / pixel_size_y | number | X/Y 像元尺寸，必须 > 0 |
| transform | 6 或 9 个有限数值 | affine/matrix 变换 |
| crs | string 或 None | 水平 CRS；未知时为 None |
| horizontal_unit | string 或 None | 水平坐标单位 |
| nodata_value | number 或 None | 明确的 NoData 值；None 不是默认值 |
| vertical_unit | string 或 None | 垂直单位 |
| vertical_datum | string 或 None | 垂直基准；未知时必须 None |
| elevation_type | string 或 None | DEM/DTM/DSM 类型；未知时必须 None |
| area_or_point | string 或 None | 文件原始 AREA_OR_POINT 信息 |
| valid_pixel_count | int 或 None | 选定波段有效像元数 |
| nodata_pixel_count | int 或 None | 选定波段 NoData 像元数 |
| valid_coverage_ratio | number 或 None | 数据集级有效像元比例 |

valid_coverage_ratio 是整个选定波段的像元比例：
valid_pixel_count / (width × height)；代码也提供等价的 `valid_ratio` 和
`dataset_valid_ratio` 只读别名，统计未扫描时返回 `None`。它不能代替映射阶段的
逐计算单元面积比例。

vertical_datum 和 elevation_type 不得根据文件名、位置或数值范围猜测。

## 4. DEMReader

DEMReader 是未来具体文件读取器应实现的 Protocol：

- read_metadata(source) -> DEMMetadata
- read_elevation(source) -> 二维高程序列

Reader 负责读取格式、几何、CRS、NoData 和原始高程，不负责模型网格映射。
当前 PlaceholderDEMReader 只抛出明确的 NotImplementedError；没有 Rasterio/GDAL
作为核心运行时强依赖。

## 5. DEMValidator

DEMValidator.validate() 只验证 DEMMetadata，不打开文件、不替换数据：

- width > 0、height > 0、band_count >= 1；
- pixel_size_x > 0、pixel_size_y > 0；
- 边界有序且所有数值有限；
- transform 为 6/9 个有限数值；
- require_projected_crs=True 时必须有 CRS；
- 默认要求显式 nodata_value，不假设任何 sentinel；
- 可选检查 valid_pixel_count、nodata_pixel_count 和 valid_ratio。

validate_for_model() 是进入模型映射前的严格检查入口。它仍不验证 DEM 是否覆盖
具体模型域；覆盖和像元元数据比较属于未来 TerrainMapper。调用方可选择
`require_vertical_datum=True`，在垂直基准未确认时明确失败。

## 6. CRS

模型 CRS 与 DEM CRS 必须独立记录。DEM CRS 缺失时不能猜测；如果模型要求投影
坐标，Validator 应明确失败。CRS 不一致时由数据准备/TerrainMapper 层进入未来
重投影流程，Solver 不处理 CRS。

当前样本文件内嵌 WKT/GeoKey 明确包含 EPSG:4548，水平单位为 metre；这不表示
垂直基准已知。

## 7. 高程基准和单位

当前样本明确提供：

- 垂直单位：metre（GeoKey EPSG:9001）；
- 波段单位字段：metre。

当前样本无法确定：

- 垂直 CRS；
- vertical datum；
- 椭球高、正常高或正高；
- geoid、EGM96、EGM2008 或国家高程基准关系。

DEM 高程、初始水位、边界水位必须使用一致的垂直基准，否则可能产生系统水深
偏差。禁止自动加减常数或依据数值范围转换。

## 8. 两种有效覆盖率

必须区分：

### 8.1 DEM 对模型计算区域的覆盖

模型区域必须被 DEM 的有效空间范围完全包含。超出或真实缺失必须失败；不能
用边缘值、0、最小值、最大值或自动外推。

### 8.2 单个计算单元的有效覆盖率

对于一个模型单元：

~~~
valid_coverage_ratio =
    有效 DEM 与单元的重叠面积 / 单元面积
~~~

area_weighted_mean 使用有效重叠面积：

~~~
z_cell = Σ(Ak × zk) / Σ(Ak)
~~~

其中 Ak 是有效 DEM 像元与单元的重叠面积，zk 是有效高程；映射结果还应记录
valid_area 和该单元 coverage_ratio。

数据集级 valid_coverage_ratio 不能直接当作每个计算单元的 coverage_ratio。

## 9. NoData

当前配置为兼容已有 V1.0 接口，保留 `terrain.nodata`（数值）与
`terrain.nodata_strategy`（策略）两个平级字段；嵌套 `nodata: {strategy, ...}`
形式暂不启用，避免破坏既有配置。

NoDataStrategy 保持 error、nearest、interpolate：

- error：目标单元无法得到可靠高程时立即失败；
- nearest：未来邻近有效值填补，当前 NOT IMPLEMENTED；
- interpolate：未来空间插值，当前 NOT IMPLEMENTED。

NoData 不得自动变成 0、最小值、最大值、边缘值，也不得静默忽略。
terrain.min_valid_coverage 当前为可选接口字段，默认 null；null 表示阈值尚未
冻结，不执行覆盖率门槛判断。显式值只做 0～1 范围校验。模型配置中的
`vertical_datum_required` 默认 true，只表达运行前置要求，不提供具体基准名称。

## 10. DEM → Model Grid

V1.0 计算网格由 domain 的 xmin/xmax/ymin/ymax、dx/dy 定义，cell-centered、
regular rectangular structured grid。nx、ny 由程序内部计算。

resampling.strategy 的固定接口值：

- auto：DEM 更细 → area_weighted_mean；
- 同分辨率且 CRS、范围、像元完全对齐 → direct；
- DEM 更粗 → bilinear；
- 一轴更细、一轴更粗或同分辨率未验证对齐 → 不猜测，报错。

本契约只定义策略和元数据条件，不执行上述算法。

## 11. TerrainField

TerrainField 保存与模型网格一一对应的：

- elevation，按 [j][i] 或概念上的 [j, i] 访问；
- nx、ny、dx、dy、xmin、ymin；
- 可选 nodata_mask（True 表示源 NoData）；
- 可选 valid_mask（True 表示当前可靠）；
- 可选 coverage_ratio[j][i]，范围 0～1。

valid_mask 与 nodata_mask 在只提供一方时可以互相推导；两者同时提供时保留
独立语义，以允许未来“有填充值但可信度不足”的状态。TerrainField 不读取
DEM，不把 NoData sentinel 转成 elevation。

## 12. 错误处理

- 配置错误：由现有 Pydantic ConfigValidationError 报告；
- DEM 元数据错误：DEMValidationError，包含可读错误列表；
- 未声明 NoData：默认严格失败，不隐式假设；
- 缺 CRS（模型要求时）：明确失败；
- 目标域超出有效 DEM 覆盖：未来 Mapper 必须失败；
- 未实现 Reader、重投影、重采样或 nearest/interpolate：明确抛出
  NotImplementedError。

## 13. 当前已实现

- DEMMetadata 数据结构及基础字段校验；
- DEMValidator 基础元数据、CRS、NoData 和统计字段校验；
- TerrainField 的质量 mask/coverage 结构和形状校验；
- TerrainMapper 仍保持不产生伪造结果的占位边界；
- 使用真实 DEM 构造 metadata 并通过兼容性校验的独立验证。

## 14. 当前未实现

- 正式 GeoTIFF/DEM Reader；
- 实际 raster 数据读取封装；
- DEM 覆盖范围运行时检查；
- CRS 重投影；
- area_weighted_mean、direct、bilinear；
- nearest、interpolate；
- 任意二维水动力 Solver 或物理计算。

## 15. 后续扩展

- 在不改变 Solver 输入的前提下增加具体 Reader；
- 确认 min_valid_coverage 的默认阈值和失败/填补策略；
- 确认垂直基准元数据是否必须作为一次运行的前置条件；
- 确认 transform/bounds 一致性和像元对齐容差；
- 为 Raster 数据集、质量报告和结果写出增加独立适配器。
