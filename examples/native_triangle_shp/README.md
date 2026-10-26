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

## M1 compatibility and native viewer QA / M1 兼容出口与原生查看器验证

The M1 exporter consumes only the already-existing, strictly source-bound small native results: this 2-face `sources=none` sample and the 4-face Manning/rainfall/security sample under `work/example_runs/20261020_combined_security_01a1537a`. It does not invoke a solver. Generated `m1qa-*` folders and request files are local QA artifacts and are ignored by Git; they are not production bundles and are never eligible for `latest`.

Each QA bundle contains the authoritative native result copy, native face web profile, the legacy structured-grid NC/nc2web/stepjson derived profile, and four true-native-face SHP packages (`FULL_DOMAIN` and `MAXIMUM_WET_SURFACE`, each in native CRS and EPSG:4490). The native viewer opens first and keeps exact face IDs as decimal strings. The compatibility viewer uses area-overlap mapped values, records partial coverage and per-cell source-CRS overlap area, and stays a derived display grid; it is never used to build SHP geometry. Native wet-face area uses the native dry-depth threshold; compatibility wet-cell covered area counts cells whose aggregate depth is positive. New NC files use NaN for missing values. In legacy `_FillValue=0` files, active available zeros are recovered only when the masked underlying sample is actually zero; nonzero missing sentinels remain unavailable.

The M1 compatibility rules are in [the frozen output contract](../../docs/full_dem_output_compatibility_contract_2026-10-26.md). The versioned bundle, native-web, compatibility-grid, nc2web-v1, and stepjson-v1 schemas are in this directory. The selected project-domain route for the later full run is the actual existing D geometry minus validated security geometry S; M1's small examples are not that domain. M2 must recompute the domain from the verified source geometries and audit DEM coverage before any full run.

For partially covered compatibility cells, the browser clips the derived-grid display to the exact normalized native-triangle intersection geometry. The covered-area mean is not painted over the excluded part of a security cell.

Viewer frame and profile requests use generation checks and cancellation. Frame data, time, HUD, and seek position commit together only for the newest selection; its two-frame cache is bounded to 32 MiB. Legacy workflow `run.json` fields are normalized in memory after a matching successful report is validated; the original metadata is untouched, and the legacy format is available by explicit path only. Windows viewer servers bind ports exclusively and include the expected `run_id` in the opened URL.

本M1只复用已存在且通过严格来源绑定的小型native结果：本例2面` sources=none`，以及`work/example_runs/20261020_combined_security_01a1537a`下的4面糙率/降雨/安全区样本；不会启动Solver。生成的`m1qa-*`目录和请求文件是本地QA资产，已加入忽略规则；它们不是生产bundle，也不能进入`latest`。

每个M1 QA bundle包含权威native结果副本、native face web profile、旧结构化NC/nc2web/stepjson派生profile，以及四个真实计算三角SHP包（`FULL_DOMAIN`与`MAXIMUM_WET_SURFACE`分别输出native CRS和EPSG:4490）。查看器默认native拓扑，face ID以十进制字符串保真；兼容显示按面积交叠映射并保留partial coverage，但不会用于构造SHP几何。

兼容网格另存每格source CRS中的实际覆盖面积；重投影后的web间距不参与物理湿面积统计。原生湿面面积按native dry threshold统计；派生兼容湿格按聚合深度>0统计覆盖面积，两个数值分别显示。新NC以NaN区分NoData和合法0；旧`_FillValue=0`加`active_mask`合同只在活动格、明确可用变量且掩码下原值实际为0时恢复合法0。非零`missing_value` sentinel继续作为缺测处理。

查看器以请求代次和取消隔离切帧/切profile，仅最新选择会一起更新数据、时间、HUD和seek；两帧LRU总量不超过32 MiB。旧workflow `run.json`按实际字段在内存归一化，并须通过同ID成功报告；历史文件不改写，也不补造生产批准状态，旧格式只接受显式路径。Windows服务独占端口，打开URL携带预期`run_id`供页面核对。

对于部分覆盖的兼容格，浏览器按每格内归一化的native三角交叠几何裁剪绘制区域，避免把覆盖面积均值涂到安全区一侧。

全量工程域已选为**现有真实D几何扣除经验证S安全区**。M1小样本不是该工程域；M2必须从真实D/S来源文件重算并核查DEM覆盖，不得用固定面积或bbox代替。旧成功完整bundle仍未找到，因此本例不证明全量产物superset。
