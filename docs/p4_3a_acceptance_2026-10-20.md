# P4-3A 边界准备合同独立审查（2026-10-20）

批次：P4-3A-BOUNDARY-CONTRACT-20261018 / T-003 首次实质独立审查。
验证窗口：01a1596e-ce50-7360-b140-7e9b001383aa。北京时间2026-10-20。

结论：**未通过，需最小准备层返工；不能进入开放边界 RHS 实施。** 正式准备测试26通过，但独立反例确认安全区闭壁绑定、覆盖发布保护、布尔标记严格类型三类缺口。真实工程资料仍未补齐。没有改生产代码、执行数值推进、发布提交或改写原冻结参考。

## 身份与审查范围

接续前实际核对 `work/acceptance_boundary_contract_20261020/HANDOFF.md`、manifest及17个文件：全部SHA-256/字节数相同。HEAD为c57ca7387ece77fefa0d5c4580e9e0f2812da5ee。triangular_boundaries.py为实际工作区42497字节成果，未因其未提交而忽略。中文 `p4_3a_triangular_boundary_contract_adr.md` 是唯一权威ADR，英文只译文；冻结工作单不变。

只审查P2/P3实际几何与来源身份、边界请求准备、ADR/技术说明/示例/机器参考。保留T-001已通过结论，不重跑BAT、wheel或旧39/46/45项验收，不读真实DEM、社会经济源记录、SHP安全区原始工程数据或数据库，不运行288小时工程。合成安全区仅用仓库6个微型JSON/GeoJSON/YAML输入重新生成4面P2，面积D=8、S=2、D减S=6 m²。

证据目录：`work/acceptance_boundary_contract_20261020_sol_review/`。独立探针只留本地work目录，禁止当生产测试或提交内容；任务报告目录仅本地。

## 通过、失败、未验证

| 项目 | 结论 | 证据及限制 |
| --- | --- | --- |
| 真实外边/长度/外法向 | 通过 | 正式旋转90°、节点重编、面序和端点顺序测试；独立用坐标核对法向与边垂直、向相邻面外、范数误差≤1e-12。不是按任意节点顺序决定正负。 |
| 总组Q、方向、分配、请求积分 | 通过 | 独立Fraction参考：1/3 m边、Q=.01，权重1/4和3/4；入流-.0025/-.0075，单位宽-.0025；出流相反符号。入水.1/0/.2、累计.3，出水.15，净入.15 m³，绝对误差≤1e-12。未从输出反推容差。 |
| 时间原点/断点/端点 | 通过准备范围 | 显式null或时区时间；无时区拒绝；保留raw/effective时间；分段常值、最终样本不延长积分；错/缺/歧义断点拒绝。RK第二阶段旧区间是未来设计约定，本批未运行RK验证。 |
| 严格配置/来源单位与身份 | 通过已测范围 | 未知key、非法轴、bool/NaN/inf/负Q、错单位/基准、缺方向/分配/时间策略、缺组/未知组/内部组/重叠组、P2几何/P3数值漂移、法向内容篡改拒绝。硬链接别名实际复测通过。符号链接创建未另测。 |
| 准备/实际执行边界 | 部分通过、类型失败 | 生产模块没有调用flux/RHS/推进；旧closed入口及Manning/rain/combined准备入口继续拒绝开放绑定。值为true的boundary_applied篡改拒绝；整数0/1标记却被接收，见F3。 |
| 安全区活动域/新闭壁 | 失败 | D减S几何身份和重读源验证存在，未绑定控制通过；同一真实闭壁边3/7被绑定流量/水位，别名也能绑定，见F1。 |
| 输出保护 | 部分通过、覆盖失败 | 默认不覆盖的晚出现目标拒绝且字节保留；硬链接输入拒绝；显式overwrite在校验后替换无关文件，见F2。 |
| 既有V0/source账本分离 | 通过静态契约核查 | ADR保持固定初始预算，requested积分独立；本模块无V0/source ledger写入，无实际boundary_net。将来RK接受通量/回滚的数值行为未验证。 |
| 完整外态闭合与后续执行 | 未闭合/未验证 | ADR仍列“显式完整外态”与“经验证特征/关系闭合”候选，未选定一个具体闭合方法；全湿/亚临界建议、干态/回流/超临界拒绝表和两阶段CFL/回滚仅未来规则。不能据本审查授权P4-3B。 |
| 中文/英文与示例 | 主要语义一致；两处文档差异待修 | 独占复制文档示例的P2→P3→准备→读取全链4项补充检查中通过；中英均标准备、方向外向、未应用；中英技术表将format/schema_version写必填，但模型实际有默认值。英文ADR亚临界行多写fully closed，与中文完整外态闭合含义不同；返工同步纠正。 |

