# 网格剖分：完整调用方法

从**DEM + 计算范围**生成三角网格。可以只剖分网格，不需要断面、流量和计算参数；需要时再接着做地形映射和计算。

## 1. 需要提供的数据

| 数据 | 必填 | 格式 | 要求 |
|---|---|---|---|
| DEM | 是 | GeoTIFF（`.tif`） | 投影坐标、单位米；高程单位米（GeoTIFF 里没写高程单位时，配置里要声明，并附一个说明文件） |
| 计算范围 | 是 | 面图层：SHP（`.shp` `.shx` `.dbf` `.prj`，建议附 `.cpg`）或 GeoJSON；也可以直接在配置里写坐标 | **一个面**（几块面合并后要连成一个多边形）；坐标系与 DEM 相同；完全落在 DEM 范围内 |
| 孔洞 | 否 | 同计算范围 | 不剖分的区域，例如安全区、村庄、高地，边界在计算中当作墙；必须完全位于计算范围内部 |
| 约束线 | 否 | 写在配置里的坐标 | 网格必须贴合的线，例如堤顶线、河道中心线；必须完全位于计算范围内部 |

计算范围就是要计算的区域的外轮廓（例如蓄滞洪区边界），**不是**地形图；地形只从 DEM 里取。

## 2. 数据存放位置

路径都写在配置文件里，**相对配置文件所在目录**解析，也可以写绝对路径。推荐以下目录结构（仓库根目录 = `hydro-pipeline/`）：

```
hydro-pipeline/
├─ data/                               用户数据，不入库（已写进 .gitignore）
│  ├─ dem/<DEM 名>.tif                  DEM
│  ├─ domain/<范围名>.shp (.shx .dbf .prj .cpg)   计算范围，或 <范围名>.geojson
│  └─ holes/<孔洞名>.shp (…)            孔洞（可选，可以有多个）
├─ cases/<case_id>/case.yaml           方案配置（可以入库）
└─ work/<case_id>/                     自动生成的阶段产物（不入库）
   ├─ geometry/geometry.json, stage.json
   ├─ mesh/mesh.json, stage.json       ← 网格
   └─ terrain/terrain_faces.npz, mapping_report.json, stage.json   ← 每个三角形的地面高程
```

按这个结构，`cases/<case_id>/case.yaml` 里的路径写成 `../../data/...`，`work_dir` 写成 `../../work/<case_id>`。
模板：[`examples/mesh_only/case.mesh_only.example.yaml`](../examples/mesh_only/case.mesh_only.example.yaml)，复制到
`cases/<case_id>/case.yaml` 后，改 `case_id`、文件名和网格尺寸即可。

DEM 很大时可以不复制，`dem.path` 直接写它的绝对路径，例如 `D:/data/dem/DEMn_1m.tif`。

### 坐标系不一致时先转换

流水线**不做坐标转换**。计算范围是经纬度（如 CGCS2000，EPSG:4490）而 DEM 是投影坐标（如 EPSG:4548）时，先转换计算范围：

```
ogr2ogr -t_srs EPSG:4548 -lco ENCODING=UTF-8 data\domain\dongdian.shp "原始\东淀&文安洼_shape.shp"
ogr2ogr -t_srs EPSG:4548 -lco ENCODING=UTF-8 data\holes\security_area.shp "原始\dd_securityArea_shape.shp"
```

也可以在 QGIS 中右键图层 → 导出 → 另存为，坐标系选 EPSG:4548。DEM 本身要重投影时，同样在流水线之外完成（`gdalwarp`）。

## 3. 配置文件（只剖分网格）

```yaml
format: case-pipeline-config-v1
schema_version: "1.0"
case_id: dongdian_mesh                 # 字母、数字、._-；也用作网格编号前缀
description: "东淀蓄滞洪区，扣除安全区"
work_dir: ../../work/dongdian_mesh      # 阶段产物目录

dem:
  path: ../../data/dem/DEMn_1m.tif
  crs: "EPSG:4548"                     # 计算范围、孔洞、网格都使用这个坐标系；DEM 的坐标系必须与它等价

domain:
  polygon: {type: shp, path: ../../data/domain/dongdian.shp}     # 或 {type: geojson, path: ...}
  holes:
    - {type: shp, path: ../../data/holes/security_area.shp}
  constraints: []                      # 例：[{name: dike_1, points: [[x1, y1], [x2, y2], ...]}]
  simplify_tolerance_m: 2.0            # 边界简化容差，m；null 表示不简化
  min_vertex_spacing_m: 25.0           # 边界上相邻顶点的最小间距，m；null 表示不处理

mesh:
  default_size_m: 50                   # 一般单元边长，m
  minimum_size_m: 5                    # 边界附近加密时的最小边长，m
  boundary_adaptive: true              # 在边界、孔洞、约束线附近自动加密
  grading_ratio: 0.3                   # 加密到一般尺寸的过渡速度（0–1）
  maximum_nodes: 2000000
  algorithm_2d: 5                      # Gmsh 剖分算法（5 = Delaunay）
```

