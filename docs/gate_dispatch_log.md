# 最终结果调闸记录

`write_hourly_netcdf()` 和 `NetCDFWriter.write()` 在结果写出成功后，同目录输出
`<结果文件名>_gate_dispatch.csv` 和 `<结果文件名>_gate_dispatch.json`。
CSV 使用 UTF-8 BOM，可在 Excel 中打开。V1.1 保留 V1.0 的列名、顺序和 CSV/JSON 文件名，并在末尾追加列；JSON 显式说明兼容版本。NetCDF 全局属性保存两个文件名及兼容语义的操作记录总数。
返回值仍为 NetCDF 路径。`overwrite` 同时保护结果和两个记录文件；有任一记录文件冲突时，写出前报错。

记录依据 `docs/dispatchRule.docx` 整理的 13 条规则，并保存文档 SHA256、规则编号和
OOXML 段落索引。`4.2m` 警戒水位和 `5.75m` 保证水位使用原文附图的“大沽高程”；
防潮闸 `3.0/3.8m` 使用原文“大沽冻结”。清南分洪规则 `7.0m` 的基准需调用方确认。

水尺附图的 `1.5m` 和 `1.668m` 关系只作为附图注记保存，不做统一换算；已有
`大沽高程 → 1985国家高程基准` 的 `target = source - 1.56 m` 用户确认关系
保留“下游东淀-文安洼”作用域，`applied=false`，不自动改变任何规则阈值。

## 输入契约

调度数据为独立的 `GateDispatchInput`，用于生成输出记录。它不修改计算边界、总体流量或闸门状态。
小时结果通过 `gate_dispatch=` 传入，通用结果通过 `SimulationOutput.gate_dispatch` 传入。
字段和嵌套结构都拒绝未知字段。数值拒绝布尔值、字符串和 NaN/Inf，错误注明字段路径。

| 字段 | 类型和单位 | 默认或要求 | 范围和校验 |
|---|---|---|---|
| `schema_version` | 字符串 | `1.0` | 仅支持 `1.0` |
| `time_origin` | ISO8601 字符串 | `null` | 有值时必须含时区，表示 `time_s=0` 的时刻；结果有 `event_start` 时可继承该时原点，不猜当前时间 |
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

`main_channel_discharge_m3s`、`zaolinzhuang_discharge_m3s` 和 `zhongting_discharge_m3s` 默认 `null`，单位 m3/s，
有值时有限且 ≥0。主槽 `850m3/s` 与中亭河 `<400m3/s` 都是源文档背景条件，不是闸门目标或独立开闸阈值。

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

## V1.1 来源、证据和决策阶段

`source` 继续保存调用方原始说明；新增的 `source_category` 为严格枚举：
`model_prescribed_change`、`reconstruction`、`external_report`、`undeclared`。
旧输入缺少分类时默认为 `undeclared`，不会从自由文本推断真实来源。
workflow 生成的西河预设流量变化显式标记为 `model_prescribed_change`。
JSON 分别统计四类快照和操作。旧 `reported_operation_count` 仍表示输入操作记录行数，
并非实际闸门动作数；`asserted_external_action_count` 单独表示外部来源报告的操作数，
仍不代表独立核验后的事实。

`options.snapshot_time_mode` 默认 `output_aligned`，快照必须与输出帧精确相等；
不插值、不用最近帧，也不沿用旧观测。显式 `independent_observations` 模式允许快照在
输出窗口内使用自己的时刻，JSON 保留其原始 `time_s` 并只在该时刻评估。没有独立快照时
不会制造观测时间点。`indeterminate_sample_counts` 在独立模式统计观测快照数而非 NC 帧数。
两个模式下的操作时间都保留原值。

JSON V1.1 保存完整快照和每条规则的逐条件评估。`snapshot_id` 根据完整快照内容和时刻生成，
`evaluation_id` 关联快照与规则；事件表继续按状态、缺项和动作阶段去重，重复状态的新观测
仍在 `evaluations` 数组保留。CSV/JSON 事件继续保留兼容的 `event_id`，并增加稳定 `event_key`、
`snapshot_id`、`evaluation_id`、比较值、运算符、阈值单位/基准及源段落 1-based 编号。

