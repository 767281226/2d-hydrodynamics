# P4-3A 三角网格开放边界契约 ADR

批次：`P4-3A-BOUNDARY-CONTRACT-20261018`
状态：**准备契约与解析参考已实现，等待主聊天审查；未接入 RHS 或时间推进。**
决策范围：保留 P3-A 源数据语义，为后续开放边界定义严格、可复核的准备层；不声明任何开放边界已在求解器中验证。

文档关系：本文件是 **P4-3A 唯一权威 ADR**。英文对应稿 [p4_3a_triangular_boundary_contract_adr_en.md](p4_3a_triangular_boundary_contract_adr_en.md) 仅作阅读译文，所有决策以本文件为准；两份稿件均处于待主聊天审查的准备/候选状态。

## 决策摘要

P3-A 的边界对象仍是原始请求：`discharge` 是非负总组流量 `Q`（m³/s），没有方向；`water_level` 是绝对水位（m），其垂直基准必须与 P2 地形中已知的基准完全一致。新的 `triangular-boundary-preparation-v1` 逐组要求显式映射、固定方向（仅 discharge）、按边长均匀分配策略（仅 discharge）以及时序的分段常值策略。未绑定外边按显式 `closed_wall` 记录。

准备层只计算基于 P2 几何的外法向、边长权重和请求分配，并保留原始断点、匹配后的输出时刻以及解析请求积分。方向约定为**外向为正**：inflow 请求写为负，outflow 请求写为正。输出固定为 `preparation_only=true`、`simulation_result=false`、`solver_completed=false`、`execution_available=false`、`boundary_applied=false`。准备值不进入 HLL、不会改变守恒状态，也不是求解器实际接受的边界质量通量。

后续 P4-3B 在新增“完整外边状态闭合”契约及测试前，不得把 P3-A 单独的 Q 或水位请求直接接到 HLL。建议首批执行范围限于充分定义且全湿的固定方向边界；干湿切换、回流、超临界入流和低于床面的水位一律明确拒绝，除非后续阶段为这些分支选定并验证独立物理规则。

## 原输入和符号

P2 的每条边 `e` 由实际节点、相邻面、外部边界身份给出。对真实外边，以边中点 `m_e`、唯一相邻面形心 `c_e` 和任一端点顺序下的单位垂线候选 `n_e*` 计算方向：若 `n_e* · (m_e-c_e) < 0`，则取反。这样法向由 P2 几何和相邻面决定，不依赖节点对的存储顺序。该检查只对 `edge_faces` 中恰有一个有效面且另一个为边界标记 `-1` 的边执行。

对于组总长 `L = Σ_e L_e` 和非负请求 `Q(t)`，本批冻结的**候选准备分配**是：

```text
w_e = L_e / L
Q_e(t) = s · Q(t) · w_e
q_n,e(t) = Q_e(t) / L_e = s · Q(t) / L
s = -1 for direction=inflow; +1 for direction=outflow
```

`Q_e` 是带外向符号的组请求份额，单位 m³/s；`q_n,e` 是请求法向单位宽流量，单位 m²/s。测试只检查几何长度权重及这些请求量的代数关系，不检查或假设 `HLL` 数值通量。

## 证据与项目选择

### 来源直接支持的事实

