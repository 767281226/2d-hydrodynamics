# SHP 与三角水动力链路独立排查（2026-10-19）

批次：MESH-SHP-AUDIT-20261019。验收窗口：01a1537a-111b-7952-b853-61fd34d77ae4；实施窗口：01a13ecc-73db-7b40-ab59-622559d236f3。

## 结论及用户新决定

用户指定 work/results/100001/shp。已逐记录只读检查四份 SHP，全部为四个不同顶点加闭合点的四边形；不是原生三角计算网格。未修改历史结果，未读取真实 DEM、未重算288h案例、未连接数据库。

用户本窗口明确要求“后续所有输出的shp都需要是三角网格”，并选择：**SHP 必须对应实际参与水动力计算的原生三角面；旧 structured 计算保留，但不再输出矩形或展示拆分的 SHP。** 此决定覆盖 B 路线中原有 structured GIS 输出兼容要求，但保留 structured 计算及非SHP产物。不通过拆三角、改后缀或伪造原生面ID迁移历史结果。历史100001不是三角结果，不能只重导出获得可信三角水动力SHP；将来工程重算仍需开放边界与工程适用范围另行验收。

## 指定实物证据

| 文件 | SHP/DBF记录数 | 所有环点数 | 不同顶点数 |
| --- | ---: | ---: | ---: |
| detention_basin_grid_50m.shp | 59621 | 5 | 4 |
| detention_basin_grid_50m_epsg4490.shp | 59621 | 5 | 4 |
| inundation_max_50m.shp | 21884 | 5 | 4 |
| inundation_max_50m_epsg4490.shp | 21884 | 5 | 4 |

两种CRS都只是同一批矩形面及其坐标转换副本。完整遍历结果及SHA256位于临时验收证据 work/acceptance_mesh_chain_20261019/actual_shp_evidence.json，不作为提交数据。

run_report.json 的 workflow_file 指向 examples/case_001/workflow.yaml，case_config_snapshot.domain.type=structured、dx=dy=50m，model.frames=289、active_cells=22295、combined_cells=59621。prepared_grid 真实声明为 work/inputs/qingbei_50m.npz，其 provenance 来源是既有 structured NC，并非 P2 UGRID。当前配置 terrain.file 虽指向真实DEM，workflow实际消费准备NPZ；本次没有打开DEM或NPZ大数组，也不从报告推断它们已通过三角兼容。

## 实际调用链与分类

| 层/入口 | 实际代码证据 | 判定 |
| --- | --- | --- |
| run_qingbei_nc.bat、python -m hydrodynamics、hydrodynamics-run | __main__.py:3 转 workflow.main；workflow.py:266 定义 ProgressSolver(HourlyInundationSolver) | 默认仍是旧规则网格回放 |
| workflow输入 | workflow_inputs.py:30-84 读取NPZ，要求每字段(ny,nx)，x/y为等间距格心；case_001/config.yaml:16 structured | 矩形计算输入 |
| 默认计算 | hourly_inundation.py:305-310 保存ny/nx和dx*dy；364-384按总水量fill_volume；386起简化流速 | 规则格填洼/连通回放，不是三角SWE |
| 独立旧SWE | solver.py:14要求(ny,nx)，38起沿两个坐标方向构造RHS，69 SSPRK2 | 另一条规则网格路径，不是默认workflow |
| 默认结果 | workflow.py:277-292调用write_hourly_netcdf；_validate_outputs:118-132要求(time,y,x) | structured NC |
| 默认结果SHP | workflow.py:86-104调用inundation_export；inundation_export.py:82-87按xmin+i*dx/ymin+j*dy生成四角环，188湿格函数仍转调同一writer | 原因明确：输入/计算/导出整条仍矩形，非GIS软件显示错误 |
| 其他SHP公开入口 | vector_mask.py:279及355导出诊断格，325及后续仍四角环；__init__.py公开导出 | 也须按新决定阻断，不能漏过诊断入口 |
| nc2web | nc2web.py:117-144逐矩形两次triangles.append；147-153帧仍ny/nx，180-225仅接受x/y及(time,y,x) | 展示拆三角，不参与水动力；不能直接读原生三角NC |
| P0-P2网格准备 | mesh_preparation.py:563 prepare_mesh；mesh_generation.py:297-403建立外环/孔洞/嵌入约束/尺寸场，Gmsh generate(2)，只接收3节点一阶面 | 真正独立三角拓扑准备；本身不是模拟 |
| P3 GIS | triangular_overlay.py:720-806直接遍历face_nodes取三个节点，输出GeoJSON，含稳定face_id/拓扑摘要及面积 | 真三角准备层GIS；未绑定动态结果，不是水动力SHP |
| P4运行 | triangular_solver.py:98-126公共边法向；191-260以length/face_area守恒交换HLL及静水修正；375起SSPRK2；1009-1015真实积分并写原生结果 | 真正三角有限体积计算，不是显示拆分 |
| P4原生结果 | triangular_solver_netcdf.py:151-185保存node_xy/face_nodes/edge_faces/face_id/face_area，water_depth为(time,face)，reader重验真实输入身份 | 真实三角结果；尚无结果SHP导出入口 |
| Manning/降雨 | triangular_manning、triangular_source入口复用同一三角核心及独立源项reader | 已有各阶段合成验收，不能据此宣称工程及开放边界通过 |
| 开放边界 | triangular_solver_contract.py:211只允许closed_wall，689-691拒绝P3开放binding；P4-3A ADR:4/13明确未接RHS | 准备合同，不是可用开放边界求解 |

