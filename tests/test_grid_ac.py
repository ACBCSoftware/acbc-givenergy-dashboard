"""Grid voltage and frequency decoding (#59).

IR 5 (v_ac1) and IR 13 (f_ac1) arrive in the same input-register page the live
data already uses, so this costs no extra inverter traffic - they were simply
being discarded.

The register map gives the real-world RANGE (0-500 V, 40-70 Hz) but not the
divisor, so _scaled() applies the divisor and then sanity-checks the result
against what is physically possible. A wrong scale must surface as a missing
value, never as a plausible-looking wrong number on a chart.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="gridac-test-"))
    import dashboard_server as ds
    importlib.reload(ds)
    return ds


# ── _scaled ──────────────────────────────────────────────────────────────────

def test_voltage_divisor(srv):
    assert srv._scaled(2403, srv._V_AC_DIVISOR, 10.0, 400.0) == 240.3


def test_frequency_divisor(srv):
    assert srv._scaled(5001, srv._F_AC_DIVISOR, 40.0, 70.0) == 50.01


@pytest.mark.parametrize("raw, why", [
    (0,     "inverter idle / register not populated"),
    (65535, "uint16 fill value"),
    (24030, "voltage with the divisor an order out -> 2403 V"),
])
def test_implausible_voltage_is_none(srv, raw, why):
    assert srv._scaled(raw, srv._V_AC_DIVISOR, 10.0, 400.0) is None, why


@pytest.mark.parametrize("raw, why", [
    (0,     "not running"),
    (500,   "frequency at the wrong divisor -> 5 Hz"),
    (50000, "frequency at the wrong divisor -> 500 Hz"),
])
def test_implausible_frequency_is_none(srv, raw, why):
    assert srv._scaled(raw, srv._F_AC_DIVISOR, 40.0, 70.0) is None, why


def test_non_numeric_is_none(srv):
    assert srv._scaled(None, 10.0, 10.0, 400.0) is None
    assert srv._scaled("x",  10.0, 10.0, 400.0) is None


def test_band_edges_are_inclusive(srv):
    assert srv._scaled(400, 1.0, 10.0, 400.0) == 400.0
    assert srv._scaled(401, 1.0, 10.0, 400.0) is None


# ── Decoding a full input page ───────────────────────────────────────────────

def _page(overrides=None):
    """A plausible Gen2/Gen3 input page as a g(n) accessor."""
    regs = {0: 1, 5: 2412, 13: 5002, 14: 2, 17: 120, 18: 1500, 19: 95, 20: 900,
            25: 30, 26: 80, 30: 0, 36: 50, 37: 40, 41: 300, 42: 600,
            50: 5210, 52: 0, 56: 190, 59: 75}
    if overrides:
        regs.update(overrides)
    return lambda n: regs.get(n, 0)


def test_decodes_grid_ac_from_the_live_page(srv):
    d = srv._build_from_input_page(_page())
    assert d["grid_v"] == 241.2
    assert d["grid_hz"] == 50.02


def test_existing_fields_are_untouched(srv):
    """IR 5 and IR 13 sit beside registers already in use - adding them must not
    disturb the decode of anything else."""
    d = srv._build_from_input_page(_page())
    assert d["solar_w"] == 2400          # p_pv1 1500 + p_pv2 900
    assert d["home_w"] == 600
    assert d["soc"] == 75
    assert d["charge_status"] == 2


def test_idle_inverter_reports_none_not_zero_volts(srv):
    """A page of zeros must not chart as a 0 V / 0 Hz grid collapse."""
    d = srv._build_from_input_page(_page({5: 0, 13: 0}))
    assert d["grid_v"] is None
    assert d["grid_hz"] is None


def test_gateway_page_has_no_grid_ac(srv):
    """The Gateway AIO page does not carry these. Absent is correct - the chart
    must show a gap rather than invent a reading."""
    g = lambda n: {16: 0, 17: 500, 18: 400, 19: 0}.get(n, 0)
    d = srv._build_from_gateway_page(g)
    assert d.get("grid_v") is None
    assert d.get("grid_hz") is None


# ── Storage ──────────────────────────────────────────────────────────────────

def test_migration_adds_both_columns(srv):
    srv.init_db()
    with srv._db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshots)")]
    assert "grid_v" in cols and "grid_hz" in cols
    srv.init_db()      # additive and idempotent


def test_snapshot_round_trip(srv):
    srv.init_db()
    import time as _t
    ts = int(_t.time())
    srv._log_snapshot({
        "ts": ts, "solar_w": 1000, "home_w": 400, "battery_w": 0,
        "battery_charging": 0, "battery_discharging": 0, "grid_w": 50,
        "grid_importing": 1, "grid_exporting": 0, "soc": 60,
        "solar_today": 1.0, "grid_in_today": 0.5, "grid_out_today": 0.0,
        "bat_chg_today": 0.0, "bat_dis_today": 0.0,
        "grid_v": 243.7, "grid_hz": 49.98,
    })
    with srv._db() as conn:
        row = conn.execute(
            "SELECT grid_v, grid_hz FROM snapshots WHERE ts = ?", (ts,)).fetchone()
    assert row["grid_v"] == 243.7
    assert row["grid_hz"] == 49.98


def test_missing_grid_ac_stores_null(srv):
    """Gateway profiles and pre-v3.3 rows have no value; NULL keeps the gap
    visible instead of implying a measured zero."""
    srv.init_db()
    import time as _t
    ts = int(_t.time()) + 1
    srv._log_snapshot({
        "ts": ts, "solar_w": 0, "home_w": 0, "battery_w": 0,
        "battery_charging": 0, "battery_discharging": 0, "grid_w": 0,
        "grid_importing": 0, "grid_exporting": 0, "soc": 50,
        "solar_today": 0.0, "grid_in_today": 0.0, "grid_out_today": 0.0,
        "bat_chg_today": 0.0, "bat_dis_today": 0.0,
    })
    with srv._db() as conn:
        row = conn.execute(
            "SELECT grid_v, grid_hz FROM snapshots WHERE ts = ?", (ts,)).fetchone()
    assert row["grid_v"] is None and row["grid_hz"] is None
