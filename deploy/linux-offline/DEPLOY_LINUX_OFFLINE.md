# hydro-pipeline 无网络 Linux 部署方案

适用对象：`hydro-pipeline`（通用二维三角网格洪水演进流水线）。
部署方式：一个离线包，里面带便携 Python、全部依赖 wheel、项目源码和安装脚本。目标机全程不需要联网，也不需要编译。

---

## 1. 离线包内容

文件名：`hydro-pipeline-offline-linux-x86_64.tar.gz`（约 257 MB；同目录下的 `.sha256` 文件是校验值）

```
hydro-pipeline-offline-linux-x86_64/
├── DEPLOY_LINUX_OFFLINE.md     本文档
├── install.sh                  一键安装（离线）
├── run_case.sh                 运行方案
├── start_viewer.sh             启动结果查看器（只监听 127.0.0.1）
├── requirements-linux.txt      精确版本清单（30 个包）
├── SHA256SUMS                  包内全部文件的校验值
├── python.tar.gz               便携 CPython 3.12.16（python-build-standalone，install_only）
├── wheels/                     30 个 wheel，manylinux x86_64，cp312
└── project/                    hydro-pipeline 源码、测试、文档、示例
```

## 2. 目标机要求

| 项目 | 要求 |
|---|---|
| CPU 架构 | x86_64（鲲鹏、飞腾等 aarch64 机器要另打一个包，见第 9 节） |
| glibc | ≥ 2.28。例如 RHEL/CentOS/Rocky/Alma 8+、Ubuntu 20.04+、Debian 10+、麒麟 V10、统信 UOS 20。**CentOS 7（glibc 2.17）不支持** |
| 系统 Python | 不需要，包里自带 |
| 编译器 | 不需要 |
| 磁盘 | 安装约 1.2 GB。结果另算：8 万单元、288 h 的方案约 5–10 GB，导出前会先检查磁盘空间 |
| 内存 | 8 万单元建议 ≥ 8 GB，50 万单元建议 ≥ 32 GB |
| 图形界面 | 不需要。但网格生成（gmsh）要用到一组 X11/OpenGL 客户端库，见第 4 节 |
| 权限 | 安装到 `/opt` 时需要 root；普通用户可以安装到 home 下任意目录 |

在目标机上查看 glibc 和架构：
```
ldd --version | head -1
uname -m
```

## 3. 安装步骤

```bash
# 1) 拷入离线包（U 盘、内网文件服务器等），先校验
sha256sum -c hydro-pipeline-offline-linux-x86_64.tar.gz.sha256

# 2) 解压
tar -xzf hydro-pipeline-offline-linux-x86_64.tar.gz
cd hydro-pipeline-offline-linux-x86_64

# 3) 安装（默认装到 /opt/hydro-pipeline；可指定其他目录）
sudo ./install.sh                       # 或：./install.sh $HOME/hydro-pipeline
```

`install.sh` 依次执行：
1. 检查架构和 glibc 版本；用 SHA256SUMS 校验包内文件
2. 解压便携 Python 到 `PREFIX/python`
3. 创建虚拟环境 `PREFIX/venv`，用 `pip install --no-index --find-links wheels` 离线安装依赖
4. 把项目复制到 `PREFIX/app` 并安装成 `hydro_pipeline` 包
5. 逐个导入依赖做检查；gmsh 缺系统库时只提示，不中断安装
6. 自检：用合成 DEM 跑 7 项测试，包括一次 1 小时的全流程计算，约 30 秒。设置 `SKIP_TESTS=1` 可跳过

安装后的目录：
```
/opt/hydro-pipeline/
├── python/   便携 Python
├── venv/     虚拟环境（全部依赖）
├── app/      项目源码、文档、示例、测试
├── run_case.sh
└── start_viewer.sh
```

## 4. gmsh 需要的系统库（只影响网格生成）

Linux 版 gmsh 的 `libgmsh.so` 依赖下面这些系统库（从 wheel 的 ELF 依赖表中读出）：

`libGLU.so.1 libGL.so.1 libX11.so.6 libXrender.so.1 libXcursor.so.1 libXfixes.so.3 libXext.so.6 libXft.so.2 libXinerama.so.1 libfontconfig.so.1 libgomp.so.1 libstdc++.so.6 libz.so.1`

- 它们只是客户端库，**不需要显示器，也不需要启动 X 服务**。流水线不会打开 gmsh 窗口。
- 带桌面的系统一般已经装好。最小化安装的服务器常常缺 `libGLU`、`libXft`、`libXinerama`、`libXcursor`。

