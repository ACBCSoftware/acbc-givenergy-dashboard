#!/usr/bin/env python3
"""
GivEnergy inverter — connection diagnostic & capture tool
=========================================================
Flags:
  --no-ack        Don't respond to heartbeats (comparison test)
  --handshake     Send HR reads alongside IR pokes (previous test — inconclusive)
  --sequential    THE KEY TEST: behave like a proper Modbus client —
                  detect the device first (HR read), then poll one IR request
                  at a time and wait for the exact matching response.
                  If Gen3 replies in <2s instead of ~5 minutes, this is the fix.

Output:  capture_<timestamp>.bin + .log  (next to the exe)
Notes:   type a note + Enter at any time to timestamp it in the log.
         Ctrl+C to stop.
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

POKE_INTERVAL   = 10.0   # seconds between poke sets (normal/handshake modes)
SEQ_TIMEOUT     = 2.0    # seconds to wait for a response (sequential mode)
SEQ_PACE        = 10.0   # seconds between sequential requests

# ── Frame construction ─────────────────────────────────────────────────────────
_DUMMY_SERIAL       = b"AB1234G567"
_HB_RESPONSE_PREFIX = bytes.fromhex("59590001000d010141423132333447353637")


def _crc16(data: bytes) -> bytes:
    """CRC16-Modbus (covers func+base+count only, not the slave byte)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def _make_request(slave: int, func: int, base: int = 0, count: int = 60) -> bytes:
    """Build a properly-framed GivEnergy transparent request (matches library wire format)."""
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([slave, func]) + base.to_bytes(2, "big") + count.to_bytes(2, "big")
    crc     = _crc16(inner[1:])
    payload = _DUMMY_SERIAL + padding + inner + crc
    length  = len(payload) + 2
    return b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload


# IR pokes (normal / handshake modes)
_IR_POKES = [_make_request(0x32, 0x04), _make_request(0x11, 0x04)]
_HR_HANDSHAKE = [_make_request(0x11, 0x03), _make_request(0x32, 0x03)]

# Sanity-check IR pokes against known-good captured frames
assert _IR_POKES[0] == bytes.fromhex(
    "59590001001c010241423132333447353637000000000000000832040000003cd1d5"), \
    "0x32 IR poke mismatch — CRC bug"
assert _IR_POKES[1] == bytes.fromhex(
    "59590001001c010241423132333447353637000000000000000811040000003cd1d5"), \
    "0x11 IR poke mismatch — CRC bug"

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
                   f"{'  '.join(f'{b:02x}' for b in chunk):<{width*3}}  "
                   f"{''.join(chr(b) if 32<=b<127 else '.' for b in chunk)}")
    return "\n".join(out)


def _ts():
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


def _pop_frames(buf: bytearray):
    """Extract complete GivEnergy frames from buf in-place."""
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
    """Decode an IR(0,60) response. Returns {slave, soc, solar, home} or None."""
    if len(frame) < 164 or frame[7] != 0x02 or frame[27] != 0x04:
        return None
    if (frame[38] << 8 | frame[39]) != 0 or (frame[40] << 8 | frame[41]) < 60:
        return None
    def g(n): o = 42 + n*2; return (frame[o] << 8) | frame[o+1]
    return {"slave": frame[26], "soc": g(59), "solar": g(18)+g(20), "home": g(42)}


def decode_hr_frame(frame):
    """Decode an HR(0,60) response. Returns {slave, dtc, arm_fw} or None."""
    if len(frame) < 44 or frame[7] != 0x02 or frame[27] != 0x03:
        return None
    if (frame[38] << 8 | frame[39]) != 0:
        return None
    def g(n): o = 42 + n*2; return (frame[o] << 8) | frame[o+1] if o+2 <= len(frame) else 0
    return {"slave": frame[26], "dtc": g(0), "arm_fw": g(21)}


def classify_device(dtc: int, arm_fw: int) -> str:
    """Human-readable model name from DTC and ARM firmware values."""
    if dtc == 0 and arm_fw == 0:
        return "UNKNOWN (HR returned zeros — device may not support HR reads)"
    family = (dtc >> 12) & 0xF
    fw_c   = arm_fw // 100
    if family == 2:
        if fw_c == 3:   return f"HYBRID_GEN3   (DTC=0x{dtc:04x} ARM_fw={arm_fw})"
        if fw_c in (8,9): return f"HYBRID_GEN2   (DTC=0x{dtc:04x} ARM_fw={arm_fw})"
        return f"HYBRID        (DTC=0x{dtc:04x} ARM_fw={arm_fw})"
    if family == 3: return f"AC_COUPLER    (DTC=0x{dtc:04x})"
    if family == 4: return f"HYBRID_3PH    (DTC=0x{dtc:04x})"
    if family == 5: return f"EMS           (DTC=0x{dtc:04x})"
    if family == 6: return f"AC_3PH        (DTC=0x{dtc:04x})"
    if family == 7: return f"GATEWAY       (DTC=0x{dtc:04x})"
    if family == 8: return f"ALL_IN_ONE/HV (DTC=0x{dtc:04x})"
    return f"UNKNOWN       (DTC=0x{dtc:04x} ARM_fw={arm_fw})"


