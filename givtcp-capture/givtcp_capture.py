#!/usr/bin/env python3
"""
GivTCP Library Wire Capture
============================
Uses GivTCP's bundled givenergy_modbus_async library — the one Brendon
confirmed works with his Gen3 inverter — and captures every byte sent
and received on the wire.

This answers the two key questions:
  1. Does detect_plant() get a fast response? (< 2s means real poll response)
  2. Does refresh_plant() keep getting fast responses after that?
  3. Which TX frame(s) triggered the first response?

Requirements:
  Python 3.11+   (for StrEnum — Brendon has 3.14 so this is fine)
  pip install crccheck  (GivTCP's only external dep)
  GivTCP source cloned locally (git clone https://github.com/britkat1980/giv_tcp)

Usage:
  python givtcp_capture.py
  (or double-click run_givtcp_capture.bat which does the pip install first)
"""

import sys
import asyncio
import time
import struct
import configparser
from pathlib import Path
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────────
HERE     = Path(__file__).parent
cfg      = configparser.ConfigParser()
cfg_path = HERE / "givtcp_config.ini"
if cfg_path.exists():
    cfg.read(cfg_path)

INVERTER_IP   = cfg.get("inverter", "ip",   fallback="").strip()
INVERTER_PORT = cfg.getint("inverter", "port", fallback=8899)
GIVTCP_PATH   = cfg.get("givtcp",    "path", fallback=r"").strip()

if not INVERTER_IP:
    INVERTER_IP = input("Inverter IP address: ").strip()
if not GIVTCP_PATH:
    GIVTCP_PATH = input(
        "Path to GivTCP source (the GivTCP subfolder, e.g. D:\\git\\giv_tcp\\GivTCP): "
    ).strip()

# ── Helpers ─────────────────────────────────────────────────────────────────────
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
    """One-line summary of a GivEnergy transparent frame."""
    if len(data) < 8 or data[:2] != b"\x59\x59":
        return f"(raw {len(data)}b)"
    outer = data[7]
    if outer == 0x01:
        serial = data[8:18].decode("ascii", errors="replace") if len(data) >= 18 else "?"
        return f"HEARTBEAT  serial={serial}"
    if outer == 0x02 and len(data) >= 44:
        serial = data[8:18].decode("ascii", errors="replace")
        slave  = data[26]
        inner  = data[27]
        base   = (data[38] << 8) | data[39]
        count  = (data[40] << 8) | data[41]
        fn     = {3: "HR", 4: "IR", 6: "WriteHR"}.get(inner, f"0x{inner:02x}")
        return (f"TRANSPARENT  serial={serial}  slave=0x{slave:02x}  "
                f"{fn}(base={base},count={count})")
    return f"outer_func=0x{outer:02x}  len={len(data)}"

# ── Wire capture via asyncio patching ──────────────────────────────────────────
# We patch asyncio.open_connection to wrap the reader/writer so every byte
# flowing in both directions is captured BEFORE the library processes it.
# This must happen BEFORE importing GivTCP's library.

_log_fn   = None   # set after files are opened
_binf     = None
_logf     = None
_tx_count = [0]
_rx_count = [0]
_last_tx  = [0.0]
_last_rx  = [0.0]

class _CapturingWriter:
    def __init__(self, writer):
        self._w = writer
    def write(self, data: bytes):
        _tx_count[0] += 1
        now = time.time()
        elapsed_since_rx = now - _last_rx[0] if _last_rx[0] else -1
        _last_tx[0] = now
        summary = _decode_frame_header(data)
        msg = (f"TX #{_tx_count[0]}  len={len(data)}  "
               f"(+{elapsed_since_rx:.3f}s since last RX)\n"
               f"  {summary}")
        if _log_fn: _log_fn(msg)
        if _binf: _binf.write(data); _binf.flush()
        if _logf:
            _logf.write(f"  HEX: {data.hex()}\n")
            _logf.write(hexdump(data) + "\n")
            _logf.flush()
        return self._w.write(data)
    def __getattr__(self, name):
        return getattr(self._w, name)

class _CapturingReader:
    def __init__(self, reader):
        self._r = reader
    async def read(self, n: int = -1):
        data = await self._r.read(n)
        if data:
            _rx_count[0] += 1
            now = time.time()
            elapsed_since_tx = now - _last_tx[0] if _last_tx[0] else -1
            _last_rx[0] = now
            summary = _decode_frame_header(data)
            msg = (f"RX #{_rx_count[0]}  len={len(data)}  "
                   f"({elapsed_since_tx:.3f}s after last TX)\n"
                   f"  {summary}")
            if _log_fn: _log_fn(msg)
            if _binf: _binf.write(data); _binf.flush()
            if _logf:
                _logf.write(f"  HEX: {data.hex()}\n")
                _logf.write(hexdump(data) + "\n")
                _logf.flush()
        return data
    def __getattr__(self, name):
        return getattr(self._r, name)

_orig_open_connection = asyncio.open_connection

async def _capturing_open_connection(host, port, **kwargs):
    reader, writer = await _orig_open_connection(host, port, **kwargs)
    return _CapturingReader(reader), _CapturingWriter(writer)

