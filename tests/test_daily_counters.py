"""Daily energy counters must not go backwards mid-day.

Found on a GIV-AC3.0 (29 Oct 2026): the inverter occasionally returns a lone 0
for grid_in_today (16 of 120 days). The cost guard in _handle_reading took each
one as a counter reset and recomputed the day from zero, wiping all off-peak
import before the glitch - 29 Oct showed 22p of import for 9.0 kWh, and four
stored days held 1-47p for 1.3-9.4 kWh. The bad read also landed in history.

The live sequence is replayed here: 8.1 -> 0 -> 8.1 at 16:32.
"""
import importlib
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def ds():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="counters-test-"))
    import dashboard_server as mod
    importlib.reload(mod)
    mod.init_db()
    return mod


@pytest.fixture(autouse=True)
def fresh_state(ds):
    ds._counter_state.update(date=None, best={}, drops={})
    ds._zero_reads.update({"in": 0, "out": 0})
    yield


AFTERNOON = datetime(2026, 10, 29, 16, 32, 0)


def _hold(ds, value, when, field="grid_in_today"):
    out = {field: value}
    ds._hold_daily_counters(out, now=when)
    return out[field]


# ── The counter guard itself ──────────────────────────────────────────────────

def test_lone_zero_mid_day_is_held(ds, caplog):
    with caplog.at_level(logging.WARNING, logger="dashboard"):
        seen = [_hold(ds, v, AFTERNOON) for v in (8.1, 0.0, 8.1)]
    assert seen == [8.1, 8.1, 8.1]
    assert "ignoring as a bad read" in caplog.text


def test_rising_values_pass_straight_through(ds):
    assert [_hold(ds, v, AFTERNOON) for v in (1.0, 1.2, 1.2, 1.5)] == [1.0, 1.2, 1.2, 1.5]


def test_sustained_drop_is_accepted_as_a_reset(ds):
    seen = [_hold(ds, v, AFTERNOON) for v in (8.1, 0.0, 0.0, 0.0, 0.1)]
    # held for two readings, accepted on the third, then rises normally
    assert seen == [8.1, 8.1, 8.1, 0.0, 0.1]


def test_midnight_reset_is_accepted_immediately(ds):
    _hold(ds, 8.1, datetime(2026, 10, 29, 23, 59, 50))
    # new day: yesterday's carried-over value first, then the real reset
    assert _hold(ds, 8.1, datetime(2026, 10, 30, 0, 0, 5)) == 8.1
    assert _hold(ds, 0.0, datetime(2026, 10, 30, 0, 3, 0)) == 0.0


def test_late_midnight_carryover_still_resets(ds):
    # The inverter can hold yesterday's total past the grace window; the reset
    # must still be accepted once it persists, or the day would be stuck at 8.1.
    t = datetime(2026, 10, 30, 0, 20, 0)
    _hold(ds, 8.1, t)
    seen = [_hold(ds, 0.0, t) for _ in range(3)]
    assert seen[-1] == 0.0


def test_each_counter_is_guarded_independently(ds):
    for f in ds._DAILY_COUNTERS:
        assert _hold(ds, 5.0, AFTERNOON, f) == 5.0
        assert _hold(ds, 0.0, AFTERNOON, f) == 5.0


def test_smooth_applies_the_guard(ds):
    ds._hold_daily_counters({"grid_in_today": 8.1}, now=datetime.now())
    out = ds._smooth({"grid_in_today": 0.0, "grid_out_today": 0.0})
    assert out["grid_in_today"] == 8.1


# ── The cost survives the glitch end to end ──────────────────────────────────

@pytest.fixture
def tou_day(ds, monkeypatch):
    """A TOU tariff mid-way through a day with 195p of import already costed."""
    today = datetime.now().strftime("%Y-%m-%d")
    monkeypatch.setattr(ds, "_log_snapshot", lambda d: None)
    monkeypatch.setattr(ds, "_maybe_backup", lambda: None)
    monkeypatch.setattr(ds, "_tariff_configured", lambda: True)
    monkeypatch.setattr(ds, "TARIFF_TOU", [{"start": "16:00", "end": "19:00", "rate_p": 30.2961}])
    monkeypatch.setattr(ds, "TARIFF_IMPORT_P", 24.2369)
    monkeypatch.setattr(ds, "_tariff_dirty", False)
    monkeypatch.setattr(ds, "_today_register_date", today)
    monkeypatch.setattr(ds, "_today_cost_date", today)
    monkeypatch.setattr(ds, "_today_import_cost_p", 195.0)
    monkeypatch.setattr(ds, "_today_export_income_p", 0.0)
    recomputes = []
    monkeypatch.setattr(ds, "_recompute_today_cost",
                        lambda gin=0.0, gout=0.0: recomputes.append((gin, gout)))
    return recomputes


def _reading(gin):
    return {"ok": True, "grid_in_today": gin, "grid_out_today": 0.0, "grid_w": 0,
            "grid_importing": False, "grid_exporting": False, "status": "1",
            "solar_w": 0, "home_w": 500, "battery_w": 0, "soc": 50}


def test_glitch_does_not_wipe_the_days_import_cost(ds, tou_day):
    st = {"purge": 9e18}
    for gin in (8.1, 0.0, 8.1):
        ds._handle_reading(_reading(gin), st)
    assert tou_day == []                       # no recompute from zero
    assert ds._today_import_cost_p == pytest.approx(195.0)
    assert ds._cached["grid_in_today"] == 8.1  # the bad 0 never reached the page


def test_cost_guard_alone_needs_a_persistent_zero(ds, tou_day, monkeypatch):
    # Even if a zero got past _smooth, one reading must not trigger a recompute
    monkeypatch.setattr(ds, "_hold_daily_counters", lambda out, now=None: None)
    st = {"purge": 9e18}
    ds._handle_reading(_reading(0.0), st)
    assert tou_day == []
    ds._handle_reading(_reading(0.0), st)
    ds._handle_reading(_reading(0.0), st)
    assert tou_day                              # persistent zero: now it acts
