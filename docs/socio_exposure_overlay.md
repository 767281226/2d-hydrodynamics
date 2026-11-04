# 社会经济暴露叠加（socio exposure overlay v1.0）

日期：2026-11-04。状态：opt-in 后处理；不改变求解器、不改变既有 bundle 产物（未启用时 run.json 仍为
`social_economy_status = NOT_READ_NOT_REQUESTED`）。实现：`hydrodynamics/socio_exposure.py`；
测试：`tests/test_socio_exposure.py`。

本功能把 50 m 社会经济网格（人口、耕地）按**精确面积权重**映射到原生三角面，并对每个保存帧与最大水深包络统计
暴露人口与暴露耕地。只做**暴露量**统计：没有资产价值、没有深度–损失函数，因此**不计算经济损失**。

## 1. 输入数据（只读）

| 文件 | 用途 | 说明 |
|---|---|---|
| `data/grid_50m_social_economy/shp/grid_cells_socio.shp`(+`.shx/.dbf/.prj`) | **主输入** | 59,488 个 50 m 网格多边形，WGS84 地理坐标（`.prj` 必须存在）；字段 `s_pop`（人/格）、`s_ar_mu`（亩/格）、`s_ar_km2`、`qu_dm`/`qu`（区代码/区名）、`sheng*`/`shi*` |
| `shp/grid_cells.shp` | 不使用 | 同一网格但**没有**人口/耕地字段（只有 row/col/x_km/y_km/lon/lat/z_m/hillshade/btype/gate） |
| `shp/boundary.shp`、`shp/gates.shp` | 不使用 | 网格边界单元与闸门点，与暴露统计无关 |
| `静海西青_七普三调.csv` | 可选上下文 | 区总量 `qu_dm,population,area_km2,arable_km2`；用于给出"域内占全区比例" |
| `面积汇总_按区.csv` | 可选交叉核对 | 每区网格数、网格人口合计、网格耕地（亩）合计；与读到的 shp 求和比对（容差 0.5 + 1e-4·值） |

数据语义（已核对）：两区内每格 `s_pop`、`s_ar_mu` **均匀**（静海区 1.3335 人/格、1.4292 亩/格；西青区按其总量同法），
即七普区常住人口与三调区耕地按面积均匀分摊到 50 m 网格。因此叠加结果**不含区内人口聚落分布**。

## 2. 方法

1. **重投影**：网格多边形顶点由 `.prj` CRS 经 pyproj（`always_xy`）逐点转换到运行 CRS（EPSG:4548）；在运行 CRS 中按直边处理
   （50 m 单元影响为亚厘米级）。缺 `.prj` 直接拒绝，不猜 CRS。
2. **精确相交**：每个原生三角面与其候选网格（shapely 2 `STRtree`）做平面精确求交，得到交面积 `a(f,c)`。
3. **面积加权**：`POP_f = Σ_c s_pop_c · a(f,c) / A_c`，耕地同理（`A_c` 为网格在运行 CRS 中的面积）。部分落在域外的网格只按其域内部分贡献。
4. **不填充**：没有任何网格覆盖的面 `POP/ARABLE = NaN`（SHP 中为空白），不是 0。部分覆盖的面只含被覆盖部分的值，`SOCIO_COV` 给出覆盖比例。
5. **守恒校核**（失败即报错，不写结果）：域多边形 = 全部三角形并集（独立计算）；
   - `Σ_f a(f,c) == area(c ∩ 域)`（逐格与总量）；
   - `Σ_f POP_f == Σ_c s_pop_c · area(c ∩ 域)/A_c`，耕地同；
   - 无面/格被超额分配；并集面积 == Σ 面面积（检出三角重叠）；
   - 网格自身重叠面积（重复计数风险）一并报告。
6. **暴露**：面深 `h ≥ 阈值`（默认 0.15 m）即暴露，计入该面全部映射值；深度分级 `[0.15,0.5)、[0.5,1.5)、[1.5,3)、[3,∞)` m。
   每个保存帧统计一次；包络使用保存帧最大水深 `saved_frame_max_water_depth`（若 NC 有 `solver_max_water_depth_m` 另给一组
   `SOLVER_MAX_ENVELOPE`）。按区拆分：每个面按其与各区网格的交面积拆分人口/耕地（一个面可跨两区）。
7. **缓存**：映射表写入 `socio_exposure/face_socio_mapping.npz`；若配置 `mapping_cache_dir`，另存为
   `face_socio_mapping_<网格指纹前16位>_<来源摘要前12位>.npz`，下次同一网格 + 同一来源直接复用（真实 15.9 万面：首算约 45 s，命中约 7 s）。
   网格指纹 = SHA-256(CRS, node_xy, face_nodes, face_id)；来源摘要 = SHA-256(shp/shx/dbf/prj/cpg 哈希 + 字段选择 + 目标 CRS)。

