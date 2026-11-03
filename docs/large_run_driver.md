# 大规模真实长时程运行驱动（large-run driver，`large-run-driver-config-v1`）

日期：2026-11-03。状态：实验性粘合层；不含任何新方程、离散或数值通量。
它只把既有模块串起来：

```
网格 artifact (triangle-domain-mesh-only-artifact-v1)
+ 逐面地形 .npz (face_id, elevation_m)
+ 假定河道断面审计 (assumed-river-sections-audit-v1)
+ 流量 CSV (time_s + 断面列)
    -> hydrodynamics.triangular_long_run.run_triangular_long_run      （求解器，docs/triangular_long_run.md）
    -> hydrodynamics.frame_sink.NativeUgridNetcdfSink 流式写 result_native.nc （docs/large_run_output_profile.md）
    -> hydrodynamics.large_run_export.export_large_run_bundle + verify_large_run_bundle
```

模块：`hydrodynamics/large_run_driver.py`。Schema：
[`examples/large_run_output/large_run_driver_config.schema.json`](../examples/large_run_output/large_run_driver_config.schema.json)
（由 `driver_config_json_schema()` 生成，测试保证同步）。示例：
[`large_run_driver_config.example.json`](../examples/large_run_output/large_run_driver_config.example.json)。
测试：`tests/test_large_run_driver.py`。

## 1. 命令行

```
python -X utf8 -m hydrodynamics.large_run_driver <config.json>            # 新运行（输出目录必须不存在）
python -X utf8 -m hydrodynamics.large_run_driver <config.json> --resume   # 从最新检查点续算（见 §5）
python -X utf8 -m hydrodynamics.large_run_driver <config.json> --check    # 只校验配置与输入，不建网格、不计算
python -X utf8 -m hydrodynamics.large_run_driver <config.json> --quiet    # 不把进度事件回显到 stderr
```

返回码：`0` 完成（`--check` 时为 `status: ok`），否则 `1`。真实运行需要系统 `python`
（numpy 2.4、numba 0.64、netCDF4、pyproj、shapefile、pydantic）；numba 不是必需依赖，缺失时自动回退 numpy 核。

`--check` 读取网格 artifact JSON、断面审计、CSV、地形 npz（若存在）和配置，用 numpy 边表
（排序节点对，与 `TriangularMesh` 的边序一致）校验断面边为外边界边且与审计 `edge_nodes` 一致，
解析 CRS、估算磁盘需求，并打印 JSON 摘要：`status` 为 `ok` / `missing_inputs`（只有缺文件，列在 `missing`）/
`invalid`（`errors` 非空）。它不构建 `TriangularMesh`（约 35 s）。

## 2. 配置字段（`large-run-driver-config-v1`）

通用规则：未知字段拒绝；数字不接受字符串或布尔；所有数值必须有限；相对路径相对于配置文件所在目录解析。
校验错误以 `LargeRunDriverError` 抛出，消息带字段路径（如 `run.manning_n: Input should be less than or equal to 1`）。

