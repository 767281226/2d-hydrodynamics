# V1.0 输入参数说明

`hydrodynamics.config.SimulationConfig` 是当前唯一权威配置 Schema。YAML 未知字段会被拒绝；
`load_config` 只解析和校验配置，不打开 GeoTIFF/CSV，也不启动 Solver。

状态说明：**已实现**表示配置层会解析、类型检查、枚举检查和范围检查；DEM 映射部分还包括
已实现的内存 `DEMDataset → TerrainField` 数据准备；**预留/暂未实现**表示字段已纳入接口，
但当前不会产生相应的物理计算、文件读取或结果输出。

## 一级模块

以下十个模块均为必填：`model`、`domain`、`terrain`、`initial_condition`、`roughness`、
`boundary`、`source`、`numerics`、`output`、`validation`。

## 参数明细

### model（配置解析与校验已实现）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `name` | string | — | — | 是 | 案例名称，不能为空。 |
| `description` | string/null | — | null | 否 | 案例说明；填写时不能为空。 |
| `gravity` | number | m/s² | 9.81 | 否 | 必须大于 0；当前仅保存，不执行方程。 |
| `start_time` | number | s | 0 | 否 | 必须 >= 0。 |
| `end_time` | number | s | — | 是 | 必须大于 `start_time`。 |
| `output_interval` | number/null | s | null | 否 | 兼容字段；填写时必须等于 `output.interval`。 |
| `coordinate_system` | string | CRS 标识 | — | 是 | 非空；格式和 CRS 重投影尚未冻结/实现。 |
| `units` | enum(`SI`) | — | `SI` | 否 | V1.0 目前只接受 `SI`。 |
| `vertical_datum_required` | boolean | — | true | 否 | 是否要求运行前确认地形和水位使用共同垂直基准；不填写具体基准。 |

### domain（配置解析、范围和整除校验已实现）

V1.0 是规则矩形、structured、cell-centered 网格。`xmin/xmax/ymin/ymax` 显式定义计算区域
边界（m）；`dx/dy` 分别定义 x/y 方向单元尺寸（m），允许不同，且各自必须在 30～100 m（含）
范围内。区域长度不能被对应步长整除时配置失败。

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `type` | enum(`structured`) | — | `structured` | 否 | V1.0 仅支持规则结构化网格。 |
| `xmin` | number | m | — | 是 | x 最小边界；必须小于 `xmax`。 |
| `xmax` | number | m | — | 是 | x 最大边界；必须大于 `xmin`。 |
| `ymin` | number | m | — | 是 | y 最小边界；必须小于 `ymax`。 |
| `ymax` | number | m | — | 是 | y 最大边界；必须大于 `ymin`。 |
| `dx` | number | m | — | 是 | x 方向尺寸；30 <= dx <= 100。 |
| `dy` | number | m | — | 是 | y 方向尺寸；30 <= dy <= 100，可不同于 `dx`。 |
| `nx` | internal integer | cell | 程序计算 | 禁止输入 | `nx = (xmax-xmin)/dx`，可从 `DomainConfig.nx` 读取。 |
| `ny` | internal integer | cell | 程序计算 | 禁止输入 | `ny = (ymax-ymin)/dy`，可从 `DomainConfig.ny` 读取。 |

### terrain（输入方式、NoData 和映射策略配置校验已实现）

