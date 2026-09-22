"""Inverter clock drift detection and automatic correction (#53).

Why this exists: the scheduler writes charge/discharge SLOT registers as
wall-clock HH:MM windows, and the INVERTER decides whether "now" falls inside
one using its own RTC. A drifting inverter clock therefore stops scheduled rules
firing while everything else looks healthy -- the dashboard writes a perfectly
good 14:30-15:00 slot and the inverter does not believe it is 14:30 yet.

Verified on real hardware 22 Sep 2026: HR 35-40 read [26, 9, 22, 11, 50, 26],
decoding to 2026-09-22 11:50:26, which was 51 seconds behind the dashboard.
"""
import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="clock-test-"))
    import dashboard_server as ds
    importlib.reload(ds)
    return ds


# ── Decoding ─────────────────────────────────────────────────────────────────

def test_decodes_real_hardware_reading(srv):
    """The exact register values read off the GIV-AC3.0 on 22 Sep 2026."""
    assert srv._decode_inverter_clock([26, 9, 22, 11, 50, 26]) == \
        datetime(2026, 9, 22, 11, 50, 26)


def test_year_is_offset_from_2000(srv):
    assert srv._decode_inverter_clock([0, 1, 1, 0, 0, 0]).year == 2000
    assert srv._decode_inverter_clock([99, 1, 1, 0, 0, 0]).year == 2099


def test_extra_registers_are_ignored(srv):
    """Callers pass regs_0[35:41]; a longer slice must not change the result."""
    assert srv._decode_inverter_clock([26, 9, 22, 11, 50, 26, 999, 999]) == \
        datetime(2026, 9, 22, 11, 50, 26)


# ── Refusing to act on nonsense ──────────────────────────────────────────────
# These registers read as zeros or garbage on some firmware before the clock has
# ever been set. A wild value must return None, never a huge "drift" that would
# then be corrected against -- the correction WRITES to the inverter.

@pytest.mark.parametrize("regs, why", [
    ([0, 0, 0, 0, 0, 0],        "month 0 / day 0 - never been set"),
    ([26, 13, 1, 0, 0, 0],      "month 13"),
    ([26, 2, 31, 0, 0, 0],      "31 February"),
    ([26, 9, 22, 25, 0, 0],     "hour 25"),
    ([26, 9, 22, 11, 60, 0],    "minute 60"),
    ([100, 9, 22, 11, 50, 26],  "year offset out of range"),
    ([26, 9, 22],               "short read"),
    ([],                        "empty read"),
    ([None, None, None, None, None, None], "nulls"),
    (["x", "y", "z", "a", "b", "c"],       "non-numeric"),
])
def test_nonsense_returns_none(srv, regs, why):
    assert srv._decode_inverter_clock(regs) is None, why


def test_drift_is_none_when_clock_unreadable(srv):
    assert srv._clock_drift_from([0, 0, 0, 0, 0, 0]) is None


def test_cache_untouched_by_an_unreadable_reading(srv):
    """A bad read must not wipe a good cached value."""
    srv._cache_clock_drift([26, 9, 22, 11, 50, 26])
    before = dict(srv._clock_cache)
    srv._cache_clock_drift([0, 0, 0, 0, 0, 0])
    assert srv._clock_cache["inverter_time"] == before["inverter_time"]


# ── Drift sign and magnitude ─────────────────────────────────────────────────

def _regs_for(dt):
    return [dt.year - 2000, dt.month, dt.day, dt.hour, dt.minute, dt.second]


def test_behind_is_negative(srv):
    """The real-world case: inverter lagging. Sign must not invert."""
    drift = srv._clock_drift_from(_regs_for(datetime.now() - timedelta(seconds=120)))
    assert drift == pytest.approx(-120, abs=2)


def test_ahead_is_positive(srv):
    drift = srv._clock_drift_from(_regs_for(datetime.now() + timedelta(seconds=90)))
    assert drift == pytest.approx(90, abs=2)


def test_in_step_is_near_zero(srv):
    assert abs(srv._clock_drift_from(_regs_for(datetime.now()))) < 2


# ── Correction threshold ─────────────────────────────────────────────────────

def test_threshold_suits_a_thirty_minute_scheduler_grid(srv):
    """Tight enough that a slot starts on time, loose enough not to write to the
    inverter constantly. Andi's own drift was 51s, i.e. above this."""
    assert 10 <= srv._CLOCK_CORRECT_SECS <= 60


def test_real_world_drift_would_be_corrected(srv):
    """51s behind, the figure measured on real hardware."""
    drift = srv._clock_drift_from(_regs_for(datetime.now() - timedelta(seconds=51)))
    assert abs(drift) >= srv._CLOCK_CORRECT_SECS


def test_small_drift_is_left_alone(srv):
    drift = srv._clock_drift_from(_regs_for(datetime.now() - timedelta(seconds=5)))
    assert abs(drift) < srv._CLOCK_CORRECT_SECS


def test_check_interval_is_sparing(srv):
    """Each check opens a second socket, which briefly kicks the listen loop."""
    assert srv._CLOCK_CHECK_SECS >= 3600
