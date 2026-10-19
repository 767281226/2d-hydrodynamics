# R1：新建方案合同、公开历史模板与运行预检实施单

批次：`REHEARSAL-R1-CONTRACT-CATALOG-20261019`。

派工确认：原实施聊天 `01a13ecc-73db-7b40-ab59-622559d236f3` 已接收；实际turn `01a152f0-2892-73c3-8121-8167bb75e62b`，确认时inProgress，cursor `3c8424f9-ce4e-4e24-9e54-4a4591a3a1f1:4`。模型显式 `gpt-6-luna / max`。这只是开始实施，不代表功能或测试验收通过。

## 1. 最新授权与职责

用户最新要求：补充总体显示要素、新建输入、下边界暂不要求用户考虑、内置天津2023.7.31洪水相关调度并拓展其他模板；先补方案，再指挥实现；主聊天只负责居中指挥。

这覆盖上一轮“只做R0审查”的限制，**本工作单允许原实施窗口进行下述R1生产合同/模板代码实现**。不一次性授权所有后续阶段，不启动真实工程计算、不实现新边界/闸门物理、不运行数据库迁移、不改业务仓库或部署服务。主聊天维护方案、派工和独立验收，原实施窗口写指定范围的代码/示例/测试，双方不并改生产文件。

阅读顺序：AGENTS → 01总体方案 → 02合同 → 04字段目录 → 05管理/新建补充 → 06来源方案 → 本单。本单与05/06是最新裁决，覆盖旧文档中仅R0、普通用户必填下边界等旧表述。旧03保留原审查与验收背景。

## 2. R0审查闭环与明确裁决

原实施审查turn `01a15293-9039-7350-956b-de58a515ed1e` 已完成。以下是主聊天对其重要缺口的处理，不把R0报告当作数值验收。

### 身份映射

| 字段 | 冻结规则 |
| --- | --- |
| tenantId/projectId | 可信服务端执行上下文，不由普通前端自由认领；模板是公共只读，克隆后属于租户 |
| scenarioId/versionId | 业务场景身份，不能替换模型case_id |
| platformRunId | 平台计算请求，只放平台信封，不写进旧G1的run_id |
| attemptId/attemptNo | 平台技术重试身份，输入快照不变；旧attempt不可回写新attempt |
| scriptId | 后端在派发前唯一分配并固定，worker只能回传；旧单机CLI仍可显式输入/自动生成 |
| localRunId | 对现有structured workflow等于scriptId；现有G1的run_id也保持该含义 |
| modelCaseId | 保留原模型案例身份和输入绑定，不用scenarioId随意改写 |
| gateAuditBatchId | 原G1导入批次，R1不创建；R2再按租户与身份关联 |

代码版本/依赖环境、输入摘要、执行来源单独保存。`executionOrigin=PLATFORM_MODEL` 为信封语义，不能映射成现有v2的 `data_origin=user_import` 冒充用户导入。R1不改变G1 v2枚举；R2通过明确版本迁移增加平台模型来源并保持旧格式可读。

### G1租户与发布（冻结后续原则，R1不执行SQL）

平台新审计不得跨租户共享batch身份；建议v3新批次键包含版本命名空间、tenant/project、platformRun/attempt/script、modelCase和原导出一致性摘要。旧v2批次键、JSON和摘要不重写，tenant未知保持隔离，不能根据case_id猜归属。后续迁移须为批次和明细建立可强制校验的租户关联、唯一约束和授权查询，不能只增加attempt表唯一键就宣称隔离完成。

调闸落库和业务发布默认是可恢复两步：AUDIT_INGESTED回执验证后，业务事务原子发布可见性。即使同库，当前Python导入自己提交，也不是Java同一个事务；不承诺跨库原子。平台模式唯一写入方为发布服务/其受控导入适配器，worker不同时启用workflow自身audit_database。

### 产物路径与可移植身份

