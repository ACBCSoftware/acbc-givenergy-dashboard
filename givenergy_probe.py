#!/usr/bin/env python3
"""
givenergy_probe.py — GivEnergy Gateway read-only probe
=======================================================
Sends two read-input-registers requests to a GivEnergy Gateway/AIO
inverter and prints the register tables.

  - Pure Python standard library — no pip installs, no external code
  - Read-only: never writes to the inverter
  - One TCP connection per probe, closed immediately after

Usage
-----
1. Edit GATEWAY_IP below to match your gateway's IP address
2. Run:  python givenergy_probe.py
3. Send the terminal output (or givenergy_probe_results.txt) back

Protocol note
-------------
GivEnergy uses a proprietary Modbus-over-TCP framing on port 8899.
This script implements only the read-input-registers (function 0x04)
request/response, using the Gen3/AIO CRC convention (CRC-16/Modbus
over slave + func + base + count, little-endian output).
"""

import socket
import struct
import sys
import time

# ── Edit this ──────────────────────────────────────────────────────────────
GATEWAY_IP   = "192.168.x.x"   # <-- replace with your gateway's IP address
GATEWAY_PORT = 8899
TIMEOUT_SECS = 15              # seconds to wait for a response
# ───────────────────────────────────────────────────────────────────────────


