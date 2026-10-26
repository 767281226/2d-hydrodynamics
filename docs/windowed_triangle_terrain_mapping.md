# 原生三角网格的窗口地形映射

`hydrodynamics.terrain_windowed_mapping` 的 `triangle-terrain-windowed-v1` 路线只映射已验证的 `TriangularMesh` 与原始单波段DEM窗口，不生成网格、不调用求解器。返回 `FaceTerrainField` 和逐面诊断。旧完整读取路线及其像元上限保持独立。

运行依赖使用 `pip install '.[raster,triangular]'`；测试使用 `pip install '.[test,raster,triangular]'`。正式JSON Schema验证是测试依赖，映射运行时不依赖jsonschema。

配置示例和规范见 [example](../examples/triangular_mesh/windowed_mapping_config.example.json) 与 [schema](../examples/triangular_mesh/windowed_mapping_config.schema.json)。以 `WindowedTriangleMappingConfig.model_validate` 加载，拒绝未知字段、bool/string/fraction整数伪装、非有限数值及越界值。数学整数形式如1.0与JSON Schema integer语义一致。

| 输入 | 类型、单位和范围 | 默认或required |
| --- | --- | --- |
| format | string，固定triangle-terrain-windowed-v1 | 固定值 |
| max_source_pixels | integer，pixels，1–1000000000；身份规模而非内存数组 | required |
| tile_rows / tile_cols | integer，pixels，各1–256，乘积≤65536 | 各256 |
| max_inflight_tiles | integer，固定1 | 1 |
| gdal_cache_mib | integer，固定64 MiB | 64 |
| max_faces / max_nodes | integer，faces 1–500000 / nodes 3–2000000 | 500000 / 2000000 |
| max_candidate_overlap_checks | integer，实际face/pixel clip候选次数，1–1000000000 | required |
| coverage_policy | diagnostic_only或minimum_valid_coverage | required |
| minimum_valid_coverage | finite number，[0,1]，valid area/native face area | diagnostic_only为null；strict policy required |
| area_abs_tolerance_m2 / elevation_abs_tolerance_m | finite number，m² / m，>0且≤1e-3 | 各1e-8 |
| area_rel_tolerance / elevation_rel_tolerance | finite number，无量纲，>0且≤1e-6 | 各1e-10 |

`coverage_policy_evidence_path/sha256` 在strict policy下必须与阈值齐备，diagnostic_only禁止阈值和政策证据。`source_vertical_unit_declaration` 可为m或null；`source_vertical_datum_declaration` 可为非空字符串或null。非null声明必须与对应 `source_vertical_unit_evidence_path/sha256` 或 `source_vertical_datum_evidence_path/sha256` 齐备，孤立证据拒绝。默认均为null。

证据path是相对 `config_base` 的非空路径；拒绝absolute、Windows drive/root及parent traversal，包括单反斜杠根路径；正常slash/backslash子路径均可。SHA是64位小写hex。实际证据必须为≤4MiB的普通非symlink文件，解析后仍在config目录内并匹配hash；缺件/不匹配在读窗口前报错。调用方负责政策、源单位/datum的真实来源与适用范围，文件hash不是内容审批。

`map_triangular_mesh_from_windows(reader, source, mesh, source_sha256=..., config=..., config_base=...)` 需要reader的read_metadata与iter_windows_selected接口。GeoTIFFDEMReader的selected迭代器只返回raw值、mask与window affine：先问metadata/offset predicate，再读选中窗口；不重投影、重采样、填NoData或换算高程。支持north-up、轴对齐像元；mesh和source必须为同一明确的米制投影CRS文本，未验证的WKT/EPSG alias拒绝。源垂直unit必须由metadata或带证据声明说明为metre；datum缺失仍null，不猜1985或施加offset。

每个候选三角形与像元做面积交叠，用确定顺序和补偿累加计算covered/valid/weighted-z；高程取valid面积上的weighted mean。NoData、nonfinite、其他masked、真实outside和ignored tiny overlap分别报告。`ignored_in_raster_area_m2`不算outside；忽略面积或纳入其valid weighted-z后的高程变化超过冻结界时失败。mesh fingerprint包含geometry tolerance/minimum face area等实际参数。零valid面明确失败，不补0、不丢面。

predicate在raw读前验证完整row-major grid的offset/尺寸/源界及末窗不足；提前EOF、重叠或漏交付selected窗口均拒绝。下一次读取前释放前一tile和scratch。source像元/mesh上限在对应工作前检查，clip上限在下一次clip前拒绝，错误可带face/pixel诊断。

source_sha256是调用方已校验manifest中的来源声明；本函数不重读全图重算hash。它拒绝source symlink，记录并比较元数据/映射前后的size/timestamps/device/file ID及窗口metadata，普通变更时不返回有效地形；stat保护不等于重新计算字节hash或完整来源链证明。

输出始终 `preparation_only=true`、`simulation_result=false`、`production_approved=false`。diagnostic_only仅给覆盖诊断；strict policy通过也只标coverage_policy_passed_preparation_only。`write_windowed_mapping_report` 以临时文件和no-clobber原子发布诊断，不覆盖已有目标。

256² raw payload、64MiB GDAL cache和面数cap不证明总进程内存硬界。真实D−S准备还需要受限worker/子进程联合硬限、质量与来源材料、CRS/vertical等价证据、prepared writer/reader身份链及明确派发。coverage政策pending可做诊断，不能称工程terrain ready；本接口不能据此启动水动力或宣称全量结果完成。