## 独立运行证据及限制

只运行6格/2面及不等面积两三角的小型合成。临时独立文件 work/acceptance_mesh_chain_20261019/test_chain_independent.py 四项检查：默认workflow实物四边形、GIS转换仍四角、三角原生(time,face)且水跨对角线交换、面积3/1 m²非矩形三角守恒；并确认旧nc2web拒绝原生三角结果。默认workflow探针只模拟0.2s，三角动态探针0.01s。

正式+独立命令：

~~~text
.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp=.venv/acceptance-chain-20261019-isolated work/acceptance_mesh_chain_20261019/test_chain_independent.py tests/test_inundation_export.py tests/test_nc2web.py tests/test_triangular_overlay.py
30 passed in 5.71s; exit 0; no skips
~~~

首次受限进程出现Windows 0xc0000008异常退出，未计通过；同一合成集合按工具授权正常复验。该30项证明审计事实和既有行为，不代表用户新SHP合同已实现。P2真实Gmsh生成沿用既有独立CLI验收证据（三角网格验收记录），此次代码核对与合成reader/overlay验证不重复冒充Gmsh新生成验收。

## 修复范围

见12工作单：先停止所有公开旧矩形SHP写出、调整workflow成功/发现/校验合同，再增加独立原生三角结果SHP及验证器，明确身份、时间选择、属性和单位。不得将真实DEM的栅格存储改成三角、不得替换structured方程、不得新增开放边界、不得覆盖历史100001。R2A保持10工作单，单独完成单独验收，不将本审计算进R2A交付。

## 新合同的修复前反例

另已冻结5项用户新合同反例：四个旧公开writer必须拒绝且不创建目录，旧structured工作流仍计算但不产生SHP并能被最新完成发现器找到。当前原实现实际5 failed，pytest正常退出1、无跳过（2.00s），是待修缺口，未计入30项行为核查通过。文件 work/acceptance_mesh_chain_20261019/test_native_shp_policy_independent.py，SHA256为 `3e52589afe2e3b006b231b619c44127357889ccc29f2c6c30d374dd6672378b6`。实施时不改该原样反例，修复后独立复验。

## 本轮真实Gmsh生成补证

随后在独立CLI复制运行仓库小型合成 gmsh_hole_refinement 示例，正常退出0：96节点/156三角面/252边，总面积27.5m²，外边28/孔洞边8/内部约束边4。无真实DEM、无Solver运行。独立进程重新读取实际UGRID，并用Shapely构造外环减孔洞的解析几何对比：并集面积27.500000000000004m²、对称差面积1.2559397966072083e-15m²，孔洞交叠面积为0，约束线长度/几何误差小于1e-9m，全部面为3节点闭合环。该补证替代前文“仅沿用旧Gmsh生成记录”的限制；产物及临时检查脚本仅留work验收目录，不提交。

## 指定NC实物元数据补证

只读打开100001的既有NC元数据（未取水动力大数组），实际dimensions为time=289、y=719、x=507；water_depth.dimensions=(time,y,x)、shape=(289,719,507)，不存在face_nodes/node_x，coordinate_system=EPSG:4548。正常退出0。进一步排除了“结果本来三角但SHP写错”的可能。
