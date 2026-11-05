# 业务层数据接口（只读，相对路径）

本包只产出水动力结果。暴露评估、调度日志、入库和前端等业务功能，都在本包之外实现，通过**相对于运行目录的路径**读取结果，
不导入 `hydro_pipeline` 的内部模块。

## 运行目录（`<output.root>/<方案号>/`）

| 相对路径 | 内容 | 用途 |
|---|---|---|
| `run_report.json` | `status`：`running`、`complete` 或 `failed`；`branch`（分支来源）；`error` | 业务层只在 `status == "complete"` 时读取结果 |
| `config.resolved.json` | 解析后的驱动配置（绝对路径） | 追溯输入 |
| `ledger.jsonl` | 每个保存帧一行：蓄量、入流和出流体积，各边界组的流量（`groups.<断面>.outflow_discharge_m3_s` 等）、残差 | 闸门过流过程、水量平衡 |
| `progress.jsonl` | 计算进度事件 | 监控 |
| `bundle/` | 结果包，见下表 | 主要结果 |

## 结果包（`bundle/`）

| 相对路径 | 格式 | 内容 |
|---|---|---|
| `result_native.nc` | UGRID NetCDF（time, face） | 权威结果：水深、单宽流量、流速、水位；保存帧最大水深、最大流速、到达时刻；地形；面 ID |
| `run.json` | `large-run-output-bundle-v1` | 方案号、时间原点、坐标系、高程基准、强迫说明、各产物路径和哈希 |
| `summary.json` | JSON | 逐帧最大水深、最大流速、蓄量、湿面积；`boundary_group_ledger_series`（各边界组逐帧序列）；出流缺额 |
| `native_web/` | `native-triangle-web` 2.0 | `manifest.json`、`mesh.bin`、每帧 `frame_NNNNNN.bin`（网页渲染） |
| `shp/<role>/faces.{shp,dbf,…}` | ESRI Shapefile | 每个原生三角面一个要素。`role` 取以下四种：`full_domain`（末帧）、`maximum_wet_surface`（最大淹没面），以及二者的 `_4490` 版本（CGCS2000 经纬度） |
| `shp/<role>/faces.geojson` | GeoJSON | 与同目录 SHP 要素、属性、坐标完全一致 |
| `shp/<role>/manifest.json` | JSON | 字段含义、单位、坐标系、选择条件、文件哈希 |
| `<case_id>.nc`、`nc2web/`、`stepjson/`、`stepjson_native/` | 旧版结构化格式 | 由原生三角结果派生的兼容格网，供旧前端使用 |
| `viewer.html` | HTML | 本地查看器 |
| `checksums.json` | JSON | 结果包内所有文件的字节数和 SHA-256 |

`run.json` 中的 `legacy_optional_artifacts`（`gate_dispatch.*`、`social_economy_overlay`、`database_receipt`）是兼容占位，
状态始终为未请求，因为这些由业务层产出。

## 业务层的典型做法

- **暴露评估**：读取 `shp/maximum_wet_surface/faces.shp`（或 `.geojson`）的 `max_depth`、`arrive_s` 字段，或逐帧读取
  `result_native.nc`，与人口和耕地网格叠加，结果写到业务层自己的目录。
- **调度日志**：读取 `ledger.jsonl` / `summary.json` 中各边界组的流量序列，与调度台账对照，结果写到业务层自己的目录。
- **入库**：以 `checksums.json` 校验后，按 `run.json` 的路径逐项读取。

业务层只读不写，结果包内的文件不要改。需要新结果时，新建方案号重新计算，或用 `--branch-from` 分支。
