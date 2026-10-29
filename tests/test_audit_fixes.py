"""Register audit against givenergy-modbus (Oct 2026) - the four clear-cut fixes.

1. EMS controllers (DTC 0x5x) were treated as Gen1/2 hybrids, so control writes
   went to inverter registers an EMS ignores. Now read-only on every path.
2. Battery and inverter temperatures (IR 56 / IR 41) are signed; read unsigned,
   -1.0 C showed as 6552.6 C.
3. All-in-One capacity used 51.2 V instead of its ~307 V HV stack - 6x too small.
4. Hybrid Gen1 keeps today's battery energy in IR 182/183, not IR 36/37.
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
def ds():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="audit-test-"))
    import dashboard_server as mod
    importlib.reload(mod)
    mod.init_db()
    return mod


@pytest.fixture(autouse=True)
def reset(ds, monkeypatch):
    monkeypatch.setattr(ds, "_inverter_model", "AC Coupled Inverter")
    ds._gen1_batt_today.update(ts=0.0, chg=None, dis=None)
    ds._last_gen1_poke = 0.0
    yield


def _page(**regs):
    """g() for an IR 0-59 page; everything else 0."""
    vals = [0] * 60
    for k, v in regs.items():
        vals[int(k[1:])] = v
    return lambda n: vals[n]


# ── 1. EMS is read-only ───────────────────────────────────────────────────────

@pytest.mark.parametrize("model", ["Energy Management System", "EMS Commercial"])
def test_ems_control_is_refused_before_any_write(ds, monkeypatch, model):
    monkeypatch.setattr(ds, "_inverter_model", model)
    monkeypatch.setattr(ds, "_detect_inverter", lambda: (0x11, "single_phase_2slot", model))
    writes = []
    monkeypatch.setattr(ds, "_hr_write", lambda *a, **k: writes.append(a))
    for cmd, params in (("enable_charge", {}), ("sync_time", {}), ("set_charge_limit", {"value": 50})):
        res = ds._execute_control(cmd, params)
        assert res["ok"] is False and "EMS" in res["message"]
    assert writes == []


def test_ems_scheduler_and_quick_actions_refuse(ds, monkeypatch, caplog):
    model = "Energy Management System"
    monkeypatch.setattr(ds, "_detect_inverter", lambda: (0x11, "single_phase_2slot", model))
    writes = []
    monkeypatch.setattr(ds, "_hr_write", lambda *a, **k: writes.append(a))
    # The scheduler logs its failure and retries later rather than raising
    ds._sched_apply({"mode": "charge", "start": "01:00", "end": "02:00"})
    assert "not supported on this inverter" in caplog.text
    with pytest.raises(RuntimeError):
        ds._quick_action_do("charge")
    assert writes == []


def test_other_models_are_not_blocked(ds):
    for model in ("AC Coupled Inverter", "Hybrid Inverter Gen 1", "All in One", ""):
        assert ds._writes_blocked_reason(model) is None


# ── 2. Signed temperatures ────────────────────────────────────────────────────

def test_negative_temperatures_decode_as_negative(ds):
    d = ds._build_from_input_page(_page(r56=65526, r41=65506))   # -10 and -30 raw
    assert d["t_battery"] == -1.0
    assert d["t_heatsink"] == -3.0


def test_positive_temperatures_unchanged(ds):
    d = ds._build_from_input_page(_page(r56=190, r41=348))
    assert (d["t_battery"], d["t_heatsink"]) == (19.0, 34.8)


# ── 3. All-in-One battery voltage ─────────────────────────────────────────────

def test_aio_capacity_uses_its_hv_stack(ds, monkeypatch):
    monkeypatch.setattr(ds, "_battery_capacity_ah", lambda: 44)
    monkeypatch.setattr(ds, "_inverter_model", "All in One")
    assert ds._battery_capacity_kwh() == 13.5          # 44 Ah x 307 V
    monkeypatch.setattr(ds, "_inverter_model", "AC Coupled Inverter")
    assert ds._battery_capacity_kwh() == 2.3           # LV modules unchanged


def test_andis_bank_unchanged(ds, monkeypatch):
    monkeypatch.setattr(ds, "_battery_capacity_ah", lambda: 372)
    assert ds._battery_capacity_kwh() == 19.0


# ── 4. Gen1 battery energy today ──────────────────────────────────────────────

def _frame(base, regs):
    f = bytearray(42 + 2 * len(regs))
    f[0:4] = b"\x59\x59\x00\x01"
    f[4:6] = (len(f) - 6).to_bytes(2, "big")
    f[7], f[27] = 0x02, 0x04
    f[38:40] = base.to_bytes(2, "big")
    f[40:42] = len(regs).to_bytes(2, "big")
    for i, v in enumerate(regs):
        f[42 + 2 * i: 44 + 2 * i] = v.to_bytes(2, "big")
    return bytes(f)


def test_ir180_page_caches_gen1_battery_energy(ds):
    regs = [0] * 60
    regs[2], regs[3] = 43, 61        # IR182 discharge 4.3, IR183 charge 6.1 kWh
    assert ds._decode_listen_frame(_frame(180, regs)) is None
    assert (ds._gen1_batt_today["dis"], ds._gen1_batt_today["chg"]) == (4.3, 6.1)


def test_gen1_uses_ir182_183_when_fresh(ds, monkeypatch):
    monkeypatch.setattr(ds, "_inverter_model", "Hybrid Inverter Gen 1")
    ds._gen1_batt_today.update(ts=time.time(), chg=6.1, dis=4.3)
    d = ds._build_from_input_page(_page(r36=0, r37=0))
    assert (d["bat_chg_today"], d["bat_dis_today"]) == (6.1, 4.3)


def test_gen1_falls_back_when_stale_or_missing(ds, monkeypatch):
    monkeypatch.setattr(ds, "_inverter_model", "Hybrid Inverter Gen 1")
    d = ds._build_from_input_page(_page(r36=12, r37=34))
    assert (d["bat_chg_today"], d["bat_dis_today"]) == (1.2, 3.4)
    ds._gen1_batt_today.update(ts=time.time() - 3600, chg=6.1, dis=4.3)
    d = ds._build_from_input_page(_page(r36=12, r37=34))
    assert (d["bat_chg_today"], d["bat_dis_today"]) == (1.2, 3.4)


def test_other_models_keep_ir36_37(ds):
    ds._gen1_batt_today.update(ts=time.time(), chg=6.1, dis=4.3)
    d = ds._build_from_input_page(_page(r36=12, r37=34))
    assert (d["bat_chg_today"], d["bat_dis_today"]) == (1.2, 3.4)


class _Sock:
    def __init__(self): self.sent = []
    def sendall(self, b): self.sent.append(b)


def test_ir180_requested_only_on_gen1_and_sparingly(ds, monkeypatch):
    slave = next(iter(ds._POKE_BY_SLAVE))
    monkeypatch.setattr(ds, "_inverter_slave", slave)
    monkeypatch.setattr(ds, "_inverter_profile", "single_phase_2slot")
    page180 = ds._make_poke(slave, 0x04, 180, 60)

    monkeypatch.setattr(ds, "_inverter_model", "Hybrid Inverter Gen 2")
    s = _Sock(); ds._send_pokes(s)
    assert page180 not in s.sent

    monkeypatch.setattr(ds, "_inverter_model", "Hybrid Inverter Gen 1")
    s = _Sock(); ds._send_pokes(s); ds._send_pokes(s)
    assert s.sent.count(page180) == 1            # not again within 5 minutes