R1执行信封须区分内部实际路径、source_result_file溯源文本、artifactId及内容摘要，不向前端暴露磁盘路径。现有 `read_validated_exports` 的实际路径一致性检查保持不变，禁止为远端导入静默跳过。

R2选择**版本化可移植审计包适配器**：保留原CSV/JSON字节及一致性摘要，外层manifest绑定native artifactId/hash、原source_result_file与审核输入，不重写原JSON改路径。新适配器应独立验证实际文件、内容摘要、来源绑定和租户，不将“文件哈希形状正确”视为通过。新适配器交付前，只允许旧API使用原稳定受控路径；搬移后路径失配应明确失败，不宣称已支持对象存储回读。

### 产品选择器

统一字段：resultSetId、productCode、scopeId、scopeHash、aggregation、frameIndex/timeS或timeWindow、metricPolicyVersion。scope绑定不可变区域几何/版本及并集去重policy；普通产品不能用空scopeHash假装全域覆盖。

aggregation首批白名单：FRAME、SERIES、MAX_OVER_WINDOW、PEAK_AREA_OVER_WINDOW、UNION_FOOTPRINT、FIRST_ARRIVAL、WET_DURATION、EVENT_TIMELINE。FRAME必须frameIndex和timeS匹配真实manifest，timeWindow=null；窗口产品frameIndex/timeS=null，timeWindow必须在原范围内且产品允许。不能泛用ENTIRE_WINDOW代替不同聚合算法。

G1非空间审计使用 `scopeId=AUDIT_RUN_GLOBAL`、`aggregation=EVENT_TIMELINE`、scopeHash=null，绑定attempt/script和事件范围；只有明确地理映射的事件才支持分区筛选。覆盖状态按选择器/产品记录。原始变量、适配完成、质量通过和可发布四层分别标识；平台SUCCEEDED不等于engineeringValidated=true。

## 3. 本批实际实现范围

在 `D:/devTools/project/2d-hydrodynamics` 实现独立的新建/模板/预检合同，不在该仓库新建Java/Vue项目。

1. **严格输入合同**：场景草稿/冻结请求、四类上边界来源、逐断面序列引用、工程规则引用、初始/降雨profile、未知下边界policy、执行信封和产品选择器。沿现有Pydantic/结构化API风格；未知字段、非有限数值、bool充数、非法单位等明确拒绝。
2. **草稿与可计算性分开**：提供纯验证/预检接口，能保存有效业务草稿但返回完整计算阻断项；不实际调用solver、不联网取数据、不写数据库。明确 `canSave`、`canCompute`、`canPublish` 不能相互代替。

   引擎能力/profile来自受信registry，不接受前端自报supported=true。仅字段完整不等于输入文件/版本/摘要已核验；没有可信resolved-input证据时executionReadiness=NOT_ASSESSED、canCompute=false。正例只能使用独立验证过的小型合成fixture或受信解析器上下文，不能靠用户输入一个布尔值放行。
3. **输出时刻规范化**：小时/分钟仅在适配层换秒，120h/60min为121时刻，非整除含精确末端，严格检查浮点重复/终点/最小间隔与数量上界。输出间隔不作为solver dt。源项断点冲突提供诊断或显式待确认计划，不静默改输入。
4. **下边界policy**：默认DEFERRED_UNKNOWN、userInputRequired=false、applied=false；不生成3.2/3.48/3.85m、0水位、0流量或closed_wall。保存不阻断；计算是否阻断依据明确引擎能力和profile。只有合成封闭域的显式标识可对应既有closed_wall，真实开放域不可偷用。
5. **内置catalog**：按06实现只读、可校验、可列出/查看/克隆的来源/事件/模板资产；首批仅S1-S4已核正文的内容。候选访问失败/未核材料保留review_pending且不启用。所有历史模板runnable=false；设计/预报/人工空模板需数据补齐，禁止自带伪洪水曲线。
6. **原型字段适配**：映射name/region/start/duration/outputStep/sourceMode等到新合同；忽略/拒绝来源不明的旧硬编码lower-boundary默认值，不能默默保留。openingHeight(m)和openingRatio不能互填。预报ID缺失不回退默认曲线；未实现initial/rain字段明确缺项。
7. **交付接口**：Python公共函数及最小CLI用于schema导出、模板list/show/clone、草稿validate/preflight；不是新HTTP服务，不建GUI。文件输出遵循no-clobber并保留原输入/来源。
8. **同步材料**：正式JSON Schema、合成完整/缺项/非法示例、模板数据、单元/反例测试和短使用说明。业务前后端合同示例使用现有空格日期+timeZone；内部模型ISO时间独立适配。

