r"""Unit tests for predictive charge target computation.

Covers:
  _compute_morning_demand_kwh  -- queries snapshots DB, averages top-25% morning kWh
  _compute_predictive_target   -- combines forecast + demand into an SOC target
  _sched_desired_state         -- predictive=True rule substitutes the computed target

Run:  venv\Scripts\python.exe -m pytest tests\test_predictive.py
"""
import gc
import os
import sys
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard_server as ds


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ts(days_ago, hour=9, minute=0):
    """Unix timestamp for `days_ago` days before now at the given LOCAL hour.
    09:00 local reliably falls inside the 06:00-13:00 morning window."""
    base = datetime.now().replace(hour=hour, minute=minute,
                                  second=0, microsecond=0)
    return (base - timedelta(days=days_ago)).timestamp()


def _make_db(rows):
    """Temporary SQLite DB with a minimal snapshots table.
    rows: list of (ts, home_w) tuples."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("CREATE TABLE snapshots (ts REAL, home_w REAL)")
    conn.executemany("INSERT INTO snapshots (ts, home_w) VALUES (?,?)", rows)
    conn.commit()
    conn.close()
    return Path(f.name)


def _day_snapshots(days_ago, home_w, n=30, hour=9):
    """n snapshots starting at local `hour`, each 120 s apart.
    Produces n-1 integration intervals of 120 s each.
    kWh = home_w / 1000 * (120/3600) * (n-1)"""
    t0 = _local_ts(days_ago, hour=hour)
    return [(t0 + i * 120, home_w) for i in range(n)]


def _expected_kwh(home_w, n=30):
    """kWh produced by _day_snapshots at home_w watts and n snapshots."""
    return home_w / 1000.0 * (120.0 / 3600.0) * (n - 1)


def _set_pred(battery_kwh=10.0, pessimism=0.85, reserve=3.0, min_soc=20):
    saved = (ds.PRED_BATTERY_KWH, ds.PRED_PESSIMISM,
             ds.PRED_EVENING_RESERVE, ds.PRED_MIN_SOC)
    ds.PRED_BATTERY_KWH     = battery_kwh
    ds.PRED_PESSIMISM       = pessimism
    ds.PRED_EVENING_RESERVE = reserve
    ds.PRED_MIN_SOC         = min_soc
    return saved


def _restore_pred(saved):
    (ds.PRED_BATTERY_KWH, ds.PRED_PESSIMISM,
     ds.PRED_EVENING_RESERVE, ds.PRED_MIN_SOC) = saved


def _set_forecast(kwh):
    """Patch _sf_cached with a tomorrow forecast of `kwh` kWh."""
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    old = ds._sf_cached
    ds._sf_cached = {"watt_hours_day": [{"date": tomorrow, "value": kwh * 1000}]}
    return old


# ── _compute_morning_demand_kwh ───────────────────────────────────────────────

def test_morning_demand_no_data_returns_none():
    db = _make_db([])
    old = ds.DB_PATH
    try:
        ds.DB_PATH = db
        assert ds._compute_morning_demand_kwh() is None
    finally:
        ds.DB_PATH = old
        gc.collect(); db.unlink(missing_ok=True)


def test_morning_demand_fewer_than_3_days_returns_none():
    rows = _day_snapshots(1, 3000) + _day_snapshots(2, 3000)  # 2 days only
    db = _make_db(rows)
    old = ds.DB_PATH
    try:
        ds.DB_PATH = db
        assert ds._compute_morning_demand_kwh() is None
    finally:
        ds.DB_PATH = old
        gc.collect(); db.unlink(missing_ok=True)


def test_morning_demand_3_days_is_sufficient():
    rows = (_day_snapshots(1, 3000) + _day_snapshots(2, 3000)
            + _day_snapshots(3, 3000))
    db = _make_db(rows)
    old = ds.DB_PATH
    try:
        ds.DB_PATH = db
        assert ds._compute_morning_demand_kwh() is not None
    finally:
        ds.DB_PATH = old
        gc.collect(); db.unlink(missing_ok=True)


def test_morning_demand_top_25_percent():
    """With 8 days, top 25% = top 2; result must equal average of those 2 days."""
    # Powers give increasing daily demand: 3 kW ... 24 kW
    powers = [3000, 6000, 9000, 12000, 15000, 18000, 21000, 24000]
    rows   = []
    for i, pw in enumerate(powers, start=1):
        rows.extend(_day_snapshots(i, pw))
    db = _make_db(rows)
    old = ds.DB_PATH
    try:
        ds.DB_PATH = db
        result = ds._compute_morning_demand_kwh()
        # n = max(1, round(8 * 0.25)) = 2 -- top 2 are 24 kW and 21 kW days
        top2_avg = round((_expected_kwh(24000) + _expected_kwh(21000)) / 2, 2)
        assert result is not None
        assert abs(result - top2_avg) < 0.01
    finally:
        ds.DB_PATH = old
        gc.collect(); db.unlink(missing_ok=True)


def test_morning_demand_single_day_result_equals_that_day():
    """With exactly 3 days of identical demand, top 25% = 1 day = the common value."""
    kw = 4000
    rows = (_day_snapshots(1, kw) + _day_snapshots(2, kw) + _day_snapshots(3, kw))
    db = _make_db(rows)
    old = ds.DB_PATH
    try:
        ds.DB_PATH = db
        result = ds._compute_morning_demand_kwh()
        assert result is not None
        assert abs(result - round(_expected_kwh(kw), 2)) < 0.01
    finally:
        ds.DB_PATH = old
        gc.collect(); db.unlink(missing_ok=True)


def test_morning_demand_excludes_night_snapshots():
    """Snapshots at 03:00 local (outside 06:00-13:00) must not contribute to demand."""
    # Night snapshots at 50 kW -- would dominate if mistakenly counted
    night = [((_local_ts(1, hour=3) + i * 120), 50000) for i in range(30)]
    morning = (_day_snapshots(1, 3000) + _day_snapshots(2, 3000)
               + _day_snapshots(3, 3000))
    db = _make_db(night + morning)
    old = ds.DB_PATH
    try:
        ds.DB_PATH = db
        result = ds._compute_morning_demand_kwh()
        expected = round(_expected_kwh(3000), 2)
        assert result is not None
        # night rows at 50 kW would give ~48 kWh if counted; morning at 3 kW gives ~2.9
        assert abs(result - expected) < 0.1
    finally:
        ds.DB_PATH = old
        gc.collect(); db.unlink(missing_ok=True)


# ── _compute_predictive_target ────────────────────────────────────────────────

def test_predictive_target_no_battery_returns_none():
    saved = _set_pred(battery_kwh=0)
    try:
        assert ds._compute_predictive_target() is None
    finally:
        _restore_pred(saved)


def test_predictive_target_no_forecast_returns_none():
    old_cache = ds._sf_cached
    saved     = _set_pred(battery_kwh=10.0)
    ds._sf_cached = {}
    try:
        assert ds._compute_predictive_target() is None
    finally:
        ds._sf_cached = old_cache
        _restore_pred(saved)


def test_predictive_target_uses_learned_demand_not_reserve():
    """When history is available, learned demand is used; PRED_EVENING_RESERVE is ignored."""
    rows = sum([_day_snapshots(i, 3000) for i in range(1, 5)], [])
    db   = _make_db(rows)
    old_cache = _set_forecast(10.0)  # 10 kWh tomorrow
    # Set reserve to 99 kWh -- if used, formula gives target = 100% (clamped)
    saved    = _set_pred(battery_kwh=10.0, pessimism=0.85, reserve=99.0, min_soc=20)
    old_path = ds.DB_PATH
    ds.DB_PATH = db
    try:
        learned = ds._compute_morning_demand_kwh()
        assert learned is not None, "need >= 3 days of history for this test"
        result = ds._compute_predictive_target()
        # reserve=99 would give 100%; with learned demand (~2.9 kWh) target is well below
        assert result < 100, f"expected to use learned demand, not reserve=99; got {result}%"
        # verify exact value against manual calculation
        usable   = 10.0 * 0.85 - learned
        expected = int(max(20, min(100, round(100.0 * (1.0 - usable / 10.0)))))
        assert result == expected
    finally:
        ds._sf_cached = old_cache
        ds.DB_PATH    = old_path
        _restore_pred(saved)
        gc.collect(); db.unlink(missing_ok=True)


def test_predictive_target_falls_back_to_reserve_when_no_history():
    """Fewer than 3 days of history -> falls back to PRED_EVENING_RESERVE."""
    rows = _day_snapshots(1, 3000) + _day_snapshots(2, 3000)  # 2 days only
    db   = _make_db(rows)
    old_cache = _set_forecast(10.0)
    reserve   = 3.0
    saved    = _set_pred(battery_kwh=10.0, pessimism=0.85, reserve=reserve, min_soc=20)
    old_path = ds.DB_PATH
    ds.DB_PATH = db
    try:
        assert ds._compute_morning_demand_kwh() is None  # confirm no history
        usable   = 10.0 * 0.85 - reserve
        expected = int(max(20, min(100, round(100.0 * (1.0 - usable / 10.0)))))
        assert ds._compute_predictive_target() == expected
    finally:
        ds._sf_cached = old_cache
        ds.DB_PATH    = old_path
        _restore_pred(saved)
        gc.collect(); db.unlink(missing_ok=True)


def test_predictive_target_floors_at_min_soc():
    """Very high solar forecast -> formula yields low target, clamped at min_soc."""
    rows = sum([_day_snapshots(i, 3000) for i in range(1, 5)], [])
    db   = _make_db(rows)
    old_cache = _set_forecast(100.0)  # 100 kWh -- massively exceeds battery
    saved    = _set_pred(battery_kwh=10.0, pessimism=0.85, reserve=3.0, min_soc=20)
    old_path = ds.DB_PATH
    ds.DB_PATH = db
    try:
        assert ds._compute_predictive_target() == 20
    finally:
        ds._sf_cached = old_cache
        ds.DB_PATH    = old_path
        _restore_pred(saved)
        gc.collect(); db.unlink(missing_ok=True)


def test_predictive_target_caps_at_100():
    """Very low solar forecast -> formula yields > 100%, clamped at 100."""
    rows = _day_snapshots(1, 3000)  # 1 day -- below 3-day threshold, uses reserve
    db   = _make_db(rows)
    old_cache = _set_forecast(0.1)  # 0.1 kWh -- nearly nothing
    saved    = _set_pred(battery_kwh=10.0, pessimism=0.85, reserve=9.5, min_soc=20)
    old_path = ds.DB_PATH
    ds.DB_PATH = db
    try:
        assert ds._compute_predictive_target() == 100
    finally:
        ds._sf_cached = old_cache
        ds.DB_PATH    = old_path
        _restore_pred(saved)
        gc.collect(); db.unlink(missing_ok=True)


# ── _sched_desired_state (predictive=True integration) ───────────────────────

def test_sched_predictive_rule_uses_computed_target():
    """A rule with predictive=True must substitute _compute_predictive_target()
    for target_soc when history and forecast are available."""
    rows = sum([_day_snapshots(i, 3000) for i in range(1, 5)], [])
    db   = _make_db(rows)
    old_cache = _set_forecast(10.0)
    saved    = _set_pred(battery_kwh=10.0, pessimism=0.85, reserve=99.0, min_soc=20)
    old_path = ds.DB_PATH
    ds.DB_PATH = db
    try:
        rule = {"enabled": True, "action": "charge",
                "start": "00:30", "end": "04:30",
                "days_mask": 127, "target_soc": 100, "power_pct": 50,
                "predictive": True}
        d = ds._sched_desired_state([rule], 0, 120)  # MON, 02:00
        assert d["mode"] == "charge"
        expected = ds._compute_predictive_target()
        assert expected is not None
        assert d["target_soc"] == expected
        assert d["predictive"] is True
        # target_soc in rule was 100%, but computed target should be well below that
        assert d["target_soc"] < 100
    finally:
        ds._sf_cached = old_cache
        ds.DB_PATH    = old_path
        _restore_pred(saved)
        gc.collect(); db.unlink(missing_ok=True)


def test_sched_predictive_rule_falls_back_to_fixed_soc_when_no_forecast():
    """If forecast is unavailable, predictive=True rule uses the fixed target_soc."""
    old_cache = ds._sf_cached
    ds._sf_cached = {}
    saved = _set_pred(battery_kwh=10.0)
    try:
        rule = {"enabled": True, "action": "charge",
                "start": "00:30", "end": "04:30",
                "days_mask": 127, "target_soc": 80, "power_pct": 50,
                "predictive": True}
        d = ds._sched_desired_state([rule], 0, 120)
        assert d["mode"] == "charge"
        assert d["target_soc"] == 80   # falls back to rule's fixed soc
        assert d["predictive"] is False
    finally:
        ds._sf_cached = old_cache
        _restore_pred(saved)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nAll {len(fns)} predictive tests passed.")
