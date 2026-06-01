@echo off
REM GivTCP burst mode — sends the EXACT same 5 frames GivTCP's fork
REM sends for Gen3 (IR(0,60), IR(180,60), HR(0,60), HR(60,60), HR(120,60))
REM all at slave 0x11, in rapid burst every 10 seconds.
REM
REM HR(60,60) and HR(120,60) are frames we had NEVER tried before.
REM GivTCP's approach is KNOWN to work — this finds out why.
cd /d "%~dp0"
gen3-capture.exe --givtcp
