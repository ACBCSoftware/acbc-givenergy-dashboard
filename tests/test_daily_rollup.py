"""Daily rollup cache correctness and self-healing.

Why it exists: finding "the last snapshot of each day" across the raw table cost
4.11s on a real 2.2M-row database, because date(ts,'unixepoch','localtime') has
to be computed for every row and cannot be indexed. That cost was paid on EVERY
history request, including period=day - which is why asking for one day cost the
same as asking for a year.

daily_totals is strictly a CACHE. Every row is recomputable from snapshots, so
the tests below care most about one thing: it must never be able to drift into
being quietly wrong.
"""
import importlib
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="rollup-test-"))
    import dashboard_server as ds
    importlib.reload(ds)
    ds.init_db()
    return ds


def _day_str(offset_days=0):
    return (datetime.now() - timedelta(days=offset_days)).strftime("%Y-%m-%d")


@pytest.fixture
def seeded(srv):
    """Three days of snapshots with known end-of-day counters."""
    with srv._db() as conn:
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM daily_totals")
        for back, solar in ((2, 11.4), (1, 7.7), (0, 3.2)):
            t0, t1 = srv._local_day_bounds(_day_str(back))
            # Three samples; the LAST one carries the day's final counters.
            for i, frac in enumerate((0.2, 0.5, 0.9)):
                conn.execute(
                    "INSERT INTO snapshots (ts, solar_today, grid_in_today, "
                    " grid_out_today, bat_chg_today, bat_dis_today) VALUES (?,?,?,?,?,?)",
                    (int(t0 + (t1 - t0) * frac), solar * (i + 1) / 3,
                     1.0 * (i + 1), 0.5 * (i + 1), 2.0 * (i + 1), 1.5 * (i + 1)))
        conn.commit()
    return srv


# ── Correctness ──────────────────────────────────────────────────────────────

def test_rollup_takes_the_last_snapshot_of_each_day(seeded):
    """The inverter's counters are cumulative within a day, so the final sample
    of the day is the day's total. Picking any other row understates it."""
    seeded._ensure_daily_totals()
    with seeded._db() as conn:
        rows = {r["day"]: r for r in conn.execute("SELECT * FROM daily_totals")}
    assert len(rows) == 3
    assert rows[_day_str(2)]["s"] == pytest.approx(11.4)
    assert rows[_day_str(1)]["s"] == pytest.approx(7.7)
    assert rows[_day_str(2)]["gi"] == pytest.approx(3.0)   # 1.0 * 3
    assert rows[_day_str(2)]["bd"] == pytest.approx(4.5)   # 1.5 * 3


def test_matches_a_direct_scan_of_the_raw_table(seeded):
    """The rollup must agree with the query it replaced, or it is just a fast
    way to be wrong."""
    seeded._ensure_daily_totals()
    with seeded._db() as conn:
        direct = {r[0]: r[1] for r in conn.execute(
            "SELECT date(ts,'unixepoch','localtime'), solar_today FROM snapshots "
            "WHERE ts IN (SELECT MAX(ts) FROM snapshots "
            "             GROUP BY date(ts,'unixepoch','localtime'))")}
        cached = {r[0]: r[1] for r in conn.execute("SELECT day, s FROM daily_totals")}
    assert cached == pytest.approx(direct)


# ── Self-healing ─────────────────────────────────────────────────────────────

def test_a_deleted_row_is_rebuilt(seeded):
    """A cache that cannot repair itself becomes a source of silent wrong
    answers the first time anything touches it."""
    seeded._ensure_daily_totals()
    target = _day_str(2)
    with seeded._db() as conn:
        conn.execute("DELETE FROM daily_totals WHERE day = ?", (target,))
        conn.commit()
    seeded._ensure_daily_totals()
    with seeded._db() as conn:
        row = conn.execute("SELECT s FROM daily_totals WHERE day = ?", (target,)).fetchone()
    assert row is not None and row["s"] == pytest.approx(11.4)


def test_a_corrupted_row_is_corrected_for_recent_days(seeded):
    """Today and yesterday are still accumulating, so they are always recomputed
    rather than trusted."""
    seeded._ensure_daily_totals()
    today = _day_str(0)
    with seeded._db() as conn:
        conn.execute("UPDATE daily_totals SET s = 999 WHERE day = ?", (today,))
        conn.commit()
    seeded._ensure_daily_totals()
    with seeded._db() as conn:
        row = conn.execute("SELECT s FROM daily_totals WHERE day = ?", (today,)).fetchone()
    assert row["s"] == pytest.approx(3.2), "today must be refreshed, not trusted"


def test_new_data_for_today_is_picked_up(seeded):
    """The common case: the day advances while the dashboard is running."""
    seeded._ensure_daily_totals()
    today = _day_str(0)
    t0, t1 = seeded._local_day_bounds(today)
    with seeded._db() as conn:
        conn.execute(
            "INSERT INTO snapshots (ts, solar_today, grid_in_today, grid_out_today,"
            " bat_chg_today, bat_dis_today) VALUES (?,?,?,?,?,?)",
            (int(t0 + (t1 - t0) * 0.95), 4.9, 9.9, 0.0, 0.0, 0.0))
        conn.commit()
    seeded._ensure_daily_totals()
    with seeded._db() as conn:
        row = conn.execute("SELECT s FROM daily_totals WHERE day = ?", (today,)).fetchone()
    assert row["s"] == pytest.approx(4.9)


