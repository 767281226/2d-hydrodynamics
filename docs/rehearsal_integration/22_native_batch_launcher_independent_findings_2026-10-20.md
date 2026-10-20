# 原生批处理入口独立验证记录（进行中）

批次：NATIVE-BATCH-LAUNCHER-20261020。基线：8504b6f956925336ba5ec7be600782108f76d521。实施turn：01a15812-2688-7761-bd11-68fafa8ce9e8。当前结论：**R2最终独立验收通过，允许按冻结白名单提交及普通推送。** 本记录不将实施自测等同独立验收。

## 独立证据

Root实际以正常权限运行work/acceptance_native_launcher_20261020/netcdf_path_probe.py，退出0。探针仅在新建work子目录写读一维单值合成NC，无求解器/真实数据。证据：netcdf_path_probe/evidence.json。

环境：netCDF4-python 1.7.5、libnetcdf 4.10.1。

| 验证项 | 结果 | 事实 |
| --- | --- | --- |
| ASCII目录原生Dataset文件写读 | 通过 | 单值1.25m原样回读 |
| 含空格的ASCII目录原生Dataset文件写读 | 通过 | 空格本身未导致失败 |
| 中文目录原生Dataset文件写读 | 失败 | Dataset(str(path))返回PermissionError(13)；已用正常权限排除仅沙箱解释 |
| 内存Dataset＋Python字节IO中文路径往返 | 通过 | 65536字节；数值、单位、中文来源属性均回读一致；仅证明该IO方案可行，不等于完整UGRID链已通过 |
| 独立CMD调用器中文/空格路径及caller cwd | 通过 | helper_smoke.py使用新建假BAT，退出0；未调用生产模型，不能计模型测试通过 |
| 当前完整BAT→P2/P3→源项→两类SHP独立验证 | 未验证 | 21项独立案例已准备，等待本轮修复冻结；尚未执行生产gate |
| 隔离候选/wheel及发布门禁 | 未验证 | 辅助脚本已准备，未构建、未提交、未推送 |

实施方报告当前正式自测5/6通过，合成链卡在准备NC写入；此为实施方证据，未充当本窗口独立通过数。此前引用缺失与预检旧字段名由原窗口修复，尚需统一冻结后独立核对。

## 缺陷定位与建议

已确定的环境行为是中文文件路径的底层NetCDF文件IO兼容问题。8.3别名经既有Path.resolve还原中文绝对路径，不能据此证明兼容。不能把失败报告标complete、删掉中文路径断言、伪造NC/SHP或降低strict reader/源图校验来绕过。

建议主协调审查最小纯IO兼容范围；内存Dataset经Python字节IO已有小探针证据，仍须保留文件原子发布、异常保护、数值/变量/单位/来源属性与源图语义。若调整上游writer/reader，应明确21工作单的IO例外、变更文件及受影响回归；不修改数值、几何或合同约束，不以此引入新物理。

## 分工及后续门禁

用户最新分工：主协调01a1585c-194b-7352-bb24-3ce48743879b负责技术方案、分配、新返工/下一批决定及最终交付；原窗口实施，本窗口独立验证并用final给逐项结论。现有本轮已授权修复继续，不重复派工、不中断，不重试被拒通知。

冻结后需实际运行独立BAT正例、旧目录/ID/参数/路径保护、工程填满仍不计算且不读数据、故障报告，以及真实4面/3时刻/6m²活动域/2m²排除区/体积与雨量/SHP几何。通过后再做最小候选、wheel和按既有授权选择性提交推送。不通过产物禁止发布。

任务报告/仅本地保存并显式排除Git；跟踪报告随状态更新，实现完成只记待验收，独立通过才归档完成，保留日期、证据及提交推送状态。

当前可见已有压缩历史，部分旧临时验收目录当前不存在；正式历史证据已按验收记录/Git/真实turn交叉核对。本窗口无精确上下文计数接口，不估造比例。新一轮脚本、状态和不可重复项保存在work/acceptance_native_launcher_20261020/STATE.md。当前不迁移；若需交接，应在安全批次边界冻结后由主协调建立替代验收聊天，不能并行改同文件，也不使用handoff_thread搬移Git工作区。

## 中文路径IO修复后的独立复验：不通过

主协调已批准四处必要NetCDF调用及局部共享适配，不改数值/格式/安全区/严格校验。实际新实施turn为01a1586f-c681-76e3-9685-077d37617d8b，已完成并冻结。本窗口读取了真实final及实际文件，未继续等待未送达的消息。

