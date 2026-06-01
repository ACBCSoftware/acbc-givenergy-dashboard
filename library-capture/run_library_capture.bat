@echo off
echo ============================================================
echo  GivEnergy Library Wire Capture
echo  Requires Python 3.14  (python.org/downloads)
echo ============================================================
echo.

REM Check Python version is 3.14+
python --version 2>&1 | findstr /r "3\.1[4-9]" >nul 2>&1
if errorlevel 1 (
    python --version 2>&1 | findstr /r "3\.[2-9][0-9]" >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python 3.14+ required.
        echo Download from https://python.org/downloads/
        pause
        exit /b 1
    )
)

echo Installing / updating givenergy-modbus...
python -m pip install --upgrade --quiet givenergy-modbus
if errorlevel 1 (
    echo.
    echo pip install failed - check your internet connection.
    pause
    exit /b 1
)

echo.
echo Running capture...
echo.
python library_capture.py
