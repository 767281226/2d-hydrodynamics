# 输出结果数据契约 V1.0

执行 `python -m hydrodynamics` 会运行统一计算流程，按阶段自动生成 NC、调闸记录、SHP、nc2web、
stepjson 和 viewer，并检查输出完整性。每次结果位于 `work/results/<id>/`，只有全部必需阶段完成才
将 `run_report.json.status` 写为 `complete`。输入和命令见 [workflow.md](workflow.md)。

NetCDF 是最终模型的主要输出格式，契约目标为完整时序二维场 `(time, y, x)`，并携带网格坐标、CRS、垂直基准、变量单位、质量掩膜和全局元数据。本轮已实现 `NetCDFWriter.write` 实际写出；GeoTIFF/CSV writer、Solver 和物理计算仍未实现。
小时步长蓄滞洪回放由 `HourlyInundationSolver` / `write_hourly_netcdf` 写出完整 `(time,y,x)` 水深、水位、流速和流量，以及二维最大水深/最大流速/到达时间。1 h 步长是 DEM 静水填容，不是 CFL 限制的 HLL 时间步。

最终 NetCDF 写出同时生成同目录 `<文件名>_gate_dispatch.csv/.json` 调闸记录。
`write_hourly_netcdf(..., gate_dispatch=...)` 和 `SimulationOutput.gate_dispatch` 接收独立的
`GateDispatchInput`，记录规则触发、条件解除、判断缺项和调用方提供的实际调闸动作及来源。
时间戳、站点/基准、开度/流量前后值和来源随记录保存；详细契约见 [gate_dispatch_log.md](gate_dispatch_log.md)。
调闸 JSON V1.1 另保留完整快照/逐条件证据及来源分类；`reported_operation_count` 是兼容的输入记录行数，
模型预设流量变化单独分类，不等同实际闸门动作。CSV 与 JSON 含可读的成对一致性清单。

`OutputField` 校验变量单位、二维/时序 shape、有限值、时间戳和掩膜；`water_level` 必须提供垂直基准。`SimulationOutput` 校验格式和变量唯一性。


第14轮新增 NetCDFWriter.write：统一写出 (time,y,x)、cell-centered 坐标、units、CRS、垂直基准和掩膜；netCDF4 为可选 netcdf extra。writer 不代表 Solver 或物理计算已完成。
第14轮已实现 NetCDFWriter.write 的实际写出；GeoTIFF/CSV writer 和 Solver/物理计算仍未实现。多字段写出要求网格元数据和 CRS 一致，不一致时拒绝混写。
terrain.nc 是准备阶段地形 artifact，不属于最终 Solver 结果；最终结果 NetCDF 仍使用 time,y,x。
第30轮增加 NetCDFReader 解析契约和数据库无关 cell 记录；PG/PostGIS sink 由使用方注入。
NetCDF reader 与数据库入库适配器保持 time/y/x、mask、CRS 和 datum 元数据，不把中心坐标当 polygon 边界。

## 大规模运行输出 profile（opt-in）

全量原生三角运行（约 15–19 万面、289 帧）的流式 FrameSink、50 m 派生兼容格、逐帧 native_web 2.0、SHP 四包与 forcing 标签见 `docs/large_run_output_profile.md`。M1 小样本合同不变。
