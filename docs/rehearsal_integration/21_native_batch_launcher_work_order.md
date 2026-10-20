# 原生三角批处理启动入口工作单

本地报告维护约束（2026-10-20新增）：任务报告/仅本地保存，禁止stage/commit，本批白名单显式排除，不能通过提交.gitignore混入。本验收窗口维护未完成任务跟踪报告：状态变化及时更新，实现完成只记待独立验收，通过后记完成日期/证据/提交推送状态；完成项移入归档并保留历史。后续工作单/交接延续此要求。日报按北京时间日界，跨日验收/推送不倒算，更正留痕。

批次NATIVE-BATCH-LAUNCHER-20261020，基线8504b6f956925336ba5ec7be600782108f76d521。原实施聊天01a13ecc-73db-7b40-ab59-622559d236f3（gpt-6-luna/max）实现，本验收窗口独立验收。保护全部既有WIP/地图删除，不reset或clean，不覆盖100001或已生成示例。

## 已核实授权和决定

安排聊天真人turn01a1580b-0d60-7303-ab40-2bbc70a96ce7批准诊断后的计划，并要求完成后告知手动执行run_qingbei_nc.bat。已核实P4原生boundary_policy只支持closed_wall，开放边界准备execution_available=false；真实case_001含入出流，不能偷偷转闭壁。

本验收窗口已向真人明确说明该能力缺口，真人对问题“是否同意run_qingbei_nc.bat默认运行已验收4面Manning/降雨/安全区合成示例，并提供--engineering缺项预检”明确答复“同意：默认合成示例，保留工程预检（推荐）”。因此本批默认运行目标为显式SYNTHETIC_ONLY；不是青北真实工程。工程模式仅预检，不能执行任何工程solver或回退到合成/structured。

## 范围

1. 修改run_qingbei_nc.bat路由到明确原生三角launcher；默认采用已验收examples/security_exclusion动态combined链，4面、三时刻0/.05/.1s，保留n.035、雨1e-4m/s及D减S。不得修改冻结基准参数、增加物理、关闭安全区或旧structured保护、展示拆三角冒充计算。
2. 新launcher及严格版本化启动/工程预检输入合同，schema、说明、示例和正式测试同步。每个影响运行的字段有类型、单位/路径含义、必填/默认、范围、清楚错误。默认配置须明示synthetic_demo/SYNTHETIC_ONLY，报告/控制台/结果摘要也明示。不能把工程输入缺项填成假数据或标为ready。
3. batch从任意当前目录可用，项目路径/输出路径含空格或中文正确；引用脚本自身位置，正确pushd/popd或等价恢复调用目录。优先项目.venv\Scripts\python.exe、-X utf8；缺Python/依赖有可执行安装指引，不能自动下载安装或访问外网。不把系统python是否存在当成项目venv可用。
4. 支持--id及--script-id显式ID并原样保留；未给ID调用既有resolve_script_id，按北京时间YYYYMMDD加Unix纳秒时间戳规则。保持validate_run_id约束。--out-dir支持明确文档化的相对/绝对路径。若需要--config/--workflow/--show-latest等兼容，明确定义或清楚拒绝；未知参数必须非零，不能默默忽略/重复吞参。
5. 默认输出为项目work/results/<script_id>。目录已存在（包括100001、文件、路径别名/junction等）必须拒绝且原字节不变，提示换新ID；不修改旧报告、不自动删目录。仅本次新目录内复制白名单纯合成JSON/GeoJSON/YAML，所有运行使用复制后的相对路径；禁止引用历史生成件当新结果。不得写原模板、源码、地图、DEM或业务数据。
6. 默认链为P2→P3动态初值→combined source准备→source runner→v2 MAX EPSG4490及FRAME/native投影SHP→严格validate；不运行或要求base-none结果，不改现有runner/reader/contract/预算。安全区必须在真正计算域中排除；两个source非零效应、逐帧质量和未模拟雨量信息真实可追溯。
7. 新run_report使用独立、严格、版本化原生启动报告，明示engine/topology、mode/data_origin、case_id和script_id、配置/模块版本来源、各阶段状态/实际错误/退出码、开始结束与输出文件。不要伪装hourly报告导致旧查看器误识别。任何失败不得标complete或leave有效成功marker，不能吞异常；不能启动DB、HTTP、viewer服务器或打开GUI。
8. summary/README列面数、时刻、源项身份、active/excluded面积、初末体积、接受雨量/排除未模拟雨量、质量残差及输出位置。SHP保留.shp/.shx/.dbf/.prj/.cpg/manifest，geometry/fields来自真实native。可选标准库SVG预览不成为依赖/成功前提，不为PNG安装新库。复现说明要求新空目录/新ID。

