#!/usr/bin/env python3
"""
hr_monitor.py — Watch GivEnergy holding register changes in real-time.

Usage (run from Pi with givenergy service STOPPED):
    python3 hr_monitor.py [inverter_ip] [--snapshot]

    Default IP: 192.168.68.65
    --snapshot  : print once and exit (for before/after comparisons)

Press Enter to label a snapshot (e.g. "after eco mode"), Ctrl-C to quit.
"""

import socket, struct, time, sys, threading, datetime

INV_IP   = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "192.168.68.65"
INV_PORT = 8899
SLAVE    = 0x11
SNAPSHOT = "--snapshot" in sys.argv

# ── Registers we care about ────────────────────────────────────────────────
NAMED_HRS = {
    19:  "dsp_fw_version",
    20:  "enable_charge_target       [must be 1 for target SOC to work]",
    21:  "arm_fw_version",
    27:  "battery_power_mode         (EXPORT=0 / SELF_CONSUMPTION=1)",
    31:  "charge_slot_2_start        (HHMM)",
    32:  "charge_slot_2_end          (HHMM)",
    44:  "discharge_slot_2_start     (HHMM)",
    45:  "discharge_slot_2_end       (HHMM)",
    56:  "discharge_slot_1_start     (HHMM)",
    57:  "discharge_slot_1_end       (HHMM)",
    59:  "enable_discharge",
    94:  "charge_slot_1_start        (HHMM)",
    95:  "charge_slot_1_end          (HHMM)",
    96:  "enable_charge",
    110: "battery_soc_reserve        (%)",
    111: "battery_charge_limit       (0-50 scale)",
    112: "battery_discharge_limit    (0-50 scale)",
    114: "battery_discharge_min_power_reserve",
    116: "charge_target_soc          (%)",
    117: "charge_soc_stop_2          (%)",
    119: "charge_soc_stop_1          (%)",
    175: "enable_battery_on_pv_or_grid  [HYPOTHESIS: 1 = allow grid charging]",
    199: "enable_standard_self_consumption_logic  [HYPOTHESIS: 0 = force charge from grid]",
    200: "cmd_bms_flash_update",
    313: "battery_charge_limit_ac    (AC-specific, 1-100 scale)",
    314: "battery_discharge_limit_ac (AC-specific, 1-100 scale)",
}

# Build the HR blocks we need to read (merge nearby regs into single reads)
# Blocks: 19-32, 44-45, 56-57, 59, 94-120, 175-200, 313-314
READ_BLOCKS = [
    (19,  14),   # 19-32
    (44,   2),   # 44-45
    (56,   2),   # 56-57
    (59,   1),   # 59
    (94,  12),   # 94-105  (charge slot 1, enable_charge)
    (110,  7),   # 110-116 (soc_reserve, charge_limit, discharge_limit, charge_target_soc)
    (117,  3),   # 117-119 (soc_stop targets)
    (175,  1),   # 175     (enable_battery_on_pv_or_grid)
    (187, 14),   # 187-200 (enable_standard_self_consumption_logic at 199)
    (313,  2),   # 313-314
]

# ── Protocol helpers ────────────────────────────────────────────────────────
def _crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])

def _build_read_frame(base: int, count: int) -> bytes:
    serial  = b"AB1234G567"
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([SLAVE, 0x03]) + base.to_bytes(2, "big") + count.to_bytes(2, "big")
    crc     = _crc16(inner)
    payload = serial + padding + inner + crc
    length  = len(payload) + 2
    return b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload

def _parse_response(buf: bytearray, base: int, count: int):
    """Scan buf for a matching HR read response. Returns list of values or None."""
    i = 0
    while i <= len(buf) - 6:
        if buf[i:i+2] == b"\x59\x59" and buf[i+6:i+8] == b"\x01\x02":
            flen = 6 + struct.unpack(">H", buf[i+4:i+6])[0]
            if len(buf) >= i + flen:
                f = buf[i:i+flen]
                if (len(f) >= 42 + count * 2
                        and f[27] == 0x03
                        and ((f[38] << 8) | f[39]) == base
                        and ((f[40] << 8) | f[41]) == count):
                    return [(f[42+j*2] << 8) | f[43+j*2] for j in range(count)], i + flen
        i += 1
    return None, 0

