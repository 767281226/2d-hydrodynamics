# 全量DEM原格式结果：M0已知合同与M1小样本映射

日期：2026-10-26。依据 `full_dem_output_compatibility_work_order_2026-10-26.md`、`full_dem_output_readonly_findings_2026-10-26.md` 和 `work/full_dem_output_feasibility_20261026/`。

## 状态与范围

M0只能冻结当前有实物支撑的旧NC/codec合同。当前项目未找到旧完整成功bundle；`work/results/`里唯一现存run失败，不能作为成功基准。旧NC `examples/case_001/results/dongdian_qingbei_2023_288h.nc` 是20变量、289帧、719×507的hourly hydrostatic DEM fill；它是格式/字段参考，不能当作CFL-limited HLL三角水动力结果。未知旧目录和可视化资产继续标为待核。

M1使用已存在且经当前严格P2/P3/native reader核验的两个小样本：2面` sources=none` native结果，及4面Manning/rainfall、带安全区闭壁约束的native结果。生成物状态统一为`QA_FIXTURE_READY_NOT_PRODUCTION`，`qa_fixture=true`，`live_solver_ran=false`，`production_approved=false`，不在`work/results`，不进latest，不作为全量结果交付。

## 全量工程域冻结选择

用户已选择：全量DEM仅作地形来源，计算域采用**既有真实D几何扣除已验证安全区S**。这不表示所有有效DEM像元都进入计算，也不表示1m地形必须用1m计算网格。真实D/S geometry文件、CRS、哈希和security exclusion证据是后续M2的权威输入；M2必须重新校验并从实际几何计算面积、三角覆盖和DEM有效覆盖。本M1样本的网格范围不是清北D-S，任何估算面积都不写成常量或伪装成通过。

开放边界线段与P3 group归属仍未绑定到实际工程输入；本批保持现有closed-wall样本，只为native result数据出口做验证，不创建流量/出流映射，不跑全量求解。

## 旧NC 20变量映射

固定的M1兼容网格为1m显示格，格值由native三角面与显示格的平面交叠面积计算。对部分覆盖格，保存“覆盖native面积上的面积均值”及`active_coverage_fraction`；viewer根据单元内归一化交叠几何裁剪兼容格的可视部分，不把网格外/安全排除/无覆盖部分涂成受淹区域。兼容显示格只是derived display，SHP与`native_web`仍直接使用原三角面、节点和face_id。

| 旧变量 | M1映射与可用性 | 语义限制 |
|---|---|---|
| `time` | native saved time，float64秒 | time origin照搬来源；空origin显示相对秒 |
| `x`,`y` | 1m兼容显示格中心坐标，float64米 | 新grid ID空间；不宣称与旧50m row-major ID代表同一位置 |
| `water_depth` | 覆盖面积上的native depth均值 | partial cell同时有coverage fraction；不是native face值 |
| `water_level` | P3 bed elevation与depth的覆盖面积均值；沿用原垂直datum标签 | 不做datum转换；源datum缺失时整变量为NaN并标记不可用 |
| `velocity_x`,`velocity_y` | 覆盖面积上的native momentum均值除以depth均值；mapped depth为正时保留比值，只有depth精确为0时速度取0 | 聚合格不套native face干阈值；是显示格的derived速度，不能替代face velocity |
| `velocity_magnitude` | `hypot(mapped velocity_x, mapped velocity_y)` | 对齐旧字段名，面积均值后再求模 |
| `discharge` | `mapped depth × mapped velocity_x × display_dy` | 保留旧网格诊断公式；明确不是native edge flux或面流量 |
| `elevation` | P3 terrain elevation覆盖面积均值 | NoData/网格外无覆盖为NaN；不填DEM NoData |
| `active_mask` | 有任何有效native面交叠则1，否则0 | 安全区等没有native活动面的位置为0 |
| `maximum_depth` | 所有native saved frames映射后的最大值 | 仅saved-frame maximum，不代表内步连续峰值 |
| `maximum_velocity` | 所有saved frames映射后velocity magnitude的最大值 | 仅saved-frame maximum |
| `arrival_time` | 首个`depth > native dry_depth_threshold_m`的saved frame | 阈值写入metadata；未达到为NaN，不等于0秒 |
| `inflow`,`outflow` | closed-wall样本为0 | 仅因样本boundary policy是closed wall；工程开放边界未知，M3之前不可套用 |
| `rainfall` | source none为0；已应用rainfall按前一saved interval累计深度换算mm，初始帧0 | 保留已验证的source schedule，不能从水位差推造雨量 |
| `eta` | NaN，`NOT_AVAILABLE_LEGACY_HYDROSTATIC_FILL` | 旧全局hydrostatic fill参数没有同义native SWE字段，不填0、不宣称映射成功 |
| `volume` | `Σ(native depth × native face area)` | 每帧与native `mass_budget_final_volume_m3`独立核对 |
| `wet_area` | `Σ(native face area where depth > dry threshold)` | 原native拓扑湿面面积口径，非`wet cell count × dx × dy` |
| `active_coverage_fraction`（新增） | native faces与display cell交叠面积 / source-CRS display cell面积 | 用于显式辨识partial display cells |
| `source_covered_area_m2`（新增） | 每格native face与display cell在source CRS中的实际交叠面积，单位m² | 兼容湿格覆盖面积对mapped depth严格大于0的显示格按该值求和；重投影后的web dx×dy不替代source/native物理面积 |

