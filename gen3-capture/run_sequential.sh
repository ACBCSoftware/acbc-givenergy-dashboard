#!/bin/bash
# THE KEY TEST: sequential mode with real adapter serial.
# Waits for the dongle heartbeat, extracts the real serial,
# then tests requests A-F one at a time.
cd "$(dirname "$0")"
python3 gen3_capture.py --sequential
