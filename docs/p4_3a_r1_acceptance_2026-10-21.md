# P4-3A 准备层 R1 独立复验（2026-10-21）

批次：T-003 / P4-3A-BOUNDARY-CONTRACT-20261018-R1。验证窗口01a1596e-ce50-7360-b140-7e9b001383aa。实施turn01a15bc9-d4cb-7433-aa5a-ca72f2482d4c已完成并冻结；本窗口只读生产。

结论：**准备层R1独立通过，可选择性发布本批必要文件；不授权开放边界RHS。** 首轮F1/F2/F3及两处文档合同差异已关闭。完整外态闭合和实际开放边界数值参考仍未冻结，属于后续设计门槛。真实工程输入仍未补齐。

## 证据身份与范围

冻结的原33项探针SHA-256为b1b06301ec277b40b48ed23d1d36216a3120759359e3ad4cf0167f8dcff7e762；机器参考SHA-256为7ca081fcb9ec3cb1379db70426456fc6413e7110892c2b25353a80b444185c4a，均未修改。原探针逐字节复制到新独占目录运行，历史首轮失败证据未覆盖。R1启动时冻结171个生产/正式测试/相关合同文件，门禁后核对未漂移。

本轮证据在 `work/acceptance_boundary_contract_20261021_r1/`；安装后的原样探针位于同级 `work/acceptance_boundary_contract_20261021_r1_installed/`。临时脚本、原始日志、生成NC/SHP及任务报告只本地保存。没有重跑T-001整批，没有读取真实大DEM/社会经济记录或连接数据库。

## 缺陷关闭与合同边界

| 原问题 | 独立复验证据 | 结论 |
| --- | --- | --- |
| F1：安全区闭壁边、别名可绑定开放请求 | 原33项中的流量/水位/别名原样通过；正式部分重叠用例通过；新增手工注入P3 JSON的混合别名仍拒绝，不能靠绕过P3写入器进入P4。独立按x=1.5/2.5的实际边坐标确认保护边集合；修改安全源字节、伪造保护边hash/数量并重新计算合法外层摘要，仍被真实P2来源重验拒绝。 | 关闭 |
| F2：显式覆盖替换暂存期间出现的无关文件 | 开始无目标时，即使overwrite=true也走原子不替换发布；最后发布函数处才出现的目标也保留。已有目标须为结构有效且同case_id的准备产物，身份及SHA复核；换inode但字节相同、readback后换内容、异case/非法标记均拒绝并保留原目标。成功更新同case请求及失败临时清理通过。 | 在下述独占合同内关闭 |
| F3：6个固定标记接受整数0/1 | 原6个整数反例原样通过；新增6字段的浮点、字符串及相反bool均拒绝，schema仍为固定boolean。 | 关闭 |
| format/schema_version必填与英译 | 正式加载器与生成schema均要求两字段；中英技术表一致。英文亚临界行改为complete exterior-state closure，与权威中文一致，未替用户选定方法。 | 关闭 |

可信来源链为：P2 reader重读原配置、实际S及D来源并重验差集几何、面身份和保护边摘要；P3读者比对实际P2与包中安全证据，构造包时从已核定P2边集合得到保护边并检查物理边交集；P4准备仍重读这一链。新增P3缓存不把自报hash当作认证，也不重复推导新的物理边界规则。

已有目标覆盖的mutex只协调同Windows会话、同规范路径键、使用本模块的发布者。本轮真实双进程对同一旧快照竞争，恰一方发布成功，另一方因身份漂移拒绝，临时文件清理通过。**已有目标的覆盖要求调用方在发布期间独占控制目标；没有任意外部写者/别名下的文件系统CAS。** 独立限制探针在最后摘要复核后注入不协作外部写入，证实该窗口仍可被替换，已作为限制记录，不能包装成保护通过。中英技术说明均明确此条件；开始不存在的目标使用OS不替换发布，不依赖该独占假设。

## 实际命令与结果

所有命令从项目根使用 `.venv/Scripts/python.exe`，显式保留faulthandler；各basetemp为独占新目录。完整命令在证据目录commands.md，原始日志/XML保留。下表分别计数，不把不同环境的重复执行当作新增用例。

