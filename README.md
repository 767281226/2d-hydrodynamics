# 2d-hydrodynamics

用于二维洪水研究的 Python 计算、输入准备和结果交换工具。**已有可执行求解器与合成示例，仍是实验性软件，不是已率定或经工程认证的洪水预报系统。** 本页依据2026-10-28源码核查和实测、2026-10-29恢复的测试结果整理；测试文件存在不等于测试全通过。

## 能力状态

二维浅水方程以水深和两个方向的水量动量描述水流；有限体积法按单元边界计算交换量。结构化网格是规则方格，原生三角网格直接以三角面参与计算。

| 能力 | 当前状态与边界 | 代码与测试依据 |
| --- | --- | --- |
| 配置与校验 | 已实现YAML类型、单位、范围与组合检查；加载不等于读取全部数据或启动计算 | [config.py](hydrodynamics/config.py)、[配置测试](tests/test_config.py) |
| 结构化浅水求解器 | 实验性：NumPy、HLL通量、SSPRK2两阶段推进、CFL限步；含闭壁、降雨和Manning摩阻；非默认CLI引擎 | [solver.py](hydrodynamics/solver.py)、[test_hll_rest_state_finite_and_ssprk2_rhs等](tests/test_solver.py) |
| 小时淹没回放 | 已实现按入/出流和雨量更新体积，再按DEM填容；不是逐步求解二维浅水方程 | [hourly_inundation.py](hydrodynamics/hourly_inundation.py)、[test_hourly_step_conserves_volume_and_keeps_inactive_dry](tests/test_hourly_inundation.py) |
| 原生三角计算 | 实验性：一阶HLL、静水重构、SSPRK2；标准入口为闭壁，none/Manning/雨量/联合源项分别有运行合同 | [三角求解器](hydrodynamics/triangular_solver.py)、[求解测试](tests/test_triangular_solver.py)、[源项测试](tests/test_triangular_source_run.py) |
| 开放边界 | 已有流量/绝对水位准备；T004另有全湿、固定方向及认证条件限制的流量入/出实验内核，不是通用工程入口 | [边界准备](hydrodynamics/triangular_boundaries.py)、[T004推进](hydrodynamics/t004_k1_experimental_solver.py)、[阶段/回滚测试](tests/test_t004_k1_i.py) |
| 水位出流、水闸、自由出流 | 标准入口没有通用执行能力；能准备水位不等于能算边界通量，调闸日志不会发送控制命令 | [边界字段](hydrodynamics/boundary.py)、[调度审计](hydrodynamics/gate_dispatch.py) |
| Manning糙率 | 已有常数/空间字段、逐三角面赋值及源项执行；需明确来源和SI单位解释。糙率表达地面阻力 | [roughness.py](hydrodynamics/roughness.py)、[糙率测试](tests/test_roughness.py)、[三角业务字段](hydrodynamics/triangular_business.py)、[Manning测试](tests/test_triangular_manning.py) |
| 干湿处理 | 求解器已有阈值、干面零动量及负深诊断/保护；独立通用策略仍占位，动态湿润前沿工程验证未完成；T004限定全湿 | [dry_wet.py](hydrodynamics/dry_wet.py)、[干湿测试](tests/test_solver.py)、[三角全干/静水测试](tests/test_triangular_solver.py) |
| DEM与网格 | 已有原始/窗口读取、独立映射、JSON三角导入及可选Gmsh剖分；准备产物不等于仿真结果 | [网格准备](hydrodynamics/mesh_preparation.py)、[网格测试](tests/test_triangular_mesh.py)、[窗口测试](tests/test_windowed_mapping.py) |
| 安全区 | 原生路线按原域D减安全区S建活动域；启用该约束的旧structured流程明确拒绝 | [安全区实现](hydrodynamics/security_exclusion.py)、[测试](tests/test_security_exclusion.py) |
| NC/SHP/网页交换 | 已有写出与严格读回，生成条件随入口/格式而异，见输出表 | [输出测试](tests/test_output.py)、[源项SHP测试](tests/test_native_source_triangle_shp.py)、[小型网页适配测试](tests/test_full_dem_m1_outputs.py) |
| 审计库、社会经济与真实工程 | 有适配/资料门禁和覆盖审计；不等于风险/损失计算、真实区域全链或率定通过 | [审计库测试](tests/test_gate_dispatch_db.py)、[exposure_risk.py](hydrodynamics/exposure_risk.py)、[工程缺项](docs/rehearsal_integration/20_real_case_triangular_input_gaps_2026-10-20.md) |

