# 通用方案流水线（case_pipeline）

用一个配置文件，把任意 DEM 加上计算范围、断面和流量，依次跑完网格、地形映射、断面选取、计算、导出和校验。
模块：`hydrodynamics/case_pipeline.py`；配置格式：`case-pipeline-config-v1`；
示例：`examples/case_pipeline/case.example.yaml`；JSON Schema：`examples/case_pipeline/case_pipeline_config.schema.json`。

```
python -X utf8 -m hydrodynamics.case_pipeline case.yaml --check          # 轻量预检：配置、文件、DEM 坐标系和范围、断面距离、CSV 列
python -X utf8 -m hydrodynamics.case_pipeline case.yaml --stage mesh     # 只跑到某一阶段
python -X utf8 -m hydrodynamics.case_pipeline case.yaml                  # 全流程（默认 --stage run）
python -X utf8 -m hydrodynamics.case_pipeline case.yaml --resume         # 从最新检查点续算
python -X utf8 -m hydrodynamics.case_pipeline x --print-schema           # 打印配置 JSON Schema
start_viewer.bat <run_id>                                                # 查看结果
```

## 阶段

| 阶段 | 做什么 | 输出（work_dir 下） | 调用的已有模块 |
|---|---|---|---|
| geometry | 读外边界多边形（坐标 / GeoJSON / SHP），减去孔洞，简化边界，检查约束线 | `geometry/geometry.json` | shapely、pyshp |
| mesh | Gmsh 约束三角网格，统计质量 | `mesh/mesh.json`（triangle-domain-mesh-only-artifact-v1） | `mesh_generation.generate_gmsh_mesh` |
| terrain | 逐单元精确面积加权取 DEM 原始值 | `terrain/terrain_faces.npz`、`terrain/mapping_report.json` | `terrain_windowed_mapping` |
| sections | 按 DEM 主槽规则在外边界上选入流/出流断面边 | `sections/sections_audit.json` | `river_section_selection` |
| config | 生成运行驱动配置 | `driver_config.json`（large-run-driver-config-v1） | — |
| run | 计算 → 原生 NetCDF → 结果包导出 → 校验 | `<output.root>/<run_id>/bundle` | `large_run_driver` |

每个阶段写一个 `stage.json`，里面记录输入的哈希。输入没变时自动跳过该阶段，例如只改流量 CSV 不会重算网格和地形。`--force` 强制重算。

## 参数（类型、单位、默认值、范围）

| 字段 | 类型/单位 | 默认 | 范围/规则 |
|---|---|---|---|
| case_id | 字符串 | 必填 | `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` |
| work_dir | 路径 | 必填 | 相对配置文件目录或绝对路径 |
| dem.path | 路径 | 必填 | GeoTIFF |
| dem.crs | 字符串 | 必填 | 投影坐标系、单位米；DEM 坐标系须与之等价（pyproj 判定，忽略轴序） |
| dem.sha256 | 64 位十六进制 | null | 给出时必须与文件一致 |
| dem.vertical_unit / _evidence | `m` / 相对路径 | null | 只在 GeoTIFF 缺少高程单位时声明，两者必须同时给出 |
| dem.vertical_datum / _evidence | 字符串 / 相对路径 | null | 两者必须同时给出；不做基准换算 |
| domain.polygon | 坐标 / geojson / shp | 必填 | 合并后必须是单个连通多边形；SHP 的 .prj、GeoJSON 的 crs 必须与 dem.crs 等价 |
| domain.holes | 多边形列表 | [] | 必须严格位于外边界内部；孔洞边界在计算中是闭边界（墙） |
| domain.constraints | `{name, points}` 列表，m | [] | 折线必须严格位于计算范围内 |
| domain.simplify_tolerance_m | m | null | 0–1000 |
| domain.min_vertex_spacing_m | m | null | >0–1000 |
| mesh.default_size_m / minimum_size_m | m | 50 / 1 | >0–10000，最小值不大于默认值 |
| mesh.boundary_adaptive / grading_ratio | 布尔 / 1 | true / 0.3 | 0–1 |
| mesh.maximum_nodes | 个 | 2 000 000 | 3–10 000 000 |
| mesh.algorithm_2d | 整数 | 5 | 1–11（Gmsh Mesh.Algorithm） |
| terrain.max_source_pixels / max_candidate_overlap_checks | 个 | 1e9 | 1–1e9；网格单元数上限 500 000 |
| sections.items[].name | 字符串 | 必填 | `[A-Za-z0-9_]{1,32}`，不能重复 |
| sections.items[].role | inflow / outflow | 必填 | |
| sections.items[].xy | [x, y]，m | 必填 | 到外边界的距离 ≤ max_point_distance_m |
| sections.items[].column | 字符串 | 必填 | 流量 CSV 的列名，m³/s，取非负值 |
| sections.items[].status | RECONSTRUCTED_NOT_MEASURED / MEASURED / COMPUTED_OUTFLOW | RECONSTRUCTED_NOT_MEASURED | |
| sections.items[].boundary_type | prescribed_outflow / free_outflow | prescribed_outflow | 只用于出流断面 |
| sections.items[].discharge_coefficient | 1 | null（约 0.5443） | 0–1.5；只用于 free_outflow |
| sections.items[].crest_elevation_m | m（DEM 基准） | null（取单元底高程） | −1000–10000；只用于 free_outflow |
| sections.max_point_distance_m | m | 100 | >0–10000 |
| sections.rule | 对象 | {} | 见 river_section_selection 的 ChannelRuleParameters |
| forcing.path | 路径 | 必填 | 含 `time_s` 和各断面列，覆盖 0…hours×3600 s |
| run.hours | h | 必填 | 1–8760，逐小时输出 |
| run.manning_n | s·m^-1/3 | 0.035 | >0–1 |
| run.initial_depth_m | m | 0 | 0–1000 |
| run.time_origin | ISO 8601 带时区 | 必填 | |
| run.cfl_number | 1 | 0.5 | >0–1 |
| run.kernel | auto / numpy / numba | auto | |
| run.checkpoint_interval_s | s | 21600 | null 表示不写检查点，也就不能续算 |
| output.root / run_id | 路径 / 字符串 | work/results / 自动 | 结果目录为 `<root>/<run_id>` |
| output.profile | 对象或路径 | null | LargeRunOutputProfile |

## 不做的事

- 不做坐标转换和重采样，不填补 NoData。计算范围或断面落在 DEM 外、或单元完全落在 NoData 上时，直接报错并给出单元编号。
- 计算范围和断面位置由使用者提供。程序只检查，不判断该淹哪里、河从哪里进出。
- 求解器能力不变：不支持降雨（v1 固定为 null）、按水位调度闸门、堤防漫顶。
- 1 m DEM 上做精确地形映射，8 万个单元大约要 4–5 小时，这是正常的。

## 验证

`tests/test_case_pipeline.py` 用一个合成 DEM（EPSG:32650，2 km × 1 km 河谷）测试：
- 配置错误能被拒绝
- 计算范围超出 DEM、断面离边界太远时预检报错
- GeoJSON 加孔洞加约束线能正确生成几何
- 单元落在 NoData 上时拒绝映射
- 全流程跑 1 h，导出后校验结果为 PASS，并验证阶段缓存会跳过未变的阶段
