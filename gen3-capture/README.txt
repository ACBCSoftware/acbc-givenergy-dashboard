GivEnergy Connection Diagnostic  (heartbeat fix test)
=====================================================

Thanks again for testing! We found the cause of the "drops out, comes back,
drops out" problem: the inverter's data dongle sends a little "heartbeat" every
few minutes and expects the software to answer it. If it isn't answered, the
dongle stops replying for a while — that's the dropout.

This updated tool now answers those heartbeats. We'd love you to confirm it
keeps a rock-solid connection where the old version went dead.

It's safe: it only reads data and replies to the dongle's heartbeat. It cannot
change any settings on your inverter.


WHAT YOU NEED
-------------
- gen3-capture.exe
- gen3_config.ini        (keep it in the same folder as the exe)
- run-no-ack.bat         (only used for the optional second test)


SETUP
-----
1. Open gen3_config.ini in Notepad.
2. Set "ip" to your inverter's IP address. Save and close.

Please CLOSE the official GivEnergy app and stop any other tools that talk to
the inverter (Home Assistant, GivTCP, another dashboard) while you test.


TEST 1 — the fix (please run this — about 15 minutes)
-----------------------------------------------------
1. Double-click gen3-capture.exe. A black window opens.
2. Near the top you should see:  Heartbeat acknowledgement: ON
3. You'll see a "REPLY ..." line every ~10 seconds, and every few minutes:
       HEARTBEAT received  ->  ACK sent
4. WHAT WE'RE HOPING FOR: it keeps replying steadily for the whole 15 minutes,
   with NO "!! No reply for NNs" warnings and no long dead patches.
5. Press Ctrl+C (or close the window) to stop. It prints a RESULT SUMMARY —
   "Gaps over 30s: 0" is the result we want.


TEST 2 — the old behaviour, for comparison (optional but really useful)
-----------------------------------------------------------------------
This runs the SAME tool but with the heartbeat answer switched OFF, so we can
show the contrast.

1. Double-click  run-no-ack.bat
2. It will show:  Heartbeat acknowledgement: OFF (--no-ack)
3. Leave it ~10 minutes. We expect it to go quiet / dead after a short while,
   like the old version did.
4. Stop with Ctrl+C.


WHAT TO SEND BACK
-----------------
Each run creates two files named like:
    capture_20260601_071058.bin
    capture_20260601_071058.log

Please send BOTH files from each test (so 2 files for Test 1, or 4 if you also
did Test 2). The .log is readable; the .bin is the raw data.


TROUBLESHOOTING
---------------
- "Connecting..." but no REPLY lines ever appear:
    * Double-check the IP in gen3_config.ini.
    * Make sure the inverter's dongle is online (the official app shows data).

- Windows SmartScreen ("Windows protected your PC"):
    * Click "More info" then "Run anyway". The tool isn't code-signed yet.

Cheers — this is the last piece we need to confirm the fix.
