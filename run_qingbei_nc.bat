@echo off
setlocal EnableExtensions
chcp 65001 >nul
pushd "%~dp0" >nul
if errorlevel 1 goto pushd_failed

set "PROJECT_ROOT=%CD%"
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" goto missing_venv

"%PYTHON%" -X utf8 -c "import sys; assert sys.version_info >= (3,11); import pydantic, yaml, numpy, netCDF4, pyproj, shapely, shapefile" >nul 2>nul
if errorlevel 1 goto missing_dependencies

echo [native-batch] Using project virtual environment: %PYTHON%
echo [native-batch] Default mode is SYNTHETIC_ONLY; --engineering is preflight-only.
"%PYTHON%" -X utf8 -m hydrodynamics.native_batch_launcher %*
set "EXIT_CODE=%ERRORLEVEL%"
goto finish

:missing_venv
echo [error] Project virtual environment not found: %PYTHON%
echo Create it with: python -m venv .venv
set "EXIT_CODE=2"
goto finish

:missing_dependencies
echo [error] Project virtual environment is missing native-triangle dependencies.
echo Install with: "%PYTHON%" -m pip install -e ".[triangular,shp]"
echo This batch file does not download or install packages.
set "EXIT_CODE=2"
goto finish

:pushd_failed
echo [error] Could not enter the directory containing this batch file.
endlocal & exit /b 1

:finish
popd >nul
endlocal & exit /b %EXIT_CODE%
