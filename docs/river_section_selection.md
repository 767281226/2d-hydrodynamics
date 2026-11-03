# 假定河道断面的边界边选择（DEM 主槽规则）

模块：`hydrodynamics/river_section_selection.py`
输入契约：`assumed-river-sections-request-v1`
（`examples/river_sections/assumed_river_sections_request.schema.json`）
输出契约：`assumed-river-sections-audit-v1`
（`examples/river_sections/assumed_river_sections_audit.schema.json`）
测试：`tests/test_river_section_selection.py`

## 定位与诚实声明

* 断面位置是**假定的**（来自 OSM 河道与网格外边界的交点，经用户确认），
  不是水利部门的权威断面。审计文件中 `section_status` 固定为
  `ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE`，下游使用者必须保留这一状态。
* 选边规则是一条 **DEM 地形启发式**：在外边界沿线用 DEM 高程找"主槽"并向两侧
  扩展到岸坎阈值。它不做任何水力计算，不代表设计断面、行洪断面或过流能力。
* DEM 只按原始像元值读取（NoData -> NaN），不做坐标转换、重采样或 NoData 填补。
  请求、网格、DEM 三者的 CRS 必须等价（pyproj 等价判定；无 pyproj 时按字符串比较），
  任何不等价都直接报错，不做转换。
* 本模块不涉及求解器；输出的 `edge_indices` 只是供
  `triangular-open-boundary-group-v1`（见 `docs/triangular_long_run.md`）引用的
  外边界边索引（`mesh.edge_nodes` 顺序）。

## 规则（与实验脚本逐步一致）

1. 取网格外边界边（只有一个面的边），串成环，取最长环为外边界，按连续方向定向，
   计算每条边起点的弧长 `s`。
2. 每条边的高程 = 沿边 `dem_samples_per_edge` 个等分位置（k=5 时为
   0.1、0.3、0.5、0.7、0.9）的 DEM 原始值的 `nanmean`（同时记录 `nanmin`）。
3. 对每个断面：
   * 取离断面点最近的边中点为锚边 `i0`；与锚边中点距离超过 `max_point_distance_m`
     报错 `point_far_from_boundary`。
   * 窗口 = 沿环弧长 `|s - s_i0| < window_m` 的边（跨环首尾回绕）。
   * 主槽 = `|s - s_i0| < thalweg_search_m` 内平均高程最低的边。
   * 岸坎 = 窗口内平均高程的 `bank_percentile` 分位数。
   * 阈值 = `max(岸坎 - bank_margin_m, 主槽 + thalweg_margin_m)`。
   * 从主槽向两侧扩展：低于阈值的边纳入；高于阈值（或 NaN）的连续边累计长度
     不超过 `gap_tolerance_m` 且其后仍有低于阈值的边时一并纳入；到窗口边缘停止。
   * 选中的边是连续的 `lo..hi` 一段；`width_m` 为其边长之和。

规则本体是纯函数 `select_channel_run(rel_s, length, z, rule)`，不依赖 DEM
文件，可直接用手写剖面测试。

## 请求参数

| 字段 | 类型 / 单位 | 默认 | 合法范围 | 错误码 |
|---|---|---|---|---|
| `format` | 常量 | 必填 | `assumed-river-sections-request-v1` | `format` |
| `schema_version` | 常量 | `1.0` | `1.0` | `format` |
| `crs` | 字符串 | 必填 | 须与网格 `coordinate_system`、DEM CRS 等价 | `crs_mismatch`、`dem_crs_mismatch`、`dem_crs_missing`、`crs_invalid` |
| `sections[].name` | 字符串 | 必填 | `[A-Za-z0-9_-]{1,32}`，唯一 | `field_type`、`duplicate_section` |
| `sections[].role` | 枚举 | 必填 | `inflow` / `outflow` | `section_role` |
| `sections[].xy` | `[x, y]`，m（网格 CRS） | 必填 | 两个有限数 | `field_type` |
| `sections[].discharge_series_column` | 字符串 | 必填 | 标识符 `[A-Za-z_][A-Za-z0-9_]*` | `missing_field`、`field_type` |
| `sections[].name_zh` | 字符串 | 必填 | 非空 | `field_type` |
| `max_point_distance_m` | 数，m | 100 | (0, 10000] | `parameter_range`、`point_far_from_boundary` |
| `dem_sha256` | 64 位十六进制 | 可选 | 与 DEM 文件 SHA-256 一致 | `dem_sha256_mismatch` |
| `rule.window_m` | 数，m | 1500 | [50, 50000]，且 `2*window_m` < 外环周长 | `parameter_range` |
| `rule.thalweg_search_m` | 数，m | 300 | (0, 50000]，<= `window_m` | `parameter_range` |
| `rule.bank_percentile` | 数，% | 75 | [50, 100] | `parameter_range` |
| `rule.bank_margin_m` | 数，m | 2.0 | [0, 50] | `parameter_range` |
| `rule.thalweg_margin_m` | 数，m | 1.5 | [0, 50] | `parameter_range` |
| `rule.gap_tolerance_m` | 数，m | 60 | [0, 50000]，<= `window_m` | `parameter_range` |
| `rule.dem_samples_per_edge` | 整数 | 5 | [1, 25] | `parameter_type`、`parameter_range` |

