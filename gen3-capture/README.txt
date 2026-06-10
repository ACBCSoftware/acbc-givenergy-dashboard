GivEnergy Connection Diagnostic — Real Serial Test
====================================================

Tests whether using the inverter dongle's real adapter serial (extracted
from its heartbeat) makes it respond to local requests.

Available for BOTH Windows and Raspberry Pi / Linux.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RASPBERRY PI / LINUX
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Requirements: Python 3 (already installed on any Pi running the dashboard).
No pip packages needed — only Python stdlib is used.

STEP 1 — Download the script (one command, SSH into your Pi first):

    cd ~ && curl -fsSL https://raw.githubusercontent.com/ACBCSoftware/acbc-givenergy-dashboard/main/gen3-capture/gen3_capture.py -o gen3_capture.py

STEP 2 — Create a config file with your inverter's IP:

    cat > gen3_config.ini << 'EOF'
    [inverter]
    ip   = 192.168.x.x
    port = 8899
    EOF

  (Replace 192.168.x.x with your inverter's real IP address.)
  If you skip this step the script will ask for the IP when it starts.

STEP 3 — Run the sequential test:

    python3 gen3_capture.py --sequential

  Or to stop it with a note already in-place, just Ctrl+C when done.

STEP 4 — Send back the two output files:

    capture_<timestamp>.bin
    capture_<timestamp>.log

  Copy them off the Pi however is easiest (scp, USB, email, etc.).


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WINDOWS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SETUP:
1. Open gen3_config.ini in Notepad, set your inverter's IP, save.
2. CLOSE the GivEnergy app and STOP GivTCP / Home Assistant.

RUN:  Double-click  run-sequential.bat

  (Not gen3-capture.exe directly — the .bat passes the right flags.)

SEND BACK:  capture_<timestamp>.bin  +  capture_<timestamp>.log


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLOT REGISTER HUNT (All in One owners)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Finds where your inverter keeps its charge/discharge schedule slots.

1. In the GivEnergy app, set distinctive schedule times first, e.g.:
     charge slot 1    01:00 - 01:30
     charge slot 2    02:00 - 02:30
     discharge slot 1 18:00 - 19:00
     discharge slot 2 20:00 - 21:00
2. Double-click  run-slots.bat
3. It probes a handful of register ranges (about a minute, then stops
   by itself) and prints any value that looks like an HH:MM time.
4. Send back both capture files as usual.

Pi/Linux:  python3 gen3_capture.py --slots


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT THE TEST DOES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Connects and waits silently for the dongle's heartbeat (up to 4 min).
   When it arrives you'll see:
     Heartbeat! Adapter serial extracted: WH2301G954

2. Tests 6 request types (A-F) one at a time, using the real serial.
   For each it sends a request and waits 5 seconds for a reply:
     TX slave=0x32 func=0x04 base=0 count=60 serial=WH2301G954
     ...
     Test A RESULT: ✓ MATCHED in 0.347s     <- worked!
     Test A RESULT: ✗ no response           <- still timing out

3. Prints a TEST SUMMARY showing which of A-F responded.

4. Keeps running to capture background cloud syncs (Ctrl+C to stop).


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT TO SEND BACK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  capture_<timestamp>.bin   (raw wire data)
  capture_<timestamp>.log   (readable timeline)

Both files, please. A brief note of what the TEST SUMMARY showed helps too.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TROUBLESHOOTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Pi: "python3: command not found"
  → try: python gen3_capture.py --sequential

Pi: permission denied on the .sh scripts
  → chmod +x run_sequential.sh && ./run_sequential.sh

Pi: can't reach inverter
  → Check the IP in gen3_config.ini matches what your router shows
  → Make sure nothing else is talking to the inverter (GivTCP etc.)

Windows: SmartScreen warning on the .exe
  → Click "More info" -> "Run anyway"