不写 `sections`、`forcing`、`run` 这三项，这份配置就是"只剖分"：只能运行 geometry、mesh、terrain 三个阶段。三项都写上，才能继续选断面和计算，详见 [case_pipeline.md](case_pipeline.md)。

## 4. 调用

```
# 0. 安装（一次）
python -m venv .venv
.venv\Scripts\python -m pip install -e .[test]

# 1. 预检（几秒）：检查配置、文件、DEM 坐标系与范围、计算范围面积；不剖分
python -X utf8 -m hydro_pipeline.case_pipeline cases\dongdian_mesh\case.yaml --check

# 2. 剖分网格
python -X utf8 -m hydro_pipeline.case_pipeline cases\dongdian_mesh\case.yaml --stage mesh

# 3.（可选）把 DEM 高程赋到每个三角形（逐单元精确面积加权）
python -X utf8 -m hydro_pipeline.case_pipeline cases\dongdian_mesh\case.yaml --stage terrain

# 改了参数想强制重算（默认输入不变时会跳过已完成的阶段）
python -X utf8 -m hydro_pipeline.case_pipeline cases\dongdian_mesh\case.yaml --stage mesh --force
```

在 Python 中调用：

```python
from hydro_pipeline.case_pipeline import run_pipeline
result = run_pipeline("cases/dongdian_mesh/case.yaml", stage="mesh")
print(result["outputs"]["mesh"], result["summary"])
```

每个阶段都会在 `stage.json` 里记录输入的哈希。输入没变时再次运行会直接跳过；改了 DEM、范围或网格参数，会自动重算受影响的阶段。

## 5. 输出

| 文件 | 内容 |
|---|---|
| `work/<case_id>/geometry/geometry.json` | 简化后的外边界、孔洞、约束线，面积（m²），坐标系 |
| `work/<case_id>/mesh/mesh.json` | 网格（格式 `triangle-domain-mesh-only-artifact-v1`）：`node_xy`（节点坐标，m）、`face_nodes`（每个三角形的 3 个节点序号）、`face_id`（稳定的面编号）、`mesh_id`、`coordinate_system`，以及 `provenance`（来源、剖分参数、质量统计） |
| `work/<case_id>/mesh/stage.json` | 节点数、单元数、最小质量（1 为正三角形，>0.3 一般可用）、质量 < 0.1 的单元数、最小面积、面积合计 |
| `work/<case_id>/terrain/terrain_faces.npz` | 每个三角形的 `face_id`、`elevation_m`（地面高程）、`coverage`（被 DEM 有效值覆盖的比例） |
| `work/<case_id>/terrain/mapping_report.json` | 地形映射报告 |

## 6. 实测示例（东淀，2026-11-05）

用本项目的东淀外边界和安全区（按第 2 节转换到 EPSG:4548），1 m DEM（26677 × 36813 像元），`default_size_m: 100`：

- `--check`：通过，计算范围 149.06 km²
- `--stage mesh`：用时 11 s；22,802 个节点，43,394 个三角形；最小质量 0.64，没有质量 < 0.1 的单元；网格面积 146.31 km²（扣除安全区）
- `--stage terrain`：1 m DEM 上精确映射较慢，约 8 万单元需要 4–5 h，4 万单元约 2–3 h

- 按第 2 节目录结构、用模板原样（`default_size_m: 50`）运行：用时 33 s；79,916 个节点，156,744 个三角形；最小质量 0.64，没有质量 < 0.1 的单元；面积 146.31 km²

精确地形映射上限 500 000 个单元。

## 7. 常见错误

| 报错 | 原因 | 处理 |
|---|---|---|
| `DEM CRS is not equivalent to dem.crs` | DEM 与配置的坐标系不同 | 在流水线外重投影 DEM（`gdalwarp`），或把 `dem.crs` 改成 DEM 的坐标系 |
| `.prj` / `crs` 与 dem.crs 不等价 | 计算范围的坐标系与 DEM 不同 | 用第 2 节的 `ogr2ogr` 转换 |
| `domain polygon extends outside the DEM bounds` | 计算范围超出 DEM | 缩小范围或换更大的 DEM |
| 合并后不是单个多边形 | 范围由几块不相连的面组成 | 只保留一块，或分成几个方案分别剖分 |
| `stage 'sections' needs sections, forcing, run` | 只剖分配置被用来跑后续阶段 | 只运行 `--stage mesh` / `terrain`，或补全这三项 |
| 地形阶段报 NoData 单元 | 有三角形完全落在 DEM 无数据区 | 修改计算范围，或用孔洞把这些地方挖掉 |
