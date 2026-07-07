r"""Unit tests for tariff TOU-window extraction and cost application (#55).

The v2.9-and-earlier extractor took the *cheapest* band as the base flat rate
and kept only the longest stretch of each pricier rate as a window. For Octopus
Flux (standard rate split into three disjoint stretches) that mis-set the base
to the off-peak rate and left ~5 h/day of standard usage costed at off-peak.

v3.0 fix: base = the rate covering the greatest total duration (the flat rate);
every band that *differs* from base — cheaper or pricier — becomes its own
window, each disjoint stretch kept separately. These tests pin that behaviour
for all four fetched Octopus tariff shapes plus the naming/cap edges.

Run:  venv\Scripts\python.exe tests\test_tariff.py
   or: venv\Scripts\python.exe -m pytest tests\test_tariff.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard_server as ds  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

# A fixed winter day so _is_bst() applies no offset — local == UTC, keeping the
# expected HH:MM boundaries identical to what we feed in.
_DAY = "2026-01-15"


def _slots(bands):
    """bands: list of (start_hhmm, end_hhmm, rate). 24:00 -> next-day 00:00."""
    out = []
    for s, e, r in bands:
        sf = f"{_DAY}T{s}"
        ef = ("2026-01-16T00:00" if e == "24:00" else f"{_DAY}T{e}")
        out.append({"valid_from": sf, "valid_to": ef, "value_inc_vat": r})
    return out


def _win_map(windows):
    """{start_hhmm: (name, rate_p)} for easy assertions."""
    return {w["start"]: (w["name"], round(w["rate_p"], 2)) for w in windows}


def _rate_at(hhmm):
    """Cost the module would apply at HH:MM today, given current globals."""
    dt = datetime.strptime(f"{_DAY}T{hhmm}", "%Y-%m-%dT%H:%M")
    return round(ds._tariff_import_rate(dt.timestamp()), 2)


def _apply(base_rate, windows):
    """Push extractor output into the module globals, as a fetch would."""
    ds.TARIFF_SOURCE = "manual"      # non-variable -> TOU windows are honoured
    ds.TARIFF_IMPORT_P = base_rate
    ds.TARIFF_TOU = windows


# ── Flux: standard fragmented into 3 stretches, one off-peak, one peak ─────────

def test_flux_base_is_standard_not_cheapest():
    base, wins = ds._extract_tou_windows(_slots([
        ("00:00", "02:00", 25.55),   # standard
        ("02:00", "05:00", 15.34),   # off-peak
        ("05:00", "16:00", 25.55),   # standard
        ("16:00", "19:00", 35.78),   # peak
        ("19:00", "24:00", 25.55),   # standard
    ]))
    assert round(base, 2) == 25.55                       # dominant, NOT 15.34
    wm = _win_map(wins)
    assert wm == {"02:00": ("Off-peak", 15.34),
                  "16:00": ("Peak",     35.78)}


def test_flux_cost_at_every_stretch():
    base, wins = ds._extract_tou_windows(_slots([
        ("00:00", "02:00", 25.55), ("02:00", "05:00", 15.34),
        ("05:00", "16:00", 25.55), ("16:00", "19:00", 35.78),
        ("19:00", "24:00", 25.55),
    ]))
    _apply(base, wins)
    assert _rate_at("01:00") == 25.55   # was wrongly 15.34 pre-v3.0
    assert _rate_at("03:00") == 15.34
    assert _rate_at("12:00") == 25.55
    assert _rate_at("17:00") == 35.78
    assert _rate_at("20:00") == 25.55   # was wrongly 15.34 pre-v3.0


# ── Cosy: cheap rate in THREE disjoint windows + a peak (needs >3 slots) ───────

def test_cosy_keeps_all_three_cheap_windows():
    base, wins = ds._extract_tou_windows(_slots([
        ("00:00", "04:00", 26.70),   # standard
        ("04:00", "07:00", 13.00),   # cheap
        ("07:00", "13:00", 26.70),   # standard
        ("13:00", "16:00", 13.00),   # cheap
        ("16:00", "19:00", 40.06),   # peak
        ("19:00", "22:00", 26.70),   # standard
        ("22:00", "24:00", 13.00),   # cheap
    ]))
    assert round(base, 2) == 26.70
    wm = _win_map(wins)
    assert wm == {"04:00": ("Off-peak", 13.00),
                  "13:00": ("Off-peak", 13.00),
                  "16:00": ("Peak",     40.06),
                  "22:00": ("Off-peak", 13.00)}
    _apply(base, wins)
    assert _rate_at("01:00") == 26.70   # standard stretch, NOT cheap
    assert _rate_at("05:00") == 13.00
    assert _rate_at("14:00") == 13.00
    assert _rate_at("17:00") == 40.06
    assert _rate_at("23:00") == 13.00


# ── Go: single overnight cheap window, standard the rest ───────────────────────

def test_go_single_offpeak_window():
    base, wins = ds._extract_tou_windows(_slots([
        ("00:00", "00:30", 24.00),
        ("00:30", "05:30", 8.50),
        ("05:30", "24:00", 24.00),
    ]))
    assert round(base, 2) == 24.00
    assert _win_map(wins) == {"00:30": ("Off-peak", 8.50)}
    _apply(base, wins)
    assert _rate_at("03:00") == 8.50
    assert _rate_at("00:15") == 24.00
    assert _rate_at("12:00") == 24.00


# ── Intelligent Flux: cheap block fragmented across midnight, no peak ──────────

def test_intelligent_flux_cross_midnight_fragments():
    base, wins = ds._extract_tou_windows(_slots([
        ("00:00", "05:30", 12.00),   # tail of the overnight cheap block
        ("05:30", "23:30", 28.00),   # standard
        ("23:30", "24:00", 12.00),   # head of the overnight cheap block
    ]))
    assert round(base, 2) == 28.00
    wm = _win_map(wins)
    assert wm == {"00:00": ("Off-peak", 12.00),
                  "23:30": ("Off-peak", 12.00)}
    _apply(base, wins)
    assert _rate_at("02:00") == 12.00
    assert _rate_at("12:00") == 28.00
    assert _rate_at("23:45") == 12.00


# ── naming: two cheaper + two pricier levels get Super variants ────────────────

def test_super_naming_multiple_levels():
    _, wins = ds._extract_tou_windows(_slots([
        ("00:00", "02:00", 5.00),    # cheapest  -> Super off-peak
        ("02:00", "05:00", 12.00),   # cheaper   -> Off-peak
        ("05:00", "17:00", 25.00),   # base (dominant, 12h)
        ("17:00", "19:00", 35.00),   # pricier   -> Peak
        ("19:00", "21:00", 60.00),   # priciest  -> Super peak
        ("21:00", "24:00", 25.00),   # base again
    ]))
    names = {w["start"]: w["name"] for w in wins}
    assert names == {"00:00": "Super off-peak", "02:00": "Off-peak",
                     "17:00": "Peak", "19:00": "Super peak"}


# ── flat tariff (single rate) -> no windows, base is that rate ─────────────────

def test_flat_single_rate_no_windows():
    base, wins = ds._extract_tou_windows(_slots([
        ("00:00", "12:00", 22.00),
        ("12:00", "24:00", 22.00),
    ]))
    assert round(base, 2) == 22.00
    assert wins == []


# ── window count never exceeds the storage cap ────────────────────────────────

def test_window_cap_enforced():
    # Alternate base/deviation bands to manufacture more deviations than the cap.
    bands, t = [], 0
    for i in range(20):
        rate = 20.00 if i % 2 == 0 else 9.00   # base 20 dominates by count+width
        bands.append((f"{t:02d}:00", f"{t + 1:02d}:00", rate))
        t += 1
    # pad the rest of the day with base so 20.00 is clearly dominant
    bands.append((f"{t:02d}:00", "24:00", 20.00))
    _, wins = ds._extract_tou_windows(_slots(bands))
    assert len(wins) <= ds._MAX_TOU_WINDOWS


# ── multi-day fetch: a recurring daily window must be stored once ──────────────

def test_multiday_fetch_dedupes_recurring_window():
    # Two days of Go-shaped data. The 00:30-05:30 off-peak recurs each day; it
    # must collapse to a single stored window rather than duplicate (the bug the
    # Go UAT fetch exposed — same window emitted twice).
    slots = []
    for day, nxt in (("2026-01-15", "2026-01-16"), ("2026-01-16", "2026-01-17")):
        slots += [
            {"valid_from": f"{day}T00:00", "valid_to": f"{day}T00:30", "value_inc_vat": 31.17},
            {"valid_from": f"{day}T00:30", "valid_to": f"{day}T05:30", "value_inc_vat": 8.625},
            {"valid_from": f"{day}T05:30", "valid_to": f"{nxt}T00:00", "value_inc_vat": 31.17},
        ]
    base, wins = ds._extract_tou_windows(slots, label="import")
    assert round(base, 2) == 31.17
    assert len(wins) == 1
    w = wins[0]
    assert w["name"] == "Off-peak" and w["start"] == "00:30" and w["end"] == "05:30"
    assert abs(w["rate_p"] - 8.625) < 1e-6


# ── logging: extractor emits DEBUG detail only, never INFO ─────────────────────

def test_extract_logs_debug_detail_not_info():
    import logging
    records = []

    class _Capture(logging.Handler):
        def emit(self, r):
            records.append((r.levelno, r.getMessage()))

    handler = _Capture()
    handler.setLevel(logging.DEBUG)
    ds.log.addHandler(handler)
    old_level = ds.log.level
    try:
        ds.log.setLevel(logging.DEBUG)
        ds._extract_tou_windows(_slots([
            ("00:00", "02:00", 25.55),
            ("02:00", "05:00", 15.34),
            ("05:00", "24:00", 25.55),
        ]), label="import")
        debug_msgs = [m for lvl, m in records if lvl == logging.DEBUG]
        info_msgs  = [m for lvl, m in records if lvl == logging.INFO]
        # base decision + the derived window both appear at DEBUG, tagged [import]
        assert any("base=25.55" in m and "[import]" in m for m in debug_msgs)
        assert any("TOU window" in m and "Off-peak" in m for m in debug_msgs)
        # the pure extractor must not emit INFO — that is the one-line fetch summary
        assert info_msgs == []
    finally:
        ds.log.setLevel(old_level)
        ds.log.removeHandler(handler)


def test_extract_silent_when_not_debug():
    import logging
    records = []

    class _Capture(logging.Handler):
        def emit(self, r):
            records.append(r)

    handler = _Capture()
    ds.log.addHandler(handler)
    old_level = ds.log.level
    try:
        ds.log.setLevel(logging.INFO)   # DEBUG suppressed
        ds._extract_tou_windows(_slots([
            ("00:00", "05:00", 10.0), ("05:00", "24:00", 20.0),
        ]), label="import")
        assert records == []            # nothing logged below INFO
    finally:
        ds.log.setLevel(old_level)
        ds.log.removeHandler(handler)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nAll {len(fns)} tariff tests passed.")


if __name__ == "__main__":
    _run()
