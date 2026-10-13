# 最终结果调闸记录

`write_hourly_netcdf()` 和 `NetCDFWriter.write()` 在结果写出成功后，同目录输出
`<结果文件名>_gate_dispatch.csv` 和 `<结果文件名>_gate_dispatch.json`。
CSV 使用 UTF-8 BOM，可在 Excel 中打开。NetCDF 全局属性保存两个文件名及调用方报告的调闸动作数。
返回值仍为 NetCDF 路径。`overwrite` 同时保护结果和两个记录文件；有任一记录文件冲突时，写出前报错。

记录依据 `docs/dispatchRule.docx` 整理的 13 条规则，并保存文档 SHA256、规则编号和
OOXML 段落索引。`4.2m` 警戒水位和 `5.75m` 保证水位使用原文附图的“大沽高程”；
防潮闸 `3.0/3.8m` 使用原文“大沽冻结”。清南分洪规则 `7.0m` 的基准需调用方确认。

## 输入契约

调度数据为独立的 `GateDispatchInput`，用于生成输出记录。它不修改计算边界、总体流量或闸门状态。
小时结果通过 `gate_dispatch=` 传入，通用结果通过 `SimulationOutput.gate_dispatch` 传入。
字段和嵌套结构都拒绝未知字段。数值拒绝布尔值、字符串和 NaN/Inf，错误注明字段路径。

| 字段 | 类型和单位 | 默认或要求 | 范围和校验 |
|---|---|---|---|
| `schema_version` | 字符串 | `1.0` | 仅支持 `1.0` |
| `time_origin` | ISO8601 字符串 | `null` | 有值时必须含时区，表示 `time_s=0` 的时刻；零起点结果可默认使用 `event_start` 元数据 |
| `provenance` | 字符串 | 未提供调度输入的说明 | 非空；假设/重建材料须如实标明 |
| `snapshots` | 调度快照列表 | 空列表 | `time_s` 唯一且严格递增；必须与输出时刻精确匹配，缺点不插值、不沿用旧点 |
| `operations` | 调闸记录列表 | 空列表 | 时间非降序；同一闸门同一时刻不能重复；须在输出窗口内 |
| `options.near_5_75_tolerance_m` | 数值，m | `null` | 有限且 ≥0；“接近”解释为 `abs(H-5.75)≤容差`，未提供时相关规则无法判断 |
| `options.qingnan_threshold_datum` | 字符串 | `null` | 有值时非空；确认两个 `7.0m` 阈值使用的基准 |

快照的 `time_s` 必填，单位 s，有限且 ≥0；`source` 必填且非空。
以下水位字段默认 `null`，有值时使用 `GateWaterLevel(value_m, vertical_datum, unit='m')`：
`value_m` 为有限数，允许负高程；`vertical_datum` 必填且非空，`unit` 仅支持 `m`。

| 水位字段 | 对象 |
|---|---|
| `sixth_port_level` | 大清河第六埠河道站 |
| `dongdian_level` / `daqing_river_level` | 东淀与大清河的同步水位，用于退洪闸水位差判断 |
| `qingbei_level` | 东淀大清河以北区域水位 |
| `dongdian_sixth_port_level` | 东淀内第六埠水位；与河道站区分 |
| `tidal_gate_level` | 独流减河防潮闸闸上水位 |

`zaolinzhuang_discharge_m3s` 和 `zhongting_discharge_m3s` 默认 `null`，单位 m3/s，
有值时有限且 ≥0。中亭河 `<400m3/s` 是河道下泄背景条件，不能单独表示西河闸已开闸。

`sixth_port_forecast_rising`、`xihe_diversion_suitable`、`qingbei_rising`、
`qingbei_fully_used`、`dongdian_sixth_port_rising`、`main_flood_season`、
`tidal_release_window` 和 `tidal_release_authorized` 均为严格布尔值或 `null`，默认 `null`。
它们分别记录预报上涨、排沥/水质分泄判断、持续上涨、清北充分运用、淀内站持续上涨、
主汛期、赶潮窗口和防潮闸运用安排。当前窗口或季节不能由日期猜测，持续上涨/预报上涨也不能
用一次水位增加替代。水位基准不一致时记录缺项，不执行高程转换。

`GateOperation` 必填 `time_s`、`gate_id`、`action`、`reason`、`source`。
`action/reason/source` 为非空字符串，时间单位 s、有限且 ≥0。
`rule_id` 和 `approval_ref` 默认 `null`；有值时非空，规则编号须属于同一闸门。
`opening_before_ratio/opening_after_ratio` 默认 `null`，无量纲，有值时有限且在 [0,1]；
`discharge_before_m3s/discharge_after_m3s` 默认 `null`，单位 m3/s，有值时有限且 ≥0。
未提供的开度、泄量保持空值。“充分泄水”没有被转换为开度 1 或固定流量。

闸门 ID：`duliu_inlet`、`xihe`、`renzhuangzi`、`laolongwan`、`sixth_port_return`、
`shuigaozhuang`、`yangliuqing`、`qingnan`、`duliu_tidal`。
任庄子是原文口门，作为调度构筑物记录。

## 输出记录含义

每条记录包含相对秒数/可用的绝对时刻、闸名、规则编号、动作、原因、开度/流量前后值、
操作来源、审批引用、判断证据和源文档。规则状态为 `triggered`、`not_triggered`、`indeterminate`：
已知任一必要条件为假时不触发，全部条件为真时触发，否则无法判断。

`record_type=trigger` 表示规则条件满足、等待调度决定；`operation` 仅表示转录了调用方提供的
调闸动作及其来源。两者分别统计，规则触发不会凭空增加调闸动作数。
初始状态和状态变化写入记录，持续满足同一规则不会每小时重复记一次。
`condition_cleared` 表示条件解除，原文没有给出关闸规则，因此它不生成关闸动作。
`unavailable` 说明缺少数据/条件或水位基准不匹配。
JSON 保存最终规则状态和每条规则无法判断的样本数，以区分“未触发”和“无法判断”。

模型淀区 `eta`、总体 `outflow`、二维单元流量不能直接作为第六埠站点水位或某一闸门的开度/泄量。
默认没有调度快照时仍输出记录并标注缺项，报告的调闸动作数为 0。

## 使用方式

```python
from pathlib import Path
from hydrodynamics import GateDispatchInput, NetCDFWriter

dispatch = GateDispatchInput.model_validate_json(Path('dispatch.json').read_text(encoding='utf-8'))
output.gate_dispatch = dispatch
NetCDFWriter.write(output, 'results/result.nc')
# 小时计算：write_hourly_netcdf(..., gate_dispatch=dispatch)
```

为已有结果补充记录（只读时间和元数据，不重算水动力场）：

```powershell
python examples/export_gate_dispatch_log.py --result work/results/result.nc --input dispatch.json
```

省略 `--input` 会输出判断缺项。已存在记录时显式加 `--overwrite`。
`examples/gate_dispatch.example.json` 是清楚标注的假设数据，展示触发记录与转录调闸动作，
不可作为 2023 年实际操作日志。