def _ack_heartbeat(s, frame, st, log):
    """Acknowledge a heartbeat frame (dummy serial, correct type byte)."""
    st["heartbeats"] += 1
    if ACK_HEARTBEATS:
        try:
            type_byte = frame[18:19] if len(frame) > 18 else b"\x01"
            s.sendall(_HB_RESPONSE_PREFIX + type_byte)
            log(f"HEARTBEAT received  ->  ACK sent (#{st['heartbeats']})")
        except Exception as exc:
            log(f"HEARTBEAT received  ->  ACK FAILED: {exc}")
    else:
        log(f"HEARTBEAT received  (not acknowledged; #{st['heartbeats']})")


# ── Sequential mode: send one request, wait for the exact matching response ────

def _send_and_wait(s, request, match_func, match_base, match_count,
                   timeout, st, log, logf, binf):
    """
    Send `request` and block until a transparent frame arrives that matches
    (inner func, base, count). Heartbeats are acked inline and don't break
    the wait. Returns (frame, elapsed_s) or (None, elapsed_s) on timeout.
    All received bytes are written to binf; every frame is hexdumped to logf.
    """
    s.sendall(request)
    t0  = time.time()
    buf = bytearray()

    while True:
        remaining = timeout - (time.time() - t0)
        if remaining <= 0:
            return None, time.time() - t0

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
            st["frames"] += 1
            func = frame[7] if len(frame) > 7 else 0
            logf.write(f"[{_ts()}] FRAME #{st['frames']} len={len(frame)} func={func:02x}\n")
            logf.write(hexdump(frame) + "\n"); logf.flush()

            if func == 0x01:                           # heartbeat
                _ack_heartbeat(s, frame, st, log)
                continue

            if len(frame) < 44 or frame[7] != 0x02:   # not transparent
                continue
            if frame[27] != match_func:                # wrong inner func
                continue
            f_base  = (frame[38] << 8) | frame[39]
            f_count = (frame[40] << 8) | frame[41]
            if f_base == match_base and f_count >= match_count:
                return frame, time.time() - t0          # ✓ matched