本轮独立测试文件work/acceptance_native_launcher_20261020/test_netcdf_io_independent.py，SHA256=c3f62e7d2b35fdbf6a2da9da428c08caadd992297da54b9ae10700aa6ced9eeb。实际6项：**3 passed、3 failed，0skip，正常退出1，1.16s**。全部在本窗口新建目录使用合成文件；失败为真实字节被覆盖，不是模拟validator结果或实际用户文件。门禁JSON为同目录independent_io_gate.json。87个生产Python模块运行前后SHA一致，未并改生产。

### 阻断A：clobber=False未作用于最终目标

open_netcdf_dataset把clobber=False传给新的ASCII临时文件，最终_publish_atomically仍无条件os.replace覆盖真正的Unicode目标。

两个独立反例均失败：目标原本存在时，原字节被新的HDF5字节替换；目标初始不存在、在真实最终复制过程中出现时，新出现的独立文件也被替换。临时后端无冲突不等于最终目标允许覆盖。

建议最小返工：保持原Dataset参数语义，将目标级no-clobber传入最终发布，并采用不会覆盖已存在目标的原子发布方式；不能只补前置exists检查而保留同一发布竞态。核查P2等上游overwrite=false是否正确传递，不改变内部明确允许覆盖的自有临时文件语义。

### 阻断B：r+身份复核早于最终复制

当前r+在进入_publish_atomically之前检查目标身份，随后还要复制到目标同目录临时文件并fsync，最后os.replace。在此真实复制完成时向原目标写入独立并发字节，当前调用未报错且覆盖了并发内容。

建议最小返工：将期望身份绑定带入最终发布，在同目录暂存复制/同步完成后、替换前复核；发现冲突保留目标并明确失败，清理仅本次临时内容。原有body期间并发修改的测试不能替代这一发布阶段反例。

### 已通过及未验证项

| 项目 | 结论与证据 |
| --- | --- |
| 正式IO回归 | 本窗口实际5 passed，0skip，exit0，0.66s；与独立6项是分开的进程 |
| 32MiB合成NC传输 | 独立通过；强制复制缓冲不超过1MiB，禁止Path.read_bytes整文件吞入，int64大于2^53及中文属性回读一致；只证明传输层复制内存有界，不声称数值数组/NetCDF内部缓存总内存常量 |
| 读入期间源文件漂移 | 独立拒绝且保留并发字节，清理临时内容 |
| ASCII实际权限错误 | 原PermissionError对象传播，不进入Unicode回退 |
| 原始底层探针重复 | 原脚本逐字节复制到新目录运行exit0；ASCII/空格ASCII仍通过，中文直接C库路径仍Errno13，内存字节往返通过；该原生库限制由新适配层处理，不是探针全通过 |
| 已生成完整产物只读复验 | producer_output_independent_evidence.json：严格native/source reader及两类SHP验证通过；4面、3时刻、活动6m²/排除2m²、体积0.9/0.90003/0.90006m³、接受雨量6e-5m³/排除未模拟2e-5m³，FRAME几何对应实际计算三节点；报告10阶段complete；47文件前后字节不变 |
| 四个上游改动范围 | 实际diff仅新增适配导入与Dataset调用替换；未修改数组、掩膜、CRS、源图检查或数值方程 |
| 新鲜完整BAT、物理中文项目根目录、完整必要旧回归 | 未验证；因已发现确定发布保护阻断而暂缓，不把已有产物复验当成新BAT执行 |
| 隔离候选/wheel/提交推送 | 未执行，禁止以本批不通过产物发布 |

当前不能按“已验收入口”通知用户手动运行。修复并复验通过后，预期手动方式是在原项目D:/devTools/project/2d-hydrodynamics运行run_qingbei_nc.bat，默认合成，输出work/results/<script_id>；--engineering只预检且不能执行真实工程。此处为待验收使用合同，不是当前完成声明。

## 通知失效与交接机制

已由协调确认：实现完成通知exec-e4072fc2-e81d-42cf-a8ed-c121a753f6d8实际status=failed、durationMs=0，目标本窗口，发送方报告自动审核未识别人类授权。消息未送达，上一轮空闲等待因而没有新事件；不能说实现未完成或通知已经成功。未重试或绕过。当前由主协调正式下达任务恢复验收，后续不依赖未验证跨聊天回传保证空闲唤醒，不承诺永久自动续接，不设定时任务/周期轮询。本轮由主协调一次有界事件等待读取final。