`water_condition_status` 与 `action_stage` 分离。防潮闸水文条件满足但未提供批准时显示
`request_arrangement`；明确未批准显示 `awaiting_arrangement`；已安排但缺赶潮窗口显示
`awaiting_tidal_window`。若批准布尔为真但无 `tidal_release_authorization_reference`，显示
`awaiting_authorization_evidence`；潮窗布尔为真但无 `tidal_release_window_reference` 时显示
`awaiting_tidal_window_evidence`。证据齐全时仍只显示 `ready_for_manual_decision`，不表示已执行。
GDR-004 条件满足只显示 `request_external_authority`。只有调用方另行提供 `operation` 才会产生
转录记录；日志不发送上报或闸控动作。

规则评估保留原段落 0-based 字段，并增加 1-based 来源定位、文档 SHA-256、原文阈值、实际比较值、
比较符、单位和垂直基准。`4.2` 是 `>=`、`700` 的口门分支是严格 `>`、`700` 本身仍无定义；
`5.75` 的容差记录为调用方解释和来源，不冒充原文算法；中亭河 `400 m3/s` 保持背景条件。
第 4 段 `850 m3/s` 另保存为主槽背景值，`comparison_threshold=null`，不作为闸门目标或触发条件。
东淀/大清河退洪闸只要求两观测同一声明基准。附图两个换算注记与既有 `-1.56 m`
限定作用域关系分别记录，均 `applied=false`。基准不一致时保持 `indeterminate`。

`rule_coverage` 列出 13 条自动评估规则及段落 5、6、13-14、18-20、23-24、28 的人工/背景覆盖项；
段落 20 明确不推断破堤。它不是全文完整规则化声明。

时间原点统一比较成带时区瞬间：等价 UTC/本地时区写法可共存，不同瞬间在 NetCDF/sidecar 写出前拒绝。
原点缺失时只输出相对秒数，绝不取当前时间填充。

CSV 使用固定 `CSV_COLUMNS`，旧列在前、新列追加。写入时先序列化两份内容并写同目录临时文件，
替换中失败会回滚可恢复的旧文件。JSON 清单包含 CSV SHA-256、行数、列定义和 `consistency_id`；
普通两文件替换仍不是跨文件原子事务。`validate_gate_dispatch_exports(result_path)` 会重新读取两份文件
并检查清单、行数、事件 ID 和嵌套证据对应关系。

### V1.1 新增输入字段

| 字段 | 类型/单位 | 默认或要求 | 校验与含义 |
|---|---|---|---|
| `options.snapshot_time_mode` | enum | `output_aligned` | `output_aligned` 或 `independent_observations`；独立模式的时刻仍须在结果窗口内 |
| `options.near_5_75_tolerance_source` | 非空字符串 | `caller_supplied` | 说明“接近”容差的解释来源；该容差不是原文公式 |
| `options.qingnan_threshold_datum_source` | 非空字符串 | `caller_supplied` | 保存清南 7.0 m 阈值基准确认来源；不自动换算 |
| 快照 `source_category` | enum | `undeclared` | `model_prescribed_change`、`reconstruction`、`external_report` 或 `undeclared` |
| 操作 `source_category` | enum | `undeclared` | 同上；旧输入自由文本不能推断为外部实测或实际调闸 |
| `main_channel_discharge_m3s` | 有限数值，m3/s 或 null | default null | ≥0；850 m3/s 是段落背景，作为证据保存但不参与触发判断 |
| `tidal_release_authorization_reference` | 非空字符串或 null | default null | 批准/安排证据引用；布尔为 true 但引用缺失时保持等待阶段 |
| `tidal_release_window_reference` | 非空字符串或 null | default null | 赶潮窗口证据引用；布尔为 true 但引用缺失时保持等待阶段 |
| `datum_conversion_evidence` | 对象或 null | default null | 仅接受已存在的 scoped `大沽高程 → 1985国家高程基准`、`target = source - 1.56 m`、东淀-文安洼关系，且 `applied=false` |

`GateDispatchInput.model_json_schema()` 同步反映这些类型与枚举。V1.0 输入缺字段时使用上述兼容默认；V1.1 JSON 输出新增稳定 ID、完整快照、逐规则条件、来源分项统计和覆盖清单。

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
