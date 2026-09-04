# 输入数据格式约定

当前 `load_config` 只校验文件引用字符串，不打开 GeoTIFF/CSV，也不执行重采样
或 NoData 插值。下列内容是未来数据读取层需要遵循的接口约定。

## Case 路径

`config.yaml` 中的相对路径以 case 目录为基准。未来数据准备层应解析路径并在
文件缺失时报告完整路径；当前加载器只要求路径是非空字符串。

## 计算区域和结构化网格

V1.0 的计算区域由 `domain.xmin`、`domain.xmax`、`domain.ymin`、`domain.ymax`
（单位 m）显式定义。`domain.dx`、`domain.dy`（单位 m）分别是 x/y 方向网格
尺寸，允许不同，且必须在 30～100 m（含）范围内。

区域长度必须能被对应步长整除。内部网格数量由程序计算：

```text
nx = (xmax - xmin) / dx
ny = (ymax - ymin) / dy
```

`nx`、`ny` 不是用户输入字段，也不应写入 `config.yaml`。栅格读取层未来还需
检查范围、分辨率、尺寸、CRS 和 NoData 元数据的一致性。

## 栅格输入

- 地形高程：米（`m`）。
- 初始水深：米（`m`）。
- 初始水位：米（`m`），参考垂向基准待确定。
- Manning 糙率：无量纲。
- 初始速度 x/y：米每秒（`m/s`），接口已预留但暂未实现。

`terrain.nodata` 可指定栅格 NoData 数值。`terrain.nodata_strategy` 默认值为
`error`：策略枚举还包含 `nearest` 和 `interpolate`，用于未来扩展；本阶段不
实现任何替换或插值算法。

## DEM 映射策略

`terrain.resampling.strategy` 的可选值为 `auto`、`area_weighted_mean`、
`direct` 和 `bilinear`，默认 `auto`。固定判定规则、覆盖范围要求和
`TerrainField`/`TerrainMapper` 接口见 [terrain_mapping.md](terrain_mapping.md)。
当前只保存配置和接口，不读取 DEM、不解析 GeoTIFF，也不执行重采样。

## CSV 时间序列

默认列名为 `time,value`，可通过 `time_column` 和 `value_column` 覆盖。文件应
有表头，时间为递增数值，默认时间单位为秒（`s`），相对于
`model.start_time` 的时间基准和插值/外推规则待后续确定。

值单位由所属字段决定：

- discharge 边界：立方米每秒（`m³/s`）；
- water_level 边界：米（`m`）；
- rainfall：毫米每小时（`mm/h`，默认）。

重复时间、缺失值、插值和外推策略尚未实现。

## DEM 数据契约

原始 DEM 的标准数据流是：

```text
原始 DEM 文件 → DEMReader → DEMMetadata → TerrainMapper → TerrainField → Solver
```

`DEMMetadata` 保存尺寸、波段、dtype、extent、像元大小、transform、CRS、NoData
和垂直单位等信息；`vertical_datum` 与 `elevation_type` 可以是 `None`，不得猜测。
`DEMValidator` 在模型要求投影 CRS 或显式 NoData 时拒绝缺失元数据。当前没有正式
Reader，核心包也不依赖 Rasterio/GDAL。

必须区分两种覆盖率：DEM 有效空间范围对模型区域的覆盖，以及每个模型单元的
`coverage_ratio = 有效 DEM 重叠面积 / 单元面积`。`area_weighted_mean` 只对有效
像元按重叠面积计算，并应记录 `valid_area` 与单元 coverage；具体阈值由
`terrain.min_valid_coverage` 预留字段表达，默认 `null`，尚未冻结。

NoData 不得变成 0、最小值、最大值、边缘值或被静默忽略。`error` 表示无法取得
可靠高程时失败；`nearest`/`interpolate` 仅声明，当前未实现。

真实 DEM 的逐文件体检见 [dem_inspection_report.md](dem_inspection_report.md)。
