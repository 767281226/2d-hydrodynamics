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

## M2-B0 受限 worker 与 prepared diagnostic

新的 `hydrodynamics.restricted_worker` 路线使用独立配置格式 `triangle-terrain-preparation-worker-config-v1`。配置 schema 和占位示例见 [schema](../examples/triangular_mesh/restricted_worker_config.schema.json) 与 [example](../examples/triangular_mesh/restricted_worker_config.example.json)。要生成带当前小型合成输入 hash/stat 的可运行配置，可运行 `python examples/triangular_mesh/create_restricted_worker_smallcase.py work/b0_synthetic_case --project-root .`；这个生成器只建立 256×256 合成 GeoTIFF、两面网格及配置，不会启动 worker。运行前应检查配置，然后使用 `python -m hydrodynamics.restricted_worker --run-config work/b0_synthetic_case/worker_config.json --project-root .`。

配置必须给出下列资源值，没有默认预算：

| 字段 | 类型、单位与范围 | 当前 B0 候选值及来源 |
| --- | --- | --- |
| `max_workers` | JSON integer，固定为 1 | 固定 1，不支持并发扩张 |
| `job_memory_limit_mib` | strict positive integer，64–65536 MiB | 2048 MiB；固定运行时冷启动peak 694898688 bytes，256²/2面小例peak 709095424 bytes，100000面索引/映射/写出两次peak为1072828416和1072496640 bytes。上限为最大代表峰值的2.002倍 |
| `wall_clock_limit_s` | finite number，0.1–86400 s | 900 s；约为100000面、256²栅格最慢实测25.219 s的35.7倍，覆盖该批启动和读回余量 |
| `output_budget_bytes` | strict positive integer，至少131072 bytes | 1073741824 bytes；100000面JSONL实测29188890 bytes；按500000面线性外推约146 MB，预算仍有7倍以上空间 |
| `scratch_budget_bytes` | strict positive integer，至少65536 bytes | 16777216 bytes；256²小例与100000面代表性worker最大scratch峰值为4240 bytes；scratch用于配置副本及重定向TEMP/PROJ目录 |
| `child_process_policy` | 固定字符串 | `job_object_inheritance_without_breakaway` |

supervisor 创建Job Object并设置每进程和整个作业的 committed-memory 上限及 `KILL_ON_JOB_CLOSE`，然后在 `CreateProcessW` 的 `PROC_THREAD_ATTRIBUTE_JOB_LIST` 中直接附上Job，同时使用 `CREATE_SUSPENDED`。创建API返回后、恢复worker主线程前，再用 `IsProcessInJob` 针对该Job句柄核验成员关系；路径中没有“CreateProcess之后再Assign”的出生空窗。Job Object不允许breakaway；worker启动的Python/GDAL/GEOS/PROJ进程及所有后代都在同一作业内，supervisor等到作业活动进程数为零才允许发布。Windows Python启动器的helper进程也列入Job并等待退出。独立自测在create返回/主线程仍挂起时持有worker进程handle并终止自有supervisor，handle最终变为signaled；已resume后的父死/child树清理仍保留PASS。没有Job Object或当前环境不支持`PROC_THREAD_ATTRIBUTE_JOB_LIST`时拒绝启动，不退化到RSS观察。

完整回执见 [resource profile](</D:/devTools/project/2d-hydrodynamics/work/full_dem_m2_b0_20261027/resource_profile_r6_final/resource_profile_evidence.json>)。剖析运行时是 Windows 11、Python 3.12.14、Rasterio 1.5.2/GDAL 3.12.2、NumPy 2.5.4、Shapely 2.2.0、pyproj 3.8.0/PROJ 9.8.1和Pydantic 2.14.1。100000面代表网格含50451节点、网格JSON 10104787 bytes；两次受限worker均成功，时间23.687/25.219 s，worker输出peak 29199112 bytes，最终attempt总输出29210758/29210759 bytes。没有profile真实D−S网格或DEM像元。

Job Object 限制的是进程树 committed virtual memory，不是整台机器的物理 RAM 用量。内存映射页和系统文件缓存的物理驻留、内核内存、其他进程及不在作业里的 supervisor 内存不在这个上限内。receipt 同时保留 `PeakJobMemoryUsed`、限制值、全部观察到的作业进程 PID、进程树退出计数和输出/scratch峰值；这些峰值用于核对，不替代 Job Object 的强制限制。输出 JSONL 每行在写入前计入 `output_budget_bytes`；重定向到 attempt 私有目录的临时文件在worker运行期间持续计量，超额即终止。磁盘预算由路线的写前检查和目录监控执行，不声称是 NTFS 文件系统配额。

成功件使用 `prepared_faces.jsonl`、`prepared_body.json`、`manifest.json`、`status.json` 和 `READY`。`READY`只在worker完成、作业树退出、文件hash/readback和身份绑定检查通过后生成；最终目录以原子目录重命名作为发布commit点，不写`latest`，不覆盖已有目录。默认`read_prepared_terrain`要求目录名与`run_id`相同；私有`.pending` attempt即使在commit前已有`READY`，默认reader仍拒绝。supervisor退出时recover会清掉pending标记、留下`supervisor_lost`诊断。cancel/time检查覆盖prepared文件hash和逐face readback，并在directory commit前再次检查；提交前取消/超时无final目录/READY，提交之后的事件不回滚已发布结果。失败状态区分 `resource_rejected`、`path_rejected`、`timed_out`、`cancelled`、`failed_read`、`failed_write`、`source_identity_rejected`、`worker_failed` 和 `supervisor_lost`。所有成功prepared件都为`preparation_only=true`、`simulation_result=false`、`production_approved=false`、`engineering_ready=false`；不含水深/动量或stepjson模拟帧。`read_prepared_terrain`只读已保存的manifest/body/逐面行并核对身份，不打开DEM或重跑映射。M1 `open_validated_native_result_frames` 不接受该manifest。