def _run_sequential(s, st, log, logf, binf):
    """
    The key test loop. Behaves like a proper Modbus client:
      1. Send HR(0,60) to 0x11 — detect the device
      2. Loop: send IR(0,60) to 0x11, wait up to SEQ_TIMEOUT seconds
               measure whether the response arrives in <2s or ~300s
    Raises an exception to trigger reconnect.
    """
    # ── Phase 1: detection ────────────────────────────────────────────────────
    log("=" * 64)
    log("SEQUENTIAL MODE — behaving like a proper Modbus client")
    log("=" * 64)
    log("")
    log("Phase 1: sending HR(0,60) to slave 0x11 for device detection...")

    hr_req = _make_request(0x11, 0x03, 0, 60)
    frame, elapsed = _send_and_wait(
        s, hr_req, 0x03, 0, 60, timeout=5.0,
        st=st, log=log, logf=logf, binf=binf)

    if frame:
        hr = decode_hr_frame(frame)
        if hr and (hr["dtc"] or hr["arm_fw"]):
            model = classify_device(hr["dtc"], hr["arm_fw"])
            log(f"DETECTION OK ({elapsed:.2f}s): {model}")
            log(f"  slave in response = 0x{hr['slave']:02x}")
        else:
            log(f"HR response received in {elapsed:.2f}s but registers are zero.")
            log(f"  Device may not support HR reads, or slave 0x11 is wrong.")
    else:
        log(f"No HR response within 5s.")
        log(f"  The inverter did not respond to HR(0,60) at slave 0x11.")
        log(f"  Proceeding to IR polling anyway...")

    log("")
    log("Phase 2: polling IR(0,60) at slave 0x11 — ONE request at a time")
    log(f"  Wait up to {SEQ_TIMEOUT}s for response, then send next request after {SEQ_PACE}s")
    log("")
    log("*** KEY RESULT: are IR replies arriving in <2s (FAST) or ~300s (slow)? ***")
    log("-" * 64)

    # ── Phase 2: sequential IR poll ───────────────────────────────────────────
    RETRY_LIMIT = 3
    fails = 0

    while True:
        ir_req = _make_request(0x11, 0x04, 0, 60)
        t_send = time.time()
        print(f"  > IR request sent to 0x11 ...", end=" ", flush=True)
        log(f"IR REQUEST → slave=0x11  IR(0,60)", to_console=False)

        frame, elapsed = _send_and_wait(
            s, ir_req, 0x04, 0, 60, timeout=SEQ_TIMEOUT,
            st=st, log=log, logf=logf, binf=binf)

        if frame:
            d = decode_input_frame(frame)
            if d:
                now = time.time()
                gap = (now - st["last_reply"]) if st["last_reply"] else 0.0
                if st["last_reply"] and gap > st["max_gap"]: st["max_gap"] = gap
                if st["last_reply"] and gap > 30:            st["gaps_over_30s"] += 1
                st["replies"] += 1
                st["slaves"].add(d["slave"])
                st["last_reply"] = now
                fails = 0
                verdict = "FAST — sequential polling IS working!" if elapsed < SEQ_TIMEOUT else f"{elapsed:.2f}s"
                print(f"REPLY in {elapsed:.3f}s  ← {verdict}", flush=True)
                log(f"IR REPLY  slave=0x{d['slave']:02x}  SOC={d['soc']}%  "
                    f"solar={d['solar']}W  home={d['home']}W  "
                    f"elapsed={elapsed:.3f}s  gap={gap:.1f}s")
                if elapsed < SEQ_TIMEOUT * 0.8:
                    log(f"*** FAST REPLY ({elapsed:.3f}s) — "
                        f"sequential request-response mode is working! ***")
            else:
                print(f"frame received but couldn't decode (slave=0x{frame[26]:02x} "
                      f"func=0x{frame[27]:02x})", flush=True)
        else:
            fails += 1
            print(f"NO RESPONSE (>{SEQ_TIMEOUT}s timeout) [{fails}/{RETRY_LIMIT}]", flush=True)
            log(f"!! IR request timed out — no response within {SEQ_TIMEOUT}s  "
                f"[fail {fails}/{RETRY_LIMIT}]")
            if fails >= RETRY_LIMIT:
                raise ConnectionError(f"no IR response after {RETRY_LIMIT} attempts — reconnecting")

        # pace: aim for SEQ_PACE seconds between sends
        wait = max(0, SEQ_PACE - (time.time() - t_send))
        if wait > 0:
            time.sleep(wait)


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

    mode_str = ("SEQUENTIAL (detect+request-response)" if SEQUENTIAL_MODE else
                "HANDSHAKE (IR+HR poke-and-listen)"    if HANDSHAKE_MODE  else
                "NORMAL (poke-and-listen)")

    log("=== GivEnergy connection diagnostic ===")
    log(f"Target:       {ip}:{port}")
    log(f"Mode:         {mode_str}")
    log(f"Heartbeat ACK: {'ON (dummy serial)' if ACK_HEARTBEATS else 'OFF (--no-ack)'}")
    log(f"Raw bytes:    {bin_path.name}")
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
    }

    # ── frame handler for normal/handshake modes ──────────────────────────────
    def handle_frame(frame):
        st["frames"] += 1
        func = frame[7] if len(frame) > 7 else 0
        logf.write(f"[{_ts()}] FRAME #{st['frames']} len={len(frame)} func={func:02x}\n")
        logf.write(hexdump(frame) + "\n"); logf.flush()
        if func == 0x01:
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
                            log(f"!! No reply for {t-st['last_reply']:.0f}s "
                                f"— this is the disconnect symptom")
                            st["warned"] = True
                        if t - st["last_report"] > 30:
                            sl = ", ".join(f"0x{x:02x}" for x in sorted(st["slaves"])) or "none"
                            log(f"... summary: {st['replies']} replies, "
                                f"max gap {st['max_gap']:.0f}s, "
                                f"gaps>30s: {st['gaps_over_30s']}, slave(s): {sl}")
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
        log("-" * 64)
        log("RESULT SUMMARY")
        log(f"  Mode:                 {mode_str}")
        log(f"  Replies received:     {st['replies']}")
        log(f"  Slave address(es):    {sl}")
        log(f"  Longest gap:          {st['max_gap']:.0f}s")
        log(f"  Gaps over 30s:        {st['gaps_over_30s']}")
        log(f"  Heartbeats / acked:   {st['heartbeats']} / {'yes' if ACK_HEARTBEATS else 'no'}")
        log(f"  Total frames / bytes: {st['frames']} / {st['bytes']}")
        log(f"Please send back:  {bin_path.name}  and  {log_path.name}")
        binf.close(); logf.close()


if __name__ == "__main__":
    main()
