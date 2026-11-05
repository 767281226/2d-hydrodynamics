# hydro-pipeline

纯二维水动力分析：任意投影坐标 DEM + 计算范围 + 进出流断面 + 流量 CSV → 三角网格 → 地形映射 → 断面 → 浅水方程求解 → 结果包 → 查看器。

从 [2d-hydrodynamics](https://github.com/767281226/2d-hydrodynamics) 抽取，放在独立分支 `hydro-pipeline`（无共同历史）。包名为
`hydro_pipeline`，算法和文件格式与主仓库相同（格式标识不变）。

## 范围

**包含**（只做水动力）：

- 网格生成（Gmsh）、精确面积加权地形映射、断面选取
- 原生三角网格浅水求解器：一阶 HLL、静水重构、SSPRK2、Manning 摩阻、numba 加速
- 开放边界：给定入流、给定出流、自由出流（临界流堰公式）
- 大算例驱动：检查点、续算 `--resume`、从父方案检查点分支 `--branch-from`、运行看门狗
- 结果包：原生 UGRID NetCDF、native_web、兼容格网 NC、nc2web、stepjson、SHP 和 GeoJSON（原生三角面）、查看器

**不包含**（业务层，留在主仓库）：

- 社会经济暴露叠加、闸门调度日志和调度规则审计、数据库入库
- 结构化 workflow 和小时回放、演练业务接口、外部证据门禁

业务层不调用本包内部代码，只**按相对路径读取结果包**。接口见 [docs/business_data_interface.md](docs/business_data_interface.md)。

## 安装

```
python -m venv .venv
.venv\Scripts\python -m pip install -e .[test]        # 需要 numba 加速时用 .[test,numba]
```

## 使用

```
# 一份配置走完全流程（任意 DEM）
python -X utf8 -m hydro_pipeline.case_pipeline examples\case_pipeline\case.example.yaml --check
python -X utf8 -m hydro_pipeline.case_pipeline my_case.yaml [--stage mesh|terrain|sections|run|export]

# 已有网格、地形和断面时，直接用大算例驱动
python -X utf8 -m hydro_pipeline.large_run_driver config.json --check
python -X utf8 -m hydro_pipeline.large_run_driver config.json --id <方案号> [--resume]

# 父方案算完后，只更正某个时刻之后的边界流量：从检查点分支续算（与从头算逐位一致）
python -X utf8 -m hydro_pipeline.large_run_driver new_config.json --id <新方案号> --branch-from <父运行目录>\checkpoints\<检查点>.npz

# 看门狗：时间步塌缩、计算变慢或停止输出进度时停止运行
python -X utf8 -m hydro_pipeline.run_watchdog <运行目录> [--action stop|alert]

# 从已有 result_native.nc 重新导出并校验
python -X utf8 -m hydro_pipeline.large_run_export <bundle目录> --verify

start_viewer.bat <方案号>                                    # 查看 work\results\<方案号>\bundle
```

只剖分网格（DEM + 计算范围 → 三角网格）的完整步骤、数据存放位置和实测示例见 [docs/mesh_generation.md](docs/mesh_generation.md)。

配置字段、单位、范围和限制：

- `docs/case_pipeline.md`；
- `examples/large_run_output/*.schema.json`；
- `examples/case_pipeline/case_pipeline_config.schema.json`。

## 限制

- 不做坐标转换、重采样和 NoData 填补；DEM 必须是投影坐标、单位为米的 GeoTIFF
- 不支持降雨输入（驱动 v1）、按水位调度闸门、堤防漫顶、闸下淹没出流
- 计算范围和断面位置由使用者提供；断面流量过程的来源（实测、重建、设计）由使用者标注
- 精确地形映射上限 500 000 个单元
- 实验性软件，未经率定，结果中 `production_approved=false`

## 测试

```
.venv\Scripts\python -m pytest -q
```
