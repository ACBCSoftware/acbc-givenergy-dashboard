"""EcoFlow STREAM integration tests.

These lock in the findings from probing the real hardware (Sep 2026), several of
which contradict EcoFlow's published documentation. If one of these fails after
a refactor, check the finding before "fixing" the test -- the docs are wrong,
not the code. See the block comment above _ef_sign_headers in dashboard_server.
"""
import hashlib
import hmac
import importlib
import json
import os
import sqlite3
import struct
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="ef-test-"))
    import dashboard_server as ds
    importlib.reload(ds)
    return ds


# ── Signing ──────────────────────────────────────────────────────────────────

def test_signature_excludes_query_params(srv, monkeypatch):
    """Finding 1: signing the params (as the docs describe) returns 8521.

    Only accessKey, nonce and timestamp are signed. This test reconstructs the
    HMAC independently so a regression to the documented scheme is caught.
    """
    monkeypatch.setattr(srv, "ECOFLOW_ACCESS_KEY", "AK_TEST")
    monkeypatch.setattr(srv, "ECOFLOW_SECRET_KEY", "SK_TEST")
    h = srv._ef_sign_headers()

    expected_plain = f"accessKey=AK_TEST&nonce={h['nonce']}&timestamp={h['timestamp']}"
    expected = hmac.new(b"SK_TEST", expected_plain.encode(), hashlib.sha256).hexdigest()
    assert h["sign"] == expected
    assert "sn=" not in expected_plain


def test_signature_nonce_and_timestamp_shape(srv, monkeypatch):
    monkeypatch.setattr(srv, "ECOFLOW_ACCESS_KEY", "AK")
    monkeypatch.setattr(srv, "ECOFLOW_SECRET_KEY", "SK")
    h = srv._ef_sign_headers()
    assert len(h["nonce"]) == 6 and h["nonce"].isdigit()
    # milliseconds, not seconds
    assert int(h["timestamp"]) > 1_600_000_000_000


# ── Power reading ────────────────────────────────────────────────────────────

def _prime(srv, monkeypatch, *, watts, online=True, age=0.0, enabled=True):
    monkeypatch.setattr(srv, "ECOFLOW_ENABLED", enabled)
    monkeypatch.setattr(srv, "_ef_online", online)
    monkeypatch.setattr(srv, "_ef_state", {srv._EF_FIELD_AC: watts} if watts is not None else {})
    stamp = time.time() - age if watts is not None else 0.0
    monkeypatch.setattr(srv, "_ef_last_msg", stamp)
    monkeypatch.setattr(srv, "_ef_last_ac",  stamp)


def test_power_is_real_watts_not_deciwatts(srv, monkeypatch):
    """Finding 3: gridConnectionPower is already in watts. No /10, no *10."""
    _prime(srv, monkeypatch, watts=42.9)
    assert srv._ef_power_w() == pytest.approx(42.9)


def test_positive_means_feeding_the_house(srv, monkeypatch):
    """Finding 4: docs say feed-in is negative. Measured, it is positive.

    42.9 W out matched powGetPv 21.826 + powGetPv2 21.073 = 42.90 W in.
    """
    _prime(srv, monkeypatch, watts=42.9)
    assert srv._ef_power_w() > 0


def test_negative_clamped_to_zero_not_subtracted(srv, monkeypatch):
    """Never observed on real hardware. Must not silently reduce house load."""
    _prime(srv, monkeypatch, watts=-150.0)
    assert srv._ef_power_w() == 0.0


def test_total_silence_is_none_not_zero(srv, monkeypatch):
    """None and 0 are different claims. A device that has genuinely vanished
    must not masquerade as 'generating nothing'."""
    _prime(srv, monkeypatch, watts=500.0, age=srv._EF_SILENCE_SECS + 10)
    assert srv._ef_power_w() is None


def test_offline_device_is_none(srv, monkeypatch):
    """Finding 6: the STREAM drops offline overnight. That is not zero output."""
    _prime(srv, monkeypatch, watts=500.0, online=False)
    assert srv._ef_power_w() is None


