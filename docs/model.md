# 模型和案例架构

一次模拟由一个 case 目录表示。`config.yaml` 是总控制文件；`data/` 保存外部
栅格和时间序列；`results/` 预留给未来结果输出。

V1.0 使用规则矩形结构化网格：`domain` 显式给出 xmin/xmax/ymin/ymax 计算区域
和 dx/dy 网格尺寸。程序根据区域长度与步长计算内部 nx/ny；用户不配置 nx/ny。

配置加载器负责类型、枚举、范围和字段组合校验，但不会解析外部数据、建立网格、
运行时间步或生成结果。数值方法字段仍是预留字符串。

## DEM mapping boundary

地形 DEM 由未来的数据准备/Terrain Mapping 层映射到与计算网格一一对应的
`TerrainField.elevation[j][i]`（概念上即 `terrain_elevation[j, i]`）。映射策略通过 `terrain.resampling.strategy`
声明，`auto` 的分辨率判定规则和 NoData/覆盖约束见
[terrain_mapping.md](terrain_mapping.md)。当前 `TerrainMapper` 仅为不产生伪造
结果的占位接口，实际 DEM 读取、CRS 转换和重采样尚未实现。
