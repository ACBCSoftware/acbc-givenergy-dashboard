GivEnergy Connection Diagnostic
===============================

Thanks for helping! We're chasing an intermittent "lost connection / no data
for 75 seconds" problem that affects some inverter models. This little tool
talks to your inverter exactly like the dashboard does, but it records how
reliably the inverter replies — which tells us what's going wrong.

It is safe: it only sends a tiny "read" request and listens. It cannot change
any settings on your inverter.


WHAT YOU NEED
-------------
- gen3-capture.exe
- gen3_config.ini   (keep it in the same folder as the exe)


SETUP
-----
1. Open gen3_config.ini in Notepad.
2. Set "ip" to your inverter's IP address. Save and close.


IMPORTANT — BEFORE YOU START
----------------------------
For the first test we need the inverter talking ONLY to this tool, so please:

  * CLOSE the official GivEnergy app on your phone.
  * STOP any other local tools that talk to the inverter (Home Assistant,
    GivTCP, another copy of the dashboard, etc.).

This matters: if something else is polling the inverter at the same time, it
can mask the very problem we're trying to see.


TEST A — on its own (about 10 minutes)
---------------------------------------
1. Double-click gen3-capture.exe. A black window opens.
2. You'll see lines like:
       > poke sent
       REPLY  slave=0x11  SOC=63%  solar=120W  home=480W  (gap ... 0.7s after poke)
   - "REPLY" lines mean the inverter answered. The "slave=0x.." value and the
     SOC/solar/home confirm it's reading your real inverter.
   - Watch the "gap since last reply" — small gaps are good.
   - If you see "!! No reply for NNs — this is the disconnect symptom", that's
     exactly what we're hunting. Let it keep running.
3. Leave it running for about 10 minutes.
4. Press Ctrl+C (or close the window) to stop. It prints a RESULT SUMMARY.


TEST B — with the official app open (about 5 minutes)  [if you have time]
-------------------------------------------------------------------------
This tells us whether the inverter behaves better when the official app is
actively talking to it.

1. Start gen3-capture.exe again (this makes a fresh pair of files).
2. Type a note and press Enter:   opening official app now
3. Open the official GivEnergy app and leave it on the live/power screen.
4. Let it run ~5 minutes, then stop with Ctrl+C.


WHAT TO SEND BACK
-----------------
Each run creates two files named like:
    capture_20260531_201831.bin
    capture_20260531_201831.log

Please send BOTH files from each test. If you did Test A and Test B, that's
four files in total. The .log is readable; the .bin is the raw data.

A one-line note of your inverter MODEL (e.g. "All-In-One", "Gen3 hybrid 3.6")
is also a big help.


TROUBLESHOOTING
---------------
- "Connecting..." but no REPLY lines ever appear:
    * Double-check the IP in gen3_config.ini.
    * Make sure the inverter's dongle is online (the official app shows data).

- Windows SmartScreen ("Windows protected your PC"):
    * Click "More info" then "Run anyway". The tool isn't code-signed yet.

Cheers — this data is exactly what we need to fix it properly.
