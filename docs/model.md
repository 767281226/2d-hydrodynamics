# 模型和案例架构

一次模拟由一个 case 目录表示。`config.yaml` 是总控制文件；`data/` 保存外部
栅格和时间序列；`results/` 预留给未来结果输出。

V1.0 使用规则矩形结构化网格：`domain` 显式给出 xmin/xmax/ymin/ymax 计算区域
和 dx/dy 网格尺寸。程序根据区域长度与步长计算内部 nx/ny；用户不配置 nx/ny。

配置加载器负责类型、枚举、范围和字段组合校验，但不会解析外部数据、建立网格、
运行时间步或生成结果。数值方法字段仍是预留字符串。

## DEM mapping boundary

地形 DEM 由 `GeoTIFFDEMReader` 读取为 `DEMDataset`，再由
`TerrainMapper.map_dataset` 或 `map_dataset_to_field` 映射到与计算网格一一对应的
`TerrainField.elevation[j][i]`（概念上即 `terrain_elevation[j, i]`）。
`auto`、`area_weighted_mean`、`direct` 和 `bilinear` 的内存映射规则见
[terrain_mapping.md](terrain_mapping.md)。映射层只接受已经匹配的平面 CRS，不执行
重投影；NoData 的 `nearest`/`interpolate` 仍是预留策略。

旧的 `TerrainMapper.map(domain, terrain)` 保留为配置校验占位接口，不读取文件。
`load_config` 也不会因为配置中存在 `terrain.file` 就自动读取或映射。

## DEM data contract boundary

DEM 不直接进入 Solver。当前可选流程为 `GeoTIFFDEMReader → DEMMetadata/DEMDataset →`
`TerrainMapper → TerrainField → Solver`；Solver 只接收与 V1.0 cell-centered 计算网格对应的
`TerrainField`。垂直基准必须在运行前确认，`model.vertical_datum_required` 默认
为 true，但当前不指定具体基准。详细字段和错误语义见
[dem_data_contract.md](dem_data_contract.md)。
