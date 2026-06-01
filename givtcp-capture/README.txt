GivTCP Library Wire Capture
============================

Uses GivTCP's bundled library (givenergy_modbus_async) — the one that's
KNOWN to work with Gen3/AIO — and captures every byte on the wire.

This shows exactly what GivTCP sends and what comes back, so we can
understand WHY it works when the published library doesn't, and replicate
it for both Gen3 and AIO in the dashboard.


REQUIREMENTS
------------
  Python 3.11+   (you already have 3.14)
  GivTCP source  (you already have it cloned)
  crccheck       (only pip dep — the bat file installs it)


SETUP
-----
1. Open givtcp_config.ini in Notepad.
2. Check/set:
     ip   = your inverter's IP
     path = path to the GivTCP subfolder of your giv_tcp clone
            (the folder that CONTAINS givenergy_modbus_async)
            e.g. D:\Coding\givenergy\git\giv_tcp\GivTCP
3. CLOSE the GivEnergy app. STOP any other GivTCP instances.
   (only one connection to port 8899 at a time)


RUN
---
Double-click  run_givtcp_capture.bat

It will:
  - Install crccheck if needed
  - Connect using GivTCP's library
  - Run detect_plant() — same as GivTCP does on startup
  - Run 6x refresh_plant() at 10-second intervals
  - Log every TX byte and every RX byte with timestamps
  - Takes about 2 minutes total


WHAT TO LOOK FOR
----------------
If detect_plant() gets a FAST response (<2s):
  TX #N  len=34  (Xs since last RX)
    TRANSPARENT serial=AB1234G567 slave=0x11 HR(base=0,count=60)
  RX #N  len=164  (0.347s after last TX)
    TRANSPARENT serial=WH2301G954 slave=0x11 HR(base=0,count=60)

  → The TX frame that got a fast RX is the key. That's what we
    need to replicate in the dashboard.

If it still takes 300s:
  → GivTCP's library is also seeing cloud syncs, not polling directly.
    The "it works" story needs more investigation.


WHAT TO SEND BACK
-----------------
Two files:
    givtcp_<timestamp>.log
    givtcp_<timestamp>.bin

The log is the most important — it shows exact TX/RX frames and timing.
