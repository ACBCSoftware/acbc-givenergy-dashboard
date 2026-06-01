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

_DEFAULT_HASH = hashlib.sha256(b"password").hexdigest()
ADMIN_HASH   = _cfg.get("admin", "password_hash", fallback=_DEFAULT_HASH)

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

# ── Live-data smoothing ────────────────────────────────────────────────────────
# Fields that should almost never be zero in a real home.
# If a zero reading hasn't persisted for this many consecutive polls it is
# treated as a blip and the last known good value is shown instead.
_DEBOUNCE = {
    "home_w":    3,   # 3 × poll_interval = 30 s before a zero is believed
    "solar_w":   3,
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
        "soc":        g(59),                          # BATTERY_PERCENT
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
_POKE_REQUESTS = [
    bytes.fromhex("59590001001c010241423132333447353637000000000000000832040000003cd1d5"),  # Gen2 (slave 0x32)
    bytes.fromhex("59590001001c010241423132333447353637000000000000000811040000003cd1d5"),  # Gen3 (slave 0x11)
]

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
    """If `frame` is a broadcast 'read input registers 0–59' response, decode it
    into the live data dict; otherwise return None."""
    # outer func at [7]; inner function at [27]; base at [38:40]; count at [40:42]
    if len(frame) < 44 or frame[7] != 0x02:
        return None
    inner_func = frame[27]
    base  = (frame[38] << 8) | frame[39]
    count = (frame[40] << 8) | frame[41]
    if inner_func != 0x04 or base != 0 or count < 60:
        return None
    regs_off = 42
    if len(frame) < regs_off + 60 * 2:
        return None
    def g(n):
        o = regs_off + n * 2
        return (frame[o] << 8) | frame[o + 1]
    return _build_from_input_page(g)

def _is_heartbeat_frame(frame: bytes) -> bool:
    """True if this is a 1/Heartbeat frame from the dongle (outer function 0x01)."""
    return len(frame) >= 8 and frame[7] == 0x01

def _ack_heartbeat(s, frame: bytes) -> None:
    """The GivEnergy data dongle emits a 1/Heartbeat frame roughly every 3 minutes
    and expects the client to echo a HeartbeatResponse within ~5s. If we don't, the
    dongle marks our session stale and stops answering our register reads — the root
    cause of the recurring 'no inverter data for 75s' drops on both Gen2 and Gen3.

    A HeartbeatResponse encodes to the same bytes as the request (uid + function +
    data-adapter serial + type), and the dongle ignores the serial on inbound
    frames, so the correct acknowledgement is simply to echo the frame back."""
    try:
        s.sendall(frame)
    except Exception:
        pass

# ── Shared loop housekeeping ─────────────────────────────────────────────────────
def _handle_reading(data: dict, st: dict):
    """Process one fresh reading (from either mode): log to DB, smooth, publish
    to the cache, log recovery, and track inverter status changes + purging."""
    global _cached, _error
    _log_snapshot(data)              # raw values to DB
    data = _smooth(data)             # suppress brief zero-blips for display
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
        time.sleep(POLL_INTERVAL)

def _send_pokes(s):
    """Send the read-request frame for every known slave address so both Gen2
    (0x32) and Gen3 (0x11) inverters are triggered; each ignores the one not
    addressed to it."""
    for poke in _POKE_REQUESTS:
        s.sendall(poke)

def _run_listen(st: dict):
    """Poke-and-listen loop (Gen2 + Gen3). Sends the static read request to
    trigger the inverter, then decodes the input-register frames it emits.
    Publishes one reading per POLL_INTERVAL so the database grows at the normal
    rate. Needs no Modbus library and never hits the IR:052 parse problem."""
    global _cached, _error
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
                # Read whatever has arrived
                try:
                    chunk = s.recv(8192)
                except socket.timeout:
                    chunk = b""
                if chunk:
                    buf.extend(chunk)
                    for frame in _pop_data_frames(buf):
                        if _is_heartbeat_frame(frame):
                            _ack_heartbeat(s, frame)   # keep the session serviced
                            if not st.get("hb_seen"):
                                log.info("Heartbeat acknowledged — holding inverter session open")
                                st["hb_seen"] = True
                            continue
                        d = _decode_listen_frame(frame)
                        if d:
                            latest = d
                            last_frame = now
                # Publish at most once per interval
                if latest and now - last_proc >= POLL_INTERVAL:
                    _handle_reading(latest, st)
                    log.info("Listen: solar=%dW soc=%d%%", latest["solar_w"], latest["soc"])
                    last_proc = now
                _maybe_weather()
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
                    _ack_heartbeat(s, frame)
                    continue
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


# ── Inverter control ──────────────────────────────────────────────────────────
def _ctrl_client():
    """Return a low-level Modbus client for register writes."""
    from givenergy_modbus.modbus import GivEnergyModbusTcpClient as _MC
    return _MC(host=INVERTER_IP, port=INVERTER_PORT)

def _bcd_to_hhmm(v):
    """Convert BCD-encoded inverter time (e.g. 430) to HH:MM string."""
    s = f"{v:04d}"
    return f"{s[:2]}:{s[2:]}"

def _hhmm_to_bcd(hhmm: str) -> int:
    """Convert HH:MM string to BCD int (e.g. '04:30' → 430)."""
    h, m = hhmm.split(":")
    return int(h) * 100 + int(m)

def _read_control_state() -> dict:
    """Read current inverter control settings from holding registers."""
    from givenergy_modbus.model.register import HoldingRegister as HR
    from givenergy_modbus.model.register_cache import RegisterCache
    mc = _ctrl_client()
    rc = RegisterCache()
    rc.set_registers(HR, mc.read_registers(HR, 0,  60, slave_address=0x32))
    rc.set_registers(HR, mc.read_registers(HR, 60, 60, slave_address=0x32))

    def g(name):
        r = HR[name]
        return rc[r]

    return {
        "ok":                    True,
        "enable_charge":         bool(g("ENABLE_CHARGE")),
        "enable_discharge":      bool(g("ENABLE_DISCHARGE")),
        "battery_power_mode":    g("BATTERY_POWER_MODE"),   # 0=max/export 1=demand
        "charge_target_soc":     g("CHARGE_TARGET_SOC"),
        "soc_reserve":           g("BATTERY_SOC_RESERVE"),
        "charge_limit":          g("BATTERY_CHARGE_LIMIT"),
        "discharge_limit":       g("BATTERY_DISCHARGE_LIMIT"),
        "power_reserve":         g("BATTERY_DISCHARGE_MIN_POWER_RESERVE"),
        "charge_slot_1_start":   _bcd_to_hhmm(g("CHARGE_SLOT_1_START")),
        "charge_slot_1_end":     _bcd_to_hhmm(g("CHARGE_SLOT_1_END")),
        "charge_slot_2_start":   _bcd_to_hhmm(g("CHARGE_SLOT_2_START")),
        "charge_slot_2_end":     _bcd_to_hhmm(g("CHARGE_SLOT_2_END")),
        "discharge_slot_1_start":_bcd_to_hhmm(g("DISCHARGE_SLOT_1_START")),
        "discharge_slot_1_end":  _bcd_to_hhmm(g("DISCHARGE_SLOT_1_END")),
        "discharge_slot_2_start":_bcd_to_hhmm(g("DISCHARGE_SLOT_2_START")),
        "discharge_slot_2_end":  _bcd_to_hhmm(g("DISCHARGE_SLOT_2_END")),
        # Power-display settings so the control page can show limits in watts
        "power_units":     POWER_UNITS,
        "max_charge_w":    MAX_CHARGE_W,
        "max_discharge_w": MAX_DISCHARGE_W,
    }

def _log_control(command, params, success, message=""):
    with _db() as conn:
        conn.execute(
            "INSERT INTO control_log (ts,command,params,success,message) VALUES (?,?,?,?,?)",
            (int(time.time()), command, json.dumps(params) if params else None,
             int(success), message))
        conn.commit()

# Map of simple command name → (register_name, value)
_SIMPLE_CMDS = {
    "enable_charge":    ("ENABLE_CHARGE",    1),
    "disable_charge":   ("ENABLE_CHARGE",    0),
    "enable_discharge": ("ENABLE_DISCHARGE", 1),
    "disable_discharge":("ENABLE_DISCHARGE", 0),
}

def _execute_control(command: str, params: dict) -> dict:
    """Write a control command to the inverter. Returns {ok, message}."""
    from givenergy_modbus.model.register import HoldingRegister as HR
    mc = _ctrl_client()

    try:
        if command in _SIMPLE_CMDS:
            reg_name, val = _SIMPLE_CMDS[command]
            mc.write_holding_register(HR[reg_name], val)
            msg = f"{command} applied"

        elif command == "set_mode_dynamic":
            # Eco: demand mode, low reserve, disable discharge override
            mc.write_holding_register(HR["BATTERY_POWER_MODE"], 1)
            mc.write_holding_register(HR["BATTERY_SOC_RESERVE"], 4)
            mc.write_holding_register(HR["ENABLE_DISCHARGE"], 0)
            msg = "Mode set to Dynamic (Eco)"

        elif command == "set_mode_storage":
            # Storage: enable discharge, demand mode (no export)
            mc.write_holding_register(HR["ENABLE_DISCHARGE"], 1)
            mc.write_holding_register(HR["BATTERY_POWER_MODE"], 1)
            msg = "Mode set to Storage"

        elif command == "set_charge_slot":
            slot = int(params.get("slot", 1))
            start_reg = f"CHARGE_SLOT_{slot}_START"
            end_reg   = f"CHARGE_SLOT_{slot}_END"
            mc.write_holding_register(HR[start_reg], _hhmm_to_bcd(params["start"]))
            mc.write_holding_register(HR[end_reg],   _hhmm_to_bcd(params["end"]))
            msg = f"Charge slot {slot} set to {params['start']}–{params['end']}"

        elif command == "set_discharge_slot":
            slot = int(params.get("slot", 1))
            start_reg = f"DISCHARGE_SLOT_{slot}_START"
            end_reg   = f"DISCHARGE_SLOT_{slot}_END"
            mc.write_holding_register(HR[start_reg], _hhmm_to_bcd(params["start"]))
            mc.write_holding_register(HR[end_reg],   _hhmm_to_bcd(params["end"]))
            msg = f"Discharge slot {slot} set to {params['start']}–{params['end']}"

        elif command == "set_charge_target_soc":
            val = max(4, min(100, int(params["value"])))
            mc.write_holding_register(HR["CHARGE_TARGET_SOC"], val)
            msg = f"Charge target SOC set to {val}%"

        elif command == "set_soc_reserve":
            val = max(4, min(100, int(params["value"])))
            mc.write_holding_register(HR["BATTERY_SOC_RESERVE"], val)
            msg = f"SOC reserve set to {val}%"

        elif command == "set_charge_limit":
            val = max(0, min(50, int(params["value"])))
            mc.write_holding_register(HR["BATTERY_CHARGE_LIMIT"], val)
            msg = f"Charge power limit set to {val}%"

        elif command == "set_discharge_limit":
            val = max(0, min(50, int(params["value"])))
            mc.write_holding_register(HR["BATTERY_DISCHARGE_LIMIT"], val)
            msg = f"Discharge power limit set to {val}%"

        elif command == "set_discharge_mode":
            val = max(0, min(1, int(params["value"])))
            mc.write_holding_register(HR["BATTERY_POWER_MODE"], val)
            msg = f"Discharge mode set to {'Max Power / Export' if val == 0 else 'Demand only'}"

        else:
            return {"ok": False, "message": f"Unknown command: {command}"}

        _log_control(command, params, True, msg)
        log.info("Control: %s", msg)
        return {"ok": True, "message": msg}

    except Exception as exc:
        err = str(exc)
        _log_control(command, params, False, err)
        log.error("Control failed %s: %s", command, err)
        return {"ok": False, "message": err}


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
        return jsonify(_cached)

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
        # API key is intentionally never returned to the browser
    })

