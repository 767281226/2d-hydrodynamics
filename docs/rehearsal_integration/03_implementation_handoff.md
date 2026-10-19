# 分工、遗漏场景与可转交实施工作单

批次：`REHEARSAL-INTEGRATION-DESIGN-20261019`。
状态：本单保留首轮R0审查和总体分工。用户最新已要求补充后指挥实现，**当前获准的R1代码范围见[07实施单](07_r1_authorized_work_order.md)**；不要继续把既有批准当作缺失，也不要把R1扩展为未经分批审查的全仓/数据库改造。

交接记录：R0审查turn `01a15293-9039-7350-956b-de58a515ed1e` 已completed，主聊天已读取并在07裁决身份、租户、来源、路径与选择器缺口。原实施聊天仍为 `01a13ecc-73db-7b40-ab59-622559d236f3`，显式 `gpt-6-luna / max`。R0不是数值验收，不重复派发历史P4-3A批次。

权威阅读顺序：[总体方案](01_solution.md) → [对接合同草案](02_integration_contract.md) → 本工作单。二者矛盾时先记录并修订，不能凭模型猜测实施。后续明确批准实施后按下列分批推进。

逐页输入/输出与VO字段见[字段目录](04_page_field_catalog.md)，与其余文件一起审查。平台script_id由后端分配，旧单机CLI显式ID能力保留；G1与业务发布默认两步可恢复事务，不宣称跨库原子写入。

## 1. 现有代码定位

| 系统 | 核对入口 | 已知事实 |
| --- | --- | --- |
| 演示包 | `D:/devTools/docuement/2.天津蓄水洪区/原型/演示页面包/assets/index-BvAxFjaQ.js` | 现存路径已核对，脚本SHA与初次核查一致；静态数据仅作需求参照 |
| 业务前端 | `D:/devTools/project/tianjin-waterx/waterx-front/waterx-ui-biz/src/views/flood/Rehearsal.vue:161` | 现成预演页面，路由 `/flood/rehearsal`；不是演示包hash路由 |
| 前端路由 | 同项目 `src/router/route.ts:211` | 复用现有路由、菜单与页面加载规则 |
| mock替换点 | `src/domain/flood/rehearsal/planProcessDeduction.js:1`、`planRiskRaster.js:9`、`planSim3d.js:1` | 移到明确demo适配器；生产API失败不得回落mock |
| 路线缓存 | `src/utils/evacuationRouteEditStorage.js:1` | 当前localStorage；需改为版本隔离的后端权威保存 |
| 前端API样式 | `src/api/flood/floodDispatchPlans.ts:66`、`floodDispatchRules.ts:65` | `rfcaddGet/rfcaddPost`、R数据解包与/api代理 |
| 后端主模块 | `D:/devTools/project/tianjin-waterx/waterx/waterx-rfcadd` | API/Biz分层，禁止写到工作区外层同名残留目录 |
| 文档预案实体 | `waterx-rfcadd-api/src/main/java/com/bidr/waterx/rfcadd/dispatch/entity/FloodDispatchPlanEntity.java:18` | `bbs_flood_dispatch_plan`是资料目录，不是run |
| 规则实体/控制器 | 同API `dispatch/entity/FloodDispatchRuleEntity.java:16`；Biz `dispatch/controller/FloodDispatchRuleController.java:33` | 引用规则/动作/联合调度，不复制一套 |
| 转移资料 | API `basic/entity/FsdaRelocateItemEntity.java:16`；Biz `basic/controller/FsdaExtController.java:42` | 已有转移基础资料，不是动态风险计算 |
| 安置点/安全区 | API `basic/entity/ResettlementEntity.java:17`、`SafeZoneEntity.java:17` | 已有位置/容量，保留原业务编码，通过映射引用 |
| 迁移约定 | Biz `src/main/resources/db/migration/AGENTS.md` | 实施时先看最新版本；不能硬编码复用旧V1.55/V1.56/V1.67号 |
| 文件服务 | waterx公共模块 `waterx-common-oss/.../OssEndpoint.java:47`；前端 `src/api/admin/pubFile.ts:72` | 已有上传能力，但大模型结果发布/切片/鉴权仍需适配 |
| 模型全流程 | `D:/devTools/project/2d-hydrodynamics/hydrodynamics/workflow.py` | 本地流程、报告与审计入库，不是异步HTTP服务 |
| 原生结果 | `hydrodynamics/hourly_inundation.py:577`、`triangular_solver_netcdf.py:219` | time×y×x与time×face两种格式，需要分别适配 |
| 审计数据库 | `hydrodynamics/gate_dispatch_db.py`、`sql/gate_dispatch_v1.sql`、`sql/gate_dispatch_v2.sql` | 已验收单机审计与script_id；生产多租户归属尚需补齐 |

上述为2026-10-19只读核查点。实现前按当前工作区重新确认路径/行号、AGENTS和脏文件，不覆盖用户未提交成果。

## 2. 模型实施窗口任务

### 当前仅执行R0审查

