# 计算与完整输出自动流程

在仓库根目录执行计算命令，流程按阶段自动输出所有结果：

```powershell
python -m pip install -e ".[workflow]"
python -m hydrodynamics --id run01
# 或指定流程文件和输出父目录
python -m hydrodynamics --workflow examples/case_001/workflow.yaml --id run01 --out-dir D:/out
```

`run_qingbei_nc.bat` 调用同一入口，接受 `--id` 和 `--out-dir` 等参数。
安装后也可使用 `hydrodynamics-run --workflow <流程文件>`。
默认流程文件为 `examples/case_001/workflow.yaml`，相对输入路径以该文件所在目录为基准。
省略 `--id` 时生成 UTC+8 时间戳目录；已有运行目录拒绝覆盖。
所有输入文件在计算前检查，原始 DEM 不由该流程读取。

## 自动执行顺序

`preflight → prepare → model → gate_dispatch → shp → nc2web → stepjson → socio_overlay → viewer → validate`

社会经济叠加未配置时标为 `skipped`；配置后出错会使流程失败。
其余阶段为必需阶段。NC 计算完成不会被当作全流程完成；只有最终检查通过才标为 `complete`。
每次运行写出原子更新的 `run_report.json`，包括阶段状态、时间、输入路径和 SHA256、失败阶段与错误。
失败退出码为 1，中断为 130，成功为 0。失败保留已生成文件供检查，后续阶段保持 `pending`。

```text
work/results/<id>/
  dongdian_qingbei_2023_288h.nc
  *_gate_dispatch.csv / *.json
  dispatch_input.json
  shp/                       # 4 个图层及其 sidecars
  nc2web/                    # manifest、mesh、逐时 frame
  stepjson/                  # index、逐时 JSON
  viewer.html
  run.json
  parse_summary.json
  run_report.json
```

SHP 包含最大淹没范围和完整蓄滞洪区网格，分别输出 EPSG:4548 与 EPSG:4490。
全干场景也写出 0 条记录的淹没图层。最后检查 NC 字段/时间/有效范围、实际帧数、二进制完整性、
逐步 JSON 数量、SHP sidecars/条数/CRS 和 viewer 文件。viewer 模板随 Python 包分发。
查看结果可使用 `start_viewer.bat <id>`；省略 ID 时选择最新完整成功的运行，计算命令只生成文件。
`python -m hydrodynamics --show-latest` 只打印最新结果目录，失败或只有部分输出的目录不会被选中。

## 流程配置契约

完整配置示例见 `examples/case_001/workflow.yaml`。配置拒绝未知字段；数值拒绝字符串、布尔值和 NaN/Inf。
错误包含字段路径或 CSV 行列。该流程复用已有 `HourlyInundationSolver`，不修改其数值实现。
当前支持零初始水深、常数 Manning 糙率、`model.start_time=0` 和 EPSG:4548 网格。
`case_config` 提供网格范围、时间窗、初值、糙率及数值常数；计算使用流程指定的准备网格和 forcing CSV，
不由 case 的原始 DEM/独立边界 CSV 引用重新准备数据。

| 字段 | 类型/单位 | 默认或要求 | 范围及校验 |
|---|---|---|---|
| `schema_version` | 字符串 | `1.0` | 仅支持 `1.0` |
| `case_config` | 路径字符串 | 必填 | 非空，文件必须存在；复用 `load_config` 校验 |
| `prepared_grid` | 路径字符串 | 必填 | 非空 `.npz`，准备网格必须存在 |
| `forcing.file/source` | 路径/来源字符串 | 必填 | 非空；CSV 文件必须存在 |
| `forcing.time_unit` | 字符串 | `s` | 仅支持 `s` |
| `forcing.discharge_unit` | 字符串 | `m3/s` | 仅支持 `m3/s` |
| `forcing.rainfall_unit` | 字符串 | `mm/step` | 仅支持 `mm/step` |
| `time_origin` | ISO8601 字符串 | 必填 | 必须含时区，表示相对秒数 0 的时刻 |
| `settings.eta_crest_m` | 数值，m | `null` | 有限高程；null 使用已有模型的无固定限水位模式 |
| `settings.water_surface_slope` | 数值，无量纲 | `null` | 有值时有限且 >0；null 使用模型原默认值 |
| `settings.max_spread_m_s` | 数值，m/s | `null` | 有值时有限且 >0；null 使用模型原默认值 |
| `settings.area_schedule` | 进度点列表 | 空列表 | 非空时至少两点；`time_h` 有限、≥0 且严格递增；`fraction` 有限、在 [0,1] |
| `output_directory` | 路径字符串 | `../../work/results` | 非空；创建新的运行子目录 |
| `result_name` | 文件名 | `result.nc` | ASCII 字母/数字开头，仅含字母/数字/._-，以 `.nc` 结尾；无路径及 Windows 保留名 |
| `dispatch_file` | 路径字符串或 null | `null` | 有值时非空、文件存在，按 `GateDispatchInput` 校验 |
| `record_xihe_component_changes` | 严格布尔值 | `true` | 控制是否转录 forcing 西河闸分量变化，不改变边界数据 |
| `socio_overlay_shp` | 路径字符串或 null | `null` | 有值时要求 SHP/DBF 存在，仅用于可视化叠加 |