`type: raster` 时 `file` 必填；`type: constant` 时 `elevation` 必填。`load_config` 只保存路径
和策略，不读取文件。显式调用 mapper 时，已读入内存的 `DEMDataset` 才会进入映射。

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `type` | enum(`raster`,`constant`) | — | — | 是 | 地形输入方式。 |
| `file` | string/null | path | null | 条件 | `raster` 时必填；当前加载器不检查文件存在性。 |
| `elevation` | number/null | m | null | 条件 | `constant` 时必填；`raster` 时禁止填写。 |
| `nodata` | number/null | 源栅格值 | null | 否 | 有限数值 sentinel；仅适用于 raster，须与读取数据元数据一致；不能把 NoData 当作 0。 |
| `nodata_strategy` | enum(`error`,`nearest`,`interpolate`) | — | `error` | 否 | `error` 在映射调用中遇到无可靠高程即失败；`nearest`/`interpolate` 仅预留，映射调用时抛 `NotImplementedError`。 |
| `min_valid_coverage` | number/null | 0～1 | null | 否 | raster 单元有效面积阈值；显式值须在 0～1，`null` 表示默认阈值尚未冻结。 |
| `resampling` | object | — | `{strategy: auto}` | 否 | raster 的 DEM→网格策略；mapper 内存路径已实现，配置加载不会自动执行。 |
| `resampling.strategy` | enum(`auto`,`area_weighted_mean`,`bilinear`,`direct`) | — | `auto` | 否 | `auto` 推荐；按两轴分辨率和对齐条件选择策略，不满足条件时报错。 |

映射策略固定规则：DEM 两轴更细使用 `area_weighted_mean`（实际重叠面积加权）；同分辨率且
CRS、范围、像元边界完全对齐使用 `direct`；DEM 两轴更粗使用 `bilinear`（目标中心四邻域）。
这些算法只对已读 `DEMDataset` 实现；CRS 重投影、路径自动读取和 Solver 仍未实现。一轴更细
一轴更粗、同分辨率未对齐或 bilinear 需要外推时，映射层明确失败。

### initial_condition（配置校验已实现；速度场预留）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `type` | enum(`constant_depth`,`constant_level`,`raster`) | — | — | 是 | 选择初始条件形式。 |
| `depth` | number/null | m | null | 条件 | `constant_depth` 时必填且 >= 0。 |
| `level` | number/null | m | null | 条件 | `constant_level` 时必填；垂直基准待确认。 |
| `depth_file` | string/null | path | null | 条件 | raster 时与 `level_file` 二选一；当前不读取。 |
| `level_file` | string/null | path | null | 条件 | raster 时与 `depth_file` 二选一；当前不读取。 |
| `velocity` | object/null | m/s | null | 否 | 二维初始速度场接口预留，暂未实现。 |

速度对象字段（均为预留接口）：

| 字段 | 类型 | 单位 | 默认值 | 必填 | 说明 |
|---|---|---|---|:---:|---|
| `velocity.type` | enum(`constant`,`raster`) | — | — | 是（启用时） | 速度场来源。 |
| `velocity.x` / `velocity.y` | number/null | m/s | null | constant 时是 | 常量 x/y 速度；当前不计算。 |
| `velocity.x_file` / `velocity.y_file` | string/null | path | null | raster 时是 | x/y 速度栅格；当前不读取。 |

### roughness（配置校验已实现；物理计算预留）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `type` | enum(`constant`,`raster`) | — | — | 是 | 糙率来源。 |
| `manning_n` | number/null | 无量纲 | null | constant 时是 | 必须 > 0。 |
| `file` | string/null | path | null | raster 时是 | 糙率栅格路径；当前不读取。 |

### boundary（列表、枚举和条件字段校验已实现；边界物理预留）

至少一个边界对象，`id` 必须唯一；`location` 为 `west/east/north/south`。`wall` 不接受
值；`discharge`/`water_level` 必须且只能填写 `value` 或 `timeseries`；通用 `timeseries`
还必须填写 `quantity`。`value` 当前统一校验为 >= 0，具体边界物理尚未实现。

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `id` | string | — | — | 是 | 非空且列表内唯一。 |
| `location` | enum | — | — | 是 | `west/east/north/south`。 |
| `type` | enum | — | — | 是 | `discharge/water_level/wall/timeseries`。 |
| `value` | number/null | m³/s 或 m | null | 条件 | 当前 >= 0；单位由边界类型决定。 |
| `timeseries` | object/null | — | null | 条件 | `discharge`/`water_level` 或通用 timeseries 的文件引用。 |
| `quantity` | enum(`discharge`,`water_level`)/null | — | null | 通用 timeseries | 指定 CSV 值的物理量。 |

边界 `timeseries` 对象字段：

