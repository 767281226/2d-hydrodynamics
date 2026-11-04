# 大规模运行输出 profile（large-run output profile v1.0）

日期：2026-11-03。状态：opt-in 实现；M1 小样本 bundle（14c984c）合同、上限与测试不变。

本 profile 让全量真实运行（约 15–19 万三角面、289 个小时帧 0…288 h、起点
`2023-08-01T02:00:00+08:00`）以流式方式落盘并导出全部交付件。它不含求解器方程；
求解器通过 `FrameSink` 逐帧交出 numpy 数组。

## 1. FrameSink 接口（`hydrodynamics/frame_sink.py`）

```python
sink = NativeUgridNetcdfSink(bundle_dir / "result_native.nc", profile)
sink.open(mesh, metadata)                      # 一次
sink.write_frame(index, time_s, arrays, ledger=None)   # 每个保存帧，index 从 0 连续
report = sink.close(maxima=None, ledger=None)  # 成功结束；失败时 sink.abort(reason)
```

| 参数 | 类型/单位 | 必填 | 规则 |
|---|---|---|---|
| `mesh.node_xy_m` | float64 (N,2) m | 是 | 有限，投影米制 CRS |
| `mesh.face_nodes` | int (F,3) | 是 | 0 基，三点互异，面积 > 0 |
| `mesh.face_id` | int64 (F,) | 否（默认 1..F） | 唯一、非负；面顺序即权威顺序 |
| `mesh.bed_elevation_m` | float64 (F,) m | 是 | 有限 |
| `mesh.face_area_m2` | float64 (F,) m² | 否 | 给出时须与平面几何一致（rtol 1e-9） |
| `arrays["water_depth_m"]` | (F,) m | 是 | 有限、≥0 |
| `arrays["unit_discharge_x_m2_s"/"_y_"]`（别名 `momentum_*`, `qx/qy`） | (F,) m²/s | 二选一或都给 | 成对给出 |
| `arrays["velocity_x_m_s"/"_y_"]`（别名 `u/v`） | (F,) m/s | 二选一或都给 | 成对给出；缺失时由 q/h（h>干阈值）导出 |
| `arrays["water_level_m"]` | (F,) m | 否 | 缺省为 bed+h；若给出须从第 0 帧起每帧都给 |
| `ledger`（每帧） | 有限标量；可嵌套一层（如每个边界组） | 否 | 键 `[A-Za-z0-9_]+`；第 0 帧起键集不变；嵌套键扁平化为 `a__b__c` |
| `maxima`（close） | (F,) | 否 | `max_water_depth_m`,`max_speed_m_s`,`max_water_level_m`,`time_of_max_water_depth_s`,`arrival_time_s`（NaN=未淹）；不得低于 sink 自己统计的保存帧最大值 |
| `ledger`（close） | 标量/字符串/逐帧序列/嵌套映射 | 否 | 序列长度=帧数；如每组入流/出流体积、出流不足量 shortfall |

识别的每帧 ledger 键：`volume_m3`（与 Σh·A 校核，`volume_check_rel_tol`），`rain_depth_mm`（前一保存区间雨深），
`inflow_m3_s`/`outflow_m3_s`（保存时刻总流量）或 `inflow_volume_m3`/`outflow_volume_m3`（前一区间体积，换算为 m³/s）。
其余键原样保存为 `mass_budget_<key>` 变量。

NC 写法：NETCDF4、`time` 为 unlimited 维、chunk `(1, native_nc_face_chunk)`、zlib；运行中文件名
`result_native.nc.partial`，`status=INCOMPLETE`；`close` 成功后 `status=COMPLETE` 并原子改名；失败改名为
`.failed` 留作诊断。内存只保留当前帧与 F 长度的运行统计（保存帧最大水深/时刻、最大流速、最大水位、到达时刻）。

close ledger 去向：数值标量 → 全局属性 `ledger_<flat_key>`；序列 → `mass_budget_<flat_key>(time)`；完整原样 JSON →
字符串变量 `mass_ledger_json`；同时写入 bundle 的 `summary.json`（`mass_ledger`、`boundary_group_ledger_series`、
`outflow_shortfall`）与 `mass_budget/summary.json`。

