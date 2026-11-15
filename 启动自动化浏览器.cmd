@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PROFILE=%~dp0runtime\browser-profile"
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
set "BROWSER="
if exist "%CHROME%" set "BROWSER=%CHROME%"
if not defined BROWSER if exist "%EDGE%" set "BROWSER=%EDGE%"
if not defined BROWSER if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER (
  echo [ERROR] Chrome / Edge not found. Please install Microsoft Edge.
  pause
  exit /b 1
)
start "" "%BROWSER%" --user-data-dir="%PROFILE%" --remote-debugging-port=9222 --no-first-run --no-default-browser-check "https://i.chaoxing.com/base"
exit /b 0