HLL是单元间通量计算方法；SSPRK2是两阶段时间推进；CFL条件限制时间步。静水重构用于平衡床面变化与水面压力。

## 安装

要求Python 3.11+。Windows使用PowerShell 7（pwsh），在项目根目录执行。下面是未安装环境时的步骤，本次未重装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test,triangular,shp]"
```

GeoTIFF读取另装`.[raster]`，Gmsh剖分另装`.[gmsh]`，审计库另装`.[database]`。安装不等于已配置真实资料或数据库。

## 最小快速开始

推荐直接运行原生合成模块。以下命令本次实测完成；**再次运行必须换新ID**，已有目录会拒绝覆盖：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m hydrodynamics.native_batch_launcher --id readme_native_check
```

这是4个三角面、0/0.05/0.1秒的合成例，计算Manning与降雨并排除安全区，10个阶段全部完成。输出为`work/results/readme_native_check/`，先看`run_report.json`和`summary.json`。支持`--script-id`及`--out-dir`，规则见[启动说明](docs/native_batch_launcher.md)与[中文说明](docs/zh-CN/native_batch_launcher.md)。

`run_qingbei_nc.bat`也是同一合成模块的包装，不是青北真实工程入口。当前要求shell仅用pwsh，本次未运行需要cmd解释的BAT，也排除了直接调用cmd的测试。

| 入口 | 实际行为 |
| --- | --- |
| `python -m hydrodynamics` | workflow默认读取[case_001/workflow.yaml](examples/case_001/workflow.yaml)，走hourly_inundation结构化回放，非原生三角求解器 |
| `python -m hydrodynamics.native_batch_launcher` | 独立原生三角合成链 |
| 原生启动器的`--engineering` | 只做资料/能力预检，填全模板也不启动工程求解器；缺项/不支持时非零退出 |

本次`python -m hydrodynamics --id readme_check`在安全区预检返回`UNSUPPORTED_STRUCTURED_SECURITY_EXCLUSION`，退出1；prepare、model、所有输出及audit_database均pending。没有运行288小时工程或写实际数据库。报告在`work/results/readme_check/run_report.json`；勿靠关闭安全区获得表面成功。

## DEM、网格、边界和糙率输入

1. **DEM**是地面高程栅格。提供源CRS（坐标参考系）、水平/垂直单位、高程基准、NoData（无有效数据）掩膜及覆盖证据。Reader只读原值，不承担坐标转换、补洞或求解；映射在独立层。[DEM契约](docs/dem_data_contract.md)、[映射规则](docs/terrain_mapping.md)、[历史只读检查](docs/dem_inspection_report.md)。
2. **网格**需可信原域D、安全区S、投影米制坐标、三角连接和稳定面ID。导入/Gmsh生成后需通过质量和覆盖门，不能把方格展示拆三角冒充计算网格。[三角准备](docs/triangular_mesh_preparation.md)、[安全区](docs/security_exclusion.md)。
3. **初值和边界**：水深用m；绝对水位须与地形基准一致。流量用m³/s，明确来源、时间轴、时区/原点、组及方向。上游流量、下游水位与闸站观测不能互相替代，准备通过不保证引擎支持执行。[参数](docs/input_parameters.md)、[初值](docs/initial_conditions.md)、[三角业务准备](docs/triangular_business_preparation.md)。
4. **糙率与雨量**：明确常数/每面Manning系数、空间分布和SI单位解释，雨强及区间不可省略；排除区雨量记为未模拟，不默认转入活动域。[源项执行](docs/triangular_source_execution.md)。

[case_001/config.yaml](examples/case_001/config.yaml)是配置示例，不是完整工程认证输入包。

## 输出

NC（NetCDF）是带坐标、时间、单位和元数据的科学数据文件；SHP是GIS可读取的矢量图层。