| 门禁 | 实际结果/原始退出码 | 证据 |
| --- | --- | --- |
| 工作区正式边界与业务 | 44+26=70 passed，exit0，0 skip，日志无原生异常 | formal.log/xml |
| 原33项独立探针，获准普通权限环境 | 33 passed，exit0，0 skip | independent.log/xml |
| 新增R1反例与合同限制观察，获准普通权限环境 | 18 passed，exit0，0 skip；其中一项验证已声明的外部写者限制 | additional_escalated.log/xml、cooperating_publishers.json、external_writer_contract_limit.json |
| R1安全区集成，沙箱 | 3 passed但含0xc0000008，exit0，**非干净通过** | security_sandbox.log/xml |
| 新增探针，沙箱 | 0xc0000008后访问冲突，exit -1073741819（0xC0000005），没有完整JUnit，不计通过 | additional.log |
| 未含R1的已发布基线安全区对照，沙箱 | 同类异常，exit -1073741816（0xC0000008），不计通过 | security_baseline.log |
| R1及已发布基线安全区，分别获准沙箱外对照 | 各3 passed，均exit0、0 skip且无原生异常 | security_escalated.log/xml、security_baseline_escalated.log/xml及基线模块来源JSON |
| 微型原生I/O诊断，无项目模块导入 | 同一合成NC的250次lstat/只读打开均完成，exit0；不能证明低层问题全局消失 | native_io_sandbox.log |
| 离线wheel构建 | 初次沙箱PermissionError/原生异常，exit -1073741816；获准正常环境、仓库内TEMP重建exit0 | wheel_build.log、wheel_build_escalated.log |
| 实际安装CLI | pip离线target安装成功；首个验收脚本把Windows默认编码按UTF8解码，脚本exit1，非产品断言；改存原始字节并显式PYTHONUTF8=1的新目录CLI/readback exit0，未改生产 | install_and_cli.log、wheel_install.log、verify_installed_cli.log、installation_evidence.json |
| 安装包原33项＋安全区3项 | 36 passed，exit0、0 skip，日志无原生异常；项目模块均来自installed，无live回退 | installed.log/xml、installed_imports.json |

安全区集成测试会运行既有4面封闭none/rain合成推进及SHP链路；这在本次受影响回归范围内。没有运行开放边界推进、288小时工程或新物理。实施final中的“No solver”不能解释为这些既有封闭测试完全没有数值执行；本记录按实际测试代码说明范围。

## Windows异常定位结论

异常不能仅因exit0或“3 passed”而忽略。本轮既保留了exit0的诊断，也保留了真正异常退出；按正常权限流程获准对比，未禁用faulthandler或删测试。未包含R1的发布基线也在沙箱中复现，R1与基线在沙箱外均干净通过。因此证据支持**沙箱相关的既有原生文件/句柄环境问题，不是本次R1独有回归**；具体驱动、注入或原生库组件未定位，不声称已修复该平台问题。该沙箱环境不用于宣称数值集成已通过。

[Microsoft NTSTATUS表](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-erref/596a1078-e883-4972-9bbc-49e60bebca55)将0xC0000008定义为无效句柄。[CPython 3.12.14的faulthandler实现](https://github.com/python/cpython/blob/v3.12.14/Modules/faulthandler.c)会先输出Windows异常栈，再继续异常处理链；因此带有该文字的日志与进程后来exit0并不矛盾，但它仍须单列。以上只解释诊断机制，不据此推断具体原生组件根因。

## 最小发布候选与安装

候选从已发布HEAD c57ca738的hydrodynamics/pyproject/README建立，只覆盖triangular_business.py和triangular_boundaries.py，并在基线pyproject增加边界CLI注册。没有复制当前其他未提交模块或整份WIP pyproject。wheel SHA-256为93e777b6b939ca4115c2040b2e2f1141eb55a7e7b8ab743e86cc6e704d628aae；50个package资源在候选、wheel、安装目录逐项核对，实际CLI/readback的30个加载模块全来自installed，安装专项另核模块来源。两份运行模块与已复验工作区字节一致。

安装采用离线pip --target，复用既有虚拟环境中的第三方依赖；真实生成的console exe在独立cwd、显式目标PYTHONPATH及PYTHONUTF8=1下运行。验证了本批模块依赖闭包和入口，不声称是所有依赖版本或全新机器环境的兼容认证。普通外边/非安全区孔洞的原示例继续可准备。

允许发布白名单：上述2个运行模块、边界CLI的单项pyproject差异、正式准备测试、冻结工作单/中文ADR/英文译文/中英技术说明、2个合成配置/参考、首轮及本R1验收记录。原工作区pyproject的DB/vector/overlay/SQL等WIP、其他代码、地图删除、真实数据、临时脚本、生成件与任务报告排除。实际暂存对象核验、提交与远端SHA另存本地publication_evidence.json，不因可发布而提前记作已推送。

暂存格式检查补记：新文件首次进入index后，git diff --cached --check发现两份ADR的Markdown行尾空格/末尾空行、中文技术说明末尾空行和正式准备测试末尾空行。仅在待发布Git对象中清除这些空白；文档非空白内容及正式测试AST一致，运行模块/wheel、原33项探针和机器参考不变。工作区生产文件不改写；原失败日志和规范后的对象摘要保留，不把此格式过程当作新增测试通过。

## 后续边界

T-003本轮准备层修复通过；T-004的完整外态方法、版本化互补状态、实际HLL通量、两阶段CFL/回滚和非静态数值参考仍未选定/未验证。机器参考中pending_main_review原字节保留，不据本轮几何请求验收伪装数值执行批准。T-002工程资料缺项保持，不能运行真实青北工程。其他堤闸、径流、动态干床、风险指标和业务展示能力不随本批扩张。

21日用户另要求20日日报和未完成跟踪：两份已先行本地保存并实际单次通知协调成功，凭据reports_notification_receipt.json；19日报SHA保持6ef089d693db0c6c54ef1208ab24085ff781fc76a267158766959239e4e11328。本批报告、测试/通知均按实际日界记录，不倒记昨日。下一物理设计由协调依据既有路线另行决定，本窗口没有派新实现任务或建定时轮询。
