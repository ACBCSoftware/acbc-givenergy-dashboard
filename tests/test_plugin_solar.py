r"""Unit tests for plug-in ("balcony") solar detection (v3.3).

The UK legalised plug-in solar on 27 Aug 2026: a <=800VA micro-inverter plugged
straight into a socket circuit, with no CT clamp and no metering. It sits
downstream of the inverter's grid CT, so the inverter cannot see it -- its
generation just makes the house look like it is drawing less. Consumption and
solar totals then read low, though costs stay correct.

Detection rests on one physical fact: derived house load
(solar + grid + battery) can never be negative on a correctly-metered house,
because the inverter's own books balance. Persistently negative means something
is generating that the inverter cannot measure.

The subtlety these tests pin down is that INSTANTANEOUS samples cannot be used.
Measured against 30 days of real data from a system with no plug-in solar, ~2%
of raw samples come out negative (worst -2991W) purely because the solar, grid
and battery registers are not sampled in lockstep across transients. Averaging
into 15-minute buckets removes that entirely: the lowest real bucket was +177W.
Hence bucket-then-threshold, plus a multi-day persistence requirement.

Run:  venv\Scripts\python.exe tests\test_plugin_solar.py
   or: venv\Scripts\python.exe -m pytest tests\test_plugin_solar.py
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard_server as ds  # noqa: E402


def _make_db(rows):
    """Temp DB with the snapshot columns the detector reads."""
    path = os.path.join(tempfile.mkdtemp(), "history.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE snapshots (
        ts INTEGER PRIMARY KEY, solar_w INTEGER, home_w INTEGER,
        battery_w INTEGER, battery_charging INTEGER, battery_discharging INTEGER,
        grid_w INTEGER, grid_importing INTEGER, grid_exporting INTEGER)""")
    con.executemany("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def _synth(plugin_w, days=14, baseline=350, step=120):
    """Days of samples for a house with `baseline` W of load and a solar arc.
    `plugin_w` is invisible generation running 09:00-16:00 -- the inverter never
    sees it, so it only shows up as the house appearing to draw less."""
    now = int(time.time())
    rows = []
    for d in range(days):
        for s in range(0, 86400, step):
            ts = now - d * 86400 - (86400 - s)
            hour = s / 3600
            pv = max(0, int(2500 * (1 - ((hour - 13) / 6.5) ** 2))) if 6.5 < hour < 19.5 else 0
            plug = plugin_w if 9 < hour < 16 else 0
            visible = baseline - plug          # what the inverter thinks the house uses
            net = visible - pv                 # >0 import, <0 export
            rows.append((ts, pv, max(0, visible), 0, 0, 0,
                         abs(net), 1 if net > 0 else 0, 1 if net < 0 else 0))
    return rows


def _detect(rows):
    ds.DB_PATH = _make_db(rows)
    ds._plugin_cache = (None, 0.0)      # bypass the TTL cache between cases
    return ds._detect_plugin_solar()


def test_clean_system_does_not_trigger():
    """A house with no plug-in solar must never be flagged."""
    r = _detect(_synth(plugin_w=0))
    assert r["detected"] is False
    assert r["buckets"] == 0
    assert r["lowest_w"] > 0, "derived load should stay positive without hidden generation"


def test_800w_plugin_is_detected():
    r = _detect(_synth(plugin_w=800))
    assert r["detected"] is True
    assert r["days"] >= ds._PLUGIN_MIN_DAYS
    assert r["lowest_w"] < ds._PLUGIN_THRESHOLD_W


def test_small_plugin_under_house_baseline_is_not_flagged():
    """A 200W kit under a 350W baseline never drives derived load negative, so
    it is genuinely undetectable by this method. Better to stay silent than to
    guess -- the detector is deliberately specific rather than sensitive."""
    r = _detect(_synth(plugin_w=200))
    assert r["detected"] is False


def test_single_day_anomaly_does_not_trigger():
    """One odd day (a fault, a one-off) must not be enough -- the signature has
    to persist across several days."""
    rows = _synth(plugin_w=0, days=14)
    # Overwrite one day with a strongly negative derived load.
    now = int(time.time())
    spike = []
    for s in range(0, 86400, 120):
        ts = now - 3 * 86400 - (86400 - s)
        spike.append((ts, 0, 0, 0, 0, 0, 900, 0, 1))   # 900W export, nothing generating it
    rows = [r for r in rows if not any(r[0] == s[0] for s in spike)] + spike
    r = _detect(rows)
    assert r["detected"] is False, "one anomalous day should not be enough to flag"


def test_transient_sample_noise_is_averaged_out():
    """Regression for the reason instantaneous samples were rejected: isolated
    wild samples (registers not read in lockstep) must not survive bucketing.
    Real data showed 2% of raw samples negative, worst -2991W, on a system with
    no plug-in solar at all."""
    rows = _synth(plugin_w=0, days=14)
    # Inject a -3000W style transient into roughly every 20th sample by making
    # the inverter briefly report a large export with no generation behind it.
    noisy = []
    for i, r in enumerate(rows):
        if i % 20 == 0:
            noisy.append((r[0], 0, 0, 0, 0, 0, 3000, 0, 1))
        else:
            noisy.append(r)
    res = _detect(noisy)
    assert res["detected"] is False, (
        "sparse transients must average out inside 15-minute buckets; "
        "if this fails the detector is back to trusting instantaneous samples"
    )


def test_hint_suppressed_once_acknowledged():
    """Once the user has dismissed or declared, the hint never shows again."""
    ds.DB_PATH = _make_db(_synth(plugin_w=800))
    ds._plugin_cache = (None, 0.0)

    ds.PLUGIN_SOLAR_NOTIFIED = False
    ds.PLUGIN_SOLAR_DECLARED = False
    assert ds._plugin_solar_hint()["show"] is True

    ds.PLUGIN_SOLAR_NOTIFIED = True
    assert ds._plugin_solar_hint()["show"] is False

    ds.PLUGIN_SOLAR_NOTIFIED = False
    ds.PLUGIN_SOLAR_DECLARED = True
    h = ds._plugin_solar_hint()
    assert h["show"] is False and h["declared"] is True

    ds.PLUGIN_SOLAR_NOTIFIED = False
    ds.PLUGIN_SOLAR_DECLARED = False


def test_detection_never_raises_on_bad_db():
    """Detection is a nice-to-have: a broken/missing DB must degrade quietly
    rather than take out whatever called it."""
    ds.DB_PATH = os.path.join(tempfile.mkdtemp(), "does-not-exist.db")
    ds._plugin_cache = (None, 0.0)
    r = ds._detect_plugin_solar()
    assert r["detected"] is False


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nAll {len(fns)} plug-in solar tests passed.")


if __name__ == "__main__":
    _run()
