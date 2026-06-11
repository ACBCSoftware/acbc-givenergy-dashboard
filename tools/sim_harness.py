#!/usr/bin/env python3
"""GivEnergy simulator regression harness for the ACBC dashboard.

Drives the dashboard's real inverter-detection path against psylsph's
givenergy-simulator (built from source, MIT) running on the Pi, cycling the
inverter model so we exercise _classify_model / _detect_inverter end to end over
the LAN, including the proprietary 59 59 framing and CRC.

Does NOT touch config.ini: it overrides INVERTER_IP / INVERTER_PORT on the
imported dashboard_server module, so the real inverter config is never altered.

The simulator must already be built on the Pi at
  ~/givenergy-simulator/target/release/sim-api
(see PROMPT-simulator-harness.md). This script starts and stops it per model.

Usage:
  venv\\Scripts\\python.exe tools\\sim_harness.py
"""
import os
import socket
import subprocess
import sys
import time

# --- config -----------------------------------------------------------------
PI_SSH   = "givacbc23@192.168.68.54"
SIM_HOST = "192.168.68.54"
SIM_PORT = 5020
SIM_BIN  = "~/givenergy-simulator/target/release/sim-api"
SIM_SCEN = "~/givenergy-simulator/examples/basic_day.yaml"
SETTLE_S = 0.5          # brief pause after wait_ready() for final socket settle
DASH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Import the dashboard and point it at the sim (no config.ini change).
sys.path.insert(0, DASH_DIR)
import dashboard_server as ds          # noqa: E402
ds.INVERTER_IP   = SIM_HOST
ds.INVERTER_PORT = SIM_PORT

# (sim inverter_type, expected_profile, expected_model_substring, note)
MATRIX = [
    ("AllInOne6",     "single_phase_extended",   "All in One",               "0x8001  <-- v2.4 release-gate headline"),
    ("AllInOne",      "single_phase_extended",   "All in One",               "0x8002"),
    ("AllInOne5",     "single_phase_extended",   "All in One",               "0x8003"),
    ("AIO8kW",        "single_phase_extended",   "Hybrid Inverter Gen 3 HV", "0x8102 (we name 0x81xx HV Gen3)"),
    ("AIOHybrid6kW",  "single_phase_extended",   "All in One 2",             "0x8201  newly modelled upstream"),
    ("Gen4Hybrid6kW", "single_phase_extended",   "Unknown (0x83)",           "0x8304"),
    ("Gen1Hybrid",    "single_phase_2slot",      "Hybrid Inverter Gen 1",    "0x2001 ARM fw century 2"),
    ("Gen2Hybrid",    "single_phase_2slot",      "Hybrid Inverter Gen 2",    "0x2001 ARM fw century 8"),
    ("Gen3Hybrid",    "single_phase_extended",   "Hybrid Inverter Gen 3",    "0x2001 ARM fw century 3 (>302)"),
    ("ACCoupled",     "single_phase_ac_coupled", "AC Coupled Inverter",      "0x3001"),
    ("EMS",           "single_phase_2slot",      "Energy Management",        "0x5001"),
    ("ThreePhase",    "three_phase_aio",         "Three-Phase Hybrid",       "0x4001"),
    ("ACThreePhase",  "three_phase_aio",         "Three-Phase AC",           "0x6001"),
    ("AIOCommercial", "three_phase_aio",         "AIO Commercial",           "0x4101"),
    ("Gateway12kW",   "gateway_aio",             "Gateway",                  "0x7001"),
]


_SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-i", _SSH_KEY]

