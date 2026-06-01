#!/usr/bin/env python3
"""
GivEnergy inverter — connection diagnostic & capture tool

Flags:
  --no-ack        Don't respond to heartbeats
  --handshake     Send HR reads alongside IR pokes (previous test)
  --sequential    Proper Modbus client: wait for heartbeat, extract real
                  adapter serial, then test A-F with that serial.
                  This is the definitive test for whether the serial is
                  what makes the Gen3 respond.
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

POKE_INTERVAL   = 10.0
HB_WAIT_TIMEOUT = 240.0   # 4 minutes to wait for first heartbeat

# ── Frame construction ─────────────────────────────────────────────────────────
_DUMMY_SERIAL       = b"AB1234G567"
_HB_RESPONSE_PREFIX = bytes.fromhex("59590001000d010141423132333447353637")


def _crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def _make_request(slave: int, func: int, base: int = 0, count: int = 60,
                  serial: bytes = _DUMMY_SERIAL) -> bytes:
    """Build a GivEnergy transparent request.
    serial: 10-byte adapter serial to embed. Use real serial from heartbeat
            for active requests; use _DUMMY_SERIAL for heartbeat ACKs.
    """
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([slave, func]) + base.to_bytes(2, "big") + count.to_bytes(2, "big")
    crc     = _crc16(inner[1:])
    payload = serial + padding + inner + crc
    length  = len(payload) + 2
    return b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload


# IR / HR pokes for normal/handshake modes (dummy serial)
_IR_POKES     = [_make_request(0x32, 0x04), _make_request(0x11, 0x04)]
_HR_HANDSHAKE = [_make_request(0x11, 0x03), _make_request(0x32, 0x03)]

# Sanity-check IR pokes against known-good captured frames
assert _IR_POKES[0] == bytes.fromhex(
    "59590001001c010241423132333447353637000000000000000832040000003cd1d5"), \
    "0x32 IR poke CRC mismatch"
assert _IR_POKES[1] == bytes.fromhex(
    "59590001001c010241423132333447353637000000000000000811040000003cd1d5"), \
    "0x11 IR poke CRC mismatch"

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
    if len(frame) < 164 or frame[7] != 0x02 or frame[27] != 0x04: return None
    if (frame[38] << 8 | frame[39]) != 0 or (frame[40] << 8 | frame[41]) < 60: return None
    def g(n): o = 42+n*2; return (frame[o]<<8)|frame[o+1]
    return {"slave": frame[26], "soc": g(59), "solar": g(18)+g(20), "home": g(42)}


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
                verdict += f"  SOC={d['soc']}% solar={d['solar']}W home={d['home']}W"
            log(f"Test {test_label} RESULT: ✓ {verdict}")
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

    mode_str = ("SEQUENTIAL (real serial + A-F test)" if SEQUENTIAL_MODE else
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
        log(f"REPLY  slave=0x{d['slave']:02x}  SOC={d['soc']}%  "
            f"solar={d['solar']}W  home={d['home']}W  "
            f"(gap:{gap:5.1f}s, {spok:.1f}s after poke)")

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

                if SEQUENTIAL_MODE:
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
