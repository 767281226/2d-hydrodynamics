@echo off
setlocal EnableExtensions
set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" goto missing_python
cd /d "%PROJECT_ROOT%"
if errorlevel 1 goto bad_root
if "%~1"=="" goto latest
goto explicit

:latest
"%PYTHON%" -X utf8 -m hydrodynamics.viewer_server --project-root "%PROJECT_ROOT%"
goto finish

:explicit
"%PYTHON%" -X utf8 -m hydrodynamics.viewer_server --project-root "%PROJECT_ROOT%" "%~1"
goto finish

:missing_python
echo [ERROR] Project .venv Python is missing: "%PYTHON%"
exit /b 1

:bad_root
echo [ERROR] Cannot enter project directory: "%PROJECT_ROOT%"
exit /b 1

:finish
exit /b %errorlevel%
