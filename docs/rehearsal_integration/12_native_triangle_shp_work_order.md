# 原生三角结果SHP与旧矩形输出阻断工作单

批次：NATIVE-TRIANGLE-SHP-20261019。前置：本窗口11审计与用户明确选择。由原实施窗口 gpt-6-luna / max 实现；验收窗口只维护标准/独立测试，不并改生产文件。当前R2A不得重复派工或把旧R1复验当交付；先完成并报告R2A，验收窗口随后确认本批开始。

## 用户冻结合同

后续所有新输出SHP必须直接对应实际参与水动力计算的原生三角面。旧structured计算保留，停止矩形/展示拆分SHP；不迁移或覆盖历史100001。导入/只读外部矩形SHP继续允许。准备层只有拓扑、没有模拟结果时不得标成水动力SHP。现有P3准备GeoJSON继续按准备产物标记。

## 必须实现

1. 盘点并阻断旧矩形SHP公共接口：inundation_export.write_grid_shapefile/write_wet_grid_shapefile及vector_mask.write_active_grid_shapefile/write_active_grid_shapefile_reprojected。可保留名字作为清楚的拒绝/迁移提示，但拒绝须在mkdir/open前发生，不留下sidecar、不删除既有文件。不得给它们新增“允许矩形”或“拆分展示”的旁路。保留mask构造/读取等能力。
2. 旧workflow继续执行原structured计算及已有非SHP结果，shp阶段显式NOT_APPLICABLE或语义等价状态，报告原因native_triangular_result_required，不能标COMPLETE或伪造空三角成功。_validate_outputs及find_latest_completed_bundle不得再强制4个矩形SHP；兼容发现历史完整包须明确历史结构，不把它验证为新三角交付。新run元数据明确engineMode/grid topology，相关CLI帮助、批处理提示、schema、文档、示例、测试同步。
3. 新独立只读三角SHP导出模块/最小CLI：输入真实run config、P2/P3源图及原生P4结果，通过现有严格reader绑定真实结果，不接受仅mesh/preparation、自报JSON、任意同形数组或structured NC。首批仅sources=none已验收小型封闭域，未支持源项类型清楚拒绝；不得降低其他原生reader校验。模块不调用solver。
4. 原生每面一feature，三个实际不同节点加闭合点；稳定face_id与原生face_id一一对应，不重新编号作身份，不作矩形重采样/拆分/合并。支持FRAME与MAX_OVER_WINDOW两种受控选择器；索引/秒轴/窗口包含端点/并列最早时刻规则清楚且严格。FRAME含深度(m)、水位(m且datum)、ux/uy(m/s)；MAX含每面最大深度(m)及最早出现时刻(s)。字段受SHP DBF限制时以正式映射表和元数据保留语义，ID不得截断或精度丢失。无计算的风险/损失/到达字段不制造。
5. 至少提供全计算域与最大淹没面选择（原生h>明确阈值，阈值m且有限非负、默认/范围文档化）；干域输出有效空层，明确不是缺数据。先提供原计算投影CRS及可选EPSG:4490副本，PRJ正确。坐标转换只在GIS导出层，不改源节点/地形/面积。投影几何面积与原face_area匹配；另给area_m2属性；EPSG:4490不得将经纬度面积当m²。
6. 配套版本化metadata/manifest：input/result/config摘要、case/mesh/topology/terrain身份、原生face集合、selector及时间轴、字段单位/垂直基准、原生计算scope、CRS、输出文件SHA/字节数、experimental=true/engineering_validated=false。无原生run/script绑定时准确保留其证据局限，不伪造身份；不要扩张平台租户/DB方案。独立validator重读原生和SHP/DBF/PRJ，校验三角几何、ID唯一/集合/属性/时间/面积/摘要，不能信任自报通过。
7. 新目录不覆盖输入/结果/源图/历史文件，拒绝路径别名或覆盖；写入失败无看似成功manifest，支持有效空层；保护SHP/SHX/DBF/PRJ/CPG完整性。无需引入新数据库、HTTP、Java/Vue、GUI、新数值或新物理。
8. schema、安装入口/包资源、双语使用说明、小型自包含例子及正式定向测试同步，wheel可用。新例子最多64面、8帧、1s；独立面积积分/字段可从真实数组核对，不能只数三角顶点。复测现有structured非SHP链路与R1/R2A受影响合同。不要运行有实际DB连接或真实大数据的测试；按明确节点选择测试，跳过不当通过。

## 必须覆盖反例

所有旧writer拒绝且无文件；structured新流程无SHP但正常生成NC；原生非矩形、不等面积面及大face_id正确；旧structured NC/准备UGRID/异案/错config/改真实数组/改face_nodes或ID/错CRS/错units/错frame/window/NaN/篡改DBF或SHP/缺sidecar/已有目标/越界路径拒绝；全干有效空层；EPSG:4490保留逐面对应且面积属性仍m²；独立reader可打开且三角只含3不同顶点。

## 归属与完成

只修改本批必要生产代码、测试/示例/schema及实现说明。主窗口维护11/12与验收记录，实施窗口不改；既有独立审计探针不能改写成通过新行为，可新增正式新合同测试。不要修改数值公式、开放边界ADR或历史结果。不要暂存/提交/推送，验收通过后由验收窗口选择已核验依赖闭包提交。完成final保留真实batch/turn、文件、实际命令及正常退出码/通过失败跳过数和限制；不重试被拒的跨聊天发送、不新建聊天或定时任务，由本窗口wait_threads读取。

## 同日安全区新增要求

用户已新增map/securityArea完全不淹没业务约束，实物核查及冻结合同见13/14文档。12与14作为R2A之后有序子批由同一实施窗口负责，不并改或混入R2A；12单批通过不能冒称安全区整体已完成。本轮最终须覆盖14的计算/源项预算/输出统计一致性及明确支持或拒绝矩阵。

## 实际开工确认

R2A FIX2工作区41项独立正常通过、源码快照锁定，发布候选/安装门禁在验收窗口独立继续。现正式确认12开工：turn `01a15401-0eaa-7823-a190-dc4e9659a2c2`，cursor `3c8424f9-ce4e-4e24-9e54-4a4591a3a1f1:174`，inProgress；模型gpt-6-luna/max。12不改R2A包模块/profile及独立反例；14待本批独立验收后启动。
