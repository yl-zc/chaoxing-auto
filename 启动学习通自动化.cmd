@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "%~dp0runtime\python\pythonw.exe" (
  echo [ERROR] runtime\python\pythonw.exe not found. Package is incomplete.
  pause
  exit /b 1
)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0runtime\python\Lib\site-packages"
start "" "%~dp0runtime\python\pythonw.exe" "%~dp0launcher.py"
exit /b 0