@app.route("/api/settings", methods=["POST"])
def save_settings():
    global INVERTER_IP, INVERTER_PORT, NUM_BATTERIES, POLL_INTERVAL
    global DATA_RETENTION_DAYS, MET_API_KEY, MET_GEOHASH, _last_weather_ts
    global ADMIN_HASH, WEATHER_POLL_MINS
    global POWER_UNITS, MAX_CHARGE_W, MAX_DISCHARGE_W
    global BACKUP_ENABLED, BACKUP_KEEP_DAYS

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

    if not cfg.has_section("weather"): cfg.add_section("weather")
    if "weather_poll_mins" in data:
        WEATHER_POLL_MINS = max(5, int(data["weather_poll_mins"]))
        cfg.set("weather", "poll_interval_mins", str(WEATHER_POLL_MINS))
    new_key = (data.get("weather_api_key") or "").strip()
    if new_key:
        MET_API_KEY = new_key;  cfg.set("weather", "met_api_key", MET_API_KEY); _last_weather_ts = 0
    if "weather_geohash" in data:
        MET_GEOHASH = data["weather_geohash"].strip(); cfg.set("weather", "geohash", MET_GEOHASH); _last_weather_ts = 0

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
            LIMIT 12 OFFSET {offset}
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
            LIMIT 12 OFFSET {offset}
        """
    else:  # year
        sql = daily_cte + """
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
                   AVG(soc) AS soc, AVG(t_battery) AS tb, AVG(t_heatsink) AS th
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
