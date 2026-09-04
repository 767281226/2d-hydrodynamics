# DEM → 二维计算网格映射规范 V1.0

本文档描述第 5 轮已冻结并实现的“已读 DEM 数据集 → 模型计算网格”规则。
实现只接收内存中的 `DEMDataset` 和已校验的 `DomainConfig`，不打开文件、
不执行 CRS 重投影、不调用二维水动力 Solver。GeoTIFF 文件读取仍由可选的
`GeoTIFFDEMReader` 负责。

## 1. 数据流与职责边界

```
原始 DEM 文件
    ↓  GeoTIFFDEMReader（读取原值、掩码和元数据）
DEMDataset
    ↓  TerrainMapper.map_dataset / map_dataset_to_field
TerrainField（与模型网格一一对应）
    ↓
未来 Solver
```

`load_config` 只解析 YAML，不会自动读取 `terrain.file` 或执行映射。调用方
必须显式读取 DEM，再调用映射 API。映射层不允许猜测 CRS、填补 NoData 或把
外部 CRS 转换到模型 CRS。

## 2. 模型计算区域与数组方向

V1.0 是 cell-centered、规则矩形 structured 网格。计算区域由
`domain.xmin`、`xmax`、`ymin`、`ymax` 显式定义，单位为 m；`dx`、`dy`
分别是 x/y 方向的单元尺寸，均须在 30～100 m（含）范围内，允许不同。

```text
nx = (xmax - xmin) / dx
ny = (ymax - ymin) / dy
x_i = xmin + (i + 1/2) * dx
y_j = ymin + (j + 1/2) * dy
```

长度不能整除步长时，`DomainConfig` 在配置阶段直接报错。用户不能配置
`nx`/`ny`；它们是程序由区域和步长计算出的内部属性。输出
`TerrainField.elevation[j][i]` 的形状为 `(ny, nx)`，其中 `j=0` 对应
`ymin` 一侧（南侧），`i=0` 对应 `xmin` 一侧（西侧）。

## 3. 映射前置检查

`map_dataset_to_field` 在执行任何策略前检查：

- DEM 必须是单波段，数据形状必须与 `DEMMetadata.width/height` 一致；
- DEM transform 必须是北向上、轴对齐仿射变换：禁止旋转和剪切，x 像元尺度
  为正、y 方向尺度为负；
- transform 推导出的像元大小和边界必须与元数据一致；
- DEM CRS 和模型 `coordinate_system` 都必须存在且相同，并且是平面/投影坐标；
  CRS 不一致时映射失败，不在本层重投影；
- 模型区域必须完全位于 DEM 几何边界内。只容忍极小浮点误差，不截断区域、
  不外推、不用边缘值补齐；
- `nodata_strategy` 目前只能为 `error`；`nearest` 和 `interpolate` 已
  保留枚举，但调用时明确抛出 `NotImplementedError`。

## 4. 策略状态与适用条件

| 策略 | 适用条件和行为 | 当前状态 |
|---|---|---|
| `auto` | DEM 两轴都更细 → `area_weighted_mean`；同分辨率且完全对齐 → `direct`；两轴都更粗 → `bilinear`；混合方向或同分辨率未对齐时报错 | 已实现（内存数据集） |
| `area_weighted_mean` | DEM 在两轴均不粗于目标且至少一轴更细；按真实重叠面积加权 | 已实现 |
| `direct` | 分辨率、范围、像元边界、轴方向完全一致 | 已实现 |
| `bilinear` | DEM 在两轴均不细于目标且至少一轴更粗；按目标单元中心的四个源像元中心插值 | 已实现 |

显式策略如果不满足对应分辨率条件会报 `TerrainMappingError`，不会静默改用
另一种策略。映射结果不代表求解器或物理模型已经实现。

## 5. area_weighted_mean

对每个模型单元，遍历与其相交的 DEM 像元，使用实际平面重叠面积：

```text
z_cell = Σ(A_k × z_k) / Σ(A_k)
coverage_ratio = Σ(A_k) / (dx × dy)
valid_area = Σ(A_k)       [m²]
```

