# DEM → 二维计算网格映射规范 V1.0

本文档落实第二轮冻结规则。当前代码只提供配置、元数据判定函数和接口占位；
不会读取 DEM/GeoTIFF、执行 CRS 重投影、进行重采样或调用 Solver。

## 1. 计算区域与网格

DEM 不决定计算区域。计算区域由 `domain.xmin`、`xmax`、`ymin`、`ymax` 显式
定义，单位为米。V1.0 网格是规则矩形、结构化、cell-centered 的有限体积控制
体；`dx`、`dy` 分别是 x/y 方向尺寸，范围为 30～100 m（含），允许不同。

区域长度必须被对应步长整除。内部网格数量由程序计算：

```text
nx = (xmax - xmin) / dx
ny = (ymax - ymin) / dy
```

单元中心为：

```text
x_i = xmin + (i + 1/2) * dx
y_j = ymin + (j + 1/2) * dy
```

数组约定为 `elevation[j][i]`，形状为 `(ny, nx)`；当前无 NumPy 占位对象也支持
概念性的 `elevation[j, i]` 访问。Solver 的未来输入是与该
网格一一对应的 `terrain_elevation[j, i]`，不直接依赖原始 DEM 的格式、分辨率、
CRS 或尺寸。

## 2. DEM 覆盖范围

DEM 的有效覆盖范围必须完全包含计算区域。实际缺失必须报错；禁止边缘值填充、
零填充、自动 extrapolation 或静默忽略。未来数据准备层可以容忍极小浮点误差，
但不能容忍真实空间缺失。

## 3. CRS 边界

模型拥有自己的计算 CRS。若 DEM CRS 不一致，必须由数据准备/Terrain Mapping
层先重投影，再映射到计算网格。Solver 不负责 CRS 转换。当前版本不执行实际重投影。

## 4. 分辨率与映射策略

配置字段为：

```yaml
terrain:
  resampling:
    strategy: auto
```

支持的枚举值：

| 策略 | 固定含义 | 当前状态 |
|---|---|---|
| `auto` | 按下述 V1.0 矩阵选择策略；推荐值 | 仅元数据判定，实际映射未实现 |
| `area_weighted_mean` | DEM 比计算网格细时的面积加权平均 | 未实现 |
| `direct` | CRS、分辨率、边界和像元完全对齐时一一对应 | 未实现 |
| `bilinear` | DEM 比计算网格粗时的双线性插值 | 未实现 |

面积加权平均的规范公式为：

```text
z_cell = Σ(A_k * z_k) / Σ(A_k)
```

其中 `A_k` 是 DEM 像元与计算单元的重叠面积。双线性插值不会创造新的真实
地形信息，把粗 DEM 插值到更细网格不代表获得了更高精度的地形数据。

`auto` 的固定判定矩阵：

- DEM 在两个方向均更细（每轴 `dem_step <= grid_step`，且至少一轴严格更细）
  → `area_weighted_mean`；
- DEM 与计算网格同分辨率，且未来数据层已验证 CRS、范围、像元边界和对齐
  → `direct`；同分辨率但未验证对齐时显式报错；
- DEM 在两个方向均更粗（每轴 `dem_step >= grid_step`，且至少一轴严格更粗）
  → `bilinear`；
- 一个方向更细、另一个方向更粗 → V1.0 未定义，显式报错，不猜测。

`resolve_auto_resampling_strategy` 只接收已经由未来数据层提供的分辨率和对齐
元数据，负责分类，不读取或修改栅格。

## 5. NoData

`terrain.nodata_strategy` 默认是 `error`。它表示映射过程中任一计算单元无法
获得有效地形高程时必须失败。`nearest` 和 `interpolate` 已保留为枚举值，但
当前不实现相应算法；`TerrainMapper` 对它们会明确抛出 `NotImplementedError`。

## 6. TerrainField

`hydrodynamics.config.TerrainField`（也可从 `hydrodynamics.terrain_mapping`
导入）是轻量、不可变的数据结构，保存：

- `elevation`：二维序列，按 `[j][i]` 索引；
- `nx`、`ny`、`dx`、`dy`、`xmin`、`ymin`；
- 可选 `nodata_mask`，形状必须同为 `(ny, nx)`，元素为布尔值。

`TerrainField.from_domain(domain, elevation, nodata_mask=None)` 会从
`DomainConfig` 传递网格元数据并校验二维形状。它不把 NoData sentinel 或 NaN
转换成高程；每个 elevation 值仍必须是有限数值。

## 7. TerrainMapper

`hydrodynamics.config.TerrainMapper` 定义未来数据准备层的边界：

```python
field = TerrainMapper().map(
    domain,
    terrain,
    coordinate_system=config.model.coordinate_system,
)
```

当前 `map` 只接受 raster 地形，检查参数类型、策略一致性和已声明的 NoData 能力，
然后抛出明确的 `NotImplementedError`；constant 地形不需要 DEM 映射，传入时会被
显式拒绝。它不会伪造 elevation，也不会打开文件。真实 DEM 读取、
GeoTIFF 解析、覆盖检查、CRS 转换、NoData 处理和三种重采样算法留到后续阶段。

## 8. 明确未实现项

- 实际 DEM/GeoTIFF 读取和元数据解析；
- DEM 覆盖范围检查的运行时实现；
- CRS 重投影；
- `area_weighted_mean`、`direct`、`bilinear` 的实际计算；
- `nearest`、`interpolate` NoData 算法；
- Solver、数值通量、时间积分、Manning 计算和边界物理。
