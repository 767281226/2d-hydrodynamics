# Manning/rainfall原生三角结果SHP适配工作单

批次：NATIVE-SOURCE-TRIANGLE-SHP-20261020。发布基线b2f40c3c3882b4d1b2935cf4d9fe8f94525ea49b。用户重启后已明确恢复，安排聊天真人turn=01a15740-f95c-7a11-88c8-9a055d835ef3；原实施聊天01a13ecc-73db-7b40-ab59-622559d236f3（gpt-6-luna/max）实现，本验收窗口独立验收。记录17及旧批次结论继续有效，不重复派工或大集合验收。

## 范围与不可变边界

仅将已经存在的Manning、rainfall、manning_and_rainfall原生结果接入只读SHP导出和独立包验证。三种模式均已由既有P4-2 runner/reader支持；不增加方程、源项、边界通量、时间积分、干湿或径流转移物理。导出器不得调用runner、重算状态或生成假native结果。

保留全部WIP、地图删除及旧100001产物。不读真实大型DEM/社会经济数据，不重跑工程，不连接DB/HTTP，不做Java/Vue、GUI/GPU或开放边界执行。只用不超过64面、8保存时刻、1s的小型合成运行。真实工程输入缺项只在后续列清单，本批不读取工程栅格。

生产数值核心、三种native reader/contract、R2A和安全区几何模块保持不变；若发现真实上游缺陷，报告具体反例由验收窗口确定最小补修，不以弱化reader/预算/容差来让导出通过。修改本批adapter、schema、文档、示例与正式测试；18/17、独立参考/反例归验收窗口，实施窗口不修改。

## S1 版本与入口

1. 原`native-triangle-result-shp-export-request-v1`及manifest-v1仍只接受none/closed_wall。旧类、默认schema打印、CLI命令和拒绝source行为保留，旧冻结测试原样通过；不得直接把v1的Literal/枚举扩宽来伪装兼容。
2. 新增独立v2请求：`format: native-triangle-result-shp-export-request-v2`、`schema_version: '2.0'`、必填`result_kind: manning | rainfall | manning_and_rainfall`。`run_config_file`指向该模式的顶层执行YAML。其他selector/scope/CRS/新输出子目录字段沿用v1语义和严格类型，不引入隐式默认family。所有输入有类型、单位、必填/默认、范围和清楚错误。
3. v2 manifest独立format=`native-triangular-result-shapefile-v2`、schema_version='2.0'，真实标出family、native format/product_type/solver_version、sources_applied及applied/enabled/nonzero-effect状态。验证依据不得标成P4-1 none，engine_mode区分Manning与source运行。保留experimental=true、engineering_validated/production_approved/can_publish=false，以及原生无platform_run/attempt/script身份的NOT_ASSESSED边界。
4. 现有`hydrodynamics-triangle-shp export/validate`可按请求format分派；schema默认仍v1，新增显式v2 schema入口/参数。v2静态schema单独随wheel打包，不改变v1资源含义。公开Python API可以增加v2类型/函数，原v1 API保留。

## S2 严格native身份和只读来源图

5. manning使用现有`read_triangular_manning_result`；rainfall/combined使用`read_triangular_source_result`。保持enforce_configured_result_path=True，使用对应现有capability验证。显式result_kind须与顶层config.format/sources、native格式、所有source enabled/applied标志一致。未知family、复制/改名的错误native、prepared-only数据、structured NC、错case/mesh/time/path、错来源摘要、旧source preparation全部拒绝，不降级到none reader或基线none结果。
6. 来源图最少覆盖请求、顶层执行config、base config、source config、source preparation、P3、P2及实际native文件，逐角色记录路径/字节数/SHA。安全区输入config、几何/SHX/PRJ及原域D等由已有证据引用的依赖同样重读并绑定，不只信任自报applied=true。输出读取前后和发布后fresh校验核对原图；原源文件一字节不改。
7. 不要求不存在且未参与source执行的base-none结果文件；source-only链路在base result不存在时合法。不得偷读base-none结果代替真正source状态。Manning的legacy_unit1_as_si_manning声明、物理单位s/m^(1/3)、rationale及真实source-preparation身份可追溯，不能把输入provenance当成已施加物理。
8. 导出包仍是LOCAL_SOURCE_GRAPH语义，验证依赖原始输入；不冒充便携、自认证或密码学执行证明。即使数值相同，none/manning/rain/combined及不同顶层config/source graph的result_set_id也须区分。

