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


def _rule(action, start, end, days=EVERY_DAY, soc=None, power_pct=None, enabled=True):
    """Create a scheduler rule dict matching _sched_load_rules() output.
    start and end are 'HH:MM' strings (matching the start_hhmm/end_hhmm DB columns).
    t_min values passed to _sched_desired_state are integers (minutes from midnight)."""
    return {"enabled": enabled, "action": action,
            "start": start, "end": end,
            "days_mask": days, "target_soc": soc,
            "power_pct": power_pct or 50}


# ── _mins_to_hhmm ─────────────────────────────────────────────────────────────

def test_mins_to_hhmm_basic():
    assert ds._mins_to_hhmm(30)   == 30      # 00:30
    assert ds._mins_to_hhmm(270)  == 430     # 04:30
    assert ds._mins_to_hhmm(960)  == 1600    # 16:00
    assert ds._mins_to_hhmm(1140) == 1900    # 19:00
    assert ds._mins_to_hhmm(1380) == 2300    # 23:00
    assert ds._mins_to_hhmm(1439) == 2359    # 23:59

def test_mins_to_hhmm_midnight_clamped():
    # 0 = 00:00 = disabled on inverter firmware — must clamp to 1 (00:01)
    assert ds._mins_to_hhmm(0)    == 1       # clamped 00:00 → 00:01
    assert ds._mins_to_hhmm(1440) == 1       # mod 1440 = 0 → clamped 00:01

def test_mins_to_hhmm_non_zero_pass_through():
    assert ds._mins_to_hhmm(1)    == 1       # 00:01 unchanged
    assert ds._mins_to_hhmm(60)   == 100     # 01:00


# ── _block_contains ───────────────────────────────────────────────────────────
# start/end are "HH:MM" strings; t_min is integer minutes from midnight.

def test_block_contains_basic():
    assert ds._block_contains("00:30", "04:30", 60)  is True     # 01:00 inside
    assert ds._block_contains("00:30", "04:30", 30)  is True     # start inclusive
    assert ds._block_contains("00:30", "04:30", 270) is False    # 04:30 end exclusive
    assert ds._block_contains("00:30", "04:30", 0)   is False    # before start
    assert ds._block_contains("00:30", "04:30", 600) is False    # after end

def test_block_contains_zero_width():
    assert ds._block_contains("02:00", "02:00", 120) is False

def test_block_contains_wraps_midnight():
    # 23:00 → 01:00 window
    assert ds._block_contains("23:00", "01:00", 23 * 60) is True    # 23:00 in
    assert ds._block_contains("23:00", "01:00", 30)       is True    # 00:30 in
    assert ds._block_contains("23:00", "01:00", 60)       is False   # 01:00 exclusive
    assert ds._block_contains("23:00", "01:00", 12 * 60)  is False   # midday out


# ── _sched_desired_state ──────────────────────────────────────────────────────

def test_no_rules_is_baseline():
    assert ds._sched_desired_state([], MON, 120) == {"mode": "baseline"}

def test_single_charge_active():
    # 02:00 (t_min=120) is inside "00:30"-"04:30"
    d = ds._sched_desired_state([_rule("charge", "00:30", "04:30", soc=90)], MON, 120)
    assert d["mode"]       == "charge"
    assert d["target_soc"] == 90
    # Slot times are "HH:MM" strings, passed straight from the winning rule.
    assert d["slot_start"] == "00:30"
    assert d["slot_end"]   == "04:30"

def test_charge_passes_slot_times():
    d = ds._sched_desired_state([_rule("charge", "23:00", "00:30", soc=80)], MON, 23 * 60)
    assert d["mode"]       == "charge"
    assert d["slot_start"] == "23:00"
    assert d["slot_end"]   == "00:30"

def test_export_passes_slot_times():
    d = ds._sched_desired_state([_rule("export", "16:00", "19:00", soc=20)], MON, 960)
    assert d["mode"]       == "export"
    assert d["slot_start"] == "16:00"
    assert d["slot_end"]   == "19:00"

def test_day_mask_excludes():
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
    assert ds._sched_desired_state(rules, TUE, 30)["mode"] == "charge"


# ── _sched_compute_writes (hardware register mapping) ─────────────────────────
# slot_start / slot_end in the desired dict are "HH:MM" strings.

def _gen2():
    """Patch _detect_inverter to return GIV-AC3.0 (single_phase_2slot) profile."""
    ds._detect_inverter = lambda: (0x11, "single_phase_2slot", "GIV-AC3.0")


# Slot register addresses (GIV-AC3.0 uses slot 1 only — slot 2 at HR 31/32 times out)
_HR94, _HR95 = 94, 95    # charge slot 1 start/end
_HR56, _HR57 = 56, 57    # discharge slot 1 start/end