# Patch BEFORE importing GivTCP
asyncio.open_connection = _capturing_open_connection

# ── Import GivTCP ──────────────────────────────────────────────────────────────
try:
    sys.path.insert(0, GIVTCP_PATH)
    from givenergy_modbus_async.client.client import Client as Gen3Client
    print(f"[{_ts()}] GivTCP library loaded from: {GIVTCP_PATH}")
except ImportError as e:
    print(f"ERROR: Could not import GivTCP library from {GIVTCP_PATH!r}")
    print(f"  {e}")
    print()
    print("Make sure:")
    print("  1. You've cloned GivTCP: git clone https://github.com/britkat1980/giv_tcp")
    print("  2. The path in givtcp_config.ini points to the GivTCP subfolder")
    print("  3. pip install crccheck  (only external dep needed)")
    input("\nPress Enter to exit...")
    sys.exit(1)

# ── Main async function ────────────────────────────────────────────────────────
async def run(ip: str, port: int, logf, binf):
    global _log_fn, _binf, _logf
    _binf = binf
    _logf = logf

    def log(msg, to_console=True):
        line = f"[{_ts()}] {msg}"
        if to_console: print(line, flush=True)
        logf.write(line + "\n"); logf.flush()

    _log_fn = log

    log("=" * 64)
    log("GivTCP Library Wire Capture")
    log("=" * 64)
    log(f"Target:      {ip}:{port}")
    log(f"GivTCP path: {GIVTCP_PATH}")
    log(f"Redaction:   DISABLED — real frame content visible")
    log("")

    client = Gen3Client(host=ip, port=port)

    log("Connecting...")
    await client.connect()
    log("Connected.")
    log("")

    # ── Phase 1: detect_plant() ────────────────────────────────────────────────
    log("─" * 64)
    log("Phase 1: detect_plant()  retries=10 timeout=3  (up to 30s per req)")
    log("─" * 64)
    t0 = time.time()
    try:
        await client.detect_plant(timeout=3, retries=10)
        elapsed = time.time() - t0
        plant = client.plant
        log(f"detect_plant() SUCCEEDED in {elapsed:.3f}s")
        log(f"  device_type:    {plant.device_type}")
        log(f"  slave_address:  0x{plant.slave_address:02x}")
        log(f"  is_hv:          {plant.isHV}")
        log(f"  num_batteries:  {plant.number_batteries}")
        log(f"  meter_list:     {plant.meter_list}")
    except Exception as exc:
        elapsed = time.time() - t0
        log(f"detect_plant() FAILED after {elapsed:.3f}s: {exc}")
        log("  (still attempting refresh_plant() below)")

    # ── Phase 2: 6 × refresh_plant() ──────────────────────────────────────────
    log("")
    log("─" * 64)
    log("Phase 2: 6 × refresh_plant() at 10-second intervals")
    log("KEY: do we get fast responses or still waiting ~300s?")
    log("─" * 64)

    for i in range(6):
        log("")
        log(f"refresh_plant() #{i+1} ...")
        t_r = time.time()
        try:
            await client.refresh_plant(
                True,
                number_batteries=client.plant.number_batteries,
                meter_list=client.plant.meter_list,
            )
            elapsed = time.time() - t_r
            log(f"refresh_plant() #{i+1} completed in {elapsed:.3f}s")

            # Try to extract live readings
            try:
                data = client.plant.inverter.getall()
                readings = []
                for k in ("p_pv1", "p_pv2", "p_load_demand", "p_battery",
                          "battery_percent", "p_grid_out"):
                    v = data.get(k)
                    if v is not None:
                        readings.append(f"{k}={v}")
                if readings:
                    log(f"  Readings: {', '.join(readings[:5])}")
            except Exception as de:
                log(f"  (could not decode readings: {de})")

        except Exception as exc:
            elapsed = time.time() - t_r
            log(f"refresh_plant() #{i+1} FAILED after {elapsed:.3f}s: {exc}")

        if i < 5:
            log(f"  waiting 10s...")
            await asyncio.sleep(10)

    await client.close()

    log("")
    log("=" * 64)
    log("CAPTURE COMPLETE")
    log(f"  TX frames sent:     {_tx_count[0]}")
    log(f"  RX chunks received: {_rx_count[0]}")
    log("=" * 64)


def main():
    stamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = HERE / f"givtcp_{stamp}.log"
    bin_path = HERE / f"givtcp_{stamp}.bin"

    print(f"[{_ts()}] Log: {log_path.name}")
    print(f"[{_ts()}] Bin: {bin_path.name}")
    print()

    with open(log_path, "w", encoding="utf-8") as logf, \
         open(bin_path, "wb") as binf:
        try:
            asyncio.run(run(INVERTER_IP, INVERTER_PORT, logf, binf))
        except KeyboardInterrupt:
            print(f"\n[{_ts()}] Stopped by user.")
        except Exception as exc:
            print(f"\n[{_ts()}] FATAL: {exc}")
            import traceback; traceback.print_exc()

    print(f"\n[{_ts()}] Send back:")
    print(f"  {log_path.name}")
    print(f"  {bin_path.name}")
    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
