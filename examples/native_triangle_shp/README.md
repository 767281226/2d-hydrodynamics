# Native triangular result SHP example / 原生三角结果 SHP 示例

This example starts from two actual triangular faces with stable IDs above `2**53`, prepares flat terrain and 0.2 m still water, then runs the existing closed-wall P4 solver for three saved times over one second. The SHP adapter reads that completed native result and never calls the solver.

本例包含两个真实计算三角面，`face_id` 大于 `2**53`，用于验证 DBF 身份不经浮点数转换。示例准备平底和 0.2 m 静水，再用已有 P4 封闭边界求解器计算 0、0.5、1 秒三个输出时刻。SHP 适配器只读取已完成的原生结果，不运行 Solver。

Install the optional P4/GIS dependencies and prepare the example:

```powershell
python -m pip install -e ".[triangular,shp]"
hydrodynamics-mesh-prepare examples/native_triangle_shp/mesh_preparation.yaml
hydrodynamics-business-prepare examples/native_triangle_shp/business_preparation.yaml
hydrodynamics-triangular-run examples/native_triangle_shp/run.yaml
```

Export and independently validate one frame in the native projected CRS, then a maximum-depth window in EPSG:4490:

```powershell
hydrodynamics-triangle-shp export --request examples/native_triangle_shp/export_frame.yaml
hydrodynamics-triangle-shp validate --request examples/native_triangle_shp/export_frame.yaml
hydrodynamics-triangle-shp export --request examples/native_triangle_shp/export_max_wgs84.yaml
hydrodynamics-triangle-shp validate --request examples/native_triangle_shp/export_max_wgs84.yaml
```

`MAX_OVER_WINDOW` accepts only exact saved-time endpoints and includes both endpoints. Each face's tie uses its earliest native time. `MAXIMUM_WET_SURFACE` includes faces whose selected depth is strictly greater than `wet_depth_threshold_m`; if none qualify, the exporter writes a valid empty polygon layer. The `area_m2` attribute always comes from the original projected P4 `face_area`; geographic coordinates are never used to calculate square metres.

`FRAME` records include exact decimal-text `face_id`, seconds, depth, water level, velocity components and area. `face_id` remains text in DBF so IDs above `2**53` retain every digit. `water_level_m` uses the P3-A vertical datum recorded in `manifest.json`. Velocity components remain in the original projected calculation axes even when the polygon geometry is exported in EPSG:4490.

所有图层的每个 feature 对应一个实际 P4 三角面；SHP 由三个原生节点组成，不拆分、不合并、不重采样。导出目录包含 `.shp/.shx/.dbf/.prj/.cpg` 和 `manifest.json`。manifest 记录源图摘要、selector、CRS、单位、垂直基准和每个组件的 SHA-256/字节数。它始终标记 `experimental=true`、`engineering_validated=false`、`can_publish=false`。该首批适配只接受现有 `closed_wall`、`sources=none` P4-1 结果。

Usage and limitations are also described in [English implementation notes](../../docs/native_triangle_shp.md) and [中文说明](../../docs/zh-CN/native_triangle_shp.md).
