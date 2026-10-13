# nc2web 解析器 V1.0

对应 `docs/nc2web.png`：后端一次性把水动力 NetCDF 解析为 Web 可加载的三件套。

1. `manifest.json`：时间轴、极值、帧索引、网格元数据；
2. `mesh.bin`：一次加载的节点坐标 + 三角拓扑（量化）；
3. `frame_NNN.bin`：各时刻水深 / uv 二进制帧。

Web 坐标固定声明 `"epsg": "EPSG:4548"`。源文件已是 EPSG:4548 时只做声明，不改网格值。源为经纬度且仍为北向结构化轴时，才用可选 `pyproj` 转换节点坐标。本解析器不启动 Solver、不连接数据库、不把地形高程当成水深、不编造站点时序。

`convert_netcdf_to_web(path, output_dir)` 读取 `(time,y,x)` 或 `(y,x)` 的 `water_depth` / `water_level` / `velocity_x` / `velocity_y`。无这些变量时只写 mesh 并 `warn`。inactive / 非有限值写成 NaN，不填 0。


## 逐步 JSON 解析器

`convert_netcdf_to_step_json` 每个时次写一个 JSON，格式为：

`{"all": ["网格编号_水深_流速_流向", ...]}`

网格编号为 1-based 行优先 `row * nx + col + 1`。字段用下划线拼接。只输出 active 且水深 > 0 的格子。第 0 步若尚未上水，则 all 为空数组。流速为水平合速度；静止时流向为 180。

`parse_netcdf_for_web(nc, bundle_dir)` 会在同一目录下写出：

- `nc2web/`：manifest + mesh.bin + frame_NNN.bin
- `stepjson/`：step_000.json ...