## 2. 运行标识（`LargeRunMetadata`，`large_run_profile.py`）

必填：`run_id`, `case_id`, `time_origin`（ISO 8601，必须带时区偏移）, `coordinate_system`（投影米制）,
`dry_depth_threshold_m`（0–10 m）, `data_origin`（`LIVE_SOLVER_RUN`|`SYNTHETIC_TEST_FIELDS`）, `live_solver_ran`, `forcing`。
可选：`expected_times_s`（给出则每帧时间必须精确一致，且触发磁盘预检）、`vertical_datum`（缺省时 water_level 标为不可用）、
`production_approved`（默认 false；合成数据不得为 true）、`solver`、`source_hashes`。

`forcing`（必填，无默认）：`scope`（RAIN_ONLY|RIVER_ONLY|RAIN_AND_RIVER|NONE）、`boundary_policy`、
`rainfall_included`、`river_inflow_outflow_included`、`river_section_status`、`discharge_series_status`、
`boundary_groups[]`（`group_id`,`role`,`name`,`section_status`,`discharge_series_status`）、`notice`。
一致性：river 运行须 `open` 边界、至少一个 inflow 组、声明断面/流量状态；非 river 运行不得带组。

首个真实运行的标签使用 `river_assumed_sections_forcing(groups)`：
"RIVER INFLOW/OUTFLOW AT ASSUMED (USER-CONFIRMED, NON-AUTHORITATIVE) SECTIONS; NO RAINFALL; Q SERIES RECONSTRUCTED,
NOT MEASURED"（中文同时给出）。该标签写入 native NC、兼容 NC 全局属性、run.json/summary/report、native_web manifest、
每个 SHP manifest、stepjson_native index，并在 viewer 侧栏以醒目横幅显示。兼容 NC 中 `rainfall`=0
（`ZERO_NO_RAINFALL_IN_RUN`），`inflow/outflow` 来自求解器 ledger（`AVAILABLE_SOLVER_LEDGER_ASSUMED_SECTIONS_RECONSTRUCTED_Q`）。
仅降雨闭边界运行用 `rain_only_forcing()`，此时 inflow/outflow=0 并标 `ZERO_CLOSED_BOUNDARY_RIVER_INFLOW_OUTFLOW_NOT_INCLUDED`。

## 3. Profile 选项（`LargeRunOutputProfile`；schema `examples/large_run_output/large_run_output_profile.schema.json`）

| 选项 | 类型/单位 | 默认 | 合法范围 |
|---|---|---|---|
| `max_face_count` / `max_node_count` / `max_frame_count` | int | 2,000,000 / 4,000,000 / 10,000 | 1–2e7 / 3–4e7 / 1–1e5 |
| `native_nc_complevel` | int | 4 | 0–9 |
| `native_nc_face_chunk` | int 面 | 65,536 | 1–16,777,216 |
| `display_grid_cell_size_m` | m | 50.0 | 1–10,000 |
| `display_grid_anchor_x_m`/`_y_m` | m | 474150 / 4299600 | 有限 |
| `max_display_grid_cells` | int | 4,000,000 | 1–5e7 |
| `compat_nc_complevel` | int | 4 | 0–9 |
| `native_web_value_dtype` | enum | float32 | float32/float64 |
| `stepjson_min_depth_m` | m | 0.0 | 0–100（记录条件 depth > 值） |
| `stepjson_still_direction_deg` | deg | 180 | [0,360) |
| `write_stepjson_native` | bool | true | |
| `shp_wet_depth_threshold_m` | m 或 null | null（=干阈值） | 0–100 |
| `volume_check_rel_tol` | 1 | 1e-6 | (0,0.1] |
| `disk_safety_factor` / `disk_preflight` | 1 / bool | 1.2 / true | 1–10 |

未知键、字符串数字、布尔冒充数字均拒绝，错误信息带字段路径。

## 4. 导出（`hydrodynamics/large_run_export.py`）

