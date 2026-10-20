# 安全区排除输入

本合同将原计算域 `D` 与安全区并集 `S` 明确分开，实际活动域为 `D \\ S`。安全区被排除在面网格之外；其新边界使用已有 `closed_wall` 语义。不增加堤高、越流、闸控、干湿规则或新的源项物理。

## 输入格式

在 P2 网格配置或工作流配置中，可省略 `security_exclusion`，表示未配置安全区。启用时必须完整提供以下版本化字段：

```yaml
security_exclusion:
  format: security-area-exclusion-v1
  schema_version: '1.0'
  enabled: true
  source_file: security_area.geojson
  source_crs: EPSG:32650
  constraint: excluded_closed_wall
```

`enabled` 是必填布尔值；`source_file` 是相对声明配置文件的非空路径；`source_crs` 是必填 CRS 声明；`constraint` 当前只接受 `excluded_closed_wall`。面积单位为 m²。源几何需为有效、非空 Polygon/MultiPolygon，坐标必须有限；无 CRS、非法几何、自交或不匹配的 Shapefile `.prj` 会报错，不做静默修复。Shapefile 读取只打开 `.shp`、`.shx`、`.prj` 几何与 CRS 文件，不读取 DBF 属性。

若 GeoJSON 根对象含 `crs`，其 `name`/`EPSG` 声明必须能解析且必须与 `source_crs` 等价；冲突、畸形或无法解析的声明会拒绝。没有内嵌 CRS 时只使用必填的 `source_crs`，不猜测。

导入网格另需显式原域 `D`，不能以安全区包络框代替：

```yaml
  domain_geometry_file: domain.geojson
  domain_crs: EPSG:32650
```

该 GeoJSON 使用 `security-domain-geometry-v1`，且其内嵌 `coordinate_system` 必须与 `domain_crs` 声明一致。Gmsh 路线从配置中的外边界和洞构造 `D`，再生成 `D \\ S`。适配器不能表达的多分量或边界切割会给出 `UNSUPPORTED` 错误。导入网格逐面做几何相交和精确域覆盖核验；不会按面中心裁切。

## 身份与执行

P2 保存源文件和 CRS 摘要、规范化几何、原域/排除域/活动域面积、实际活动面集合摘要、覆盖残差及真实安全边界边组。P3 必须与 P2 证据完全一致。P4 的 none、Manning 和 rainfall 入口在执行前重读来源并核验活动面集合；安全输入或网格变化会使旧 P3、结果和 SHP 导出失效。

常数初始水深只定义在活动面上。逐面字段若引用已排除面 ID 会报错；不会删除后重分配或对旧全域体积重新平均。均匀雨量按实际活动面积计入源项。安全区上的潜在雨量另以 `security_rainfall_accounting.security_excluded_unmodeled_rainfall_volume_m3` 报告；它是未模拟雨量，不是已接受水量预算的损失，也不自动转移到活动域。

原生结果只含活动面。native SHP manifest 绑定安全证据摘要和同一活动 face ID 集合。结构化 `nc2web`/stepjson 不能表示三角安全区结果，会明确拒绝。尚未实现的风险、人口或损失统计保持 `UNSUPPORTED`，不能报告为零。

`examples/security_exclusion/` 是小型合成两室案例（4 面、0.1 s），包含导入网格、原域、排除几何、P2/P3 配置、none 与 rainfall 配置及 SHP 请求。所有路径都相对该目录。`examples/case_001/workflow.yaml` 已显式引用 `../../map/securityArea/dd_securityArea_shape.shp`；当前 structured/hourly 工作流会在模型阶段前以 `UNSUPPORTED_STRUCTURED_SECURITY_EXCLUSION` 拒绝，避免无保护运行。