运行 ID 最长 96 字符，ASCII 字母/数字开头，只含字母/数字/._-，不允许路径、尾随点或 Windows 保留名。
calendar 起点通过 `time_origin` 设置，forcing 时间仍从 0 开始。

## 准备网格 NPZ

准备输入使用 `allow_pickle=False` 读取，包含以下必需字段：

| 字段 | 类型和单位 | 校验 |
|---|---|---|
| `elevation` | 二维有限数值，m | `(ny,nx)`，不填 NoData |
| `active_mask` | 布尔或 0/1 | `(ny,nx)`，至少一个有效单元，位于 combined 内 |
| `combined_mask` | 布尔或 0/1 | `(ny,nx)`，完整蓄滞洪区，包含 active/inlet/conveyance |
| `inlet_mask` | 布尔或 0/1 | `(ny,nx)`，至少一个入口单元 |
| `conveyance_mask` | 布尔或 0/1 | `(ny,nx)`，可为空 |
| `zones` | 二维字符串 | none/qingbei/qingnan/wenanwa，与两个区域掩膜一致 |
| `x/y` | 有限一维数值，m | 与 case 的单元中心坐标一致，各轴至少 2 点 |
| `coordinate_system` | 非空标量字符串 | 与 case 一致且为 EPSG:4548 |
| `vertical_datum` | 非空标量字符串 | 与已配置的目标基准一致，不执行基准转换 |
| `elevation_unit` | 标量字符串 | `m` |
| `provenance` | 非空标量字符串 | 保留准备输入来源 |

本地的准备网格不随代码提交。已有映射好的 NC 可通过独立准备脚本整理：

准备脚本另需安装可选矢量栅格化依赖：`python -m pip install -e ".[workflow,raster]"`。

```powershell
python examples/prepare_workflow_grid.py --source-nc work/results/dongdian_qingbei_2023_288h.nc --output work/inputs/qingbei_50m.npz
```

脚本只读已处理 NC 和矢量几何，复用既有区域分类函数。`--source-mask` 默认 `qingbei`，可显式指定 `combined`。
`--river-buffer-m` 默认 100、`--inlet-width-m` 默认 800，单位 m，均要求有限且 >0。
准备脚本拒绝已有目标文件，并拒绝缺失基准、NoData 或与指定作用域不一致的掩膜。

## forcing CSV 和调闸记录

必需列：`time_s,inflow_m3s,outflow_m3s,rainfall_mm_per_step`。
所有值有限且 ≥0；至少两行，时间严格递增、每步与 `output.interval` 一致，覆盖整个运行时间窗。
降雨单位为每个计算步的 mm；case 降雨关闭时该列必须为 0。不插值、不填缺测、不从日期推断控制条件。
可选 `duliu_component_m3s,xihe_component_m3s` 必须成对提供，均 ≥0，且两者之和等于总出流。

西河闸分量改变时，流程转录一条模型预设调度记录，注明 CSV 行号与来源。
它不是实测调闸，也不代表规则已触发。真实站点条件和操作记录通过 `dispatch_file` 另行提供，
缺少水位、潮位窗口或人工判断时仍记录无法判断。外部记录与自动记录同闸同时冲突时直接报错，
可显式关闭自动转录来采用外部记录。详见 [gate_dispatch_log.md](gate_dispatch_log.md)。
