r"""Unit tests for the scheduler's pure decision logic (no inverter/network).

Run:  venv\Scripts\python.exe tests\test_scheduler.py
   or: venv\Scripts\python.exe -m pytest tests\test_scheduler.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard_server as ds  # noqa: E402

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
EVERY_DAY = 127
WEEKDAYS  = 31          # Mon-Fri
WEEKENDS  = 96          # Sat+Sun


def _rule(action, start, end, days=EVERY_DAY, soc=None, enabled=True):
    return {"enabled": enabled, "action": action, "start": start, "end": end,
            "days_mask": days, "target_soc": soc}


# ── _block_contains ───────────────────────────────────────────────────────────

def test_block_contains_basic():
    assert ds._block_contains("00:30", "04:30", 60) is True      # 01:00 inside
    assert ds._block_contains("00:30", "04:30", 30) is True      # start inclusive
    assert ds._block_contains("00:30", "04:30", 270) is False    # 04:30 end exclusive
    assert ds._block_contains("00:30", "04:30", 0)  is False     # before
    assert ds._block_contains("00:30", "04:30", 600) is False    # after

def test_block_contains_zero_width():
    assert ds._block_contains("02:00", "02:00", 120) is False

def test_block_contains_wraps_midnight():
    # 23:00 → 01:00 window
    assert ds._block_contains("23:00", "01:00", 23 * 60) is True   # 23:00
    assert ds._block_contains("23:00", "01:00", 30)      is True   # 00:30
    assert ds._block_contains("23:00", "01:00", 60)      is False  # 01:00 exclusive
    assert ds._block_contains("23:00", "01:00", 12 * 60) is False  # midday


# ── _sched_desired_state ──────────────────────────────────────────────────────

def test_no_rules_is_baseline():
    assert ds._sched_desired_state([], MON, 120) == {"mode": "baseline"}

def test_single_charge_active():
    d = ds._sched_desired_state([_rule("charge", "00:30", "04:30", soc=90)], MON, 120)
    assert d == {"mode": "charge", "target_soc": 90, "start": "00:30", "end": "04:30"}

def test_day_mask_excludes():
    # charge only on weekdays, but it's Saturday
    rules = [_rule("charge", "00:30", "04:30", days=WEEKDAYS, soc=90)]
    assert ds._sched_desired_state(rules, SAT, 120) == {"mode": "baseline"}
    assert ds._sched_desired_state(rules, FRI, 120)["mode"] == "charge"

def test_outside_window_is_baseline():
    rules = [_rule("charge", "00:30", "04:30", soc=90)]
    assert ds._sched_desired_state(rules, MON, 600) == {"mode": "baseline"}

def test_disabled_rule_ignored():
    rules = [_rule("charge", "00:30", "04:30", soc=90, enabled=False)]
    assert ds._sched_desired_state(rules, MON, 120) == {"mode": "baseline"}

def test_hold_action():
    d = ds._sched_desired_state([_rule("hold", "16:00", "19:00")], MON, 17 * 60)
    assert d == {"mode": "hold"}

def test_precedence_charge_beats_hold():
    rules = [_rule("hold", "00:00", "06:00"), _rule("charge", "00:30", "04:30", soc=80)]
    assert ds._sched_desired_state(rules, MON, 120)["mode"] == "charge"

def test_precedence_export_beats_charge():
    rules = [_rule("charge", "00:00", "06:00", soc=80), _rule("export", "00:30", "04:30")]
    assert ds._sched_desired_state(rules, MON, 120)["mode"] == "export"

def test_wrap_midnight_charge_picked():
    rules = [_rule("charge", "23:00", "01:00", soc=100)]
    # 00:30 — next calendar day, but window wraps
    assert ds._sched_desired_state(rules, TUE, 30)["mode"] == "charge"


# ── _sched_compute_writes (hardware register mapping) ─────────────────────────
def _gen2():
    ds._detect_inverter = lambda: (0x32, "single_phase_2slot", "Gen2")  # reserved slot 2

def test_compute_charge_uses_slot2_never_slot1():
    _gen2()
    _, w, _ = ds._sched_compute_writes({"mode": "charge", "target_soc": 90,
                                        "start": "00:30", "end": "04:30"})
    assert (116, 90) in w and (31, 30) in w and (32, 430) in w and (96, 1) in w
    assert all(reg not in (94, 95, 56, 57) for reg, _ in w)   # slot 1 untouched

def test_compute_export():
    _gen2()
    _, w, _ = ds._sched_compute_writes({"mode": "export", "start": "16:00", "end": "19:00"})
    assert (44, 1600) in w and (45, 1900) in w and (27, 0) in w and (59, 1) in w \
        and (31, 0) in w and (32, 0) in w

def test_compute_cleanup():
    _gen2()
    _, w, _ = ds._sched_compute_writes({"mode": "cleanup"})
    assert w == [(31, 0), (32, 0), (44, 0), (45, 0), (96, 0)]

def test_compute_baseline_eco():
    _gen2(); ds.SCHEDULER_BASELINE = "eco"
    _, w, _ = ds._sched_compute_writes({"mode": "baseline"})
    assert (96, 0) in w and (59, 1) in w and (27, 1) in w

def test_compute_rejects_three_phase():
    ds._detect_inverter = lambda: (0x11, "three_phase_aio", "AIO")
    try:
        ds._sched_compute_writes({"mode": "baseline"})
        assert False, "should have raised"
    except RuntimeError:
        pass


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nAll {len(fns)} scheduler tests passed.")


if __name__ == "__main__":
    _run()
