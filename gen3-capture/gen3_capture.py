#!/usr/bin/env python3
"""
GivEnergy inverter — connection diagnostic & capture tool
=========================================================
Connects to a GivEnergy inverter's local port (8899) and, like the dashboard,
sends a small "read" request ("poke") every few seconds and decodes the reply.
It measures how reliably the inverter answers, records any gaps, and detects the
inverter's Modbus slave address — exactly the data we need to diagnose the
"lost connection / no data for 75s" problem on some inverters.

It writes two files next to the program:
  • capture_<timestamp>.bin  — every raw byte received (source of truth)
  • capture_<timestamp>.log  — readable timeline: pokes, replies, gaps, your notes

You can TYPE A NOTE + Enter at any time to drop a timestamped marker in the log.

Stop with Ctrl+C or by closing the window.
"""
import socket
import sys
import time
import threading
import configparser
from pathlib import Path
from datetime import datetime

# Read-input-registers requests, exactly as the dashboard sends them.
# Same frame for both, differing only in the Modbus slave address byte:
#   0x32 (50) = Gen2 separate inverters
#   0x11 (17) = Gen3 / HV hybrid
# We send both; the inverter answers the one addressed to it.
POKES = [
    bytes.fromhex("59590001001c010241423132333447353637000000000000000832040000003cd1d5"),
    bytes.fromhex("59590001001c010241423132333447353637000000000000000811040000003cd1d5"),
]
POKE_INTERVAL = 10        # seconds between pokes (matches the dashboard default)

# The dongle emits a 1/Heartbeat frame (outer function 0x01) roughly every 3
# minutes. The correct response uses a dummy serial (AB1234G567) — NOT the
# dongle's real serial echoed back. Echoing the real serial causes the dongle
# to disrupt its data stream. This matches what givenergy-modbus does.
# Pass --no-ack to disable the response (for comparison testing).
ACK_HEARTBEATS = "--no-ack" not in sys.argv
_HB_RESPONSE_PREFIX = bytes.fromhex("59590001000d010141423132333447353637")

if getattr(sys, "frozen", False):
    HERE = Path(sys.executable).parent
else:
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


def hexdump(data, width=16):
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"      {i:04x}  {hexpart:<{width*3}}  {asc}")
    return "\n".join(out)


def decode_input_frame(frame):
    """If `frame` is a 'read input registers 0–59' reply, return key values;
    otherwise None. Also exposes the Modbus slave address the inverter used."""
    if len(frame) < 164 or frame[7] != 0x02:
        return None
    inner = frame[27]
    base  = (frame[38] << 8) | frame[39]
    count = (frame[40] << 8) | frame[41]
    if inner != 0x04 or base != 0 or count < 60:
        return None
    def g(n):
        o = 42 + n * 2
        return (frame[o] << 8) | frame[o + 1]
    return {
        "slave": frame[26],                 # 0x32 = Gen2, 0x11 = Gen3, others = tell us!
        "soc":   g(59),
        "solar": g(18) + g(20),
        "home":  g(42),
    }


def main():
    ip, port = load_config()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = HERE / f"capture_{stamp}.log"
    bin_path = HERE / f"capture_{stamp}.bin"
    logf = open(log_path, "w", encoding="utf-8")
    binf = open(bin_path, "wb")

    def log(msg, to_console=True):
        line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
        if to_console:
            print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    log("=== GivEnergy connection diagnostic ===")
    log(f"Target:    {ip}:{port}")
    log(f"Raw bytes: {bin_path.name}")
    log(f"Log file:  {log_path.name}")
    log("Sends a read request every 10s and times the replies. Ctrl+C to stop.")
    log('TIP: type a note + Enter to mark an event (e.g. "official app open now").')
    log("-" * 64)

    def note_reader():
        try:
            for raw in sys.stdin:
                txt = raw.strip()
                if txt:
                    log(f"NOTE >>> {txt}")
        except Exception:
            pass
    threading.Thread(target=note_reader, daemon=True).start()

    st = {
        "frames": 0, "bytes": 0, "replies": 0, "heartbeats": 0,
        "last_reply": 0.0, "last_poke": 0.0,
        "max_gap": 0.0, "gaps_over_30s": 0,
        "slaves": set(), "last_report": time.time(), "sock": None,
    }

    def handle_frame(frame):
        st["frames"] += 1
        # Full hex of every frame goes to the log file (not console) as the record
        func = frame[7] if len(frame) > 7 else 0
        logf.write(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
                   f"FRAME #{st['frames']} len={len(frame)} func={func:02x}\n")
        logf.write(hexdump(frame) + "\n"); logf.flush()

        # 1/Heartbeat from the dongle. Respond with dummy serial AB1234G567 —
        # NOT the real serial echoed back (that disrupts the data stream).
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
        log(f"REPLY  slave=0x{d['slave']:02x}  SOC={d['soc']}%  "
            f"solar={d['solar']}W  home={d['home']}W  "
            f"(gap since last reply: {gap:4.1f}s, "
            f"{since_poke:.1f}s after poke)")

    def process_buffer(buf):
        while True:
            start = buf.find(b"\x59\x59")
            if start < 0:
                if len(buf) > 1:
                    del buf[:-1]
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

    buf = bytearray()
    try:
        while True:
            try:
                log(f"Connecting to {ip}:{port} ...")
                s = socket.create_connection((ip, port), timeout=15)
                s.settimeout(2)
                st["sock"] = s
                log("Connected. Sending pokes and listening...")
                log(f"Heartbeat acknowledgement: {'ON' if ACK_HEARTBEATS else 'OFF (--no-ack)'}")
                while True:
                    now = time.time()
                    if now - st["last_poke"] >= POKE_INTERVAL:
                        try:
                            for poke in POKES:
                                s.sendall(poke)
                            st["last_poke"] = now
                            log("POKE sent (slave 0x32 + 0x11)", to_console=False)
                            print("  > poke sent", flush=True)
                        except Exception as exc:
                            log(f"Poke send failed: {exc}")
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
                    # Warn (once) when a long gap is building
                    if st["last_reply"] and now - st["last_reply"] > 30 \
                            and not st.get("warned"):
                        log(f"!! No reply for {now - st['last_reply']:.0f}s — "
                            f"this is the disconnect symptom")
                        st["warned"] = True
                    if data and st.get("warned") and st["last_reply"] == now:
                        st["warned"] = False
                    # Running summary every ~30s
                    if now - st["last_report"] > 30:
                        slaves = ", ".join(f"0x{x:02x}" for x in sorted(st["slaves"])) or "none yet"
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
        log(f"  Total frames / bytes: {st['frames']} / {st['bytes']}")
        log(f"Please send back:  {bin_path.name}  and  {log_path.name}")
        binf.close()
        logf.close()


if __name__ == "__main__":
    main()