| 字段 | 类型 / 单位 | 必填 / 默认 | 合法范围与校验 |
|---|---|---|---|
| `format` | const | `large-run-driver-config-v1` | |
| `schema_version` | const | `1.0` | |
| `description` | string 或 null | null | 仅说明 |
| `mesh.path` | 路径 | 必填 | `triangle-domain-mesh-only-artifact-v1` JSON；须含 `node_xy`、`face_nodes`、`face_id`（唯一非负 int64）、`coordinate_system`、`mesh_id` |
| `mesh.expected_sha256` | 64 位十六进制或 null | null | 给出时 artifact 的 SHA-256 必须一致 |
| `terrain.path` | 路径 | 必填 | `.npz`，数组 `face_id`（整数）与 `elevation_m`（浮点）；按 `face_id` 匹配到网格面序；缺面、重复、多余 id、非有限值均拒绝 |
| `terrain.dem_path` | 路径 | 必填 | 仅溯源；驱动不打开 DEM，只记录是否存在 |
| `terrain.dem_sha256` | 64 位十六进制 | 必填 | 仅溯源，按用户提供记录 |
| `terrain.mapping_report_path` | 路径或 null | null | 仅溯源；存在时记录 SHA-256，不存在时 `--check` 给出警告 |
| `terrain.method` | string | 必填 | 非空自由文本（映射方法标签） |
| `terrain.vertical_datum` | string 或 null | null | null 时派生产品中的水位标为不可用（sink 既有规则） |
| `terrain.vertical_unit` | enum | `m` | 只允许 `m` |
| `sections.path` | 路径 | 必填 | `assumed-river-sections-audit-v1` JSON（见 §3） |
| `sections.discharge_series_status` | map 断面键 → enum | 必填 | 每个审计断面都要有；`RECONSTRUCTED_NOT_MEASURED` / `MEASURED` / `COMPUTED_OUTFLOW`；未知断面键拒绝 |
| `sections.distribution` | map 断面键 → enum | `{}` | `length` 或 `conveyance_depth_5_3`；缺省 inflow=`length`，outflow=`conveyance_depth_5_3` |
| `forcing.path` | 路径 | 必填 | CSV，含 `time_s` 与审计里各断面的 `discharge_series_column`；`time_s` 严格递增且覆盖 `[0, 最后输出时刻]`；流量值有限且 ≥ 0（方向由断面 role 决定） |
| `forcing.rainfall` | null | null | v1 必须为 null（无降雨）；降雨列合同留待后续版本 |
| `run.hours` | int | 与 `output_times_s` 二选一 | [1, 8760]；输出时刻 0..hours 整点 |
| `run.output_times_s` | number[] s | 与 `hours` 二选一 | ≥ 2 个，首项 0，严格递增，末项 ≤ 31 536 000 |
| `run.initial_depth_m` | number m | 必填 | [0, 1000]，全域均匀 |
| `run.manning_n` | number s·m^-1/3 | 必填 | (0, 1]，全域均匀 |
| `run.long_run_options` | object | `{}` | 按 `triangular-long-run-options-v1` 校验；不得含 `checkpoint_interval_s`；`manning_friction` 不得为 `none` |
| `run.checkpoint_interval_s` | number s 或 null | null | (0, 31 536 000]；null = 不写检查点，`--resume` 不可用 |
| `run.time_origin` | ISO 8601 | 必填 | 必须带 UTC 偏移，如 `2023-08-01T02:00:00+08:00` |
| `run.run_id` | string | 必填 | `^[A-Za-z0-9][A-Za-z0-9._-]*$`，≤ 128 |
| `run.case_id` | string | 必填 | 同上；不得为 `result_native`（它命名派生的 `<case_id>.nc`） |
| `run.coordinate_system` | string | 必填 | 必须等于网格 artifact 的 `coordinate_system`（以及审计的 `crs`）；须为米制投影 CRS |
| `output.directory` | 路径 | 必填 | 运行目录；新运行时必须不存在；bundle 写到 `<directory>/bundle` |
| `output.profile` | object / 路径 / null | null | `LargeRunOutputProfile` 字段或 YAML/JSON 文件；null = 默认 |
| `output.export` | bool | true | 求解结束后执行导出 + 校验 |

## 3. 断面审计与流量

审计 JSON（`assumed-river-sections-audit-v1`）的 `sections` 为映射，键作为求解器边界组名与元数据 `group_id`
（`[A-Za-z0-9_]{1,64}`，不含 `__`）。每个断面：`role`（inflow/outflow）、`edge_indices`（网格外边界边索引，
唯一，不得跨断面重复）、`discharge_series_column`（CSV 列）、`name_zh`、可选 `edge_nodes`（与 `edge_indices` 一一对应，
驱动据此核对网格）、可选 `section_status`（缺省取顶层）、可选 `width_m`。顶层 `mesh_id`、`crs` 若存在必须与网格一致。
至少一个 inflow 断面。

求解器边界组：`{name: 键, role, edge_indices, time_s: CSV time_s, discharge_m3_s: CSV 列, distribution}`。
Q(t) 由求解器线性插值。无降雨（`rainfall_rate_m_s_by_interval=None`）。

元数据（`LargeRunMetadata`）：`data_origin=LIVE_SOLVER_RUN`、`live_solver_ran=true`、`production_approved=false`、
`forcing=river_assumed_sections_forcing(groups)`（标签即 "RIVER INFLOW/OUTFLOW AT ASSUMED (USER-CONFIRMED, NON-AUTHORITATIVE)
SECTIONS; NO RAINFALL; Q SERIES RECONSTRUCTED, NOT MEASURED"）、`dry_depth_threshold_m` 取求解器选项、`expected_times_s` 为输出时刻、
`vertical_datum` 取 `terrain.vertical_datum`。`solver` 块记录求解器名/版本（`triangular-long-run-1.0.0`）、选项、数值指纹、
地形溯源（路径、SHA-256、DEM 路径与 SHA-256、映射报告、方法、垂直基准）、断面与 CSV 摘要；`source_hashes` 记录
配置、网格、断面审计、CSV、地形 npz 的 SHA-256 和用户提供的 DEM SHA-256。

## 4. 运行目录与产物

```
<directory>/
  run_report.json       status: running | failed | complete（complete 仅在全部帧写完、sink 关闭、导出完成、verify PASS 之后）
  run_receipt.json      输入 SHA-256、选项、数值指纹、kernel、步数、各阶段墙钟、python/numpy/numba/netCDF4/pyproj 版本、续算信息
  progress.jsonl        每行一个 JSON 事件：start / inputs_ready / sink_opened / solver_progress / frame_written / solver_done /
                        sink_closed / export / export_done / verify_done / complete | failed
  ledger.jsonl          每个保存帧一行求解器 ledger（运行中逐帧追加，结束时按求解器 summary 重写，续算后仍完整）
  config.resolved.json  校验后、路径绝对化的配置
  checkpoints/          求解器的 checkpoint_XXXXXXXX.npz（驱动从不删除；保留个数由 checkpoint_keep_count 决定）
  bundle/               result_native.nc 及导出的全部交付件（docs/large_run_output_profile.md §4）
```

