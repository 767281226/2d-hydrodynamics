# 原生 source 结果三角 SHP v2

现有 v1 request、manifest、Python API、默认 `schema` 命令和 `sources=none` 范围保持不变。v1 继续拒绝 Manning、rainfall 和 combined source 结果；source 结果使用独立的 v2 request 与 manifest。

## Request 与 CLI

```yaml
format: native-triangle-result-shp-export-request-v2
schema_version: '2.0'
result_kind: rainfall # manning | rainfall | manning_and_rainfall
run_config_file: source_run.yaml
output_directory: source_result_shp_v2
selector:
  aggregation: FRAME
  frame_index: 2
  time_s: 0.1
  window_start_s: null
  window_end_s: null
scope_selection: FULL_DOMAIN
wet_depth_threshold_m: 0.0
target_crs: NATIVE # 或 EPSG:4490
```

`result_kind` 必填，且必须与顶层 P4-2 配置格式及其 `sources` 一致。路径必须规范化并相对 request 文件。selector、scope、阈值、CRS、保存时刻匹配、精确 int64 面 ID 文本、原始投影面积及几何检查沿用 v1 规则。

默认 `schema` 命令仍输出 v1；source v2 schema 使用显式 `schema-v2`。原有 `export`/`validate` 命令按 request `format` 分派：

```powershell
python -m hydrodynamics.triangular_shp_export schema
python -m hydrodynamics.triangular_shp_export schema-v2
python -m hydrodynamics.triangular_shp_export export --request examples/security_exclusion/export_combined_v2.yaml
python -m hydrodynamics.triangular_shp_export validate --request examples/security_exclusion/export_combined_v2.yaml
```

wheel 单独打包 v2 Schema：`hydrodynamics/shapefile_schemas/triangular_source_result_shp_v2.schema.json`；v1 资源仍是 `triangular_result_shp.schema.json`。

## Source 身份和预算核验

Manning 通过 `read_triangular_manning_result` 读取；rainfall/combined 通过 `read_triangular_source_result` 读取；两者都强制使用配置的 native 结果路径。导出器不调用 runner，也不读取或要求存在 base-none 结果。导出前、stage 校验时和发布后，都会重新读取并核对 request、顶层运行配置、base 配置、source 配置、source preparation、P3 业务包、P2 网格、native 结果及所有安全区证据依赖。manifest 按角色记录路径、字节数和 SHA-256。该包属于 `LOCAL_SOURCE_GRAPH`，不是可移植或自认证的执行证明。

v2 manifest 记录 native 格式、product type、solver version、source family 以及 requested/enabled/applied/nonzero-effect 标志。Manning 来源保留既有 `unit: 1` 声明、SI 解释策略、物理单位和 rationale。写入器独立用 native 面面积积分每个保存时刻的水深，并与 native mass ledger 每行核对；最大同步水量取各保存帧积分的最大值，不把不同时刻的逐面峰值相加。窗口摘要仅取精确选中的保存帧。

降雨区间接受量按 `rate × active 面积 × 区间时长` 独立核对，并与 native ledger 比较。启用安全区时，active 接受雨量和排除区潜在雨量分开保存；后者标为未模拟，不计作已接受预算损失，也不自动转移。零效应 Manning 和全零降雨仍保留真实 family/applied 标志，`nonzero_effect_applied` 为 false。

v2 writer 沿用现有不覆盖、reparse-point、组件摘要、fresh 校验、stage 所有权、成功标记退役和异常传播规则。source 结果仍不受 v1 writer 支持。风险、人口和损失输出仍为 `UNSUPPORTED`，不推断为零。

`examples/security_exclusion/` 包含三种 family 的 v2 request、启用安全区的 rainfall、零效应和动态 Manning/combined 示例。CLI 示例不超过四个面和 0.1 秒，不要求存在 base-none 结果。