def test_compute_charge_writes_slot1():
    """Charge mode must write HR 94/95 (charge slot 1) before ENABLE_CHARGE."""
    _gen2()
    ds.SCHEDULER_SKIP_SLOT_WRITES = False
    # "00:30"-"04:30" → slot reg values: 30 (00:30) and 430 (04:30)
    _, w, summary = ds._sched_compute_writes(
        {"mode": "charge", "target_soc": 90, "power_pct": 50,
         "slot_start": "00:30", "slot_end": "04:30"})
    regs = {r: v for r, v in w}
    assert regs[_HR94] == 30    # charge slot 1 start = 00:30
    assert regs[_HR95] == 430   # charge slot 1 end   = 04:30
    assert regs[96]    == 1     # ENABLE_CHARGE = ON
    assert regs[116]   == 90    # CHARGE_TARGET_SOC
    assert regs[59]    == 0     # ENABLE_DISCHARGE = OFF
    # Slot must be written BEFORE ENABLE_CHARGE (firmware gate: no slot → EN_CH rejected)
    slot_idx   = next(i for i, (r, _) in enumerate(w) if r == _HR94)
    enable_idx = next(i for i, (r, _) in enumerate(w) if r == 96)
    assert slot_idx < enable_idx
    assert "Charge" in summary

def test_compute_charge_midnight_clamp():
    """Slot times of 00:00 must be clamped to 00:01 (0 = disabled on inverter)."""
    _gen2()
    _, w, _ = ds._sched_compute_writes(
        {"mode": "charge", "target_soc": 80,
         "slot_start": "00:00", "slot_end": "00:00"})
    regs = {r: v for r, v in w}
    assert regs[_HR94] == 1    # 00:00 → 00:01
    assert regs[_HR95] == 1    # 00:00 → 00:01

def test_compute_export_writes_discharge_slot1():
    """Export mode must write HR 56/57 (discharge slot 1) before ENABLE_DISCHARGE."""
    _gen2()
    _, w, summary = ds._sched_compute_writes(
        {"mode": "export", "stop_soc": 20, "power_pct": 50,
         "slot_start": "16:00", "slot_end": "19:00"})
    regs = {r: v for r, v in w}
    assert regs[_HR56] == 1600  # discharge slot 1 start = 16:00
    assert regs[_HR57] == 1900  # discharge slot 1 end   = 19:00
    assert regs[59]    == 1     # ENABLE_DISCHARGE = ON
    assert regs[27]    == 0     # BATTERY_POWER_MODE = export
    assert regs[96]    == 0     # ENABLE_CHARGE = OFF
    # Slot must be written BEFORE ENABLE_DISCHARGE
    slot_idx   = next(i for i, (r, _) in enumerate(w) if r == _HR56)
    enable_idx = next(i for i, (r, _) in enumerate(w) if r == 59)
    assert slot_idx < enable_idx
    assert "Export" in summary

def test_compute_hold_clears_slots():
    """Hold mode must clear both charge and discharge slot 1 register pairs."""
    _gen2()
    _, w, summary = ds._sched_compute_writes({"mode": "hold"})
    regs = {r: v for r, v in w}
    assert regs.get(_HR94, -1) == 0    # charge slot 1 cleared
    assert regs.get(_HR95, -1) == 0
    assert regs.get(_HR56, -1) == 0    # discharge slot 1 cleared
    assert regs.get(_HR57, -1) == 0
    assert regs[96] == 0               # ENABLE_CHARGE = OFF
    assert regs[59] == 0               # ENABLE_DISCHARGE = OFF
    assert "Hold" in summary

def test_compute_baseline_eco_clears_slots():
    """Eco baseline must clear both slot pairs and restore eco / discharge-on."""
    _gen2()
    ds.SCHEDULER_BASELINE = "eco"
    _, w, summary = ds._sched_compute_writes({"mode": "baseline"})
    regs = {r: v for r, v in w}
    assert regs.get(_HR94, -1) == 0    # charge slot 1 cleared
    assert regs.get(_HR95, -1) == 0
    assert regs.get(_HR56, -1) == 0    # discharge slot 1 cleared
    assert regs.get(_HR57, -1) == 0
    assert regs[96]  == 0              # ENABLE_CHARGE = OFF
    assert regs[59]  == 1              # ENABLE_DISCHARGE = ON  (eco)
    assert regs[27]  == 1              # BATTERY_POWER_MODE = demand/eco
    assert "Eco" in summary

def test_compute_baseline_storage_clears_slots():
    """Storage baseline must clear both slot pairs and leave both charge/discharge off."""
    _gen2()
    ds.SCHEDULER_BASELINE = "storage"
    _, w, summary = ds._sched_compute_writes({"mode": "baseline"})
    regs = {r: v for r, v in w}
    assert regs.get(_HR94, -1) == 0
    assert regs.get(_HR95, -1) == 0
    assert regs.get(_HR56, -1) == 0
    assert regs.get(_HR57, -1) == 0
    assert regs[96]  == 0              # ENABLE_CHARGE = OFF
    assert regs[59]  == 0              # ENABLE_DISCHARGE = OFF
    assert "Storage" in summary
    ds.SCHEDULER_BASELINE = "eco"      # restore

def test_compute_rejects_three_phase():
    ds._detect_inverter = lambda: (0x11, "three_phase_aio", "AIO")
    try:
        ds._sched_compute_writes({"mode": "baseline"})
        assert False, "should have raised RuntimeError"
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
