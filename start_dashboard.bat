@echo off
REM Run from this script's own folder, wherever it was installed.
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo.
    echo ERROR: Python environment not found in this folder:
    echo   %~dp0
    echo.
    echo The dashboard may not have installed correctly.
    echo Try reinstalling, or run setup again.
    echo.
    pause
    exit /b 1
)

echo Starting GivEnergy Dashboard...
echo.
"venv\Scripts\python.exe" "dashboard_server.py"
pause
