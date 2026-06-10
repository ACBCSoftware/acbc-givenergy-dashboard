@echo off
REM SLOT REGISTER HUNT (All in One — issue #21)
REM Sweeps the holding-register ranges where charge/discharge schedule
REM slots could live and flags every value that looks like an HH:MM time.
REM
REM BEFORE RUNNING: set distinctive schedule times in the GivEnergy app
REM (e.g. charge 01:00-01:30 and 02:00-02:30, discharge 18:00-19:00 and
REM 20:00-21:00) so the slot registers are unmistakable in the dump.
REM
REM Finishes by itself in about a minute.
cd /d "%~dp0"
gen3-capture.exe --slots
pause
