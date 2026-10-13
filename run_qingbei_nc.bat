@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONPATH=%CD%"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python 3.11+。
    exit /b 1
)
python -c "import sys; assert sys.version_info >= (3,11); import numpy, netCDF4, pyproj, pydantic, yaml" 2>nul
if errorlevel 1 (
    echo [错误] 请先安装依赖: python -m pip install -e ".[workflow]"
    exit /b 1
)

echo 开始完整流程: NC、调闸记录、SHP、nc2web、stepjson、viewer、结果检查
python -m hydrodynamics --workflow "%~dp0examples/case_001/workflow.yaml" %*
if errorlevel 1 (
    echo [失败] 流程未全部完成，请查看上方错误及 run_report.json。
    exit /b 1
)
echo [完成] 所有配置的任务及输出检查均已通过。
exit /b 0