## 工程预检（本批不执行工程）

9. --engineering进入单独工程预检，默认读取新的case_001原生工程预检模板；模板不是旧structured config的直接改名。参照20列可信原域D、水平CRS、DEM源基准/单位/掩膜/覆盖、active初值、SI糙率解释、降雨来源/时段、边界分组/时间/方向/单位/datum、原生准备/运行配置与验证依据。缺失值保留null/明确未提供，不伪造WKT、D或来源证明。
10. 文件状态检查仅限路径/存在性及必要小型YAML/JSON声明；不打开真实DEM/NPZ/CSV/DBF业务记录、不做地形映射/网格生成/solver或实际数据库写入。未提供源高程证据不能用target datum顶替。不能把bbox当D、旧回放eta/slope/area_schedule当SWE物理。
11. 工程模式始终preflight_only、execution_available=false（本批未增加边界执行）；case_001真实入出流所需开放边界能力单列UNSUPPORTED，不允许通过用户自报ready/closed_wall忽略该语义。收集并打印结构化blocker codes、具体字段/路径和下一步动作；非零退出，报告status=blocked或明确等价状态，不生成native/SHP成功产物。即使模板字段填写，也不能在本批自动执行工程；不回退synthetic。
12. 安全区工程源map/securityArea声明保留enabled=true/CRS4490，几何兼容已有记录17，不重读真实业务数据。用户未授权真实大DEM/长工程；后续开放边界或真实范围执行为重大独立设计，不由本批决定。

## 验收与交付

13. 小型合成端到端验证真实batch→native/source/SHP路径，范围不超过64面/8帧/1s。覆盖默认mode、显式id/script-id、缺省ID格式、--out-dir、外部cwd/空格中文路径、重复/非法ID、参数不识别、缺Python/依赖可操作错误、阶段失败非零/报告不伪成功、工程blockers无计算/无读取真实栅格。测试使用独占新目录，不调用真实工程入口运行。
14. 保留旧100001字节和原始冻结示例输入，任何安全保护测试不删改用户数据。生成产物只在work/.pytest_cache等明确临时目录，不擅自加入Git。Root独立反例/旧基准不修改，不重复已有无关全套验收。
15. Windows正常权限小型调用用exec_command审查参数并write_stdin等实际退出；默认环境0xc0000008不计pass。wheel测试独立进程，若launcher依赖模板资源，确保安装/源码路径合同一致、资源齐全或明确项目本地入口前提，不能出现安装后静默读其他目录。
16. 只改本批launcher、batch、合同/schema、纯合成/工程预检模板、说明、正式测试；不改数值、reader/contract、安全几何、R2A/已验收SHP物理语义。18–21等Root记录/独立probe由验收窗口维护。无源码并改、无新聊天/子agent/自动化、无重试被拒通知。实际final列batch/turn/命令/pass-fail-skip-exit、手动脚本绝对路径/参数、输出位置/限制/前置条件。验收窗口锁源码、独立验证及最小候选/wheel后按持续授权发布；实施窗口不提交推送，也不代真人运行真实工程批处理。