| 产物 | 条件及含义 |
| --- | --- |
| 原生NC | 准备UGRID只含网格/地形；仿真NC为(time, face)，绑定网格/业务/运行/源项身份，二者不可混用 |
| 原生三角SHP | 从严格验证的原生结果逐实际计算面导出，none/Manning/雨量/联合源项分版本。FRAME为保存帧，MAX为指定保存时窗最大值；标准导出不接受任意T004结果；旧矩形writer拒绝 |
| nc2web二进制、stepjson | 旧结构格式要求合格NC，显示三角片不等于计算三角面；另有native_triangle_web和小型M1兼容适配，限QA验证，不代表真实全域production/latest可用 |
| 调闸CSV/JSON | hourly写出及独立审计入口可生成；原生默认例不自动生成全部调闸产物。规则触发、模型预设变化和真实操作来源分开记录，不执行闸控 |
| CSV/GeoTIFF | SimulationOutput有独立writer，受字段/网格/单位及可选依赖约束，不是每个入口自动导出 |
| 报告/摘要 | 看status、failed_stage及真实退出码；目录或部分产物存在不等于成功，QA包不等于工程完成 |

详见[输出说明](docs/output_results.md)、[源项SHP v2](docs/native_source_triangle_shp.md)、[调闸日志](docs/gate_dispatch_log.md)、[旧整体流程](docs/workflow.md)。审计库写入需显式配置和授权，旧case默认虽配置audit_database，本次在此前预检即被阻断。

## 限制与本次验证

- `TerrainMapper.map`仍是占位，应区别于已实现的`map_dataset`/窗口映射；抽象DEMReader未实现方法不代表具体GeoTIFFReader未实现。窗口双线性映射、nearest/interpolate缺测填补仍拒绝。
- 结构化求解器拒绝通用非wall边界与直接level初始化；独立初值工厂的velocity输入仍保留。schema能保存字段不等于引擎能执行。
- 动态湿润前沿、堤顶越流、闸门水力、通用反流/自由出流及真实洪水精度未在此次证实；T004流量实验不能外推为通用水位出口。
- 真实网格、地形映射、求解、结果发布分别验收；人工重建过程不证明率定精度，社会经济覆盖审计不证明风险/损失结果。
- 本次pytest正常重跑退出1，完整进度记录计得**1237通过、14失败、9跳过**（1260项）。失败涉及Gmsh提前导入、pending清理、worker测试接口/字段不一致及wheel构建缺目录。本次仅改README，不修这些问题。首次受限运行发生Windows原生异常，不计通过。
- PowerShell7政策另行排除三份直接调用cmd的测试：`tests/test_native_batch_launcher.py`、`tests/acceptance/native_batch/test_native_batch_launcher_independent.py`、`tests/acceptance/test_r2a_windows_junction_independent.py`；排除不计入上述通过或跳过数。真实数据库集成未启用，没有无条件全套通过结论。
- [TODO](TODO.md)、[PROJECT](PROJECT.md)和部分早期文档有历史阶段描述，应以具体合同和实际报告核对当前行为。

## 目录

| 路径 | 内容 |
| --- | --- |
| `hydrodynamics/` | 输入/准备、独立求解器、实验边界、输出和审计 |
| `examples/` | 配置、合成输入和参考，不是每份都可直接计算 |
| `tests/` | 单元、合成回归和独立验收，含依赖/真实集成门禁 |
| `docs/` | 设计、说明和分批验收，注意日期和范围 |
| `work/` | 本地结果/验收产物，勿覆盖历史目录或提交真实数据 |

## 文档索引

- [中文文档](docs/zh-CN/README.md) / [English documentation](docs/en/README.md)。
- [数值ADR](docs/triangular_solver_adr_p4_0.md)、[三角运行合同](docs/triangular_solver_contract.md)、[边界准备中文ADR](docs/p4_3a_triangular_boundary_contract_adr.md)；准备层文档不替代T004实验版本说明。
- [三角空间叠加](docs/triangular_overlay_preparation.md)、[社会经济只读查询合同](docs/exposure_risk_query_contract.md)。
- [数据库中文指南](docs/zh-CN/database.md) / [English database guide](docs/en/database.md)。审计入库不等于真实闸控或业务五页上线。

## 待确认

真实工程仍需可信原域/网格、DEM源基准与覆盖、初始水量、实测流量/水位/雨量、边界物理方案、糙率依据，以及独立观测和误差验收目标。未知应保留未知，不能用合成默认值代替资料确认。

## 许可证

[Apache License 2.0](LICENSE)。
