# 2d-hydrodynamics

二维水动力模型输入接口与工程架构设计。

## 当前阶段

项目目前处于“二维水动力模型输入接口与工程架构设计阶段”。当前代码
只提供一次模拟所需的 YAML 配置数据模型、DEM 映射接口占位和严格校验加载器，
尚未实现二维水动力求解器，也不会解释或执行具体数值离散、数值通量、控制方程
或干湿边算法。

## 快速开始

```bash
python -m pip install -e ".[test]"
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
- `hydrodynamics/dem_contract.py`：DEMMetadata、DEMReader 和 DEMValidator 契约。
- `TerrainMapper` 当前只做参数检查并明确抛出未实现异常，不读取 DEM。
- `examples/`：完整配置案例；当前不会读取其中的栅格或 CSV 数据。
- `docs/`：模型、参数、数据格式、数值接口、校验和开发约定。
- `tests/`：配置加载与错误检测测试。
- [TODO.md](TODO.md)：需要在求解器设计阶段确认的开放决策。
- [docs/terrain_mapping.md](docs/terrain_mapping.md)：DEM → 计算网格 V1.0 规范。
- [docs/dem_data_contract.md](docs/dem_data_contract.md)：DEM 数据契约 V1.0。
- [docs/dem_inspection_report.md](docs/dem_inspection_report.md)：真实 DEM 只读体检报告。
