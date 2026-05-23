@echo off
:: Local dev launcher — shows the admin console UI with no login required.
:: Runs on http://localhost:8765 — close this window to stop.

set FLEET_NAME=Test Fleet
set CLOUD_LAB_DIR=%~dp0

echo Starting dev console at http://localhost:8765 ...
echo Close this window to stop.
echo.

:: Open browser after 2s while server starts below.
start /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8765/"

python "%~dp0fleet\management\admin_console.py" --dev
