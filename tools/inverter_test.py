#!/usr/bin/env python3
"""
inverter_test.py  --  Non-interactive inverter register test tool
ACBC GivEnergy Dashboard — live testing session

Usage:
    python inverter_test.py baseline          # read + save baseline to baseline.json
    python inverter_test.py live [n]          # read live power data n times (default 5)
    python inverter_test.py test C1           # apply named test writes
    python inverter_test.py restore           # restore from saved baseline.json
    python inverter_test.py snapshot          # current HR state
    python inverter_test.py write HR VAL      # write single register e.g. write 27 0

Tests: C1 C2 C3 E1 E2
"""

import socket, struct, time, sys, datetime, json, os

INV_IP   = "192.168.68.65"
INV_PORT = 8899
SLAVE    = 0x11
BASELINE_FILE = os.path.join(os.path.dirname(__file__), "baseline.json")

# ── CRC ──────────────────────────────────────────────────────────────────────
def _crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])

# ── Low-level ─────────────────────────────────────────────────────────────────
def _sock(timeout=6.0):
    s = socket.create_connection((INV_IP, INV_PORT), timeout=timeout)
    s.settimeout(timeout)
    return s

def _read_block(s, base, count, func=0x03):
    serial  = b"AB1234G567"
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([SLAVE, func]) + base.to_bytes(2,"big") + count.to_bytes(2,"big")
    crc     = _crc16(inner)
    payload = serial + padding + inner + crc
    length  = len(payload) + 2
    frame   = b"\x59\x59\x00\x01" + length.to_bytes(2,"big") + b"\x01\x02" + payload
    s.sendall(frame)
    buf = bytearray()
    deadline = time.time() + 6.0
    while time.time() < deadline:
        try:
            chunk = s.recv(4096)
            if not chunk: break
            buf.extend(chunk)
        except socket.timeout:
            break
        i = 0
        while i <= len(buf) - 6:
            if buf[i:i+2] == b"\x59\x59" and buf[i+6:i+8] == b"\x01\x02":
                flen = 6 + struct.unpack(">H", buf[i+4:i+6])[0]
                if len(buf) >= i + flen:
                    f = buf[i:i+flen]
                    if (len(f) >= 42 + count*2
                            and f[27] == func
                            and ((f[38]<<8)|f[39]) == base
                            and ((f[40]<<8)|f[41]) == count):
                        return [(f[42+j*2]<<8)|f[43+j*2] for j in range(count)]
            i += 1
    raise TimeoutError(f"No response: func=0x{func:02x} base={base} count={count}")

def _write_hr(s, reg, val):
    serial  = b"AB1234G567"
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([SLAVE, 0x06]) + reg.to_bytes(2,"big") + val.to_bytes(2,"big")
    crc     = _crc16(inner)
    payload = serial + padding + inner + crc
    length  = len(payload) + 2
    frame   = b"\x59\x59\x00\x01" + length.to_bytes(2,"big") + b"\x01\x02" + payload
    s.sendall(frame)
    buf = bytearray()
    deadline = time.time() + 6.0
    while time.time() < deadline:
        try:
            chunk = s.recv(4096)
            if not chunk: break
            buf.extend(chunk)
        except socket.timeout:
            break
        i = 0
        while i <= len(buf) - 6:
            if buf[i:i+2] == b"\x59\x59" and buf[i+6:i+8] == b"\x01\x02":
                flen = 6 + struct.unpack(">H", buf[i+4:i+6])[0]
                if len(buf) >= i + flen:
                    f = buf[i:i+flen]
                    if f[27] == 0x06 and ((f[38]<<8)|f[39]) == reg and ((f[40]<<8)|f[41]) == val:
                        return True
            i += 1
    return False

# ── HR reads ──────────────────────────────────────────────────────────────────
HR_BLOCKS = [
    (19, 14), (44, 2), (56, 2), (59, 1),
    (94, 12), (110, 7), (117, 3),
    (175, 1), (187, 14), (313, 2),
]

HR_NAMES = {
    20:  "enable_charge_target",
    27:  "battery_power_mode      (EXPORT=0 / ECO=1)",
    31:  "charge_slot_2_start",    32: "charge_slot_2_end",
    44:  "discharge_slot_2_start", 45: "discharge_slot_2_end",
    56:  "discharge_slot_1_start", 57: "discharge_slot_1_end",
    59:  "enable_discharge",
    94:  "charge_slot_1_start",    95: "charge_slot_1_end",
    96:  "enable_charge",
    110: "battery_soc_reserve     (%)",
    111: "battery_charge_limit    (0-50)",
    112: "battery_discharge_limit (0-50)",
    116: "charge_target_soc       (%)",
    175: "enable_battery_on_pv_or_grid",
    199: "enable_standard_self_consumption_logic",
    313: "battery_charge_limit_ac",
    314: "battery_discharge_limit_ac",
}

