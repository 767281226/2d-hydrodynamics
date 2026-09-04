# Development guide

## Environment

Python 3.11 or newer is supported. Install the package and its small runtime
dependency from the repository root:

```bash
python -m pip install -e .
```

Install the optional test dependency when using pytest:

```bash
python -m pip install -e ".[test]"
```

如需使用可选 GeoTIFF Reader：

```bash
python -m pip install -e ".[raster]"
```

## Load and map a case from Python

```python
from hydrodynamics import GeoTIFFDEMReader, TerrainMapper, load_config

config = load_config("examples/case_001/config.yaml")
dataset = GeoTIFFDEMReader().read_dataset("path/to/dem.tif")
field = TerrainMapper().map_dataset(
    dataset,
    config.domain,
    coordinate_system=config.model.coordinate_system,
    strategy=config.terrain.resampling.strategy,
    nodata_strategy=config.terrain.nodata_strategy,
    min_valid_coverage=config.terrain.min_valid_coverage,
)
```

`SimulationConfig` 是类型明确的 Pydantic 对象；`load_config` 不打开引用文件。
`map_dataset` 只接收已读入内存的 `DEMDataset`，不重投影、不调用 Solver。

## Checks

```bash
python -m compileall hydrodynamics
python -m pytest
git diff --check
```

测试覆盖配置错误、DEM Reader 错误、几何/CRS/NoData 校验和三种内存映射策略。
当前仍不包含任何二维水动力方程或时间步计算。

## Read a GeoTIFF (optional)

```python
from hydrodynamics import GeoTIFFDEMReader

reader = GeoTIFFDEMReader()
metadata = reader.read_metadata("path/to/dem.tif")
dataset = reader.read_dataset("path/to/dem.tif")
```

Reader 只读取源值、掩码和元数据；不重采样、不重投影、不填补 NoData。
当前整幅读取接口不是大型栅格的分块处理方案，低内存优化留待后续。
