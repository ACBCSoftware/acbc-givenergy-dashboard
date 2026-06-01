@echo off
REM Runs the diagnostic with heartbeat acknowledgement turned OFF,
REM to reproduce the old "goes dead after a while" behaviour for comparison.
cd /d "%~dp0"
gen3-capture.exe --no-ack
