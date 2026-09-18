@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
python "%SCRIPT_DIR%rotate_secrets.py" %*
echo.
echo Exit code: %ERRORLEVEL%
pause