检查缺哪些：
```bash
ldd /opt/hydro-pipeline/venv/lib/libgmsh.so* | grep "not found"     # 没有输出表示齐全
```
（`install.sh` 也会自动执行这一步，并列出缺少的库。）

### 方案 A：离线补装系统库

先在一台**同发行版、同版本、能联网**的机器上下载安装包，再拷到目标机安装。

**RHEL / CentOS / Rocky / 麒麟 V10（dnf/yum）**
```bash
# 联网机器
mkdir rpms && cd rpms
dnf download --resolve --alldeps mesa-libGLU mesa-libGL libXrender libXcursor libXfixes libXext libXft libXinerama fontconfig libgomp
# 目标机
sudo dnf install --disablerepo='*' ./*.rpm       # 或 sudo rpm -Uvh --replacepkgs ./*.rpm
```

**Ubuntu / Debian / 统信 UOS（apt）**
```bash
# 联网机器（用干净缓存，只收集缺少的包）
sudo apt-get clean
sudo apt-get install --download-only --reinstall libglu1-mesa libgl1 libxrender1 libxcursor1 libxfixes3 libxext6 libxft2 libxinerama1 libfontconfig1 libgomp1
mkdir debs && cp /var/cache/apt/archives/*.deb debs/
# 目标机
sudo dpkg -i debs/*.deb
```

### 方案 B：在 Windows 上生成网格，拷到 Linux 计算

流水线各阶段的缓存（`work_dir/*/stage.json`）记录的是相对路径，输入不变就直接复用，所以整个 `work_dir` 可以跨机器、跨系统拷贝：

1. 在 Windows 上用**相同的** case 配置、DEM 和计算范围文件运行：`--stage terrain`（或 `--stage sections`）
2. 把 case 配置、DEM、范围和流量文件，连同整个 `work_dir`，按相同的相对目录结构拷到 Linux
3. 在 Linux 上运行 `./run_case.sh case.yaml`：网格和地形会命中缓存，直接开始计算

注意：只要配置里的网格或范围参数改了一点，就会重新生成网格，这时 Linux 上仍然需要 gmsh。

## 5. 运行

```bash
cd /data/cases/my_case                         # 放 case.yaml、DEM、范围文件和流量 CSV 的目录
/opt/hydro-pipeline/run_case.sh case.yaml --check            # 轻量预检
/opt/hydro-pipeline/run_case.sh case.yaml --stage terrain    # 只做网格和地形（耗时阶段）
nohup /opt/hydro-pipeline/run_case.sh case.yaml > run.log 2>&1 &   # 全流程，断开 SSH 后继续运行
tail -f run.log
/opt/hydro-pipeline/run_case.sh case.yaml --resume           # 中断后从最新检查点续算
```

- 配置格式见 `/opt/hydro-pipeline/app/docs/case_pipeline.md`，示例见 `app/examples/case_pipeline/case.example.yaml`
- 长时计算建议用 `nohup`、`tmux`，或 `systemd-run --user --unit=hydro-xxx ./run_case.sh case.yaml`，避免 SSH 断开导致中断
- 默认 `kernel: auto`，有 numba 时自动使用；离线包已带 numba 0.68.1，和纯 numpy 路径结果逐位一致
- 参考耗时：8 万单元时约 0.0065 s/步，288 h 约 20–30 h；1 m DEM 上精确映射 8 万单元约 4–5 h

## 6. 查看结果

结果在 `<output.root>/<run_id>/bundle`，里面有 NetCDF、nc2web、三角形 SHP、stepjson 和网页查看器。

- **SSH 隧道（推荐）**：服务器上运行 `/opt/hydro-pipeline/start_viewer.sh <结果目录>/<run_id>`；在自己电脑上运行 `ssh -L 8788:127.0.0.1:8788 user@服务器`，然后在本地浏览器打开脚本打印的地址
- **拷回 Windows**：把 `<run_id>` 目录拷到 Windows 项目的 `work/results/` 下，运行 `start_viewer.bat <run_id>`
- 查看器只监听 127.0.0.1，不对外开放端口

## 7. 升级和卸载

- 升级：用新离线包重新运行 `install.sh <同一 PREFIX>`。`python/`、`venv/`、`app/` 会被覆盖；方案目录和结果放在 PREFIX 之外，不受影响
- 卸载：`sudo rm -rf /opt/hydro-pipeline`

## 8. 常见问题

