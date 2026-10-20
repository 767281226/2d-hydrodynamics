# 安全区排除与原生输出独立验收（2026-10-20）

批次 SECURITY-EXCLUSION-20261019；原实施窗口gpt-6-luna/max，turn=01a156b1-7d4c-70e1-9de8-8de0c8c07457。合同见13/14，当前发布基线main=5f309d645c7d9bbb0132b5e86fa9c5bdf99f2f4a。采用D减安全区并集S，新边界为既有closed_wall；无新堤高、越流、径流去向或数值方程。原100001/map/DEM不改，合成运行最多64面、8保存时刻、1s。

## 首轮统一独立验收

实际65项=62 passed/3 failed/0 skipped in34.16s，正常exit1；75个项目模块全部来自实际workspace，运行前后生产Python文件SHA未变。38项安全区（14几何+23集成+1正向Manning）及27项SHP（原22+stage3+投影精度1+post-publish失败1），独立断言/输入参考门槛未放宽。冻结文件/摘要位于work/acceptance_security_area_20261019/frozen_independent_hashes.json和work/acceptance_native_shp_20261019/all_frozen_independent_hashes.json，原始解析参考JSON摘要b7bb12a783f711e114bc8706ebda5c21b9c6f5c344a0c732a314b804c0e4b370。

已验证：6种精确导入的D\S覆盖与实际闭壁外边；Gmsh真正生成内孔；多面/重叠只排除一次、环形安全区保留水域岛；非贴合/缺面积/中心点漏检单元拒绝；来源改变使P2/原生结果/SHP及none/Manning/rain入口拒绝；自重哈希的伪面积证据拒绝。none原生(time,face)与最大SHP的IDs/几何/面积一致，8面/active15m²/h0.2m/体积3m³；双室真实4面各V0.8/0.4m³、1s不交换，不被全域填洼平均；均匀雨1e-4m/s给active15m²接受0.0015m³，逐时体积3→3.0015m³，区内潜在雨0.0001m³单独标为未模拟；Manning正向原生reader仍绑定相同active面及3m³。启用安全区的structured明确拒绝，native/security结果不进入旧nc2web；旧总量/排除内点等未实现初值语义拒绝。大投影坐标小数原生面SHP精度及三个stage所有权/成功marker反例已关闭。

仍有两个真实阻断：GeoJSON内部声明EPSG:4326与policy.source_crs=EPSG:32650不一致仍未拒绝；发布后真实request漂移导致fresh validator失败，成功marker退役后export异常分支却隐式返回None而未抛错。原样反例已交回修复。第三个失败是测试runner的-I忽略UTF8环境，旧冻结Path.read_text使用GBK读取正确UTF8报告；同一probe显式-X utf8 -I实际1 passed in1.11s，exit0、0skip。无需修改生产编码或probe。统一最终gate将采用-X utf8 -I，尚未宣称65项全通过或14已发布。

## 数值与范围边界

函数AST比较显示既有推进类/通量/摩阻/源项算法保持不变，差异限结果构造和雨量报告/证据绑定。工作区快照与比较产物仅在work验收目录。此次通过属于小型合成的软件功能证据，不是实际洪水工程或安全认证；安全区表示EXCLUDED/非计算域，未实现人口、风险、损失指标不能解释为零风险。首批SHP仍只支持none/closed_wall native结果，source结果SHP不冒充支持。最终源码、正式回归、隔离依赖闭包及wheel门禁仍需完成。

## 实际安全矢量只读兼容

使用本批新读取模块对用户给定map/securityArea/dd_securityArea_shape.shp实际执行几何/CRS兼容核查，正常exit0。1个有效Polygon，EPSG:4490→4548面积2745116.5829153364m²，与13既有只读参考一致；SHP/SHX/PRJ/CPG字节前后不变。io.open及builtins.open显式拦截DBF和TIF/TIFF，未读取业务记录或DEM，未推进solver，也未写原矢量/100001。此证据仅说明矢量输入可读且投影兼容，不构成真实工程数值验收。

## 大域面积不能掩盖安全区内的小面

新增3个预检实际3 failed in1.39s，exit1、0skip：全为9个合成三角面、无DEM、无数值推进。D面积1e9m²、S为1m²内孔，8个面精确覆盖D\S，再加一个完整位于S内的合法原生小三角面，边长分别0.05/0.001/1e-5m，均大于geometry_tolerance_m且面积大于既有minimum_face_area_m2。check_mesh_against_security以总D面积乘1e-10得到0.1m²容差，三种正面积侵入均被接受，外侧正确的S壁边还会让boundary group检查通过。这违反无安全区计算面的核心合同，与大DEM/工程模拟无关。文件test_security_local_overlap_tolerance_independent.py SHA256=c80cdfe4b83b102809542da9e9283aed27e28e0a56ba7a5fbc77af93d62e919d。逐面支撑须按局部几何精度核对face属于D\S，不能以总域相对面积预算吞掉正面积侵入，也不能偷偷删面或提高minimum_face_area绕过。原65项不改；最终独立集合现为68项。