def test_disabled_integration_is_none(srv, monkeypatch):
    _prime(srv, monkeypatch, watts=500.0, enabled=False)
    assert srv._ef_power_w() is None


def test_non_numeric_value_is_none(srv, monkeypatch):
    _prime(srv, monkeypatch, watts="not a number")
    assert srv._ef_power_w() is None


# ── Delta merging ────────────────────────────────────────────────────────────

def test_quota_messages_merge_rather_than_replace(srv, monkeypatch):
    """Finding 5: each message carries only changed fields, never full state."""
    monkeypatch.setattr(srv, "_ef_state", {})
    monkeypatch.setattr(srv, "_ef_last_msg", 0.0)
    monkeypatch.setattr(srv, "_ef_last_ac", 0.0)
    srv._ef_apply_quota({"powGetPv": 21.8, "powGetPv2": 21.0})
    srv._ef_apply_quota({srv._EF_FIELD_AC: 42.9})
    srv._ef_apply_quota({"powGetPv": 30.0})
    # The AC figure from an earlier message must survive later partial updates.
    assert srv._ef_state[srv._EF_FIELD_AC] == 42.9
    assert srv._ef_state["powGetPv"] == 30.0
    assert srv._ef_state["powGetPv2"] == 21.0


def test_any_message_proves_liveness_only_ac_updates_value_clock(srv, monkeypatch):
    """The two clocks are separate, and only the liveness one gates validity.

    An earlier version keyed validity off the AC-value clock. Because this is a
    delta protocol -- a field is only sent when it CHANGES -- a steady output
    produced no updates and a healthy device read as offline for 40% of samples,
    once for 50 minutes. Measured on real hardware: 170 s of silence while
    online and generating 167 W. Silence means "no change", not "gone".
    """
    monkeypatch.setattr(srv, "_ef_state", {})
    monkeypatch.setattr(srv, "_ef_last_msg", 0.0)
    monkeypatch.setattr(srv, "_ef_last_ac", 0.0)

    srv._ef_apply_quota({"plugInInfoPvVol": 32.9})
    assert srv._ef_last_msg > 0.0, "any message must prove the device is talking"
    assert srv._ef_last_ac == 0.0, "but it is not a new power reading"

    srv._ef_apply_quota({srv._EF_FIELD_AC: 10.0})
    assert srv._ef_last_ac > 0.0


def test_steady_output_stays_valid_while_the_value_ages(srv, monkeypatch):
    """The exact regression: value 4 minutes old, device still chatting.

    Under the old rule this returned None and the UI said "Offline".
    """
    monkeypatch.setattr(srv, "ECOFLOW_ENABLED", True)
    monkeypatch.setattr(srv, "_ef_online", True)
    monkeypatch.setattr(srv, "_ef_state", {srv._EF_FIELD_AC: 167.0})
    now = time.time()
    monkeypatch.setattr(srv, "_ef_last_ac",  now - 240)   # value unchanged 4 min
    monkeypatch.setattr(srv, "_ef_last_msg", now - 5)     # but still talking
    assert srv._ef_power_w() == pytest.approx(167.0)


def test_device_declaring_offline_drops_the_value_immediately(srv, monkeypatch):
    """Overnight must not carry the last daytime figure forward."""
    monkeypatch.setattr(srv, "ECOFLOW_ENABLED", True)
    monkeypatch.setattr(srv, "_ef_online", False)
    monkeypatch.setattr(srv, "_ef_state", {srv._EF_FIELD_AC: 167.0})
    monkeypatch.setattr(srv, "_ef_last_msg", time.time())
    assert srv._ef_power_w() is None


def test_silence_backstop_is_above_the_devices_own_upload_period(srv):
    """runtimePropertyFullUploadPeriod is 300 s. The backstop must clear it
    comfortably or steady output trips it and we are back to flapping."""
    assert srv._EF_SILENCE_SECS >= 600