检查这三份方案是否遗漏实际模型接口约束，返回字段/ID/时间/单位/产品差异、工作量分批建议和可执行的小型合成验收设计。不要修改生产文件、数值方法、冻结容差或数据库；不继续历史P4-3A编码以替代本批报告。完成报告必须注明本批次和实际turn，避免复制旧批次。

### 批准后R1/R2负责内容

- 新建独立 `rehearsal_result_adapter` / worker包装层，负责请求合同、实际引擎能力预检、manifest导出；不得在求解器里耦合JavaHTTP/租户/业务数据库。
- 明确平台run、attempt与既有local_run/script的映射。代码版本、输入/配置摘要、运行环境单独记录；不更改旧NetCDF身份和冻结基准。
- 输出已有可信产品，清楚区分INPUT_REQUESTED与MODEL_APPLIED、结构化简化回放与实验三角。当前三角开放边界任务必须拒绝，不用关闭边界偷跑。
- 统一frame索引、SI单位、scope/时间聚合、NoData和缺项状态；质量门禁读取真实数组/输入，不只看summary字符串或成功次数。
- 给业务侧一个固定小型合成包及一份故意不完整/身份冲突的反例包，支持发布方独立验证；不导出真实DEM/人口/社会经济资料。
- 若对接模式由业务发布服务入库，显式保留原CLI默认能力，禁用重复写入者并证明发布方已完成必需G1入库；不以“模型退出0”代替端到端成功。
- 输出schema/文档/示例/测试同步。新到达/历时、暴露/损失、自动撤离等产品另立合同，不用图上假值顶替。

## 3. 给tianjin-waterx大模型的可直接使用任务说明

> 用户已要求补充后分批实现。请先核对方案包差异，然后按07当前获准范围推进；后续业务批次由主聊天验收续派，不一次改全仓。目标是在已有 `/flood/rehearsal` 页面接入可追溯、可重复查询的预演数据，涉及管理/新建、方案详情、推演结果、过程推演、避险转移和三维展示。不要另建相同页面，不修改演示包压缩JS作为实现。
>
> 先读工作区、waterx、RFCADD API/Biz、迁移目录及waterx-front中适用的AGENTS。后端实际位于 `waterx/waterx-rfcadd`；Java 8，Entity/DTO/VO置API，Controller/Service/Mapper/XML置Biz。Controller只做传输和校验，业务事务在Service；查询GET、写操作POST；沿用R、rfcaddGet/Post、ASSIGN_ID、租户/日志/权限和现有文件设施。LocalDateTime传 `yyyy-MM-dd HH:mm:ss`，另带timeZone，经适配层生成模型ISO8601；不可全仓更改日期序列化。
>
> 旧 `bbs_flood_dispatch_plan` 是资料目录，新计算scenario可引用它但不能直接把文件行改成任务。复用现有调度规则/条件/动作/联合调度、转移资料、安置点、安全区，按版本引用；新增场景版本、run/attempt/result_set、产品索引、统计查询和某次结果上的避险版本。
>
> 后端先实现元数据和异步编排，再实现受认证的worker协议及校验发布服务。输入冻结、重复提交幂等、任务租约、取消/重试、文件清单/摘要、暂存集合不可见和最终原子发布必须完整。所有客户端传入ID、对象资源和下载均检查租户/项目权限。旧G1 v2无租户归属，生产接入前必须显式补归属，历史未知批次不能自动归当前用户。
>
> 前端新增service/types/store后替换现有domain/mock数据源，五个面板共用scenario/version/run/result_set/scope/frame。切换方案应取消旧请求并丢弃迟到响应；无结果、缺测、未计算、失败与有效0不同。正式模式禁止fallback到硬编码mock；速度/到达/历时等缺产品时禁用对应图层。路线按避险版本保存，人工计划不等于真实撤离执行；3D先是同一二维产品的场景展示，不声明新3D计算。
>
> 数据库采用增量迁移并同步API/Biz实体、DTO/VO、XML、索引、全量中文COMMENT和前端类型。检查迁移编号冲突及已有表语义；不要重复生成已存在的资料表。测试用隔离合成数据，普通单元测试不连接共享Nacos/Redis/MQ/生产库，不运行288h工程案例，不提交或推送未授权修改。
>
> 按R0→R1元数据/任务→R2结果包/发布→R3页面联调逐批交付，并完成本文验收表。每批报告实际文件、命令、正常退出码、失败/跳过数、SQL迁移/回滚风险、已实现与未实现能力。测试跳过或异常退出不能视为通过。对于缺失的统计policy、资产数据、租户策略或重大工程假设，列出具体阻断项，不自行猜值。

## 4. 容易遗漏的场景与验收

