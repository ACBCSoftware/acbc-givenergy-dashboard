r"""Unit tests for the Year in Review annual cost projection (#53 follow-up,
v3.2).

Background: `_project_year_totals` used to scale the actual recorded £
import/export totals up to a full year by the SAME ratio as the kWh override
(or curve estimate). That works fine once real data spans a representative
mix of the year, but early in an install's life the only real data recorded
can be a lopsided slice (e.g. entirely mid-summer for a fresh solar+battery
install). Extrapolating that slice's favourable £/kWh ratio across the whole
year produced an overly optimistic full-year estimate (predicting a net
EARNING) that contradicted a full multi-month tariff history, since a
solar/battery household's cost efficiency is much worse in winter than summer
in a way plain kWh scaling doesn't capture.

First fix tried: cost ALL remaining kWh at the plain average import rate, as
if zero further solar offset applied. Too conservative in the other
direction, and physically incoherent -- it double-counted the same
unrecorded solar as both "doesn't reduce import" and "fully exported".

Actual fix (`_remaining_import_export_kwh`): net the seasonal curves' monthly
solar against monthly consumption for days not yet recorded -- that month's
solar first offsets that month's consumption, only the shortfall becomes
import, only the surplus becomes export. Winter nets mostly to import,
shoulder months (Apr/Sep) net mostly to export -- the real asymmetry, using
curves already in the code, no new data needed.

Run:  venv\Scripts\python.exe tests\test_year_projection.py
   or: venv\Scripts\python.exe -m pytest tests\test_year_projection.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard_server as ds  # noqa: E402


def _set_flat_tariff(import_p, export_p, standing_p=0.0):
    ds.TARIFF_SOURCE   = "manual"
    ds.TARIFF_TOU      = []
    ds.TARIFF_IMPORT_P = import_p
    ds.TARIFF_EXPORT_P = export_p
    ds.TARIFF_STANDING_P = standing_p


def _summer_days(n, start="2026-06-01"):
    """n consecutive YYYY-MM-DD strings starting at `start`, June-ish (high
    solar, low consumption in the seasonal curve)."""
    y, m, d = (int(x) for x in start.split("-"))
    base = datetime(y, m, d)
    from datetime import timedelta
    return [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def test_summer_only_data_does_not_flip_year_to_net_earning():
    """Regression for the exact bug reported: ~70 real days entirely in
    summer, with annual kWh overrides set (as Andi's install had), must not
    extrapolate summer's favourable £/kWh ratio into a net-earning full-year
    prediction when the household's own tariff clearly implies a net cost."""
    _set_flat_tariff(import_p=24.2, export_p=11.8, standing_p=56.3373)
    ds.YR_CONSUMPTION_OVERRIDE_KWH = 5124.0
    ds.YR_SOLAR_OVERRIDE_KWH       = 4435.0

    days = _summer_days(69)
    daily_rows = [{"day": d, "s": 21.7} for d in days]   # ~1500 kWh solar / 69 days
    total_solar = sum(r["s"] for r in daily_rows)

    def home_fn(r):
        return 11.2   # ~772 kWh consumption / 69 days, matches the reported case

    # Recorded days were cheap to run (lots of self-consumed solar): small
    # actual import cost, large actual export income -- exactly the lopsided
    # slice that broke the old ratio-based scaling.
    actual = {"import_cost_p": 5762.6, "export_income_p": 22069.9}
    orig = ds._get_costs_for_period
    ds._get_costs_for_period = lambda period_type, period_key: actual
    try:
        year = days[0][:4]
        # Force "current year" path regardless of when the test runs.
        real_now = ds.datetime
        class _FixedNow(real_now):
            @classmethod
            def now(cls):
                return real_now(int(year), 7, 1)
        ds.datetime = _FixedNow
        try:
            result = ds._project_year_totals(int(year), daily_rows, total_solar, home_fn)
        finally:
            ds.datetime = real_now
    finally:
        ds._get_costs_for_period = orig

    assert result["has_enough_data"] is True
    # The core assertion: a household whose real tariff averages a much
    # higher import rate than export rate must not be projected as a net
    # EARNER for the year just because the only recorded days were sunny --
    # but nor should it swing to an implausible extreme just because none of
    # the unrecorded months get any solar credit at all. Hand-verified
    # against the real Pi data this scenario is drawn from: netting lands at
    # +£405.86 (vs the old bug's -£59.81 and the too-pessimistic +£747.82
    # tried and rejected in between).
    net_cost_gbp = result["net_cost_p"] / 100
    assert 300 < net_cost_gbp < 550, (
        f"expected a moderate net COST for the year (~£406), got £{net_cost_gbp:.2f} "
        "-- either the old optimistic-extrapolation bug or the zero-solar-credit "
        "overcorrection may be back"
    )


def test_remaining_import_export_nets_solar_against_consumption():
    """With equal annual consumption/solar totals, total netted import must
    exactly equal total netted export (a conservation check: the sum of
    monthly shortfalls must equal the sum of monthly surpluses when the two
    curves sum to the same annual total) -- while individual months still
    net asymmetrically: December (heavy consumption weight, negligible solar
    weight) must net to import, not export."""
    annual_home_kwh  = 3650.0
    annual_solar_kwh = 3650.0   # matched, so only the curve SHAPES differ

    imp_kwh, exp_kwh = ds._remaining_import_export_kwh(
        2026, day_strs=[], annual_home_kwh=annual_home_kwh, annual_solar_kwh=annual_solar_kwh)
    assert abs(imp_kwh - exp_kwh) < 0.01

    dec_cons  = annual_home_kwh  * ds._YR_CONSUMPTION_WEIGHTS[11]
    dec_solar = annual_solar_kwh * ds._YR_SOLAR_WEIGHTS[11]
    assert dec_solar < dec_cons   # December: solar can't cover its own consumption

    # Recording every month EXCEPT December leaves only December "remaining"
    # -- isolates that one month's net direction.
    from datetime import timedelta, datetime as dt
    all_but_dec = [dt(2026, m, d).strftime("%Y-%m-%d")
                   for m in range(1, 12) for d in range(1, ds._days_in_month(2026, m) + 1)]
    dec_imp, dec_exp = ds._remaining_import_export_kwh(
        2026, all_but_dec, annual_home_kwh, annual_solar_kwh)
    assert dec_imp > 0 and dec_exp == 0


def test_no_override_falls_back_to_curve_estimate_for_kwh():
    """Without a manual override, annual kWh totals still come from the
    seasonal curve -- unaffected by the cost-side rework."""
    _set_flat_tariff(import_p=20.0, export_p=10.0, standing_p=0.0)
    ds.YR_CONSUMPTION_OVERRIDE_KWH = 0
    ds.YR_SOLAR_OVERRIDE_KWH       = 0

    days = _summer_days(30)
    daily_rows = [{"day": d, "s": 10.0} for d in days]
    total_solar = 300.0

    ds._get_costs_for_period = lambda period_type, period_key: {
        "import_cost_p": 1000.0, "export_income_p": 2000.0}
    year = days[0][:4]
    real_now = ds.datetime
    class _FixedNow(real_now):
        @classmethod
        def now(cls):
            return real_now(int(year), 7, 1)
    ds.datetime = _FixedNow
    try:
        result = ds._project_year_totals(int(year), daily_rows, total_solar, lambda r: 5.0)
    finally:
        ds.datetime = real_now

    assert result["annual_solar_kwh"] > total_solar   # scaled up via the curve
    assert result["net_cost_p"] is not None


def test_avg_tou_export_rate_flat_tariff():
    _set_flat_tariff(import_p=20.0, export_p=15.0)
    assert ds._avg_tou_export_rate() == 15.0


def test_avg_tou_export_rate_tou_window():
    ds.TARIFF_SOURCE = "manual"
    ds.TARIFF_EXPORT_P = 10.0
    ds.TARIFF_TOU = [{"start": "00:00", "end": "12:00", "rate_p": 5.0,
                       "export_rate_p": 30.0}]
    rate = ds._avg_tou_export_rate()
    # Half the day at 30p export, half at the flat 10p fallback -> 20p avg.
    assert abs(rate - 20.0) < 0.01


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nAll {len(fns)} year-projection tests passed.")


if __name__ == "__main__":
    _run()
