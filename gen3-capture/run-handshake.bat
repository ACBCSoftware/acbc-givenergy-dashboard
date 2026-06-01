@echo off
REM Runs the diagnostic with the handshake mode ON.
REM This sends Holding Register reads alongside the normal pokes,
REM mimicking what the givenergy-modbus library does at startup.
REM If data starts arriving every 10s instead of every 5 minutes,
REM the handshake is what Gen3/AIO needs.
cd /d "%~dp0"
gen3-capture.exe --handshake