## F1 [P1] 安全区闭壁边可成为开放请求

位置：`hydrodynamics/triangular_boundaries.py` 的 `_validate_config_bindings`、`_build_result`；P3包构造同样未排除安全区已核定闭壁边。

原生P2实际带 `excluded_closed_wall` 证据，P3 v3完整复制该证据。准备却接受对 `security_exclusion_boundary` 的常数discharge inflow=.01 m³/s或绝对水位1 m的绑定。实际边ID为[3,7]；入水请求累计.1 m³，并从未绑定closed_wall列表移除。再在P2源JSON给相同物理边增加组名 `innocent_alias`，也得到同样结果。3个独立断言失败，夹带安全区来源漂移不是触发条件。

证据：`security_dischargeFalse.json`、`security_water_levelFalse.json`、`security_dischargeTrue.json`及independent_tmp相应完整P2/P3/准备文件。`boundary_applied=false`仍正确，**未发生实际淹没或数值流动**；缺陷是已批准安全约束在准备合同中失守，不能交给下一执行层。

最小修复：从已重验安全区证据/实际P2得到保护边集合，对所有开放绑定按物理边ID交集拒绝，不能只封禁组名；原普通外边、非安全区孔洞语义保留。加载/适配/产物重验全路径一致。新增直接流量/水位/别名及跨保护/普通边部分重叠反例，保留未绑定控制和D减S身份。必要P3共同入口防御由实施者限定实现，不能更改排除区物理或自动把请求重分配到别处。

## F2 [P1] 显式覆盖绕过“仅覆盖准备产物”限制

位置：`_load_capability`检查后，`_atomic_write_json`在 `if overwrite: os.replace(temporary, path)` 无条件替换。

确定性探针仅在临时JSON完整readback校验返回后、最终发布之前写入无关目标字节。overwrite=false控制正常拒绝并保留字节；overwrite=true返回成功并删除该无关目标字节。这个时点的目标不会再经过类型/身份保护；即使最初目标不存在，也会被配置中的true开启覆盖。不是概率性压力测试。

证据：`late_destination_False.json`、`late_destination_True.json`及对应独立断言。不要求本轮设计任意非协作写者下的原子CAS；但不能继续宣称当前发布保护能防止此已复现的覆盖。

最小修复：冻结可兑现的覆盖/所有权并发合同，保护不存在与已存在两个分支，确认本次被允许替换的确为既有准备产物并防止发布期间目标换成无关输入/结果。可评估复用本项目已有有界所有权机制，或由协调接受更严格的覆盖拒绝合同；不得仅再加一次exists/摘要检查便宣称原子CAS。正式测试加入晚出现目标、既有目标被替换、保护输入别名和失败清理；原探针/门槛不放宽。

## F3 [P2] 准备标记接受整数伪装布尔类型

位置：`TriangularBoundaryPreparationResult`中6个 `Literal[True/False]` 字段及读取时model_validate。

在有效准备JSON里把preparation_only改为整数1，或把simulation_result/solver_completed/production_approved/execution_available/boundary_applied分别改为整数0，6次严格读取均接受。Pydantic literal校验会规范成bool，即使模型strict=True，最终与重建产物相等比较也未发现原JSON类型错误。此问题没有把false语义变为true，但违反严格布尔字段和完整篡改拒绝合同。

证据：6个 `marker_*.json`，independent.log中6个失败断言。最小修复：解析前或字段before校验要求实际bool且值为冻结literal；生成schema/文档/正式测试保持相同固定值，拒绝整数、浮点、字符串及反向布尔，正确真布尔仍通过。

## 一手来源实际核查

本轮实际打开正文，不继承旧窗口未读转述。以下仅支持概念区分和来源自身行为，不证明本项目二维HLL算法。