def test_a_day_whose_data_is_purged_loses_its_row(seeded):
    """Retention purging deletes snapshots; a stale rollup row would keep the day
    visible in history with no data behind it."""
    seeded._ensure_daily_totals()
    target = _day_str(2)
    t0, t1 = seeded._local_day_bounds(target)
    with seeded._db() as conn:
        conn.execute("DELETE FROM snapshots WHERE ts >= ? AND ts < ?", (t0, t1))
        conn.execute("DELETE FROM daily_totals WHERE day = ?", (target,))
        conn.commit()
    seeded._ensure_daily_totals()
    with seeded._db() as conn:
        row = conn.execute("SELECT 1 FROM daily_totals WHERE day = ?", (target,)).fetchone()
    assert row is None


# ── Day boundaries ───────────────────────────────────────────────────────────

def test_day_bounds_are_a_full_local_day(srv):
    t0, t1 = srv._local_day_bounds("2026-06-15")
    assert t1 - t0 == 86400
    assert datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M:%S") == "2026-06-15 00:00:00"


def test_clock_change_days_are_not_assumed_to_be_24h(srv):
    """UK clocks go forward on 29 Mar 2026 and back on 25 Oct 2026. Assuming
    86400 would clip an hour off one and double-count an hour on the other.
    Skipped where the test machine is not on a DST-observing timezone.
    """
    spring = srv._local_day_bounds("2026-03-29")[1] - srv._local_day_bounds("2026-03-29")[0]
    autumn = srv._local_day_bounds("2026-10-25")[1] - srv._local_day_bounds("2026-10-25")[0]
    if spring == 86400 and autumn == 86400:
        pytest.skip("test machine timezone does not observe DST")
    assert spring == 82800, "spring forward day should be 23 hours"
    assert autumn == 90000, "autumn back day should be 25 hours"


def test_bad_day_string_raises(srv):
    with pytest.raises(ValueError):
        srv._local_day_bounds("not-a-date")


# ── Plug-in energy inside the rollup ─────────────────────────────────────────

def test_plugin_energy_is_integrated_and_gaps_ignored(srv):
    srv.init_db()
    day = _day_str(5)
    t0, _ = srv._local_day_bounds(day)
    with srv._db() as conn:
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM daily_totals")
        # 10 samples 10s apart at 360W, then an hour gap, then one more.
        for i in range(10):
            conn.execute("INSERT INTO snapshots (ts, plugin_w, solar_today) VALUES (?,?,?)",
                         (t0 + 3600 + i * 10, 360.0, 1.0))
        conn.execute("INSERT INTO snapshots (ts, plugin_w, solar_today) VALUES (?,?,?)",
                     (t0 + 3600 + 3600, 360.0, 1.0))
        conn.commit()
        srv._rollup_day(conn, day)
        conn.commit()
        row = conn.execute("SELECT p FROM daily_totals WHERE day = ?", (day,)).fetchone()
    # 9 intervals x 10s x 360W = 0.009 kWh; the hour gap contributes nothing.
    assert row["p"] == pytest.approx(0.009, abs=1e-4)


# ── The repeating-backfill bug ───────────────────────────────────────────────

def test_days_without_data_do_not_retrigger_the_cost_backfill(srv, monkeypatch):
    """The expensive one, and it only appears once history predates the app.

    _dates_for_period('year', Y) returns every day of the year, but a day with
    no snapshots can never produce a daily_costs row. Those days therefore stayed
    "missing" for ever and re-ran the whole cost back-fill on EVERY request. It
    was invisible until a year of data was back-dated in, which created 273
    permanently-empty days and made a full-table rescan happen on every year
    view. The guard filters the list down to days that actually have data.
    """
    srv.init_db()
    with srv._db() as conn:
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM daily_totals")
        conn.execute("DELETE FROM daily_costs")
        # Two days of data inside a month-long window of requested dates.
        for back in (1, 2):
            t0, t1 = srv._local_day_bounds(_day_str(back))
            conn.execute(
                "INSERT INTO snapshots (ts, solar_today, grid_in_today, grid_out_today,"
                " bat_chg_today, bat_dis_today) VALUES (?,?,?,?,?,?)",
                (int(t0 + (t1 - t0) * 0.9), 5.0, 2.0, 1.0, 0.0, 0.0))
        conn.commit()
    srv._ensure_daily_totals()

    asked = []
    monkeypatch.setattr(srv, "_batch_compute_and_store_costs", lambda d: asked.append(list(d)))
    monkeypatch.setattr(srv, "_tariff_configured", lambda: True)

    # 30 days requested, only 2 have data.
    dates = [_day_str(i) for i in range(30)]
    srv._ensure_daily_costs(dates)

    assert len(asked) == 1
    requested = set(asked[0])
    assert requested <= {_day_str(1), _day_str(2)}, \
        f"back-fill asked for days with no data: {sorted(requested)}"


def test_backfill_is_skipped_entirely_when_no_day_has_data(srv, monkeypatch):
    srv.init_db()
    with srv._db() as conn:
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM daily_totals")
        conn.execute("DELETE FROM daily_costs")
        conn.commit()
    called = []
    monkeypatch.setattr(srv, "_batch_compute_and_store_costs", lambda d: called.append(d))
    monkeypatch.setattr(srv, "_tariff_configured", lambda: True)
    srv._ensure_daily_costs([_day_str(i) for i in range(10)])
    assert called == [], "nothing to compute, so nothing should be attempted"
