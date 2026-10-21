# 三角网格边界请求准备

状态：**P4-3A 仅准备层文档，等待主聊天审查；不代表开放边界已经可执行。**

文档关系：本文件是中文技术说明；英文对应稿为 [triangular_boundary_preparation.md](triangular_boundary_preparation.md)。本批唯一权威决策记录是[中文 ADR](p4_3a_triangular_boundary_contract_adr.md)；[英文 ADR](p4_3a_triangular_boundary_contract_adr_en.md)是阅读译文。

P4-3A 将开放边界请求绑定到实际 P2 网格和 P3-A 业务包，但不会运行求解器、计算 ghost 状态、调用数值通量或改变现有封闭壁路线。物理状态闭合提案和审查状态见 [P4-3A ADR](p4_3a_triangular_boundary_contract_adr.md)。

## 输入与运行

先运行小型合成环形网格示例的 P2 网格准备和 P3-A 业务准备：

```text
hydrodynamics-mesh-prepare examples/triangular_mesh/preparation.yaml
hydrodynamics-business-prepare examples/triangular_mesh/business_preparation.yaml
```

P2 生成 `examples/triangular_mesh/prepared_mesh.nc`；随后 P3-A 将生成 `examples/triangular_mesh/business_preparation.json`，并保留真实外边、源时序、来源标签、绝对水位和声明的 datum。然后运行独立的边界准备配置 [`boundary_preparation.yaml`](../examples/triangular_boundaries/boundary_preparation.yaml)：

```text
hydrodynamics-triangular-boundary-prepare examples/triangular_boundaries/boundary_preparation.yaml
```

配置中的路径相对于该 YAML 文件。`business_file` 必须解析到有效 P3-A 包，其 `mesh_artifact` 必须解析到有效 P2 UGRID。边界配置拥有独立的输出时间轴，不依赖也不修改求解运行配置。

| 字段 | 类型和单位 | 必填/默认 | 范围或规则 | 错误处理 |
| --- | --- | --- | --- | --- |
| `format` | 字符串 | 必填固定值 | `triangular-boundary-preparation-v1` | 其他格式拒绝 |
| `schema_version` | 字符串 | 必填固定值 | `1.0` | 其他版本拒绝 |
| `case_id` | 非空字符串 | 必填 | 标识本次准备请求 | 空字符串拒绝 |
| `business_file` | 相对路径 | 必填 | 现存 P3-A 包；不能作为输出文件 | 拒绝绝对路径、缺失输入或身份不一致 |
| `output_file` | 相对路径 | 必填 | 普通 JSON 文件路径；不同于配置、P3-A 和所链接 P2 | 拒绝输入/结果冲突、符号链接、硬链接或未经授权覆盖 |
| `output_times_s` | 有限实数数组，秒 | 必填 | 2 至 100,000 个时点；首点严格为 0；严格递增；范围 `[0, 31,536,000]` | 拒绝 bool、NaN、无穷、重复或倒序时点 |
| `time_origin` | 带时区 ISO-8601 字符串或 null | 必填 | null 表示仅使用相对秒 | 不带时区的时间戳拒绝 |
| `unbound_exterior_policy` | 字符串 | 必填固定值 | `closed_wall` | 拒绝隐式自由出流或其他策略 |
| `boundary_bindings` | 边界组选择数组 | 默认空数组 | 必须对每个 P3-A 绑定组一一映射 | 拒绝缺失、重复、未知、内部或额外组 |
| `boundary_bindings[].group_name` | 非空字符串 | 必填 | 必须与现有 P3-A 组名完全一致 | 身份不匹配时拒绝 |
| `direction` | 字符串 | discharge 必填；其他量不应填写 | `inflow` 或 `outflow`；逐组固定 | 缺失/非法方向拒绝；不改写 P3-A 的 Q 符号 |
| `allocation_policy` | 字符串 | discharge 必填；其他量不应填写 | `uniform_per_unit_length` | 其他分配策略拒绝 |
| `temporal_policy` | 字符串 | timeseries 必填；constant 不应填写 | `piecewise_constant` | 插值、外推或未知时序策略拒绝 |
| `overwrite_existing_output` | 布尔值 | 默认 `false` | 若为 true，仅可替换同一 `case_id` 下结构有效的既有边界准备结果 | 拒绝替换 P2/P3/配置、格式损坏、不同 case 或无关文件 |

P3-A 的 discharge 值继续是非负总组流量，单位 m³/s。`direction=inflow` 添加外向为正约定下的 `-1` 符号；`direction=outflow` 添加 `+1`。水位继续是与已知地形 datum 完全一致的绝对米制高程。不执行基准转换、基准猜测、裁零或高程填补。

