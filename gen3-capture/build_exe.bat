@echo off
REM ============================================================
REM  Build gen3-capture.exe  (standalone Windows tool)
REM  Run on a machine with Python installed. Produces a single
REM  .exe in the dist\ folder that the tester can run directly.
REM ============================================================
cd /d "%~dp0"

echo Installing PyInstaller (if needed)...
py -m pip install --quiet --upgrade pyinstaller
if errorlevel 1 (
    echo.
    echo Could not run "py". Trying "python" instead...
    python -m pip install --quiet --upgrade pyinstaller
    if errorlevel 1 (
        echo.
        echo ERROR: Python was not found. Install Python from https://python.org
        echo and tick "Add Python to PATH", then run this again.
        pause
        exit /b 1
    )
    set PYEXE=python
) else (
    set PYEXE=py
)

echo.
echo Building gen3-capture.exe ...
%PYEXE% -m PyInstaller --onefile --console --name gen3-capture gen3_capture.py
if errorlevel 1 (
    echo.
    echo Build failed — see the messages above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Done!
echo  The program is here:  %~dp0dist\gen3-capture.exe
echo.
echo  Copy gen3_config.ini next to the exe (set the inverter IP),
echo  then send dist\gen3-capture.exe + gen3_config.ini to the tester.
echo ============================================================
pause