## 3. 配置（`LargeRunOutputProfile.socio_exposure`；schema `examples/large_run_output/large_run_output_profile.schema.json#/$defs/socio_exposure`）

| 选项 | 类型/单位 | 默认 | 合法范围/规则 |
|---|---|---|---|
| `grid_cells_path` | 字符串路径 | **必填** | 多边形 shp；`.shp/.shx/.dbf/.prj` 必须齐全；相对路径相对于当前工作目录 |
| `population_field` | 字段名 | `s_pop` | 1–10 字符；值须有限且 ≥ 0，不允许缺失（不填充） |
| `arable_mu_field` | 字段名（亩） | `s_ar_mu` | 同上；km² = 亩 / 1500 |
| `district_code_field` / `district_name_field` | 字段名或 null | `qu_dm` / `qu` | null = 不按区拆分 |
| `district_totals_csv` | 路径或 null | null | 列 `qu_dm,population,area_km2,arable_km2` |
| `district_grid_summary_csv` | 路径或 null | null | 列 `区代码,网格数,网格内常住人口_合计,网格内耕地_亩_合计` |
| `exposure_depth_threshold_m` | m | 0.15 | (0, 100]；暴露条件 `h ≥ 值` |
| `depth_class_edges_m` | m 数组 | [0.15, 0.5, 1.5, 3.0] | 1–10 个，非负、严格递增 |
| `mapping_cache_dir` | 路径或 null | null | 可复用映射缓存目录 |
| `conservation_rel_tol` | 1 | 1e-6 | (0, 0.01] |

未知键、字符串数字、非递增分级均被拒绝（错误带字段路径）。`profile.socio_exposure = null`（默认）时完全不读取社会经济数据。

## 4. 三种调用方式

1. **随运行导出**：驱动配置 `output.profile` 中写入 `socio_exposure`（见 `large_run_output_profile.example.yaml` 注释示例），
   `large_run_driver` 调用 `export_large_run_bundle(bundle, profile)` 时自动叠加，无需修改驱动。
2. **从 result_native.nc 重新导出**：
   `python -m hydrodynamics.large_run_export <只含 result_native.nc 的新目录> --profile socio_profile.yaml --verify`，
   或 `--socio data/grid_50m_social_economy/shp/grid_cells_socio.shp`（其余选项取默认/取 profile）。
3. **对已导出的 bundle 追加（就地，建议先复制）**：
   ```
   python -m hydrodynamics.socio_exposure <bundle> --socio data/grid_50m_social_economy/shp/grid_cells_socio.shp \
       --district-totals data/grid_50m_social_economy/静海西青_七普三调.csv \
       --grid-summary data/grid_50m_social_economy/面积汇总_按区.csv [--threshold 0.15] [--classes 0.15,0.5,1.5,3] [--cache-dir DIR]
   ```
   写 `socio_exposure/`，重写两个 `maximum_wet_surface*` SHP 包（加字段），更新 run.json / summary.json / parse_summary.json，
   重算 `checksums.json` 并执行 `verify_large_run_bundle`。已存在 `socio_exposure/` 时拒绝（no-clobber）。

须用含 pyshp 的环境（仓库 `.venv`）；系统 python 无 pyshp。

## 5. 输出（`<bundle>/socio_exposure/`，全部列入 `checksums.json`）

| 文件 | 内容 |
|---|---|
| `exposure_by_frame.csv` | 每行 = (聚合方式, 帧, 范围)；范围 `ALL`（全部）+ 每个区 |
| `exposure_summary.json` | 输入文件路径/SHA-256（shp/shx/dbf/prj/cpg、两份 CSV、result_native.nc）、选项、单位、映射报告（覆盖率、域内人口/耕地、守恒校核）、各区域内量及占全区比例、CSV 交叉核对、包络与峰值帧、方法说明、局限性、文件哈希 |
| `face_socio_mapping.npz` | `pair_face`、`pair_cell`、`pair_area_m2`、`cell_area_m2`、`cell_domain_area_m2`、`cell_pop`、`cell_arable_mu`、`cell_district`、`district_codes/names`、`mesh_fingerprint`、`source_digest`、`report_json` |

`exposure_by_frame.csv` 字段：

