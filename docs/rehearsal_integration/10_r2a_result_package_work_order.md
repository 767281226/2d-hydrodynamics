# R2A：小型合成结果包导出与独立校验

批次：`REHEARSAL-R2A-PACKAGE-20261019`。前置：R1 FIX1独立通过，提交 `eb45b0e` 已推送origin/main。主聊天只协调/验收/提交；原实施窗口负责代码。

## 范围

实现独立、只读结果适配模块与最小CLI：从已生成的合法小型**三角封闭域 sources=none**原生结果及其真实配置/P2/P3输入，导出可审查的结果包并独立校验。R1当前唯一受信可计算profile就是三角合成闭域，先闭合这条路径，不伪称已支持structured、Manning/rainfall、开放边界或真实工程。

不修改数值方程、通量、边界、原生结果格式/既有读写校验；不连接DB、HTTP服务、实际闸控或业务Java/Vue。R2A不做跨库发布与生产多租户授权，不实现人口/损失/历时/自动撤离/三维计算。

## 必须交付

1. 新模块（建议 `hydrodynamics/rehearsal_package.py`）使用已有原生reader核对真实输入身份及结果完整性，不能只读元数据JSON或弱化现有校验。来源配置/输入/结果只读，输出新目录，保护已有文件/目录和源文件。
2. 正式版本化包合同 `rehearsal-result-package-v1`：resultSetId、platformRunId、attemptId/no、scriptId/localRunId、modelCaseId、可信输入/配置摘要、engineMode、scope及scopeHash、metricPolicyVersion、产品清单、原始来源信息、文件清单/相对键/SHA256/字节数、验证依据与限制。
3. 重用R1执行身份校验及其可信派发参数，不能从待验证信封自身反填expected身份。数据包保留平台身份声明与实际原生证据的区别；原生文件缺少script/run绑定时，必须明示绑定来自外层受信执行上下文，不能说原生数组已独立证明该身份。真实授权/租户证明未完成则NOT_ASSESSED。
4. R2A包状态固定为离线验收/暂存，`can_publish=false`、`engineering_validated=false`。可给出某产品已从真实数组生成/校验的状态，但不能把完整性通过变为生产发布许可。
5. 输出真实 `frames.json`：与原生时间轴逐项一致；至少输出每帧最大水深、按原面面积积分的体积、全程最大水深及对应时刻，单位m/m3/s明确。结果数组仍存原生文件或严格索引，不能把每格每时刻全展开成数据库行；不制造未实现风险字段。
6. 产品选择器与R1一致：FRAME/MAX_OVER_WINDOW等受控枚举、scopeHash、frame/time/window/policy必须真实匹配。整域汇总仅对应被验证的合成scope；不假造分区。未实现的产品明确UNSUPPORTED或NOT_COMPUTED。
7. 若提供现有G1 CSV/JSON，保留原始字节与一致性摘要，验证二者配对、时间/案例来源及native artifact对应关系；缺失时产品标MISSING_INPUT。不得改写原JSON中的result_file路径以伪造可移植性，不得绕过现有legacy路径校验。R2A可只支持原稳定本地source graph，明确 `validation_basis=LOCAL_SOURCE_GRAPH_V1`；移动包后失去真实源图时应明确拒绝/未验证，不能宣称已完成可移植审计导入。可移植适配与G1 v3租户落库留R2B。
8. 包validator独立重读实际文件和源图：禁止未知字段、重复ID/相对键、路径穿越、越界/重复帧、缺文件、字节数/hash不符、非有限数值、scope/案例/输入不一致；不信任包内自报passed/COMPLETE。目录覆盖与部分写入失败需明确行为，失败不得留下看似完整的成功manifest。
9. schema、示例、CLI和正式测试同步；包资源必须能从安装后的wheel读取，不依赖当前cwd或未打包源码。新固定字节摘要资源加LF属性，避免Windows检出破坏摘要。

## 冻结小型验证

- 采用R1已钉住的闭域合成profile或明确独立的小型闭域fixture，最多64面、最多8个输出时刻、模拟不超过1s；不用120h/288h工程案例。测试中可用现有solver生成这些微型结果，模块本身不调用solver。
- 平底静水独立参考：h=0.2m，实际几何总面积由fixture解析得到，期望每帧体积A*0.2；最大水深0.2m。深度和体积汇总绝对误差各不超过1e-12 SI；不倒推参考、不放宽既有数值门槛。
- 严格校验frame索引/秒轴、面积/深度积分、全程最大值及极值时间（并列极值采用最早时刻并记录policy），不从报告文本抄数。
- 至少包含：正常导出/校验、已有目标拒绝覆盖、源文件哈希前后不变、篡改原生真实数组、假摘要、缺产物、相对路径穿越、重复键、错scope/metric policy/frame、跨案例/旧attempt/script、丢失source graph、G1错时/错案例/缺项、请求未支持产品、schema与示例一致、wheel资源完整。
- G1不存在时可以生成显式不完整审查包，但不能满足require-complete；CLI须有可供流水线识别的非零阻断退出码。schema无效/源图无效/篡改失败与正常不完整状态分开，不把失败当成功。

## 文件归属与Git

实现范围：新package模块、对应测试、`examples/rehearsal_package/`、必要最小CLI/包配置及实现说明 `docs/rehearsal_package_implementation.md`。不改主聊天维护的 `docs/rehearsal_integration/`、进度与验收记录；不改原样独立反例。不自行暂存、提交、推送；主聊天验收后统一处理，保留工作区所有已有成果。

## 完成通道

连续跨聊天发送已被自动审核拒绝，本批不再重复尝试该发送操作。完成时在原窗口final给真实turn/batch、变更文件、命令、通过/失败/跳过和正常退出码、示例与限制。主聊天在本轮使用原生 `wait_threads` 完成事件等待取回报告，继续验收；不建定时任务、不写外部轮询程序、不降低审核/权限。

使用 `gpt-6-luna / max`，主聊天模型不变。若遇常规接口/依赖问题按范围自行修复；只有新增重大物理假设或扩大真实工程范围才形成具体决策，不重新询问已经批准的B路线。
