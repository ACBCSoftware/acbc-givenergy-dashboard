r"""Unit tests for the protocol/detection pure functions (no inverter/network).

Covers the highest-regression-value targets identified after v2.3:
  - _crc16 / _bms_crc16          (the two CRC conventions, vs proven wire values)
  - _make_poke                    (frame layout, vs the hardcoded proven pokes)
  - _classify_model               (DTC -> profile/model, incl. all tester hardware)
  - _smooth                       (zero-blip debounce + SOC spike filter, incl.
                                   the v2.3 Gateway-AIO SOC seeding bug, issue #23)
  - slot register maps            (counts, patterns, no overlaps)
  - HHMM codecs                   (_bcd_to_hhmm / _hhmm_to_bcd / _hhmm_to_min)
  - ConfigParser construction     (inline comments stripped everywhere, issue #24)

Run:  venv\Scripts\python.exe tests\test_core.py
   or: venv\Scripts\python.exe -m pytest tests\test_core.py
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard_server as ds  # noqa: E402


# ── _crc16 (LSB-first, slave-inclusive — Gen3/AIO + all HR reads/writes) ───────

def test_crc16_gen3_poke_value():
    # Proven on the wire: IR(0,60) @ slave 0x11 -> CRC f2 8b
    inner = bytes([0x11, 0x04]) + (0).to_bytes(2, "big") + (60).to_bytes(2, "big")
    assert ds._crc16(inner) == bytes.fromhex("f28b")

def test_crc16_matches_hardcoded_gen3_poke_frame():
    # The hardcoded proven Gen3 poke must end with the CRC of its own inner block.
    poke  = ds._POKE_BY_SLAVE[0x11]
    inner = poke[26:32]            # slave + func + base(2) + count(2)
    assert poke[32:34] == ds._crc16(inner)

def test_crc16_hr_read_frame():
    # HR(0,60) @ 0x11 (the detection read) — func 0x03 changes the CRC
    inner = bytes([0x11, 0x03]) + (0).to_bytes(2, "big") + (60).to_bytes(2, "big")
    crc = ds._crc16(inner)
    assert len(crc) == 2
    assert crc != ds._crc16(bytes([0x11, 0x04]) + b"\x00\x00\x00\x3c")


# ── _bms_crc16 (MSB-first, NO slave byte — Gen2 poke + LV battery reads) ───────

def test_bms_crc16_gen2_poke_value():
    # Proven on the wire: IR(0,60), no slave byte -> CRC d1 d5
    assert ds._bms_crc16(0x04, 0, 60) == bytes.fromhex("d1d5")

def test_bms_crc16_matches_hardcoded_gen2_poke_frame():
    poke = ds._POKE_BY_SLAVE[0x32]
    assert poke[32:34] == ds._bms_crc16(0x04, 0, 60)

def test_bms_crc16_battery_module_read():
    # IR(60,60) — the LV battery module page read. Just structural sanity:
    # 2 bytes, different from the base-0 CRC.
    crc = ds._bms_crc16(0x04, 60, 60)
    assert len(crc) == 2
    assert crc != ds._bms_crc16(0x04, 0, 60)


# ── _make_poke frame layout ────────────────────────────────────────────────────

def test_make_poke_reproduces_proven_gen3_poke():
    # _make_poke with the Gen3 parameters must reproduce the hardcoded proven
    # frame byte-for-byte (same serial, padding, CRC convention).
    assert ds._make_poke(0x11, 0x04, 0, 60) == ds._POKE_BY_SLAVE[0x11]

def test_gateway_pokes_layout():
    # Frame layout: [0:4] 5959 0001, [4:6] len, [6] uid, [7] func=02,
    # [8:18] serial, [18:26] padding (last byte 0x08 is significant),
    # [26] slave, [27] inner func, [28:30] base, [30:32] count, [32:34] CRC.
    for poke, base in ((ds._GATEWAY_POKE_1600, 1600), (ds._GATEWAY_POKE_1780, 1780)):
        assert poke[:4]   == b"\x59\x59\x00\x01"
        assert len(poke)  == 6 + int.from_bytes(poke[4:6], "big")
        assert poke[7]    == 0x02                      # transparent
        assert poke[25]   == 0x08                      # padding terminator
        assert poke[26]   == 0x11                      # gateway answers on 0x11
        assert poke[27]   == 0x04                      # read input registers
        assert int.from_bytes(poke[28:30], "big") == base
        assert int.from_bytes(poke[30:32], "big") == 60
        assert poke[32:34] == ds._crc16(poke[26:32])   # slave-inclusive CRC


# ── _classify_model ────────────────────────────────────────────────────────────
# Tester hardware first — these exact classifications must never break.

def test_classify_ac_coupled_giv_ac30():
    # AC-coupled tester unit (GIV-AC3.0, DTC 0x3001)
    assert ds._classify_model(0x3001, 212) == \
        ("single_phase_ac_coupled", "AC Coupled Inverter")

def test_classify_gen3_hybrid_tester():
    # Gen3 hybrid tester unit (DTC 0x2003, ARM fw 318 > 302 -> extended)
    assert ds._classify_model(0x2003, 318) == \
        ("single_phase_extended", "Hybrid Inverter Gen 3")

def test_classify_gateway_tester():
    # Gateway AIO tester unit (DTC 0x7001)
    assert ds._classify_model(0x7001, 0) == ("gateway_aio", "Gateway")

def test_classify_aio2_tester():
    # All in One 2 (DTC 0x8201): single-phase 10-slot extended, same family as 0x80xx
    assert ds._classify_model(0x8201, 0) == ("single_phase_extended", "All in One 2")

def test_classify_hybrid_fw_century_rule():
    # 0x20xx family disambiguates on ARM firmware century
    assert ds._classify_model(0x2001, 302) == \
        ("single_phase_2slot", "Hybrid Inverter Gen 3")     # cent 3, <=302
    assert ds._classify_model(0x2001, 303) == \
        ("single_phase_extended", "Hybrid Inverter Gen 3")  # cent 3, >302
    assert ds._classify_model(0x2001, 845) == \
        ("single_phase_2slot", "Hybrid Inverter Gen 2")     # cent 8
    assert ds._classify_model(0x2001, 910) == \
        ("single_phase_2slot", "Hybrid Inverter Gen 2")     # cent 9
    assert ds._classify_model(0x2001, 119) == \
        ("single_phase_2slot", "Hybrid Inverter Gen 1")     # other century

def test_classify_two_digit_overrides_beat_hybrid_family():
    # 21xx/23xx must hit the two-digit map, NOT fall into the 0x2 hybrid branch
    assert ds._classify_model(0x2101, 318) == ("single_phase_2slot", "Polar")
    assert ds._classify_model(0x2301, 318) == ("unknown", "String Inverter Gen 3")

def test_classify_remaining_two_digit_prefixes():
    assert ds._classify_model(0x4101, 0) == ("three_phase_aio", "AIO Commercial")
    assert ds._classify_model(0x5101, 0) == ("single_phase_2slot", "EMS Commercial")
    assert ds._classify_model(0x8101, 0) == \
        ("single_phase_extended", "Hybrid Inverter Gen 3 HV")

def test_classify_one_digit_families():
    assert ds._classify_model(0x4001, 0) == \
        ("three_phase_aio", "Three-Phase Hybrid Inverter")
    assert ds._classify_model(0x5001, 0) == \
        ("single_phase_2slot", "Energy Management System")
    assert ds._classify_model(0x6001, 0) == \
        ("three_phase_aio", "Three-Phase AC Inverter")
    # All in One (0x8001, Gen1): single-phase 10-slot extended, confirmed on hardware
    # (issue #21). NOT three_phase_aio — HR 1100+ does not exist on it.
    assert ds._classify_model(0x8001, 0) == ("single_phase_extended", "All in One")

def test_classify_unknown_dtc():
    profile, model = ds._classify_model(0x9999, 0)
    assert profile == "unknown"
    assert model == "Unknown (DTC 0x9999)"
    profile, model = ds._classify_model(0x0001, 0)
    assert profile == "unknown"

def test_classify_no_gen4_anywhere():
    # There is no "Gen4" product. 0x83xx identity is unconfirmed and must be
    # labelled as such — never relabel it Gen4 (the old wrong label).
    for dtc in (0x8300, 0x8304, 0x83FF):
        profile, model = ds._classify_model(dtc, 318)
        assert profile == "single_phase_extended"
        assert "Unknown" in model
        assert "Gen4" not in model and "Gen 4" not in model

def test_classify_aio_family_all_extended():
    # Every All-in-One DTC (0x80xx/0x81xx/0x82xx/0x83xx) is single-phase 10-slot
    # extended, NOT three_phase_aio. Confirmed on AIO 0x8001 hardware (issue #21);
    # corroborated by the psylsph simulator and home-energy-manager. HR 1100+ (the
    # three-phase slot block) does not exist on these units.
    for dtc in (0x8001, 0x8002, 0x8003, 0x8102, 0x8103, 0x8201, 0x8304):
        profile, _ = ds._classify_model(dtc, 318)
        assert profile == "single_phase_extended", f"{dtc:#06x} should be extended"
    # True three-phase inverters stay three_phase_aio
    assert ds._classify_model(0x4001, 0)[0] == "three_phase_aio"
    assert ds._classify_model(0x6001, 0)[0] == "three_phase_aio"

def test_charge_slot_hrs_per_profile():
    # Only the extended profile uses the contiguous charge slot 2 (243/244) and reads
    # the 240-299 block; everything else uses the legacy 31/32 (and never reads beyond
    # HR 119). Routing gateway_aio to 31/32 avoids an out-of-range read on the AIO map.
    assert ds._charge_slot_hrs("single_phase_extended")[1] == (243, 244)
    assert ds._charge_slot_hrs("single_phase_2slot")[1]    == (31, 32)
    assert ds._charge_slot_hrs("gateway_aio")[1]           == (31, 32)
    # Slot 1 is HR 94/95 on every profile
    for p in ("single_phase_extended", "single_phase_2slot",
              "single_phase_ac_coupled", "gateway_aio"):
        assert ds._charge_slot_hrs(p)[0] == (94, 95)


# ── _smooth: zero-blip debounce ────────────────────────────────────────────────

def _reset_smooth():
    ds._zero_streak.clear()
    ds._last_good.clear()
    ds._last_soc = None

def test_smooth_home_zero_blip_held():
    _reset_smooth()
    assert ds._smooth({"home_w": 500})["home_w"] == 500
    # Zeros are held at the last good value until the streak hits 12 polls
    for _ in range(11):
        assert ds._smooth({"home_w": 0})["home_w"] == 500
    assert ds._smooth({"home_w": 0})["home_w"] == 0     # 12th poll: genuine zero

def test_smooth_streak_resets_on_good_value():
    _reset_smooth()
    ds._smooth({"home_w": 500})
    for _ in range(11):
        ds._smooth({"home_w": 0})
    ds._smooth({"home_w": 480})                          # streak resets
    assert ds._smooth({"home_w": 0})["home_w"] == 480    # held again

def test_smooth_solar_shorter_debounce():
    _reset_smooth()
    ds._smooth({"solar_w": 900})
    assert ds._smooth({"solar_w": 0})["solar_w"] == 900  # streak 1
    assert ds._smooth({"solar_w": 0})["solar_w"] == 900  # streak 2
    assert ds._smooth({"solar_w": 0})["solar_w"] == 0    # streak 3 = threshold

def test_smooth_zero_with_no_history_passes_through():
    _reset_smooth()
    # No last-good value yet — nothing to hold, zero must pass through
    assert ds._smooth({"home_w": 0})["home_w"] == 0


# ── _smooth: SOC spike filter ──────────────────────────────────────────────────

def test_soc_spike_suppressed():
    _reset_smooth()
    ds._smooth({"soc": 50})
    assert ds._smooth({"soc": 90})["soc"] == 50          # +40 in one poll: corrupt
    assert ds._smooth({"soc": 54})["soc"] == 54          # +4 from held value: fine

def test_soc_gradual_change_passes():
    _reset_smooth()
    ds._smooth({"soc": 50})
    for soc in (53, 56, 59, 62):
        assert ds._smooth({"soc": soc})["soc"] == soc

def test_soc_filter_not_seeded_by_startup_zero():
    # v2.3 regression (issue #23): Gateway AIO starts with soc=0 until the
    # base=1780 frame arrives. The filter must NOT treat 0 as a baseline and
    # suppress the first real reading.
    _reset_smooth()
    ds._smooth({"soc": 0})
    assert ds._smooth({"soc": 98})["soc"] == 98

def test_soc_missing_key_untouched():
    _reset_smooth()
    out = ds._smooth({"home_w": 400})
    assert "soc" not in out


# ── Capacity-weighted SOC fallback (v2.4) ──────────────────────────────────────

def test_capacity_weighted_soc_single_module():
    assert ds._capacity_weighted_soc(
        [{"cap_remaining": 5000, "cap_design": 10000}]) == 50

def test_capacity_weighted_soc_is_capacity_weighted():
    # 90% of a small pack + 11% of a big pack must weight by capacity:
    # (900+1000)/(1000+9000) = 19%, NOT the 50% a naive average would give.
    modules = [{"cap_remaining": 900,  "cap_design": 1000},
               {"cap_remaining": 1000, "cap_design": 9000}]
    assert ds._capacity_weighted_soc(modules) == 19

def test_capacity_weighted_soc_skips_blank_modules():
    modules = [{"cap_remaining": 0,    "cap_design": 0},      # blank module
               {"cap_remaining": None, "cap_design": 8000},   # missing field
               {"cap_remaining": 4000, "cap_design": 8000}]
    assert ds._capacity_weighted_soc(modules) == 50

def test_capacity_weighted_soc_no_usable_modules():
    assert ds._capacity_weighted_soc([]) is None
    assert ds._capacity_weighted_soc([{"cap_remaining": 5, "cap_design": 0}]) is None

def test_capacity_weighted_soc_clamped():
    # Remaining > design (drifted BMS calibration) must clamp to 100
    assert ds._capacity_weighted_soc(
        [{"cap_remaining": 11000, "cap_design": 10000}]) == 100

def test_smooth_soc_zero_uses_bms_fallback():
    _reset_smooth()
    orig = ds._bms_soc_fallback_value
    ds._bms_soc_fallback_value = lambda: 42
    try:
        out = ds._smooth({"soc": 0})
        assert out["soc"] == 42
        assert out["soc_source"] == "bms"
        # The estimate must NOT seed the spike filter — a real IR59 reading
        # is accepted the moment it appears, however far from the estimate.
        assert ds._smooth({"soc": 98})["soc"] == 98
    finally:
        ds._bms_soc_fallback_value = orig

def test_smooth_soc_zero_without_fallback_passes_through():
    _reset_smooth()
    orig = ds._bms_soc_fallback_value
    ds._bms_soc_fallback_value = lambda: None
    try:
        out = ds._smooth({"soc": 0})
        assert out["soc"] == 0
        assert "soc_source" not in out
    finally:
        ds._bms_soc_fallback_value = orig

def test_smooth_soc_fallback_not_used_once_soc_known():
    _reset_smooth()
    orig = ds._bms_soc_fallback_value
    ds._bms_soc_fallback_value = lambda: 42
    try:
        ds._smooth({"soc": 50})
        # A later corrupt 0 is held at the last good value by the spike
        # filter — the BMS estimate must not override a known-good SOC.
        assert ds._smooth({"soc": 0})["soc"] == 50
    finally:
        ds._bms_soc_fallback_value = orig

def test_bms_fallback_gated_by_profile():
    # With no detected profile (the state in tests), the fallback must bail
    # out before touching the cache or any socket.
    ds._bms_soc_cache["ts"] = 0.0
    ds._bms_soc_cache["soc"] = None
    assert ds._bms_soc_fallback_value() is None
    assert ds._bms_soc_cache["ts"] == 0.0      # untouched — bailed at the gate


# ── Slot register maps ─────────────────────────────────────────────────────────

def test_slot_map_lengths():
    assert len(ds._CHARGE_SLOT_HR)        == 10
    assert len(ds._DISCHARGE_SLOT_HR)     == 10
    assert len(ds._CHARGE_SOC_HR)         == 10
    assert len(ds._DISCHARGE_SOC_HR)      == 10
    assert len(ds._CHARGE_SLOT_HR_3PH)    == 2
    assert len(ds._DISCHARGE_SLOT_HR_3PH) == 2

def test_slot_map_known_registers():
    # Extended (Gen3/HV/AIO) charge slot 2 is the contiguous 243/244, confirmed on the
    # AIO 0x8001 hardware 11 Jun 2026. 2-slot / gateway profiles keep the legacy 31/32.
    assert ds._CHARGE_SLOT_HR[0]      == (94, 95)
    assert ds._CHARGE_SLOT_HR[1]      == (243, 244)
    assert ds._CHARGE_SLOT_HR_2SLOT   == [(94, 95), (31, 32)]
    assert ds._DISCHARGE_SLOT_HR[0]   == (56, 57)
    assert ds._DISCHARGE_SLOT_HR[1]   == (44, 45)
    assert ds._CHARGE_SLOT_HR_3PH     == [(1113, 1114), (1115, 1116)]
    assert ds._DISCHARGE_SLOT_HR_3PH  == [(1118, 1119), (1120, 1121)]
    assert ds._HR_3PH_CHARGE_TARGET   == 1111

def test_extended_slot_patterns():
    # Extended slots 3-10: charge from 246, discharge from 276, stride 3
    for i in range(2, 10):
        start = 246 + (i - 2) * 3
        assert ds._CHARGE_SLOT_HR[i] == (start, start + 1)
        start = 276 + (i - 2) * 3
        assert ds._DISCHARGE_SLOT_HR[i] == (start, start + 1)
    # Per-slot SOC targets: charge base 242, discharge base 272, stride 3
    assert ds._CHARGE_SOC_HR    == [242 + n * 3 for n in range(10)]
    assert ds._DISCHARGE_SOC_HR == [272 + n * 3 for n in range(10)]

def test_no_register_overlaps():
    regs = []
    for a, b in ds._CHARGE_SLOT_HR + ds._DISCHARGE_SLOT_HR:
        regs += [a, b]
    regs += ds._CHARGE_SOC_HR + ds._DISCHARGE_SOC_HR
    for a, b in ds._CHARGE_SLOT_HR_3PH + ds._DISCHARGE_SLOT_HR_3PH:
        regs += [a, b]
    assert len(regs) == len(set(regs)), "slot register maps overlap"

def test_slot_counts_per_profile():
    # AC-coupled: only 1 usable charge slot (HR 31/32 not writable on GIV-AC3.0,
    # confirmed on hardware 07 Jun 2026) but 2 discharge slots.
    assert ds._CHARGE_SLOT_COUNT["single_phase_ac_coupled"]    == 1
    assert ds._DISCHARGE_SLOT_COUNT["single_phase_ac_coupled"] == 2
    assert ds._CHARGE_SLOT_COUNT["single_phase_extended"]      == 10
    assert ds._DISCHARGE_SLOT_COUNT["single_phase_extended"]   == 10
    # Every count must be servable by the register maps
    for profile, n in ds._CHARGE_SLOT_COUNT.items():
        cap = 2 if profile == "three_phase_aio" else 10
        assert n <= cap, profile
    # Every schedulable profile needs entries in both count maps
    for profile in ds._SCHED_PROFILES:
        assert profile in ds._CHARGE_SLOT_COUNT
        assert profile in ds._DISCHARGE_SLOT_COUNT


# ── HHMM codecs ────────────────────────────────────────────────────────────────

def test_bcd_to_hhmm():
    assert ds._bcd_to_hhmm(430)  == "04:30"
    assert ds._bcd_to_hhmm(0)    == "00:00"
    assert ds._bcd_to_hhmm(1)    == "00:01"
    assert ds._bcd_to_hhmm(2359) == "23:59"
    assert ds._bcd_to_hhmm(1600) == "16:00"

def test_hhmm_to_bcd():
    assert ds._hhmm_to_bcd("04:30") == 430
    assert ds._hhmm_to_bcd("00:00") == 0
    assert ds._hhmm_to_bcd("23:59") == 2359

def test_hhmm_roundtrip():
    for v in (0, 1, 30, 430, 1245, 1600, 2359):
        assert ds._hhmm_to_bcd(ds._bcd_to_hhmm(v)) == v

def test_hhmm_to_min():
    assert ds._hhmm_to_min("00:00") == 0
    assert ds._hhmm_to_min("04:30") == 270
    assert ds._hhmm_to_min("23:59") == 1439


# ── ConfigParser construction (issue #24 regression) ──────────────────────────

def test_all_configparsers_strip_inline_comments():
    # v2.3 shipped a startup crash because ConfigParser doesn't strip inline
    # ';' comments by default. Every ConfigParser in the codebase must pass
    # inline_comment_prefixes so a commented config.ini can never crash a
    # read path (and save paths don't round-trip comment text into values).
    src   = Path(ds.__file__).read_text(encoding="utf-8")
    calls = re.findall(r"ConfigParser\([^)]*\)", src)
    assert calls, "expected ConfigParser usages in dashboard_server.py"
    for call in calls:
        assert "inline_comment_prefixes" in call, f"missing inline_comment_prefixes: {call}"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nAll {len(fns)} core tests passed.")


if __name__ == "__main__":
    _run()
