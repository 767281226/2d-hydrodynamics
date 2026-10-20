# 2d-hydrodynamics

二维水动力模型输入接口与工程架构设计。

## 当前阶段

项目目前处于“二维水动力模型输入接口与工程架构设计阶段”。当前代码提供一次模拟所需的 YAML 配置数据模型、可选单波段 GeoTIFF Reader、纯 Python DEM 映射接口和严格校验加载器。尚未实现二维水动力求解器，也不会解释或执行具体
数值离散、数值通量、控制方程或干湿边算法。

## 快速开始

```bash
python -m pip install -e ".[test]"
# 可选 GeoTIFF Reader：python -m pip install -e ".[raster]"
python -m pytest
```

从 Python 程序加载配置：

```python
from hydrodynamics.config import load_config

config = load_config("examples/case_001/config.yaml")
```

完整示例见 [examples/case_001/config.yaml](examples/case_001/config.yaml)，
输入契约见 [docs/input_parameters.md](docs/input_parameters.md)，DEM 映射规范见
[docs/terrain_mapping.md](docs/terrain_mapping.md)。

## 目录

- `hydrodynamics/config.py`：Pydantic 配置模型、TerrainField/TerrainMapper 接口和 YAML 加载器。
- `hydrodynamics/terrain_mapping.py`：DEM 映射与数据契约 API 的公共导出入口。
- `hydrodynamics/dem_contract.py`：DEMMetadata、DEMDataset、可选 GeoTIFFDEMReader 和 DEMValidator 契约。
- `hydrodynamics/dem_reader.py`：Reader-focused 公共导入入口。
- `GeoTIFFDEMReader` 可选读取单波段 GeoTIFF；`TerrainMapper.map_dataset` 已支持内存 DEMDataset 的 V1 映射，旧的 `map` 方法仍是配置占位接口。
- `examples/`：完整配置案例；当前不会由 `load_config` 自动读取其中的栅格或 CSV 数据。
- `docs/`：模型、参数、数据格式、数值接口、校验和开发约定。
- `tests/`：配置、DEM Reader 和内存映射测试。
- [TODO.md](TODO.md)：需要在求解器设计阶段确认的开放决策。
- [docs/terrain_mapping.md](docs/terrain_mapping.md)：DEM → 计算网格 V1.0 规范。
- [docs/dem_data_contract.md](docs/dem_data_contract.md)：DEM 数据契约 V1.0。
- [docs/dem_inspection_report.md](docs/dem_inspection_report.md)：真实 DEM 只读体检报告。

## 最终结果调闸记录

最终 NetCDF 导出同时生成同目录的 `<文件名>_gate_dispatch.csv/.json` 调闸记录，保存规则触发时间、
判断依据和调用方提供的调闸动作及来源。站点水位、潮位窗口或人工判断条件缺失时会记录缺项。
`NetCDFWriter.write()` 和 `write_hourly_netcdf()` 接收独立的 `GateDispatchInput`。
安装输出依赖：`python -m pip install -e ".[netcdf]"`。
输入格式、结果字段和已有结果补充记录的方法见 [调闸记录契约](docs/gate_dispatch_log.md)。

## 计算与完整输出自动流程

在仓库根目录执行 `python -m pip install -e ".[workflow]"` 后，运行：

```powershell
python -m hydrodynamics --id run01
# 或 run_qingbei_nc.bat --id run01
```

流程配置为 `examples/case_001/workflow.yaml`，准备网格为本地外部输入。
一次命令自动完成 NC、调闸记录、nc2web、stepjson、viewer 和完整性检查；structured 路径不输出 SHP，
结果写入 `work/results/<id>/`。所有必需阶段通过才报告成功；失败返回非零退出码，错误和阶段状态
记录在 `run_report.json`。查看脚本默认选择最新完整成功的结果。详见 [流程输入和运行说明](docs/workflow.md)。

## Native triangular result SHP

All new SHP features must be actual native calculation faces. The retained structured workflow writes NC and web products and marks SHP as not applicable. Rectangle SHP writers now reject calls before creating output. See [usage and limitations](docs/native_triangle_shp.md), [中文说明](docs/zh-CN/native_triangle_shp.md), and the [small synthetic example](examples/native_triangle_shp/README.md). This first adapter supports existing closed-wall sources=none P4 results, FRAME or saved-time MAX_OVER_WINDOW, full or wet face sets, native projected CRS or EPSG:4490. It remains experimental and cannot approve engineering publication.

## Security-area exclusion

安全区从实际三角计算域排除，新边界使用既有 closed_wall；P2/P3/native/SHP共同绑定来源与活动面。项目默认case_001已启用map/securityArea约束，当前structured/hourly会在模型执行前明确拒绝；通用未配置安全区的structured仍输出非SHP产品。见[输入与范围](docs/security_exclusion.md)、[English](docs/en/security_exclusion.md)及[小型合成例子](examples/security_exclusion/README.md)。Manning与rainfall运行可使用同一排除守卫；原生SHP当前仅支持none结果。所有验证限合成小案例及真实安全矢量只读几何兼容，不构成工程认证。

## 原生源项三角 SHP v2

`hydrodynamics-triangle-shp`支持独立v2请求，读取已完成的Manning、rainfall或组合源项原生结果，保留实际计算面、source身份/预算及安全区活动域。v1 none合同与默认schema保留。见[说明](docs/zh-CN/native_source_triangle_shp.md)、[English](docs/native_source_triangle_shp.md)与[合成案例](examples/security_exclusion/README.md)。导出器只读，不执行solver，结果仍为实验性离线GIS产物。


## Native synthetic batch

`run_qingbei_nc.bat` now defaults to the four-face `SYNTHETIC_ONLY` native Manning/rainfall/security example. Run it from any caller directory; outputs go to a new `work/results/<script_id>` directory. `--id`/`--script-id` and `--out-dir` are supported. `--engineering` only reports missing engineering inputs and unsupported open-boundary execution; it never starts an engineering solver. See [the launcher guide](docs/native_batch_launcher.md) and [中文说明](docs/zh-CN/native_batch_launcher.md). The script requires the project `.venv` with `.[triangular,shp]`; it does not install dependencies automatically.
