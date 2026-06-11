#!/usr/bin/env python3
"""Control-state + slot write/read-back regression against the simulator.

Complements sim_harness.py (detection). For representative profiles it:
  1. calls the dashboard's _read_control_state() and checks it succeeds with the
     right profile, slot counts and register ranges (no crash, no wrong block);
  2. writes charge slot 1 (HR 94/95) and reads it back through the dashboard;
  3. for the All in One (single_phase_extended) also round-trips charge slot 2 at
     HR 243/244 -- the exact v2.4 _charge_slot_hrs(profile) extended path.

Does NOT touch config.ini (overrides INVERTER_IP/PORT on the module).
Requires the sim built on the Pi (see sim_harness.py).
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
SETTLE_S = 1.2
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


def wait_port_free(timeout=12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection((SIM_HOST, SIM_PORT), timeout=1).close()
            time.sleep(0.4)
        except OSError:
            return True
    return False


def start_sim(model):
    stop_sim()
    wait_port_free()
    # Coarse tick-interval: the day simulates in a handful of ticks (instead of
    # 86400 at 1s), so the run reaches the keep-alive (no-projection) state
    # almost immediately. Otherwise the still-running tick loop re-projects the
    # default schedule every tick and clobbers our slot writes.
    ssh(". $HOME/.cargo/env 2>/dev/null; "
        f"nohup {SIM_BIN} run {SIM_SCEN} --inverter-type {model} "
        f"--tick-interval 3600 --modbus 0.0.0.0:{SIM_PORT} "
        ">~/sim.log 2>&1 </dev/null & echo $!")


def wait_port(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection((SIM_HOST, SIM_PORT), timeout=2).close()
            return True
        except OSError:
            time.sleep(0.4)
    return False


def wait_ready(timeout=15):
    """Block until the sim has finished the scenario and entered keep-alive
    (stops re-projecting), so register writes will persist."""
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


# (model, expected_profile, n_charge, n_discharge, writable, test_slot2_ext)
CASES = [
    ("AllInOne6",  "single_phase_extended",   10, 10, True,  True),
    ("Gen2Hybrid", "single_phase_2slot",        2,  2, True,  False),
    ("ACCoupled",  "single_phase_ac_coupled",   1,  2, True,  False),
    ("ThreePhase", "three_phase_aio",           2,  2, False, False),
]

results = []


def check(label, cond, detail=""):
    results.append((label, "PASS" if cond else "FAIL", detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def run_model(model, exp_profile, n_c, n_d, writable, test_slot2):
    reset_cache()
    st = ds._read_control_state()
    check(f"{model}: read ok", st.get("ok") is True, st.get("error", ""))
    check(f"{model}: profile", st.get("profile") == exp_profile,
          f"got {st.get('profile')}")
    check(f"{model}: charge slot count", len(st.get("charge_slots", [])) == n_c,
          f"got {len(st.get('charge_slots', []))} expected {n_c}")
    check(f"{model}: discharge slot count",
          len(st.get("discharge_slots", [])) == n_d,
          f"got {len(st.get('discharge_slots', []))} expected {n_d}")
    check(f"{model}: writable flag", st.get("writable") == writable,
          f"got {st.get('writable')}")

    if writable:
        try:
            ds._hr_write(0x11, 94, 430, attempts=1)
            ds._hr_write(0x11, 95, 600, attempts=1)
            reset_cache()
            st2 = ds._read_control_state()
            cs0 = st2["charge_slots"][0]
            check(f"{model}: slot1 HR94/95 round-trip",
                  cs0["start"] == "04:30" and cs0["end"] == "06:00",
                  f"got {cs0['start']}-{cs0['end']}")
        except Exception as exc:
            check(f"{model}: slot1 HR94/95 round-trip", False, f"write/read err: {exc}")

    if test_slot2:
        try:
            ds._hr_write(0x11, 243, 900,  attempts=1)
            ds._hr_write(0x11, 244, 1030, attempts=1)
            reset_cache()
            st3 = ds._read_control_state()
            cs1 = st3["charge_slots"][1]
            check(f"{model}: slot2 HR243/244 (v2.4 extended) round-trip",
                  cs1["start"] == "09:00" and cs1["end"] == "10:30",
                  f"got {cs1['start']}-{cs1['end']}")
        except Exception as exc:
            check(f"{model}: slot2 HR243/244 round-trip", False, f"write/read err: {exc}")


def main():
    print(f"\nControl-state harness vs sim at {SIM_HOST}:{SIM_PORT}\n")
    for model, exp_profile, n_c, n_d, writable, test_slot2 in CASES:
        print(f"== {model}  (expect {exp_profile}) ==")
        for attempt in range(3):
            start_sim(model)
            if not wait_port():
                if attempt == 0:
                    print(f"  [model retry] {model}: no listener, restarting sim...")
                    stop_sim()
                    continue
                check(f"{model}: sim listening", False, "no listener after retry")
                stop_sim()
                break
            if not wait_ready():
                time.sleep(2.0)
            else:
                time.sleep(SETTLE_S)
            # Confirm the sim is still up before recording any checks.
            try:
                socket.create_connection((SIM_HOST, SIM_PORT), timeout=3).close()
            except OSError as exc:
                stop_sim()
                if attempt == 0:
                    print(f"  [model retry] {model}: port gone after init ({exc}), restarting...")
                    continue
                check(f"{model}: sim alive after init", False, str(exc))
                break
            try:
                run_model(model, exp_profile, n_c, n_d, writable, test_slot2)
                stop_sim()
                time.sleep(0.3)
                break
            except Exception as exc:
                stop_sim()
                time.sleep(0.3)
                if attempt == 0:
                    print(f"  [model retry] {model}: {exc}, restarting sim...")
                else:
                    check(f"{model}: control-state read", False, f"exception: {exc}")
        print()

    npass = sum(1 for _, v, _ in results if v == "PASS")
    nfail = sum(1 for _, v, _ in results if v == "FAIL")
    print("=" * 70)
    print(f"{npass} PASS, {nfail} FAIL (of {len(results)} checks)")
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        stop_sim()
