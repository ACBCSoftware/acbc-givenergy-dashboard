r"""Scheduler apply test: writes to the detected slave (manual-control path), delta writes,
status reporting, and retry-on-failure. No exclusive window — the live Pi proved writes
succeed to the detected slave (0x32) with the listen loop OPEN, so the scheduler just does
what the manual controls do.

Run: venv\Scripts\python.exe tests\test_scheduler_window.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard_server as ds  # noqa: E402

ds._SCHED_WRITE_GAP = 0.0
ds.time.sleep = lambda *_: None                       # no real delays in the test
ds._detect_inverter = lambda: (0x32, "single_phase_2slot", "Gen2")   # detected broadcast slave

written = []
def ok_write(slave, reg, val, timeout=5.0, attempts=7):
    written.append((slave, reg, val, attempts))
ds._hr_write = ok_write

def reset():
    written.clear()
    ds._sched_applied_sig = None
    ds._sched_applied_regs = {}

# 1) Charge writes to the DETECTED slave 0x32 (not 0x11), slot 1 untouched, status set
reset()
ds._sched_apply({"mode": "charge", "target_soc": 90, "start": "00:30", "end": "04:30"})
print("charge writes:", [(hex(s), r, v, a) for s, r, v, a in written])
assert all(s == 0x32 for s, *_ in written), "must write to the detected slave 0x32"
assert (0x32, 31, 30, 7) in written and (0x32, 32, 430, 7) in written and (0x32, 96, 1, 7) in written
assert all(r not in (94, 95, 56, 57) for _, r, _, _ in written), "slot 1 untouched"
assert ds._sched_applied_sig is not None
assert ds._sched_last_status.startswith("Charge to 90%")

# 2) Delta — identical re-apply writes nothing
n = len(written)
ds._sched_apply({"mode": "charge", "target_soc": 90, "start": "00:30", "end": "04:30"})
assert len(written) == n, "delta should skip unchanged registers"

# 3) Changing to baseline writes only the registers that differ
written.clear()
ds._sched_apply({"mode": "baseline"})
regs = {r for _, r, _, _ in written}
print("baseline delta wrote regs:", sorted(regs))
assert regs == {27, 31, 32, 59, 96}, f"unexpected delta set: {sorted(regs)}"

# 4) A failed write leaves it un-applied (retry) and records busy status; delta means the
#    retry only re-sends what hasn't landed yet.
reset()
calls = {"n": 0}
def busy_after_two(slave, reg, val, timeout=5.0, attempts=7):
    calls["n"] += 1
    if calls["n"] >= 3:
        raise OSError("dongle busy")
    ds._sched_applied_regs[reg] = val                 # first two land
ds._hr_write = busy_after_two
ds._sched_apply({"mode": "charge", "target_soc": 80, "start": "01:00", "end": "05:00"})
assert ds._sched_applied_sig is None, "partial/failed apply must stay un-applied (retry)"
assert ds._sched_last_status == "busy — will retry"
landed = len(ds._sched_applied_regs)
print("first attempt landed", landed, "regs before busy")
# retry: only the un-landed registers are re-sent
ds._hr_write = lambda s, r, v, timeout=5.0, attempts=7: ds._sched_applied_regs.__setitem__(r, v)
ds._sched_apply({"mode": "charge", "target_soc": 80, "start": "01:00", "end": "05:00"})
assert ds._sched_applied_sig is not None, "retry completes the apply"
print("retry completed the apply")

print("\nAPPLY TEST PASSED — detected slave, delta, status, retry-completes")
