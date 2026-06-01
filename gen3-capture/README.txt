GivEnergy Connection Diagnostic
================================

Thanks again — one more test. This one is the most important yet.

Previous tests showed data arriving every ~5 minutes (cloud sync cadence).
The official library gets data every 10 seconds. The difference is HOW it
asks: the library sends ONE request and waits for that exact response. Our
tool was sending bursts of requests and listening for anything. This new
"sequential" mode tests whether that difference is what matters.


SETUP
-----
1. Open gen3_config.ini in Notepad.
2. Set "ip" to your inverter's IP address. Save and close.

CLOSE the official GivEnergy app and STOP GivTCP / Home Assistant first.


THE TEST — run-sequential.bat  (~10-15 minutes)
------------------------------------------------
This is the only test you need to run this time.

1. Double-click  run-sequential.bat
2. You'll see:  SEQUENTIAL MODE — behaving like a proper Modbus client
3. First it tries to detect the device (HR read). You'll see either:
     DETECTION OK (x.xxs): HYBRID_GEN3  DTC=0x2003  ARM_fw=312
   or "no HR response" — either is fine, it continues to the IR test.
4. Then it starts sending IR requests one at a time and timing the replies:
     > IR request sent to 0x11 ... REPLY in 0.347s  ← FAST — sequential polling IS working!
   OR
     > IR request sent to 0x11 ... NO RESPONSE (>2s timeout)
5. Run it for 10-15 minutes.
6. Stop with Ctrl+C.

WHAT WE'RE LOOKING FOR:
  Fast replies (<2s) every 10s  = sequential polling is the fix — great news!
  Timeouts or still ~5 min gaps = the dongle won't respond regardless of approach


WHAT TO SEND BACK
-----------------
Two files are created each run:
    capture_<timestamp>.bin
    capture_<timestamp>.log

Please send both. A one-line note of what you saw (fast/slow/timeouts) helps.


TROUBLESHOOTING
---------------
- Windows SmartScreen: click "More info" then "Run anyway".
- No output at all: check the IP in gen3_config.ini.

The other launchers (run-handshake.bat, run-no-ack.bat) are only needed
if asked for specifically.

This is the last piece of the puzzle — appreciate your patience!