- [USACE HEC-RAS External Boundary Conditions](https://www.hec.usace.army.mil/confluence/rasdocs/r2dum/latest/boundary-and-initial-conditions-for-2d-flow-areas/external-boundary-conditions)：当前页7.0；Flow/Stage节及同face重叠警告支持ADR转述的类型区分、其流量符号、stage可能双向及干单元无交换条件。项目按边长分配是自己的选择。
- [Deltares D-Flow FM Technical Reference Manual](https://content.oss.deltares.nl/delft3dfm2d3d/D-Flow_FM_Technical_Reference_Manual.pdf)：本次取到2027.01、revision81136、2026-10-17、DRAFT，PDF第88/89零基页（印刷78/79页），§6.4式6.151、6.156及内向法向说明支持积分Q和切向补充条件；§6.3.3支持按实际通量估步长的来源描述。不能将其符号或算法等同本项目方案。已打开对应PDF页面核对。
- [Antonopoulos & Dougalis arXiv:1507.08209](https://arxiv.org/html/1507.08209)：本次HTML为v2（2016-02-28，原稿2015），§1明确一维无量纲、特征速度及不变量，入射信息数随流态变化。ADR对此作用范围有明确限定；二维外态算法和本项目数值门槛仍无该来源的验证结论。

ADR可作为候选设计说明；冻结工作单要求“必须闭合未来实际边界状态/通量设计”，当前仍将完整闭合留给下一版本，故此项不算通过。协调下一步应先完成准备缺陷返工；准备通过后再决定并冻结具体闭合方法、状态输入版本和独立非静态数值参考，再派实际执行批次。已选用户B路线不重问。

## 命令、退出码与证据

均从项目根用 `.venv/Scripts/python.exe -m pytest` 执行，无Solver数值推进。完整命令见 `work/acceptance_boundary_contract_20261020_sol_review/commands.md`。

| 批次 | 结果 | 退出码 | 输出 |
| --- | --- | --- | --- |
| 生产正式 `tests/test_triangular_boundary_preparation.py` | 26 passed，0 skip | 0 | formal.log / formal.xml |
| 本地独立首轮29项 | 18 passed；10产品断言失败；1环境权限失败；0 skip | 1 | independent.log / independent.xml |
| 硬链接单项按权限流程获准复测 | 1 passed（关闭首轮环境不确定项） | 0 | hardlink_escalated.log / .xml |
| 新增文档实例与3种源项入口定向补充 | 4 passed，0 skip | 0 | supplemental.log / .xml |

独立去重结论：33个探针中23通过、10产品断言失败（F1三项/F2一项/F3六项），无未决环境失败。首轮环境错误保留，不伪装首次通过；已获准单项复测，不重跑未变整批。准备参考SHA始终为7ca081fcb9ec3cb1379db70426456fc6413e7110892c2b25353a80b444185c4a。

未跑：开放边界真实数值/非静态HLL、RK/CFL/反向/干态/质量回滚、真实工程、DEM/社会经济/数据库、全仓回归、wheel/隔离发布、符号链接创建及跨平台发布。这些均不计通过。生产未改，不为确定失败批次构建或发布。

## T-002 同次小型清单核对

只读取记录20及case_001两份小YAML：structured、EPSG:4548、50 m、288 h、带时区2023-08-01原点，目标1985国家高程；默认安全区S启用4490，旧入/出流CSV引用与重建强迫来源声明确在配置。未打开这些源文件。原D仍只有矩形框/NPZ引用，不是可信工程D；DEM源单位/datum/掩膜/覆盖、初值/糙率证据、雨量和物理边界来源/版本等没有因准备合同而补齐。安全区存在不能证明工程域或开放边界执行成立。

结论：记录20缺项清单与已读小配置相符；**T-002资料未补齐、未具执行条件**。没有把旧注释的未实现概括覆盖后来的closed实验能力，也没有把已通过合成/脚本结论扩展为工程通过。

## 交接与后续

建议由主协调给原实现窗口一次最小准备层R1工作单：F1/F2/F3及上述format/schema_version、英文fully closed文档一致性；仍不接RHS或新增物理，不修改冻结准备参考容差，不放宽既有closed validators，不触碰真实数据。完成后冻结生产文件，交本替代验收窗口原样复验与风险相称回归；未通过前不发布。重大设计项只有后续具体外态闭合/工程范围，当前三类准备缺陷修复无需重新询问用户已有路线。

本窗口只形成验收/推进与本地跟踪记录。完成后按已核对的人类授权向协调01a1585c-194b-7352-bb24-3ce48743879b发送一次结论，凭据另存；工具成功前不声明送达。

通知核实补记：2026-10-21 08:08（北京时间）实际读取本次单次发送返回，isError=false、目标threadId正确；已送达，receipt为证据目录notification_receipt.json。final_identity_check.json为通知补记前的记录哈希快照；其后只补本节与推进/本地跟踪的送达记录。10-20的测试/审查不倒记10-19，未重新测试或派新任务。