## S3 三角面、selector及源项预算

9. 每个feature对应实际native face_id及三个原生节点；稳定int64 ID以精确十进制文本写DBF，不经浮点。不得矩形展示拆分、重网格化、合并、中心点裁切或插值。FRAME字段仍为真实保存帧的深度、水位、momentum/depth速度、时间与native米制面积；MAX_OVER_WINDOW为所选保存时段逐面最大深度及最早并列时刻。全域/湿面严格按所选值大于阈值选择；全干可产生有效空层。
10. 时间/index必须精确匹配保存轴；MAX端点均为保存时刻且包含两端，不扩大窗口/插值。native CRS与EPSG:4490输出均保留同一计算面和native area_m2；局部原点稳定几何面积/方向检查保留，不放宽容差。水位遵循已知垂直基准，缺失仍按现有明确支持范围拒绝，不编造datum。
11. v2 manifest带类型化预算/源项摘要。验证器从重新严格读取的native depth和area逐帧独立积分体积，核对完整native ledger及保存时刻/区间对齐；selected window摘要必须对应真实选段。Manning没有虚构水量sink，rain/combined的已接受降雨量须对应native rate×active面积×实际区间，不能重算solver。最大同步水量若给出，取逐帧积分的最大值，不把逐面不同时刻峰值相加当成同步体积。
12. 安全区仍为非计算域。P2/P3/base/top/source/native/SHP绑定相同active面/排除证据；无安全区有效场景继续支持。rain/combined将active接受雨量和排除区潜在未模拟雨量分开，并保留false的loss/transfer语义；最大湿面或FRAME筛选不重新改变原预算。验证器重新核对安全证据与几何支撑，不先算出S内正水再清零，不用展示遮盖替代排除。
13. requested/prepared/applied/nonzero-effect分清：零雨强和零速度Manning仍可具有真实applied模式而nonzero_effect=false；不得把它们标成none。至少一个动态非零Manning效应和一个非零雨量/combined案例验证真实source结果，不能全部靠静水相同值证明family。

## S4 输出事务、兼容与验收

14. 沿用现有新子目录/不覆盖、路径/Windows junction与reparse保护、逐组件哈希、独立SHP/DBF/PRJ/CPG读取校验。篡改数值/面ID/顶点或重算组件摘要仍须与实际source native逐面比对而拒绝。
15. 失败始终抛清楚异常且CLI非零；stage或发布后失败均安全退役本次成功marker，保护并发新增/修改内容与被替换根/父链，不能递归删除他人内容。保留fresh验证和已关闭的postpublish吞异常、stage所有权、投影消差修复。v1/v2共用逻辑可最小提取，但既有冻结故障注入点和语义保留，不进行无关清理/大重构。
16. 独立验收覆盖三family、FRAME/MAX、full/wet、native/4490、无安全/有安全、dry/zero/nonzero-effect、真实逐帧/区间预算、子窗口及最早并列、超大ID、source-only无base结果；模式/路径/轴/预算/来源篡改与source漂移、自己重哈希后的DBF/几何/manifest篡改、旧v1拒绝和事务回滚。旧68项不重写；只对新改动运行必要兼容门禁，不无故重复旧大集合。
17. 同步中英文说明、请求/manifest/schema、自包含纯合成CLI示例、正式测试与wheel安装实跑。Windows用项目.venv，显式-X utf8；受限进程0xc0000008不算通过，正常权限小型合成调用通过exec_command后write_stdin等到真实退出。两个setuptools wheel测试各在新进程，避免全局目录缓存；跳过/崩溃不计通过。
18. 实施final列实际batch和turn（不是旧turn当chat ID）、修改文件、支持/拒绝矩阵、真实命令/pass/fail/skip/exit、未运行范围。实施窗口不Git提交/推送；验收窗口按实际final冻结源码、运行独立反例、最小依赖隔离候选和wheel，再按已有授权仅发布相关闭包。禁止新聊天/子agent/定时任务及重试被拒的完成通知；本窗口活动轮次wait_threads读取。
