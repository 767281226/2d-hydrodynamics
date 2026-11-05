# 东淀清北案例：23·7 复演与设计洪水

本目录收齐了清北案例的全部输入。它们原先放在仓库外的 `D:\devTools\hydro_exp\c8\qb`、`c8\design`、
`D:\devTools\ai\WorkBuddySpace\...` 和 `work/b1r31`，现按原样复制进来。配置文件只用相对路径
（相对配置文件解析）。每个文件的来源路径和 SHA-256 见 `provenance.json`。

## 运行

在仓库根目录：

    python -X utf8 -m hydrodynamics.large_run_driver cases/qingbei/configs/real_23_7_288h.json --check
    python -X utf8 -m hydrodynamics.large_run_driver cases/qingbei/configs/real_23_7_288h.json --id <方案号>
    python -X utf8 -m hydrodynamics.large_run_driver cases/qingbei/configs/design_P100_config.json --id <方案号>

结果写到 `work/results/<方案号>/`。仓库外还需要以下文件（体积大或属于本地数据，不入库）：

| 文件 | 用途 |
|---|---|
| `data/dem/DEMn_1m.tif`（SHA-256 见 `inputs/dem_sha256.txt`） | 校验地形来源；重新做地形映射和断面选取时需要 |
| `data/grid_50m_social_economy/` | 社会经济暴露叠加（人口、耕地） |
| `examples/case_001/results/dongdian_qingbei_2023_288h.nc` | 只有 `scripts/qb_split.py` 用到（旧模型的淹没掩膜，用来判定清北范围） |

## 目录

| 路径 | 内容 |
|---|---|
| `configs/real_23_7_288h.json` | 23·7 复演，288 h，从 2023-08-01 02:00 起（即正在跑的 `qingbei_23_7_288h` 的配置） |
| `configs/design_P{10,20,50,100}_config.json` | 设计洪水方案，说明见 `design/README.md` |
| `mesh/qb_ext_s25_Swall_domain-mesh.json` | 计算网格：80,990 个三角形，62.96 km²，安全区外围筑墙 |
| `terrain/terrain_qbext_faces.npz` | 每个三角形的精确面积加权床面高程 |
| `terrain/terrain_qbext_report_full.json` | 地形映射完整报告，48 MB，只作来源记录，**不入库**（见 `.gitignore`） |
| `inputs/forcing_qb_23_7.csv` | 来流：A 入口 = 新盖房 Q 延后 80 h × 0.413 + 基流 200；西河闸控制泄量；独流减河参考流量 |
| `inputs/inflow_calibration.json` | 上述 k、延时、基流的率定记录（按水量平衡对淀内台头站 5.93 m 峰值率定） |
| `inputs/request_w450.json` → `inputs/sections_audit_qbext.json` | 断面选取请求及 `hydrodynamics.river_section_selection` 的输出 |
| `inputs/sections_audit_qbext_2307.json` | 运行使用的版本：在上一份基础上手工改了 A 的流量列名（`inflow_A_m3s`） |
| `inputs/qb_geom_ext.json`、`inputs/S_face_mask_ext.npy` | 剖分用边界几何；安全区内三角形掩膜 |
| `source/天津23·7洪水闸门调度台账.xlsx` | 闸门调度台账，用于西河闸开闸时刻、控制泄量和观测水位 |
| `design/` | 设计洪水过程线、驱动 CSV、依据文献（`refs/`） |
| `scripts/` | 构建脚本，路径已改为相对仓库；中间文件写到 `work/cases/qingbei/` |

## 重建顺序（scripts/）

1. `qb_split.py`：用河道中心线切分东淀，挑出与旧淹没范围对应的清北部分，得到 `parts.pkl`
2. `qb_poly.py`：生成 `QB.pkl`
3. `south_dike.py`：沿大清河南堤追踪 DEM 堤顶，得到 `centerline.pkl` 和 `south_dike_raw.npz`
4. `build_domain.py`：清北 + 大清河河道，得到 `domain_qb.pkl`
5. `extend_domain.py`：并入两片清北之间的低地，得到 `domain_qb_ext.pkl`（`north_dike.py` 只是诊断用）
6. `prep_geom_ext.py`：简化和加密边界，得到 `qb_geom_ext.json`
7. `mesh_qbext.py`：用 Gmsh 剖分，得到 `qb_ext_s25_domain-mesh.json`
8. `wall_S_ext.py`：给安全区筑墙（节点偏移 1 mm），得到 `qb_ext_s25_Swall_domain-mesh.json` 和 `S_face_mask_ext.npy`
9. `run_map_any.py <mesh.npz> <输出前缀>`：精确地形映射
10. `python -m hydrodynamics.river_section_selection inputs/request_w450.json --mesh ... --dem ... --out ...`：选取断面
11. `build_design_hydrographs.py`、`make_design_case.py`：生成设计洪水过程线和配置

`inputs/forcing_qb_23_7.csv` 是在会话中直接计算出来的，没有保存单独的生成脚本。计算方法见
`inputs/inflow_calibration.json` 和上表。

## 假设（结果须带这些标注）

- 不考虑降雨；初始水深 0；糙率 n = 0.035
- 断面位置是假设的（用户确认，但不是权威数据）；A 入口流量为重建值，不是实测值
- 独流减河进洪闸 B 按自由出流处理：C_d 0.3275，堰顶 1.03 m；西河闸 C 从 08-12 17:00（t = 279 h）起泄 125 m³/s。2026-11-05 已更正：原 forcing 在 t = 255 h（08-11 17:00）开闸，早了 1 天；当时在跑的 `qingbei_23_7_288h` 用的是原 forcing，更正结果由 `--branch-from` 从 t = 252 h 检查点续算得到（`qingbei_23_7_288h_xihe0812`）
- 安全区按不漫顶处理；DEM 高程基准取 1985 国家高程（误差约 ±0.8 m）