`python -m hydrodynamics.large_run_export <bundle_dir> [--profile p.yaml] [--verify]`，或
`export_large_run_bundle(bundle_dir, profile)` + `verify_large_run_bundle(bundle_dir)`。一次逐帧读取 native NC，
同时写兼容 NC、native_web 帧与 stepjson_native，然后用既有 nc2web/stepjson 编码器（逐帧读兼容 NC）生成 v1 产物，
最后导出四个 SHP 包与元数据。输出不覆盖既有文件。

- 兼容显示格：锚点 A 的 50 m 格（规则同 `parallel_output_integration/full_dem_final_acceptance_matrix_and_work_order.md` §2），
  `grid_id_digest` 为规范 identity 的 SHA-256。三角形∩格面积用向量化 Sutherland–Hodgman 精确裁剪计算，
  并校核 Σ覆盖面积=Σ原生面积（rel 1e-9）、每面覆盖=面面积。稀疏映射表 `compatibility_grid_mapping.npz`，
  覆盖率/覆盖面积二进制 `compatibility_grid_coverage.bin`；`compatibility_grid.json` 为 v2（不再内联逐格列表）。
- `<case_id>.nc`：旧 20 个变量 + `active_coverage_fraction`、`source_covered_area_m2`，chunk (1,ny,nx)。
- `nc2web/`、`stepjson/`：既有 v1 编码器，结构与 M1 一致，`{"all":["id_depth_speed_direction",...]}`，ID 为
  1 基 row-major 格 ID。`stepjson_native/` 为附加件（同结构，ID 为原生 face_id 字符串）。
- `native_web/`：`native-triangle-web` 2.0；`mesh.bin` 与 v1 相同布局；每帧单独文件 `frame_NNNNNN.bin`
  （magic `NTW3F02\n`，32 字节头，末字段=每值字节数，float32 默认），manifest 每帧记录 sha256/bytes。
- `shp/`：`full_domain`、`full_domain_4490`（末帧 FRAME 字段 + 最大值/到达/地形附加字段）、
  `maximum_wet_surface`、`maximum_wet_surface_4490`（保存帧最大水深 > 阈值的面）；每包 `.shp/.shx/.dbf/.prj/.cpg`
  + `manifest.json`；每个要素是一个真实原生三角（顺时针，三个原顶点），DBF `face_id` 为 C32 文本。
  4490 用 pyproj 逐顶点转换并在 manifest 记录版本。
- `run.json`（`large-run-output-bundle-v1`）、`run_report.json`、`summary.json`、`parse_summary.json`、
  `mass_budget/`、`quality/`、`provenance/`、`logs/`、`checksums.json`（全部文件 bytes+SHA-256）。

## 5. Viewer 与 launcher

`start_viewer.bat <bundle_dir>` 不变。`viewer_server` 对 large bundle：必须显式路径（除非 production_approved 且
can_publish）；核对 checksums 的文件清单与全部文件大小，元数据/mesh/coverage 的 SHA-256；native_web 2.0
manifest 的 run_id、时间轴、帧列表与文件大小；兼容格 v2 identity。页面只预载 mesh.bin（约 11 MB/19 万面），
每次只请求当前帧并预取下一帧（缓存 ≤2 帧、≤32 MiB），每帧载入时校验 SHA-256。大网格按颜色桶批量填充 Path2D（每个 Path2D 最多 2048 面后即绘制并重建；Chromium 中单个超大 Path2D 追加为超线性，19 万面单帧曾需 100+ 秒），
兼容格用 ImageData 绘制；侧栏显示 forcing 标签、范围、面数、帧数与时间起点；悬停显示精确 face_id。

真实求解器接入（网格 artifact + 逐面地形 + 假定断面 + 流量 CSV → 本 sink → 导出/校验）与 `reopen` 续算见
`docs/large_run_driver.md`（`python -m hydrodynamics.large_run_driver`）。

## 6. 测试

- 仓库：`tests/test_large_run_output.py`（小网格、两种 forcing、拒绝路径、面积守恒、stepjson 格式一致、viewer 校验）。
- 全量合成：`examples/large_run_output/run_large_synthetic.py --mesh <npz> --output <仓库外目录>`。合成字段只测 I/O 规模，
  `data_origin=SYNTHETIC_TEST_FIELDS`，不代表水动力结果。
