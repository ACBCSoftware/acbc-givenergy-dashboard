"""Battery charge/discharge power limits (HR 111/112 and HR 313/314).

Found in UAT (Oct 2026): setting a charge or discharge limit appeared to do
nothing. Two separate causes, both covered here:

1. Wrong unit. HR 111/112 are a battery C-rate cap in C/100 of the pack's
   capacity (50 = 0.5C), per givenergy-modbus and GivTCP. We treated them as a
   percentage of max battery power and halved it, so on a 19 kWh bank "1 kW"
   wrote 16 (~3 kW) and "3 kW" wrote 50 (~9.5 kW) - the cap never bit.
2. A connection timeout in _hr_write escaped the retry logic, failing a manual
   control on its first attempt ("Control failed set_discharge_limit: timed out").

Andi's system is the worked example throughout: GIV-AC3.0, 19.0 kWh bank,
max_charge_w = max_discharge_w = 3000.
"""
import importlib
import logging
import math
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def ds():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="limits-test-"))
    import dashboard_server as mod
    importlib.reload(mod)
    mod.init_db()         # _execute_control writes control_log
    return mod


@pytest.fixture
def andi(ds, monkeypatch):
    """19.0 kWh detected, 3000 W max both ways, no predictive override."""
    monkeypatch.setattr(ds, "_battery_capacity_kwh", lambda: 19.0)
    monkeypatch.setattr(ds, "PRED_BATTERY_KWH", 0.0)
    monkeypatch.setattr(ds, "MAX_CHARGE_W", 3000)
    monkeypatch.setattr(ds, "MAX_DISCHARGE_W", 3000)
    return ds


DC = "single_phase_ac_coupled"   # any non-extended profile uses HR 111/112
AC = "single_phase_extended"     # Gen3/AIO use HR 313/314


# ── HR 111/112: C-rate in C/100 of capacity ──────────────────────────────────

@pytest.mark.parametrize("pct, watts, expected", [
    (100, 3000, 16),   # full rate rounds UP: 3000/19000 = 15.8% -> 16, never 50
    (33,  1000,  5),   # the UAT case: was written as 16
    (10,   300,  2),   # was written as 5 (~950 W), above the 770 W house load
    (0,      0,  0),
])
def test_watts_become_c_rate_on_a_large_bank(andi, pct, watts, expected):
    assert andi._limit_pct_to_reg("charge", pct, DC) == (111, expected)
    assert andi._limit_pct_to_reg("discharge", pct, DC) == (112, expected)


def test_small_nonzero_request_is_never_off(andi):
    # 1% of 3000 W = 30 W -> 0.16 C/100, which would round to 0 = no charging
    assert andi._limit_pct_to_reg("charge", 1, DC) == (111, 1)


def test_small_pack_full_rate_is_half_c(ds, monkeypatch):
    # Gen1-style 5.2 kWh pack, 2600 W: full rate is exactly 0.5C = 50
    monkeypatch.setattr(ds, "_battery_capacity_kwh", lambda: 5.2)
    monkeypatch.setattr(ds, "MAX_CHARGE_W", 2600)
    assert ds._limit_pct_to_reg("charge", 100, DC) == (111, 50)
    assert ds._limit_pct_to_reg("charge", 50, DC) == (111, 25)


def test_never_exceeds_half_c(ds, monkeypatch):
    # Tiny pack behind a big inverter: 3000 W on 2 kWh would be 150 - capped at 50
    monkeypatch.setattr(ds, "_battery_capacity_kwh", lambda: 2.0)
    monkeypatch.setattr(ds, "MAX_CHARGE_W", 3000)
    assert ds._limit_pct_to_reg("charge", 100, DC) == (111, 50)


def test_predictive_override_used_when_capacity_not_detected(ds, monkeypatch):
    monkeypatch.setattr(ds, "_battery_capacity_kwh", lambda: None)
    monkeypatch.setattr(ds, "PRED_BATTERY_KWH", 19.0)
    monkeypatch.setattr(ds, "MAX_CHARGE_W", 3000)
    assert ds._limit_pct_to_reg("charge", 33, DC) == (111, 5)


