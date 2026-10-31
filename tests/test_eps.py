"""Backup power (EPS) control - HR 317.

Only AC-coupled inverters and the All-in-One have the HR 300-359 block that
holds it; DC hybrids time out reading it, so the control must never be offered
or written anywhere else. Read on a GIV-AC3.0 (29 Oct 2026): HR 317 = 1.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

AC = ("single_phase_ac_coupled", "AC Coupled Inverter")
AIO = ("single_phase_extended", "All in One")
HYBRID = ("single_phase_2slot", "Hybrid Inverter Gen 2")


@pytest.fixture(scope="module")
def ds():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="eps-test-"))
    import dashboard_server as mod
    importlib.reload(mod)
    mod.init_db()
    return mod


def _use(ds, monkeypatch, profile_model, eps_raw=1, fail=False):
    profile, model = profile_model
    monkeypatch.setattr(ds, "_inverter_model", model)
    monkeypatch.setattr(ds, "_detect_inverter", lambda: (0x11, profile, model))
    reads = []
    def fake(slave, base, count, timeout=5.0):
        reads.append(base)
        if base == 317:
            if fail:
                raise TimeoutError("no reply")
            return [eps_raw]
        if base == 240:
            return [0] * 60
        if base == 313:
            return [100, 100]
        return [0] * count
    monkeypatch.setattr(ds, "_hr_read", fake)
    writes = []
    monkeypatch.setattr(ds, "_hr_write", lambda slave, reg, val, *a, **k: writes.append((reg, val)))
    return reads, writes


@pytest.mark.parametrize("pm, raw, expect", [(AC, 1, True), (AC, 0, False), (AIO, 1, True)])
def test_eps_state_read_on_supported_models(ds, monkeypatch, pm, raw, expect):
    _use(ds, monkeypatch, pm, eps_raw=raw)
    state = ds._read_control_state()
    assert state["eps_supported"] is True
    assert state["eps_enabled"] is expect


def test_hybrids_never_read_the_ac_config_block(ds, monkeypatch):
    reads, _ = _use(ds, monkeypatch, HYBRID)
    state = ds._read_control_state()
    assert state["eps_supported"] is False and state["eps_enabled"] is None
    assert 317 not in reads


def test_failed_eps_read_shows_unknown_not_an_error(ds, monkeypatch):
    _use(ds, monkeypatch, AC, fail=True)
    state = ds._read_control_state()
    assert state["eps_supported"] is True and state["eps_enabled"] is None


@pytest.mark.parametrize("cmd, val", [("enable_eps", 1), ("disable_eps", 0)])
def test_eps_commands_write_hr317(ds, monkeypatch, cmd, val):
    _, writes = _use(ds, monkeypatch, AC)
    res = ds._execute_control(cmd, {})
    assert res["ok"] is True and "EPS" in res["message"]
    assert writes == [(317, val)]


def test_eps_command_refused_on_hybrid(ds, monkeypatch):
    _, writes = _use(ds, monkeypatch, HYBRID)
    res = ds._execute_control("enable_eps", {})
    assert res["ok"] is False and "only available" in res["message"]
    assert writes == []