* USACE HEC-RAS 2D 手册将 Flow Hydrograph 和 Stage Hydrograph 列为不同边界类型。其 Flow Hydrograph 用正值表示流入、负值表示流出；Stage Hydrograph 可按边界单元水面高低产生进/出流，且干单元的边界水位低于最低地面时可能没有水量交换。手册也明确禁止把两类不同外边界条件挂到同一 cell face。[HEC-RAS 2D External Boundary Conditions](https://www.hec.usace.army.mil/confluence/rasdocs/r2dum/latest/boundary-and-initial-conditions-for-2d-flow-areas/external-boundary-conditions)
* Deltares D-Flow FM 技术手册把 water level、normal velocity、组积分 discharge、Riemann/outflow 等作为不同边界条件，并将 tangential velocity 列为可与前述条件组合的补充条件。手册写出 discharge 的积分定义 `∫ h u·n dΓ = Q_b`，并单独给出 `u·t = v_b`。这些公式采用其自身的内向法向约定；本项目外向为正是项目选择，不能从其符号照搬。[D-Flow FM Technical Reference Manual, §6.4](https://content.oss.deltares.nl/delft3dfm2d3d/D-Flow_FM_Technical_Reference_Manual.pdf)
* Antonopoulos 与 Dougalis 的浅水方程特征边界分析给出一维浅水特征速度 `u ± √(g h)` 和 Riemann 不变量形式；论文讨论的是一维、无量纲模型和特定边界假设。它能说明亚/超临界状态的入射信息数不同，但不能证明某个二维有限体积 HLL 外边界算法有效。[Notes on Galerkin-finite element methods for the Shallow Water equations with characteristic boundary conditions](https://arxiv.org/abs/1507.08209)

### 项目选择，不是外部软件的验证结论

* 不改 P3-A 中的原始数值、单位、来源、量的含义或原生边组身份。绝不从组名、当前内场、配置缺项或案例文档猜流量方向。
* 本批的外向正号、`uniform_per_unit_length`、对实际 P2 外边的映射、显式未绑定壁面、严格断点匹配与准备标记是项目输入契约。它们是可以审查和修改的项目选择，不是 HEC-RAS 或 D-Flow FM 的通量算法。
* 本批不赋予 water_level 请求速度、切向动量、流向或质量通量，不由水位请求推导干湿转换。

## 为什么 Q 和绝对水位不能单独构成 HLL 外态

在二维浅水系统中，边界边上的守恒态至少包含 `h`、`h u_n` 和 `h u_t`。绝对水位 `ζ_b` 在已知边界床面 `z_b` 时最多给出候选水深 `h_b=ζ_b-z_b`；它没有给出法向速度或切向速度。若 `h_b<=0`，还没有定义这里的湿态。组总流量只约束一条边组上的积分 `∫ h u_n dΓ`，即使采用本批固定分配得到 `q_n,e`，仍缺少用于计算 `u_n=q_n/h` 的水深和切向速度。一个标量不能自行补全 `(h, hu, hv)`。

因此，后续边界状态闭合必须选定并记录完整方法，候选有：

1. **显式完整外态**：对每边或规则化的边段给出与基准一致的绝对水位/水深、法向速度或 Q 分配，以及切向速度；用外法向 `n=(n_x,n_y)` 和切向 `t=(-n_y,n_x)` 旋回 `hu=h(u_n n_x+u_t t_x)`、`hv=h(u_n n_y+u_t t_y)`。
2. **经验证的特征/关系型闭合**：例如水位与一个入射特征量组合，并明确哪些特征从内域取值。必须区分二维、局部法向/切向、床面源项、干湿和超临界方向，不能把一维关系直接当成二维定理。

当前 P3-A 每组只存 discharge 或 water_level 一种量。未来若选择完整外态，需另行审查一个版本化补充契约，把互补状态数据和原始 P3-A 字段关联起来；不得改写历史 P3-A 包，也不得在本批悄悄复制一个缺失的水位/切向速度默认值。

## 与 HLL、静水重构和动量源项的未来接口

项目 P4-0 选择了三角有限体积 `HLL`、静水重构和 `SSPRK2`，但该路线目前只支持封闭壁边界。未来开放边必须沿同一守恒式使用边法向通量：先把内、外状态的动量转到局部法向/切向坐标；保持与内部边一致的静水重构和边床面处理；对经重构的两侧态计算一个 HLL 界面质量/动量通量；再把动量通量旋回全局坐标。开边床面压力和地形源项必须和既有 well-balanced 处理成对设计。闭壁边仍需与反射法向动量状态相容，确保零法向质量交换和一致压力项。任何仅给水位、把外态动量无条件置零、先算通量再裁切质量、或绕过静水重构的做法均不能视为此契约的实现。

准备请求 `Q_e` / `q_n,e` 与将来实际接受的 HLL 外边通量 `F^mass_e` 是两个不同量。运行结果中的边界质量账必须来自**被两个 RK 阶段实际接受的数值通量及其时间权重**；`boundary_net_volume_m3` 采用域内为正，等于 `-Σ_e∫ F^mass_e L_e dt`。报告可并列展示准备请求和实际接收量及差值，但不能把实际通量强制改写为请求 Q 来消掉差异。

## 后续执行支持表：本批准备能力与未来拒绝边界

下表中“准备层”指 P4-3A 当前实现；“P4-3B 前置规则”是建议冻结的执行入口门槛，不是已运行结论。

| 情况 | P4-3A 准备层 | P4-3B 前置规则（建议） |
| --- | --- | --- |
| 非负组 Q + 明确固定 inflow/outflow | 接受并按边长输出带符号请求分配 | 在完整状态闭合前拒绝执行；首批仅审查全湿、已声明方向且亚临界的用例 |
| `Q=0` 停流样本 | 作为零请求值接受；不多积分最终端点 | 不能凭零请求推断真实 HLL 质量通量为零；没有对应外态/停止条件时拒绝或回滚 |
| 同一组随时间反向 | 当前 P3-A 无符号 Q，准备方向逐组固定 | 不接受隐式反向；将来需显式有符号时间序列/切换规则和反向特征状态，另行版本设计 |
| 固定方向失配的回流 | 不分类或改变 P3-A 请求 | 两个 RK 阶段任一实际外向通量方向违反固定声明时拒绝该步并回滚，直到双向规则通过专门验证 |
| 水位高于床的全湿边界 | 保存绝对水位和原 datum；不推导完整外态 | 水位仍需法向/切向闭合；没有闭合方法时拒绝执行 |
| 水位低于任一实际边床面 | 保留输入数值，不自动裁零或改写 | 首批拒绝；需另行设计干态边界、湿润阈值和质量账测试 |
| 一部分湿、一部分干或边界 wet/dry 转换 | 不判断，也不改变组分配 | 首批拒绝；不允许仅按湿边重分配 Q 或填补未定义速度 |
| 全干出口/由干域流入 | 保留请求，不声称产生流量 | 首批拒绝；需验证干出口特征、干湿重试、非负性和守恒策略后再开放 |
| 亚临界 | 不作流态判断 | 建议首批仅限有完整外态闭合的亚临界固定方向；仍须两阶段 CFL 和通量验证 |
| 超临界入流 | 不作流态判断 | 拒绝，除非补齐所有入射特征/完整状态且有单独测试 |
| 超临界出流 | 不作流态判断 | 首批拒绝；之后也只能用显式论证的纯外推/出流闭合，不复制未经验证的成熟软件选项 |

一维参考论文的 `u±√(g h)` 特征速度和特征数量依赖浅水方程方向及流态；因此上述二维首批范围是项目的防护建议，不是把该论文外推成二维算法证明。HEC-RAS 的干单元 stage 规则以及 D-Flow FM 手册中的 discharge depth 覆写/干湿逻辑是其各自的模型实现，项目不把它们照搬为默认。

## 时间离散、稳定性与重试账本

本批要求所有 timeseries 断点匹配输出轴上的唯一时刻，容差沿用 `max(1e-12 s, 8 ULP)`，保留源 `raw_time_s` 和匹配 `effective_time_s`。不插值、不外推、不补断点。分段常值区间采用左闭右开；最终时刻单独存为 terminal sample，不积分额外时长。内部断点处，若一步恰在 `t_b` 结束，SSPRK2 第二阶段仍使用旧区间的左侧值，新区间从下一步开始。

未来 P4-3B 必须将实际边界 HLL 波速纳入稳定步长上界，并且在 SSPRK2 的两个阶段都检查状态与边界波速；候选单元约束为 `Δt <= C_CFL · min_K A_K / Σ_{e∈∂K}(L_e α_e)`，其中 `α_e` 至少覆盖 HLL 两侧重构状态的最大法向波速。Deltares 手册也明确将其时间步估计与实际通量/流速联系起来，但该候选公式仍需和项目的实际 HLL、三角单元面积及 SSPRK2 实现验证。边界计划断点继续与当前 `dt` 取最小，不能跨过断点。

若阶段状态、边界波速、负水深或本步质量条件失败，未来执行器应拒绝本次临时更新并用较小 `dt` 重试；任何试算阶段的源项账、boundary_net 和校正体积都必须随状态一并回滚。重试耗尽则按现有失败路径停止，不能保留一半阶段通量或通过质量校正掩盖边界错配。

## C 源项与质量修正预算

固定初始修正预算沿用 P4-2C 算出的 `V0` 和原修正尺度；不会随 P4-3A 的请求流量、准备积分或未来边界净交换而重算。未来 source-aware 边界执行配置需单独设计新版本组合：`source_net_volume_m3` 继续只代表独立源项，`boundary_net_volume_m3` 继续只代表接受的边界通量。不得把边界量加进初始 `V0`、雨量总量或 source ledger 来抵消边界误差。既有 none/Manning/C 源项运行结果不因准备文件存在而变成开放边界结果。

## 准备接口和产物

模块 `hydrodynamics.triangular_boundaries` 提供 `load_triangular_boundary_preparation_config`、`prepare_triangular_boundaries` 和 `read_triangular_boundary_preparation`。准备命令是 `hydrodynamics-triangular-boundary-prepare CONFIG.yaml`。

结果引用 config、P3-A business package、其实际 P2 UGRID、mesh/terrain fingerprint 和 SHA-256；列出全部真实 P2 外边、被请求组绑定的外边及显式闭壁的未绑定外边。读取准备 JSON 会重读所有输入并重新计算整个预期产物后逐字段比较，不以文件里自报的 hash 代替真实性校验。输出采用同目录临时文件、readback 校验、原子发布；默认不覆盖，显式覆盖仅允许替换之前的边界准备 JSON，不可覆盖配置/P2/P3-A 或既有非准备文件。

所有边界请求分组与 P3-A 组一一对应。组名未知、漏配、多配、内部约束组、P3-A 数据与 P2 不一致、datum 未知/不符、同一物理外边重复分配、时间段缺口/超出/断点不匹配及输出时间非法均拒绝。未绑定 P2 外边在 `unbound_exterior_edges` 中逐条列出并保持壁面。

## 独立合成参考与容差

机器清单：[boundary_preparation_references.json](../examples/triangular_boundaries/boundary_preparation_references.json) 标为 `pending_main_review`。其中的解析几何/请求积分参考不代表开放边界求解已验证：

* 1 m 与 3 m 边、`Q=0.01 m³/s`：权重 0.25/0.75，每边份额 0.0025/0.0075 m³/s，单位宽外向流量大小均 0.0025 m²/s；inflow/outflow 符号相反。
* `[0,10,20,30] s` 入流 `Q=[0.01,0,0.02,0.02] m³/s` 的三个积分区间为 0.1、0、0.2 m³，总入流 0.3 m³；出流 `0.005 m³/s` 的总出流 0.15 m³，净请求入流 0.15 m³。最终时刻的样本是终止样本，不再积分。
* 同一几何旋转、面次序/边节点次序重排时，长度、权重和 Q 分配不变；法向跟随刚体旋转。
* datum 一致、平床 0 m、绝对水位 1 m 的解析静水期望为 `h=1 m, q=0` 和零交换；P4-3A 只保存水位请求，并不计算/通过水动力状态。

请求组边 Q 和为 Q 的绝对误差上限 `1e-12*max(1,|Q|) m³/s`；单位法向范数误差 `1e-12`；独立分段/累计请求体积误差 `1e-12 m³`；字段变换参考等价性为 `1e-10*max(1,|reference|)`。这些是准备及解析算术门槛，P4-3B 仍需单独的实际 HLL 质量通量用例和门槛审查。

## 来源清单

1. USACE, *HEC-RAS 2D User's Manual*, External Boundary Conditions（当前在线手册页）。仅引用其区分 Flow/Stage、flow 正负号、按单元流量和 dry stage 行为，以及同一 cell face 不允许不同 boundary condition 的描述。该来源解释的是 HEC-RAS 行为，不是本项目验证。[官方文档](https://www.hec.usace.army.mil/confluence/rasdocs/r2dum/latest/boundary-and-initial-conditions-for-2d-flow-areas/external-boundary-conditions)
2. Deltares, *D-Flow Flexible Mesh Technical Reference Manual*, §6.4，尤其式 6.147–6.158。只引用 discharge 积分、waterlevel、normal/tangential 变量作为互补边界输入的区分；其正内向法向和软件算法不移植为项目约定。[官方 PDF](https://content.oss.deltares.nl/delft3dfm2d3d/D-Flow_FM_Technical_Reference_Manual.pdf)
3. Antonopoulos, D. C. & Dougalis, V. A., *Notes on Galerkin-finite element methods for the Shallow Water equations with characteristic boundary conditions* (2015), §1、§3、§4. 只作为一维特征速度/不变量及流态分支的数学说明；不是二维 FV/HLL 边界验证。[arXiv:1507.08209](https://arxiv.org/abs/1507.08209)