只有 `valid_mask=True`、有限且不是 NoData sentinel 的像元才参与求和；
NoData 和 NaN/Inf 从不被替换为 0、边缘值或其他高程。结果写入
`TerrainField.elevation`、`coverage_ratio` 和 `valid_area`。当一个单元
没有任何有效重叠时，`error` 策略立即失败。可选的
`min_valid_coverage`（0～1，默认 `null`）只有在显式设置时才作为单元覆盖率
下限；默认值尚未冻结，因此不会自行引入阈值。

## 6. direct

只有 DEM 与模型网格在两方向分辨率相同、范围相同、像元边界相同且轴对齐时
才能直接映射。GeoTIFF/北向上栅格的第 0 行是北侧，而
`TerrainField[j=0]` 是南侧，因此实现会反转源行顺序；不会改变像元值。
任一源单元无有效高程时，在 `error` 策略下失败。

## 7. bilinear

双线性插值以每个目标单元中心为位置，使用包围该位置的四个源像元中心和标准
双线性权重。四个邻域必须都在 DEM 内且都为有限有效值；缺少邻域或需要外推时
明确失败。该策略只允许 DEM 较粗（两轴均不细于目标）的场景。双线性插值不会
创造新的真实地形信息，把粗 DEM 插值到更细网格不等于获得更高精度地形。

## 8. auto 判定

`resolve_auto_resampling_strategy` 和映射器采用同一判定矩阵：

1. `dem_dx <= grid_dx` 且 `dem_dy <= grid_dy`，至少一轴严格更细
   → `area_weighted_mean`；
2. 两轴同分辨率且已验证完整对齐 → `direct`；
3. `dem_dx >= grid_dx` 且 `dem_dy >= grid_dy`，至少一轴严格更粗
   → `bilinear`；
4. 一轴更细、一轴更粗，或同分辨率但未验证对齐 → 显式报错。

比较允许实现约定的极小浮点容差，但不允许借此掩盖真实空间错位。

## 9. Python 调用示例

```python
from hydrodynamics import GeoTIFFDEMReader, TerrainMapper, load_config

config = load_config("examples/case_001/config.yaml")
dataset = GeoTIFFDEMReader().read_dataset("examples/case_001/data/dem.tif")

field = TerrainMapper().map_dataset(
    dataset,
    config.domain,
    coordinate_system=config.model.coordinate_system,
    strategy=config.terrain.resampling.strategy,
    nodata_strategy=config.terrain.nodata_strategy,
    min_valid_coverage=config.terrain.min_valid_coverage,
)
print(field.shape, field.elevation[0][0], field.coverage_ratio[0][0])
```

等价的函数入口是 `hydrodynamics.terrain_mapping.map_dataset_to_field`。两者都只
接受已经读入内存的 `DEMDataset`。现有 `TerrainMapper.map(domain, terrain)`
是保留的配置边界占位接口，仍会在不读取文件的情况下抛出
`NotImplementedError`，不要把两个方法混淆。

## 10. TerrainField 输出

`TerrainField` 是不可变、依赖无关的结果容器，包含：

- `elevation`：形状 `(ny, nx)`，支持 `[j][i]` 和概念上的 `[j, i]`；
- `nx`、`ny`、`dx`、`dy`、`xmin`、`ymin`；
- `valid_mask`：目标单元是否得到可靠有限高程；
- `nodata_mask`：保留的 NoData 状态；
- `coverage_ratio`：有效 DEM 重叠面积占单元面积比例（0～1）；
- `valid_area`：由 coverage 推导的有效面积，单位 m²。

映射层不会返回 NoData sentinel 或非有限高程；源数据的原始值和掩码仍保留在
`DEMDataset` 中。

## 11. 明确未实现项

- CRS 重投影、坐标转换和不同 CRS 之间的自动配准；
- `nearest`、`interpolate` NoData 填补策略；
- GeoTIFF 的分块/窗口式低内存映射和自动路径解析；
- 初始水深、边界、降雨等物理过程；
- 控制方程、空间离散、数值通量、时间积分、干湿处理和二维水动力 Solver；
- 结果文件写出和运行时质量报告。

本轮已实现的是纯数据准备层的内存映射，不代表任何 Solver 数值方案已经选定。

\n
