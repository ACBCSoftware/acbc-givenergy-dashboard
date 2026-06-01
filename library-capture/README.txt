GivEnergy Library Wire Capture
================================

This tool uses the official givenergy-modbus library to talk to your
inverter and captures every byte sent and received on the wire.

It answers the question: does the library get immediate (<2s) poll
responses from Gen3/AIO, or is it also seeing 5-minute cloud syncs?
If the library gets fast responses, the raw TX frames will show us
exactly what it sends that our custom code doesn't.

REQUIREMENTS
------------
  Python 3.14+   https://python.org/downloads/
  Internet access (to pip install the library)

  This is Windows-only. You need Python 3.14 specifically — the
  library requires it.


SETUP
-----
1. Copy gen3_config.ini.example to gen3_config.ini
2. Open gen3_config.ini in Notepad, set your inverter's IP, save.
3. CLOSE the GivEnergy app and STOP GivTCP / Home Assistant.


RUN
---
Double-click  run_library_capture.bat

It will:
  - Install/update givenergy-modbus automatically (needs internet)
  - Connect to your inverter
  - Run detect() to identify the device
  - Run 6 refresh() cycles at 10-second intervals
  - Log every TX byte and every RX byte with timestamps

The whole thing takes about 2 minutes.


WHAT TO SEND BACK
-----------------
Two files are created:
    libcapture_<timestamp>.log
    libcapture_<timestamp>.bin

Please send both. The key thing we're looking for in the log:

  If you see lines like:
    RX  (0.347s after last TX)
    func=02/TRANSPARENT  slave=0x11  IR(base=0,count=60)
  → the library is getting FAST poll responses — the TX frames
    show us what we're missing.

  If RX chunks only arrive every ~300s:
  → the library is also seeing cloud syncs, not polling directly.


TROUBLESHOOTING
---------------
"Python 3.14+ required":
  Install from https://python.org/downloads/ and tick
  "Add Python to PATH" during install.

"pip install failed":
  Check you have internet access, then try running:
    python -m pip install givenergy-modbus
  in a command prompt.

"detect() FAILED":
  Check the IP in gen3_config.ini. Make sure the inverter is
  reachable (the GivEnergy app shows data from it).
