#!/usr/bin/env python3
"""
GivEnergy inverter — connection diagnostic & capture tool
=========================================================
Connects to a GivEnergy inverter's local port (8899), sends read requests,
decodes replies, and measures connection reliability.

Flags:
  --no-ack        Receive heartbeats but send no response (comparison test)
  --handshake     Also send Holding Register reads before and during IR polls,
                  mimicking what the givenergy-modbus library does at startup.
                  This is the key test for Gen3/AIO — if data comes every 10s
                  instead of every 5 minutes, the handshake is what's needed.

Output files (written next to the exe):
  capture_<timestamp>.bin  — every raw byte received
  capture_<timestamp>.log  — readable timeline of pokes, replies, heartbeats

Type a note + Enter at any time to drop a timestamped marker in the log.
Stop with Ctrl+C or by closing the window.
"""
import socket
import sys
import time
import threading
import configparser
from pathlib import Path
from datetime import datetime

# ── Flags ────────────────────────────────────────────────────────────────────
ACK_HEARTBEATS = "--no-ack"    not in sys.argv
HANDSHAKE_MODE = "--handshake" in sys.argv

POKE_INTERVAL = 10   # seconds between poke sets

# ── Frame construction ────────────────────────────────────────────────────────
_DUMMY_SERIAL = b"AB1234G567"     # library default; dongle ignores it on inbound
_HB_RESPONSE_PREFIX = bytes.fromhex("59590001000d010141423132333447353637")


