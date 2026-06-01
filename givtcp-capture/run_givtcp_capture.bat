@echo off
echo ============================================================
echo  GivTCP Library Wire Capture
echo ============================================================
echo.
echo Installing crccheck (GivTCP's only external dependency)...
python -m pip install --quiet --upgrade crccheck
echo.
echo Running capture...
echo.
python givtcp_capture.py