新写兼容NC以NaN作为缺值，0保留为有效物理值。旧NC若使用`_FillValue=0`并同时提供`active_mask`，codec只恢复活动格、明确可用变量中，netCDF掩码下的原始值确实等于零fill的样本；非零`missing_value` sentinel、把0也声明为缺失的变量、超出`valid_range`的0、非有限值、不可用变量和inactive格仍为NaN。`stepjson`与nc2web共用此规则，静止水流保留speed=0及方向180°，真实缺测速度不制造静水记录。native湿面面积使用native face干阈值；派生兼容湿格覆盖面积按mapped depth>0计算，两个指标分开记录。旧workflow `run.json`按其真实字段只读归一化；viewer不写回历史文件，也不从其status臆造`production_approved`，只允许显式路径打开此旧格式。viewer端口在Windows独占绑定，启动URL带expected run_id，页面会核对运行身份。切帧仅当最新选择的profile与generation相符时才同时提交帧数据、时间、HUD和seek值；prefetch缓存最多2帧且总计32MiB。

`eta`、工程`inflow/outflow`与数据库/社会经济输出不在M1伪造。数据库为`DISABLED_NOT_REQUESTED`，社会经济资料为`NOT_READ_NOT_REQUESTED`。旧NC单位、原点/spacing、time列及字段集合记录于`work/full_dem_output_feasibility_20261026/known_output_contract.json`。

## 逐件产物矩阵（当前已知合同）

| 旧件/路径 | 新件或M1证据 | 来源与验证 | 当前结论 |
|---|---|---|---|
| 未找到的旧成功完整bundle / 记录中的`100001` | M0清单，不创建替代基准 | 只读核对当前`work/results`和历史记录 | 缺成功bundle；不宣称superset全过 |
| `examples/case_001/results/dongdian_qingbei_2023_288h.nc` | `result_native.nc` + `<case_id>.nc` derived compatibility NC | old NC用于字段合同；native NC hash及P2/P3链单列 | hydrostatic旧NC不是native结果 |
| `nc2web/manifest.json` | 同名v1文件，键结构不变 | 只读验证`nc2web` v1 manifest/binary schema | M1兼容codec |
| `nc2web/mesh.bin` | 同名v1文件，`NC2WM01\n`、little-endian | header和长度按v1结构验证 | 衍生显示格，非native mesh |
| `nc2web/frame_NNN.bin` | 同名v1帧文件，`NC2WF01\n`、little-endian float32 | 每帧magic/dims/field顺序检查 | 逐帧生成，有限内存 |
| `stepjson/index.json` | 同名v1 index，保留原字段 | 逐字段回归 | M1兼容codec |
| `stepjson/step_NNN.json` | 精确`{"all":["id_depth_speed_direction",...]}` | 旧ID规则、三/二/一位格式与still direction回归 | row-major是derived grid ID |
| `native_web/manifest.json`,`mesh.bin`,`frame_NNNNNN.bin` | 新版native UGRID web profile | source SHA、face集合、三角节点、64位精确字符串face_id、逐帧和浏览器检查 | native viewer默认层 |
| 四个旧SHP角色 | `FULL_DOMAIN/MAXIMUM_WET_SURFACE × NATIVE/EPSG:4490`四包 | 每包原生face几何、ID、sidecar、manifest与严格validator | 2面与4面QA分别新导出，不覆旧包 |
| `viewer.html`,`start_viewer.bat` | bundle viewer及严格本地launcher | native/compat两层、HTTP、帧/图层/hover/错误/viewport | 不使用wet bbox裁native域 |
| `run.json`,`parse_summary.json`,`run_report.json`,`summary.json`,`logs/`,`quality/`,`mass_budget/`,`provenance/`,`checksums.json` | M1每sample run metadata和验收证据 | 内容hash及文件矩阵 | `qa_fixture`,不可发布为生产run |
| `*_gate_dispatch.csv/json`,`dispatch_input.json` | 只记录NOT_REQUESTED状态 | 当前小样本无gate-dispatch input | 旧成功bundle缺失，schema待基准补证 |
| 数据库回执 | 不生成；status=`DISABLED_NOT_REQUESTED` | 明确无连接、无写入 | 不冒称成功入库 |
| 社会经济/其他叠加图层 | 不读取；status=`NOT_READ_NOT_REQUESTED` | 原成功bundle缺失且本批无社会经济授权 | 文件名/字段待旧bundle补证 |

本矩阵只冻结本批可观察的旧合同。旧成功完整bundle位置仍是最终“可多不可少”逐件核对的必要基准；本M1不把缺失件标成通过。
