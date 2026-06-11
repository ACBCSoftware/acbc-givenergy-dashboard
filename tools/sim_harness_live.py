#!/usr/bin/env python3
"""Live-data decode regression against the simulator.

Sends the dashboard's own IR(0,60) poke to the sim, decodes the reply with the
dashboard's _decode_listen_frame / _build_from_input_page, and checks the
solar / home / battery / grid / SOC values decode into a sane range.

Does NOT touch config.ini. Requires the sim built on the Pi (see sim_harness.py).
"""
import os
import socket
import subprocess
import sys
import time

PI_SSH   = "givacbc23@192.168.68.54"
SIM_HOST = "192.168.68.54"
SIM_PORT = 5020
SIM_BIN  = "~/givenergy-simulator/target/release/sim-api"
SIM_SCEN = "~/givenergy-simulator/examples/basic_day.yaml"
DASH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, DASH_DIR)
import dashboard_server as ds          # noqa: E402
ds.INVERTER_IP   = SIM_HOST
ds.INVERTER_PORT = SIM_PORT


_SSH_KEY  = os.path.expanduser("~/.ssh/id_ed25519")
_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-i", _SSH_KEY]

def ssh(cmd, timeout=60, retries=2):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return subprocess.run(
                ["ssh"] + _SSH_OPTS + [PI_SSH, cmd],
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            if attempt < retries:
                print(f"  [retry {attempt+1}/{retries}] SSH timeout, retrying...")
                time.sleep(3)
    raise last_exc


def stop_sim():
    ssh("pkill -9 -f 'target/release/sim-api' 2>/dev/null; true")


def wait_port_free(timeout=10):
    """Block until port SIM_PORT is no longer bound on the Pi."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection((SIM_HOST, SIM_PORT), timeout=1).close()
            time.sleep(0.4)   # still bound -- keep waiting
        except OSError:
            return True       # connection refused / timed out -- port free
    return False


def start_sim(model):
    stop_sim()
    wait_port_free()
    ssh(". $HOME/.cargo/env 2>/dev/null; "
        f"nohup {SIM_BIN} run {SIM_SCEN} --inverter-type {model} "
        f"--tick-interval 3600 --modbus 0.0.0.0:{SIM_PORT} "
        ">~/sim.log 2>&1 </dev/null & echo $!")


def wait_ready(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection((SIM_HOST, SIM_PORT), timeout=2).close()
        except OSError:
            time.sleep(0.4)
            continue
        r = ssh("grep -c 'holding final state' ~/sim.log 2>/dev/null || true")
        if r.stdout.strip().isdigit() and int(r.stdout.strip()) > 0:
            return True
        time.sleep(0.4)
    return False


def poll_once(timeout=5.0):
    """Send IR(0,60) pokes and return the first decoded base=0 data dict."""
    s = socket.create_connection((SIM_HOST, SIM_PORT), timeout=timeout)
    s.settimeout(timeout)
    try:
        for poke in ds._POKE_REQUESTS:
            s.sendall(poke)
        buf = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf.extend(chunk)
            for f in ds._pop_data_frames(buf):
                d = ds._decode_listen_frame(f)
                if d:
                    return d
    finally:
        s.close()
    return None


CASES = ["AllInOne6", "Gen2Hybrid", "Gen3Hybrid"]
results = []


def check(label, cond, detail=""):
    results.append((label, cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def main():
    print(f"\nLive-data decode harness vs sim at {SIM_HOST}:{SIM_PORT}\n")
    for model in CASES:
        print(f"== {model} ==")
        start_sim(model)
        if not wait_ready():
            check(f"{model}: sim ready", False, "not ready")
            stop_sim()
            continue
        time.sleep(0.5)
        try:
            d = poll_once()
            if not d:
                check(f"{model}: live page decoded", False, "no decodable frame")
            else:
                vals = (f"solar={d.get('solar_w')}W home={d.get('home_w')}W "
                        f"batt={d.get('battery_w')}W grid={d.get('grid_w')}W "
                        f"soc={d.get('soc')}% vbat={d.get('v_battery')}V")
                check(f"{model}: live page decoded", d.get("ok") is True, vals)
                check(f"{model}: SOC in 0..100", 0 <= (d.get('soc') or -1) <= 100,
                      f"soc={d.get('soc')}")
                check(f"{model}: v_battery 30..70", 30 <= (d.get('v_battery') or 0) <= 70,
                      f"vbat={d.get('v_battery')}")
                for k in ("solar_w", "home_w", "battery_w", "grid_w"):
                    check(f"{model}: {k} >= 0 and present",
                          isinstance(d.get(k), (int, float)) and d.get(k) >= 0,
                          f"{k}={d.get(k)}")
        except Exception as exc:
            check(f"{model}: live decode", False, f"exception: {exc}")
        finally:
            stop_sim()
            wait_port_free()
        print()

    npass = sum(1 for _, c in results if c)
    nfail = sum(1 for _, c in results if not c)
    print("=" * 60)
    print(f"{npass} PASS, {nfail} FAIL (of {len(results)} checks)")
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        stop_sim()
