# T004-K0-R1 独立实现验收

日期：2026-10-22。结论：**PASS_RESTRICTED_K0_IMPLEMENTATION：原F-K0-01/02关闭，允许选择性发布K0受限模块。** 只覆盖零切向、单边常量非零Q、全湿/固定同向/严格亚临界/平床/none的typed API及逐边CFL诊断；不包含RHS、时间推进、CLI成功run或结果writer，不是K1/完整D0/动态出口通过。

## 1. 冻结身份及两项关闭

| 候选 | 实际SHA-256 |
| --- | --- |
| hydrodynamics/t004_k0.py | 3980eab383e221adfd8f8b7970b33529da007263feca123ee88c5ba320123fcd |
| tests/test_t004_k0.py（冻结工作区原测试） | f2ea893661ffbb3a92fdee68489df8af7c56de0a41c5d030899d2557a3d39386 |
| docs/t004_k0_restricted_kernel_2026-10-22.md | 381ba5570e721017f256ab0b54fcb0abef4cb8b404c5cf5eb249076d11004fdb |
| examples/triangular_boundaries/t004_k0_single_edge_demo.py | aa0459704bdfc4fa874677652700377c705ae99b3cab6aee01a0317e0ae0e9a8 |

**F-K0-01关闭。** adapter与diagnose均要求owner精确投影及实际binary64投影严格非零同向；原四个零/反向例及直接替换request绕过路径均拒绝。连续trace、实际构造ghost和最终HLL通量分别检查方向；等态恒等式与普通端点异号证明没有混用。

**F-K0-02关闭。** CFL每条绑定边须携带原typed request，验证当前package/binding对象并重新读取规范字段和派生q。独立验证不仅测试对象is：同对象Q数组、来源、terrain样本、initial数组、owner速度、request cap变化均拒绝；fresh Q=0.3 request不能合法化旧Q=0.2诊断，而fresh Q=0.3诊断正例通过。阶段、方向、切向、不同边和ghost/flux篡改拒绝；当前输入身份、实际ghost和通量消费者fingerprint一致。

原9b037825…候选的摘要/manifest/失败JSON反例保留；本轮另保存R1四源码字节快照。现有证据不被重新标为历史通过。

## 2. 实际验证与范围

- 冻结候选的K0/R4定向测试：**24 pass、0 failure/error/skip、exit0**；示例exit0。
- 本窗口独立负例26项：原四流态在两个入口、两种stale-Q路径、same-object字段变动、阶段/边/request替换、ghost/flux篡改、精确与binary64投影互相失配等；合法原Q和fresh Q均通过。
- 独立几何脚本采用原验收窗口保存的源码副本重新运行，不把实现者复制probe当独立验收。180位非轴向HLL的x/y动量分量、四面真实邻居域CFL安全界通过；未推进状态。
- R0—R4/accepted子树只核身份，未重生成或重算已过参考；原严格整数/资源门槛、Q长度预算、安全区真实边/别名和未绑定wall行为保持。
- 183个保护文件在验收与发布准备期间未漂移。stage0深度仍与linked initial位相等；这是K0当前限制，K1必须用新阶段上下文合同处理，不能修改initial来绕过。

本轮本地证据位于work/acceptance_t004_k0_r1_independent_20261022，包括snapshot_before、independent_probe_evidence、geometry_probe_evidence、原始命令/日志、pytest.xml和安装/发布回执。报告只陈述有限实现行为，不以测试计数推断水动力精度。

## 3. 独立发布与安装

原工作区测试读取未提交的整份R2/R3/R4候选JSON，不能原样发布形成缺失依赖。发布版将其必需的六个已验收标量根输入/括区/表示误差上界提取为**tests/fixtures/t004_k0_root_references.json**，带来源SHA与作用域。发布测试只改该读取部分，所有行为断言不变；它是自包含静态测试参考，不包含未通过的整份数值政策或运行产物。冻结工作区原测试保持原字节并存快照。

在`git archive`的已发布父提交上只叠加本批文件，构建wheel；所有45个wheel Python模块均与隔离树对应源一致。首次核验器使用的build模块不可用已保留原日志，改用本地pip wheel/no-build-isolation构建成功，未改候选。安装后在外部cwd以isolated Python运行：**14项自包含K0测试通过、exit0**，诊断示例通过；29个已导入hydrodynamics模块均来自安装目标，内核SHA仍与冻结3980…一致。wheel SHA为15ef8ac730c5d2cbcdfd4e2a02670704e2bcf249886cf70e6303b247dcee945a。

发布白名单仅六项：K0模块、API文档、示例、发布适配后的K0测试、六元组静态fixture、本验收记录。任务报告、work证据、整份未发布R2/R3/R4候选、pyproject及其他WIP/原始数据/地图删除均不入提交；普通推送，无强推。实际提交/推送/远端SHA由本轮publication_evidence记录，不能由本页文字代替真实回执。

## 4. 逐项结论

原36项中C0—C3、C4-01/02/03/04、C5—C6、C7-01/03/04/06/07/08在上述K0作用域通过；原F-K0-01/02及严格类型P2均关闭。C4-05生产RK事务回滚、C7-05全场网格/时间收敛留K1实现后验，C7-02非线性动态出口仍未验证且阻断。有限参考/设计通过不扩展为未实现的场推进能力。

## 5. 下一步

建议协调安排K1-D0：先冻结命名R0/R1前出口合成的参数、独立机器参考、容差、当前阶段语义、组Q分配/两阶段CFL/回滚/账本及输出合同，再派K1推进实现。K1应在其实现后验证守恒、场误差和收敛，不要求这些实测先于实现；本轮没有实现K1或授权动态出口。T-008终止、最终统一交付不变，无真实DEM/DB/工程或历史恢复。