def _crc16(data: bytes) -> bytes:
    """CRC16-Modbus, LSB-first output (matching the wire format in captures)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def _make_request(slave: int, func: int, base: int = 0, count: int = 60) -> bytes:
    """Build a properly-framed GivEnergy transparent request.

    Matches the givenergy-modbus library's wire format exactly:
      outer: 5959 0001 <len> 01 02
      payload: <serial(10)> <padding=0x08 as 64-bit BE> <slave> <func>
               <base(2)> <count(2)> <crc16(slave+func+base+count)>
    """
    padding = b"\x00" * 7 + b"\x08"           # 0x08 as 64-bit big-endian
    inner   = bytes([slave, func]) + base.to_bytes(2, "big") + count.to_bytes(2, "big")
    crc     = _crc16(inner[1:])   # CRC covers func+base+count only, not the slave byte
    payload = _DUMMY_SERIAL + padding + inner + crc
    length  = len(payload) + 2                 # +2 for outer uid + func bytes
    header  = b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02"
    return header + payload


# Input-register reads (live data) — what we send as the normal "poke"
_IR_POKES = [
    _make_request(0x32, 0x04),   # Gen2 inverter
    _make_request(0x11, 0x04),   # Gen3 / AIO / HV hybrid
]

# Holding-register reads (configuration) — used in --handshake mode.
# This is what givenergy-modbus sends during detect() to put the dongle
# into poll-response mode so it answers every 10s instead of every 5 min.
_HR_HANDSHAKE = [
    _make_request(0x11, 0x03),   # HR read at 0x11 — the library's detect() step
    _make_request(0x32, 0x03),   # HR read at 0x32 — the library's refresh step
]

# Verify our IR pokes match the known-good captured frames exactly.
_KNOWN_0x32 = bytes.fromhex(
    "59590001001c010241423132333447353637000000000000000832040000003cd1d5")
_KNOWN_0x11 = bytes.fromhex(
    "59590001001c010241423132333447353637000000000000000811040000003cd1d5")
assert _IR_POKES[0] == _KNOWN_0x32, \
    f"0x32 poke mismatch: {_IR_POKES[0].hex()} != {_KNOWN_0x32.hex()}"
assert _IR_POKES[1] == _KNOWN_0x11, \
    f"0x11 poke mismatch: {_IR_POKES[1].hex()} != {_KNOWN_0x11.hex()}"

if getattr(sys, "frozen", False):
    HERE = Path(sys.executable).parent
else:
    HERE = Path(__file__).parent


# ── Config ────────────────────────────────────────────────────────────────────
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
def hexdump(data, width=16):
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"      {i:04x}  {hexpart:<{width*3}}  {asc}")
    return "\n".join(out)


def decode_input_frame(frame):
    """Decode a 'read input registers 0-59' reply; return key values or None."""
    if len(frame) < 164 or frame[7] != 0x02:
        return None
    if frame[27] != 0x04:
        return None
    base  = (frame[38] << 8) | frame[39]
    count = (frame[40] << 8) | frame[41]
    if base != 0 or count < 60:
        return None
    def g(n):
        o = 42 + n * 2
        return (frame[o] << 8) | frame[o + 1]
    return {
        "slave": frame[26],
        "soc":   g(59),
        "solar": g(18) + g(20),
        "home":  g(42),
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ip, port = load_config()
    stamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = HERE / f"capture_{stamp}.log"
    bin_path = HERE / f"capture_{stamp}.bin"
    logf     = open(log_path, "w", encoding="utf-8")
    binf     = open(bin_path, "wb")

    def log(msg, to_console=True):
        line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
        if to_console:
            print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    log("=== GivEnergy connection diagnostic ===")
    log(f"Target:       {ip}:{port}")
    log(f"Raw bytes:    {bin_path.name}")
    log(f"Log file:     {log_path.name}")
    log(f"Heartbeat ACK: {'ON (dummy serial)' if ACK_HEARTBEATS else 'OFF (--no-ack)'}")
    log(f"Handshake:    {'ON — sending HR reads to trigger poll-response mode (--handshake)' if HANDSHAKE_MODE else 'OFF (normal poke-only mode)'}")
    log("Ctrl+C to stop. Type a note + Enter to mark an event.")
    log("-" * 64)

    threading.Thread(
        target=lambda: [log(f"NOTE >>> {l.strip()}") for l in sys.stdin if l.strip()],
        daemon=True
    ).start()

    st = {
        "frames": 0, "bytes": 0, "replies": 0, "heartbeats": 0,
        "last_reply": 0.0, "last_poke": 0.0,
        "max_gap": 0.0, "gaps_over_30s": 0,
        "slaves": set(), "last_report": time.time(), "sock": None,
        "warned": False,
    }

    def handle_frame(frame):
        st["frames"] += 1
        func = frame[7] if len(frame) > 7 else 0
        logf.write(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
                   f"FRAME #{st['frames']} len={len(frame)} func={func:02x}\n")
        logf.write(hexdump(frame) + "\n"); logf.flush()

        # Heartbeat: respond with dummy serial (NOT echoing the real serial)
        if func == 0x01:
            st["heartbeats"] += 1
            if ACK_HEARTBEATS and st.get("sock") is not None:
                try:
                    type_byte = frame[18:19] if len(frame) > 18 else b"\x01"
                    st["sock"].sendall(_HB_RESPONSE_PREFIX + type_byte)
                    log(f"HEARTBEAT received  ->  ACK sent (#{st['heartbeats']})")
                except Exception as exc:
                    log(f"HEARTBEAT received  ->  ACK FAILED: {exc}")
            else:
                log(f"HEARTBEAT received  (not acknowledged; #{st['heartbeats']})")
            return

        d = decode_input_frame(frame)
        if not d:
            return
        now = time.time()
        gap = (now - st["last_reply"]) if st["last_reply"] else 0.0
        since_poke = (now - st["last_poke"]) if st["last_poke"] else -1
        st["replies"] += 1
        st["slaves"].add(d["slave"])
        if st["last_reply"] and gap > st["max_gap"]:
            st["max_gap"] = gap
        if st["last_reply"] and gap > 30:
            st["gaps_over_30s"] += 1
        st["last_reply"] = now
        st["warned"] = False
        log(f"REPLY  slave=0x{d['slave']:02x}  SOC={d['soc']}%  "
            f"solar={d['solar']}W  home={d['home']}W  "
            f"(gap: {gap:5.1f}s, {since_poke:.1f}s after poke)")

    def process_buffer(buf):
        while True:
            start = buf.find(b"\x59\x59")
            if start < 0:
                if len(buf) > 1: del buf[:-1]
                return
            if start > 0:
                del buf[:start]
            if len(buf) < 6:
                return
            length = (buf[4] << 8) | buf[5]
            total  = 6 + length
            if length <= 0 or length > 4096:
                del buf[:2]
                continue
            if len(buf) < total:
                return
            frame = bytes(buf[:total])
            del buf[:total]
            handle_frame(frame)

    def send_pokes(s):
        """Send the IR pokes (and HR handshake frames if --handshake)."""
        if HANDSHAKE_MODE:
            for frame in _HR_HANDSHAKE:
                s.sendall(frame)
        for frame in _IR_POKES:
            s.sendall(frame)
        mode = "IR+HR handshake" if HANDSHAKE_MODE else "IR only"
        log(f"POKES sent ({mode})", to_console=False)
        print("  > pokes sent", flush=True)

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
                if HANDSHAKE_MODE:
                    log("Handshake mode: sending initial HR reads to trigger poll-response mode...")
                    for frame in _HR_HANDSHAKE:
                        s.sendall(frame)
                    time.sleep(0.5)   # brief pause for the dongle to process
                while True:
                    now = time.time()
                    if now - st["last_poke"] >= POKE_INTERVAL:
                        try:
                            send_pokes(s)
                            st["last_poke"] = now
                        except Exception as exc:
                            log(f"Poke failed: {exc}")
                            break
                    try:
                        data = s.recv(8192)
                    except socket.timeout:
                        data = b""
                    if data:
                        binf.write(data); binf.flush()
                        st["bytes"] += len(data)
                        buf.extend(data)
                        process_buffer(buf)
                    if st["last_reply"] and time.time() - st["last_reply"] > 30 \
                            and not st["warned"]:
                        log(f"!! No reply for {time.time()-st['last_reply']:.0f}s "
                            f"— this is the disconnect symptom")
                        st["warned"] = True
                    if now - st["last_report"] > 30:
                        slaves = ", ".join(f"0x{x:02x}" for x in sorted(st["slaves"])) \
                                 or "none yet"
                        log(f"... summary: {st['replies']} replies, "
                            f"max gap {st['max_gap']:.0f}s, "
                            f"gaps>30s: {st['gaps_over_30s']}, slave(s): {slaves}")
                        st["last_report"] = now
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log(f"Connection error: {exc} — reconnecting in 5s")
                time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        slaves = ", ".join(f"0x{x:02x}" for x in sorted(st["slaves"])) or "none seen"
        log("-" * 64)
        log("RESULT SUMMARY")
        log(f"  Replies received:     {st['replies']}")
        log(f"  Slave address(es):    {slaves}")
        log(f"  Longest gap:          {st['max_gap']:.0f}s")
        log(f"  Gaps over 30s:        {st['gaps_over_30s']}")
        log(f"  Heartbeats / acked:   {st['heartbeats']} / {'yes' if ACK_HEARTBEATS else 'no (--no-ack)'}")
        log(f"  Handshake mode:       {'yes' if HANDSHAKE_MODE else 'no'}")
        log(f"  Total frames / bytes: {st['frames']} / {st['bytes']}")
        log(f"Please send back:  {bin_path.name}  and  {log_path.name}")
        binf.close()
        logf.close()


if __name__ == "__main__":
    main()
