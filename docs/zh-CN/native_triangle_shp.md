# 原生三角 P4 结果 Shapefile

`hydrodynamics-triangle-shp` 从已有的 P4-1 原生 NetCDF 结果逐原生面导出一个 polygon feature；该命令不运行 Solver。导出和校验都会调用既有 P4-1 reader，重新核对 run config、当前 P3-A business package、关联的 P2 UGRID 网格、真实原生数组、case、拓扑与地形。首批只支持 `closed_wall`、`initial_momentum=zero`、`sources=none`。仅准备阶段的 UGRID、structured `(time,y,x)` NetCDF、带源项结果、开放边界和仅形状相似的数组都会拒绝。

安装可选依赖并运行小型合成例子：

```powershell
python -m pip install -e ".[triangular,shp]"
hydrodynamics-mesh-prepare examples/native_triangle_shp/mesh_preparation.yaml
hydrodynamics-business-prepare examples/native_triangle_shp/business_preparation.yaml
hydrodynamics-triangular-run examples/native_triangle_shp/run.yaml
hydrodynamics-triangle-shp export --request examples/native_triangle_shp/export_frame.yaml
hydrodynamics-triangle-shp validate --request examples/native_triangle_shp/export_frame.yaml
```

`hydrodynamics-triangle-shp schema` 输出已安装的 request/manifest 合同。请求路径相对于请求文件；输出必须是请求目录下的新子目录，不得通过路径别名绕出。已有输出不覆盖。若 package 被复制到别处，`validate --package-directory` 可以指定副本位置，但仍需要原始 run config、P3-A、P2 网格和原生结果源图。

受控 selector 有两种。`FRAME` 要求 `frame_index` 和 `time_s` 与原生保存时间轴逐项严格一致。`MAX_OVER_WINDOW` 的窗口端点必须是原生保存时刻，端点均计入，不插值；每面并列最大值取最早原生时刻。范围可以是 `FULL_DOMAIN` 或 `MAXIMUM_WET_SURFACE`；后者按选定深度严格执行 `depth > wet_depth_threshold_m`（有限非负，默认0 m）。选择结果为空时仍写出有效的零 feature Polygon 图层。

## Native-web manifest schema / native-web manifest schema

Native-web wire `version` 保持 `1.0`。当前 JSON Schema 修订为 1.0.1，路径为 [`native_web_manifest_v1_0_1.schema.json`](../../examples/native_triangle_shp/native_web_manifest_v1_0_1.schema.json)，`$id` 为 `native-triangle-web-manifest-v1.0.1`。旧的 [`native_web_manifest.schema.json`](../../examples/native_triangle_shp/native_web_manifest.schema.json) 保留原 v1 身份，供历史消费者使用。

`data_origin` 对旧版 v1 manifest 保持可选。出现时只允许 `SYNTHETIC_SMALL_NATIVE_FIXTURE`，并要求 `qa_fixture=true`、`live_solver_ran=false`、`status=qa_fixture_ready`。缺失表示未声明；schema 不设置默认值，也不推断为 live 或 production。

每个 feature 的几何直接来自原生 `face_nodes` 中的三个节点；不转换矩形、不重网格、不拆分、不合并、不重采样。`face_id` 在 DBF 中以精确十进制文本保存，避免超过 `2**53` 时浮点身份损失。`FRAME` 字段是 `time_s`、`depth_m`、`water_lvl`、`ux_m_s`、`uy_m_s` 和 `area_m2`；`MAX_OVER_WINDOW` 字段是 `max_depth`、`max_time` 和 `area_m2`。版本化 manifest 保存字段语义和单位。水位要求当前 P3-A 垂直基准；速度分量由原生动量除以水深计算，仍采用原始投影 CRS 轴方向。

默认输出原生投影 CRS；可选 `EPSG:4490` 仅转换导出的坐标。`area_m2` 始终取自 P4 原生 `face_area`，并与源投影 CRS 中的三角形面积核对；不会将经纬度面积当作平方米。manifest 记录 request/config/P2/P3-A/result 摘要、case/mesh/topology/terrain 身份、selector/时间轴、CRS、垂直基准、原生 face ID 集合摘要和组件 SHA256/字节数。P4-1 不含平台 attempt/script ID，manifest 会准确声明其缺失，不伪造身份。所有产物固定为 `experimental=true`、`engineering_validated=false`、`production_approved=false`、`can_publish=false`。

发布先写入新暂存目录，重命名后再做 fresh 校验。末尾校验失败时，只有输出目录父链及 manifest 文件身份仍属于本次导出，才会退役成功 marker。并发改写的 manifest 会先逐字节复制到唯一的 `manifest.recovered.<id>.json`，再移除 marker。被替换的输出根目录或 manifest inode 会原样保留并在错误中报告位置；不会递归删除输出目录。

四个旧 structured 矩形 SHP writer 入口仍可导入，但会在访问输出路径前明确拒绝。小时级 structured workflow 保留 NC、调闸日志、nc2web、stepjson 和 viewer；SHP 阶段标记为 `not_applicable`，原因是 `native_triangular_result_required`。历史 schema 1.0 structured bundle 可继续发现，并明确分类为 `legacy_structured_rectangular_shp`，不会作为原生三角结果识别。