最小后续依赖建议：先关闭本记录A/B并原样复验，再完成fresh BAT/中文项目根/必要旧回归/候选wheel，全部通过才选择性发布和用户手动交付；然后按已批准范围审查T-003开放边界准备合同。T-002工程来源清单可核对缺项，但真实数据读取、开放边界数值、堤闸/径流等扩大物理仍不在本批，不自行启动。


## R2最终独立验收通过

新实施turn01a158e4-d87e-7672-856c-f4b9c40d737a已冻结；其后01a15934仅确认保持冻结，不是新代码实施。上方失败结论为历史，以下最终门禁取代当前不通过状态。

- 本窗口原样6独立＋8正式IO同次14 passed（1.08s）；原六项SHA仍为c3f62e7d2b35fdbf6a2da9da428c08caadd992297da54b9ae10700aa6ced9eeb。
- 额外4项mutex独立通过（3.65s）：Windows真实跨进程互斥/异常释放、失败获取句柄清理、两份适配器快照拒绝旧写入。
- 原项目21项独立BAT/NC/SHP/CLI/路径/工程阻断通过（129.06s），包括新鲜真实BAT、默认ID/目录、中文与空格输出、脚本路径、原生面几何/雨量和旧目录保护。
- 基于HEAD8504b6f的26项最小白名单候选位于真实物理中文及空格目录candidate 中文 项目，46 passed（183.73s），33个模块均来自候选；没有借用其他WIP或live源码。必要旧兼容45 passed（70.42s），34模块来自同一候选，覆盖UGRID整数/掩膜/单位/元数据、native/source读取与原子保护、安全区及源项SHP。以上进程均正常exit0、0skip。Windows专属独立测试位于tests/acceptance/native_batch，非Windows不收集，不能将本次Windows实测说成其他平台验收。
- 最终离线wheel SHA256=dc2641dd4aff1581a8eb9161a4529b78c79032a319f364d0990fc61925fca015。49个包文件逐字节等于候选及安装目录；隔离安装30模块均来自installed。schema/新CLI入口、中文NC大int64往返、工程只预检实际通过。安装入口仍要求checkout示例树及项目venv，不声称独立wheel携带工程数据。

no-clobber使用目标层Windows原子不替换rename（非Windows分支未在本机另验）；r+最终副本完成后在同目标mutex内复核身份并替换。实际唯一r+调用位于triangular_source_netcdf.write_triangular_source_result_atomic：mkstemp分配、关闭句柄、只由当前runner修改并严格回读，再最终发布。不是把任意公共目标仅凭文档认作独占。mutex只覆盖同Windows会话/同规范路径键的配合发布者；不承诺任意外部写入者或路径别名的文件系统CAS，其他r+调用者仍须独占。

复制缓冲1MiB，32MiB分块测试与整文件read_bytes禁用探针已通过；临时磁盘占用与文件体积同阶，数值数组及NetCDF库内部缓存不在此复制层常量内存声明内。无真实DEM、工程长算例、DB/HTTP/GUI或新物理。原100001及旧合成产物摘要保持不变。

验收启动器问题如实保留：候选首轮45pass/1fail源于独立bootstrap缺少__main__保护，spawn子进程重复运行pytest；加保护后用全新basetemp完整46项通过，未改生产或原断言。准备候选时mklink的OEM输出导致控制台解码告警，已核实链接与26项文件hash，未计作产品测试。另一个临时P2探针错误使用不存在的overwrite参数，在调用前TypeError（1failed），经实际签名核对排除为无效探针，不是产品缺陷或有效通过；未触发写入，未纳入永久测试或发布白名单。

实际机器证据为work/acceptance_native_launcher_20261020/r2_acceptance_evidence.json、candidate_module_provenance.json、legacy_module_provenance.json及wheel_evidence.json。只提交26项相关代码/纯合成与工程预检模板/schema/说明/永久回归；README和pyproject基于HEAD作最小追加，其他WIP/地图删除、任务报告、生成NC/SHP、临时脚本均排除。实际Git提交与推送凭据另记录到本地跟踪报告和publication_evidence.json。

最终可手动方式（原项目根目录）：.\run_qingbei_nc.bat。前置为现有.venv/Scripts/python.exe及.[triangular,shp]依赖；未满足只提示安装、不自动下载。默认4面/3时刻/0.1s SYNTHETIC_ONLY，输出work/results/<新script_id>；可显式--id或--script-id及--out-dir，已有目标拒绝。--engineering只列blocker并非零退出，不能执行真实工程。下一优先为已批准T-003边界准备合同独立审查；工程输入证据T-002只核清单，真实数据/开放边界RHS等仍须另定范围。
