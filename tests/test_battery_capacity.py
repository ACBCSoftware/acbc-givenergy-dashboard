"""Battery capacity detection tests.

Verified against real hardware 21 Sep 2026: HR55 reported 372 Ah, and the two
BMS modules independently reported cap_design 18600 each. 18600 x 2 = 372 x 100,
which confirms both the HR55 reading and pins cap_design's unit at 0.01 Ah -- a
scale the code had previously flagged as unvalidated because only the ratio was
ever consumed.
"""
import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="batt-test-"))
    import dashboard_server as ds
    importlib.reload(ds)
    return ds


# Verified on real hardware 21 Sep 2026: HR55 = 372 Ah, and the two BMS modules
# reported cap_design 18600 each (18600 x 2 = 372 x 100), which independently
# confirms both the HR55 reading and cap_design's 0.01 Ah unit.

def test_capacity_cache_accepts_plausible_ah(srv, monkeypatch):
    monkeypatch.setattr(srv, "_inverter_profile", "single_phase_ac_coupled")
    srv._cache_battery_capacity_ah(372)
    assert srv._batt_cap_cache["ah"] == 372


def test_capacity_cache_rejects_zero_and_absurd(srv, monkeypatch):
    monkeypatch.setattr(srv, "_inverter_profile", "single_phase_ac_coupled")
    srv._cache_battery_capacity_ah(372)
    srv._cache_battery_capacity_ah(0)          # unconfigured inverter
    assert srv._batt_cap_cache["ah"] == 372    # good value not clobbered
    srv._cache_battery_capacity_ah(999999)     # nonsense
    assert srv._batt_cap_cache["ah"] == 372
    srv._cache_battery_capacity_ah(None)
    assert srv._batt_cap_cache["ah"] == 372


def test_kwh_conversion_matches_real_hardware(srv, monkeypatch):
    """372 Ah at 51.2 V nominal = 19.0 kWh (2 x 9.5 kWh modules)."""
    monkeypatch.setattr(srv, "_inverter_profile", "single_phase_ac_coupled")
    monkeypatch.setattr(srv, "_batt_cap_cache", {"ts": time.time(), "ah": 372})
    assert srv._battery_capacity_kwh() == 19.0


def test_single_module_bank(srv, monkeypatch):
    """186 Ah = one 9.5 kWh module -- the value Andi's config had gone stale at."""
    monkeypatch.setattr(srv, "_inverter_profile", "single_phase_ac_coupled")
    monkeypatch.setattr(srv, "_batt_cap_cache", {"ts": time.time(), "ah": 186})
    assert srv._battery_capacity_kwh() == 9.5


def test_capacity_never_cached_on_profiles_without_lv_modules(srv, monkeypatch):
    """AIO / three-phase store capacity differently. A value read from one must
    never reach the cache at all.

    The guard lives in _cache_battery_capacity_ah rather than the accessor so it
    is enforced for every caller -- the control-state read caches this too, and
    an accessor-only guard let that path slip a value through.
    """
    for profile in ("three_phase_aio", "gateway_aio", "unknown", ""):
        monkeypatch.setattr(srv, "_inverter_profile", profile)
        monkeypatch.setattr(srv, "_batt_cap_cache", {"ts": 0.0, "ah": None})
        srv._cache_battery_capacity_ah(372)
        assert srv._batt_cap_cache["ah"] is None, profile
        assert srv._battery_capacity_ah() is None
        assert srv._battery_capacity_kwh() is None


def test_zero_config_means_follow_the_hardware(srv, monkeypatch):
    monkeypatch.setattr(srv, "_inverter_profile", "single_phase_ac_coupled")
    monkeypatch.setattr(srv, "_batt_cap_cache", {"ts": time.time(), "ah": 372})
    monkeypatch.setattr(srv, "PRED_BATTERY_KWH", 0.0)
    assert srv._effective_battery_kwh() == 19.0


def test_explicit_config_overrides_hardware(srv, monkeypatch):
    """A deliberate conservative figure must not be silently overwritten."""
    monkeypatch.setattr(srv, "_inverter_profile", "single_phase_ac_coupled")
    monkeypatch.setattr(srv, "_batt_cap_cache", {"ts": time.time(), "ah": 372})
    monkeypatch.setattr(srv, "PRED_BATTERY_KWH", 8.0)
    assert srv._effective_battery_kwh() == 8.0


def test_predictive_target_uses_detected_capacity(srv, monkeypatch):
    """The actual bug this fixes: a stale 9.5 on a 19 kWh bank divides by half
    the real capacity and sets the overnight target too LOW, under-importing on
    cheap rate. Same quiet under-charge as the plug-in solar case."""
    monkeypatch.setattr(srv, "_inverter_profile", "single_phase_ac_coupled")
    monkeypatch.setattr(srv, "_batt_cap_cache", {"ts": time.time(), "ah": 372})
    monkeypatch.setattr(srv, "_get_tomorrow_forecast_kwh", lambda: 10.0)
    monkeypatch.setattr(srv, "_compute_morning_demand_kwh", lambda: 2.0)
    monkeypatch.setattr(srv, "PRED_PESSIMISM", 0.85)
    monkeypatch.setattr(srv, "PRED_MIN_SOC", 20)

    monkeypatch.setattr(srv, "PRED_BATTERY_KWH", 0.0)      # auto -> 19.0 kWh
    auto = srv._compute_predictive_target()
    monkeypatch.setattr(srv, "PRED_BATTERY_KWH", 9.5)      # stale one-module value
    stale = srv._compute_predictive_target()

    # usable = 10*0.85 - 2 = 6.5
    # 19.0 kWh -> 100*(1-6.5/19.0)  = 66%
    #  9.5 kWh -> 100*(1-6.5/9.5)   = 32%
    assert auto == 66
    assert stale == 32
    assert auto > stale, "stale capacity must be the one that under-charges"


def test_capacity_unknown_disables_predictive(srv, monkeypatch):
    """No capacity and no override means predictive charge stays off rather than
    guessing at a bank size."""
    monkeypatch.setattr(srv, "_inverter_profile", "three_phase_aio")
    monkeypatch.setattr(srv, "_batt_cap_cache", {"ts": 0.0, "ah": None})
    monkeypatch.setattr(srv, "PRED_BATTERY_KWH", 0.0)
    assert srv._effective_battery_kwh() == 0.0
    assert srv._compute_predictive_target() is None