| 现象 | 原因和处理 |
|---|---|
| `glibc >= 2.28 required` | 系统太旧（如 CentOS 7），换 8 及以上版本，或在容器中运行 |
| `bundle is for x86_64, machine is aarch64` | 架构不对，按第 9 节打 aarch64 包 |
| `sha256sum: WARNING ... did NOT match` | 拷贝过程中文件损坏，重新拷贝 |
| 导入 gmsh 报 `libGLU.so.1: cannot open shared object file` | 按第 4 节补装系统库，或用方案 B |
| `terrain mapping failed: ... zero valid DEM overlap ... NoData is never filled` | 计算范围超出 DEM 或落在 NoData 上；缩小范围或换覆盖完整的 DEM |
| `DEM CRS is not equivalent to dem.crs` | DEM 坐标系和配置不一致；流水线不做坐标转换，需要先在外部转换 |
| `source vertical unit is missing` | DEM 缺高程单位元数据；在配置里同时给出 `dem.vertical_unit: m` 和 `dem.vertical_unit_evidence` |
| 计算慢 | 确认日志中 kernel 为 numba；检查 `run.cfl_number`（默认 0.5）和网格最小单元尺寸 |
| `disk preflight: need about ... bytes` | 磁盘不足；换 `output.root` 或清理空间 |

## 9. 重新打包（在联网机器上）

```bash
cd hydro-pipeline/deploy/linux-offline
python build_offline_bundle.py                    # x86_64
python build_offline_bundle.py --arch aarch64     # 鲲鹏/飞腾等 ARM 服务器
```
打包脚本会下载最新的便携 Python 3.12 和 `requirements-linux.in` 中各依赖的 manylinux wheel（只要二进制包），复制项目源码，生成 `requirements-linux.txt`、`SHA256SUMS` 和 tar.gz。Windows 或 Linux 上都能运行，需要 Python ≥ 3.11 并能联网。

## 10. 依赖清单（x86_64，cp312）

| 包 | 版本 | 用途 | wheel 大小 |
|---|---|---|---|
| CPython（python-build-standalone） | 3.12.16 | 运行时 | 65 MB |
| numpy | 2.5.4 | 数组计算 | 16.0 MB |
| numba / llvmlite | 0.68.1 / 0.50.0 | 求解器加速 | 3.7 / 56.9 MB |
| rasterio | 1.5.2 | 读 GeoTIFF（内含 GDAL） | 37.4 MB |
| gmsh | 4.15.2 | 三角网格生成 | 38.2 MB |
| netCDF4 / cftime | 1.7.5.1.post1 / 1.6.6.1 | 结果 NetCDF（内含 HDF5） | 10.1 / 1.6 MB |
| pyproj | 3.8.0 | 坐标系判断、导出到 EPSG:4490（内含 PROJ 数据） | 9.6 MB |
| shapely | 2.2.0 | 几何运算（内含 GEOS） | 2.2 MB |
| pyshp | 2.4.2 | 读写 SHP | <0.1 MB |
| pydantic / pydantic-core | 2.14.2 / 2.50.2 | 配置校验 | 0.5 / 2.1 MB |
| PyYAML | 6.0.3 | 读 YAML 配置 | 0.8 MB |
| tzdata | 2026.6 | 时区 | 0.3 MB |
| pytest | 8.4.2 | 安装后自检 | 0.4 MB |
| setuptools / wheel | 84.2.1 / 0.48.0 | 离线安装项目本身 | 1.2 MB |
| 其余传递依赖 | 见 requirements-linux.txt | affine、attrs、certifi、click、colorama、iniconfig、packaging、pluggy、pygments、pyparsing、typing-extensions、typing-inspection、annotated-types | 合计约 3 MB |

需要系统自带的库：glibc ≥ 2.28、libstdc++、libgcc_s、zlib（各发行版都有），以及第 4 节列出的 gmsh 图形客户端库。

## 11. 验证情况

- 已验证：在 Windows 上，项目源码 7 项测试全部通过，其中包括合成 DEM（EPSG:32650）的全流程计算、导出、校验，以及 work_dir 换目录后缓存仍然命中。离线包可以正常生成，30 个 wheel 都是 manylinux x86_64 / cp312 的二进制包。
- **未验证**：本机没有 Linux 环境，还没有在真实 Linux 上运行过 `install.sh`。首次部署时请完整跑一遍 `install.sh`（包括自检），把输出发回来；如果有问题，我按输出修改脚本或补依赖。
