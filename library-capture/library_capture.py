#!/usr/bin/env python3
"""
GivEnergy Library Wire Capture
===============================
Uses the real givenergy-modbus library to connect to a GivEnergy inverter
and captures every byte sent and received on the wire — unredacted.

This answers: what does the library do on the wire that our custom
poke-and-listen doesn't?  Does it get IMMEDIATE poll responses from
Gen3/AIO, or does it also see 5-minute cloud syncs?

Requirements:
  Python 3.14+               https://python.org/downloads/
  pip install givenergy-modbus

Usage:
  python library_capture.py
  (or double-click run_library_capture.bat which does the pip install first)
"""

import sys
import asyncio
import time
import configparser
from pathlib import Path
from datetime import datetime

# ── Version gate ──────────────────────────────────────────────────────────────
if sys.version_info < (3, 14):
    print(f"ERROR: Python 3.14+ is required (you have {sys.version.split()[0]})")
    print("Download from https://python.org/downloads/")
    input("\nPress Enter to exit...")
    sys.exit(1)

# ── Library check ─────────────────────────────────────────────────────────────
try:
    import givenergy_modbus.client.client as _client_mod
    from givenergy_modbus.client.client import Client
    from importlib.metadata import version as _pkg_ver
    LIB_VERSION = _pkg_ver("givenergy-modbus")
except ImportError:
    print("givenergy-modbus is not installed.")
    print("Run:  pip install givenergy-modbus")
    input("\nPress Enter to exit...")
    sys.exit(1)

# ── Disable frame redaction so we see real content ────────────────────────────
# The library normally masks serial numbers in captured frames. For this
# diagnostic we need to see the real bytes to compare with what we send.
_client_mod.redact = lambda frame: frame

# ── Config ────────────────────────────────────────────────────────────────────
HERE = Path(__file__).parent

def load_config():
    cfg = configparser.ConfigParser()
    cfg_path = HERE / "gen3_config.ini"
    if cfg_path.exists():
        cfg.read(cfg_path)
    ip   = cfg.get("inverter", "ip",   fallback="").strip()
    port = cfg.getint("inverter", "port", fallback=8899)
    if not ip:
        ip = input("Inverter IP address: ").strip()
    return ip, port

# ── Helpers ───────────────────────────────────────────────────────────────────
def _ts():
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]

def hexdump(data: bytes, width: int = 16) -> str:
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i+width]
        out.append(f"      {i:04x}  "
                   f"{' '.join(f'{b:02x}' for b in chunk):<{width*3}}  "
                   f"{''.join(chr(b) if 32<=b<127 else '.' for b in chunk)}")
    return "\n".join(out)

def _decode_frame_header(data: bytes) -> str:
    """Best-effort one-liner summary of a GivEnergy transparent frame."""
    if len(data) < 28 or data[:2] != b"\x59\x59":
        return "(non-standard frame)"
    outer_func = data[7]
    if outer_func == 0x01:
        serial = data[8:18].decode("ascii", errors="replace")
        return f"func=01/HEARTBEAT  serial={serial}"
    if outer_func == 0x02 and len(data) >= 44:
        serial = data[8:18].decode("ascii", errors="replace")
        slave     = data[26]
        inner_func = data[27]
        base  = (data[38] << 8) | data[39]
        count = (data[40] << 8) | data[41]
        func_name = {0x03: "HR", 0x04: "IR", 0x06: "WriteHR"}.get(inner_func, f"0x{inner_func:02x}")
        return (f"func=02/TRANSPARENT  serial={serial}  "
                f"slave=0x{slave:02x}  {func_name}(base={base},count={count})")
    return f"func=0x{outer_func:02x}"