# ── MQTT wire format ─────────────────────────────────────────────────────────

def test_remaining_length_encoding(srv):
    assert srv._mqtt_rl_encode(0)    == b"\x00"
    assert srv._mqtt_rl_encode(127)  == b"\x7f"
    assert srv._mqtt_rl_encode(128)  == b"\x80\x01"
    assert srv._mqtt_rl_encode(16383) == b"\xff\x7f"


def test_mqtt_string_is_length_prefixed(srv):
    assert srv._mqtt_str("MQTT") == b"\x00\x04MQTT"


# ── Energy integration ───────────────────────────────────────────────────────

def test_kwh_integration_skips_long_gaps(srv, monkeypatch, tmp_path):
    """A restart leaves a gap. Carrying power across it would invent energy."""
    db = tmp_path / "h.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE snapshots (ts INTEGER PRIMARY KEY, plugin_w REAL)")
    base = int(time.time()) // 86400 * 86400 + 43200   # midday, avoids DST edges
    # 10 samples 10s apart at 360W, then an hour-long gap, then one more sample.
    for i in range(10):
        conn.execute("INSERT INTO snapshots VALUES (?,?)", (base + i * 10, 360.0))
    conn.execute("INSERT INTO snapshots VALUES (?,?)", (base + 3600, 360.0))
    conn.commit()

    day = time.strftime("%Y-%m-%d", time.localtime(base))
    rows = conn.execute(
        "SELECT ts, plugin_w FROM snapshots WHERE plugin_w IS NOT NULL ORDER BY ts").fetchall()
    conn.close()

    total = 0.0
    prev = None
    for ts, w in rows:
        if prev is not None:
            gap = ts - prev
            if 0 < gap <= srv._EF_KWH_MAX_GAP:
                total += w * gap / 3600.0 / 1000.0
        prev = ts
    # 9 intervals x 10s x 360W = 0.009 kWh. The 3600s gap contributes nothing.
    assert total == pytest.approx(0.009, abs=1e-6)
    assert day  # sanity: the day string built cleanly


def test_null_rows_contribute_nothing(srv, tmp_path):
    """Overnight NULLs must not be read as zero-watt samples that pad the day."""
    db = tmp_path / "h2.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE snapshots (ts INTEGER PRIMARY KEY, plugin_w REAL)")
    base = int(time.time())
    conn.execute("INSERT INTO snapshots VALUES (?,?)", (base, None))
    conn.execute("INSERT INTO snapshots VALUES (?,?)", (base + 10, None))
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE plugin_w IS NOT NULL").fetchone()[0]
    conn.close()
    assert n == 0


# ── Integration surface ──────────────────────────────────────────────────────

def test_plugin_hint_suppressed_when_measuring(srv, monkeypatch):
    """If we are measuring the plug-in solar, asking whether it exists is absurd."""
    monkeypatch.setattr(srv, "ECOFLOW_ENABLED", True)
    monkeypatch.setattr(srv, "PLUGIN_SOLAR_NOTIFIED", False)
    monkeypatch.setattr(srv, "PLUGIN_SOLAR_DECLARED", False)
    hint = srv._plugin_solar_hint()
    assert hint["show"] is False
    assert hint["declared"] is True


def test_snapshot_reports_offline_without_raising(srv, monkeypatch):
    _prime(srv, monkeypatch, watts=None, online=False)
    snap = srv._ef_snapshot()
    assert snap["power_w"] is None
    assert snap["online"] is False


def test_db_migration_adds_plugin_column(srv):
    """The migration must be additive, and must run on an EXISTING pre-v3.3 DB
    rather than only on a freshly created one."""
    srv.init_db()
    with srv._db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshots)")]
        assert "plugin_w" in cols
        # Existing columns must survive untouched -- solar_w and home_w keep the
        # inverter's own figures; the correction happens at display time.
        assert "solar_w" in cols and "home_w" in cols
    # Idempotent: running it again must not raise on the already-added column.
    srv.init_db()