首轮修复后68项运行本身通过，但后续准备锁快照时发现triangular_shp_export.py、triangular_source_contract.py两文件摘要不同于运行前记录，且不是单纯EOF空行差异。因此撤回给实施窗口的“生产SHA未变”说明，不以该轮当作最终待提交源码冻结证明。未复制或提交该混合快照；实施窗口已idle，当前版本重新记录SHA并原样完整复验。独立断言不变。

## 停止修改后的确切版本复验

实施窗口idle后重新锁定全部生产Python，再原样运行68项；结果见root_acceptance_evidence.json。运行前后源码集合及逐文件SHA一致，随后以同一批已读取buffer保存functional_source_snapshot_v2，不再从可能漂移的live源码复制待提交候选。实现实际完成turn为01a156b1-7d4c-70e1-9de8-8de0c8c07457；其后01a15703-d4fc-7e82-87e1-717d6ef601f5只是简短确认，实施final中的旧12 turn引用不作为14实施身份。

## 首轮隔离最小依赖候选

基于main 5f309d6，仅采用functional_source_snapshot_v2及必要Source/Manning依赖、schema/docs/合成输入，排除DB接线/真实地图和DEM。实际367项=364 passed/3 failed/0skip in440.25s，exit1，38项目模块全来自candidate，无editable/live回退。

确定生产兼容回归：MeshPreparationResult旧第五位置为preparation_only，新增security_exclusion后prepare_mesh仍用第五个位置传security_evidence，未启用时preparation_only=None、启用时变成证据对象，security_exclusion属性反而None。实际旧test_small_import_example_completes_prepare_only_workflow失败，交原窗口仅修返回调用为明确keyword，保留True标记。

另一次workflow写report原子替换WinError5在单独新进程原样复验1 passed in1.23s、exit0，未改生产；两个wheel测试同进程连续构建时后一个触发setuptools目录缓存/目标viewer目录缺失，正在独立进程复验。候选验收未在这三项关闭前声明通过。最初验收脚本误写不存在的test_mesh_preparation.py，assert退出时未运行任何测试，后按实际统一test_triangular_mesh.py修正，未计作pass。

兼容补修实际完成turn=01a15718-ad16-7242-a516-69ba32dd5138（final中旧12 turn引用不作为本轮身份）。只改prepare_mesh返回调用的keyword，显式preparation_only=True/security_exclusion=security_evidence，并加一个同时覆盖两模式的正式测试。原导入测试+新模式测试实际2 passed/0skip in1.52s、exit0。生产与68项稳定快照相比仅mesh_preparation.py这一处返回调用改变，未更改推进公式或任何冻结probe。瞬时workflow单例1pass、R2A wheel新进程原样1pass in1.89s；最终候选将两个wheel测试各自放独立进程，避免全局目录缓存干扰，不删除或弱化其检查。

## 最终隔离候选门禁

在唯一返回keyword兼容修复及仅EOF空行规范化后，最终候选实际366 passed、2 deselected、0skip in231.76s，exit0；两个被选出的wheel构建测试分别在全新进程原样1 passed in1.74s和1 passed in1.87s，exit0。总368项均有实际通过证据，不能把两个deselected记为没测/跳过，也不谎称368单进程同次。38项目模块均来自candidate，editable/live回退禁用。覆盖本批68冻结独立、相关P2/P3/none/Manning/rain/SHP/structured及R2A29正式+12独立兼容。

私有候选移除DB工作流接线和配置/对应DB测试，case_001仅在已发布配置上增加安全约束，不混入当前未提交的数据库或社会经济数据路径；所有原工作区脏改动保留。候选代码只规范5个文件的末尾空行，冻结probe不改；独立index git diff --cached --check通过（另一个说明文件的多余EOF空行已去除）。最终wheel安装/实际CLI门禁随后执行。

## 最终wheel安装与CLI实跑

最终wheel SHA256=b8b99e5cd7ac708b83157899e60e83670fcb0cae18e7c94de4b4f5a40f3871aa。离线构建/独立安装及完整CLI smoke正常exit0：46个package文件与候选/安装包逐字节一致，32项目模块全部来自wheel。实际CLI执行P2/P3、none、source准备、rainfall、Manning、SHP export/validate/schema，再用三种原生strict reader重绑来源。8个真实active三角面、15m²、排除1m²，初体积3m³，1s雨后3.0015m³、单列未模拟雨0.0001m³；Manning静水3m³保持，native与SHP face_id集合完全一致，source字节不变。prepare_only状态显式True，can_publish仍false。

候选未包含真实map/DEM/100001、数据库接线/SQL、其他WIP、分析脚本或生成NC/SHP。原历史结果和用户未提交工作不动。功能68、最终368分组回归和wheel门禁全部关闭，允许白名单提交及正常推送main。只验证合成软件功能与真实矢量几何兼容，不构成工程洪水/现实安全认证。
