# 调度规则结构化契约

`docs/dispatchRule.docx` 为调闸记录的规则来源，原文保持不变。
`hydrodynamics.gate_dispatch.GATE_RULES` 从原文段落及随附水位表整理了 13 条闸门/口门规则，
保留规则编号、构筑物、触发条件、原文动作和 OOXML 段落索引，并保存来源文档 SHA256。

独立 `GateDispatchInput` / `build_gate_dispatch_log` / `write_gate_dispatch_log` 记录条件触发、
解除和判断缺项，并转录调用方提供的调闸动作及来源。小时和通用 NetCDF 导出均写出配套 CSV/JSON。
`相机`、`适时`、`接近` 等判断使用明确输入，不从文字生成闸门开度、泄量或计算边界控制。
详细输入和输出契约见 [gate_dispatch_log.md](gate_dispatch_log.md)。
