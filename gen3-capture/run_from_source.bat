@echo off
REM Quick way to run the capture tool without building an .exe
REM (needs Python installed). Handy for testing on your own machine.
cd /d "%~dp0"
py gen3_capture.py
if errorlevel 1 python gen3_capture.py
pause
