r"""Scheduler apply test: writes to the detected slave (manual-control path), delta writes,
status reporting, and retry-on-failure. v2.0+ design: the scheduler WRITES slot 1
registers (charge HR 94/95, discharge HR 56/57) because the firmware requires an active
slot for forced grid charge/export — it therefore needs exclusive inverter control.
Slot 2 (HR 31/32) is never touched (not writable on the AC-coupled GIV-AC3.0).

Run: venv\Scripts\python.exe tests\test_scheduler_window.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard_server as ds  # noqa: E402

ds._SCHED_WRITE_GAP = 0.0
ds.time.sleep = lambda *_: None                       # no real delays in the test
ds._detect_inverter = lambda: (0x32, "single_phase_2slot", "Gen2")   # detected broadcast slave
ds.SCHEDULER_BASELINE = "eco"                         # deterministic baseline regardless of config.ini

written = []
def ok_write(slave, reg, val, timeout=5.0, attempts=7):
    written.append((slave, reg, val, attempts))
ds._hr_write = ok_write

def reset():
    written.clear()
    ds._sched_applied_sig = None
    ds._sched_applied_regs = {}

# 1) Charge writes to the DETECTED slave 0x32 (not 0x11); slot 1 (HR 94/95) is written
#    BEFORE ENABLE_CHARGE (firmware rejects the enable without an active slot);
#    slot 2 (HR 31/32) untouched; status set.
reset()
ds._sched_apply({"mode": "charge", "target_soc": 90,
                 "slot_start": "00:30", "slot_end": "04:30"})
print("charge writes:", [(hex(s), r, v, a) for s, r, v, a in written])
assert all(s == 0x32 for s, *_ in written), "must write to the detected slave 0x32"
regs_written = [(r, v) for _, r, v, _ in written]
assert (94, 30)  in regs_written, "charge slot 1 start = 00:30"
assert (95, 430) in regs_written, "charge slot 1 end = 04:30"
assert (96, 1)   in regs_written, "ENABLE_CHARGE = ON"
slot_idx   = next(i for i, (r, v) in enumerate(regs_written) if r == 94)
enable_idx = next(i for i, (r, v) in enumerate(regs_written) if r == 96)
assert slot_idx < enable_idx, "slot must be written before ENABLE_CHARGE"
assert all(r not in (31, 32) for r, _ in regs_written), "slot 2 (HR 31/32) untouched"
assert ds._sched_applied_sig is not None
assert ds._sched_last_status.startswith("Charge to 90%")

# 2) Delta — identical re-apply writes nothing
n = len(written)
ds._sched_apply({"mode": "charge", "target_soc": 90,
                 "slot_start": "00:30", "slot_end": "04:30"})
assert len(written) == n, "delta should skip unchanged registers"

# 3) Changing to baseline writes only the registers that differ.
#    Charge left: 94=30, 95=430, 59=0, 27=1, 20=1, 116=90, 111=50, 96=1.
#    Eco baseline wants: 94=0, 95=0, 56=0, 57=0, 96=0, 20=0, 27=1, 110=reserve, 59=1.
#    Delta = everything except 27 (already 1).
written.clear()
ds._sched_apply({"mode": "baseline"})
regs = {r for _, r, _, _ in written}
print("baseline delta wrote regs:", sorted(regs))
assert regs == {20, 56, 57, 59, 94, 95, 96, 110}, f"unexpected delta set: {sorted(regs)}"
assert 27 not in regs, "BATTERY_POWER_MODE unchanged (1) — delta must skip it"

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
ds._sched_apply({"mode": "charge", "target_soc": 80,
                 "slot_start": "01:00", "slot_end": "05:00"})
assert ds._sched_applied_sig is None, "partial/failed apply must stay un-applied (retry)"
assert ds._sched_last_status == "busy — will retry"
landed = len(ds._sched_applied_regs)
print("first attempt landed", landed, "regs before busy")
# retry: only the un-landed registers are re-sent
ds._hr_write = lambda s, r, v, timeout=5.0, attempts=7: ds._sched_applied_regs.__setitem__(r, v)
ds._sched_apply({"mode": "charge", "target_soc": 80,
                 "slot_start": "01:00", "slot_end": "05:00"})
assert ds._sched_applied_sig is not None, "retry completes the apply"
print("retry completed the apply")

print("\nAPPLY TEST PASSED — detected slave, slot-before-enable, delta, status, retry-completes")
