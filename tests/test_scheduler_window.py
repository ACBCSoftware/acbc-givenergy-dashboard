r"""Exclusive-write-window coordination test.

Models the real Gen2 failure mode the Rust simulator does NOT: a single-client dongle
that returns 'busy' for a write UNLESS the listen loop has stepped aside. Proves the
scheduler's exclusive window (_listen_yield_req / _listen_yielded handshake) acquires
sole access so writes land, that delta-writes skip unchanged registers, and that the
handshake always releases (no deadlock).

Run: venv\Scripts\python.exe tests\test_scheduler_window.py
"""
import os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard_server as ds  # noqa: E402

ds._SCHED_WRITE_GAP = 0.0                       # no inter-write pacing delay in the test
ds._detect_inverter = lambda: (0x32, "single_phase_2slot", "Gen2")

# ── Fake single-client dongle: a write only succeeds while the scheduler holds the
#    exclusive window (yield_req set by the apply thread — race-free in the test) ──
written = []
def fake_dongle_write(slave, reg, val, timeout=5.0, attempts=7):
    if not ds._listen_yield_req.is_set():
        raise OSError("dongle busy (no exclusive window)")    # the real failure mode
    written.append((slave, reg, val))
ds._hr_write = fake_dongle_write

# ── Fake listen loop: honours the yield handshake (set yielded when asked, clear on release) ──
_stop = threading.Event()
def fake_listen():
    while not _stop.is_set():
        if ds._listen_yield_req.is_set():
            ds._listen_yielded.set()            # "closed my socket, dongle is yours"
            while ds._listen_yield_req.is_set() and not _stop.is_set():
                time.sleep(0.02)
            ds._listen_yielded.clear()          # released → "reconnecting"
        time.sleep(0.02)
threading.Thread(target=fake_listen, daemon=True).start()

def reset():
    written.clear()
    ds._sched_applied_sig = None
    ds._sched_applied_regs = {}

# 1) Charge apply lands because the window grabs exclusive access
reset()
ds._sched_apply({"mode": "charge", "target_soc": 90, "start": "00:30", "end": "04:30"})
print("charge writes:", [(hex(s), r, v) for s, r, v in written])
assert (0x11, 31, 30) in written and (0x11, 32, 430) in written and (0x11, 96, 1) in written
assert all(s == 0x11 for s, _, _ in written), "writes must target 0x11 in the window"
assert all(r not in (94, 95, 56, 57) for _, r, _ in written), "slot 1 untouched"
assert ds._sched_applied_sig is not None, "apply succeeded → applied_sig set"
assert not ds._listen_yield_req.is_set(), "window released (no deadlock)"

# 2) Delta — re-applying the SAME state writes nothing
n_before = len(written)
ds._sched_apply({"mode": "charge", "target_soc": 90, "start": "00:30", "end": "04:30"})
print("writes after identical re-apply:", len(written) - n_before)
assert len(written) == n_before, "delta should skip unchanged registers"

# 3) Changing state writes only the registers that DIFFER from what we last wrote.
#    After charge, applied_regs = {116:90, 31:30, 32:430, 96:1, 44:0, 45:0}. Baseline
#    wants 31,32,44,45 = 0, 96 = 0, 59 = 1, 27 = 1. So 44/45 are already 0 (skip), and
#    31,32,96,59,27 must be written.
written.clear()
ds._sched_apply({"mode": "baseline"})
regs = {r for _, r, _ in written}
print("baseline delta wrote regs:", sorted(regs))
assert regs == {27, 31, 32, 59, 96}, f"unexpected delta set: {sorted(regs)}"
assert 44 not in regs and 45 not in regs, "regs already at 0 must be skipped by delta"

# 4) Window still releases even when a write fails (no deadlock)
reset()
def always_busy(slave, reg, val, timeout=5.0, attempts=7):
    raise OSError("busy")
ds._hr_write = always_busy
ds._sched_apply({"mode": "hold"})
assert ds._sched_applied_sig is None, "failed apply leaves it un-applied (retry)"
assert not ds._listen_yield_req.is_set(), "window released even on failure (no deadlock)"
print("failure path released window cleanly")

_stop.set()
print("\nEXCLUSIVE-WINDOW TEST PASSED — sole access, delta writes, clean release")