| 列 | 单位 | 含义 |
|---|---|---|
| `aggregation` | – | `FRAME` / `MAX_OVER_SAVED_FRAMES` / `SOLVER_MAX_ENVELOPE` |
| `frame_index`, `time_s`, `time_iso` | –, s, ISO 8601 | 帧序号、相对 time_origin 的秒数、绝对时刻（包络行为空） |
| `district_code`, `district_name` | – | `ALL`/`全部` 或区代码/区名 |
| `depth_threshold_m` | m | 暴露阈值 |
| `exposed_face_count` | 个 | 暴露面数（区行：与该区有交的暴露面） |
| `exposed_area_m2` | m² | 暴露面面积（区行：暴露面中属于该区网格的覆盖面积） |
| `exposed_area_without_socio_m2` | m² | 暴露但无网格覆盖的面积（仅 ALL 行；这部分人口/耕地不可得） |
| `exposed_pop` | 人 | 暴露人口 |
| `exposed_arable_mu` / `exposed_arable_km2` | 亩 / km² | 暴露耕地 |
| `pop_d<lo>_<hi>`、`arable_mu_d<lo>_<hi>`、`area_m2_d<lo>_<hi>` | 人、亩、m² | 各深度级（如 `pop_d0.5_1.5`，末级 `pop_d3_inf`） |

`maximum_wet_surface` 与 `maximum_wet_surface_4490` SHP 包附加字段（manifest `fields` 同步记录）：

| DBF 字段 | 类型 | 单位 | 含义 |
|---|---|---|---|
| `POP` | N(20,6) | 人 | 面映射人口；无网格覆盖为空白 |
| `ARABLE_MU` | N(20,6) | 亩 | 面映射耕地；无覆盖为空白 |
| `SOCIO_COV` | N(12,6) | 1 | 面被网格覆盖的面积比例 0–1 |
| `QU_DM` | C(12) | – | 按覆盖面积占优的区代码；无覆盖为空 |
| `EXPOSED` | N(2,0) | – | 1 = 保存帧最大水深 ≥ 暴露阈值 |

run.json 新增 `socio_exposure` 块（`status=COMPLETE_EXPOSURE_ONLY`、文件路径、summary SHA-256、域内总量、包络暴露量、
`economic_loss=NOT_COMPUTED_NO_ASSET_VALUES`），`social_economy_status` 同步。`verify_large_run_bundle` 校核：
summary 哈希与 run.json 一致、CSV/npz 哈希、npz 网格指纹与面数、CSV 帧数、守恒状态 PASS、两个 SHP 包含附加字段。

## 6. 局限性（同时写入 exposure_summary.json）

- 人口 = 七普区常住人口按面积均匀分摊到 50 m 网格；耕地 = 三调区耕地同法分摊；**区内无空间差异**，域内人口/耕地与"域内面积占全区比例"成正比。
- 只给暴露量；无资产价值与损失曲线，不计算经济损失。
- 面平均水深 ≥ 阈值即整面计入，不考虑面内水深分布。
- 帧暴露只看保存帧；包络为保存帧最大水深（帧间峰值不可见）。
- 无网格覆盖的面（域边缘约 0.5% 面积）为 NaN，其暴露面积单列，不当作 0 人口。
- 结果继承水动力运行的标签（forcing notice）；非 production 运行不是权威洪水图。

## 7. 真实核对（trial_2h，159,349 面，EPSG:4548；原 bundle 未修改，在副本上执行）

- 域面积 146.31 km²；网格覆盖 99.52%（未覆盖 0.71 km²，136 面 NaN，4,216 面部分覆盖）。网格 59,488 格中 56,801 全在域内、1,684 部分、1,003 域外。
- 域内人口 131,590.8 人（网格全量 133,716.9）；域内耕地 71,102.4 亩 = 47.40 km²。静海区 59,210.8 人 / 63,460.1 亩（占全区七普人口 7.52%），
  西青区 72,380.1 人 / 7,642.3 亩（6.06%）。CSV 交叉核对两区 PASS；守恒相对误差 ~1e-16。
- 2 h（帧 2，阈值 0.15 m）：暴露 12,672 面、11.52 km²，暴露人口 6,106.0 人、耕地 6,544.3 亩（4.36 km²），全部在静海区；
  分级人口 0.15–0.5 m 1,017.4 / 0.5–1.5 m 4,208.0 / 1.5–3 m 750.1 / >3 m 130.5。1 h：3,262.6 人、3,496.7 亩。
- 两条路径（就地追加、从 result_native.nc 重新导出）得到逐字节相同的 `exposure_by_frame.csv`；`verify_large_run_bundle` PASS。

## 8. 未做

viewer 未加社会经济图层：需要新增逐面二进制资产、viewer_server 的大 bundle 校验与图层切换 UI，不属于低成本改动；
暴露结果请用 SHP（`POP`/`EXPOSED` 字段）在 GIS 中查看。