| 字段 | 类型 | 单位/默认值 | 必填 | 说明 |
|---|---|---|:---:|---|
| `timeseries.file` | string | path | 是 | CSV 路径；当前不读取。 |
| `timeseries.time_column` | string | `time` | 否 | 时间列名；不能为空且必须不同于 value_column。 |
| `timeseries.value_column` | string | `value` | 否 | 数值列名；不能为空。 |
| `timeseries.time_unit` | enum(`s`) | `s` | 否 | 当前只支持秒；排序、插值、外推暂未实现。 |

### source（配置校验已实现；降雨物理预留）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `rainfall.enabled` | boolean | — | false | 否 | 是否启用降雨。 |
| `rainfall.type` | enum(`timeseries`)/null | — | null | enabled 时是 | 启用时必须为 `timeseries`；物理过程暂未实现。 |
| `rainfall.file` | string/null | path | null | enabled 时是 | CSV 路径；当前不读取。 |
| `rainfall.time_unit` | enum(`s`) | `s` | `s` | 否 | 当前只支持秒；时间序列排序/插值/外推暂未实现。 |
| `rainfall.intensity_unit` | string | mm/h | `mm/h` | 否 | 非空；单位换算暂未实现。 |

### numerics（配置范围校验已实现；数值方案预留）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `scheme` | string/null | — | null | 否 | 空间离散方案预留，未冻结。 |
| `flux` | string/null | — | null | 否 | 数值通量方案预留，未冻结。 |
| `time_integrator` | string/null | — | null | 否 | 时间积分方案预留，未冻结。 |
| `cfl` | number | 无量纲 | 0.5 | 否 | 0 < cfl <= 1；不代表具体算法。 |
| `min_depth` | number | m | 0.001 | 否 | >= 0 且 >= `dry_depth`；干湿算法未冻结。 |
| `dry_depth` | number | m | 0.0001 | 否 | >= 0；干湿算法未冻结。 |

### output（配置校验已实现；输出器预留）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `directory` | string | path | `results` | 否 | 非空结果目录；当前不创建。 |
| `interval` | number | s | 60 | 否 | 必须 > 0。 |
| `variables` | enum 列表 | — | `[water_depth]` | 否 | 非空、不可重复；当前只登记不写出。 |
| `format` | enum(`netcdf`,`geotiff`,`csv`) | — | `netcdf` | 否 | 格式枚举预留，输出器未实现。 |

可选输出变量：`water_depth`、`water_level`、`velocity_x`、`velocity_y`、`velocity_magnitude`、
`discharge`、`maximum_depth`、`maximum_velocity`、`arrival_time`、`froude_number`。当前
没有任何 Solver 或结果写出实现。

### validation（开关关系校验已实现；验证算法预留）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `enabled` | boolean | — | false | 否 | 未来运行校验总开关。 |
| `mass_balance_check` | boolean | — | false | 否 | 必须先启用 `enabled`；算法未实现。 |
| `report` | boolean | — | true | 否 | 未来报告开关；报告器未实现。 |

## 调用映射接口

```python
from hydrodynamics import GeoTIFFDEMReader, TerrainMapper, load_config

config = load_config("examples/case_001/config.yaml")
dataset = GeoTIFFDEMReader().read_dataset("path/to/dem.tif")
field = TerrainMapper().map_dataset(
    dataset, config.domain,
    coordinate_system=config.model.coordinate_system,
    strategy=config.terrain.resampling.strategy,
    nodata_strategy=config.terrain.nodata_strategy,
    min_valid_coverage=config.terrain.min_valid_coverage,
)
```

该调用显式执行已读 DEMDataset 的内存映射；`load_config` 本身不会执行它。

## 当前不做的校验和开放决策

配置加载阶段不会检查引用文件是否存在、栅格 CRS/尺寸/时间序列内容，不执行重投影、
NoData 填补、Solver 或物理过程。待后续确认的内容包括：坐标系语法和重投影责任、
`min_valid_coverage` 默认阈值、垂直基准前置条件、时间序列插值/外推规则、输出格式兼容性、
水位初始条件的精确定义，以及具体数值离散/通量/时间积分方案。
