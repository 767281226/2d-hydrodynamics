# V1.0 输入参数说明

`hydrodynamics.config.SimulationConfig` 是当前唯一权威配置 Schema。
配置文件使用 YAML；未知字段会被拒绝。当前加载器只解析和校验配置，不读取
GeoTIFF/CSV，也不启动 Solver。

状态说明：**已实现**表示配置层会解析并校验；**预留/暂未实现**表示字段
已纳入接口，但还不会产生计算或输出效果。

## 一级模块

以下十个模块均为必填：`model`、`domain`、`terrain`、`initial_condition`、
`roughness`、`boundary`、`source`、`numerics`、`output`、`validation`。

## 参数明细

### model（已实现：配置解析与校验）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `name` | string | — | — | 是 | 案例名称，不能为空 |
| `description` | string/null | — | null | 否 | 案例说明，填写时不能为空 |
| `gravity` | number | m/s² | 9.81 | 否 | 必须大于 0 |
| `start_time` | number | s | 0 | 否 | 必须 >= 0 |
| `end_time` | number | s | — | 是 | 必须大于 `start_time` |
| `output_interval` | number/null | s | null | 否 | 兼容字段；填写时必须等于 `output.interval` |
| `coordinate_system` | string | CRS 标识 | — | 是 | 不能为空；具体格式和重投影暂未实现 |
| `units` | enum(`SI`) | — | `SI` | 否 | V1.0 目前只接受 `SI` |

### domain（已实现：范围、步长和整除校验）

V1.0 使用规则矩形结构化网格。`xmin/xmax/ymin/ymax` 定义计算区域边界，
单位均为米；`dx/dy` 分别定义 x、y 方向网格尺寸，允许不同。V1.0 目标网格
尺度为 30～100 m（含边界），因此 `dx`、`dy` 都必须落在该范围内。

区域长度必须整除对应步长：`(xmax - xmin) / dx` 和 `(ymax - ymin) / dy`
必须为正整数，否则配置失败。`nx`、`ny` 不再是用户输入字段；程序通过上述
公式得到内部网格数量，可在 `DomainConfig.nx`、`DomainConfig.ny` 属性中读取。

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `type` | enum(`structured`) | — | `structured` | 否 | V1.0 仅支持规则结构化网格 |
| `xmin`、`xmax` | number | m | — | 是 | `xmax > xmin`；定义 x 范围 |
| `ymin`、`ymax` | number | m | — | 是 | `ymax > ymin`；定义 y 范围 |
| `dx` | number | m | — | 是 | 30 <= dx <= 100；x 方向尺寸 |
| `dy` | number | m | — | 是 | 30 <= dy <= 100；y 方向尺寸，可不同于 dx |
| `nx`、`ny` | 内部 integer | cell | 程序计算 | 否/禁止输入 | 不属于用户 Schema；分别为 x/y 方向网格数量 |

### terrain（已实现：输入方式和 NoData 配置校验）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `type` | enum(`raster`,`constant`) | — | — | 是 | 选择地形输入方式 |
| `file` | string/null | path | null | 条件 | `raster` 时必填；当前不读取文件 |
| `nodata` | number/null | 栅格原值 | null | 否 | 栅格 NoData 标记 |
| `nodata_strategy` | enum(`error`,`nearest`,`interpolate`) | — | `error` | 否 | `nearest`、`interpolate` 仅预留，插值算法暂未实现 |
| `elevation` | number/null | m | null | 条件 | `constant` 时必填；raster 时禁止填写 |
| `resampling` | object | — | `{strategy: auto}` | 否 | raster 的 DEM 映射策略配置；实际算法暂未实现 |
| `resampling.strategy` | enum(`auto`,`area_weighted_mean`,`bilinear`,`direct`) | — | `auto` | 否 | `auto` 是 V1 推荐值；按 DEM/网格分辨率固定选择策略；算法暂未实现 |

`resampling.strategy` 的固定规则为：DEM 更细使用 `area_weighted_mean`；
同分辨率且 CRS、范围、像元边界完全对齐使用 `direct`；DEM 更粗使用
`bilinear`。同分辨率但未验证完全对齐、或两个方向一细一粗时，V1.0 不猜测，
由映射层报错。`auto` 只依据未来数据准备层提供的元数据，当前不读取 DEM。

### initial_condition（已实现：配置校验；速度场预留）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `type` | enum(`constant_depth`,`constant_level`,`raster`) | — | — | 是 | 选择初始条件形式 |
| `depth` | number/null | m | null | 条件 | `constant_depth` 时必填，必须 >= 0 |
| `level` | number/null | m | null | 条件 | `constant_level` 时必填 |
| `depth_file` | string/null | path | null | 条件 | raster 时与 `level_file` 二选一 |
| `level_file` | string/null | path | null | 条件 | raster 时与 `depth_file` 二选一 |
| `velocity` | object/null | m/s | null | 否 | 二维初始速度场预留，暂未实现 |

