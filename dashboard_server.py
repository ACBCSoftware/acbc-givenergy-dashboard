"""
GivEnergy Local Dashboard — Flask backend
Polls inverter via Modbus TCP (port 8899) and serves live JSON + the dashboard HTML.
Run:  python dashboard_server.py
Open: http://localhost:7890  (or http://<your-PC-IP>:7890 from your phone)
"""
import asyncio
import configparser
import gzip
import hashlib
import io
import json
import logging
import shutil
import socket
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, make_response, request, send_file, send_from_directory

# ── givenergy-modbus library shim ──────────────────────────────────────────────
# Poll mode uses the library: v2.x (Python ≥3.14) is async; v0.10.x is sync.
# Gen3 "listen" mode needs NEITHER — it parses raw broadcast frames with the
# stdlib socket module — so library import failures are non-fatal: the app can
# still run in listen mode. _LIB is 'v2', 'v0', or None (poll unavailable).
_LIB = None
_API_V2 = False
_IR_BY_INDEX = {}
try:
    from givenergy_modbus.client.client import Client as _GivClient   # type: ignore
    _LIB, _API_V2 = "v2", True
except Exception:
    try:
        # 0.10.x — bypass the Pydantic model and read registers directly
        from givenergy_modbus.modbus import GivEnergyModbusTcpClient as _GivModbus  # type: ignore
        from givenergy_modbus.model.register import InputRegister as _IR             # type: ignore
        from givenergy_modbus.model.register_cache import RegisterCache as _RC       # type: ignore
        _LIB = "v0"
        # Map integer register index -> enum member, so the active-poll path can
        # use the same index-based field mapping as the Gen3 listen path.
        def _ir_index(m):
            v = m.value
            return v[0] if isinstance(v, tuple) else v
        _IR_BY_INDEX = {_ir_index(m): m for m in _IR}
    except Exception:
        _LIB = None   # no usable poll library — only listen mode will work

APP_VERSION = "1.8"

# ── Config ─────────────────────────────────────────────────────────────────────
_cfg = configparser.ConfigParser()
_cfg.read(Path(__file__).parent / "config.ini")

INVERTER_IP   = _cfg.get("inverter", "ip",            fallback="192.168.68.65")
INVERTER_PORT = _cfg.getint("inverter", "port",        fallback=8899)
NUM_BATTERIES = _cfg.getint("inverter", "num_batteries", fallback=1)
# Connection mode: auto | poll | listen
#   poll   = actively read registers (Gen2 and most inverters)
#   listen = passively decode the broadcast stream (Gen3 / HV hybrid)
#   auto   = try poll first; if the inverter won't answer but is broadcasting,
#            switch to listen automatically.
INVERTER_MODE = _cfg.get("inverter", "mode", fallback="auto").strip().lower()
_active_mode  = ""   # resolved at runtime ('poll' or 'listen'), shown in settings
# Control-page power display: show charge/discharge limits in watts instead of %.
# The register tops out at 50 ("full power"), so watts = (limit/50) × max_power.
# The battery's true max power isn't in a single clean register, so these are
# user settings (sensible default; the user matches them to their GivEnergy app).
POWER_UNITS     = _cfg.get("inverter",    "power_units",   fallback="percent").strip().lower()
MAX_CHARGE_W    = _cfg.getint("inverter", "max_charge_w",    fallback=2600)
MAX_DISCHARGE_W = _cfg.getint("inverter", "max_discharge_w", fallback=2600)
POLL_INTERVAL      = _cfg.getint("server", "poll_interval",      fallback=10)
WEB_PORT           = _cfg.getint("server", "web_port",           fallback=7890)
DATA_RETENTION_DAYS = _cfg.getint("server", "data_retention_days", fallback=365)

MET_API_KEY      = _cfg.get("weather",    "met_api_key",       fallback="")
MET_GEOHASH      = _cfg.get("weather",    "geohash",           fallback="")
WEATHER_POLL_MINS = _cfg.getint("weather", "poll_interval_mins", fallback=30)

BACKUP_ENABLED   = _cfg.getboolean("backup", "enabled",   fallback=True)
BACKUP_KEEP_DAYS = _cfg.getint("backup",     "keep_days", fallback=7)

CHECK_UPDATES = _cfg.getboolean("server", "check_for_updates", fallback=True)

# ── Scheduler (app-held, 48 half-hour block engine — see BACKLOG.md) ──────────
# Master on/off + the baseline mode asserted in every unscheduled block.
# Per-rule schedule rows live in the `schedules` DB table, not in config.
SCHEDULER_ENABLED  = _cfg.getboolean("scheduler", "enabled", fallback=False)
SCHEDULER_BASELINE = _cfg.get("scheduler", "baseline", fallback="eco").strip().lower()
_SCHED_BASELINES   = ("eco", "storage")   # eco = discharge-on/grid-charge-off
if SCHEDULER_BASELINE not in _SCHED_BASELINES:
    SCHEDULER_BASELINE = "eco"

_DEFAULT_HASH = hashlib.sha256(b"password").hexdigest()
ADMIN_HASH   = _cfg.get("admin", "password_hash", fallback=_DEFAULT_HASH)

_COLOUR_DEFAULTS: dict[str, str] = {
    "solar":    "#f59e0b",
    "home":     "#38bdf8",
    "grid_in":  "#f87171",
    "grid_out": "#4ade80",
    "bat_chg":  "#818cf8",
    "bat_dis":  "#c084fc",
    "soc":      "#fbbf24",
}
CHART_COLORS: dict[str, str] = {
    k: _cfg.get("colours", k, fallback=v)
    for k, v in _COLOUR_DEFAULTS.items()
}

def _valid_hex(v: str) -> bool:
    return (isinstance(v, str) and len(v) == 7 and v[0] == "#"
            and all(c in "0123456789abcdefABCDEF" for c in v[1:]))

def _authorised():
    """Check X-Admin-Password header against stored hash."""
    pw = request.headers.get("X-Admin-Password", "")
    return hashlib.sha256(pw.encode()).hexdigest() == ADMIN_HASH

DB_PATH       = Path(__file__).parent / "history.db"
BACKUPS_DIR   = Path(__file__).parent / "backups"
PENDING_IMPORT = Path(str(DB_PATH) + ".pending")   # history.db.pending

# ── Backup / restore ─────────────────────────────────────────────────────────
def _make_backup_gz(dest: Path):
    """Write a consistent, gzipped copy of the live database to `dest`.
    Uses SQLite's online backup API so it's safe while the DB is being written."""
    BACKUPS_DIR.mkdir(exist_ok=True)
    tmp = dest.with_suffix(".tmpdb")
    src = sqlite3.connect(DB_PATH)
    try:
        bk = sqlite3.connect(str(tmp))
        src.backup(bk)
        bk.close()
    finally:
        src.close()
    with open(tmp, "rb") as f, gzip.open(dest, "wb") as g:
        shutil.copyfileobj(f, g)
    tmp.unlink(missing_ok=True)

def _prune_backups(days: int):
    if not BACKUPS_DIR.exists():
        return
    cutoff = time.time() - days * 86400
    for p in BACKUPS_DIR.glob("history-*.db.gz"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass

def _maybe_backup():
    """Create one auto-backup per calendar day (on the first reading after
    midnight), then prune old ones. Cheap no-op once today's exists."""
    if not BACKUP_ENABLED:
        return
    dest = BACKUPS_DIR / f"history-{datetime.now().strftime('%Y%m%d')}.db.gz"
    if dest.exists():
        return
    try:
        _make_backup_gz(dest)
        _prune_backups(BACKUP_KEEP_DAYS)
        log.warning("Daily backup written: %s", dest.name)
    except Exception as exc:
        log.error("Backup failed: %s", exc)

def _last_backup_info():
    """Return (filename, iso-date) of the most recent auto-backup, or (None,None)."""
    if not BACKUPS_DIR.exists():
        return None, None
    files = sorted(BACKUPS_DIR.glob("history-*.db.gz"))
    if not files:
        return None, None
    latest = files[-1]
    return latest.name, datetime.fromtimestamp(latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

def _apply_pending_import():
    """If a restore was staged, swap it in on startup (after backing up current)."""
    if not PENDING_IMPORT.exists():
        return
    try:
        if DB_PATH.exists():
            BACKUPS_DIR.mkdir(exist_ok=True)
            safe = BACKUPS_DIR / f"pre-import-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db.gz"
            try:
                _make_backup_gz(safe)
            except Exception as exc:
                log.error("Could not back up current DB before import: %s", exc)
        # Clear stale WAL/SHM so the new file isn't mixed with old journal data
        for ext in ("-wal", "-shm"):
            p = Path(str(DB_PATH) + ext)
            if p.exists():
                p.unlink(missing_ok=True)
        shutil.move(str(PENDING_IMPORT), str(DB_PATH))
        log.warning("Imported database applied from staged restore.")
    except Exception as exc:
        log.error("Failed to apply pending import: %s", exc)

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("dashboard")

# The givenergy-modbus library logs a multi-line traceback on every transient
# read failure (very common on Modbus TCP). These are harmless blips that we
# already handle ourselves, so silence the library's own noisy error logging.
logging.getLogger("givenergy_modbus").setLevel(logging.CRITICAL)
# pymodbus (the transport underneath) can be chatty too.
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

# ── Database ───────────────────────────────────────────────────────────────────
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    # ── Schema migration policy ────────────────────────────────────────────────
    # This runs on every startup and upgrades an existing database IN PLACE
    # without ever dropping data:
    #   • New tables  → CREATE TABLE IF NOT EXISTS
    #   • New columns → ALTER TABLE ADD COLUMN, guarded by a PRAGMA check
    # Existing rows and the user's history are always preserved across upgrades.
    # When adding future schema changes, follow the same additive pattern below.
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                ts                  INTEGER PRIMARY KEY,
                solar_w             INTEGER,
                home_w              INTEGER,
                battery_w           INTEGER,
                battery_charging    INTEGER,
                battery_discharging INTEGER,
                grid_w              INTEGER,
                grid_importing      INTEGER,
                grid_exporting      INTEGER,
                soc                 INTEGER,
                solar_today         REAL,
                grid_in_today       REAL,
                grid_out_today      REAL,
                bat_chg_today       REAL,
                bat_dis_today       REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON snapshots(ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS control_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      INTEGER NOT NULL,
                command TEXT    NOT NULL,
                params  TEXT,
                success INTEGER NOT NULL DEFAULT 1,
                message TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS event_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      INTEGER NOT NULL,
                kind    TEXT    NOT NULL,   -- 'status' | 'fault' | 'info'
                message TEXT    NOT NULL
            )
        """)
        # Scheduler rules (app-held 48-block engine — see BACKLOG.md).
        # days_mask: 7-bit, bit0=Mon … bit6=Sun (127 = every day).
        # target_soc applies to 'charge' only (NULL otherwise).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                enabled    INTEGER NOT NULL DEFAULT 1,
                action     TEXT    NOT NULL,           -- 'charge' | 'hold' | 'export'
                start_hhmm TEXT    NOT NULL,           -- 'HH:MM' (snaps to 30-min grid)
                end_hhmm   TEXT    NOT NULL,           -- 'HH:MM' (snaps to 30-min grid)
                days_mask  INTEGER NOT NULL DEFAULT 127,
                target_soc INTEGER,
                created    INTEGER NOT NULL
            )
        """)
        # Migration: add temperature columns if upgrading from an older version
        cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshots)")]
        if "t_battery"  not in cols:
            conn.execute("ALTER TABLE snapshots ADD COLUMN t_battery  REAL")
        if "t_heatsink" not in cols:
            conn.execute("ALTER TABLE snapshots ADD COLUMN t_heatsink REAL")
        conn.commit()

def _log_snapshot(data):
    with _db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO snapshots
            (ts, solar_w, home_w, battery_w, battery_charging, battery_discharging,
             grid_w, grid_importing, grid_exporting, soc,
             solar_today, grid_in_today, grid_out_today, bat_chg_today, bat_dis_today,
             t_battery, t_heatsink)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(data["ts"]),
            data["solar_w"],   data["home_w"],
            data["battery_w"], int(data["battery_charging"]), int(data["battery_discharging"]),
            data["grid_w"],    int(data["grid_importing"]),   int(data["grid_exporting"]),
            data["soc"],
            data["solar_today"], data["grid_in_today"], data["grid_out_today"],
            data["bat_chg_today"], data["bat_dis_today"],
            data.get("t_battery"), data.get("t_heatsink"),
        ))
        conn.commit()

def _purge_old(days):
    cutoff = int(time.time()) - days * 86400
    with _db() as conn:
        conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
        # Keep event/control logs for the same retention window
        conn.execute("DELETE FROM event_log   WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM control_log WHERE ts < ?", (cutoff,))
        conn.commit()

def _log_event(kind, message):
    with _db() as conn:
        conn.execute(
            "INSERT INTO event_log (ts, kind, message) VALUES (?,?,?)",
            (int(time.time()), kind, message))
        conn.commit()

# ── Shared state ───────────────────────────────────────────────────────────────
_lock        = threading.Lock()
_cached: dict = {}
_error: str   = ""

# Slave address of the inverter as seen in the most recent IR frame from the
# listen loop (frame[26]).  0 = not yet observed.  Read by the control engine
# as a hint; never used as the sole discriminator for generation.
_inverter_slave: int = 0

