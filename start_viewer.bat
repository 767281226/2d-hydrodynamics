@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "PORT=8788"
set "URL=http://127.0.0.1:%PORT%/viewer.html?v=10"
set "VIEWDIR=%~1"

echo ============================================
echo  东淀清北 淹没渲染服务
echo  地址 %URL%
echo  关闭本窗口即停止服务
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.11+ 并加入 PATH。
    goto :fail
)

if "%VIEWDIR%"=="" (
    for /f "delims=" %%I in ('python -m hydrodynamics --show-latest 2^>nul') do set "VIEWDIR=%%I"
)
if "%VIEWDIR%"=="" set "VIEWDIR=work\results\dongdian_qingbei_2023_288h"

if exist "%VIEWDIR%\viewer.html" goto :ready
if not "%~1"=="" if exist "work\results\%~1\viewer.html" (
    set "VIEWDIR=work\results\%~1"
    goto :ready
)

echo [错误] 未找到 viewer.html
echo         默认目录: work\results\dongdian_qingbei_2023_288h
echo         也可传入结果目录或 id，例如:
echo         start_viewer.bat run01
echo         start_viewer.bat D:\out\run01
goto :fail

:ready
for %%I in ("%VIEWDIR%") do set "VIEWDIR=%%~fI"
if not exist "%VIEWDIR%\nc2web\manifest.json" (
    echo [警告] 未找到 nc2web\manifest.json ，页面可能无法加载帧。
)

echo 服务目录: %VIEWDIR%
echo 浏览器将打开: %URL%
echo.

start "" "%URL%"
cd /d "%VIEWDIR%"
python -m http.server %PORT% --bind 127.0.0.1
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo [失败] 渲染服务未启动。
pause
exit /b 1
