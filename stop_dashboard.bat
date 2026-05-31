@echo off
REM No "pause" here — this script is also run silently by the uninstaller,
REM and a pause would hang it waiting for a keypress that never comes.
echo Stopping GivEnergy Dashboard...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":7890 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
    echo Stopped PID %%a
)
echo Done.
