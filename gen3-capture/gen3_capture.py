#!/usr/bin/env python3
"""
GivEnergy inverter — connection diagnostic & capture tool

Flags:
  --no-ack        Don't respond to heartbeats
  --handshake     Send HR reads alongside IR pokes (previous test)
  --sequential    Proper Modbus client: wait for heartbeat, extract real
                  adapter serial, then test A-F with that serial.
  --givtcp        Mirror GivTCP's EXACT burst — sends all five frames that
                  GivTCP's fork sends for Gen3 (IR(0,60), IR(180,60),
                  HR(0,60), HR(60,60), HR(120,60) all at slave 0x11)
                  in rapid succession, waits 30s for any response.
                  This is the key test — GivTCP's approach is KNOWN to work.
  --aio           Gateway AIO refresh-rate diagnostic. Actively polls
                  IR(1600,60) and IR(1780,60) every 10 seconds for 10 minutes
                  and labels every frame as POKE_RESPONSE or UNSOLICITED.
                  Answers the key question: does active polling return fresh
                  data, or only the last 5-minute cloud-sync snapshot?
                  Key values decoded: solar, home, battery, grid, SOC.
"""
import socket
import sys
import time
import threading
import configparser
from pathlib import Path
from datetime import datetime

# ── Flags ─────────────────────────────────────────────────────────────────────
ACK_HEARTBEATS  = "--no-ack"      not in sys.argv
HANDSHAKE_MODE  = "--handshake"   in sys.argv
SEQUENTIAL_MODE = "--sequential"  in sys.argv
GIVTCP_MODE     = "--givtcp"      in sys.argv
AIO_MODE        = "--aio"         in sys.argv

POKE_INTERVAL   = 10.0
HB_WAIT_TIMEOUT = 240.0   # 4 minutes to wait for first heartbeat

# ── Frame construction ─────────────────────────────────────────────────────────
_DUMMY_SERIAL       = b"AB1234G567"
_HB_RESPONSE_PREFIX = bytes.fromhex("59590001000d010141423132333447353637")