def test_unknown_capacity_falls_back_and_says_so(ds, monkeypatch, caplog):
    monkeypatch.setattr(ds, "_battery_capacity_kwh", lambda: None)
    monkeypatch.setattr(ds, "PRED_BATTERY_KWH", 0.0)
    with caplog.at_level(logging.WARNING, logger="dashboard"):
        assert ds._limit_pct_to_reg("charge", 60, DC) == (111, 30)
    assert "Battery capacity unknown" in caplog.text


def _givtcp_battery_limit_percent(rate, batcap, invmaxrate):
    """GivTCP write.py batteryLimitPercent(), verbatim logic (Oct 2026)."""
    percent = rate / batcap * 100
    if rate >= invmaxrate:
        percent = math.ceil(invmaxrate / batcap * 100)
    return min(round(percent), 50)


@pytest.mark.parametrize("watts", range(100, 3001, 100))
def test_matches_givtcp_for_every_100w_step(andi, watts):
    # The control page snaps to 100 W, so this covers every value it can send.
    # Watts reach the helper as a whole percent of max (as the page sends them),
    # so compare at the rate that percent actually represents.
    pct = round(watts / 3000 * 100)
    rate = pct / 100 * 3000
    _, raw = andi._limit_pct_to_reg("charge", pct, DC)
    theirs = _givtcp_battery_limit_percent(rate, 19000, 3000)
    if theirs == 0:
        # Deliberate difference: GivTCP rounds a tiny request (100 W here) down to
        # 0, which means no charging at all; we keep the smallest real cap.
        assert raw == 1
    else:
        assert raw == theirs


def test_read_back_round_trips(andi):
    for pct in (10, 33, 50, 75, 100):
        _, raw = andi._limit_pct_to_reg("charge", pct, DC)
        back = andi._limit_reg_to_pct("charge", raw, DC)
        # C/100 steps are 190 W on this bank (~6% of 3000 W)
        assert abs(back - pct) <= 4, (pct, raw, back)


def test_read_back_of_old_full_value_is_full(andi):
    # Registers written by older versions hold 50; that reads as full, not 300%
    assert andi._limit_reg_to_pct("charge", 50, DC) == 100


# ── HR 313/314: inverter-power percentage, floor 1 ───────────────────────────

def test_ac_pair_is_a_plain_percentage(andi):
    assert andi._limit_pct_to_reg("charge", 33, AC) == (313, 33)
    assert andi._limit_pct_to_reg("discharge", 100, AC) == (314, 100)
    assert andi._limit_reg_to_pct("charge", 33, AC) == 33


def test_ac_pair_never_written_as_zero(andi):
    # 0 ERRORS on AC hardware (givenergy-modbus #306); 1 is the closest to off
    assert andi._limit_pct_to_reg("charge", 0, AC) == (313, 1)


# ── Every write path goes through the helpers ─────────────────────────────────

def test_control_command_writes_converted_value(andi):
    writes = []
    import dashboard_server as mod
    orig = mod._hr_write
    mod._hr_write = lambda slave, reg, val, *a, **k: writes.append((reg, val))
    try:
        msg = andi._do_control(0x32, DC, "set_charge_limit", {"value": 33})
    finally:
        mod._hr_write = orig
    assert writes == [(111, 5)]
    assert "HR 111 = 5" in msg


# ── _hr_write: a connection timeout is retried, not fatal ────────────────────

def _echo_frame(reg, val):
    """Minimal frame _hr_write accepts as the echo for reg/val."""
    f = bytearray(42)
    f[0:4] = b"\x59\x59\x00\x01"
    f[4:6] = (len(f) - 6).to_bytes(2, "big")
    f[7] = 0x02
    f[27] = 0x06
    f[38:40] = reg.to_bytes(2, "big")
    f[40:42] = val.to_bytes(2, "big")
    return bytes(f)