每帧交给 sink 的数组：`water_depth_m`、`unit_discharge_x/y_m2_s`、`velocity_x/y_m_s`、`water_level_m`（bed + h）。
每帧 ledger（NC 中 `mass_budget_<key>`）：`volume_m3`（= 求解器 `stored_volume_m3`，sink 用它核对 Σh·A）、
`initial_volume_m3`、全部 `interval_*_volume_m3` / `cumulative_*_volume_m3`、`residual_m3`、
`volume_correction_reference_scale_m3`、`minimum/maximum_water_depth_m`、`interval_accepted/rejected_step_count`，
以及每组 `groups.<断面>.cumulative_*`（扁平为 `groups__A__cumulative_inflow_volume_m3`）。`interval_wall_clock_s`
与可能为 null 的区间步长只保留在 `ledger.jsonl`。兼容 NC 的 `inflow`/`outflow` 列由导出器从
`interval_inflow_volume_m3` / `interval_outflow_delivered_volume_m3` 换算为前一区间平均 m³/s（第 0 帧为 0）。

close ledger：初始/末态体积、累计雨/入流/请求出流/交付出流/修正体积、`outflow_shortfall_m3`、每组角色与体积、
步数与拒绝原因、最小/最大步长、修正尺度与预算、`kernel`、`solver_version`。close maxima：求解器逐步的
`max_water_depth_m`、`max_speed_m_s`、`time_of_max_water_depth_s`。

失败：任何异常 → `sink.abort()`（`result_native.nc.partial` 改名 `.failed`，`status=FAILED`）、`run_report.json`
写 `failed` 与错误/traceback、检查点保留、异常继续抛出（CLI 返回 1）。

## 5. 续算（`--resume`）

前提：运行目录存在、`run_report.json` 状态不是 `complete`、`run.checkpoint_interval_s` 非 null、
`checkpoints/` 中有检查点（无检查点则拒绝，需新目录重跑）。流程：

1. 重新读入并校验全部输入（网格重建，指纹由求解器核对：输入或数值选项改变时拒绝）。
2. `NativeUgridNetcdfSink.reopen(mesh, metadata)`：以追加模式重开 `result_native.nc.partial`（或 `abort` 留下的 `.failed`，
   先改回 `.partial`）；核对元数据、profile、网格（face_id、face_nodes、节点坐标、床高）完全一致；读取全局属性
   `frames_written`（每写完一帧更新一次）得到已存在帧数 n；从已存的 float64 水深/单宽流量重建保存帧最大值与到达时刻；
   把 `status` 置回 `INCOMPLETE` 并在 `resume_history_json` 追加记录。若崩溃发生在数组写入与计数更新之间，
   多出的那半帧会被下一次 `write_frame(n)` 覆盖。
3. 求解器从最新检查点（输出索引 k，必须 k+1 ≤ n）继续，只发出 k+1 以后的帧；驱动跳过索引 < n 的帧
   （`frame_already_present` 事件），已存帧绝不重写。
4. 正常关闭、导出、校验；`run_report.json` 记 `resume_count`、`frames_present_before`、`resumed_from_checkpoint`。

若 `bundle/result_native.nc` 已完成而导出失败，`--resume` 跳过求解只做导出；bundle 中除 `result_native.nc`
外若有残留文件，驱动拒绝并提示手工移除（它从不删除文件）。限制：HDF5 文件在进程被强杀时可能损坏，
此时 `reopen` 报错，只能新目录重跑。测试 `test_resume_after_crash_matches_uninterrupted_run` 证明：在第 1 帧后于 sink
内抛错，续算后的 ledger、各帧水深/动量/水位、保存帧最大值与 close ledger 与不中断运行逐位一致。

## 6. 首个真实案例配置

`C:/Users/lenovo/hydro_exp/c8/real_case_config.json`（仓库外）：mesh005 artifact（178,962 面，EPSG:4548，SHA-256 已固定）、
地形 `terrain005_faces.npz`（待生成；`--check` 目前仅报它缺失）、断面审计 `sections_audit.json`（A 入流 12 边 / B、C 出流 22、21 边，
全部为外边界边且与审计 `edge_nodes` 一致）、`examples/case_001/data/forcing.csv`（289 行，0..1,036,800 s）、288 h、
初始水深 0、n=0.035、每 6 h 检查点、输出 `work/results/real_ds_mesh005_river_288h`。磁盘估算约 11.7 GB（×1.2）。

## 7. 限制

* 实验性，`production_approved=false`。无降雨、无闸门调度、无空间变化糙率与初始水深。
* 断面为假定断面（用户确认、非权威），Q 为重建值；该标签写入全部产物。
* 数值方案与选项见 `docs/triangular_long_run.md`，驱动不改变它们。