def _crc16(data: bytes) -> bytes:
    """CRC16-Modbus over the full inner frame (slave+func+base+count), LSB first.
    Gen3/AIO validates CRC including the slave byte — omitting it causes the
    dongle to silently discard requests (confirmed from GivTCP wire capture)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _make_request(slave: int, func: int, base: int = 0, count: int = 60,
                  serial: bytes = _DUMMY_SERIAL) -> bytes:
    """Build a GivEnergy transparent request.
    serial: 10-byte adapter serial to embed. Use real serial from heartbeat
            for active requests; use _DUMMY_SERIAL for heartbeat ACKs.
    """
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([slave, func]) + base.to_bytes(2, "big") + count.to_bytes(2, "big")
    crc     = _crc16(inner)   # includes slave byte — required for Gen3/AIO
    payload = serial + padding + inner + crc
    length  = len(payload) + 2
    return b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload


# IR / HR pokes for normal/handshake modes (dummy serial)
_IR_POKES     = [_make_request(0x32, 0x04), _make_request(0x11, 0x04)]
_HR_HANDSHAKE = [_make_request(0x11, 0x03), _make_request(0x32, 0x03)]

# Sanity-check IR pokes — CRC now includes slave byte (confirmed from GivTCP capture)
assert _IR_POKES[0] == bytes.fromhex(
    "59590001001c010241423132333447353637000000000000000832040000003cf5d8"), \
    "0x32 IR poke CRC mismatch"
assert _IR_POKES[1] == bytes.fromhex(
    "59590001001c010241423132333447353637000000000000000811040000003cf28b"), \
    "0x11 IR poke CRC mismatch — must match GivTCP's frame exactly"

# GivTCP's five-frame burst for Gen3 — all slave 0x11, all dummy serial.
# Source: britkat1980/giv_tcp → GivTCP/givenergy_modbus_async/client/commands.py
# refresh_plant_data() always sends these for a Gen3 with slave_addr=0x11.
# HR(60,60) and HR(120,60) are frames we had NEVER tried before.
_GIVTCP_BURST = [
    _make_request(0x11, 0x04,   0, 60),  # IR(0,60)   live inverter data
    _make_request(0x11, 0x04, 180, 60),  # IR(180,60) extended input regs
    _make_request(0x11, 0x03,   0, 60),  # HR(0,60)   config block 1
    _make_request(0x11, 0x03,  60, 60),  # HR(60,60)  config block 2  ← NEW
    _make_request(0x11, 0x03, 120, 60),  # HR(120,60) config block 3  ← NEW
]

HERE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent


# ── Config ─────────────────────────────────────────────────────────────────────
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


# ── Helpers ────────────────────────────────────────────────────────────────────
def hexdump(data, width=16):
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i+width]
        out.append(f"      {i:04x}  "
                   f"{' '.join(f'{b:02x}' for b in chunk):<{width*3}}  "
                   f"{''.join(chr(b) if 32<=b<127 else '.' for b in chunk)}")
    return "\n".join(out)


def _ts():
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


def _pop_frames(buf: bytearray):
    frames = []
    while True:
        i = buf.find(b"\x59\x59")
        if i < 0:
            if len(buf) > 1: del buf[:-1]
            return frames
        if i > 0: del buf[:i]
        if len(buf) < 6: return frames
        length = (buf[4] << 8) | buf[5]
        total  = 6 + length
        if length <= 0 or length > 4096:
            del buf[:2]; continue
        if len(buf) < total: return frames
        frames.append(bytes(buf[:total]))
        del buf[:total]


def decode_input_frame(frame):
    if len(frame) < 44 or frame[7] != 0x02 or frame[27] != 0x04: return None
    base  = (frame[38] << 8) | frame[39]
    count = (frame[40] << 8) | frame[41]
    if count < 60: return None
    regs_off = 42
    if len(frame) < regs_off + count * 2: return None
    def g(n): o = regs_off + n*2; return (frame[o]<<8)|frame[o+1]
    regs = [g(n) for n in range(count)]
    return {
        "slave": frame[26],
        "base":  base,
        "count": count,
        "regs":  regs,
        # key metrics — only valid when base==0 (standard IR page)
        "soc":   g(59) if base == 0 else 0,
        "solar": (g(18)+g(20)) if base == 0 else 0,
        "home":  g(42) if base == 0 else 0,
    }


def dump_regs(d, log):
    """Print all non-zero registers from a decoded input frame."""
    regs = d["regs"]
    log(f"  Register dump (slave=0x{d['slave']:02x} base={d['base']} count={d['count']}):")
    nonzero = [(i, v) for i, v in enumerate(regs) if v != 0]
    if not nonzero:
        log("    (all registers are zero)")
        return
    # Print in rows of 8
    for i in range(0, len(regs), 8):
        chunk = regs[i:i+8]
        row = "  ".join(f"r{i+j:03d}={v:5d}" for j, v in enumerate(chunk) if v != 0)
        if row:
            log(f"    {row}")
    log(f"  Non-zero registers: {[i for i, v in nonzero]}")


def _ack_heartbeat(s, frame, st, log):
    """ACK heartbeat using DUMMY serial (always AB1234G567, per spec)."""
    st["heartbeats"] += 1
    if ACK_HEARTBEATS:
        try:
            type_byte = frame[18:19] if len(frame) > 18 else b"\x01"
            s.sendall(_HB_RESPONSE_PREFIX + type_byte)
            log(f"HEARTBEAT received  ->  ACK sent (#{st['heartbeats']}, dummy serial)")
        except Exception as exc:
            log(f"HEARTBEAT ACK FAILED: {exc}")
    else:
        log(f"HEARTBEAT received (not acknowledged; #{st['heartbeats']})")


# ── Sequential mode ─────────────────────────────────────────────────────────────

def _run_sequential(s, st, log, logf, binf):
    """
    Sequential test with real adapter serial extraction.

    Phase 1: Wait passively for the heartbeat (up to 4 min).
             Extract adapter serial from frame[8:18].
             ACK heartbeat using dummy serial.
    Phase 2: Test requests A-F with the real serial, one at a time.
             5s wait per request. No reconnect on timeout.
    """
    buf = bytearray()

    # ── helper: receive frames for `duration` seconds ─────────────────────────
    def recv_for(duration, pending_req=None):
        """
        Receive and log frames for `duration` seconds.
        `pending_req`: dict {"slave","func","base","count"} or None.
        Returns list of (frame, elapsed, is_match) for transparent frames.
        """
        results = []
        t0 = time.time()
        while time.time() - t0 < duration:
            remaining = duration - (time.time() - t0)
            s.settimeout(min(remaining, 0.5))
            try:
                data = s.recv(8192)
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError("socket closed by remote host")
            binf.write(data); binf.flush()
            st["bytes"] += len(data)
            buf.extend(data)
            for frame in _pop_frames(buf):
                elapsed = time.time() - t0
                st["frames"] += 1
                outer_func = frame[7] if len(frame) > 7 else 0
                logf.write(f"[{_ts()}] FRAME #{st['frames']} len={len(frame)} "
                            f"func={outer_func:02x}\n")
                logf.write(hexdump(frame) + "\n"); logf.flush()

                if outer_func == 0x01:
                    # Heartbeat — extract serial and ACK
                    if len(frame) >= 18 and frame[8:18] != _DUMMY_SERIAL:
                        new_serial = frame[8:18]
                        s_str = new_serial.decode('ascii', errors='replace')
                        if new_serial != st.get("adapter_serial"):
                            st["adapter_serial"] = new_serial
                            log(f"Heartbeat! Adapter serial extracted: {s_str}")
                    _ack_heartbeat(s, frame, st, log)
                    continue

                if len(frame) < 44 or outer_func != 0x02:
                    continue

                rx_slave = frame[26]
                rx_func  = frame[27]
                rx_base  = (frame[38] << 8) | frame[39]
                rx_count = (frame[40] << 8) | frame[41]

                is_match = (pending_req is not None and
                            rx_slave == pending_req["slave"] and
                            rx_func  == pending_req["func"] and
                            rx_base  == pending_req["base"] and
                            rx_count >= pending_req["count"])

                label = "MATCHED_PENDING_REQUEST" if is_match else "UNSOLICITED_BACKGROUND"
                log(f"RX slave=0x{rx_slave:02x} func=0x{rx_func:02x} "
                    f"base={rx_base} count={rx_count} {label}"
                    + (f" in {elapsed:.3f}s" if is_match else ""))

                results.append((frame, elapsed, is_match))
        return results

    # ── Phase 1: wait for heartbeat ───────────────────────────────────────────
    log("=" * 64)
    log("SEQUENTIAL MODE — real serial test")
    log("=" * 64)
    log("")
    log(f"Phase 1: waiting up to {HB_WAIT_TIMEOUT/60:.0f} minutes for heartbeat "
        f"to extract real adapter serial...")
    log("(Not sending anything yet — just listening)")
    log("")

    st["adapter_serial"] = None
    t_hb_start = time.time()

    while time.time() - t_hb_start < HB_WAIT_TIMEOUT:
        remaining = HB_WAIT_TIMEOUT - (time.time() - t_hb_start)
        print(f"  ... waiting for heartbeat ({remaining:.0f}s remaining) ...", end="\r", flush=True)
        recv_for(min(10.0, remaining), pending_req=None)
        if st.get("adapter_serial"):
            break

    print("", flush=True)  # clear the \r line

    if st.get("adapter_serial"):
        serial = st["adapter_serial"]
        serial_str = serial.decode('ascii', errors='replace')
        log(f"Serial confirmed: {serial_str}")
        log(f"Will use this serial for all active requests.")
    else:
        serial = _DUMMY_SERIAL
        serial_str = _DUMMY_SERIAL.decode('ascii')
        log(f"No heartbeat received within {HB_WAIT_TIMEOUT/60:.0f} minutes.")
        log(f"Falling back to dummy serial: {serial_str}")

    # ── Phase 2: test sequence A-F ─────────────────────────────────────────────
    log("")
    log("Phase 2: testing requests A-F with "
        f"serial={serial_str}")
    log("One request at a time, 5s wait, 1s sleep between.")
    log("")

    TESTS = [
        ("A", 0x32, 0x04,   0, 60, "IR(0,60)   slave=0x32"),
        ("B", 0x11, 0x04,   0, 60, "IR(0,60)   slave=0x11"),
        ("C", 0x32, 0x03,   0, 60, "HR(0,60)   slave=0x32"),
        ("D", 0x11, 0x03,   0, 60, "HR(0,60)   slave=0x11"),
        ("E", 0x32, 0x04, 180, 60, "IR(180,60) slave=0x32"),
        ("F", 0x32, 0x04,  60, 60, "IR(60,60)  slave=0x32"),
        ("G", 0x11, 0x04, 180, 60, "IR(180,60) slave=0x11  ← AIO/Gen3 extended"),
        ("H", 0x11, 0x04,  60, 60, "IR(60,60)  slave=0x11  ← AIO/Gen3 extended"),
    ]

    results_summary = []

    for test_label, slave, func, base, count, desc in TESTS:
        log(f"--- Test {test_label}: {desc} ---")

        req = _make_request(slave, func, base, count, serial=serial)

        # TX logging
        log(f"TX slave=0x{slave:02x} func=0x{func:02x} base={base} "
            f"count={count} serial={serial_str}")
        logf.write(f"[{_ts()}] TX HEX: {req.hex()}\n"); logf.flush()
        print(f"  TX {desc} ... ", end="", flush=True)

        # Send the request
        try:
            s.sendall(req)
        except Exception as exc:
            log(f"  SEND FAILED: {exc}")
            results_summary.append((test_label, desc, None, None))
            time.sleep(1.0)
            continue

        # Receive for 5 seconds
        pending = {"slave": slave, "func": func, "base": base, "count": count}
        rx_frames = recv_for(5.0, pending_req=pending)

        # Find first match
        matched = next(((f, e) for f, e, m in rx_frames if m), None)

        if matched:
            mf, me = matched
            d = decode_input_frame(mf)
            verdict = f"MATCHED in {me:.3f}s"
            if d:
                if d["base"] == 0:
                    verdict += f"  SOC={d['soc']}% solar={d['solar']}W home={d['home']}W"
            log(f"Test {test_label} RESULT: ✓ {verdict}")
            if d:
                dump_regs(d, log)
            print(f"MATCHED in {me:.3f}s", flush=True)
            results_summary.append((test_label, desc, True, me))
        else:
            log(f"Test {test_label} RESULT: ✗ no matching response in 5s")
            print("no response", flush=True)
            results_summary.append((test_label, desc, False, None))

        time.sleep(1.0)

    # ── Phase 2 summary ───────────────────────────────────────────────────────
    log("")
    log("=" * 64)
    log("TEST SUMMARY")
    log("=" * 64)
    log(f"  Adapter serial used: {serial_str}")
    log("")
    for label, desc, matched, elapsed in results_summary:
        if matched:
            log(f"  {label}. {desc:<30}  ✓  RESPONDED in {elapsed:.3f}s")
        elif matched is False:
            log(f"  {label}. {desc:<30}  ✗  no response")
        else:
            log(f"  {label}. {desc:<30}  !  send failed")
    log("")
    log("Keep the session open — watching for background frames (press Ctrl+C to stop)")

    # Keep receiving passively so we capture any cloud sync data
    while True:
        recv_for(30.0, pending_req=None)


# ── GivTCP mode ────────────────────────────────────────────────────────────────

def _run_givtcp(s, st, log, logf, binf):
    """
    Mirror GivTCP's exact Gen3 polling behaviour:

    1. Send all five frames in rapid succession (0.25s apart — matching
       GivTCP's tx_message_wait).
    2. Wait up to 30 seconds for any matching response.
    3. Log each received frame as MATCHED_REQUEST or UNSOLICITED_BACKGROUND.
    4. Repeat every 10 seconds.
    5. Never reconnect on timeout — only on socket error.

    If this gets fast responses where our earlier tests got nothing, one of
    the two new frames (HR(60,60) or HR(120,60)) or the burst pattern itself
    is what GivTCP does that we were missing.
    """
    buf = bytearray()

    GIVTCP_WAIT   = 30.0   # total seconds to wait for responses per burst
    GIVTCP_CYCLE  = 10.0   # seconds between bursts (GivTCP default poll period)
    TX_PACE       = 0.25   # delay between frames (matches GivTCP tx_message_wait)

    # Map of pending requests: (slave, func, base) -> description
    PENDING = {
        (0x11, 0x04,   0): "IR(0,60)   slave=0x11",
        (0x11, 0x04, 180): "IR(180,60) slave=0x11",
        (0x11, 0x03,   0): "HR(0,60)   slave=0x11",
        (0x11, 0x03,  60): "HR(60,60)  slave=0x11  ← NEW",
        (0x11, 0x03, 120): "HR(120,60) slave=0x11  ← NEW",
    }

    log("=" * 64)
    log("GIVTCP MODE — mirroring GivTCP's exact Gen3 burst")
    log("=" * 64)
    log("")
    log("Five frames sent in rapid burst every 10s, wait 30s for response.")
    log("New frames never previously tested: HR(60,60) and HR(120,60).")
    log("")

    cycle = 0
    while True:
        cycle += 1
        log(f"─── Burst #{cycle} ───")

        # Send all five frames with GivTCP-style pacing
        matched_this_cycle = set()
        t_burst = time.time()

        for req in _GIVTCP_BURST:
            slave = req[26]; func = req[27]
            # Request frames are 34 bytes; base is at [28:30], not [38:40]
            base  = (req[28] << 8) | req[29]
            desc  = PENDING.get((slave, func, base), f"0x{slave:02x}/f{func:02x}/b{base}")
            log(f"TX  {desc}  hex={req.hex()}")
            logf.write(f"[{_ts()}] TX HEX: {req.hex()}\n"); logf.flush()
            try:
                s.sendall(req)
            except Exception as exc:
                raise ConnectionError(f"send failed: {exc}")
            time.sleep(TX_PACE)

        print(f"  > burst sent — waiting {GIVTCP_WAIT:.0f}s for responses ...", flush=True)

        # Receive for GIVTCP_WAIT seconds, labelling every frame
        t0 = time.time()
        while time.time() - t0 < GIVTCP_WAIT:
            remaining = GIVTCP_WAIT - (time.time() - t0)
            s.settimeout(min(remaining, 0.5))
            try:
                data = s.recv(8192)
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError("socket closed")
            binf.write(data); binf.flush()
            st["bytes"] += len(data)
            buf.extend(data)

            for frame in _pop_frames(buf):
                elapsed = time.time() - t_burst
                st["frames"] += 1
                outer_func = frame[7] if len(frame) > 7 else 0
                logf.write(f"[{_ts()}] FRAME #{st['frames']} len={len(frame)} "
                           f"func={outer_func:02x}\n")
                logf.write(hexdump(frame) + "\n"); logf.flush()

                if outer_func == 0x01:
                    _ack_heartbeat(s, frame, st, log)
                    continue

                if len(frame) < 44 or outer_func != 0x02:
                    continue

                rx_slave = frame[26]; rx_func = frame[27]
                rx_base  = (frame[38] << 8) | frame[39]
                rx_count = (frame[40] << 8) | frame[41]
                key = (rx_slave, rx_func, rx_base)

                is_match = key in PENDING
                label    = "MATCHED_REQUEST" if is_match else "UNSOLICITED_BACKGROUND"
                desc     = PENDING.get(key, f"slave=0x{rx_slave:02x} "
                                       f"func=0x{rx_func:02x} base={rx_base}")
                log(f"RX  {desc}  count={rx_count}  {label}  ({elapsed:.3f}s after burst)")

                if is_match and key not in matched_this_cycle:
                    matched_this_cycle.add(key)
                    st["replies"] += 1; st["slaves"].add(rx_slave)
                    d = decode_input_frame(frame)
                    if d:
                        if d["base"] == 0:
                            log(f"  → SOC={d['soc']}% solar={d['solar']}W home={d['home']}W")
                        dump_regs(d, log)
                    if elapsed < 5.0:
                        log(f"  *** FAST RESPONSE in {elapsed:.3f}s — "
                            f"GivTCP burst IS working! ***")

        # Summary for this cycle
        if matched_this_cycle:
            descs = [PENDING.get(k, str(k)) for k in matched_this_cycle]
            log(f"Cycle #{cycle} result: {len(matched_this_cycle)} matched — "
                f"{', '.join(descs)}")
        else:
            log(f"Cycle #{cycle} result: no matching responses in {GIVTCP_WAIT:.0f}s")

        # Sleep remainder of the 10-second cycle
        cycle_elapsed = time.time() - t_burst
        sleep_time = max(0, GIVTCP_CYCLE - cycle_elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)


# ── AIO refresh-rate diagnostic ───────────────────────────────────────────────

def _decode_aio_1600(frame):
    """Decode a base=1600 gateway frame into key AIO power values."""
    if len(frame) < 44 or frame[7] != 0x02 or frame[27] != 0x04:
        return None
    base  = (frame[38] << 8) | frame[39]
    count = (frame[40] << 8) | frame[41]
    if base != 1600 or count < 20:
        return None
    if len(frame) < 42 + 20 * 2:
        return None
    def g(n):
        o = 42 + n * 2
        return (frame[o] << 8) | frame[o + 1]
    def s16(v): return v - 65536 if v >= 32768 else v
    return {
        "solar_w":   g(17),
        "home_w":    g(18),
        "bat_w":     s16(g(19)),   # positive=charge, negative=discharge (GivTCP convention)
        "grid_w":    s16(g(16)),   # negative=import, positive=export
        "v_ac":      g(8) / 10,    # AC voltage ×0.1
    }

def _decode_aio_soc(frame):
    """Extract SOC from a base=1780 gateway frame (IR1801 = offset 21)."""
    if len(frame) < 44 or frame[7] != 0x02 or frame[27] != 0x04:
        return None
    base  = (frame[38] << 8) | frame[39]
    count = (frame[40] << 8) | frame[41]
    if base != 1780 or count < 22:
        return None
    if len(frame) < 42 + 22 * 2:
        return None
    soc = (frame[42 + 21*2] << 8) | frame[43 + 21*2]
    return soc if 0 <= soc <= 100 else None


def _run_aio(s, st, log, logf, binf):
    """
    Gateway AIO refresh-rate diagnostic.

    Sends IR(1600,60) and IR(1780,60) pokes to slave 0x11 every 10 seconds,
    then labels every base=1600 and base=1780 frame as either POKE_RESPONSE
    (arrived within 3s of our poke) or UNSOLICITED_BROADCAST.

    This answers definitively: does the gateway return FRESH values when
    actively polled, or only serve its last 5-minute cloud-sync snapshot?

    Key values printed each time a 1600-frame arrives:
      solar, home, battery, grid (all in watts), AC voltage, SOC%
    """
    POLL_SEC      = 10.0    # how often to send pokes
    POKE_WINDOW   = 3.0     # seconds after poke = response counts as POKE_RESPONSE
    RUN_MINUTES   = 10      # total run time
    SOC_INTERVAL  = 60.0    # how often to also poke base=1780 for SOC

    poke_1600 = _make_request(0x11, 0x04, 1600, 60)
    poke_1780 = _make_request(0x11, 0x04, 1780, 60)

    buf               = bytearray()
    last_poke_ts      = 0.0
    last_soc_poke_ts  = 0.0
    last_soc          = None
    poke_count        = 0
    frame_count_1600  = 0
    poke_resp_count   = 0
    unsol_count       = 0
    last_values       = None
    start_ts          = time.time()

    log("=" * 64)
    log("AIO REFRESH-RATE DIAGNOSTIC")
    log("=" * 64)
    log("")
    log(f"Actively polls IR(1600,60) every {POLL_SEC:.0f}s and IR(1780,60) "
        f"every {SOC_INTERVAL:.0f}s for {RUN_MINUTES} minutes.")
    log("Each base=1600 frame is labelled:")
    log("  POKE_RESPONSE   — arrived within 3s of our poke   (= gateway serves fresh data)")
    log("  UNSOLICITED     — arrived outside poke window      (= cloud-sync broadcast)")
    log("")
    log("Key values: solar | home | battery (+=charge +=discharge) | grid (+=export -=import) | SOC%")
    log("-" * 64)

    deadline = start_ts + RUN_MINUTES * 60

    while time.time() < deadline:
        now = time.time()
        elapsed_total = now - start_ts

        # Send pokes
        if now - last_poke_ts >= POLL_SEC:
            s.sendall(poke_1600)
            last_poke_ts = now
            poke_count += 1
            log(f"  [{elapsed_total:5.0f}s] POKE  IR(1600,60) #{poke_count}", to_console=False)
            print(f"  [{elapsed_total:5.0f}s] > poke sent (#{poke_count})", flush=True)

        if now - last_soc_poke_ts >= SOC_INTERVAL:
            time.sleep(0.25)           # space from previous poke
            s.sendall(poke_1780)
            last_soc_poke_ts = now
            log(f"  [{elapsed_total:5.0f}s] POKE  IR(1780,60) for SOC", to_console=False)

        # Receive
        try:
            data = s.recv(8192)
        except socket.timeout:
            data = b""
        if not data:
            continue

        binf.write(data); binf.flush()
        st["bytes"] += len(data)
        buf.extend(data)

        for frame in _pop_frames(buf):
            st["frames"] += 1
            outer_func = frame[7] if len(frame) > 7 else 0

            # Log raw frame
            logf.write(f"[{_ts()}] FRAME #{st['frames']} len={len(frame)} "
                       f"func={outer_func:02x}\n")
            logf.write(hexdump(frame) + "\n"); logf.flush()

            # Heartbeat
            if outer_func == 0x01:
                if len(frame) >= 18:
                    st["adapter_serial"] = frame[8:18]
                _ack_heartbeat(s, frame, st, log)
                continue

            if len(frame) < 44 or outer_func != 0x02:
                continue

            rx_base  = (frame[38] << 8) | frame[39]
            rx_count = (frame[40] << 8) | frame[41]
            rx_slave = frame[26]
            elapsed_since_poke = now - last_poke_ts

            # ── base=1600: key AIO live data ──────────────────────────────
            if rx_base == 1600:
                frame_count_1600 += 1
                is_poke_resp = elapsed_since_poke < POKE_WINDOW
                label = "POKE_RESPONSE  " if is_poke_resp else "UNSOLICITED    "
                if is_poke_resp:
                    poke_resp_count += 1
                else:
                    unsol_count += 1

                d = _decode_aio_1600(frame)
                soc_str = f"SOC={last_soc}%" if last_soc is not None else "SOC=?"

                if d:
                    bat_dir = "chg" if d["bat_w"] >= 0 else "dis"
                    grd_dir = "exp" if d["grid_w"] >= 0 else "imp"
                    values_str = (
                        f"solar={d['solar_w']:5d}W  "
                        f"home={d['home_w']:5d}W  "
                        f"bat={abs(d['bat_w']):5d}W({bat_dir})  "
                        f"grid={abs(d['grid_w']):4d}W({grd_dir})  "
                        f"{soc_str}  "
                        f"Vac={d['v_ac']:.1f}V"
                    )

                    # Flag if values changed from last reading
                    changed = ""
                    if last_values and d:
                        diffs = []
                        for k in ("solar_w", "home_w", "bat_w", "grid_w"):
                            if abs(d[k] - last_values.get(k, d[k])) > 10:
                                diffs.append(k.replace("_w", ""))
                        if diffs:
                            changed = f"  << CHANGED: {', '.join(diffs)}"
                    last_values = d

                    msg = (f"[{elapsed_total:5.0f}s] {label}  "
                           f"{values_str}{changed}")
                else:
                    msg = (f"[{elapsed_total:5.0f}s] {label}  "
                           f"(decode failed, count={rx_count})")

                log(msg)

            # ── base=1780: SOC update ─────────────────────────────────────
            elif rx_base == 1780:
                elapsed_since_soc_poke = now - last_soc_poke_ts
                is_poke_resp = elapsed_since_soc_poke < POKE_WINDOW
                soc_label = "POKE_RESPONSE" if is_poke_resp else "UNSOLICITED  "
                soc = _decode_aio_soc(frame)
                if soc is not None:
                    last_soc = soc
                    log(f"[{elapsed_total:5.0f}s] SOC {soc_label}  aio1_soc = {soc}%")

            # ── other bases: just note them ───────────────────────────────
            else:
                if rx_base not in (0, 180):   # skip noisy known-zero pages
                    log(f"[{elapsed_total:5.0f}s] OTHER  slave=0x{rx_slave:02x} "
                        f"base={rx_base} count={rx_count}  (logged to file)")

    # ── Summary ───────────────────────────────────────────────────────────────
    log("")
    log("=" * 64)
    log("AIO DIAGNOSTIC SUMMARY")
    log("=" * 64)
    log(f"  Run time:           {RUN_MINUTES} minutes")
    log(f"  Pokes sent:         {poke_count}  (every {POLL_SEC:.0f}s)")
    log(f"  base=1600 frames:   {frame_count_1600}")
    log(f"    POKE_RESPONSE:    {poke_resp_count}")
    log(f"    UNSOLICITED:      {unsol_count}")
    log("")
    if poke_resp_count > 0:
        log("  RESULT: Gateway DOES respond to IR(1600) pokes.")
        log("  Next step: check if POKE_RESPONSE values change each poll,")
        log("             or stay frozen until a cloud-sync UNSOLICITED frame arrives.")
        if unsol_count > 0:
            log("  Both poke responses AND unsolicited frames seen.")
            log("  Compare their values in the log to confirm whether poke data is fresh.")
    elif unsol_count > 0:
        log("  RESULT: Gateway does NOT respond to IR(1600) pokes.")
        log("  Data only arrives in unsolicited cloud-sync broadcasts (~5 min intervals).")
        log("  Dashboard should use passive listen-only mode for this device.")
    else:
        log("  RESULT: No base=1600 frames received at all.")
        log("  Check inverter IP, port 8899, and that no other app is connected.")
    log("")
    stamp_now = datetime.now().strftime("%Y%m%d_%H%M%S")
    log(f"Please send back:  capture_{stamp_now}.log  and  capture_{stamp_now}.bin")
    log("(the .log AND .bin files)")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ip, port = load_config()
    stamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = HERE / f"capture_{stamp}.log"
    bin_path = HERE / f"capture_{stamp}.bin"
    logf     = open(log_path, "w", encoding="utf-8")
    binf     = open(bin_path, "wb")

    def log(msg, to_console=True):
        line = f"[{_ts()}] {msg}"
        if to_console: print(line, flush=True)
        logf.write(line + "\n"); logf.flush()

    mode_str = ("AIO refresh-rate diagnostic"          if AIO_MODE        else
                "GIVTCP (5-frame burst at 0x11)"       if GIVTCP_MODE     else
                "SEQUENTIAL (real serial + A-F test)" if SEQUENTIAL_MODE else
                "HANDSHAKE (IR+HR poke-and-listen)"   if HANDSHAKE_MODE  else
                "NORMAL (poke-and-listen)")

    log("=== GivEnergy connection diagnostic ===")
    log(f"Target:        {ip}:{port}")
    log(f"Mode:          {mode_str}")
    log(f"Heartbeat ACK: {'ON (dummy serial)' if ACK_HEARTBEATS else 'OFF (--no-ack)'}")
    log(f"Raw bytes:     {bin_path.name}")
    log("Ctrl+C to stop. Type a note + Enter to mark an event.")
    log("-" * 64)

    threading.Thread(
        target=lambda: [log(f"NOTE >>> {l.strip()}") for l in sys.stdin if l.strip()],
        daemon=True).start()

    st = {
        "frames": 0, "bytes": 0, "replies": 0, "heartbeats": 0,
        "last_reply": 0.0, "last_poke": 0.0,
        "max_gap": 0.0, "gaps_over_30s": 0,
        "slaves": set(), "last_report": time.time(),
        "sock": None, "warned": False,
        "adapter_serial": None,  # extracted from heartbeat
        "pending": None,         # current pending request for RX labeling
    }

    # ── frame handler for normal/handshake modes ──────────────────────────────
    def handle_frame(frame):
        st["frames"] += 1
        func = frame[7] if len(frame) > 7 else 0
        logf.write(f"[{_ts()}] FRAME #{st['frames']} len={len(frame)} func={func:02x}\n")
        logf.write(hexdump(frame) + "\n"); logf.flush()
        if func == 0x01:
            if len(frame) >= 18:
                st["adapter_serial"] = frame[8:18]
            _ack_heartbeat(st["sock"], frame, st, log)
            return
        d = decode_input_frame(frame)
        if not d: return
        now  = time.time()
        gap  = (now - st["last_reply"]) if st["last_reply"] else 0.0
        spok = (now - st["last_poke"])  if st["last_poke"]  else -1
        st["replies"] += 1; st["slaves"].add(d["slave"])
        if st["last_reply"] and gap > st["max_gap"]: st["max_gap"] = gap
        if st["last_reply"] and gap > 30:            st["gaps_over_30s"] += 1
        st["last_reply"] = now; st["warned"] = False
        if d["base"] == 0:
            log(f"REPLY  slave=0x{d['slave']:02x}  SOC={d['soc']}%  "
                f"solar={d['solar']}W  home={d['home']}W  "
                f"(gap:{gap:5.1f}s, {spok:.1f}s after poke)")
        else:
            log(f"REPLY  slave=0x{d['slave']:02x}  base={d['base']}  "
                f"(gap:{gap:5.1f}s, {spok:.1f}s after poke)")
        dump_regs(d, log)

    def process_buffer(buf):
        for frame in _pop_frames(buf):
            handle_frame(frame)

    buf = bytearray()
    try:
        while True:
            try:
                log(f"Connecting to {ip}:{port} ...")
                s = socket.create_connection((ip, port), timeout=15)
                s.settimeout(2)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                st["sock"] = s
                log("Connected.")

                if AIO_MODE:
                    _run_aio(s, st, log, logf, binf)
                    break   # _run_aio has its own run-time; exit outer loop
                elif GIVTCP_MODE:
                    _run_givtcp(s, st, log, logf, binf)
                elif SEQUENTIAL_MODE:
                    _run_sequential(s, st, log, logf, binf)

                else:
                    if HANDSHAKE_MODE:
                        log("Handshake: sending initial HR reads...")
                        for f in _HR_HANDSHAKE: s.sendall(f)
                        time.sleep(0.5)

                    while True:
                        now = time.time()
                        if now - st["last_poke"] >= POKE_INTERVAL:
                            pokes = (_HR_HANDSHAKE if HANDSHAKE_MODE else []) + _IR_POKES
                            for f in pokes: s.sendall(f)
                            st["last_poke"] = now
                            mode = "IR+HR" if HANDSHAKE_MODE else "IR"
                            log(f"POKES sent ({mode})", to_console=False)
                            print("  > pokes sent", flush=True)
                        try:
                            data = s.recv(8192)
                        except socket.timeout:
                            data = b""
                        if data:
                            binf.write(data); binf.flush()
                            st["bytes"] += len(data)
                            buf.extend(data); process_buffer(buf)
                        t = time.time()
                        if st["last_reply"] and t-st["last_reply"] > 30 and not st["warned"]:
                            log(f"!! No reply for {t-st['last_reply']:.0f}s")
                            st["warned"] = True
                        if t - st["last_report"] > 30:
                            sl = ", ".join(f"0x{x:02x}" for x in sorted(st["slaves"])) or "none"
                            log(f"... {st['replies']} replies, max gap {st['max_gap']:.0f}s, "
                                f"slave(s): {sl}")
                            st["last_report"] = t

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log(f"Connection error: {exc} — reconnecting in 5s")
                time.sleep(5)

    except KeyboardInterrupt:
        pass

    finally:
        sl = ", ".join(f"0x{x:02x}" for x in sorted(st["slaves"])) or "none seen"
        serial_found = st.get("adapter_serial")
        serial_str   = serial_found.decode('ascii', errors='replace') if serial_found else "not seen"
        log("-" * 64)
        log("RESULT SUMMARY")
        log(f"  Mode:                 {mode_str}")
        log(f"  Adapter serial seen:  {serial_str}")
        log(f"  Replies received:     {st['replies']}")
        log(f"  Slave address(es):    {sl}")
        log(f"  Longest gap:          {st['max_gap']:.0f}s")
        log(f"  Heartbeats / acked:   {st['heartbeats']} / {'yes' if ACK_HEARTBEATS else 'no'}")
        log(f"  Total frames / bytes: {st['frames']} / {st['bytes']}")
        log(f"Please send back:  {bin_path.name}  and  {log_path.name}")
        binf.close(); logf.close()


if __name__ == "__main__":
    main()