RESTORE_REGS = [20,27,31,32,44,45,56,57,59,94,95,96,110,111,112,116,175,199,313,314]

def read_hrs():
    regs = {}
    s = _sock()
    try:
        for base, count in HR_BLOCKS:
            try:
                vals = _read_block(s, base, count, 0x03)
                for i,v in enumerate(vals): regs[base+i] = v
            except Exception as e:
                regs[f"ERR{base}"] = str(e)
            time.sleep(0.12)
    finally:
        s.close()
    return regs

def _s16(v): return v - 65536 if v >= 32768 else v

def read_live():
    s = _sock()
    try:
        vals = _read_block(s, 0, 60, 0x04)
    finally:
        s.close()
    solar  = vals[18] + vals[20]
    home   = vals[42]
    grid   = _s16(vals[30])
    batt   = _s16(vals[52])
    soc    = min(100, max(0, vals[59]))
    return dict(solar=solar, home=home, grid=grid, batt=batt, soc=soc)

def print_hrs(regs, label=""):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"\n{'='*65}")
    print(f"  HR SNAPSHOT  {ts}  {label}")
    print(f"{'='*65}")
    for reg in sorted(r for r in regs if isinstance(r, int)):
        name = HR_NAMES.get(reg)
        if name is None: continue
        val = regs[reg]
        if reg in (31,32,44,45,56,57,94,95):
            disp = f"{val:4d}  ({val//100:02d}:{val%100:02d})" if val else "   0  (disabled)"
        elif reg in (20,27,59,96,175,199):
            disp = f"{val}  ({'ON' if val else 'OFF'})"
        else:
            disp = str(val)
        marker = " ***" if reg in (27,96,175,199) else ""
        print(f"  HR{reg:3d}  {disp:<32s}  {name}{marker}")
    for k,v in regs.items():
        if isinstance(k,str): print(f"  {k}: {v}")
    print()

def print_live(d, label=""):
    batt = d['batt']
    grid = d['grid']
    batt_str = f"{abs(batt):4d}W {'<<CHARGING' if batt < -20 else 'DISCHARGING>>' if batt > 20 else 'idle'}"
    grid_str = f"{abs(grid):4d}W {'IMPORTING' if grid < -20 else 'EXPORTING' if grid > 20 else 'balanced'}"
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] Battery: {batt_str:25s}  Grid: {grid_str:25s}  Solar: {d['solar']:4d}W  Home: {d['home']:4d}W  SOC: {d['soc']:3d}%  {label}")

def do_writes(writes):
    s = _sock()
    try:
        for reg, val in writes:
            ok = _write_hr(s, reg, val)
            name = HR_NAMES.get(reg, f"HR{reg}")
            print(f"  [{'OK  ' if ok else 'FAIL'}] HR{reg:3d} = {val:5d}   {name}")
            time.sleep(0.1)
    finally:
        s.close()

# ── Tests ─────────────────────────────────────────────────────────────────────
def _slot_now_plus2():
    now = datetime.datetime.now()
    e   = now + datetime.timedelta(hours=2)
    return now.hour*100+now.minute, e.hour*100+e.minute