def read_all() -> dict:
    """Read all blocks on a single persistent connection and return {reg: value}."""
    result = {}
    timeout = 6.0
    try:
        s = socket.create_connection((INV_IP, INV_PORT), timeout=timeout)
        s.settimeout(timeout)
    except Exception as e:
        return {"ERR_connect": str(e)}

    try:
        buf = bytearray()
        for base, count in READ_BLOCKS:
            frame = _build_read_frame(base, count)
            try:
                s.sendall(frame)
            except Exception as e:
                result[f"ERR_base{base}"] = f"send: {e}"
                continue

            # Drain until we find the matching response (skip heartbeats, etc.)
            deadline = time.time() + timeout
            found = False
            while time.time() < deadline:
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                except socket.timeout:
                    break
                vals, consumed = _parse_response(buf, base, count)
                if vals is not None:
                    for i, v in enumerate(vals):
                        result[base + i] = v
                    buf = buf[consumed:]
                    found = True
                    break
            if not found:
                result[f"ERR_base{base}"] = f"timeout (no response)"
            time.sleep(0.15)   # small inter-read gap
    finally:
        s.close()
    return result

def fmt_hhmm(val: int) -> str:
    """Format a raw HHMM register value."""
    if val == 0:
        return "0 (00:00 / disabled)"
    return f"{val} ({val//100:02d}:{val%100:02d})"

def fmt_bool(val: int) -> str:
    return f"{val} ({'ON' if val else 'OFF'})"

def print_snapshot(regs: dict, label: str = ""):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"\n{'='*70}")
    print(f"  SNAPSHOT  {ts}  {label}")
    print(f"{'='*70}")
    for reg in sorted(r for r in regs if isinstance(r, int)):
        val  = regs[reg]
        name = NAMED_HRS.get(reg)
        if name is None:
            continue   # skip regs we don't have names for
        # pretty-format known types
        if "slot" in name and "HHMM" in name:
            display = fmt_hhmm(val)
        elif name.startswith("enable") or name.startswith("cmd_"):
            display = fmt_bool(val)
        else:
            display = str(val)
        marker = "  ***" if reg in (175, 199, 20, 27) else ""
        print(f"  HR{reg:3d}  {display:<30s}  {name}{marker}")
    # errors
    for k, v in regs.items():
        if isinstance(k, str):
            print(f"  {k}: {v}")
    print()

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    print(f"HR Monitor — inverter {INV_IP}:{INV_PORT}  slave 0x{SLAVE:02X}")
    print("Key registers to watch: HR20, HR27, HR94/95, HR96, HR175, HR199")
    print()

    if SNAPSHOT:
        regs = read_all()
        print_snapshot(regs, "(snapshot)")
        return

    print("Polling every 4 seconds. Press Enter to add a label. Ctrl-C to quit.")
    print("Changed registers will be highlighted with  >>>  \n")

    prev = {}
    label_queue = []

    def label_thread():
        while True:
            txt = input()
            label_queue.append(txt.strip() or "mark")

    t = threading.Thread(target=label_thread, daemon=True)
    t.start()

    while True:
        try:
            curr = read_all()
        except Exception as e:
            print(f"[{datetime.datetime.now():%H:%M:%S}] Read error: {e}")
            time.sleep(4)
            continue

        label = label_queue.pop(0) if label_queue else None

        if label:
            print_snapshot(curr, f"--- {label} ---")
            prev = dict(curr)
        elif not prev:
            print_snapshot(curr, "(initial state)")
            prev = dict(curr)
        else:
            # Show only changes
            changes = []
            for reg in sorted(r for r in curr if isinstance(r, int)):
                if curr[reg] != prev.get(reg):
                    name = NAMED_HRS.get(reg, "")
                    old  = prev.get(reg, "?")
                    new  = curr[reg]
                    if "slot" in name and "HHMM" in name:
                        old_s = fmt_hhmm(old) if isinstance(old, int) else str(old)
                        new_s = fmt_hhmm(new)
                    elif "enable" in name:
                        old_s = fmt_bool(old) if isinstance(old, int) else str(old)
                        new_s = fmt_bool(new)
                    else:
                        old_s, new_s = str(old), str(new)
                    changes.append(f"  >>> HR{reg:3d}  {old_s} → {new_s}   {name}")
            if changes:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] Changes detected:")
                for c in changes:
                    print(c)
                print()
                prev = dict(curr)

        time.sleep(4)

if __name__ == "__main__":
    main()
