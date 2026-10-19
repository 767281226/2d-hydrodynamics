# R2A独立验收记录（2026-10-19）

批次：REHEARSAL-R2A-PACKAGE-20261019。实际完成turn=01a15371-240f-7720-9b14-1236ff1cfa1a，已核对完成事件cursor=3c8424f9-ce4e-4e24-9e54-4a4591a3a1f1:127。未把旧R1回归或错误turn当作新交付。

## 当前结论

**FIX2最终独立验收通过，允许按下述最小依赖闭包提交推送。** 首轮及FIX1失败记录保留，不能将其当作当时已通过。

首轮暂不通过，不提交推送（历史结论）：正式21项与独立8项合并运行：28 passed、1 failed，pytest正常退出1、无跳过，24.50s。另外实际源图字节变化的独立复现1 failed、正常退出1，1.98s。合计独立9项中7通过/2失败，两项指向同一事务完成缺口。实施方131项旧回归的报告未替代本窗口复验；本窗口在已有确定阻断后没有重复不受影响的大集合。

原样独立8项文件 work/acceptance_rehearsal_r2a_20261019/test_r2a_independent.py，SHA256=03d28d0403eb4fd46a84726b68bc4b5902de749264f4697da61f32a0ce0e7356。
第二文件 test_r2a_source_race_independent.py 的冻结SHA256=8a6e2bc52403e5ccd930679efe809be3cf6822e3b573d88826476f1a4540e920。第二文件在首次复现之后增加了成功时必须fresh validate的else检查，允许一致性重试、不允许删除末尾校验来绕过；原失败断言未放宽，原8项文件未修改。
固定合成profile SHA256=52589d5056c5e3b246c95c7affbc8ca3a0e43f63b3d8ac8db05dd9e43e21ea8b，独立夹具显式钉住此值，面积2m²/h0.2m/体积0.4m³/0、0.5、1s，绝对门槛1e-12不变。

## 已通过的独立边界

真实P2/P3/native静水数值与逐面面积积分、最大值及真实时间轴；来源文件字节前后不变；完整/缺失G1两种状态；G1原CSV/JSON字节与result_file文本不改写；身份明示OUTER_EXECUTION_CONTEXT_ONLY/NOT_ASSESSED且can_publish/engineering_validated为false；请求级require_complete无重复CLIflag仍返回3；篡改frames数值并重算其文件摘要仍拒绝；worker替换attempt拒绝；原source graph缺失拒绝。正式21项同次通过。

## 必修：发布目录后的失败仍残留成功manifest

hydrodynamics/rehearsal_package.py:1387起export_result_package在try内写stage并验证，1408 os.rename(stage,destination)后退出try；1412末尾validate_result_package(request_path,destination)在清理保护之外。

独立反例A：真实完整G1包在stage验证通过，模拟末尾复验读取/检查失败。export实际抛RehearsalPackageError，但新目标目录仍有manifest.json，package_status=COMPLETE_OFFLINE_REVIEW，quality checks仍PASS。

独立反例B：不伪造validator结果，只在真实os.rename完成后给临时合成request.yaml追加一条注释，模拟源图在阶段间改变。末尾真实validator正常发现identity/export_request.yaml与当前源图不符并抛错，但同样残留完整成功manifest。该反例不修改任何项目真实配置或数值数组。

这违反10工作单第8条：失败不得留下看似完整的成功manifest。CLI返回错误码不能使目录内成功标记自动失效，下游发现器仍可能误用。

## REHEARSAL-R2A-FIX1-20261019 修复要求

将末尾复验及其异常纳入完整导出事务。任何报告失败的导出，不得留下可被当作有效完整包的成功manifest；可安全回滚仅本次创建内容、移除/隔离成功标记并留下明确失败证据，或对源漂移做有界一致性重试。保证现有目录/文件/源图及并发出现的非本次所有内容不会被删除或覆盖。不得删除末尾fresh校验、弱化source/hash/身份/selector检查、修改原9项反例或numeric门槛来规避。

