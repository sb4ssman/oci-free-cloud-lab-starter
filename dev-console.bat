@echo off
REM Local dev launcher - shows the admin console UI with no login required.
REM Runs on http://localhost:8765 - close this window to stop.

set "FLEET_NAME=Test Fleet"
set "CLOUD_LAB_DIR=%~dp0"

REM Clear stale dev-console listeners so the browser always sees this checkout.
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":8765 .*LISTENING"') do (
  if not "%%P"=="0" (
    echo Stopping stale dev console process %%P ...
    taskkill /PID %%P /F >nul 2>nul
  )
)

echo Starting dev console at http://localhost:8765 ...
echo Close this window to stop.
echo.

REM Open browser after 2s while server starts below.
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8765/"

python "%~dp0fleet\management\admin_console.py" --dev
