# 输出结果数据契约

`OutputField` 保存二维或 `(time,y,x)` 时序场，校验单位、网格形状、有限值、递增时间和质量掩膜。
`water_level` 必须提供垂直基准。`SimulationOutput` 校验格式和变量唯一性。
`NetCDFWriter.write` 写出时序场、网格坐标、CRS、垂直基准和质量掩膜，netCDF4 为可选输出依赖。

小时步长蓄滞洪回放由 `HourlyInundationSolver` / `write_hourly_netcdf` 写出水深、水位、
流速和流量时序，以及最大水深、最大流速和到达时间。小时步长使用 DEM 静水填容，
时间推进方式的已有实现与输出记录模块独立。

最终 NetCDF 写出同时生成同目录 `<文件名>_gate_dispatch.csv/.json` 调闸记录。
`write_hourly_netcdf(..., gate_dispatch=...)` 和 `SimulationOutput.gate_dispatch` 接收独立的
`GateDispatchInput`，记录规则触发、条件解除、判断缺项和调用方提供的实际调闸动作及来源。
时间戳、站点/基准、开度/流量前后值和来源随记录保存；详细契约见 [gate_dispatch_log.md](gate_dispatch_log.md)。

统一入口 `python -m hydrodynamics` 自动执行计算和全部后处理，见 [workflow.md](workflow.md)。