速度场预留对象支持 `type: constant`（`x`、`y`，单位 m/s）或 `type: raster`
（`x_file`、`y_file`）；当前不会读取或计算速度。

### roughness（已实现：配置校验）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `type` | enum(`constant`,`raster`) | — | — | 是 | 选择糙率输入方式 |
| `manning_n` | number/null | 无量纲 | null | 条件 | `constant` 时必填，必须 > 0 |
| `file` | string/null | path | null | 条件 | `raster` 时必填；当前不读取文件 |

### boundary（已实现：列表、枚举和条件字段校验）

`boundary` 必须是至少一个对象的列表，`id` 必须唯一。`location` 当前支持
`west/east/north/south`。`wall` 不接受值；`discharge`、`water_level` 必须二选一
填写 `value` 或 `timeseries`；通用 `timeseries` 必须另外填写 `quantity`。
具体边界物理尚未实现。

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `id` | string | — | — | 是 | 非空且唯一 |
| `location` | enum | — | — | 是 | `west/east/north/south` |
| `type` | enum | — | — | 是 | `discharge/water_level/wall/timeseries` |
| `value` | number/null | m³/s 或 m | null | 条件 | 定值边界输入 |
| `timeseries` | object/null | 由边界类型决定 | null | 条件 | 文件引用；当前不读取 |
| `quantity` | enum/null | — | null | 通用 timeseries | `discharge` 或 `water_level` |

### source（已实现：开关和字段校验；降雨暂未实现）

`source.rainfall.enabled` 默认 `false`。启用时必须填写 `type: timeseries` 和
`file`；CSV 的值单位默认 `mm/h`。降雨物理过程和时间序列执行暂未实现。

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `rainfall.enabled` | boolean | — | false | 否 | 是否启用降雨输入 |
| `rainfall.type` | enum/null | — | null | 条件 | 启用时为 `timeseries`，暂未实现 |
| `rainfall.file` | string/null | path | null | 条件 | 启用时必填，暂不读取 |
| `rainfall.time_unit` | enum(`s`) | — | `s` | 否 | 当前只支持秒 |
| `rainfall.intensity_unit` | string | mm/h（默认） | `mm/h` | 否 | 非空；换算暂未实现 |

### numerics（已实现：通用范围校验；算法字段预留）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `scheme` | string/null | — | null | 否 | 空间离散方案预留，未冻结/未实现 |
| `flux` | string/null | — | null | 否 | 数值通量方案预留，未冻结/未实现 |
| `time_integrator` | string/null | — | null | 否 | 时间积分方案预留，未冻结/未实现 |
| `cfl` | number | 无量纲 | 0.5 | 否 | 0 < cfl <= 1；不代表具体算法 |
| `min_depth` | number | m | 0.001 | 否 | >= 0 且 >= `dry_depth` |
| `dry_depth` | number | m | 0.0001 | 否 | >= 0；干湿算法暂未实现 |

### output（已实现：配置校验；输出器暂未实现）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `directory` | string | path | `results` | 否 | 非空结果目录 |
| `interval` | number | s | 60 | 否 | 必须 > 0 |
| `variables` | enum 列表 | — | `[water_depth]` | 否 | 非空、不可重复；变量写出暂未实现 |
| `format` | enum(`netcdf`,`geotiff`,`csv`) | — | `netcdf` | 否 | 仅接口枚举，格式写出暂未实现 |

当前已登记输出变量：`water_depth`、`water_level`、`velocity_x`、`velocity_y`、
`velocity_magnitude`、`discharge`、`maximum_depth`、`maximum_velocity`、
`arrival_time`、`froude_number`。

### validation（已实现：开关关系校验；验证算法暂未实现）

| 参数 | 类型 | 单位 | 默认值 | 必填 | 说明/限制 |
|---|---|---|---|:---:|---|
| `enabled` | boolean | — | false | 否 | 未来运行校验总开关 |
| `mass_balance_check` | boolean | — | false | 否 | 必须先启用 `enabled` |
| `report` | boolean | — | true | 否 | 未来报告开关 |

## 当前不做的校验

配置加载阶段不会打开或读取栅格/CSV，不检查文件存在性、栅格尺寸、CRS、时间
序列排序、插值/外推、NoData 替换，也不执行任何水动力计算。这些属于后续
数据准备、求解器和结果输出设计。