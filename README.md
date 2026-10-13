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
一次命令自动完成 NC、调闸记录、4 个 SHP 图层、nc2web、stepjson、viewer 和完整性检查，
结果写入 `work/results/<id>/`。所有必需阶段通过才报告成功；失败返回非零退出码，错误和阶段状态
记录在 `run_report.json`。查看脚本默认选择最新完整成功的结果。详见 [流程输入和运行说明](docs/workflow.md)。