每组的边长来自 P2 几何。准备产物记录各边外法向单位向量、长度权重 `L_e/ΣL`、带符号请求份额 `Q_e` 和请求单位宽流量 `q_n,e=Q_e/L_e`。因此 inflow 的外向 `Q_e` 与 `q_n` 为负，outflow 为正。这些量是请求分配，不是已接受的 HLL 通量。

## 安全区闭壁

当链接的 P2/P3-A 包含经认证的 `excluded_closed_wall` 证据时，准备层会针对实际 P2 网格重新核验，并从安全区几何得到受保护边 ID。P3-A 包准备和 P4-3A 边界准备都会按物理边 ID 拒绝与受保护集合有交集的绑定，包括部分重叠及更名后的别名组。未绑定的受保护边继续作为闭壁，不会重新分配请求。

## 时序行为

常数值覆盖完整的 `[0,T]` 输出轴。timeseries 必须从 0 开始、在 `T` 结束，并且每个源断点都必须在共享 `max(1e-12 s, 8 ULP)` 容差内唯一匹配一个输出时点。准备产物保留每个源 `raw_time_s` 和匹配后的 `effective_time_s`，不会改写 P3-A 输入。

区间按左闭右开解释。最后一个样本作为终止样本保存，不多积分时长。不插值、不外推、不补缺口、不用零填充。未来 SSPRK2 的一步若恰在断点结束，则刚结束这一步的第二阶段仍使用旧区间的值，新区间从下一步开始。

请求积分使用匹配后的有效区间时长计算。它们只是解析请求汇总，不是未来求解结果中的 `boundary_net_volume_m3`。

## 输出校验

JSON 格式为 `triangular-boundary-preparation-result-v1`。每份产物均标为 `preparation_only=true`、`simulation_result=false`、`solver_completed=false`、`production_approved=false`、`execution_available=false` 和 `boundary_applied=false`。产物列出所有真实 P2 外边、映射的请求组和作为壁面保留的未绑定边，并保存配置、P3-A、P2 的 SHA-256，以及拓扑与地形 fingerprint。

`read_triangular_boundary_preparation(path, config_path)` 会重读链接输入，重新构造完整预期产物，然后逐字段比较。仅在文件中自报一个正确外观的 hash 不会被当作真实性证明。写出时使用临时 JSON，并在发布前 readback。未启用覆盖时以原子不覆盖重命名提交。启用覆盖时，目标必须先验证为同一 `case_id` 的边界准备结果；readback 后、原子替换前再次核对原目标文件身份和 SHA-256，并在目标路径的发布 mutex 内完成复核与替换。若开始时目标不存在，即使启用覆盖也使用不覆盖发布，保留暂存期间出现的无关文件。P2/P3-A 包、配置和仿真结果受保护。mutex 只串行化使用本模块的发布者，不是对任意外部进程的文件系统 compare-and-swap；不协作的外部写入仍可能在最后身份复核后竞态，因此其他覆盖调用者必须在发布期间独占控制目标。

## 合成示例和机器参考清单

示例配置把 P3-A `outer_boundary` discharge 时序映射为固定 inflow，使用按边长分配和分段常值时序策略。`hole_0` 映射为原有的常数绝对水位组；其 datum 仍采用 P3-A 地形/业务输入声明的基准。其余未绑定 P2 外边保持显式闭壁。此示例演示输入准备，不是可运行的开放边界案例。

机器清单 [`boundary_preparation_references.json`](../examples/triangular_boundaries/boundary_preparation_references.json) 是独立解析参考，状态为 `pending_main_review`。它记录四项准备参考：1 m/3 m 边长分配、`[0,10,20,30] s` 分段常值入/出流积分、刚体旋转和身份/次序变更下的不变性，以及平床上的静态绝对水位解析期望。每项包含输入、期望、推导或范围说明、适用容差及一手来源链接。水位参考只是静水解析期望；准备产物不计算水深，也不声称已应用零通量。这些参考不能验证 HLL 通量、边界 RHS 或工程模拟。

## 当前限制

该 API 不判断干湿状态、特征流态，不从水位计算法向速度或切向速度，也不构造 ghost 状态或 HLL 通量。它不声称请求 Q 等于未来实际边界质量交换。当前 P4-1 closed-wall 运行契约仍拒绝包含开放/组边界绑定的 P3-A 包；准备文件存在不会改变此验证器。

任何 RHS 实现前，P4-3B 必须审查并冻结完整外边状态闭合、干湿与反向规则、边界波速限制、SSPRK2 阶段行为、重试回滚以及实际边界/源项账本。