# ── Main async function ────────────────────────────────────────────────────────
async def run(ip: str, port: int, logf, binf):

    def log(msg, to_console=True):
        line = f"[{_ts()}] {msg}"
        if to_console:
            print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    # ── Capture sink called by the library for every TX and RX chunk ──────────
    tx_count = [0]
    rx_count = [0]
    last_tx_time = [0.0]
    last_rx_time = [0.0]

    def capture_sink(direction: str, data: bytes):
        now = time.time()
        binf.write(data)
        binf.flush()

        if direction == "tx":
            tx_count[0] += 1
            elapsed_since_last_rx = now - last_rx_time[0] if last_rx_time[0] else -1
            summary = _decode_frame_header(data)
            log(f"TX #{tx_count[0]}  len={len(data)}  (+{elapsed_since_last_rx:.3f}s since last RX)")
            log(f"  {summary}")
            logf.write(f"  HEX: {data.hex()}\n")
            logf.write(hexdump(data) + "\n")
            logf.flush()
            last_tx_time[0] = now

        else:  # rx
            rx_count[0] += 1
            elapsed_since_last_tx = now - last_tx_time[0] if last_tx_time[0] else -1
            # RX is raw TCP — may be partial frame, multiple frames, or noise
            # Parse frame headers best-effort; might be mid-stream
            summary = _decode_frame_header(data)
            log(f"RX #{rx_count[0]}  len={len(data)}  ({elapsed_since_last_tx:.3f}s after last TX)")
            log(f"  {summary}")
            logf.write(f"  HEX: {data.hex()}\n")
            logf.write(hexdump(data) + "\n")
            logf.flush()
            last_rx_time[0] = now

    # ── Header ────────────────────────────────────────────────────────────────
    log("=" * 64)
    log(f"GivEnergy Library Wire Capture  (givenergy-modbus v{LIB_VERSION})")
    log("=" * 64)
    log(f"Target:     {ip}:{port}")
    log(f"Redaction:  DISABLED — real frame content visible")
    log("")
    log("Connecting...")

    client = Client(host=ip, port=port)
    await client.connect()
    log("Connected.")
    log("")

    # Start frame capture (runs alongside normal library operation)
    capture_task = asyncio.create_task(
        client.capture_frames(capture_sink, duration=600.0)
    )

    # ── Phase 1: detect() ─────────────────────────────────────────────────────
    log("─" * 64)
    log("Phase 1: detect()  — library identifies device type")
    log("─" * 64)
    t0 = time.time()
    try:
        caps = await client.detect()
        log(f"detect() completed in {time.time()-t0:.3f}s")
        log(f"  Model:            {caps.device_type.name}")
        log(f"  Inverter address: 0x{caps.inverter_address:02x}")
        log(f"  Is HV system:     {caps.is_hv}")
        log(f"  LV batteries:     {[f'0x{a:02x}' for a in caps.lv_battery_addresses]}")
        log(f"  Meters:           {[f'0x{a:02x}' for a in caps.meter_addresses]}")
    except Exception as exc:
        log(f"detect() FAILED after {time.time()-t0:.3f}s: {exc}")
        log("  (continuing with refresh() attempts anyway)")
        caps = None

    # ── Phase 2: 6 × refresh() at 10-second intervals ─────────────────────────
    log("")
    log("─" * 64)
    log("Phase 2: 6 × refresh() at 10-second intervals")
    log("KEY QUESTION: does each refresh() get a fast response (<2s),")
    log("              or does it wait ~300s for a cloud sync?")
    log("─" * 64)

    for i in range(6):
        log("")
        log(f"refresh() #{i+1} ...")
        t_refresh = time.time()
        try:
            plant = await client.refresh()
            elapsed = time.time() - t_refresh
            log(f"refresh() #{i+1} returned in {elapsed:.3f}s")

            # Try to extract readable values
            try:
                inv = plant.inverter
                readings = []
                for attr in ("battery_percent", "p_pv", "p_pv1", "p_pv2",
                             "p_load", "p_grid_apparent", "e_pv1_day", "e_pv2_day"):
                    v = getattr(inv, attr, None)
                    if v is not None:
                        readings.append(f"{attr}={v}")
                if readings:
                    log(f"  Readings: {', '.join(readings[:6])}")
                else:
                    log("  (no decodable readings)")
            except Exception as e:
                log(f"  (decode error: {e})")

        except Exception as exc:
            elapsed = time.time() - t_refresh
            log(f"refresh() #{i+1} RAISED after {elapsed:.3f}s: {exc}")

        if i < 5:
            log(f"  waiting 10s ...")
            await asyncio.sleep(10)

    # ── Teardown ──────────────────────────────────────────────────────────────
    capture_task.cancel()
    try:
        await capture_task
    except asyncio.CancelledError:
        pass
    await client.close()

    log("")
    log("=" * 64)
    log("CAPTURE COMPLETE")
    log(f"  TX frames captured: {tx_count[0]}")
    log(f"  RX chunks captured: {rx_count[0]}")
    log("=" * 64)


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    ip, port = load_config()
    stamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = HERE / f"libcapture_{stamp}.log"
    bin_path = HERE / f"libcapture_{stamp}.bin"

    print(f"[{_ts()}] Log: {log_path.name}")
    print(f"[{_ts()}] Bin: {bin_path.name}")
    print()

    with open(log_path, "w", encoding="utf-8") as logf, \
         open(bin_path, "wb") as binf:
        try:
            asyncio.run(run(ip, port, logf, binf))
        except KeyboardInterrupt:
            print(f"\n[{_ts()}] Stopped by user.")
        except Exception as exc:
            print(f"\n[{_ts()}] ERROR: {exc}")

    print(f"\n[{_ts()}] Send back:")
    print(f"  {log_path.name}")
    print(f"  {bin_path.name}")
    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