带CRS alias的selected-window wrapper在调用下一底层read之前释放旧raw `DEMDataset`、alias副本和tile tuple。加入实际8×8 raster、2×2分块、16窗weakref回归，alias开/关时每次读取的新旧raw tile同时存活峰值均为1；没有用全图单窗或假设tile计数替代。

prepared JSONL writer和reader共同执行逐面跨字段校验：唯一face ID；行数/mesh `face_count`/mesh face-ID序列/manifest bindings一致；coverage ratio 等于 valid/native area；valid、NoData、nonfinite、other mask分区等于covered area；covered+ignored+true outside等于native face area；ignored-valid不超过ignored overlap；面级汇总与body aggregate一致。reader还核对完整Job退出/exit code/termination/wall/limit receipt、worker result receipt、文件引用和hash。单位来源严格区分raw band metadata与已验声明/evidence：raw unit为null时不会贴`source_metadata_metre`；source datum仍按实际metadata/attestation标注，历史−1.56关系始终`applied=false`。

新worker schema将当前M2-A windowed config schema的完整字段依赖约束作为`$defs.windowedMappingConfig`嵌入；测试对比两者定义一致。资源/stat整数按JSON Schema整数语义接受whole `1.0`并转成int，bool、分数和范围错误仍拒绝。Owned临时文件名使用短随机名称；worker启动前检查final/staging/临时/诊断路径UTF-16长度，<260可运行，越界返回`path_rejected`且不创建run。实测final receipt路径224字符的小例通过；source manifest SHA不一致时worker真实退出54，即使主worker-result写入失败也在scratch保留有界fallback，supervisor报告原类别/exit和写失败原因，不降成53。

## 来源材料、alias 与准入边界

`worker_started` 表示真实 `ResumeThread` 成功后已记录的 ever-started 事实；它不等于“存在 receipt”或“存在日志”。该内存状态先于可失败的日志/hook更新，失败时返回对象与status使用同一值；prelaunch和仅created-suspended仍为false。v2 resumed journal验证成功标志、prior suspend count=1及created PID绑定，用于持久诊断/恢复；有效形状的文件也不能把当前未resume的内存状态提升为true。日志写失败不能抹去已发生的启动事实。

B0沿用既有 `source_manifest_and_budgets.json` 的 DEM SHA-256 `bb5d2bd8…d63a38c7` 及既有全图统计，不重新扫描约12.8亿字节的 DEM。当前 DEM size 与既有哈希记录的 size 一致，mtime 也一致；本批保存当前 file identity/stat 并在 metadata/tile读期间比较。SHA-256是此前完整扫描继承的声明；stat检查不等于本批重新证明每个DEM字节。D/S的 `.shp/.shx/.prj/.cpg` 小型组件SHA、size与mtime都匹配既有来源清单。DEM `.prj`/`.tfw`不在旧清单中；本批读取小文件并将当时SHA/stat绑定到新候选配置，不声称存在历史摘要。分析生成的 `.aux.xml` 不作为来源组件，GDAL PAM关闭且只使用 GeoTIFF 内嵌 georeference。本批只核组件身份/元数据，不重读D/S坐标或重算D−S几何。

DEM 内嵌 WKT SHA-256为 `1535229a…940840f1`。其 CRS 被 PROJ 解析为外层 BoundCRS，外层 `to_epsg()` 为 null且与裸 `EPSG:4548` 的通用 `CRS.equals` 为 false；内层仍带 `EPSG:4548` authority。运行时证据逐项核对Easting/east与Northing/north轴集合、米单位/因子、Transverse Mercator EPSG:9807的五个参数、China 2000 datum、GRS80/CGCS2000椭球数值，以及外层EPSG:9606 bound transform的七个零参数。只有精确 WKT hash和这些条件全部通过时，准备层才使用显式 GIS XY(Easting,Northing) alias；原WKT保留、坐标不重排、不转换，Reader本身不强贴EPSG码。

GeoTIFF band vertical unit读取为 metre，source vertical datum仍为null。既有材料记载仅限下游东淀-文安洼区域的 `target = source - 1.56 m` 关系；prepared provenance绑定该证据文档SHA、范围、方向并标 `applied=false`、`proves_dem_source_datum=false`。目标1985高程基准不覆盖DEM来源datum，DEM高程不加减1.56m。来源材料/CRS等价证据只解除raw source诊断限制，不冻结工程coverage policy；coverage pending 仍不得作为terrain engineering ready。

本B0结果不生成真实D−S Gmsh网格、不读取真实DEM像元、不开始B1或solver。进入B1仍要求独立审查通过并明确派单，当前D几何路径重新可读且组件hash/stat匹配，mesh输入/代码/source/config/coverage证据绑定完整；覆盖政策未定时只允许diagnostic。Nominal50m、minimum1m、500000面/2000000节点、真实安全区S和真实来源DEM承接原决定，不在B0改动。
