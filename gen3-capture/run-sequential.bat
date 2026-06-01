@echo off
REM THE KEY TEST: behaves like a proper Modbus client.
REM Detects the device first (HR read), then polls one IR request at a time
REM and waits for the exact matching response — exactly like givenergy-modbus.
REM
REM If Gen3/AIO replies arrive in <2 seconds instead of ~5 minutes,
REM this is the fix for the dashboard.
cd /d "%~dp0"
gen3-capture.exe --sequential