def ssh(cmd, timeout=60, retries=2):
    """Run cmd on PI_SSH. Retries on TimeoutExpired (transient SSH throttle)."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return subprocess.run(
                ["ssh"] + _SSH_OPTS + [PI_SSH, cmd],
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            if attempt < retries:
                print(f"  [retry {attempt+1}/{retries}] SSH timeout for: {cmd[:60]}...")
                time.sleep(3)
    raise last_exc


def stop_sim():
    ssh("pkill -9 -f 'target/release/sim-api' 2>/dev/null; true")


def wait_port_free(timeout=12):
    """Block until port SIM_PORT is no longer bound on the Pi (sim is dead)."""
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
    wait_port_free()   # wait until the old sim fully releases the port
    cmd = (". $HOME/.cargo/env 2>/dev/null; "
           f"nohup {SIM_BIN} run {SIM_SCEN} --inverter-type {model} "
           f"--tick-interval 3600 --modbus 0.0.0.0:{SIM_PORT} "
           ">~/sim.log 2>&1 </dev/null & echo $!")
    ssh(cmd)


def wait_port(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection((SIM_HOST, SIM_PORT), timeout=2).close()
            return True
        except OSError:
            time.sleep(0.4)
    return False


def wait_ready(timeout=20):
    """Block until the sim has loaded the scenario and entered keep-alive state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = ssh("grep -c 'holding final state' ~/sim.log 2>/dev/null || true")
        if r.stdout.strip().isdigit() and int(r.stdout.strip()) > 0:
            return True
        time.sleep(0.4)
    return False


def reset_cache():
    ds._inverter_profile = ""
    ds._inverter_model = ""
    ds._inverter_slave = 0


def run_one(model):
    """Return dict with raw DTC/fw and detected profile/name, or an error."""
    for attempt in range(3):
        try:
            reset_cache()
            regs = ds._hr_read(0x11, 0, 22, timeout=5.0)
            dtc, fw = regs[0], regs[21]
            reset_cache()
            _slave, profile, name = ds._detect_inverter()
            return {"dtc": dtc, "fw": fw, "profile": profile, "name": name}
        except Exception as exc:
            if attempt < 2:
                print(f"  [run_one retry {attempt + 1}] {exc}")
                time.sleep(1.5)
            else:
                raise


def main():
    print(f"\nHarness target: sim at {SIM_HOST}:{SIM_PORT} (dashboard import from {DASH_DIR})")
    print("Cycling models; the sim is restarted per model.\n")
    rows = []
    for model, exp_profile, exp_name_sub, note in MATRIX:
        row = None
        for attempt in range(3):
            start_sim(model)
            if not wait_port():
                if attempt == 0:
                    print(f"  [model retry] {model}: no listener, restarting sim...")
                    stop_sim()
                    continue
                row = (model, "-", "-", "NO-LISTENER", "", exp_profile, "FAIL", note)
                stop_sim()
                break
            if not wait_ready():
                # Some sim models don't emit 'holding final state'; longer fallback.
                time.sleep(2.0)
            else:
                time.sleep(SETTLE_S)
            try:
                r = run_one(model)
                ok_p = (r["profile"] == exp_profile)
                ok_m = exp_name_sub.lower() in r["name"].lower()
                verdict = "PASS" if (ok_p and ok_m) else ("PROFILE-OK/NAME-DIFF" if ok_p else "FAIL")
                row = (model, f"0x{r['dtc']:04x}", r["fw"], r["profile"],
                       r["name"], exp_profile, verdict, note)
                stop_sim()
                time.sleep(0.3)
                break
            except Exception as exc:
                stop_sim()
                time.sleep(0.3)
                if attempt == 0:
                    print(f"  [model retry] {model}: {exc}, restarting sim...")
                else:
                    row = (model, "-", "-", "ERROR", str(exc)[:48],
                           exp_profile, "FAIL", note)
        if row:
            rows.append(row)

    # --- report ---
    print(f"{'sim model':14} {'DTC':7} {'fw':5} {'detected profile':24} "
          f"{'detected name':26} {'verdict':20} note")
    print("-" * 140)
    npass = npart = nfail = 0
    for (model, dtc, fw, profile, name, exp_profile, verdict, note) in rows:
        if verdict == "PASS":
            npass += 1
        elif verdict.startswith("PROFILE-OK"):
            npart += 1
        else:
            nfail += 1
        print(f"{model:14} {dtc:7} {str(fw):5} {profile:24} {str(name):26} "
              f"{verdict:20} {note}")
    print("-" * 140)
    print(f"\n{npass} PASS, {npart} profile-ok/name-diff, {nfail} FAIL "
          f"(of {len(rows)} models)\n")
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        stop_sim()
