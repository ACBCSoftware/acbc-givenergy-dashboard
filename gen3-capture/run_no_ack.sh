#!/bin/bash
# Runs the diagnostic with heartbeat acknowledgement OFF (comparison test).
cd "$(dirname "$0")"
python3 gen3_capture.py --no-ack
