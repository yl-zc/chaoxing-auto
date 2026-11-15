@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "%~dp0runtime\python\python.exe" (
  echo [ERROR] runtime\python\python.exe not found.
  pause
  exit /b 1
)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0runtime\python\Lib\site-packages"
"%~dp0runtime\python\python.exe" "%~dp0launcher.py" --stop
echo.
pause