同步正式测试及失败语义文档；原样重跑9项独立反例与正式21项，必要R1/三角/G1回归正常完成无跳过。只改本次package事务实现及相关正式测试/实现说明；不要混入12SHP或14安全区、不要改本记录/工作单/原样独立反例，不提交推送。完成final报告真实新turn/batch，验收窗口原生wait_threads读取，不重试被拒通知。

安全区13/14及SHP12是已授权后续任务，本批修复不改变它们的优先级/合同，不将R2A修复通过冒称整体SHP/安全区已交付。

FIX1派工已确认：实际新turn `01a153c0-f452-7472-8ac4-dd0204f7dffa`，启动cursor `3c8424f9-ce4e-4e24-9e54-4a4591a3a1f1:128`，inProgress；只修R2A事务，不重复首轮或12/14。

## FIX1复验及FIX2

FIX1实际完成turn=01a153c0-f452-7472-8ac4-dd0204f7dffa / cursor:138已处理。原9项独立反例全部关闭，正式24项也通过；合并新增一项目录所有权反例，实际33 passed/1 failed、正常退出1、无跳过（26.97s）。另新增修改manifest恢复反例1 failed、正常退出1（1.96s）。本轮独立11项中9通过/2失败；两项都属于FIX1新增回滚逻辑的并发内容保护，不能再把首轮成功marker残留当成仍未修复。尚不提交推送。

新增原样文件：test_r2a_rollback_ownership_independent.py，SHA256=cae4dad3e1c8b07045e022f1e03b466f838f9bf43bc38f53eca9aa5fa0381554；test_r2a_manifest_recovery_independent.py，SHA256=a6253e5ceeb011faabe03d8b7c6f0606ebd82484e63220f1ae01879174045dad。均位于work/acceptance_rehearsal_r2a_20261019；原两个文件SHA未变。

### FIX2问题A：父目录替换后仍按叶子ID删除并发路径

_rollback_published_export（约1361起）仅先核对根目录身份，再对文件核对叶子ID；嵌套父目录ID只在所有文件删除后清空目录时核对。反例实际rename发布后，把原native子目录搬到测试临时旁路，再创建新的native目录并硬链接原result.nc，随后更改真实临时request字节触发末尾fresh校验失败。新的native/result.nc是并发路径，其inode与原文件相同。当前回滚仍把该路径删除。应在任何叶子删除/移位前验证其全部父链仍是本次捕获的目录，父链发生替换就保留该子树，不能只在rmdir时才检查。

### FIX2问题B：并发改写的manifest被直接丢弃

当前对manifest特例跳过内容比较，inode未变即直接unlink。反例在真实发布后给manifest加入并发用户字段，真实严格validator正常拒绝，但回滚删除了全部修改字节，没有恢复副本。这和“并发修改保留”的交付声明不符。成功标记必须退役，但如内容已被他人修改，应以安全的恢复/隔离文件保留原字节（同输出父目录，不覆盖现有文件），不能直接丢掉。替换inode或根目录的他人内容保持不动，失败报告准确指出未清理/恢复位置。

### REHEARSAL-R2A-FIX2-20261019 范围

只修上述回滚父链所有权和修改marker恢复；保留末尾fresh校验及原9项已修行为。补正式回归与恢复语义文档，原样跑全部11项独立反例。禁止改独立断言/哈希或原数值门槛，不改12/14/本记录，不混入SHP或安全区功能，不提交推送。安全回滚不要求删除并发数据，宁可留下明确失败的恢复文件；不得以扩大递归删除来让目录消失。

FIX2启动已确认：实际turn `01a153d4-03f9-7de0-86aa-c1d050f7c250` / cursor:139，inProgress；显式gpt-6-luna/max。

### FIX2活动轮次补充：Windows junction输出越界

对同一输出父链合同追加了实际Windows junction核查。_has_symlink_component目前只检测Path.is_symlink，junction会返回false。测试在自身basetemp里创建请求目录的同级新目录，junction作为请求目录下的output_alias，输出键output_alias/package仍是无..的相对子键。当前export正常成功写入实际同级目录，没有拒绝这条越界父链。该测试实际1 failed、正常退出1、无跳过（1.91s）；junction与目标均为测试新建，finally只rmdir确切junction，不读或删除真实目录。

