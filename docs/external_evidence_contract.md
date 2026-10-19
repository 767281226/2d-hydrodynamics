# 外部证据输入契约 V1.0

本轮定义解除真实案例 readiness block 所需的外部证据结构，不执行任何数值转换、调度动作或数据库写入。

证据类型包括：source vertical datum attestation、dispatch 人工审批、levee crest elevation、工程基准算例。每项必须保留来源和确认信息；缺失或冲突时返回 block/fail，不能通过固定偏移或 DEM 数值猜测补齐。

只有 source datum 与目标 1985 国家高程基准一致且证据完整，absolute level 才可报告 verified。dispatch 必须有审核人、时间、摘要版本/哈希和证据引用；levee crest 必须有高程和垂直基准；benchmark 必须有变量、时间窗、空间范围、误差指标、容差、结果和审核状态。
第40轮增加 JSON/YAML 外部证据文件导入；示例证据不解除真实 readiness block。

## 第53轮用户确认材料

本轮可通过 build_confirmed_datum_conversion_evidence()、build_confirmed_socio_economic_evidence()
和 build_confirmed_dispatch_event_evidence() 生成只读证据对象，也可通过
load_external_evidence() 导入 JSON/YAML。用户确认的社会经济字段为：

- s_pop：格内常住人口，七普，单位 人/格；
- s_ar_mu：格内耕地面积，三调，单位 亩/格；
- s_ar_km2：格内耕地面积，单位 km²/格，关系为 s_ar_mu / 1500。

提供的 s_pop=(1.3335, 5.2848)、s_ar_mu=(1.4292, 0.558) 按原顺序保存，
仍标记为“范围/样例含义未明确”，不排序、不生成逐格风险值。行政汇总来源只作为
公式和 provenance 说明，不能直接当作逐格数据。

大沽高程到1985国家高程基准的关系仅在“下游东淀-文安洼区域”作用域内记录为
target = source - 1.56 m，applied=false。它不是全局偏移，也不会改写 DEM。
2023-08-01 02:00 东淀北部启用事件保持 narrative_only，并强制人工复核和年度
调度方案依赖；它不是水位/流量时序，也不能自动驱动 Solver。