TESTS = {
    "C1": {
        "name": "CHARGE: Eco OFF + Charge ON, no slots  [MAIN HYPOTHESIS]",
        "writes": lambda t: [
            (27, 0),   # eco OFF (export mode -- removes solar-only constraint)
            (59, 0),   # discharge OFF
            (111,25),  # charge limit ~50%
            (20, 1),   # enable charge target
            (116, t),  # target SOC
            (96, 1),   # charge ON  (armed last)
        ],
    },
    "C2": {
        "name": "CHARGE: Eco ON + Charge ON, no slots  [control -- current scheduler]",
        "writes": lambda t: [
            (27, 1),   # eco ON (self-consumption -- solar-only constraint active)
            (59, 0),   # discharge OFF
            (111,25),  # charge limit ~50%
            (20, 1),   # enable charge target
            (116, t),  # target SOC
            (96, 1),   # charge ON
        ],
    },
    "C3": {
        "name": "CHARGE: Eco ON + slot covering now+2hr  [Octopus method -- control]",
        "writes": lambda t: (lambda st,en: [
            (27, 1),   # eco ON
            (59, 0),   # discharge OFF
            (94, st),  # slot start = now
            (95, en),  # slot end   = now+2hr
            (111,25),  # charge limit ~50%
            (20, 1),   # enable charge target
            (116, t),  # target SOC
            (96, 1),   # charge ON
        ])(*_slot_now_plus2()),
    },
    "E1": {
        "name": "EXPORT: Eco OFF + Discharge ON, no slots  [slot-free export test]",
        "writes": lambda t: [
            (27, 0),   # eco OFF (export mode)
            (96, 0),   # charge OFF
            (112,25),  # discharge limit ~50%
            (110,20),  # SOC reserve 20% (safe floor for test)
            (59, 1),   # discharge ON
        ],
    },
    "E2": {
        "name": "EXPORT: Eco ON + Discharge ON, no slots  [self-consumption -- control]",
        "writes": lambda t: [
            (27, 1),   # eco ON (self-consumption)
            (96, 0),   # charge OFF
            (112,25),  # discharge limit ~50%
            (110,20),  # SOC reserve 20%
            (59, 1),   # discharge ON
        ],
    },
}

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    cmd  = args[0].lower() if args else "help"

    if cmd == "baseline":
        print("Reading baseline...")
        regs = read_hrs()
        print_hrs(regs, "BASELINE")
        live = read_live()
        print_live(live, "LIVE")
        soc = live["soc"]
        target = min(100, soc + 15)
        print(f"\n  SOC now: {soc}%  =>  charge target for tests: {target}%")
        # save
        save = {str(k): v for k,v in regs.items() if isinstance(k,int) and k in RESTORE_REGS}
        save["_charge_target"] = target
        save["_soc_at_baseline"] = soc
        with open(BASELINE_FILE,"w") as f: json.dump(save, f, indent=2)
        print(f"  Baseline saved to {BASELINE_FILE}")

    elif cmd == "live":
        n = int(args[1]) if len(args) > 1 else 8
        print(f"Live data ({n} reads, 5s apart):")
        for _ in range(n):
            try:
                d = read_live()
                print_live(d)
            except Exception as e:
                print(f"  ERROR: {e}")
            time.sleep(5)

    elif cmd == "snapshot":
        regs = read_hrs()
        print_hrs(regs, "current state")

    elif cmd == "test":
        if len(args) < 2:
            print("Usage: test <C1|C2|C3|E1|E2>")
            return
        key = args[1].upper()
        if key not in TESTS:
            print(f"Unknown test: {key}. Options: {', '.join(TESTS)}")
            return
        # load charge target
        target = 80
        if os.path.exists(BASELINE_FILE):
            with open(BASELINE_FILE) as f: bl = json.load(f)
            target = bl.get("_charge_target", 80)

        test = TESTS[key]
        writes = test["writes"](target)
        print(f"\n  TEST {key}: {test['name']}")
        print(f"  Writes:")
        for reg, val in writes:
            print(f"    HR{reg:3d} = {val:5d}   {HR_NAMES.get(reg,'')}")
        print()
        do_writes(writes)
        print(f"\n  Monitoring 60s (every 5s):")
        for i in range(12):
            try:
                d = read_live()
                print_live(d)
            except Exception as e:
                print(f"  READ ERROR: {e}")
            time.sleep(5)
        print(f"\n  Test {key} done. Run 'restore' to revert, or run next test.")

    elif cmd == "restore":
        if not os.path.exists(BASELINE_FILE):
            print("No baseline.json found -- run 'baseline' first")
            return
        with open(BASELINE_FILE) as f: bl = json.load(f)
        writes = [(int(k), v) for k,v in bl.items() if not k.startswith("_")]
        print(f"Restoring {len(writes)} registers from baseline...")
        do_writes(writes)
        print("Restored.")

    elif cmd == "write":
        if len(args) < 3:
            print("Usage: write <HR> <VAL>")
            return
        reg, val = int(args[1]), int(args[2])
        print(f"Writing HR{reg} = {val}...")
        s = _sock()
        try:
            ok = _write_hr(s, reg, val)
        finally:
            s.close()
        print(f"  [{'OK' if ok else 'FAIL'}] HR{reg} = {val}  ({HR_NAMES.get(reg,'')})")

    else:
        print(__doc__)

if __name__ == "__main__":
    main()