| 编号 | 场景 | 通过标准 |
| --- | --- | --- |
| T01 | 两个方案同时查看，切换后旧请求晚返回 | 五页、地图、曲线、缓存均以当前result_set为准，无串数 |
| T02 | 同方案不同版本/多次计算 | 旧结果保留，版本无结果时显示暂无，不偷取旧版本“最新” |
| T03 | 重复点击提交/网络重试 | 同租户幂等键+相同摘要只有一个run；同键不同摘要明确冲突 |
| T04 | worker重启、重复/迟到回调、旧attempt结束 | 租约和版本门禁拒绝过期发布，完整记录重试，不能覆盖新结果 |
| T05 | 取消与完成并发 | 原子状态比较，不把请求取消当已取消，不在取消终态后发布 |
| T06 | 文件已写DB失败/缺一个瓦片/摘要不匹配 | 结果集不公开；重试不重复数据，暂存/孤儿有恢复或清理记录 |
| T07 | 必需G1入库失败 | 不能宣告本次完整发布成功；已有数值文件保留且可补录 |
| T08 | 跨租户场景ID/结果ID/文件URL/审计batch关联 | 返回拒绝，不泄漏存在性/数据；伪造TENANT-ID不授权 |
| T09 | 路径穿越、任意URL、超大包/ZIP炸弹/字段注入 | 白名单与大小/数量/schema门禁；无任意读文件、SSRF或命令注入 |
| T10 | 120h输入含第144h事件、非等距/缺失帧 | 明确拒绝或标窗外；不截断/补齐/外推冒充完整；时间轴真实 |
| T11 | UTC/北京时间、原点缺失、未来设计洪水 | DTO格式正确，timezone映射可逆；未知原点不填当前时刻 |
| T12 | 降雨断点、Q方向、单位换算和datum不一致 | 使用冻结输入合同；不重复换算，不丢原单位/基准，不用旧区间外值 |
| T13 | 全干、有效0、NoData/无人口资料 | 明确四类状态；有效干地0，NoData不能变0风险 |
| T14 | 峰值面积与全程足迹、当前帧与全程最大 | 指标code/policy不同，结果数值来源一致，不混同标签 |
| T15 | 深度恰等0.5/1/2/3m及区域重叠 | 区间不双算、不漏算；分级总量与同scope总体闭合，多区并集去重 |
| T16 | 断续受淹、期初已淹、末帧未退水 | 到达/历时说明采样误差及截断，不能输出伪分钟精度 |
| T17 | 道路跨区/村庄同名/耕地与人口缺空间映射 | 稳定ID去重，未匹配与覆盖率可见，不凭名称或面积猜精确人口 |
| T18 | 房屋面积/损失模型缺失 | NULL及原因；房屋面积不复制淹没面积，损失不从截图填值 |
| T19 | 方案覆盖东淀+文安洼但只有东淀结果 | coverage标部分或阻断必需产品；不能宣称全方案完成 |
| T20 | 人工路线改动/安置点容量变化/基础数据更新 | 新避险版本，绑定资产快照；不篡改旧水动力结果或其他方案路线 |
| T21 | 安置容量不足、目的地自身受淹、桥梁/路网断裂 | 有具体风险/缺项，不自动标为安全可达；自动路径功能未实现则明确关闭 |
| T22 | 计划转移人数与现场到达/已安置人数不同 | 计划、审批、执行三套状态与数据来源独立，不用计划推执行成功 |
| T23 | 三维页、二维地图、时间轴不同步或流场缺失 | 使用同一result_set/scope/frame；无数据时不沿用旧图/旧值 |
| T24 | 过期资源URL、浏览器刷新、打开已归档结果 | 权限内可恢复同一不可变结果，或显示真实不可用；不fallback mock |
| T25 | 非默认CRS、经纬轴序、高程基准未知 | 独立GIS适配验证，不改GeoTIFF Reader，不用贴标签代替转换 |
| T26 | 大时间窗/高分辨率/多用户 | 分页/按帧加载、缓存分租户与版本、并发资源限制；不一次下载全时空场 |
| T27 | 工程资料/RFCADD与水动力接口版本变化 | 版本协商与失败提示，旧客户端不偷偷读取新格式 |
| T28 | 演示数据“计算完成”、静态三维图 | 来源明确DEMO，不作为MODEL_COMPUTED或真实工程验收证据 |

性能验收应先冻结测试规模、硬件、最大包/帧大小及延迟目标，再测量；本方案不凭空承诺特定秒数。自动避险/损失等高风险产品必须有单独参考数据和复核流程，不能以页面有数字作为通过条件。

## 5. 交付物与暂停点

R0交付：字段字典、OpenAPI/JSON Schema草案、指标policy、能力矩阵、租户/身份映射、迁移清单、合成夹具和验收参数。**这些冻结前不允许开展结果表/计算核心改造。**

批准后每批交付：可追溯SQL迁移、代码/契约、合成输入/反例、前后端联调步骤和正常退出的测试证据。业务账号、部署目标/存储服务、真实数据责任人、金仓模式、统计阈值和高级计算能力若未确认，保持阻断项，不将默认建议伪装成用户批准。

原实施窗口可先审阅模型侧R0；业务侧工作单由用户交给tianjin-waterx大模型即可。不得未经授权向未指定业务聊天发消息、创建新聊天或恢复定时任务。所有新子代理/委派实施turn显式使用 `gpt-6-luna` / `max`，主聊天模型不变。
