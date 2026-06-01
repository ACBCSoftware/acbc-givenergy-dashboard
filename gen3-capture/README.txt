GivEnergy Connection Diagnostic — Real Serial Test
====================================================

This version extracts the real adapter serial from the dongle's heartbeat
and uses it in active requests, which may be what the official library does.

SETUP
-----
1. Open gen3_config.ini in Notepad.
2. Set "ip" to your inverter's IP address. Save and close.
3. CLOSE the GivEnergy app and STOP GivTCP / Home Assistant.


THE TEST — run-sequential.bat
------------------------------
Just double-click run-sequential.bat. That's it.

What it does:
  1. Connects and WAITS silently for the dongle's heartbeat (up to 4 min).
     When it arrives you'll see:
       Heartbeat! Adapter serial extracted: WH2301G954

  2. Tests 6 requests (A-F) one at a time using the real serial:
       TX slave=0x32 func=0x04 base=0 count=60 serial=WH2301G954
     For each one it waits 5s and reports:
       MATCHED in 0.347s     <- this means it worked!
     or:
       no response           <- still timing out

  3. Prints a TEST SUMMARY at the end showing which of A-F responded.

  4. Keeps running to capture background cloud syncs.
     Stop with Ctrl+C when you're done.


WHAT TO SEND BACK
-----------------
Two files:
    capture_<timestamp>.bin
    capture_<timestamp>.log

Both, please. A note of what the summary showed is also helpful.


TROUBLESHOOTING
---------------
- Windows SmartScreen: click "More info" -> "Run anyway".
- "Waiting for heartbeat" for ages: the dongle sends one every ~3 minutes.
  Just leave it — if it never comes, it'll fall back and test anyway.
