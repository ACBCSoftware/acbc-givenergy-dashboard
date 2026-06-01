GivEnergy Connection Diagnostic
================================

Thanks again — we need two quick tests to pin down exactly what the Gen3
needs. Both take about 10 minutes each. The results will tell us definitively
how to fix the dashboard for Gen3/AIO.

Background: your last captures showed data arriving every ~5 minutes (cloud
sync cadence). The official library gets data every 10 seconds. We need to
find out what it does differently. The answer is probably a "handshake" step
we're missing — that's what Test 2 checks.


SETUP
-----
1. Open gen3_config.ini in Notepad.
2. Set "ip" to your inverter's IP address. Save and close.

As before: CLOSE the official GivEnergy app and STOP GivTCP / Home Assistant
while running these tests.


TEST 1 — normal mode (~10 minutes)
------------------------------------
This is our current approach, as a fresh baseline.

1. Double-click gen3-capture.exe
2. Leave it running for about 10 minutes.
3. You should see data replies arriving roughly every 5 minutes (that's the
   problem we're trying to fix).
4. Stop with Ctrl+C.

What we're looking for:
  REPLY lines arriving every ~300 seconds = cloud cadence (the problem)


TEST 2 — handshake mode (~10 minutes)  [this is the key test]
--------------------------------------------------------------
This adds a "Holding Register" read before the normal data pokes — exactly
what the official library does when it first connects. If this fixes the
5-minute cadence you should see data arriving every 10 seconds instead.

1. Double-click run-handshake.bat  (NOT gen3-capture.exe directly)
2. You'll see:  Handshake: ON — sending HR reads to trigger poll-response mode
3. Leave it running for about 10 minutes.
4. Stop with Ctrl+C.

What we're looking for:
  REPLY lines arriving every ~10 seconds = poll-response mode (the fix!)
  OR still every ~5 minutes = handshake isn't the answer


WHAT TO SEND BACK
-----------------
Each run creates two files named like:
    capture_20260601_140000.bin
    capture_20260601_140000.log

Please send BOTH files from EACH test — 4 files total.

Also handy: a one-liner note of what you saw (data every 10s, or still 5min).


TROUBLESHOOTING
---------------
- Windows SmartScreen warning: click "More info" then "Run anyway".
- No REPLY lines at all: check the IP in gen3_config.ini.

The run-no-ack.bat launcher is also included if needed (disables heartbeat
response — only useful if asked for specifically).

Cheers — this test will crack it.