class _EchoSocket:
    def __init__(self, reg, val):
        self._data = [_echo_frame(reg, val)]
    def settimeout(self, t): pass
    def sendall(self, b): pass
    def recv(self, n):
        return self._data.pop(0) if self._data else b""
    def close(self): pass


def test_connect_timeout_is_retried_then_succeeds(ds, monkeypatch):
    calls = []
    def flaky_connect(addr, timeout=None):
        calls.append(addr)
        if len(calls) == 1:
            raise socket.timeout("timed out")
        return _EchoSocket(112, 2)
    monkeypatch.setattr(ds.socket, "create_connection", flaky_connect)
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    ds._hr_write(0x32, 112, 2)          # must not raise
    assert len(calls) == 2


def test_connect_timeout_every_time_gives_up_after_three(ds, monkeypatch):
    calls = []
    def dead(addr, timeout=None):
        calls.append(addr)
        raise socket.timeout("timed out")
    monkeypatch.setattr(ds.socket, "create_connection", dead)
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError):
        ds._hr_write(0x32, 112, 2)
    assert len(calls) == 3


def test_timeout_reaches_the_page_as_plain_english(ds, monkeypatch):
    monkeypatch.setattr(ds, "_detect_inverter", lambda: (0x32, DC, "AC Coupled Inverter"))
    def boom(*a, **k):
        raise TimeoutError("HR write timeout: slave=0x32 reg=112 val=2")
    monkeypatch.setattr(ds, "_do_control", boom)
    res = ds._execute_control("set_discharge_limit", {"value": 10})
    assert res["ok"] is False
    assert "Couldn't reach the inverter" in res["message"]
    assert "may not have been applied" in res["message"]


# ── Control page: AC-coupled also reports the inverter-side limits ───────────
# Confirmed on a GIV-AC3.0 (29 Oct 2026): HR 313/314 cap forced charge and
# discharge alongside HR 111/112 and the lower wins, so the page must show them.

def _fake_reads(hr313=(100, 100), fail313=False):
    regs = [0] * 120
    regs[111] = regs[112] = 16                      # what GE/Octopus wrote for 3 kW
    def fake(slave, base, count, timeout=5.0):
        if base == 313:
            if fail313:
                raise TimeoutError("no reply")
            return list(hr313)
        if base == 240:
            return [0] * 60
        return regs[base:base + count]
    return fake


@pytest.mark.parametrize("hr313, expect", [((100, 100), (100, 100)), ((33, 100), (33, 100))])
def test_ac_coupled_control_state_reports_inverter_limits(andi, monkeypatch, hr313, expect):
    monkeypatch.setattr(andi, "_hr_read", _fake_reads(hr313))
    monkeypatch.setattr(andi, "_detect_inverter", lambda: (0x11, DC, "AC Coupled Inverter"))
    state = andi._read_control_state()
    assert (state["inverter_charge_limit_pct"], state["inverter_discharge_limit_pct"]) == expect
    assert state["charge_limit"] == 100            # 16 on a 19 kWh / 3 kW system = full


def test_inverter_limit_read_failure_does_not_break_the_page(andi, monkeypatch):
    monkeypatch.setattr(andi, "_hr_read", _fake_reads(fail313=True))
    monkeypatch.setattr(andi, "_detect_inverter", lambda: (0x11, DC, "AC Coupled Inverter"))
    state = andi._read_control_state()
    assert state["inverter_charge_limit_pct"] is None
    assert state["charge_limit"] == 100


def test_other_profiles_do_not_report_inverter_limits(andi, monkeypatch):
    monkeypatch.setattr(andi, "_hr_read", _fake_reads())
    monkeypatch.setattr(andi, "_detect_inverter", lambda: (0x11, "single_phase_2slot", "Hybrid Inverter Gen 2"))
    state = andi._read_control_state()
    assert state["inverter_charge_limit_pct"] is None