def _crc16(data: bytes) -> int:
    """CRC-16/Modbus, input-reflected LSB-first (standard Modbus CRC)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _build_request(slave: int, base: int, count: int) -> bytes:
    """
    Build a GivEnergy transparent-Modbus read-input-registers frame.

    Outer frame:
        59 59 00 01  <length 2B big-endian>  <uid 1B = 0x01>  <outer_func 1B = 0x02>

    Inner payload (transparent):
        dummy_serial (10 B)    -- inverter ignores this on requests
        padding      ( 8 B)    -- must be 0x0000000000000008; zeroing it silences the inverter
        slave        ( 1 B)    -- device address
        inner_func   ( 1 B)    -- 0x04 = read input registers
        base         ( 2 B BE) -- first register number
        count        ( 2 B BE) -- number of registers to read
        crc          ( 2 B LE) -- CRC-16/Modbus over [slave, inner_func, base_hi, base_lo, count_hi, count_lo]
    """
    DUMMY_SERIAL = b'AB1234G567'
    PADDING      = (8).to_bytes(8, 'big')   # 0x0000000000000008
    INNER_FUNC   = 0x04                      # read input registers

    crc_bytes = struct.pack('<H', _crc16(
        bytes([slave, INNER_FUNC]) + struct.pack('>HH', base, count)
    ))

    payload = (DUMMY_SERIAL + PADDING
               + bytes([slave, INNER_FUNC])
               + struct.pack('>HH', base, count)
               + crc_bytes)

    frame = (b'\x59\x59\x00\x01'
             + struct.pack('>H', len(payload) + 2)   # +2 for uid + outer_func
             + b'\x01\x02'
             + payload)
    return frame


def _probe(ip: str, port: int, slave: int, base: int, count: int,
           timeout: float) -> dict | None:
    """
    Open a fresh TCP connection, send one IR read request, return
    {register_number: raw_uint16_value} or None on failure.
    """
    frame = _build_request(slave, base, count)

    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.sendall(frame)

            # Response arrives as a stream — read until we have the full frame.
            # The length field at bytes [4:6] tells us the total payload size.
            buf = b''
            deadline = time.time() + timeout
            while time.time() < deadline:
                remaining = max(0.1, deadline - time.time())
                s.settimeout(remaining)
                try:
                    chunk = s.recv(1024)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) >= 6:
                    expected_total = 6 + struct.unpack('>H', buf[4:6])[0]
                    if len(buf) >= expected_total:
                        break

    except OSError as exc:
        print(f"    Connection error: {exc}")
        return None

    # Response frame layout (byte offsets):
    #   0– 5  outer header  (59 59 00 01 <len>)
    #   6      uid
    #   7      outer_func
    #   8–17   data_adapter serial (inverter fills its real serial here)
    #  18–25   padding
    #  26      slave (echo)
    #  27      inner_func (echo)
    #  28–37   inverter serial (10 extra bytes the inverter inserts in responses)
    #  38–39   base (echo)
    #  40–41   count (echo)
    #  42+     register data (count × 2 bytes, big-endian uint16 each)
    #  last 2  CRC
    DATA_OFFSET  = 42
    min_expected = DATA_OFFSET + count * 2 + 2   # data + CRC

    if len(buf) < min_expected:
        print(f"    Response too short: received {len(buf)} bytes, "
              f"expected at least {min_expected}")
        return None

    registers = {}
    for i in range(count):
        offset = DATA_OFFSET + i * 2
        registers[base + i] = struct.unpack('>H', buf[offset:offset + 2])[0]
    return registers


def _print_table(registers: dict, label: str) -> None:
    non_zero = {r: v for r, v in registers.items() if v != 0}
    print(f"\n  {label}")
    print(f"  {'─' * 56}")
    if not non_zero:
        print("  (all registers returned zero — possible timeout or unsupported range)")
        return
    print(f"  {'Register':<14} {'Decimal':>10}  {'Hex':>8}  {'Signed':>8}")
    print(f"  {'─' * 56}")
    for reg in sorted(non_zero):
        v = non_zero[reg]
        signed = v if v < 32768 else v - 65536
        print(f"  IR({reg:<10}) {v:>10}  0x{v:04X}  {signed:>+8}")


def main() -> None:
    if GATEWAY_IP.startswith("192.168.x"):
        print("ERROR: please edit GATEWAY_IP at the top of this script first.")
        sys.exit(1)

    banner = f"GivEnergy Gateway Probe  —  {GATEWAY_IP}:{GATEWAY_PORT}"
    print(banner)
    print("=" * len(banner))
    print("Read-only. No data is written to the inverter.\n")

    probes = [
        (0x11, 1600, 60, "Probe 1 — Live power data  IR(1600–1659) @ slave 0x11"),
        (0x11, 1780, 60, "Probe 2 — SOC / serial data  IR(1780–1839) @ slave 0x11"),
    ]

    all_results: list[tuple] = []

    for slave, base, count, desc in probes:
        print(f"\n{desc}")
        print(f"  Connecting to {GATEWAY_IP}:{GATEWAY_PORT}, "
              f"requesting {count} registers from IR({base})...")
        t0 = time.time()
        regs = _probe(GATEWAY_IP, GATEWAY_PORT, slave, base, count, TIMEOUT_SECS)
        elapsed = time.time() - t0

        if regs is None:
            print(f"  FAILED — no valid response within {TIMEOUT_SECS}s")
        else:
            print(f"  Response received in {elapsed:.2f}s")
            _print_table(regs, desc)

        all_results.append((slave, base, count, desc, regs, elapsed))

    # ── Save to file ────────────────────────────────────────────────────────
    outfile = "givenergy_probe_results.txt"
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(outfile, "w") as f:
        f.write(f"GivEnergy Gateway Probe Results\n")
        f.write(f"Host  : {GATEWAY_IP}:{GATEWAY_PORT}\n")
        f.write(f"Time  : {timestamp}\n\n")
        for slave, base, count, desc, regs, elapsed in all_results:
            f.write(f"{desc}\n")
            if regs is None:
                f.write(f"  FAILED (no response within {TIMEOUT_SECS}s)\n")
            else:
                f.write(f"  Response time: {elapsed:.2f}s\n")
                for reg in sorted(regs):
                    f.write(f"  IR({reg}) = {regs[reg]}\n")
            f.write("\n")

    print(f"\n{'─' * 56}")
    print(f"Results saved to: {outfile}")
    print("Please send that file (or copy/paste the above output) back — thank you!")


if __name__ == "__main__":
    main()