# Last SOC value seen from a Gateway AIO base=1780 broadcast (IR1801 = aio1_soc).
# Updated whenever a base=1780 frame arrives; merged into the base=1600 live dict.
_gateway_soc: int = 0

# ── Live-data smoothing ────────────────────────────────────────────────────────
# Fields that should almost never be zero in a real home.
# If a zero reading hasn't persisted for this many consecutive polls it is
# treated as a blip and the last known good value is shown instead.
_DEBOUNCE = {
    "home_w":    12,  # 12 × poll_interval = 120 s — home never truly reads 0
    "solar_w":   3,   #  3 × poll_interval =  30 s — night zeros last for hours so still stored
    "battery_w": 3,
}
_zero_streak: dict = {}
_last_good:   dict = {}

def _smooth(data: dict) -> dict:
    """Return a copy of data with brief zero-blips suppressed."""
    out = dict(data)
    for field, needed in _DEBOUNCE.items():
        v = out.get(field) or 0
        if v == 0:
            streak = _zero_streak.get(field, 0) + 1
            _zero_streak[field] = streak
            if streak < needed and field in _last_good:
                out[field] = _last_good[field]   # hold last good value
        else:
            _zero_streak[field] = 0
            _last_good[field]   = v
    return out

# ── Weather ────────────────────────────────────────────────────────────────────
_weather_cached: dict = {}
_last_weather_ts: float = 0.0

def _weather_interval():
    return max(5, WEATHER_POLL_MINS) * 60