未知字段报 `unknown_field`；类型错误报 `parameter_type` / `field_type`。
命令行参数 `--mesh`、`--dem`、`--out` 必填，`--figure`（需 matplotlib）、
`--expected-dem-sha256` 可选；网格工件必须是
`triangle-domain-mesh-only-artifact-v1`（错误码 `mesh_artifact_*`、`mesh_invalid`）。
DEM 必须是单波段 GeoTIFF（`dem_unreadable`，依赖 rasterio）。
选择阶段还可能报 `window_empty`、`thalweg_undefined`、`window_no_dem`、
`run_covers_loop`。

## 命令行

```bash
python -X utf8 -m hydrodynamics.river_section_selection \
  examples/river_sections/assumed_river_sections_request.example.json \
  --mesh work/<...>/artifacts/domain-mesh.json \
  --dem data/dem/DEMn_1m.tif \
  --out <out>/sections_audit.json [--figure <out>/sections.png]
```

退出码：0 成功；2 校验/选择错误（错误信息写到 stderr，形如 `error: <code>: <message>`）。
mesh005（178,962 面）的 `TriangularMesh` 构建约 35 s；DEM 只对窗口内的边采样。

## 审计文件要点

顶层：`mesh_id`、`mesh_artifact`（命令行给出的路径）、`mesh_artifact_sha256`、
`dem`、`dem_sha256`、`dem_crs`（GeoTIFF WKT）、`crs`、`section_status`、
`max_point_distance_m`、`rule`（含 `edge_elevation` 文字说明）、`outer_loop`
（外环边数、周长、各外边界环边数）。

每个断面：`role`、`name_zh`、`discharge_series_column`、`osm_intersection_xy`、
`nearest_edge_distance_m`、`thalweg_elevation_m`、`bank_level_p75_m`
（字段名沿用原审计，实际为 `bank_percentile` 分位数）、`threshold_m`、
`edge_count`、`width_m`、`mean/min/max_edge_elevation_m`、
`edges_above_threshold_included`、`window_edge_count`、`nodata_edges_in_window`、
`edge_indices`（全部为外边界边，沿环连续）、`edge_nodes`、`profile`（选中边）、
`window_profile`（窗口内全部边）。全 NoData 的边高程写为 `null`。

## mesh005 + DEMn_1m 的实际结果（假定断面，非权威）

示例请求 `assumed_river_sections_request.example.json` 使用三个 OSM 交点
（EPSG:4548），在 mesh005（`mesh_id 8a840936ecc1ed4e8ab2b25d`，外环 5,799 边）上
用默认规则得到：

| 断面 | 角色 | 主槽 m | 岸坎 p75 m | 阈值 m | 边数 | 宽度 m | 含高于阈值的边 |
|---|---|---|---|---|---|---|---|
| A 大清河西侧入口（新盖房来水） | inflow | 0.38 | 1.89 | 1.88 | 12 | 512 | 1 |
| B 独流减河起点 | outflow | -4.54 | 7.89 | 5.89 | 22 | 535 | 2 |
| C 子牙河/西河东北出口 | outflow | -0.31 | 9.02 | 7.02 | 21 | 401 | 0 |

本模块输出的 `edge_indices` 与最初的一次性实验脚本
（`sec_profile.py` / `sec_select.py`）在同一网格与 DEM 上得到的结果逐一相同。
这些宽度只是 DEM 主槽在网格边界上的投影，是否作为开边界使用须由用户确认。