建议新增文件范围：`hydrodynamics/rehearsal_contract.py`、`hydrodynamics/rehearsal_catalog.py`（或等价清晰模块），`examples/rehearsal/`、对应 `tests/test_rehearsal_*.py`，必要的最小包导出/CLI注册。不要修改主聊天维护的 `docs/rehearsal_integration/`；实现说明可放 `docs/rehearsal_contract_implementation.md`。避免无关重构与依赖升级。

## 4. 冻结验收

纯合同测试不需要DB/真实DEM/工程运行，不应出现环境跳过。至少验证：

- 两个断面分别FORECAST/HISTORICAL，ID/时序缺项互不串；forecast未选方案、设计仅给重现期、历史仅给新闻单点不能canCompute。
- 合法草稿可保存、字段非法不可保存；未知lower-boundary可保存但需要其数据的引擎计算阻断；显式synthetic closed只对合成profile生效。
- 120/60、非整除尾步、小间隔、点数上界、雨量断点、NaN/Inf/bool/string非法数值；不得把显示分钟当秒或求解dt。
- 120h模板不篡改8月后历史事件时刻；历史日期不被今天覆盖；日期级/未知“日前”事件不能生成精确operation时间。
- 2023东淀启用为8月1日2时的决策生效；邵七堤9.32m大沽/66.6m3s为观测；不把永定河观测放到东淀；不添加文安洼已启用事实。
- 报道发布时间与事件时间分开；sourceTimezoneExplicit=false与解释policy保留；源码静态峰值和曲线不进入真实模板。
- 首批来源引用完整、URL/标题/发布日期及类型正确；待核来源不可升级到启用；克隆不改只读catalog；文件存在拒绝覆盖。
- tenant/run/attempt/script/localRun/modelCase字段级映射；worker改script ID拒绝；R1没有把PLATFORM_MODEL错误转为user_import。
- FRAME与timeS失配、scopeHash错、聚合不支持、窗口越界、G1全局选择器冒充空间统计、原始变量存在但未适配发布等独立反例。

冻结参考不倒推、不放宽。报告实际pytest数量、正常退出码、失败/跳过和已知限制；只运行小型合成数据，不连接实际闸控、业务数据库、Nacos/Redis，不提交推送。

## 5. 后续分批与业务交接

R1通过后主聊天独立验收，再派R2结果包/可移植审计/租户发布适配；业务R1可在自己的窗口按05和合同建设场景CRUD、模板克隆、列表过滤/收藏和创建预检，复用已有Vue页面与RFCADD API/Biz。业务SQL/API实施前必须采用本批实际schema及主数据映射，不以mock字段或截图值为依据。

真实计算参数缺失不妨碍业务保存、模板查看和元数据开发。开放边界RHS、经济损失、自动撤离或水质/潮汐新物理仍按既有专项验收；不得以用户要求“拓展调度”跳过物理边界审查。

完成后在原实施窗口final提供真实turn/batch、文件、命令和证据，并按已核实的用户主动汇报授权通知主聊天一次；不得覆盖主聊天model/thinking。工具若拒绝通知则停止、不绕过，在final明确。继续采用 `gpt-6-luna / max`；不新建聊天、不恢复15分钟任务。
