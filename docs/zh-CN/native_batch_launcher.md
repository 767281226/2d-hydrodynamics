# 原生三角批处理启动器

`run_qingbei_nc.bat` 现在调用 `hydrodynamics.native_batch_launcher`。默认只运行已验收的安全区合成 combined 案例，并明确标记为 `SYNTHETIC_ONLY`，不会启动旧 structured/hourly 流程。

## ID 与输出目录

支持 `--id` 和别名 `--script-id`。显式 ID 原样通过现有 `resolve_script_id`/`validate_run_id` 校验；未提供时生成北京时间 `YYYYMMDD-<Unix纳秒>`。默认输出为 `work/results/<script_id>`。相对 `--out-dir` 以项目根目录为基准，绝对路径也可用。已有目录、文件、链接或 junction 一律拒绝，不覆盖。

`run_qingbei_nc.bat` 使用项目 `.venv\Scripts\python.exe` 和 `-X utf8`，通过 `pushd`/`popd` 恢复调用时目录。缺少虚拟环境或依赖时打印可执行安装命令，不自动安装或访问网络。未知旧参数明确拒绝，不会悄悄进入 structured 流程。

## 合成执行

严格的 `native-batch-launch-config-v1` 只选择 `examples/security_exclusion/` 的四面、三帧、0.1 秒合成案例。启动器把固定 JSON/GeoJSON/YAML 白名单复制到新运行目录，依次执行 P2 网格准备、P3 动态业务准备、combined source 准备、现有 P4 source runner、v2 MAX/EPSG:4490 与 FRAME/native SHP 导出及验证。不会运行或要求 `base_none_dynamic.nc`。`summary.json` 和 `run_report.json` 记录 case/script ID、源项标志、native 水量与 ledger、active/excluded 面积、接受和未模拟雨量、模块来源及各阶段真实退出码。可选预览为标准库 SVG，使用 native 面节点和已核验安全区几何。

失败时报告保留失败阶段和真实退出码，不能标为 complete。启动器不会连接数据库、HTTP，不开 viewer server 或 GUI。

## 工程预检

`--engineering` 使用严格的 `native-engineering-preflight-v1` 模板。20 个证据字段覆盖原域 D/CRS、native mesh CRS、DEM 来源 CRS/垂直单位与基准/NoData/覆盖、active 面初值、Manning 来源及 SI 解释、降雨来源与时段、边界分组/方向/时间/单位/基准/来源、native 准备与运行配置和验证依据，并保留启用的安全区声明。

预检仅检查路径和文件是否存在；不打开 DEM、NPZ、CSV、DBF 或矢量几何，不生成网格，不调用 solver。结果始终为 `preflight_only=true`、`execution_available=false`；case_001 的入出流要求会报告 `UNSUPPORTED_OPEN_BOUNDARY_EXECUTION`。即使模板填完整，本批也不会执行工程计算。

`--schema` 输出严格的启动、工程预检和报告 Schema，静态文件为 `examples/native_batch/native_batch_launcher.schema.json`。模块和输入示例要求项目本地 checkout；仅安装 wheel 而没有示例输入树时会明确报错。

## Windows 中文 NetCDF 路径

部分 Windows netCDF4/HDF5 构建无法直接打开中文路径，而 Python 文件 API 支持该路径。对于非 ASCII 路径，P2 UGRID 和 native source NetCDF 读写边界会使用短时 ASCII 临时文件。文件按 1 MiB 缓冲分块复制，不会把整个 NetCDF 文件无界读入内存。读取时仍由现有严格校验器检查原始字节。

`clobber=False` 使用原子且不覆盖目标的重命名发布；目标已存在或暂存期间新出现时都会保留目标原字节。`clobber=True` 使用原子替换。`r+` 会先准备并同步完整暂存副本，再核对原文件身份；按目标路径建立的 Windows mutex 会在该复核至替换期间串行化使用本适配器的发布者。这只协调本适配器管理的写入，不是针对任意外部进程的 compare-and-swap。身份复核与替换之间仍存在一个很短的窗口：不遵守本适配器的外部写入者可能在此时修改目标；因此其他 `r+` 调用者必须在操作期间独占控制目标。启动器只在每次运行的唯一临时结果上使用 `r+`，随后执行既有严格回读及最终不覆盖发布。观察到文件身份变化时拒绝发布并保留当前目标。成功和失败都会清理临时文件。临时工作区或发布失败时保留底层 I/O 错误，运行不得标记为 complete。此路径适配不改变 NetCDF 格式、source graph 或数值行为。