def _fetch_weather() -> dict:
    """Fetch latest land observation from Met Office DataHub."""
    url = f"https://data.hub.api.metoffice.gov.uk/observation-land/1/{MET_GEOHASH}"
    req = urllib.request.Request(
        url, headers={"apikey": MET_API_KEY, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        obs = json.loads(r.read())
    latest = next(
        (o for o in reversed(obs) if "temperature" in o and "weather_code" in o), None)
    if not latest:
        raise ValueError("No complete observation in response")
    return {
        "ok":           True,
        "temp":         round(latest["temperature"], 1),
        "weather_code": latest["weather_code"],
        "wind_dir":     latest.get("wind_direction", ""),
        "wind_mph":     round((latest.get("wind_speed") or 0) * 2.237),
        "humidity":     latest.get("humidity"),
        "updated":      latest.get("datetime", ""),
    }

# ── Inverter data: shared field mapping ─────────────────────────────────────────
def _build_from_input_page(g) -> dict:
    """Build the live data dict from input registers 0–59, where `g(n)` returns
    the raw value of register n. This same mapping serves BOTH the active-poll
    path (Gen2) and the Gen3 broadcast-listen path, since both deliver the
    identical register page with identical scaling."""
    def signed(v): return v - 65536 if v >= 32768 else v
    battery_w_raw = signed(g(52))    # P_BATTERY   (-=charge +=discharge)
    grid_w_raw    = signed(g(30))    # P_GRID_OUT  (-=import +=export)
    return {
        "ok":  True,
        "ts":  time.time(),
        "solar_w":             g(18) + g(20),         # P_PV1 + P_PV2
        "home_w":              g(42),                 # P_LOAD_DEMAND
        "battery_w":           abs(battery_w_raw),
        "battery_charging":    battery_w_raw < 0,
        "battery_discharging": battery_w_raw > 0,
        "battery_idle":        battery_w_raw == 0,
        "grid_w":              abs(grid_w_raw),
        "grid_importing":      grid_w_raw < 0,
        "grid_exporting":      grid_w_raw > 0,
        "soc":        max(0, min(100, g(59))),            # BATTERY_PERCENT — clamp uint16 to valid %
        "v_battery":  g(50) / 100,                    # V_BATTERY
        "t_battery":  g(56) / 10,                     # TEMP_BATTERY
        "t_heatsink": g(41) / 10,                     # TEMP_INVERTER_HEATSINK
        "solar_today":    g(17) / 10 + g(19) / 10,    # E_PV1_DAY + E_PV2_DAY
        "grid_in_today":  g(26) / 10,                 # E_GRID_IN_DAY
        "grid_out_today": g(25) / 10,                 # E_GRID_OUT_DAY
        "bat_chg_today":  g(36) / 10,                 # E_BATTERY_CHARGE_DAY
        "bat_dis_today":  g(37) / 10,                 # E_BATTERY_DISCHARGE_DAY
        "status":     str(g(0)),                      # INVERTER_STATUS
    }

# ── Gateway AIO live data (IR base=1600, GivTCP gateway.py confirmed) ────────────
def _build_from_gateway_page(g) -> dict:
    """Decode a GivEnergy Gateway AIO base=1600 input-register frame.

    Register offsets (0-based from base=1600):
      r16 p_ac1     grid W signed int16  (neg=import, pos=export — same as gen2)
      r17 p_pv      solar W uint16
      r18 p_load    home load W uint16
      r19 p_liberty battery W signed int16 (pos=charge, neg=discharge — inverted vs gen2)
    SOC comes from a separate base=1780 frame (IR1801); cached in _gateway_soc.
    """
    def s16(v): return v - 65536 if v >= 32768 else v
    grid_raw    = s16(g(16))   # p_ac1:     neg=import, pos=export
    bat_raw     = -s16(g(19))  # p_liberty: flip sign → neg=charge, pos=discharge (gen2 convention)
    return {
        "ok":  True,
        "ts":  time.time(),
        "solar_w":             g(17),
        "home_w":              g(18),
        "battery_w":           abs(bat_raw),
        "battery_charging":    bat_raw < 0,
        "battery_discharging": bat_raw > 0,
        "battery_idle":        bat_raw == 0,
        "grid_w":              abs(grid_raw),
        "grid_importing":      grid_raw < 0,
        "grid_exporting":      grid_raw > 0,
        "soc":        _gateway_soc,
        "v_battery":  0,
        "t_battery":  0,
        "t_heatsink": 0,
        "solar_today":    g(43) / 10,   # e_pv_today      IR1643
        "grid_in_today":  g(40) / 10,   # e_grid_import_today IR1640
        "grid_out_today": g(46) / 10,   # e_grid_export_today IR1646
        "bat_chg_today":  g(49) / 10,   # e_aio_charge_today  IR1649
        "bat_dis_today":  g(52) / 10,   # e_aio_discharge_today IR1652
        "status":     "1",              # Gateway broadcasts as normal
    }

# ── Active poll (Gen2 / library) ─────────────────────────────────────────────────
def _build_data(iv, *, soc, t_battery, t_heatsink, status) -> dict:
    """Build the common data dict from an inverter model object (v2 API)."""
    solar_w       = (iv.p_pv1 or 0) + (iv.p_pv2 or 0)
    home_w        = iv.p_load_demand or 0
    battery_w_raw = iv.p_battery or 0   # negative = charging, positive = discharging
    grid_w_raw    = iv.p_grid_out or 0  # negative = importing, positive = exporting
    return {
        "ok":  True,
        "ts":  time.time(),
        "solar_w":             solar_w,
        "home_w":              home_w,
        "battery_w":           abs(battery_w_raw),
        "battery_charging":    battery_w_raw < 0,
        "battery_discharging": battery_w_raw > 0,
        "battery_idle":        battery_w_raw == 0,
        "grid_w":              abs(grid_w_raw),
        "grid_importing":      grid_w_raw < 0,
        "grid_exporting":      grid_w_raw > 0,
        "soc":        soc,
        "v_battery":  iv.v_battery,
        "t_battery":  t_battery,
        "t_heatsink": t_heatsink,
        "solar_today":    (iv.e_pv1_day or 0) + (getattr(iv, "e_pv2_day", 0) or 0),
        "grid_in_today":  iv.e_grid_in_day or 0,
        "grid_out_today": iv.e_grid_out_day or 0,
        "bat_chg_today":  iv.e_battery_charge_day or 0,
        "bat_dis_today":  iv.e_battery_discharge_day or 0,
        "status":     status,
    }

async def _fetch_v2() -> dict:
    """givenergy-modbus ≥2 async API (Python ≥3.14)."""
    client = _GivClient(INVERTER_IP, port=INVERTER_PORT)
    await client.connect()
    try:
        plant = await client.refresh_plant(
            full_refresh=False, max_batteries=NUM_BATTERIES, timeout=6.0, retries=2)
    finally:
        await client.close()
    iv = plant.inverter
    return _build_data(iv,
        soc       = iv.battery_soc or 0,
        t_battery = iv.t_battery,
        t_heatsink= iv.t_inverter_heatsink,
        status    = str(iv.status).replace("Status.", ""))

def _fetch_v0() -> dict:
    """givenergy-modbus 0.10.x — actively read input registers 0–59 (Gen2)."""
    mc = _GivModbus(host=INVERTER_IP, port=INVERTER_PORT)
    rc = _RC()
    rc.set_registers(_IR, mc.read_registers(_IR, 0, 60, slave_address=0x32))
    return _build_from_input_page(lambda n: rc[_IR_BY_INDEX[n]])

# Inverter status code → human label (and whether it's a fault-class event)
_STATUS_LABELS = {
    "0": "Waiting", "1": "Normal", "2": "Warning",
    "3": "Fault",   "4": "Firmware update",
    # v2 API already returns text labels — pass those through unchanged
}

def _status_label(raw):
    return _STATUS_LABELS.get(str(raw), str(raw).title())

# ── Listen / poke-and-listen (works for Gen2 + Gen3) ────────────────────────────
# Static "read input registers 0–59" request frames, exactly as the library
# sends them (fixed placeholder serial + CRC). Sending these to the inverter
# makes it emit the 164-byte input-register response, which we decode ourselves —
# robust to the library quirks that cause IR:052, and needs no library at all.
#
# The only byte that differs is the Modbus slave address: Gen2 inverters answer
# on 0x32 (50), Gen3 / HV hybrid units answer on 0x11 (17). We send BOTH each
# cycle; the inverter ignores the one that isn't addressed to it. (The CRC is the
# same for both because GivEnergy computes it over the function + base + count,
# not the slave byte.)
def _crc16(data: bytes) -> bytes:
    """CRC16-Modbus, LSB first."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])

def _bms_crc16(func: int, base: int, count: int) -> bytes:
    """CRC16-Modbus, MSB-first, over func+base+count only (no slave byte).
    This is the Gen2/LV-battery convention — verified against the known Gen2
    poke CRC (d1d5 for IR(0,60)@0x32)."""
    crc = 0xFFFF
    for b in bytes([func]) + base.to_bytes(2, "big") + count.to_bytes(2, "big"):
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([(crc >> 8) & 0xFF, crc & 0xFF])   # MSB-first

def _make_poke(slave: int, func: int = 0x04, base: int = 0, count: int = 60) -> bytes:
    """Build a GivEnergy transparent request frame.
    Uses LSB-first CRC over slave+func+base+count (Gen3/AIO convention).
    For the listen-mode IR pokes use _POKE_REQUESTS below (hardcoded proven values).
    """
    serial  = b"AB1234G567"
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([slave, func]) + base.to_bytes(2, "big") + count.to_bytes(2, "big")
    crc     = _crc16(inner)   # LSB-first, with slave byte
    payload = serial + padding + inner + crc
    length  = len(payload) + 2
    return b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload

# Hardcoded proven poke frames — each uses the CRC convention verified on real hardware:
#   Gen2 (0x32): CRC d1d5  = MSB-first CRC over func+base+count only (original library format,
#                             17.5h clean run confirmed). Slave-inclusive CRC (f5d8) causes drops.
#   Gen3/AIO (0x11): CRC f28b = LSB-first CRC over slave+func+base+count (GivTCP format,
#                               confirmed with 25/25 fast responses in wire capture).
#   Gateway AIO (DTC 0x70xx): live data at IR base=1600, SOC at IR base=1780 (confirmed from
#                               David's wire capture + GivTCP gateway.py register map).
#                               Both use the same slave-inclusive LSB-first CRC as Gen3/AIO.
_POKE_REQUESTS = [
    bytes.fromhex("59590001001c010241423132333447353637000000000000000832040000003cd1d5"),
    bytes.fromhex("59590001001c010241423132333447353637000000000000000811040000003cf28b"),
]
# Slave byte is at frame offset 26 — build a direct lookup for adaptive poking.
_POKE_BY_SLAVE: dict[int, bytes] = {poke[26]: poke for poke in _POKE_REQUESTS}

# Gateway AIO (DTC 0x70xx) active pokes (slave 0x11, Gen3/AIO LSB-first CRC).
# David's --aio wire capture (05 Jun 2026) proved that actively polling IR(1600,60)
# every 10s and IR(1780,60) every ~60s returns FRESH data on every poke (battery
# charge taper + solar decline tracked in real time) — overturning the earlier
# "active polling returns stale data" assumption. So the gateway is now polled like
# any other inverter, gated on the detected gateway_aio profile in _send_pokes().
#   base 1600 → live power page (decoded by _build_from_gateway_page)
#   base 1780 → per-unit SOC (IR1801, cached in _gateway_soc)
_GATEWAY_POKE_1600 = _make_poke(0x11, 0x04, 1600, 60)
_GATEWAY_POKE_1780 = _make_poke(0x11, 0x04, 1780, 60)
_GATEWAY_SOC_POKE_SECS = 55          # how often to poke base=1780 for SOC
_last_soc_poke = 0.0

def _pop_data_frames(buf: bytearray):
    """Pull complete GivEnergy frames out of `buf` (each starts 0x59 0x59, total
    length = 6 + the MBAP length field). Returns a list of frames and resyncs
    past any garbage. Leftover partial data stays in the buffer."""
    frames = []
    while True:
        start = buf.find(b"\x59\x59")
        if start < 0:
            if len(buf) > 1:
                del buf[:-1]            # keep a trailing byte (0x59 may be split)
            return frames
        if start > 0:
            del buf[:start]             # drop junk before the marker
        if len(buf) < 6:
            return frames               # need the header to read the length
        length = (buf[4] << 8) | buf[5]
        total  = 6 + length
        if length <= 0 or length > 4096:
            del buf[:2]                 # bad length — skip marker, resync
            continue
        if len(buf) < total:
            return frames               # wait for the rest of the frame
        frames.append(bytes(buf[:total]))
        del buf[:total]

def _decode_listen_frame(frame: bytes):
    """Decode a broadcast input-register response into the live data dict.
    Handles three frame types:
      base=0    — Gen2/Gen3 standard IR page (IR 0-59)
      base=1600 — Gateway AIO live power data (GivTCP confirmed)
      base=1780 — Gateway AIO per-unit SOC (updates _gateway_soc cache only)
    Returns a data dict on success, None if the frame is unrecognised or
    if only the SOC cache was updated (base=1780)."""
    global _gateway_soc
    if len(frame) < 44 or frame[7] != 0x02:
        return None
    inner_func = frame[27]
    base  = (frame[38] << 8) | frame[39]
    count = (frame[40] << 8) | frame[41]
    if inner_func != 0x04:
        return None
    regs_off = 42
    def g(n):
        o = regs_off + n * 2
        if o + 1 < len(frame):
            return (frame[o] << 8) | frame[o + 1]
        return 0

    if base == 0 and count >= 60:
        if len(frame) < regs_off + 60 * 2:
            return None
        # Gateway AIO returns all-zero base=0 pages (real data lives at base=1600).
        # Detect this by checking the inverter serial registers (r13-r17): on a real
        # Gen2/Gen3 inverter these always contain ASCII bytes; on a gateway they are
        # zero.  Also guard against accepting zero-only night/idle readings as real
        # gateway noise by additionally checking that key power registers are zero.
        serial_zero = all(g(n) == 0 for n in range(13, 18))
        power_zero  = g(18) == 0 and g(20) == 0 and g(42) == 0 and g(52) == 0
        if serial_zero and power_zero:
            return None   # gateway zero-response — discard, real data is at base=1600
        return _build_from_input_page(g)

    if base == 1600 and count >= 60:
        if len(frame) < regs_off + 60 * 2:
            return None
        return _build_from_gateway_page(g)

    if base == 1780 and count >= 22:
        # IR1801 = aio1_soc is at offset 21 from base 1780.
        if len(frame) >= regs_off + 22 * 2:
            soc = g(21)
            if 0 <= soc <= 100:
                _gateway_soc = soc
        return None   # SOC cache updated; no full reading to publish yet

    return None

def _is_heartbeat_frame(frame: bytes) -> bool:
    """True if this is a 1/Heartbeat frame from the dongle (outer function 0x01)."""
    return len(frame) >= 8 and frame[7] == 0x01

# Pre-built heartbeat response: header + dummy serial 'AB1234G567' (what the
# givenergy-modbus library sends). The type byte (last byte) is patched per-frame.
# Must NOT echo the dongle's real serial back — that causes the dongle to treat the
# session as looped and reset, producing far more 75s drops than no response at all.
_HB_RESPONSE_PREFIX = bytes.fromhex("59590001000d010141423132333447353637")

def _note_heartbeat() -> float:
    """Gen2 does NOT need a heartbeat response — it ran 17.5h without one (2 drops).
    Sending any response (even dummy serial) disturbs the Gen2 broadcast enough
    to cause frequent 75s timeouts. Gen3/AIO use GivTCP-style polling and manage
    heartbeats themselves. For the listen loop: just reset the watchdog timer."""
    return time.time()

# ── Shared loop housekeeping ─────────────────────────────────────────────────────
def _handle_reading(data: dict, st: dict):
    """Process one fresh reading (from either mode): log to DB, smooth, publish
    to the cache, log recovery, and track inverter status changes + purging."""
    global _cached, _error
    data = _smooth(data)             # suppress zero-blips before DB write and display
    _log_snapshot(data)              # smoothed values to DB
    with _lock:
        _cached = data
        _error  = ""
    if st.get("offline"):
        log.warning("Connection to inverter restored")
        _log_event("info", "Connection to inverter restored")
        st["offline"] = False
    label = _status_label(data.get("status"))
    if st.get("status") is None:
        st["status"] = label                          # first reading, no event
    elif label != st["status"]:
        kind = "fault" if label in ("Fault", "Warning") else "status"
        _log_event(kind, f"Inverter status: {st['status']} → {label}")
        st["status"] = label
    if time.time() - st.get("purge", 0.0) > 86400:
        _purge_old(DATA_RETENTION_DAYS)
        st["purge"] = time.time()
    _maybe_backup()   # one auto-backup per calendar day (cheap no-op otherwise)

def _maybe_weather():
    """Fetch weather if configured and the interval has elapsed (cheap no-op otherwise)."""
    global _weather_cached, _last_weather_ts
    if MET_API_KEY and MET_GEOHASH and time.time() - _last_weather_ts > _weather_interval():
        try:
            wx = _fetch_weather()
            with _lock:
                _weather_cached = wx
            _last_weather_ts = time.time()
            log.info("Weather: %s°C code=%s", wx["temp"], wx["weather_code"])
        except Exception as exc:
            log.error("Weather fetch failed: %s", exc)

# ── Update check ──────────────────────────────────────────────────────────────
_update_info: dict = {}
_last_update_check: float = 0.0
_UPDATE_INTERVAL = 86400   # 24 hours
_GH_API = "https://api.github.com/repos/ACBCSoftware/acbc-givenergy-dashboard/releases/latest"

def _parse_version(tag: str) -> tuple:
    """'v1.7' or '1.7' → (1, 7). Returns (0,) on parse failure."""
    try:
        return tuple(int(x) for x in tag.lstrip("v").split("."))
    except Exception:
        return (0,)

def _check_for_update():
    """Fetch the latest GitHub release and update _update_info. Silent on error."""
    global _update_info, _last_update_check
    if not CHECK_UPDATES:
        return
    try:
        req = urllib.request.Request(
            _GH_API,
            headers={"User-Agent": f"acbc-givenergy-dashboard/{APP_VERSION}",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        latest_tag  = data.get("tag_name", "")
        latest_name = data.get("name", latest_tag)[:80]   # release title, capped
        available   = _parse_version(latest_tag) > _parse_version(APP_VERSION)
        _update_info = {
            "available":    available,
            "current":      APP_VERSION,
            "latest":       latest_tag.lstrip("v"),
            "release_name": latest_name,
            "url":          "https://software.andrewcampbell.co.uk/release-notes.html",
            "checked_at":   int(time.time()),
        }
        if available:
            log.warning("Update available: v%s → %s", APP_VERSION, latest_tag)
    except Exception as exc:
        log.warning("Update check failed: %s", exc)
    finally:
        _last_update_check = time.time()

def _maybe_check_update():
    """Call at most once per day from the data loop."""
    if CHECK_UPDATES and time.time() - _last_update_check > _UPDATE_INTERVAL:
        _check_for_update()

def _run_poll(st: dict):
    """Active-poll loop (Gen2). One reading per POLL_INTERVAL."""
    global _cached, _error
    fail = 0
    FAIL_THRESHOLD = 3   # tolerate brief Modbus blips before flagging offline
    if _API_V2:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    while True:
        try:
            data = loop.run_until_complete(_fetch_v2()) if _API_V2 else _fetch_v0()
            _handle_reading(data, st)
            log.info("Polled: solar=%dW soc=%d%%", data["solar_w"], data["soc"])
            fail = 0
        except Exception as exc:
            msg = str(exc)
            fail += 1
            with _lock:
                _error = msg
                if _cached and fail >= FAIL_THRESHOLD:
                    _cached["ok"] = False
            if fail >= FAIL_THRESHOLD and not st.get("offline"):
                log.warning("Lost connection to inverter after %d failed polls: %s", fail, msg)
                _log_event("fault", f"Lost connection to inverter: {msg}")
                st["offline"] = True
        _maybe_weather()
        _maybe_check_update()
        time.sleep(POLL_INTERVAL)

def _send_pokes(s):
    """Send IR read-request frame(s) to trigger an inverter response.
    Adaptive: once the responding slave is known (_inverter_slave set from the
    first decoded frame), only that slave's poke is sent.  This stops the Gen2
    dongle receiving unexpected slave-address frames every 10 s, which is one of
    the triggers for the occasional 75 s broadcast drop.
    Discovery mode (slave not yet seen): send all frames until one responds.
    Gateway AIO (once detected): poke its base=1600 live page every interval and
    base=1780 SOC every ~60s, instead of the base=0 page (which it answers with
    all-zeros)."""
    global _last_soc_poke
    if _inverter_profile == "gateway_aio":
        s.sendall(_GATEWAY_POKE_1600)
        now = time.time()
        if now - _last_soc_poke >= _GATEWAY_SOC_POKE_SECS:
            s.sendall(_GATEWAY_POKE_1780)
            _last_soc_poke = now
        return
    if _inverter_slave in _POKE_BY_SLAVE:
        s.sendall(_POKE_BY_SLAVE[_inverter_slave])
    else:
        for poke in _POKE_REQUESTS:
            s.sendall(poke)

def _detect_on_socket(s, slave: int) -> None:
    """Read HR[0]+HR[21] on an already-open listen socket to detect the inverter
    model.  Populates _inverter_profile/_inverter_model so that later calls to
    _detect_inverter() hit the cache and never open a second TCP connection."""
    global _inverter_profile, _inverter_model
    if _inverter_profile:
        return   # already done
    try:
        serial  = b"AB1234G567"
        padding = b"\x00" * 7 + b"\x08"
        inner   = bytes([slave, 0x03]) + (0).to_bytes(2, "big") + (22).to_bytes(2, "big")
        crc     = _crc16(inner)
        payload = serial + padding + inner + crc
        length  = len(payload) + 2
        req     = b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload
        s.sendall(req)
        # Read response — allow up to 3 seconds; other frames may arrive first
        buf   = bytearray()
        t0    = time.time()
        while time.time() - t0 < 3.0:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf.extend(chunk)
            for frame in _pop_data_frames(buf):
                if len(frame) < 44 or frame[7] != 0x02 or frame[27] != 0x03:
                    continue
                rx_base  = (frame[38] << 8) | frame[39]
                rx_count = (frame[40] << 8) | frame[41]
                if rx_base != 0 or rx_count < 22:
                    continue
                if len(frame) < 42 + 22 * 2:
                    continue
                def g(n): return (frame[42 + n*2] << 8) | frame[43 + n*2]
                raw_dtc = g(0)
                arm_fw  = g(21)
                with _detect_lock:
                    if not _inverter_profile:
                        profile, model = _classify_model(raw_dtc, arm_fw)
                        _inverter_profile = profile
                        _inverter_model   = model
                        log.warning(
                            "Inverter detected on listen socket: "
                            "slave=0x%02x DTC=0x%04x ARM_fw=%d → %s (%s)",
                            slave, raw_dtc, arm_fw, model, profile)
                return
    except Exception as exc:
        log.warning("In-socket detection failed: %s", exc)


def _run_listen(st: dict):
    """Poke-and-listen loop (Gen2 + Gen3). Sends the static read request to
    trigger the inverter, then decodes the input-register frames it emits.
    Publishes one reading per POLL_INTERVAL so the database grows at the normal
    rate. Needs no Modbus library and never hits the IR:052 parse problem."""
    global _cached, _error, _inverter_slave
    while True:
        buf = bytearray()
        latest = None
        last_poke = 0.0
        last_proc = 0.0
        last_frame = time.time()
        try:
            s = socket.create_connection((INVERTER_IP, INVERTER_PORT), timeout=15)
            s.settimeout(3)
            while True:
                now = time.time()
                # Trigger a fresh response every POLL_INTERVAL (and once at start)
                if now - last_poke >= POLL_INTERVAL:
                    _send_pokes(s)
                    last_poke = now
                # Pending scheduler writes: close the listen socket, apply them on a
                # clean dedicated connection, then reconnect listen. The single-client
                # dongle then never has two sockets at once → no contention, no drops,
                # and the dongle reliably echoes the writes (it won't on the broadcast
                # socket). Infrequent (only on a block change), recovers in one cycle.
                # Read whatever has arrived
                try:
                    chunk = s.recv(8192)
                except socket.timeout:
                    chunk = b""
                if chunk:
                    buf.extend(chunk)
                    for frame in _pop_data_frames(buf):
                        if _is_heartbeat_frame(frame):
                            last_frame = _note_heartbeat()  # reset watchdog, send nothing
                            if not st.get("hb_seen"):
                                log.info("Heartbeat received — connection alive")
                                st["hb_seen"] = True
                            continue
                        d = _decode_listen_frame(frame)
                        # Record the responding slave and detect the model from the
                        # first input-register response, on this socket (so later
                        # _detect_inverter() hits cache and never opens a 2nd TCP conn).
                        # Triggered on ANY IR response — including a gateway's all-zero
                        # base=0 page — so gateway_aio is detected promptly and its
                        # base=1600 pokes can start, rather than waiting ~5 min for the
                        # first unsolicited cloud-sync frame. Guarded to real inverter
                        # slaves (0x11 inverter, 0x32 Gen2) so a stray meter/BMS frame
                        # in a cloud-sync burst can't mis-set the slave.
                        if (_inverter_slave == 0 and frame[7] == 0x02
                                and len(frame) > 27 and frame[27] == 0x04
                                and frame[26] in (0x11, 0x32)):
                            _inverter_slave = frame[26]
                            if not _inverter_profile:
                                _detect_on_socket(s, _inverter_slave)
                        if d:
                            latest = d
                            last_frame = now
                # Publish at most once per interval
                if latest and now - last_proc >= POLL_INTERVAL:
                    _handle_reading(latest, st)
                    log.info("Listen: solar=%dW soc=%d%%", latest["solar_w"], latest["soc"])
                    last_proc = now
                _maybe_weather()
                _maybe_check_update()
                # Offline watchdog: no decodable frame for 75s
                if now - last_frame > 75:
                    raise ConnectionError("no inverter data for 75s")
        except Exception as exc:
            msg = str(exc)
            with _lock:
                _error = msg
                if _cached:
                    _cached["ok"] = False
            if not st.get("offline"):
                log.warning("Lost inverter data stream: %s", msg)
                _log_event("fault", f"Lost connection to inverter: {msg}")
                st["offline"] = True
            time.sleep(5)

def _probe_listen() -> bool:
    """Send a poke and see if the inverter emits a decodable input-register
    frame within ~12s. Works for both Gen2 and Gen3."""
    try:
        s = socket.create_connection((INVERTER_IP, INVERTER_PORT), timeout=10)
        s.settimeout(3)
        _send_pokes(s)
        buf = bytearray()
        t0 = time.time()
        while time.time() - t0 < 12:
            try:
                chunk = s.recv(8192)
            except socket.timeout:
                _send_pokes(s)   # nudge again
                continue
            if not chunk:
                break
            buf.extend(chunk)
            for frame in _pop_data_frames(buf):
                if _is_heartbeat_frame(frame):
                    continue  # no ACK needed for probe
                if _decode_listen_frame(frame) is not None:
                    s.close()
                    return True
        s.close()
    except Exception as exc:
        log.warning("Auto-detect: listen probe failed (%s)", exc)
    return False

def _autodetect_mode() -> str:
    """Pick the data mode. Poke-and-listen works for both Gen2 and Gen3 and is
    library-free, so try it first; only fall back to the library poll path if it
    yields nothing."""
    if _probe_listen():
        return "listen"
    log.warning("Auto-detect: no data from poke-and-listen — trying library poll…")
    if _LIB is not None:
        for attempt in range(3):
            try:
                if _API_V2:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(_fetch_v2())
                    loop.close()
                else:
                    _fetch_v0()
                return "poll"
            except Exception as exc:
                log.warning("Auto-detect: library poll attempt %d/3 failed (%s)", attempt + 1, exc)
                time.sleep(2)
    # Default to poke-and-listen — it keeps retrying until the inverter is reachable
    return "listen"

def _data_loop():
    """Thread entry point: pick the mode, then run the matching loop forever."""
    global _active_mode
    st = {"status": None, "purge": 0.0, "offline": False}
    mode = INVERTER_MODE if INVERTER_MODE in ("poll", "listen") else _autodetect_mode()
    if mode == "poll" and _LIB is None:
        log.warning("Poll mode requested but no Modbus library is available — using listen mode.")
        mode = "listen"
    _active_mode = mode
    log.warning("Inverter data mode: %s", mode)
    _log_event("info", f"Inverter data mode: {mode}")
    if mode == "listen":
        _run_listen(st)
    else:
        _run_poll(st)


# ── Inverter control — library-free raw-socket engine ─────────────────────────
#
# All HR reads and writes use a fresh short-lived TCP connection so they never
# contend with the listen-loop socket.  CRC is slave-inclusive LSB-first (the
# Gen3/AIO convention, and correct for slave 0x11 per the official spec).
# The library (givenergy-modbus) is no longer used for control at all.

def _bcd_to_hhmm(v: int) -> str:
    """BCD-encoded inverter time (e.g. 430) → 'HH:MM'."""
    s = f"{v:04d}"
    return f"{s[:2]}:{s[2:]}"

def _hhmm_to_bcd(hhmm: str) -> int:
    """'HH:MM' → BCD int (e.g. '04:30' → 430)."""
    h, m = hhmm.split(":")
    return int(h) * 100 + int(m)


# ── Raw HR read / write ───────────────────────────────────────────────────────

def _hr_read(slave: int, base: int, count: int, timeout: float = 5.0) -> list:
    """Read `count` holding registers starting at `base` from `slave`.
    Returns a list of raw uint16 values.  Raises on timeout or bad response."""
    serial  = b"AB1234G567"
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([slave, 0x03]) + base.to_bytes(2, "big") + count.to_bytes(2, "big")
    crc     = _crc16(inner)
    payload = serial + padding + inner + crc
    length  = len(payload) + 2
    frame   = b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload

    s = socket.create_connection((INVERTER_IP, INVERTER_PORT), timeout=timeout)
    s.settimeout(timeout)
    try:
        s.sendall(frame)
        buf = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf.extend(chunk)
            for f in _pop_data_frames(buf):
                if len(f) < 44 or f[7] != 0x02:
                    continue
                if f[27] != 0x03:          # must be HR read response
                    continue
                rx_base  = (f[38] << 8) | f[39]
                rx_count = (f[40] << 8) | f[41]
                if rx_base != base or rx_count != count:
                    continue
                if len(f) < 42 + count * 2:
                    continue
                return [(f[42 + i*2] << 8) | f[43 + i*2] for i in range(count)]
    finally:
        s.close()
    raise TimeoutError(f"HR read timeout: slave=0x{slave:02x} base={base} count={count}")


_DONGLE_BUSY_CODE  = 0x43  # GivEnergy Modbus exception: dongle handling another request
_WRITE_MAX_ATTEMPTS = 7    # 1 initial attempt + 6 retries (matches psylsph behaviour)


def _hr_write(slave: int, reg: int, value: int, timeout: float = 5.0,
              attempts: int = _WRITE_MAX_ATTEMPTS) -> None:
    """Write a single holding register.  Verifies the echo response.
    Retries on exception code 67 (dongle busy) up to `attempts` times (default 7,
    as the manual controls use). The scheduler passes attempts=1 to FAIL FAST: the
    sustained busy-retry hammering is what disrupted the Gen2 listen stream, so it
    aborts on busy and lets the 15s re-queue try again instead.
    Raises on timeout, echo mismatch, or exhausted retries."""
    serial  = b"AB1234G567"
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([slave, 0x06]) + reg.to_bytes(2, "big") + value.to_bytes(2, "big")
    crc     = _crc16(inner)
    payload = serial + padding + inner + crc
    length  = len(payload) + 2
    frame   = b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload

    for attempt in range(attempts):
        if attempt:
            log.warning("HR write: dongle busy, retrying in 2s (attempt %d/%d) …",
                        attempt + 1, attempts)
            time.sleep(2)
        s = socket.create_connection((INVERTER_IP, INVERTER_PORT), timeout=timeout)
        s.settimeout(timeout)
        try:
            s.sendall(frame)
            buf = bytearray()
            deadline = time.time() + timeout
            busy = False
            while time.time() < deadline:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                for f in _pop_data_frames(buf):
                    if len(f) < 29 or f[7] != 0x02:
                        continue
                    # Exception response: inner_func = request func | 0x80
                    if f[27] == 0x86 and f[28] == _DONGLE_BUSY_CODE:
                        busy = True
                        break
                    if len(f) < 42 or f[27] != 0x06:
                        continue
                    echo_reg = (f[38] << 8) | f[39]
                    echo_val = (f[40] << 8) | f[41]
                    if echo_reg != reg or echo_val != value:
                        raise ValueError(
                            f"HR write echo mismatch: reg={echo_reg} val={echo_val} "
                            f"(expected reg={reg} val={value})")
                    return  # success
                if busy:
                    break
        finally:
            s.close()
        if not busy:
            raise TimeoutError(f"HR write timeout: slave=0x{slave:02x} reg={reg} val={value}")
    raise OSError(f"HR write: dongle still busy after {attempts} attempt(s) "
                  f"(reg={reg} val={value})")


# ── Generation / profile detection ────────────────────────────────────────────
#
# Profile drives which register map and slot count to use.  Detection reads
# HR[0] (device_type_code) and HR[21] (arm_firmware_version) from the inverter
# and classifies using the same logic as the givenergy-modbus library.
#
# Profiles:
#   single_phase_2slot     – Gen1/Gen2, Gen3 with ARM fw ≤302
#   single_phase_extended  – Gen3 (ARM fw >302), Gen3+, Gen4, HV Gen3, AIO Hybrid
#   three_phase_aio        – three-phase / AIO Commercial / All-in-One
#   unknown                – detection failed or unrecognised DTC

_inverter_profile: str = ""     # "" = not yet detected
_inverter_model:   str = ""     # human-readable model name
_detect_lock = threading.Lock()

# DTC first-byte → profile (before firmware disambiguation)
_DTC_PREFIX_PROFILE = {
    "2": "single_phase_2slot",    # HYBRID family — fw disambiguates Gen2/Gen3
    "3": "single_phase_2slot",    # AC single-phase
    "4": "three_phase_aio",       # three-phase hybrid
    "5": "single_phase_2slot",    # EMS
    "6": "three_phase_aio",       # three-phase AC
    "7": "single_phase_2slot",    # GATEWAY
    "8": "three_phase_aio",       # ALL_IN_ONE family
}

# Specific two-digit DTC prefixes that override the coarse map
_DTC_TWO_PREFIX_PROFILE = {
    "21": "single_phase_2slot",   # POLAR
    "41": "three_phase_aio",      # AIO Commercial
    "51": "single_phase_2slot",   # EMS Commercial
    "70": "gateway_aio",          # Gateway AIO (DTC 0x70xx) — live data at IR base=1600
    "81": "single_phase_extended",# HV Gen3 (single-phase HV)
    "82": "three_phase_aio",      # All-in-One Hybrid
    "83": "single_phase_extended",# Gen4
}

# ARM firmware century → generation for DTC prefix "20" (HYBRID family)
# Century 3xx → Gen3, 8xx/9xx → Gen2, else → Gen1 (2-slot)
_FW_CENTURY_GEN = {3: "gen3", 8: "gen2", 9: "gen2"}


_DTC_TWO_MODEL_NAME = {
    "21": "Polar",
    "41": "AIO Commercial",
    "51": "EMS Commercial",
    "70": "Gateway AIO",
    "81": "Hybrid HV Gen3",
    "82": "All-in-One Hybrid",
    "83": "Hybrid Gen4",
}
_DTC_ONE_MODEL_NAME = {
    "3": "AC Inverter",
    "4": "Three-Phase Hybrid",
    "5": "EMS",
    "6": "Three-Phase AC",
    "7": "Gateway",
    "8": "All-in-One",
}

def _classify_model(raw_dtc: int, arm_fw: int):
    """Return (profile, model_name) from raw HR[0] and HR[21] values."""
    dtc_hex = f"{raw_dtc:04x}"
    two     = dtc_hex[:2]
    one     = dtc_hex[:1]

    if two in _DTC_TWO_PREFIX_PROFILE:
        profile = _DTC_TWO_PREFIX_PROFILE[two]
        model   = _DTC_TWO_MODEL_NAME.get(two, f"DTC-{two.upper()}")
    elif one == "2":
        # HYBRID family: firmware century determines Gen1/Gen2/Gen3
        gen = _FW_CENTURY_GEN.get(arm_fw // 100, "gen1")
        if gen == "gen3":
            profile = "single_phase_extended" if arm_fw > 302 else "single_phase_2slot"
            model   = "Hybrid Gen3"
        elif gen == "gen2":
            profile = "single_phase_2slot"
            model   = "Hybrid Gen2"
        else:
            profile = "single_phase_2slot"
            model   = "Hybrid Gen1"
    elif one in _DTC_PREFIX_PROFILE:
        profile = _DTC_PREFIX_PROFILE[one]
        model   = _DTC_ONE_MODEL_NAME.get(one, f"DTC-{one.upper()}")
    else:
        profile = "unknown"
        model   = f"Unknown (DTC 0x{raw_dtc:04x})"

    return profile, model


def _detect_inverter() -> tuple:
    """Return (slave, profile, model_name), detecting and caching on first call.

    Uses the listener's observed slave as a hint for which address to poll.
    Falls back to an active IR probe on 0x11 if the listener hasn't seen a
    frame yet, so the control page is immediately usable at startup.
    """
    global _inverter_profile, _inverter_model

    with _detect_lock:
        if _inverter_profile:
            # Already detected — return cached values.
            slave = _inverter_slave if _inverter_slave else 0x11
            return slave, _inverter_profile, _inverter_model

        # Choose slave: prefer the one the listener has already seen;
        # fall back to 0x11 (the official inverter address for all generations).
        slave = _inverter_slave if _inverter_slave else 0x11

        # If the listen loop is active (_inverter_slave known), it will have
        # already triggered _detect_on_socket() on its own socket. Wait up to
        # 4 seconds for that to complete before falling back to a new connection.
        # This avoids two concurrent TCP connections which confuse the dongle.
        if _inverter_slave:
            for _ in range(8):
                if _inverter_profile:
                    break
                time.sleep(0.5)

        if _inverter_profile:
            # In-socket detection already populated cache — use it.
            return slave, _inverter_profile, _inverter_model

        # Listen loop not yet active or detection timed out — open a fresh
        # short-lived connection (only safe at startup before listen loop runs).
        try:
            regs    = _hr_read(slave, 0, 22, timeout=5.0)
            raw_dtc = regs[0]
            arm_fw  = regs[21]
            profile, model = _classify_model(raw_dtc, arm_fw)
            log.warning(
                "Inverter detected (fresh connection): "
                "slave=0x%02x DTC=0x%04x ARM_fw=%d → %s (%s)",
                slave, raw_dtc, arm_fw, model, profile)
        except Exception as exc:
            log.warning("Inverter detection failed: %s", exc)
            profile, model = "unknown", "Detection failed"

        _inverter_profile = profile
        _inverter_model   = model
        return slave, profile, model


# ── Register maps ─────────────────────────────────────────────────────────────
#
# All register numbers are raw HR indices as per the GivEnergy Modbus spec.
# Slot tuples are (start_hr, end_hr).  SOC target lists are indexed by slot
# number (0-based internally, displayed as 1-based).

# Registers common to all single-phase profiles
_HR = {
    "ENABLE_CHARGE":         96,
    "ENABLE_DISCHARGE":      59,
    "BATTERY_POWER_MODE":    27,
    "CHARGE_TARGET_SOC":    116,   # global target; Gen3 also has per-slot at HR 242+
    "BATTERY_SOC_RESERVE":  110,
    "BATTERY_CHARGE_LIMIT": 111,
    "BATTERY_DISCHARGE_LIMIT": 112,
    "BATTERY_POWER_RESERVE": 114,
}

# Slot time-pair registers: (start_hr, end_hr).
# EXTENDED_SLOTS keeps slots 1+2 at the same addresses as 2-slot; slots 3-10
# are new registers.  Confirmed from the givenergy-modbus EXTENDED_SLOTS map.
_CHARGE_SLOT_HR = [
    (94, 95),    # slot 1
    (31, 32),    # slot 2  — same address in both 2-slot and extended profiles
    (246, 247),  # slot 3  } extended (Gen3/Gen4/HV-Gen3) only
    (249, 250),  # slot 4  }
    (252, 253),  # slot 5  }
    (255, 256),  # slot 6  }
    (258, 259),  # slot 7  }
    (261, 262),  # slot 8  }
    (264, 265),  # slot 9  }
    (267, 268),  # slot 10 }
]
_DISCHARGE_SLOT_HR = [
    (56, 57),    # slot 1
    (44, 45),    # slot 2
    (276, 277),  # slot 3  } extended only
    (279, 280),  # slot 4  }
    (282, 283),  # slot 5  }
    (285, 286),  # slot 6  }
    (288, 289),  # slot 7  }
    (291, 292),  # slot 8  }
    (294, 295),  # slot 9  }
    (297, 298),  # slot 10 }
]

# Per-slot charge/discharge target SOC registers (extended profile only).
# Pattern: slot N → base + (N-1)*3  where base=242/272.
# Explicitly listed to be auditable rather than computed.
_CHARGE_SOC_HR    = [242, 245, 248, 251, 254, 257, 260, 263, 266, 269]
_DISCHARGE_SOC_HR = [272, 275, 278, 281, 284, 287, 290, 293, 296, 299]

# Three-phase / AIO slot registers (shadow the single-phase addresses).
# Only 2 slots confirmed for three-phase — no extended map exists yet.
_CHARGE_SLOT_HR_3PH    = [(1113, 1114), (1115, 1116)]
_DISCHARGE_SLOT_HR_3PH = [(1118, 1119), (1120, 1121)]
_HR_3PH_CHARGE_TARGET  = 1111   # shadows HR 116

# Number of slots per profile
_SLOT_COUNT = {
    "single_phase_2slot":    2,
    "single_phase_extended": 10,
    "three_phase_aio":       2,
    "gateway_aio":           2,
}

# Scheduler's reserved scratch slot (1-based) per profile — the LAST slot, so
# slot 1 stays free for other integrations (Predbat, GivEnergy app). The block
# engine rewrites this slot each Charge/Export block and clears it when idle.
# three_phase_aio is omitted on purpose — control is read-only there, so the
# scheduler is disabled for it.
_SCHED_SLOT = {
    "single_phase_2slot":    2,
    "single_phase_extended": 10,
    "gateway_aio":           2,
}


# ── State reader ──────────────────────────────────────────────────────────────

def _read_control_state() -> dict:
    """Read current inverter control settings.  Returns a structured dict
    including profile and slot arrays so the frontend can render correctly."""
    slave, profile, model = _detect_inverter()

    if profile == "unknown":
        return {"ok": False,
                "error": f"Inverter model not recognised — cannot read settings ({model}). "
                          "Please send a capture log.",
                "profile": profile, "model": model}
    # gateway_aio uses the same HR 0-119 register layout as single_phase_2slot
    # (confirmed from wire capture). Treat as 2-slot for HR reads.

    # ── Read holding registers ────────────────────────────────────────────────
    regs_0   = _hr_read(slave, 0,   60)   # HR  0-59
    regs_60  = _hr_read(slave, 60,  60)   # HR 60-119

    def hr(n):
        if n < 60:   return regs_0[n]
        if n < 120:  return regs_60[n - 60]
        raise IndexError(f"HR {n} not in base read range")

    if profile == "single_phase_extended":
        regs_240 = _hr_read(slave, 240, 60)   # HR 240-299
        def hr_ext(n):
            if 240 <= n < 300: return regs_240[n - 240]
            return hr(n)
    else:
        def hr_ext(n):
            return hr(n)

    if profile == "three_phase_aio":
        # Three-phase slot registers live in the 1100 range
        regs_1100 = _hr_read(slave, 1100, 22)   # HR 1100-1121
        def hr_3ph(n):
            if 1100 <= n < 1122: return regs_1100[n - 1100]
            return hr(n)

    # ── Build slot arrays ─────────────────────────────────────────────────────
    num_slots = _SLOT_COUNT[profile]

    def read_slot(slot_hrs, soc_hrs, idx):
        start_hr, end_hr = slot_hrs[idx]
        if profile == "three_phase_aio":
            start = hr_3ph(start_hr)
            end   = hr_3ph(end_hr)
        else:
            start = hr_ext(start_hr)
            end   = hr_ext(end_hr)
        slot = {"start": _bcd_to_hhmm(start), "end": _bcd_to_hhmm(end)}
        if soc_hrs and profile == "single_phase_extended":
            slot["target_soc"] = hr_ext(soc_hrs[idx])
        return slot

    charge_slots = [
        read_slot(_CHARGE_SLOT_HR, _CHARGE_SOC_HR, i)
        for i in range(num_slots)
    ]
    discharge_slots = [
        read_slot(_DISCHARGE_SLOT_HR, _DISCHARGE_SOC_HR, i)
        for i in range(num_slots)
    ]

    if profile == "three_phase_aio":
        charge_slots    = [read_slot(_CHARGE_SLOT_HR_3PH,    None, i) for i in range(2)]
        discharge_slots = [read_slot(_DISCHARGE_SLOT_HR_3PH, None, i) for i in range(2)]
        charge_target   = hr_3ph(_HR_3PH_CHARGE_TARGET)
    else:
        charge_target = hr(_HR["CHARGE_TARGET_SOC"])

    # Serial number: HR[13-17], each register = 2 ASCII chars (big-endian)
    serial = "".join(
        chr((regs_0[i] >> 8) & 0xFF) + chr(regs_0[i] & 0xFF)
        for i in range(13, 18)
    ).strip("\x00").strip()

    result = {
        "ok":               True,
        "profile":          profile,
        "model":            model,
        "serial":           serial,
        "writable":         profile not in ("three_phase_aio",),  # gateway_aio writes now confirmed
        "enable_charge":    bool(hr(_HR["ENABLE_CHARGE"])),
        "enable_discharge": bool(hr(_HR["ENABLE_DISCHARGE"])),
        "battery_power_mode":   hr(_HR["BATTERY_POWER_MODE"]),
        "charge_target_soc":    charge_target,
        "soc_reserve":          hr(_HR["BATTERY_SOC_RESERVE"]),
        "charge_limit":         hr(_HR["BATTERY_CHARGE_LIMIT"]),
        "discharge_limit":      hr(_HR["BATTERY_DISCHARGE_LIMIT"]),
        "power_reserve":        hr(_HR["BATTERY_POWER_RESERVE"]),
        "charge_slots":         charge_slots,
        "discharge_slots":      discharge_slots,
        # Power-display config (for the UI to convert % limits to watts)
        "power_units":     POWER_UNITS,
        "max_charge_w":    MAX_CHARGE_W,
        "max_discharge_w": MAX_DISCHARGE_W,
    }
    return result


# ── Control log ───────────────────────────────────────────────────────────────

def _log_control(command, params, success, message=""):
    with _db() as conn:
        conn.execute(
            "INSERT INTO control_log (ts,command,params,success,message) VALUES (?,?,?,?,?)",
            (int(time.time()), command, json.dumps(params) if params else None,
             int(success), message))
        conn.commit()


# ── Control writer ────────────────────────────────────────────────────────────

def _execute_control(command: str, params: dict) -> dict:
    """Write a control command to the inverter via library-free HR writes.
    Returns {ok, message}."""
    slave, profile, model = _detect_inverter()

    if profile == "unknown":
        return {"ok": False, "message": f"Inverter not recognised ({model}) — writes disabled."}

    if profile == "three_phase_aio":
        return {"ok": False,
                "message": "Write control is not yet confirmed for three-phase/AIO inverters. "
                           "Please send a capture log so register maps can be verified."}
    # gateway_aio: HR register layout confirmed identical to single_phase_2slot
    # from David's wire capture (HR[27]=battery_power_mode, HR[26]=6000W, HR[30]=0x11 etc.)
    # Fall through to _do_control with profile treated as 2-slot.

    try:
        msg = _do_control(slave, profile, command, params)
        _log_control(command, params, True, msg)
        log.warning("Control: %s", msg)
        return {"ok": True, "message": msg}
    except Exception as exc:
        err = str(exc)
        _log_control(command, params, False, err)
        log.error("Control failed %s: %s", command, err)
        return {"ok": False, "message": err}


def _do_control(slave: int, profile: str, command: str, params: dict) -> str:
    """Dispatch control command to raw HR writes.  Raises on failure."""

    def wr(reg, val):
        _hr_write(slave, reg, val)

    # ── Simple enable/disable toggles ─────────────────────────────────────────
    if command == "enable_charge":
        wr(_HR["ENABLE_CHARGE"], 1);    return "Charge enabled"
    if command == "disable_charge":
        wr(_HR["ENABLE_CHARGE"], 0);    return "Charge disabled"
    if command == "enable_discharge":
        wr(_HR["ENABLE_DISCHARGE"], 1); return "Discharge enabled"
    if command == "disable_discharge":
        wr(_HR["ENABLE_DISCHARGE"], 0); return "Discharge disabled"

    # ── Mode presets ──────────────────────────────────────────────────────────
    if command == "set_mode_dynamic":
        wr(_HR["BATTERY_POWER_MODE"],   1)
        wr(_HR["BATTERY_SOC_RESERVE"],  4)
        wr(_HR["ENABLE_DISCHARGE"],     0)
        return "Mode set to Dynamic (Eco)"

    if command == "set_mode_storage":
        wr(_HR["ENABLE_DISCHARGE"],     1)
        wr(_HR["BATTERY_POWER_MODE"],   1)
        return "Mode set to Storage"

    # ── Charge slot ───────────────────────────────────────────────────────────
    if command == "set_charge_slot":
        slot = int(params.get("slot", 1))
        max_slots = _SLOT_COUNT[profile]
        if not 1 <= slot <= max_slots:
            raise ValueError(f"Slot {slot} out of range for this inverter (max {max_slots})")
        start_hr, end_hr = _CHARGE_SLOT_HR[slot - 1]
        wr(start_hr, _hhmm_to_bcd(params["start"]))
        wr(end_hr,   _hhmm_to_bcd(params["end"]))
        return f"Charge slot {slot} set to {params['start']}–{params['end']}"

    # ── Discharge slot ────────────────────────────────────────────────────────
    if command == "set_discharge_slot":
        slot = int(params.get("slot", 1))
        max_slots = _SLOT_COUNT[profile]
        if not 1 <= slot <= max_slots:
            raise ValueError(f"Slot {slot} out of range for this inverter (max {max_slots})")
        start_hr, end_hr = _DISCHARGE_SLOT_HR[slot - 1]
        wr(start_hr, _hhmm_to_bcd(params["start"]))
        wr(end_hr,   _hhmm_to_bcd(params["end"]))
        return f"Discharge slot {slot} set to {params['start']}–{params['end']}"

    # ── Per-slot SOC target (extended profile only) ───────────────────────────
    if command == "set_charge_slot_soc":
        if profile != "single_phase_extended":
            raise ValueError("Per-slot charge SOC target is only available on Gen3/Gen4 inverters")
        slot = int(params.get("slot", 1))
        if not 1 <= slot <= 10:
            raise ValueError(f"Slot {slot} out of range (max 10)")
        val = max(4, min(100, int(params["value"])))
        wr(_CHARGE_SOC_HR[slot - 1], val)
        return f"Charge slot {slot} target SOC set to {val}%"

    if command == "set_discharge_slot_soc":
        if profile != "single_phase_extended":
            raise ValueError("Per-slot discharge SOC target is only available on Gen3/Gen4 inverters")
        slot = int(params.get("slot", 1))
        if not 1 <= slot <= 10:
            raise ValueError(f"Slot {slot} out of range (max 10)")
        val = max(4, min(100, int(params["value"])))
        wr(_DISCHARGE_SOC_HR[slot - 1], val)
        return f"Discharge slot {slot} target SOC set to {val}%"

    # ── Scalar settings ───────────────────────────────────────────────────────
    if command == "set_charge_target_soc":
        val = max(4, min(100, int(params["value"])))
        wr(_HR["CHARGE_TARGET_SOC"], val)
        return f"Charge target SOC set to {val}%"

    if command == "set_soc_reserve":
        val = max(4, min(100, int(params["value"])))
        wr(_HR["BATTERY_SOC_RESERVE"], val)
        return f"SOC reserve set to {val}%"

    if command == "set_charge_limit":
        val = max(0, min(50, int(params["value"])))
        wr(_HR["BATTERY_CHARGE_LIMIT"], val)
        return f"Charge power limit set to {val}%"

    if command == "set_discharge_limit":
        val = max(0, min(50, int(params["value"])))
        wr(_HR["BATTERY_DISCHARGE_LIMIT"], val)
        return f"Discharge power limit set to {val}%"

    if command == "set_discharge_mode":
        val = max(0, min(1, int(params["value"])))
        wr(_HR["BATTERY_POWER_MODE"], val)
        label = "Max Power / Export" if val == 0 else "Demand only"
        return f"Discharge mode set to {label}"

    raise ValueError(f"Unknown command: {command}")


# ── Scheduler engine (app-held 48 half-hour block reconciler) ─────────────────
# Design in BACKLOG.md. The decision logic (_block_contains / _sched_desired_state /
# _sched_compute_writes) is pure and unit-tested. The scheduler thread evaluates the
# block every 15s and applies changes itself (_sched_apply) using the SAME pattern the
# manual controls use — _hr_write to the detected slave (0x32 on Gen2) from the scheduler
# thread while the listen loop keeps receiving concurrently. Writes are PACED and
# FAIL-FAST on 'dongle busy' (attempts=1): the old 75s drops came from sustained busy-
# retry hammering, not the writing. Failures retry on the next 15s cycle. Master on only.

# Action precedence when several rules cover the same block.
_SCHED_PRECEDENCE = {"export": 3, "charge": 2, "hold": 1}

def _hhmm_to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

def _block_contains(start: str, end: str, t_min: int) -> bool:
    """Does the [start, end) window contain minute-of-day t_min?
    Supports windows that wrap past midnight (start > end)."""
    s, e = _hhmm_to_min(start), _hhmm_to_min(end)
    if s == e:
        return False
    if s < e:
        return s <= t_min < e
    return t_min >= s or t_min < e          # wraps midnight

def _sched_desired_state(rules: list, weekday: int, t_min: int) -> dict:
    """Pure: pick the winning action for this moment, else baseline.
    `rules` are /api/schedules-shaped dicts; weekday Mon=0 … Sun=6 (matches days_mask)."""
    winner = None
    for r in rules:
        if not r.get("enabled", True):
            continue
        if not (r["days_mask"] & (1 << weekday)):
            continue
        if not _block_contains(r["start"], r["end"], t_min):
            continue
        if winner is None or _SCHED_PRECEDENCE[r["action"]] > _SCHED_PRECEDENCE[winner["action"]]:
            winner = r
    if winner is None:
        return {"mode": "baseline"}
    if winner["action"] == "charge":
        return {"mode": "charge", "target_soc": winner["target_soc"],
                "start": winner["start"], "end": winner["end"]}
    if winner["action"] == "export":
        return {"mode": "export", "start": winner["start"], "end": winner["end"]}
    return {"mode": "hold"}

def _sched_load_rules() -> list:
    with _db() as conn:
        rows = conn.execute(
            "SELECT action, start_hhmm, end_hhmm, days_mask, target_soc "
            "FROM schedules WHERE enabled=1").fetchall()
    return [{"enabled": True, "action": r["action"], "start": r["start_hhmm"],
             "end": r["end_hhmm"], "days_mask": r["days_mask"],
             "target_soc": r["target_soc"]} for r in rows]

def _sched_compute_writes(desired: dict):
    """Pure-ish: translate a desired-state dict into (slave, [(reg,val),...], summary).
    Does NO socket I/O beyond the cached _detect_inverter() — the listen loop performs
    the actual writes on its own socket. Uses the profile's reserved (last) slot as
    scratch and clears it when idle, so slot 1 stays free for other integrations.
    The register writes are absolute sets, so re-applying the whole list is idempotent
    (a partially-applied list self-heals on the next retry). Raises on unsupported HW."""
    slave, profile, model = _detect_inverter()
    if profile not in _SCHED_SLOT:
        raise RuntimeError(f"Scheduler is not supported on this inverter ({model})")
    # Write to the DETECTED slave (0x32 on Gen2) with the listen loop OPEN — the exact
    # path the manual control buttons use and which is confirmed to work. (0x32 only
    # answers while its broadcast/listen stream is live, so we must NOT close listen.)
    slot = _SCHED_SLOT[profile]                       # 1-based
    c_start, c_end = _CHARGE_SLOT_HR[slot - 1]
    d_start, d_end = _DISCHARGE_SLOT_HR[slot - 1]
    bcd = _hhmm_to_bcd
    mode = desired["mode"]

    if mode == "charge":
        w = [(_HR["CHARGE_TARGET_SOC"], desired["target_soc"]),
             (c_start, bcd(desired["start"])), (c_end, bcd(desired["end"])),
             (_HR["ENABLE_CHARGE"], 1),
             (d_start, 0), (d_end, 0)]
        summary = f"Charge to {desired['target_soc']}% ({desired['start']}–{desired['end']})"
    elif mode == "export":
        w = [(d_start, bcd(desired["start"])), (d_end, bcd(desired["end"])),
             (_HR["BATTERY_POWER_MODE"], 0),          # 0 = max power / export
             (_HR["ENABLE_DISCHARGE"], 1),
             (c_start, 0), (c_end, 0)]
        summary = f"Export ({desired['start']}–{desired['end']})"
    elif mode == "hold":
        w = [(_HR["ENABLE_CHARGE"], 0), (_HR["ENABLE_DISCHARGE"], 0),
             (c_start, 0), (c_end, 0), (d_start, 0), (d_end, 0)]
        summary = "Hold charge (no discharge)"
    elif mode == "cleanup":                           # master switched off
        w = [(c_start, 0), (c_end, 0), (d_start, 0), (d_end, 0), (_HR["ENABLE_CHARGE"], 0)]
        summary = "Off — cleared scratch slots"
    else:                                             # baseline
        w = [(c_start, 0), (c_end, 0), (d_start, 0), (d_end, 0), (_HR["ENABLE_CHARGE"], 0)]
        if SCHEDULER_BASELINE == "storage":
            w.append((_HR["ENABLE_DISCHARGE"], 0))
            summary = "Baseline: Storage (hold charge)"
        else:
            w += [(_HR["ENABLE_DISCHARGE"], 1), (_HR["BATTERY_POWER_MODE"], 1)]  # 1 = demand
            summary = "Baseline: Eco"
    return slave, w, summary


_SCHED_WRITE_GAP   = 1.0     # seconds between writes — the Gen2 dongle needs a moment
                            # to finish one write before accepting the next (back-to-back
                            # writes collide → 'busy' on the 2nd).
_sched_applied_sig = None
_sched_was_enabled = False
_CLEANUP_DESIRED   = {"mode": "cleanup"}
_CLEANUP_SIG       = json.dumps(_CLEANUP_DESIRED, sort_keys=True)

def _sched_apply(desired: dict) -> None:
    """Apply a desired state the SAME way a manual control button does — the pattern
    confirmed to work on the Gen2: _hr_write to the detected slave (0x32) from THIS
    (scheduler) thread, while the listen loop keeps receiving CONCURRENTLY in its own
    thread. That matters: the 0x32 broadcast slave only services writes while its listen
    stream is actively being read, so the write must not block the listen recv (which is
    why doing it inside the listen loop failed). Writes are PACED (_SCHED_WRITE_GAP apart)
    and FAIL FAST on busy (attempts=1) — the sustained busy-retry hammering is what used
    to cause 75s drops. On any failure _sched_applied_sig is left unchanged so the 15s
    loop re-applies (writes are idempotent absolute sets, so a partial apply self-heals)."""
    global _sched_applied_sig
    try:
        slave, writes, summary = _sched_compute_writes(desired)
        for i, (reg, val) in enumerate(writes):
            if i:
                time.sleep(_SCHED_WRITE_GAP)
            _hr_write(slave, reg, val, attempts=1)
    except Exception as exc:
        log.warning("Scheduler apply failed (will retry): %s", exc)
        return                                        # applied_sig unchanged → retried
    _sched_applied_sig = json.dumps(desired, sort_keys=True)
    _log_control("scheduler", desired, True, "Scheduler: " + summary)
    log.warning("Scheduler applied: %s", summary)

def _scheduler_loop():
    """Evaluate the active block every 15s and, on a change (or at startup so a reboot
    self-corrects within one block), apply it via _sched_apply — in THIS thread, so the
    writes run concurrently with the listen loop (the pattern that works on the Gen2).
    Master-off triggers a one-shot cleanup, then hands the inverter back."""
    global _sched_was_enabled, _sched_applied_sig
    while True:
        try:
            want = None
            if SCHEDULER_ENABLED:
                now = time.localtime()
                block_min = ((now.tm_hour * 60 + now.tm_min) // 30) * 30
                want = _sched_desired_state(_sched_load_rules(), now.tm_wday, block_min)
                _sched_was_enabled = True
            elif _sched_was_enabled:
                # Master just switched off: apply cleanup once, then hand off (want=None).
                if _sched_applied_sig == _CLEANUP_SIG:
                    _sched_was_enabled = False
                    _sched_applied_sig = None         # re-enabling later re-applies fresh
                else:
                    want = _CLEANUP_DESIRED
            # Only apply once the inverter is detected ON THE LISTEN SOCKET. Calling into
            # _detect_inverter before that triggers a fresh-socket probe that collides with
            # the live listen stream, fails, and caches an 'unknown' result permanently.
            if (want is not None
                    and json.dumps(want, sort_keys=True) != _sched_applied_sig
                    and _inverter_profile in _SCHED_SLOT):
                _sched_apply(want)
        except Exception as exc:
            log.error("Scheduler loop: %s", exc)
        time.sleep(15)


# ── Flask app ──────────────────────────────────────────────────────────────────
# Passing instance_path explicitly stops Flask from calling its auto-discovery
# (auto_find_instance_path → pkgutil.get_loader), which an older Flask removed-
# API path would crash on under Python 3.14. Belt-and-braces alongside the
# flask>=3.1.3 pin in the installers.
app = Flask(__name__,
            static_folder=str(Path(__file__).parent),
            instance_path=str(Path(__file__).parent))

@app.route("/api/data")
def api_data():
    with _lock:
        if not _cached:
            return jsonify({"ok": False, "error": _error or "Starting up..."}), 503
        data = dict(_cached)
    # Scheduler on/off is a status flag (not a control), so it rides on the
    # unauthenticated live feed to drive the header indicator.
    data["scheduler_active"] = SCHEDULER_ENABLED
    return jsonify(data)

@app.route("/api/control", methods=["GET"])
def get_control():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    try:
        return jsonify(_read_control_state())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/control", methods=["POST"])
def post_control():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    data    = request.get_json(force=True) or {}
    command = data.get("command", "")
    params  = data.get("params", {})
    return jsonify(_execute_control(command, params))

# ── Scheduler API (app-held 48-block engine — see BACKLOG.md) ─────────────────
# Step 1: storage + CRUD only. The block-execution thread arrives in step 2.

_SCHED_ACTIONS = ("charge", "hold", "export")

def _snap_hhmm(s: str) -> str:
    """Validate 'HH:MM' and snap to the 30-minute grid (00 or 30)."""
    h, m = str(s).split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time: {s}")
    m = 0 if m < 30 else 30
    return f"{h:02d}:{m:02d}"

def _rule_row(r) -> dict:
    return {
        "id":         r["id"],
        "enabled":    bool(r["enabled"]),
        "action":     r["action"],
        "start":      r["start_hhmm"],
        "end":        r["end_hhmm"],
        "days_mask":  r["days_mask"],
        "target_soc": r["target_soc"],
    }

def _validate_rule(data: dict) -> dict:
    """Validate/normalise an incoming rule. Returns clean field dict or raises."""
    action = str(data.get("action", "")).lower()
    if action not in _SCHED_ACTIONS:
        raise ValueError(f"Unknown action '{action}' (charge|hold|export)")
    start = _snap_hhmm(data.get("start", ""))
    end   = _snap_hhmm(data.get("end", ""))
    if start == end:
        raise ValueError("The schedule runs in 30-minute blocks — the end must be at "
                         "least one block after the start (e.g. 12:30–13:00).")
    days_mask = int(data.get("days_mask", 127))
    if not 1 <= days_mask <= 127:
        raise ValueError("days_mask must select at least one day (1–127)")
    target_soc = None
    if action == "charge":
        target_soc = max(4, min(100, int(data.get("target_soc", 100))))
    return {"action": action, "start": start, "end": end,
            "days_mask": days_mask, "target_soc": target_soc}

@app.route("/api/schedules", methods=["GET"])
def get_schedules():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    with _db() as conn:
        rows = [_rule_row(r) for r in conn.execute(
            "SELECT * FROM schedules ORDER BY start_hhmm, id")]
    return jsonify({
        "ok":            True,
        "master_enabled": SCHEDULER_ENABLED,
        "baseline":       SCHEDULER_BASELINE,
        "rules":          rows,
    })

@app.route("/api/schedules", methods=["POST"])
def save_schedule():
    """Create (no id) or update (id present) a single rule."""
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    data = request.get_json(force=True) or {}
    try:
        clean = _validate_rule(data)
    except (ValueError, KeyError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    enabled = 1 if data.get("enabled", True) else 0
    rid = data.get("id")
    with _db() as conn:
        if rid:
            conn.execute(
                "UPDATE schedules SET enabled=?, action=?, start_hhmm=?, end_hhmm=?, "
                "days_mask=?, target_soc=? WHERE id=?",
                (enabled, clean["action"], clean["start"], clean["end"],
                 clean["days_mask"], clean["target_soc"], int(rid)))
        else:
            cur = conn.execute(
                "INSERT INTO schedules (enabled, action, start_hhmm, end_hhmm, "
                "days_mask, target_soc, created) VALUES (?,?,?,?,?,?,?)",
                (enabled, clean["action"], clean["start"], clean["end"],
                 clean["days_mask"], clean["target_soc"], int(time.time())))
            rid = cur.lastrowid
        conn.commit()
    return jsonify({"ok": True, "id": rid})

@app.route("/api/schedules/<int:rid>", methods=["DELETE"])
def delete_schedule(rid):
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    with _db() as conn:
        conn.execute("DELETE FROM schedules WHERE id=?", (rid,))
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/schedules/config", methods=["POST"])
def save_schedule_config():
    """Set the master on/off switch and the baseline mode."""
    global SCHEDULER_ENABLED, SCHEDULER_BASELINE
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    data = request.get_json(force=True) or {}
    cfg  = configparser.ConfigParser()
    cfg.read(Path(__file__).parent / "config.ini")
    if not cfg.has_section("scheduler"):
        cfg.add_section("scheduler")
    if "master_enabled" in data:
        SCHEDULER_ENABLED = bool(data["master_enabled"])
        cfg.set("scheduler", "enabled", "yes" if SCHEDULER_ENABLED else "no")
    if "baseline" in data:
        base = str(data["baseline"]).lower()
        if base in _SCHED_BASELINES:
            SCHEDULER_BASELINE = base
            cfg.set("scheduler", "baseline", base)
    with open(Path(__file__).parent / "config.ini", "w") as f:
        cfg.write(f)
    return jsonify({"ok": True,
                    "master_enabled": SCHEDULER_ENABLED,
                    "baseline":       SCHEDULER_BASELINE})

@app.route("/api/logs")
def get_logs():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    limit = min(500, max(1, int(request.args.get("limit", 100))))
    out = []
    with _db() as conn:
        for r in conn.execute(
                "SELECT ts, command, params, success, message FROM control_log "
                "ORDER BY ts DESC LIMIT ?", (limit,)):
            out.append({
                "ts":      r["ts"],
                "type":    "command",
                "kind":    "command" if r["success"] else "error",
                "message": r["message"] or r["command"],
            })
        for r in conn.execute(
                "SELECT ts, kind, message FROM event_log "
                "ORDER BY ts DESC LIMIT ?", (limit,)):
            out.append({
                "ts":      r["ts"],
                "type":    "event",
                "kind":    r["kind"],
                "message": r["message"],
            })
    out.sort(key=lambda x: x["ts"], reverse=True)
    return jsonify(out[:limit])

@app.route("/api/colours")
def get_colours():
    return jsonify(CHART_COLORS)


# ── Battery BMS detail (on-demand, not polled or stored) ──────────────────────

def _signed10(raw: int):
    """Decode a raw uint16 BMS temperature register (0.1 °C, signed).
    Returns None for the empty-slot sentinel (≤ −270 °C)."""
    val = (raw - 65536) if raw > 32767 else raw
    t   = val / 10.0
    return None if t <= -270 else round(t, 1)

def _read_battery_module(s: socket.socket, slave: int) -> "dict | None":
    """Request IR(60, 60) from one LV battery module and decode the response.
    Uses the Gen2/LV-battery CRC convention (MSB-first, no slave in CRC).
    Returns a decoded dict or None if the module does not respond."""
    base, count = 60, 60
    crc     = _bms_crc16(0x04, base, count)
    serial  = b"AB1234G567"
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([slave, 0x04]) + base.to_bytes(2, "big") + count.to_bytes(2, "big")
    payload = serial + padding + inner + crc
    length  = len(payload) + 2
    frame   = b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload

    s.sendall(frame)
    buf      = bytearray()
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf.extend(chunk)
        for f in _pop_data_frames(buf):
            if len(f) < 44 or f[7] != 0x02:
                continue
            if f[26] != slave or f[27] != 0x04:
                continue
            rx_base  = (f[38] << 8) | f[39]
            rx_count = (f[40] << 8) | f[41]
            if rx_base != base or rx_count != count:
                continue
            if len(f) < 42 + count * 2:
                continue

            def g(n: int) -> int:
                o = 42 + n * 2
                return (f[o] << 8) | f[o + 1]

            num_cells = g(37)                       # IR97
            if not 1 <= num_cells <= 24:
                num_cells = 16
            cells_mv = [g(i) for i in range(num_cells)]

            # Temperatures: IR76-79 = offsets 16-19 (4 group readings)
            t_groups = [_signed10(g(16 + i)) for i in range(4)]
            t_groups = [t for t in t_groups if t is not None]

            soc = g(40)                             # IR100
            return {
                "slave":       f"0x{slave:02X}",
                "soc":         soc if 0 <= soc <= 100 else None,
                "cells_mv":    cells_mv,
                "t_groups":    t_groups,
                "t_mosfet":    _signed10(g(21)),    # IR81 BMS PCB temp
                "t_max":       _signed10(g(43)),    # IR103
                "t_min":       _signed10(g(44)),    # IR104
                "cycles":      g(36),               # IR96
                "num_cells":   num_cells,
                "bms_fw":      g(38),               # IR98
                "warning":     g(34),               # IR94 warning bytes (0 = healthy)
                "status_ok":   g(34) == 0,
            }
    return None

@app.route("/api/battery")
def api_battery():
    """On-demand BMS read from battery module(s).  Not polled or stored.
    Opens a fresh socket (the listen loop reconnects within a few seconds).
    Returns per-module cell voltages, temps, SOC, cycles and health flags."""
    if _inverter_profile in ("three_phase_aio", "gateway_aio"):
        return jsonify({
            "ok": False, "unsupported": True,
            "error": "Battery cell detail is not available on AIO/gateway inverters.",
        })
    num     = max(1, NUM_BATTERIES)
    modules = []
    try:
        s = socket.create_connection((INVERTER_IP, INVERTER_PORT), timeout=10)
        s.settimeout(5)
        try:
            for i in range(num):
                slave = 0x32 + i
                m = _read_battery_module(s, slave)
                if m:
                    m["module"] = i + 1
                    modules.append(m)
                else:
                    log.warning("BMS: no response from slave 0x%02x (module %d)", slave, i + 1)
                    break   # further modules are also absent
        finally:
            s.close()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})
    if not modules:
        return jsonify({"ok": False,
                        "error": "No battery modules responded — check num_batteries in settings."})
    return jsonify({"ok": True, "modules": modules})

@app.route("/api/settings", methods=["GET"])
def get_settings():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    return jsonify({
        "inverter_ip":         INVERTER_IP,
        "inverter_port":       INVERTER_PORT,
        "num_batteries":       NUM_BATTERIES,
        "inverter_mode":       INVERTER_MODE,           # configured: auto|poll|listen
        "active_mode":         _active_mode or "starting…",  # resolved at runtime
        "power_units":         POWER_UNITS,             # percent | watts
        "max_charge_w":        MAX_CHARGE_W,
        "max_discharge_w":     MAX_DISCHARGE_W,
        "poll_interval":       POLL_INTERVAL,
        "data_retention_days": DATA_RETENTION_DAYS,
        "weather_configured":  bool(MET_API_KEY and MET_GEOHASH),
        "weather_geohash":     MET_GEOHASH,
        "weather_poll_mins":   WEATHER_POLL_MINS,
        "backup_enabled":      BACKUP_ENABLED,
        "backup_keep_days":    BACKUP_KEEP_DAYS,
        "last_backup":         _last_backup_info()[1] or "none yet",
        "check_for_updates":   CHECK_UPDATES,
        "app_version":         APP_VERSION,
        "chart_colors":        CHART_COLORS,
        # API key is intentionally never returned to the browser
    })

@app.route("/api/settings", methods=["POST"])
def save_settings():
    global INVERTER_IP, INVERTER_PORT, NUM_BATTERIES, POLL_INTERVAL
    global DATA_RETENTION_DAYS, MET_API_KEY, MET_GEOHASH, _last_weather_ts
    global ADMIN_HASH, WEATHER_POLL_MINS
    global POWER_UNITS, MAX_CHARGE_W, MAX_DISCHARGE_W
    global BACKUP_ENABLED, BACKUP_KEEP_DAYS, CHECK_UPDATES, CHART_COLORS

    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401

    data = request.get_json(force=True) or {}
    cfg  = configparser.ConfigParser()
    cfg.read(Path(__file__).parent / "config.ini")

    def _set(section, key, val):
        if not cfg.has_section(section): cfg.add_section(section)
        cfg.set(section, key, str(val))

    if "inverter_ip"   in data:
        INVERTER_IP   = data["inverter_ip"].strip();           _set("inverter","ip",           INVERTER_IP)
    if "inverter_port" in data:
        INVERTER_PORT = max(1,min(65535,int(data["inverter_port"]))); _set("inverter","port",     INVERTER_PORT)
    if "num_batteries" in data:
        NUM_BATTERIES = max(0,min(10,  int(data["num_batteries"])));  _set("inverter","num_batteries", NUM_BATTERIES)
    if "poll_interval" in data:
        POLL_INTERVAL = max(5,min(300, int(data["poll_interval"])));  _set("server",  "poll_interval", POLL_INTERVAL)
    if "data_retention_days" in data:
        DATA_RETENTION_DAYS = max(30, int(data["data_retention_days"])); _set("server","data_retention_days", DATA_RETENTION_DAYS)
    if "power_units" in data:
        POWER_UNITS = "watts" if str(data["power_units"]).lower() == "watts" else "percent"
        _set("inverter", "power_units", POWER_UNITS)
    if "max_charge_w" in data:
        MAX_CHARGE_W = max(100, min(20000, int(data["max_charge_w"])));    _set("inverter", "max_charge_w", MAX_CHARGE_W)
    if "max_discharge_w" in data:
        MAX_DISCHARGE_W = max(100, min(20000, int(data["max_discharge_w"]))); _set("inverter", "max_discharge_w", MAX_DISCHARGE_W)
    if "backup_enabled" in data:
        BACKUP_ENABLED = bool(data["backup_enabled"]); _set("backup", "enabled", "yes" if BACKUP_ENABLED else "no")
    if "backup_keep_days" in data:
        BACKUP_KEEP_DAYS = max(1, min(60, int(data["backup_keep_days"]))); _set("backup", "keep_days", BACKUP_KEEP_DAYS)
    if "check_for_updates" in data:
        CHECK_UPDATES = bool(data["check_for_updates"]); _set("server", "check_for_updates", "yes" if CHECK_UPDATES else "no")

    if not cfg.has_section("weather"): cfg.add_section("weather")
    if "weather_poll_mins" in data:
        WEATHER_POLL_MINS = max(5, int(data["weather_poll_mins"]))
        cfg.set("weather", "poll_interval_mins", str(WEATHER_POLL_MINS))
    new_key = (data.get("weather_api_key") or "").strip()
    if new_key:
        MET_API_KEY = new_key;  cfg.set("weather", "met_api_key", MET_API_KEY); _last_weather_ts = 0
    if "weather_geohash" in data:
        MET_GEOHASH = data["weather_geohash"].strip(); cfg.set("weather", "geohash", MET_GEOHASH); _last_weather_ts = 0

    if isinstance(data.get("chart_colors"), dict):
        if not cfg.has_section("colours"): cfg.add_section("colours")
        for k, default_v in _COLOUR_DEFAULTS.items():
            v = str(data["chart_colors"].get(k, "")).strip()
            if _valid_hex(v):
                CHART_COLORS[k] = v
                cfg.set("colours", k, v)

    new_pw = (data.get("new_password") or "").strip()
    if new_pw:
        if not cfg.has_section("admin"): cfg.add_section("admin")
        ADMIN_HASH = hashlib.sha256(new_pw.encode()).hexdigest()
        cfg.set("admin", "password_hash", ADMIN_HASH)

    with open(Path(__file__).parent / "config.ini", "w") as f:
        cfg.write(f)
    return jsonify({"ok": True, "weather_configured": bool(MET_API_KEY and MET_GEOHASH)})

@app.route("/api/backup/export")
def backup_export():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    # Consistent online backup → temp file → gzip into memory → stream as download
    BACKUPS_DIR.mkdir(exist_ok=True)
    tmp_path = BACKUPS_DIR / "_export.tmpdb"
    src = sqlite3.connect(DB_PATH)
    try:
        bk = sqlite3.connect(str(tmp_path))
        src.backup(bk)
        bk.close()
    finally:
        src.close()
    mem = io.BytesIO()
    with open(tmp_path, "rb") as f, gzip.open(mem, "wb") as g:
        shutil.copyfileobj(f, g)
    tmp_path.unlink(missing_ok=True)
    mem.seek(0)
    name = f"givenergy-history-{datetime.now().strftime('%Y%m%d')}.db.gz"
    return send_file(mem, mimetype="application/gzip",
                     as_attachment=True, download_name=name)

@app.route("/api/backup/import", methods=["POST"])
def backup_import():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    data = f.read()
    # Transparently accept .gz or a raw .db
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except Exception:
            return jsonify({"ok": False, "error": "Could not decompress file"}), 400
    if data[:16] != b"SQLite format 3\x00":
        return jsonify({"ok": False, "error": "Not a valid database file"}), 400
    # Validate it has a snapshots table before accepting it
    BACKUPS_DIR.mkdir(exist_ok=True)
    staging = BACKUPS_DIR / "_import_check.db"
    try:
        staging.write_bytes(data)
        chk = sqlite3.connect(str(staging))
        has = chk.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='snapshots'").fetchone()
        chk.close()
        if not has:
            staging.unlink(missing_ok=True)
            return jsonify({"ok": False, "error": "Backup has no 'snapshots' table — wrong file?"}), 400
        # Stage it; applied on next restart
        shutil.move(str(staging), str(PENDING_IMPORT))
        return jsonify({"ok": True, "message": "Backup uploaded — restart the dashboard to apply it."})
    except Exception as exc:
        staging.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/update")
def api_update():
    if not CHECK_UPDATES:
        return jsonify({"available": False, "current": APP_VERSION, "checking": False})
    if not _update_info:
        return jsonify({"available": False, "current": APP_VERSION, "checking": True})
    return jsonify(_update_info)

@app.route("/api/weather")
def api_weather():
    with _lock:
        if not _weather_cached:
            return jsonify({"ok": False, "error": "Not yet fetched"}), 503
        return jsonify(_weather_cached)

@app.route("/api/history")
def api_history():
    period = request.args.get("period", "day")
    offset = max(0, int(request.args.get("offset", 0)))

    # Use the last record of each day (MAX ts per day) rather than MAX() of
    # the daily-counter fields.  This avoids the midnight carryover bug where
    # the inverter hasn't yet reset its counter so the first few readings of a
    # new day still show yesterday's total.
    last_per_day = """
        SELECT date(ts,'unixepoch','localtime') AS day,
               solar_today    AS s,
               grid_in_today  AS gi,
               grid_out_today AS go_,
               bat_chg_today  AS bc,
               bat_dis_today  AS bd
        FROM snapshots
        WHERE ts IN (
            SELECT MAX(ts) FROM snapshots
            GROUP BY date(ts,'unixepoch','localtime')
        )
    """

    daily_cte = f"WITH daily AS ({last_per_day})"

    if period == "week":
        # Monday of the week: subtract (weekday+6)%7 days so Mon=0 offset
        monday_expr = "date(day, '-' || CAST((strftime('%w', day) + 6) % 7 AS INTEGER) || ' days')"
        sql = daily_cte + f"""
            SELECT {monday_expr}     AS period,
                ROUND(SUM(s),   2) AS solar_kwh,
                ROUND(SUM(gi),  2) AS grid_in_kwh,
                ROUND(SUM(go_), 2) AS grid_out_kwh,
                ROUND(SUM(bc),  2) AS bat_chg_kwh,
                ROUND(SUM(bd),  2) AS bat_dis_kwh,
                ROUND(MAX(0, SUM(s) + SUM(gi) + SUM(bd) - SUM(go_) - SUM(bc)), 2) AS home_kwh
            FROM daily
            GROUP BY {monday_expr}
            ORDER BY 1 DESC
            LIMIT 1 OFFSET {offset}
        """
    elif period == "day":
        sql = f"""
            SELECT day AS period,
                ROUND(s,   2) AS solar_kwh,
                ROUND(gi,  2) AS grid_in_kwh,
                ROUND(go_, 2) AS grid_out_kwh,
                ROUND(bc,  2) AS bat_chg_kwh,
                ROUND(bd,  2) AS bat_dis_kwh,
                ROUND(MAX(0, s + gi + bd - go_ - bc), 2) AS home_kwh
            FROM ({last_per_day})
            ORDER BY day DESC
            LIMIT 1 OFFSET {offset}
        """
    elif period == "month":
        sql = daily_cte + f"""
            SELECT strftime('%Y-%m', day)   AS period,
                ROUND(SUM(s),   2) AS solar_kwh,
                ROUND(SUM(gi),  2) AS grid_in_kwh,
                ROUND(SUM(go_), 2) AS grid_out_kwh,
                ROUND(SUM(bc),  2) AS bat_chg_kwh,
                ROUND(SUM(bd),  2) AS bat_dis_kwh,
                ROUND(MAX(0, SUM(s) + SUM(gi) + SUM(bd) - SUM(go_) - SUM(bc)), 2) AS home_kwh
            FROM daily
            GROUP BY strftime('%Y-%m', day)
            ORDER BY 1 DESC
            LIMIT 1 OFFSET {offset}
        """
    else:  # year
        sql = daily_cte + f"""
            SELECT strftime('%Y', day)      AS period,
                ROUND(SUM(s),   2) AS solar_kwh,
                ROUND(SUM(gi),  2) AS grid_in_kwh,
                ROUND(SUM(go_), 2) AS grid_out_kwh,
                ROUND(SUM(bc),  2) AS bat_chg_kwh,
                ROUND(SUM(bd),  2) AS bat_dis_kwh,
                ROUND(MAX(0, SUM(s) + SUM(gi) + SUM(bd) - SUM(go_) - SUM(bc)), 2) AS home_kwh
            FROM daily
            GROUP BY strftime('%Y', day)
            ORDER BY 1 DESC
            LIMIT 1 OFFSET {offset}
        """

    with _db() as conn:
        rows = conn.execute(sql).fetchall()
    # Normalise: ensure the date field is always keyed as 'period'
    result = []
    for r in rows:
        d = dict(r)
        for old in ('day', 'date', 'yr', 'mon'):
            if old in d and 'period' not in d:
                d['period'] = d.pop(old)
        result.append(d)
    return jsonify(result)

@app.route("/api/hourly")
def api_hourly():
    """Hourly breakdown for a single day, derived from snapshot data.
    Energy per hour = inverter counter delta between consecutive hours
    (matches the daily totals exactly).  SOC and temps are hourly averages."""
    day = request.args.get("day", "")
    if not day:
        return jsonify({"ok": False, "error": "day required"}), 400

    with _db() as conn:
        # Last counter values at the end of each hour (post-midnight-reset safe)
        last_rows = conn.execute("""
            SELECT CAST(strftime('%H', ts,'unixepoch','localtime') AS INTEGER) AS hr,
                   solar_today AS s, grid_in_today AS gi, grid_out_today AS go_,
                   bat_chg_today AS bc, bat_dis_today AS bd
            FROM snapshots
            WHERE date(ts,'unixepoch','localtime') = ?
              AND ts IN (
                  SELECT MAX(ts) FROM snapshots
                  WHERE date(ts,'unixepoch','localtime') = ?
                  GROUP BY strftime('%H', ts,'unixepoch','localtime')
              )
            ORDER BY hr
        """, (day, day)).fetchall()

        # Hourly averages for SOC + temperatures
        avg_rows = conn.execute("""
            SELECT CAST(strftime('%H', ts,'unixepoch','localtime') AS INTEGER) AS hr,
                   AVG(CASE WHEN soc BETWEEN 0 AND 100 THEN soc END) AS soc,
                   AVG(t_battery) AS tb, AVG(t_heatsink) AS th
            FROM snapshots
            WHERE date(ts,'unixepoch','localtime') = ?
            GROUP BY hr
        """, (day,)).fetchall()

    last_by_hr = {r["hr"]: r for r in last_rows}
    avg_by_hr  = {r["hr"]: r for r in avg_rows}

    # Build 24 hours, computing counter deltas against the previous hour's totals
    hours = []
    prev = {"s": 0.0, "gi": 0.0, "go_": 0.0, "bc": 0.0, "bd": 0.0}
    for h in range(24):
        cur = last_by_hr.get(h)
        if cur is not None:
            d = {}
            for k in ("s", "gi", "go_", "bc", "bd"):
                delta = (cur[k] or 0) - prev[k]
                d[k] = round(max(0.0, delta), 3)
                prev[k] = max(prev[k], cur[k] or 0)   # never go backwards
            solar, gi, go_, bc, bd = d["s"], d["gi"], d["go_"], d["bc"], d["bd"]
            home = round(max(0.0, solar + gi + bd - go_ - bc), 3)
        else:
            solar = gi = go_ = bc = bd = home = 0.0

        a = avg_by_hr.get(h)
        hours.append({
            "hour":         h,
            "solar_kwh":    solar,
            "home_kwh":     home,
            "grid_in_kwh":  gi,
            "grid_out_kwh": go_,
            "bat_chg_kwh":  bc,
            "bat_dis_kwh":  bd,
            "soc":          round(a["soc"], 1) if a and a["soc"] is not None else None,
            "t_battery":    round(a["tb"], 1)  if a and a["tb"]  is not None else None,
            "t_heatsink":   round(a["th"], 1)  if a and a["th"]  is not None else None,
        })

    return jsonify({"ok": True, "day": day, "hours": hours})

@app.route("/")
def index():
    return send_from_directory(str(Path(__file__).parent), "dashboard.html")

@app.route("/manifest.json")
def manifest():
    return send_from_directory(str(Path(__file__).parent), "manifest.json")

@app.route("/sw.js")
def service_worker():
    resp = make_response(send_from_directory(str(Path(__file__).parent), "sw.js"))
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp

@app.route("/icons/<path:filename>")
def icons(filename):
    return send_from_directory(str(Path(__file__).parent / "icons"), filename)

@app.after_request
def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _apply_pending_import()   # swap in a staged restore before opening the DB
    init_db()

    t = threading.Thread(target=_data_loop, daemon=True)
    t.start()

    # Scheduler block engine — inert until the master switch is enabled.
    threading.Thread(target=_scheduler_loop, daemon=True).start()

    # Delayed first update check — runs 30s after startup so the inverter
    # connection settles before we make an outbound request.
    def _deferred_update_check():
        time.sleep(30)
        _check_for_update()
    threading.Thread(target=_deferred_update_check, daemon=True).start()

    # Wait for first successful poll
    print("ACBC - GivEnergy Portal - connecting to inverter...")
    for _ in range(20):
        time.sleep(0.5)
        with _lock:
            if _cached or _error:
                break

    with _lock:
        if _cached:
            print(f"OK First reading: solar={_cached['solar_w']}W  soc={_cached['soc']}%")
        else:
            print(f"WARN Could not reach inverter: {_error}")

    import socket
    host_ip = socket.gethostbyname(socket.gethostname())
    print(f"\nDashboard ready:")
    print(f"   Local:   http://localhost:{WEB_PORT}")
    print(f"   Network: http://{host_ip}:{WEB_PORT}  <-- open on your phone")
    print("\nPress Ctrl+C to stop.\n")

    # Serve with waitress (a small production-grade WSGI server) when available.
    # This gives a clean console with no dev-server warning or per-request spam.
    # Falls back to Flask's built-in server if waitress isn't installed.
    try:
        import logging as _logging
        _logging.getLogger("waitress").setLevel(_logging.ERROR)
        from waitress import serve
        serve(app, host="0.0.0.0", port=WEB_PORT, threads=8)
    except ImportError:
        # Quieten Flask's dev-server banner + per-request access logs
        import logging as _logging
        _logging.getLogger("werkzeug").setLevel(_logging.ERROR)
        try:
            from flask import cli as _cli
            _cli.show_server_banner = lambda *a, **k: None
        except Exception:
            pass
        app.run(host="0.0.0.0", port=WEB_PORT, debug=False, use_reloader=False)