新增原样test_r2a_windows_junction_independent.py，SHA256=2e21dd9d5f7bb8b2319436aae145421b9331fb80dab6bddf1fc6e750832c6ba6，独立集合现12项（其余四文件不变）。此项在FIX2进行中补入同一父链/路径保护范围，不另开重复批次。应拒绝Windows junction等目录链接/重解析别名的穿越，并确认输出解析后仍在请求目录的新子路径内；保持Python>=3.11支持，不能只依赖3.12才有的Path.is_junction无条件调用。不放宽原有本地source graph输入路径合同，不以这次修复扩大跨目录写出。

## FIX2工作区独立复验

实际FIX2完成turn=01a153d4-03f9-7de0-86aa-c1d050f7c250 / cursor:173已处理。本窗口原样12项独立+29正式同次41 passed，正常退出0、无跳过，29.29s。5份独立文件SHA均保持记录值，固定profile SHA未变，静态依赖/数值核心的源文件快照没有任何漂移。已经核对父目录链逐层身份验证、独占manifest恢复字节、成功marker退役、Windows reparse point及规范化输出子路径保护。原阻断在工作区内关闭。

发布门禁仍进行中：正在基于HEAD eb45b0e的独立候选（不借用工作区未提交模块）运行必要依赖回归及随后wheel隔离安装。未在该门禁完成前提交推送。SHP12/安全区14也尚未实施，不能把R2A功能通过冒称整体需求完成。

R2A工作区功能门禁41项已独立通过且源码快照已锁定。为继续用户已授权的业务修复，确认原窗口现在开始独立的12批次；R2A候选回归/wheel发布门禁在本窗口继续，不把待完成的发布验证标成通过。12不修改R2A包模块/固定profile/独立反例，双方不并改生产文件；14待12独立验收后启动。

## FIX2发布门禁最终结果

隔离候选基于Git HEAD eb45b0e61942a357bc927adf4e081c1057e51b02，未借用其他未提交模块：285 passed in 107.25s，exit0，无跳过；29个已加载项目模块逐一验证来自该候选，editable回退已禁用。覆盖R2A正式/12独立、R1/原8独立、P2/P3-A、closed solver/contract、G1及必要config/NetCDF/外部证据契约。

离线wheel构建成功，SHA256=1b14adf4b37905c388645ad702d9e2aef3b021e69eac8cf86fff7f4badb8d84a；安装到独立目录后以隔离Python再次生成2面/1秒原生结果及真实G1完整包，3帧h0.2m/体积0.4m³均通过，26个项目模块全部来自安装目录，固定profile确从wheel读取，CLI schema及两个入口元数据正确。can_publish/engineering_validated始终false，原生文件字节不变。完整smoke产物仅在work验收目录，不提交。

本次代码发布只包含R2A、必要closed solver/result合同与G1/证据依赖、对应正式/永久独立测试、合成例子及说明。__init__/pyproject使用最小候选内容，G1文档排除未纳入本批的数据库段落，通用导出示例保留HEAD版本；真实数据/地图删除/DB功能/其他未提交工作保持工作区原状。五份原样独立测试作为永久回归，连同solver固定参考JSON设LF，避免Windows检出破坏已审查摘要。文档范围删减和恢复既有HEAD示例没有改变已测试wheel中的运行代码。

SHP12已独立开工，安全区14后续进行；本次R2A通过不等于SHP或安全区功能通过，也不是开放边界或真实洪水工程验收。

实际暂存门禁额外检查到了两份新文件的多余EOF空行（未跟踪时普通git diff不会检查它们）；仅在候选移除末尾空行，AST逐一完全相同，5份冻结probe没有变化。最终重建wheel再次隔离安装通过，35个package源文件/资源与待提交候选逐字节一致，26模块均来自安装目录。最终wheel SHA以上述1b14adf4为准；原工作区生产文件的格式同步仍交原实施窗口处理，不并改业务逻辑。
