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
import logging.handlers
import os
import re
import secrets
import shutil
import socket
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
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

APP_VERSION = (Path(__file__).parent / "VERSION").read_text().strip()

# ── Data directory ─────────────────────────────────────────────────────────────
# Writable state (config, history DB, logs, backups, quick-action state) lives in
# ACBC_DATA_DIR when set, so a Docker container can persist everything on a single
# mounted volume while the code/static files stay read-only in the image. Unset
# (native installs) keeps it all beside the script, exactly as before.
_CODE_DIR = Path(__file__).parent
_DATA_DIR = Path(os.environ.get("ACBC_DATA_DIR", "").strip() or _CODE_DIR)
try:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    _DATA_DIR = _CODE_DIR   # target not writable — fall back to the code dir
_CONFIG_PATH = _DATA_DIR / "config.ini"

# ── Config ─────────────────────────────────────────────────────────────────────
_cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
_cfg.read(_CONFIG_PATH)

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

MET_API_KEY       = _cfg.get("weather",    "met_api_key",       fallback="")
MET_GEOHASH       = _cfg.get("weather",    "geohash",           fallback="")
WEATHER_POSTCODE  = _cfg.get("weather",    "postcode",          fallback="")
WEATHER_POLL_MINS = _cfg.getint("weather", "poll_interval_mins", fallback=30)

SF_LAT     = _cfg.get("solar_forecast", "latitude",      fallback="").strip()
SF_LON     = _cfg.get("solar_forecast", "longitude",     fallback="").strip()
SF_POSTCODE = _cfg.get("solar_forecast", "postcode",     fallback="").strip()
SF_TILT    = _cfg.get("solar_forecast", "panel_tilt",    fallback="").strip()
SF_AZ      = _cfg.get("solar_forecast", "panel_azimuth", fallback="").strip()
SF_KWP     = _cfg.get("solar_forecast", "panel_kwp",     fallback="").strip()
SF_API_KEY = _cfg.get("solar_forecast", "api_key",       fallback="").strip()

PRED_BATTERY_KWH     = _cfg.getfloat("predictive_charge", "battery_kwh",        fallback=0.0)
PRED_PESSIMISM       = _cfg.getfloat("predictive_charge", "pessimism_factor",    fallback=0.85)
PRED_EVENING_RESERVE = _cfg.getfloat("predictive_charge", "evening_reserve_kwh", fallback=3.0)
PRED_MIN_SOC         = _cfg.getint(  "predictive_charge", "minimum_soc",         fallback=20)


def _sf_configured() -> bool:
    return all([SF_LAT, SF_LON, SF_TILT, SF_AZ, SF_KWP])

# When False, control/action endpoints are open (local-only convenience mode).
# Settings always stays behind the strict password check regardless.
REQUIRE_AUTH = _cfg.getboolean("server", "require_auth", fallback=True)

BACKUP_ENABLED   = _cfg.getboolean("backup", "enabled",   fallback=True)
BACKUP_KEEP_DAYS = _cfg.getint("backup",     "keep_days", fallback=7)

CHECK_UPDATES = _cfg.getboolean("server", "check_for_updates", fallback=True)

# ── Scheduler (app-held, 48 half-hour block engine — see BACKLOG.md) ──────────
# Master on/off + the baseline mode asserted in every unscheduled block.
# Per-rule schedule rows live in the `schedules` DB table, not in config.
SCHEDULER_ENABLED  = _cfg.getboolean("scheduler", "enabled", fallback=False)
SCHEDULER_BASELINE = _cfg.get("scheduler", "baseline", fallback="eco").strip().lower()
SCHEDULER_BASELINE_SOC_RESERVE = max(4, min(100, _cfg.getint("scheduler", "baseline_soc_reserve", fallback=4)))

# ── Database schema version ────────────────────────────────────────────────────
# Increment when a new migration step is added to init_db().
# PRAGMA user_version tracks which version the installed DB is on.
# Migration steps follow the pattern: if version < N: …; conn.execute("PRAGMA user_version = N")
_SCHEMA_VERSION = 1
# NOTE: the scheduler requires exclusive inverter control.  If a cloud integration
# (Octopus Intelligent Flux, Predbat, etc.) is active, its register locks will cause
# scheduler writes to be silently rejected by the firmware — behaviour is undefined.
# Disconnect any cloud integration before enabling the scheduler.
_SCHED_BASELINES   = ("eco", "storage")   # eco = discharge-on/grid-charge-off
if SCHEDULER_BASELINE not in _SCHED_BASELINES:
    SCHEDULER_BASELINE = "eco"

# ── Quick Actions (1-hour manual charge / discharge buttons) ─────────────────
# Buttons shown on the home page when enabled; each runs for up to 1 hour then
# auto-reverts to the same baseline the scheduler uses.
QUICK_ACTIONS_ENABLED      = _cfg.getboolean("quick_actions", "enabled",            fallback=False)
QUICK_CHARGE_POWER_PCT     = max(1, min(100, _cfg.getint("quick_actions", "charge_power_pct",    fallback=100)))
QUICK_DISCHARGE_POWER_PCT  = max(1, min(100, _cfg.getint("quick_actions", "discharge_power_pct", fallback=100)))
QUICK_CHARGE_TARGET_SOC    = max(4, min(100, _cfg.getint("quick_actions", "charge_target_soc",   fallback=100)))

_DEFAULT_HASH = hashlib.sha256(b"password").hexdigest()
ADMIN_HASH   = _cfg.get("admin", "password_hash", fallback=_DEFAULT_HASH)

LOG_LEVEL          = _cfg.get("logging", "log_level",           fallback="warning").strip().lower()
LOG_RETENTION_DAYS = _cfg.getint("logging", "log_retention_days", fallback=7)

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 16384, 8, 1

def _hash_password(pw: str) -> str:
    """Hash a plaintext password with scrypt + random salt.
    Returns a self-describing string: scrypt:N:r:p:<salt_hex>:<hash_hex>
    """
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt:{_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}:{salt.hex()}:{h.hex()}"

def _verify_password(pw: str, stored: str) -> bool:
    """Verify a plaintext password against a stored hash.
    Supports both legacy SHA-256 (no prefix) and scrypt (scrypt: prefix).
    """
    if stored.startswith("scrypt:"):
        try:
            _, n, r, p, salt_hex, hash_hex = stored.split(":")
            h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex),
                               n=int(n), r=int(r), p=int(p), dklen=32)
            return h.hex() == hash_hex
        except Exception:
            return False
    return hashlib.sha256(pw.encode()).hexdigest() == stored

_COLOUR_DEFAULTS: dict[str, str] = {
    "solar":    "#f59e0b",
    "home":     "#38bdf8",
    "grid_in":  "#f87171",
    "grid_out": "#4ade80",
    "bat_chg":  "#818cf8",
    "bat_dis":  "#c084fc",
    "soc":      "#fbbf24",
    # Power graph (separate from the bar chart above)
    "graph_solar":   "#f59e0b",
    "graph_home":    "#38bdf8",
    "graph_bat_chg": "#818cf8",
    "graph_bat_dis": "#c084fc",
    "graph_grid":    "#f87171",
    "graph_soc":     "#fbbf24",
}
CHART_COLORS: dict[str, str] = {
    k: _cfg.get("colours", k, fallback=v)
    for k, v in _COLOUR_DEFAULTS.items()
}

def _valid_hex(v: str) -> bool:
    return (isinstance(v, str) and len(v) == 7 and v[0] == "#"
            and all(c in "0123456789abcdefABCDEF" for c in v[1:]))

def _authorised():
    """Check X-Admin-Password header against stored hash.
    Used for Settings and other config endpoints, which ALWAYS require the
    password regardless of REQUIRE_AUTH.
    On successful login against a legacy SHA-256 hash, silently upgrades
    to scrypt in config.ini.
    """
    global ADMIN_HASH
    pw = request.headers.get("X-Admin-Password", "")
    if not _verify_password(pw, ADMIN_HASH):
        return False
    if not ADMIN_HASH.startswith("scrypt:"):
        try:
            ADMIN_HASH = _hash_password(pw)
            cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
            _config_path = _CONFIG_PATH
            cfg.read(_config_path)
            if not cfg.has_section("admin"):
                cfg.add_section("admin")
            cfg.set("admin", "password_hash", ADMIN_HASH)
            with open(_config_path, "w") as f:
                cfg.write(f)
            log.info("Password hash upgraded from SHA-256 to scrypt")
        except Exception as exc:
            log.warning("Could not upgrade password hash: %s", exc)
    return True

def _action_authorised():
    """Auth gate for control/action endpoints (control, scheduler, quick
    actions, logs).  Open when REQUIRE_AUTH is off (local-only convenience
    mode); otherwise falls back to the strict password check."""
    return (not REQUIRE_AUTH) or _authorised()

# ── Tariff / cost estimation ──────────────────────────────────────────────────
# Max stored/derived TOU windows. Raised from 3 to 8 in v3.0 (#55): the "base +
# deviations" model can need up to ~5 windows for tariffs like Cosy, whose cheap
# rate runs in three disjoint stretches plus a separate peak.
_MAX_TOU_WINDOWS  = 8
TARIFF_CURRENCY   = _cfg.get("tariff",    "currency_symbol",   fallback="£")
TARIFF_STANDING_P = _cfg.getfloat("tariff", "standing_charge_p", fallback=0.0)
TARIFF_EXPORT_P   = _cfg.getfloat("tariff", "export_rate_p",     fallback=0.0)
TARIFF_IMPORT_P   = _cfg.getfloat("tariff", "import_rate_p",     fallback=0.0)
TARIFF_TOU: list[dict] = []
for _i in range(1, _MAX_TOU_WINDOWS + 1):
    _tn  = _cfg.get("tariff", f"tou_{_i}_name",          fallback="")
    _ts  = _cfg.get("tariff", f"tou_{_i}_start",         fallback="")
    _te  = _cfg.get("tariff", f"tou_{_i}_end",           fallback="")
    _tr  = _cfg.getfloat("tariff", f"tou_{_i}_rate_p",          fallback=0.0)
    _ter = _cfg.getfloat("tariff", f"tou_{_i}_export_rate_p",   fallback=0.0)
    if _tn and _ts and _te:
        TARIFF_TOU.append({"name": _tn, "start": _ts, "end": _te,
                           "rate_p": _tr, "export_rate_p": _ter})

# ── Auto-tariff source ────────────────────────────────────────────────────────
TARIFF_SOURCE     = _cfg.get("tariff", "tariff_source",      fallback="manual")
OCTOPUS_REGION    = _cfg.get("tariff", "octopus_region",     fallback="").upper().strip()
OCTOPUS_POSTCODE  = _cfg.get("tariff", "octopus_postcode",   fallback="").strip()
_RATES_LAST_FETCHED = _cfg.get("tariff", "rates_last_fetched", fallback="")
_PRODUCT_OVERRIDE = {
    "octopus_agile":            _cfg.get("tariff", "product_agile",            fallback=""),
    "octopus_tracker":          _cfg.get("tariff", "product_tracker",          fallback=""),
    "octopus_cosy":             _cfg.get("tariff", "product_cosy",             fallback=""),
    "octopus_go":               _cfg.get("tariff", "product_go",               fallback=""),
    "octopus_flux":             _cfg.get("tariff", "product_flux",             fallback=""),
    "octopus_intelligent_flux": _cfg.get("tariff", "product_intelligent_flux", fallback=""),
    "octopus_flexible":         _cfg.get("tariff", "product_flexible",         fallback=""),
    "edf_freephase":            _cfg.get("tariff", "product_edf",              fallback=""),
}

_PRODUCT_DEFAULTS = {
    "octopus_agile":             "AGILE-24-10-01",
    "octopus_tracker":           "SILVER-25-04-15",
    "octopus_cosy":              "COSY-22-12-08",
    "octopus_go":                "GO-VAR-22-10-14",
    "octopus_flux":              "FLUX-IMPORT-23-02-14",
    "octopus_intelligent_flux":  "INTELLI-FLUX-IMPORT-23-07-14",
    "octopus_flexible":          "VAR-22-11-01",
    "edf_freephase":             "EDF_FREEPHASE_DYNAMIC_12M_HH",
}
_AGILE_EXPORT_PRODUCT  = "AGILE-OUTGOING-19-05-13"
_EXPORT_PRODUCT_DEFAULTS = {
    # Separate export product with distinct rates
    "octopus_flux": "FLUX-EXPORT-23-02-14",
}
# Sources where export rates mirror import rates (no separate export product)
_MIRROR_EXPORT_SOURCES = frozenset({"octopus_intelligent_flux"})
_OCTOPUS_BASE          = "https://api.octopus.energy/v1"
_EDF_BASE              = "https://api.edfgb-kraken.energy/v1"
_VARIABLE_RATE_SOURCES = frozenset({"octopus_agile", "edf_freephase"})
_TARIFF_SOURCES = frozenset({
    "manual", "octopus_agile", "octopus_tracker", "octopus_cosy",
    "octopus_go", "octopus_flux", "octopus_intelligent_flux",
    "octopus_flexible", "edf_freephase",
})
_fetch_lock            = threading.Lock()
_fetch_status: dict    = {"ok": None, "msg": "Never fetched", "fetched_at": "",
                           "slots_today": 0, "slots_tomorrow": 0}

_today_import_cost_p   = 0.0
_today_export_income_p = 0.0
_today_cost_date       = ""    # YYYY-MM-DD; empty forces recompute on first poll
_tariff_dirty          = False  # set True when config saved; poll thread recomputes
_last_cost_poll_ts     = 0.0   # Unix ts of last TOU accumulation (0 = use POLL_INTERVAL)
_today_register_date   = ""    # YYYY-MM-DD; updated every poll for end-of-day capture
_prev_grid_in_today    = 0.0   # register kWh from previous poll (for day-end flat save)
_prev_grid_out_today   = 0.0


def _tariff_configured() -> bool:
    if TARIFF_SOURCE not in ("manual", ""):
        return True   # API-managed source is always "configured"
    return TARIFF_IMPORT_P > 0 or bool(TARIFF_TOU)


def _tariff_import_rate(ts: float) -> float:
    """Return import tariff rate (p/kWh) for a Unix timestamp."""
    if TARIFF_SOURCE in _VARIABLE_RATE_SOURCES:
        return _agile_rate_at(ts, "import")
    if not TARIFF_TOU:
        return TARIFF_IMPORT_P
    dt = datetime.fromtimestamp(ts)
    tod = dt.hour * 60 + dt.minute
    for w in TARIFF_TOU:
        s = int(w["start"][:2]) * 60 + int(w["start"][3:5])
        e = int(w["end"][:2])   * 60 + int(w["end"][3:5])
        if s == e:
            continue
        active = (s <= tod < e) if s < e else (tod >= s or tod < e)
        if active:
            return w["rate_p"]
    return TARIFF_IMPORT_P


def _tariff_export_rate(ts: float) -> float:
    """Return export tariff rate (p/kWh) for a Unix timestamp.
    Checks per-window export_rate_p; falls back to flat TARIFF_EXPORT_P."""
    if TARIFF_SOURCE in _VARIABLE_RATE_SOURCES:
        return _agile_rate_at(ts, "export")
    if not TARIFF_TOU:
        return TARIFF_EXPORT_P
    dt = datetime.fromtimestamp(ts)
    tod = dt.hour * 60 + dt.minute
    for w in TARIFF_TOU:
        s = int(w["start"][:2]) * 60 + int(w["start"][3:5])
        e = int(w["end"][:2])   * 60 + int(w["end"][3:5])
        if s == e:
            continue
        active = (s <= tod < e) if s < e else (tod >= s or tod < e)
        if active:
            return w.get("export_rate_p", TARIFF_EXPORT_P)
    return TARIFF_EXPORT_P


def _in_tou_window(ts: float) -> bool:
    """Return True if ts falls within any defined TOU window.
    For variable-rate sources (Agile/FreePhase) always True — every slot is variable."""
    if TARIFF_SOURCE in _VARIABLE_RATE_SOURCES:
        return True
    if not TARIFF_TOU:
        return False
    dt = datetime.fromtimestamp(ts)
    tod = dt.hour * 60 + dt.minute
    for w in TARIFF_TOU:
        s = int(w["start"][:2]) * 60 + int(w["start"][3:5])
        e = int(w["end"][:2])   * 60 + int(w["end"][3:5])
        if s == e:
            continue
        active = (s <= tod < e) if s < e else (tod >= s or tod < e)
        if active:
            return True
    return False


# ── Auto-tariff fetch infrastructure ─────────────────────────────────────────

def _agile_slot_start(ts: float) -> str:
    """Return the UTC ISO8601 30-min slot-start string for a Unix timestamp."""
    dt  = datetime.utcfromtimestamp(ts)
    m30 = 0 if dt.minute < 30 else 30
    return dt.replace(minute=m30, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _agile_rate_at(ts: float, direction: str = "import") -> float:
    """Return the inc-VAT Agile/FreePhase rate (p/kWh) for the slot at ts.
    Falls back to the flat TARIFF_IMPORT_P / TARIFF_EXPORT_P if no DB row found."""
    slot = _agile_slot_start(ts)
    col  = "import_p" if direction == "import" else "export_p"
    try:
        with _db() as conn:
            row = conn.execute(
                f"SELECT {col} FROM agile_rates WHERE slot_start = ?", (slot,)
            ).fetchone()
        if row and row[0] is not None:
            return max(0.0, float(row[0]))
    except Exception:
        pass
    return TARIFF_IMPORT_P if direction == "import" else TARIFF_EXPORT_P


def _http_get_json(url: str, timeout: int = 15) -> dict:
    """GET a URL and return parsed JSON. Raises on HTTP error or timeout."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "ACBC-GivEnergy-Dashboard/2.3"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get_product_code(source: str) -> str:
    return _PRODUCT_OVERRIDE.get(source) or _PRODUCT_DEFAULTS.get(source, "")


def _octopus_tariff_code(product: str, region: str) -> str:
    return f"E-1R-{product}-{region}"


def _tariff_api_base(product: str) -> str:
    return _EDF_BASE if product.upper().startswith("EDF_") else _OCTOPUS_BASE


def _fetch_octopus_unit_rates(product: str, tariff_code: str,
                               period_from: str = "", period_to: str = "") -> list:
    """Fetch standard unit rates. Returns list of result dicts."""
    base = _tariff_api_base(product)
    url  = (f"{base}/products/{product}/electricity-tariffs/{tariff_code}"
            f"/standard-unit-rates/?page_size=100")
    if period_from:
        url += f"&period_from={urllib.parse.quote(period_from)}"
    if period_to:
        url += f"&period_to={urllib.parse.quote(period_to)}"
    return _http_get_json(url).get("results", [])


def _fetch_standing_charge(product: str, tariff_code: str) -> float:
    """Return current standing charge (p/day inc VAT), or 0 on failure."""
    base = _tariff_api_base(product)
    url  = (f"{base}/products/{product}/electricity-tariffs/{tariff_code}"
            f"/standing-charges/?page_size=1")
    try:
        results = _http_get_json(url).get("results", [])
        return float(results[0]["value_inc_vat"]) if results else 0.0
    except Exception:
        return 0.0


def _lookup_region(postcode: str) -> str:
    """Look up DNO region code (A-P) from a UK postcode via Octopus API."""
    pc  = urllib.parse.quote_plus(postcode.replace(" ", ""))
    url = f"{_OCTOPUS_BASE}/industry/grid-supply-points/?postcode={pc}"
    results = _http_get_json(url).get("results", [])
    return results[0].get("group_id", "").lstrip("_").upper() if results else ""


def _is_bst(dt: datetime) -> bool:
    """Approximate UK BST detection for a naive UTC datetime."""
    m, d = dt.month, dt.day
    return (4 <= m <= 9) or (m == 3 and d >= 25) or (m == 10 and d < 25)


def _dominant_rate(slots: list) -> float:
    """Return the rate that covers the greatest total duration in a slot list.

    Used for export products where the cheapest rate is an anomalous overnight
    window — the dominant rate gives a more representative base flat rate.
    """
    rate_dur: dict = {}
    for r in slots:
        vf = r.get("valid_from", "");  vt = r.get("valid_to", "")
        if not vf or not vt:
            continue
        try:
            secs = (datetime.strptime(vt[:16], "%Y-%m-%dT%H:%M") -
                    datetime.strptime(vf[:16], "%Y-%m-%dT%H:%M")).total_seconds()
        except ValueError:
            secs = 0
        key = round(float(r.get("value_inc_vat", 0)), 2)
        rate_dur[key] = rate_dur.get(key, 0.0) + secs
    return float(max(rate_dur, key=rate_dur.get)) if rate_dur else 0.0


def _extract_tou_windows(slots: list, label: str = "") -> tuple:
    """
    Derive a base flat rate and TOU windows from unit-rate records.
    `label` (e.g. "import"/"export") only tags the DEBUG lines.

    Handles both wide-window tariffs (e.g. Cosy/Flux: a few multi-hour records)
    and half-hourly slot tariffs (e.g. IntelligentFlux: ~48 records/day with
    2–3 distinct rate values).

    Algorithm:
    1. Sort by start time and merge consecutive same-rate runs into contiguous bands.
    2. Base flat rate = the rate covering the greatest total duration (dominant).
    3. Every band whose rate *differs* from base — cheaper OR pricier — becomes a
       named window, each disjoint stretch kept separately.

    Taking the cheapest band as base (pre-v3.0) mis-set Flux's base to its
    off-peak rate and left standard-rate stretches costed at off-peak (#55).
    Because standard is the fragmented, dominant rate, making it the base means
    its scattered stretches simply fall through to the flat rate and need no
    window at all — only the (typically contiguous) off-peak/peak deviations do.

    Returns (base_rate, [{name, start, end, rate_p, export_rate_p}, ...]).
    """
    if not slots:
        return 0.0, []

    # Parse to (local_start_dt, local_end_dt, rate)
    parsed = []
    for r in slots:
        vf = r.get("valid_from", "");  vt = r.get("valid_to", "")
        if not vf or not vt:
            continue
        try:
            dt_f = datetime.strptime(vf[:16], "%Y-%m-%dT%H:%M")
            dt_t = datetime.strptime(vt[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        off  = timedelta(hours=1 if _is_bst(dt_f) else 0)
        rate = round(float(r.get("value_inc_vat", 0)), 4)
        parsed.append((dt_f + off, dt_t + off, rate))

    if not parsed:
        return 0.0, []
    parsed.sort(key=lambda x: x[0])

    # Merge contiguous same-rate runs (handles half-hourly slot tariffs).
    # Keep the datetimes so we can measure each band's duration.
    merged = []   # list of (start_dt, end_dt, rate)
    rs, re, rr = parsed[0]
    for s, e, r in parsed[1:]:
        if r == rr and s <= re:     # same rate, adjacent or overlapping
            re = max(re, e)
        else:
            merged.append((rs, re, rr))
            rs, re, rr = s, e, r
    merged.append((rs, re, rr))

    # Base rate = the rate covering the greatest total duration (the flat rate).
    dur_by_rate: dict = {}
    for s, e, r in merged:
        dur_by_rate[r] = dur_by_rate.get(r, 0.0) + (e - s).total_seconds()
    base_rate = max(dur_by_rate, key=dur_by_rate.get)

    _tag = f"[{label}] " if label else ""
    if log.isEnabledFor(logging.DEBUG):
        hours = {round(r, 4): round(sec / 3600, 2) for r, sec in dur_by_rate.items()}
        log.debug("%sTOU extract: %d band(s), base=%.4fp (dominant by duration); "
                  "rate->hours=%s", _tag, len(merged), base_rate, hours)

    # Name each distinct deviation rate by its relation to base. In practice the
    # big tariffs have one cheaper rate and/or one pricier rate; extra levels get
    # "Super off-peak"/"Super peak" so names stay unique and intuitive.
    cheaper = sorted(r for r in dur_by_rate if r < base_rate)   # ascending (cheapest first)
    pricier = sorted(r for r in dur_by_rate if r > base_rate)   # ascending
    name_of: dict = {}
    for i, r in enumerate(cheaper):
        name_of[r] = "Super off-peak" if (len(cheaper) > 1 and i == 0) else "Off-peak"
    for i, r in enumerate(pricier):
        name_of[r] = "Super peak" if (len(pricier) > 1 and i == len(pricier) - 1) else "Peak"

    # Emit one window per deviating band, in chronological order. Bands equal to
    # base are omitted — they are covered by the flat rate. A fetch can span more
    # than one day, so the same daily window (e.g. Go's 00:30-05:30 off-peak)
    # recurs; dedupe by clock time + rate so it is stored once. Cap defensively.
    windows = []
    seen = set()
    for s, e, r in merged:
        if r == base_rate:
            continue
        start, end = s.strftime("%H:%M"), e.strftime("%H:%M")
        key = (start, end, round(r, 4))
        if key in seen:
            continue
        seen.add(key)
        windows.append({"name": name_of[r], "start": start, "end": end,
                        "rate_p": r, "export_rate_p": 0.0})
    windows = windows[:_MAX_TOU_WINDOWS]
    if log.isEnabledFor(logging.DEBUG):
        for w in windows:
            log.debug("%sTOU window: %-14s %s-%s  %.4fp",
                      _tag, w["name"], w["start"], w["end"], w["rate_p"])
    return base_rate, windows


def _save_fetched_rates(import_p=None, export_p=None, standing_p=None,
                         tou_windows=None) -> None:
    """Write fetched rates to config.ini, update in-memory globals, mark dirty.

    Safety rule: a fetched value only overwrites an existing value when it is
    valid (> 0).  This prevents a bad API response or a missing rate from
    silently zeroing out a rate the user entered manually.
    Standing charge is exempt — 0 is a legitimate value for that field.
    """
    global TARIFF_IMPORT_P, TARIFF_EXPORT_P, TARIFF_STANDING_P
    global TARIFF_TOU, _RATES_LAST_FETCHED, _tariff_dirty
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(_CONFIG_PATH)
    if not cfg.has_section("tariff"):
        cfg.add_section("tariff")
    if import_p is not None and float(import_p) > 0:
        TARIFF_IMPORT_P = float(import_p)
        cfg.set("tariff", "import_rate_p", str(round(float(import_p), 4)))
    if export_p is not None and float(export_p) > 0:
        TARIFF_EXPORT_P = float(export_p)
        cfg.set("tariff", "export_rate_p", str(round(float(export_p), 4)))
    if standing_p is not None:                          # 0 is a valid standing charge
        TARIFF_STANDING_P = float(standing_p)
        cfg.set("tariff", "standing_charge_p", str(round(float(standing_p), 4)))
    if tou_windows is not None:
        # Strip any window whose import rate is 0 — these are bad API data
        valid_windows = [w for w in tou_windows if float(w.get("rate_p", 0)) > 0]
        TARIFF_TOU = valid_windows
        for i in range(1, _MAX_TOU_WINDOWS + 1):
            for k in (f"tou_{i}_name", f"tou_{i}_start", f"tou_{i}_end",
                      f"tou_{i}_rate_p", f"tou_{i}_export_rate_p"):
                cfg.remove_option("tariff", k)
        for i, w in enumerate(valid_windows[:_MAX_TOU_WINDOWS], 1):
            if w.get("name"):
                cfg.set("tariff", f"tou_{i}_name",          str(w["name"]))
                cfg.set("tariff", f"tou_{i}_start",         str(w["start"]))
                cfg.set("tariff", f"tou_{i}_end",           str(w["end"]))
                cfg.set("tariff", f"tou_{i}_rate_p",        str(round(float(w.get("rate_p", 0)), 4)))
                cfg.set("tariff", f"tou_{i}_export_rate_p", str(round(float(w.get("export_rate_p", 0)), 4)))
    ts_now = datetime.now().isoformat(timespec="seconds")
    _RATES_LAST_FETCHED = ts_now
    cfg.set("tariff", "rates_last_fetched", ts_now)
    with open(_CONFIG_PATH, "w") as f:
        cfg.write(f)
    _tariff_dirty = True


def _fetch_agile_rates() -> dict:
    """Fetch Agile / EDF FreePhase half-hourly import+export slots (today + tomorrow)."""
    global _RATES_LAST_FETCHED
    region  = OCTOPUS_REGION
    product = _get_product_code("edf_freephase" if TARIFF_SOURCE == "edf_freephase"
                                else "octopus_agile")
    tariff  = _octopus_tariff_code(product, region)
    now_utc = datetime.utcnow()
    from_s  = now_utc.strftime("%Y-%m-%dT00:00Z")
    to_s    = (now_utc + timedelta(days=2)).strftime("%Y-%m-%dT00:00Z")

    try:
        import_slots = _fetch_octopus_unit_rates(product, tariff, from_s, to_s)
    except Exception as exc:
        msg = f"Agile import fetch failed: {exc}"
        log.warning(msg)
        _fetch_status.update({"ok": False, "msg": msg})
        return {"ok": False, "msg": msg}

    export_dict: dict = {}
    if TARIFF_SOURCE == "octopus_agile":
        try:
            et = _octopus_tariff_code(_AGILE_EXPORT_PRODUCT, region)
            export_dict = {
                s["valid_from"]: float(s["value_inc_vat"])
                for s in _fetch_octopus_unit_rates(_AGILE_EXPORT_PRODUCT, et, from_s, to_s)
            }
        except Exception as exc:
            log.warning("Agile export fetch failed (non-fatal): %s", exc)

    if not import_slots:
        msg = "No Agile slots returned from API"
        _fetch_status.update({"ok": False, "msg": msg})
        return {"ok": False, "msg": msg}

    # Validate each slot before writing.
    # Note: zero and negative import rates ARE valid for Agile (grid oversupply),
    # so we only reject slots that are structurally malformed.
    rows = []
    skipped = 0
    for s in import_slots:
        vf = s.get("valid_from", "")
        vt = s.get("valid_to", "")
        raw = s.get("value_inc_vat")
        if not vf or not vt or raw is None:
            skipped += 1
            continue
        try:
            imp_p = float(raw)
        except (TypeError, ValueError):
            skipped += 1
            continue
        exp_p = export_dict.get(vf)
        # Only use the fetched export rate if it looks valid; otherwise keep
        # whatever is already in the DB (by omitting this slot's export_p update
        # via the fallback to the existing stored value isn't possible with
        # INSERT OR REPLACE — so fall back to TARIFF_EXPORT_P as a safe default)
        if exp_p is None or not isinstance(exp_p, (int, float)):
            exp_p = TARIFF_EXPORT_P
        rows.append((vf, vt, imp_p, exp_p))

    if skipped:
        log.warning("Agile fetch: skipped %d malformed slot(s)", skipped)

    # Refuse to write if we got a suspiciously low number of valid slots —
    # a partial/corrupt API response should not overwrite good existing data
    if len(rows) < 24:
        msg = f"Agile fetch returned only {len(rows)} valid slot(s) — minimum 24 required; aborting write"
        log.warning(msg)
        _fetch_status.update({"ok": False, "msg": msg})
        return {"ok": False, "msg": msg}

    with _db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO agile_rates (slot_start, slot_end, import_p, export_p)"
            " VALUES (?,?,?,?)", rows
        )
        conn.execute(
            "DELETE FROM agile_rates"
            " WHERE slot_end < strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 days')"
        )
        conn.commit()

    try:
        sc = _fetch_standing_charge(product, tariff)
        if sc > 0:
            _save_fetched_rates(standing_p=sc)
    except Exception as exc:
        log.warning("Agile standing charge fetch failed: %s", exc)

    today_s = now_utc.strftime("%Y-%m-%d")
    tom_s   = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
    st      = sum(1 for r in rows if r[0].startswith(today_s))
    sm      = sum(1 for r in rows if r[0].startswith(tom_s))
    ts_now  = datetime.now().isoformat(timespec="seconds")
    _RATES_LAST_FETCHED = ts_now
    _fetch_status.update({
        "ok": True, "fetched_at": ts_now, "slots_today": st, "slots_tomorrow": sm,
        "msg": f"{len(rows)} slots loaded ({sm} for tomorrow)",
    })
    return {"ok": True, "msg": _fetch_status["msg"]}


def _fetch_tracker_rates() -> dict:
    """Fetch today's single Octopus Tracker rate."""
    global _RATES_LAST_FETCHED
    region  = OCTOPUS_REGION
    product = _get_product_code("octopus_tracker")
    tariff  = _octopus_tariff_code(product, region)
    now_utc = datetime.utcnow()
    from_s  = now_utc.strftime("%Y-%m-%dT00:00Z")
    to_s    = (now_utc + timedelta(days=1)).strftime("%Y-%m-%dT00:00Z")
    try:
        slots = _fetch_octopus_unit_rates(product, tariff, from_s, to_s)
        if not slots:
            return {"ok": False, "msg": "No Tracker rate returned from API"}
        # API returns newest first; find the slot currently active (valid_from <= now)
        now_str = now_utc.strftime("%Y-%m-%dT%H:%M")
        active = sorted(
            [s for s in slots if s.get("valid_from", "") <= now_str],
            key=lambda s: s.get("valid_from", ""), reverse=True
        )
        rate = float((active[0] if active else slots[-1])["value_inc_vat"])
        sc   = _fetch_standing_charge(product, tariff)
        _save_fetched_rates(import_p=rate,
                            standing_p=sc if sc > 0 else None,
                            tou_windows=[])
        msg = f"Tracker: {rate:.2f}p/kWh"
        _fetch_status.update({"ok": True, "msg": msg, "fetched_at": _RATES_LAST_FETCHED,
                              "slots_today": 1, "slots_tomorrow": 0})
        return {"ok": True, "msg": msg}
    except Exception as exc:
        msg = f"Tracker fetch failed: {exc}"
        log.warning(msg)
        _fetch_status.update({"ok": False, "msg": msg})
        return {"ok": False, "msg": msg}


def _fetch_static_rates(source: str) -> dict:
    """Fetch rates for Cosy, Go, Flux, or Flexible Octopus."""
    global _RATES_LAST_FETCHED
    region  = OCTOPUS_REGION
    product = _get_product_code(source)
    tariff  = _octopus_tariff_code(product, region)
    now_utc = datetime.utcnow()
    from_s  = now_utc.strftime("%Y-%m-%dT00:00Z")
    to_s    = (now_utc + timedelta(days=1)).strftime("%Y-%m-%dT00:00Z")
    try:
        slots = _fetch_octopus_unit_rates(product, tariff, from_s, to_s)
        if not slots:
            slots = _fetch_octopus_unit_rates(product, tariff)   # no date filter fallback
        if not slots:
            return {"ok": False, "msg": f"No rates returned for {source}"}
        sc    = _fetch_standing_charge(product, tariff)
        rates = {round(float(r["value_inc_vat"]), 2) for r in slots}
        label = source.replace('octopus_', '').replace('_', ' ').title()
        log.debug("Fetch %s: %d unit-rate slot(s), %d distinct rate(s) %s, standing=%.2fp",
                  source, len(slots), len(rates), sorted(rates), sc)

        # Resolve export rates:
        #   a) Mirror sources — export rate == import rate per window (no separate product)
        #   b) Sources with a known export product — fetch and cross-reference by time
        exp_product   = _EXPORT_PRODUCT_DEFAULTS.get(source, "")
        exp_base_rate = None
        exp_window_rates: dict = {}   # window_start_hhmm → export_rate_p

        if source in _MIRROR_EXPORT_SOURCES:
            pass   # handled after windows are built (import rates copied to export)
        elif exp_product:
            try:
                exp_tariff  = _octopus_tariff_code(exp_product, region)
                exp_slots   = _fetch_octopus_unit_rates(exp_product, exp_tariff, from_s, to_s)
                if not exp_slots:
                    exp_slots = _fetch_octopus_unit_rates(exp_product, exp_tariff)
                if exp_slots:
                    exp_rates_set = {round(float(r["value_inc_vat"]), 2) for r in exp_slots}
                    # Dominant-by-duration gives the representative standard rate,
                    # not an anomalous overnight minimum
                    exp_base_rate = _dominant_rate(exp_slots)
                    if len(exp_rates_set) > 1:
                        _, exp_wins = _extract_tou_windows(exp_slots, label="export")
                        for w in exp_wins:
                            exp_window_rates[w["start"]] = round(w["rate_p"], 4)
            except Exception as exc:
                log.warning("Export rate fetch failed for %s: %s", source, exc)

        if len(rates) <= 1:
            rate = float(slots[0]["value_inc_vat"])
            _save_fetched_rates(import_p=rate,
                                export_p=exp_base_rate,
                                standing_p=sc if sc > 0 else None,
                                tou_windows=[])
            msg = f"{label}: {rate:.2f}p/kWh flat"
        else:
            base_rate, windows = _extract_tou_windows(slots, label="import")
            if not windows:
                # _extract_tou_windows() found nothing (e.g. all slots have
                # valid_to=None — open-ended flat tariff with a recent rate change).
                # Pick the most recently started currently-active rate instead.
                now_str = now_utc.strftime("%Y-%m-%dT%H:%M")
                active = sorted(
                    [s for s in slots if s.get("valid_from", "") <= now_str],
                    key=lambda s: s.get("valid_from", ""), reverse=True
                )
                if active:
                    base_rate = float(active[0]["value_inc_vat"])
                _save_fetched_rates(import_p=base_rate,
                                    export_p=exp_base_rate,
                                    standing_p=sc if sc > 0 else None,
                                    tou_windows=[])
                msg = f"{label}: {base_rate:.2f}p/kWh flat"
                _fetch_status.update({"ok": True, "msg": msg,
                                      "fetched_at": _RATES_LAST_FETCHED,
                                      "slots_today": 0, "slots_tomorrow": 0})
                return {"ok": True, "msg": msg}
            # Annotate each TOU window with its export rate
            if source in _MIRROR_EXPORT_SOURCES:
                # Export rate == import rate for this tariff
                for w in windows:
                    w["export_rate_p"] = w["rate_p"]
                exp_base_rate = base_rate
            else:
                for w in windows:
                    if w["start"] in exp_window_rates:
                        w["export_rate_p"] = exp_window_rates[w["start"]]
            _save_fetched_rates(import_p=base_rate,
                                export_p=exp_base_rate,
                                standing_p=sc if sc > 0 else None,
                                tou_windows=windows)
            if windows:
                peak_desc = ", ".join(
                    f"{w['name']} {w['start']}–{w['end']} "
                    f"{w['rate_p']:.2f}p in"
                    + (f"/{w['export_rate_p']:.2f}p out" if w.get('export_rate_p') else "")
                    for w in windows
                )
                msg = f"{label}: {base_rate:.2f}p base · {peak_desc}"
            else:
                msg = f"{label}: {base_rate:.2f}p/kWh"
        _fetch_status.update({"ok": True, "msg": msg, "fetched_at": _RATES_LAST_FETCHED,
                              "slots_today": 0, "slots_tomorrow": 0})
        return {"ok": True, "msg": msg}
    except Exception as exc:
        msg = f"{source} fetch failed: {exc}"
        log.warning(msg)
        _fetch_status.update({"ok": False, "msg": msg})
        return {"ok": False, "msg": msg}


def _do_rate_fetch() -> dict:
    """Thread-safe dispatcher: fetch rates from the configured tariff source."""
    if TARIFF_SOURCE in ("manual", ""):
        return {"ok": False, "msg": "Manual tariff — no fetch needed"}
    if not OCTOPUS_REGION:
        return {"ok": False, "msg": "Region code not configured — enter your region (A–P) or use postcode look-up"}
    if not _fetch_lock.acquire(blocking=False):
        return {"ok": True, "msg": "Fetch in progress — rates will update shortly"}
    try:
        src = TARIFF_SOURCE
        if src in _VARIABLE_RATE_SOURCES:
            result = _fetch_agile_rates()
        elif src == "octopus_tracker":
            result = _fetch_tracker_rates()
        elif src in ("octopus_cosy", "octopus_go", "octopus_flux",
                     "octopus_intelligent_flux", "octopus_flexible"):
            result = _fetch_static_rates(src)
        else:
            result = {"ok": False, "msg": f"Unknown source: {src}"}
        # One INFO line per fetch records the derived base rate + windows, so a
        # "rates look wrong after fetch" report (e.g. #55) is diagnosable from the
        # log alone. DEBUG detail lives in the fetch/extract helpers below.
        if result.get("ok"):
            log.info("Tariff fetch (%s): %s", src, result.get("msg", ""))
        else:
            log.warning("Tariff fetch (%s) failed: %s", src, result.get("msg", ""))
        return result
    except Exception as exc:
        msg = f"Rate fetch error: {exc}"
        log.error(msg)
        return {"ok": False, "msg": msg}
    finally:
        _fetch_lock.release()


def _startup_fetch() -> None:
    """Fetch rates at startup if data is missing or stale."""
    if TARIFF_SOURCE in ("manual", "") or not OCTOPUS_REGION:
        return
    should_fetch = not _RATES_LAST_FETCHED
    if not should_fetch and TARIFF_SOURCE in _VARIABLE_RATE_SOURCES:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            with _db() as conn:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM agile_rates WHERE slot_start LIKE ?",
                    (today + "%",)
                ).fetchone()[0]
            should_fetch = cnt < 20
        except Exception:
            should_fetch = True
    elif not should_fetch:
        try:
            last = datetime.fromisoformat(_RATES_LAST_FETCHED)
            should_fetch = last.date() < datetime.now().date()
        except Exception:
            should_fetch = True
    if should_fetch:
        log.info("Auto-tariff: fetching rates at startup")
        _do_rate_fetch()


def _rate_fetch_loop() -> None:
    """Background thread: daily fetch at 16:30 + Agile retry at 20:00."""
    _startup_fetch()
    last_day_fetched = ""
    last_day_retried = ""
    while True:
        time.sleep(60)
        now  = datetime.now()
        date = now.strftime("%Y-%m-%d")
        if now.hour == 16 and now.minute == 30 and date != last_day_fetched:
            last_day_fetched = date
            _do_rate_fetch()
        if (TARIFF_SOURCE in _VARIABLE_RATE_SOURCES
                and now.hour == 20 and now.minute == 0
                and date != last_day_retried):
            last_day_retried = date
            tom = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                with _db() as conn:
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM agile_rates WHERE slot_start LIKE ?",
                        (tom + "%",)
                    ).fetchone()[0]
            except Exception:
                cnt = 0
            if cnt < 45:
                log.info("Agile: only %d slots for tomorrow — retrying fetch", cnt)
                _do_rate_fetch()


DB_PATH           = _DATA_DIR / "history.db"
_QA_STATE_PATH    = _DATA_DIR / "quick_action_state.json"
BACKUPS_DIR       = _DATA_DIR / "backups"
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
        log.info("Daily backup written: %s", dest.name)
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
        log.info("Imported database applied from staged restore.")
    except Exception as exc:
        log.error("Failed to apply pending import: %s", exc)

_LOG_LEVELS    = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}
_log_level_int = _LOG_LEVELS.get(LOG_LEVEL, logging.WARNING)
logging.basicConfig(
    level=_log_level_int,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dashboard")

_log_file_handler = None
_LOG_PATH = _DATA_DIR / "dashboard.log"
try:
    _log_file_handler = logging.handlers.TimedRotatingFileHandler(
        _LOG_PATH, when="midnight", backupCount=LOG_RETENTION_DAYS, encoding="utf-8"
    )
    _log_file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logging.getLogger().addHandler(_log_file_handler)
except Exception as _log_err:
    print(f"[WARNING] Could not open log file {_LOG_PATH}: {_log_err}")

# The givenergy-modbus library logs a multi-line traceback on every transient
# read failure (very common on Modbus TCP). These are harmless blips that we
# already handle ourselves, so silence the library's own noisy error logging.
logging.getLogger("givenergy_modbus").setLevel(logging.CRITICAL)
# pymodbus (the transport underneath) can be chatty too.
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)


def _set_log_level(level_name: str) -> None:
    """Change log verbosity at runtime without restarting."""
    global LOG_LEVEL
    lvl = _LOG_LEVELS.get(level_name.lower(), logging.WARNING)
    logging.getLogger().setLevel(lvl)
    LOG_LEVEL = level_name.lower()

# ── Database ───────────────────────────────────────────────────────────────────
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    # ── Schema migration policy ────────────────────────────────────────────────
    # This runs on every startup and upgrades an existing database IN PLACE
    # without ever dropping data.  PRAGMA user_version tracks which migrations
    # have been applied; increment _SCHEMA_VERSION and add a guarded block below
    # for each new change.  Existing tables use CREATE IF NOT EXISTS; new columns
    # use ALTER TABLE guarded by a PRAGMA table_info check.
    with _db() as conn:
        _db_version = conn.execute("PRAGMA user_version").fetchone()[0]
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
        sched_cols = [r[1] for r in conn.execute("PRAGMA table_info(schedules)")]
        if "power_pct" not in sched_cols:
            conn.execute("ALTER TABLE schedules ADD COLUMN power_pct INTEGER NOT NULL DEFAULT 50")
        if "predictive" not in sched_cols:
            conn.execute("ALTER TABLE schedules ADD COLUMN predictive INTEGER NOT NULL DEFAULT 0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_costs (
                date              TEXT PRIMARY KEY,   -- YYYY-MM-DD
                import_cost_p     REAL NOT NULL DEFAULT 0,
                export_income_p   REAL NOT NULL DEFAULT 0,
                standing_charge_p REAL NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agile_rates (
                slot_start TEXT PRIMARY KEY,   -- '2026-06-09T16:00:00Z' (UTC)
                slot_end   TEXT NOT NULL,
                import_p   REAL NOT NULL DEFAULT 0,
                export_p   REAL NOT NULL DEFAULT 0
            )
        """)
        # ── Mark schema version 1 (all tables above + additive column migrations)
        if _db_version < 1:
            conn.execute(f"PRAGMA user_version = 1")
        # Future migrations: add `if _db_version < N:` blocks here and
        # bump _SCHEMA_VERSION at the top of the file.
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


def _recompute_today_cost(grid_in_today: float = 0.0, grid_out_today: float = 0.0):
    """Recompute today's costs from snapshot history.  Call from poll thread only.

    Uses actual timestamp deltas between consecutive snapshots rather than
    POLL_INTERVAL to avoid the ~20% undercount caused by per-poll processing
    overhead (actual interval ≈ 12–13 s vs configured 10 s).

    Separates energy into TOU-window and flat-rate portions.  The flat-rate
    portion is back-filled from the inverter register totals (grid_in_today /
    grid_out_today) so that any service-stop gaps don't undercount costs.
    """
    global _today_import_cost_p, _today_export_income_p, _today_cost_date
    today_str = datetime.now().strftime("%Y-%m-%d")

    imp_tou  = 0.0   # pence: cost from snapshots within TOU windows
    exp_tou  = 0.0
    tou_imp_kwh = 0.0  # kWh inside TOU windows (to subtract from register)
    tou_exp_kwh = 0.0

    if _tariff_configured():
        try:
            with _db() as conn:
                snap_rows = list(conn.execute(
                    "SELECT ts, grid_w, grid_importing, grid_exporting "
                    "FROM snapshots "
                    "WHERE date(ts,'unixepoch','localtime')=date('now','localtime') "
                    "ORDER BY ts"
                ))
            prev_ts = None
            for row in snap_rows:
                if prev_ts is None:
                    prev_ts = row["ts"]
                    continue
                delta = min(row["ts"] - prev_ts, 120.0)   # cap at 2 min (skip big service gaps)
                prev_ts = row["ts"]
                kwh = (row["grid_w"] or 0) / 1000.0 * delta / 3600.0
                if _in_tou_window(row["ts"]):
                    if row["grid_importing"]:
                        imp_tou     += kwh * _tariff_import_rate(row["ts"])
                        tou_imp_kwh += kwh
                    elif row["grid_exporting"]:
                        exp_tou     += kwh * _tariff_export_rate(row["ts"])
                        tou_exp_kwh += kwh
        except Exception:
            pass

    # Flat-rate portion: prefer register total minus TOU kWh.  This automatically
    # fills any snapshot gap (service restarts, skipped polls, etc.) because the
    # inverter daily counter is always accurate.
    flat_imp = max(0.0, grid_in_today  - tou_imp_kwh) * TARIFF_IMPORT_P
    flat_exp = max(0.0, grid_out_today - tou_exp_kwh) * TARIFF_EXPORT_P

    _today_import_cost_p   = imp_tou  + flat_imp
    _today_export_income_p = exp_tou  + flat_exp
    _today_cost_date       = today_str


def _save_day_end_cost(date_str: str, import_cost_p: float, export_income_p: float):
    """Persist a day's final cost totals to daily_costs.  Overwrites if exists."""
    try:
        with _db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO daily_costs "
                "(date, import_cost_p, export_income_p, standing_charge_p) "
                "VALUES (?,?,?,?)",
                (date_str, import_cost_p, export_income_p, TARIFF_STANDING_P),
            )
            conn.commit()
    except Exception as exc:
        log.warning("Failed to save daily cost for %s: %s", date_str, exc)


def _dates_for_period(period_type: str, period_key: str) -> list:
    """Return list of YYYY-MM-DD strings for every day in the period up to today."""
    today = datetime.now().date()
    dates: list = []
    if period_type == "day":
        return [period_key]
    if period_type == "week":
        try:
            monday = datetime.strptime(period_key, "%Y-%m-%d").date()
        except ValueError:
            return []
        for i in range(7):
            d = monday + timedelta(days=i)
            if d <= today:
                dates.append(d.isoformat())
    elif period_type == "month":
        try:
            y, m = map(int, period_key.split("-"))
        except ValueError:
            return []
        d = datetime(y, m, 1).date()
        while d.month == m and d <= today:
            dates.append(d.isoformat())
            d += timedelta(days=1)
    elif period_type == "year":
        try:
            y = int(period_key)
        except ValueError:
            return []
        d = datetime(y, 1, 1).date()
        while d.year == y and d <= today:
            dates.append(d.isoformat())
            d += timedelta(days=1)
    return dates


def _batch_compute_and_store_costs(dates: list):
    """Compute costs for a list of past days in one DB scan and store in daily_costs.
    Handles both flat-rate (uses last-snapshot register) and TOU (uses actual ts deltas)."""
    if not dates:
        return
    ph = ",".join("?" * len(dates))
    imp_tou_by_day     = {d: 0.0 for d in dates}
    exp_tou_by_day     = {d: 0.0 for d in dates}
    tou_imp_kwh_by_day = {d: 0.0 for d in dates}
    tou_exp_kwh_by_day = {d: 0.0 for d in dates}
    grid_in_by_day     = {d: 0.0 for d in dates}
    grid_out_by_day    = {d: 0.0 for d in dates}
    try:
        with _db() as conn:
            # Last register reading per day (flat-rate back-fill basis)
            for row in conn.execute(
                f"SELECT date(ts,'unixepoch','localtime') AS day, "
                f"grid_in_today, grid_out_today FROM snapshots "
                f"WHERE ts IN ("
                f"  SELECT MAX(ts) FROM snapshots "
                f"  WHERE date(ts,'unixepoch','localtime') IN ({ph}) "
                f"  GROUP BY date(ts,'unixepoch','localtime'))",
                dates,
            ):
                grid_in_by_day[row["day"]]  = row["grid_in_today"]  or 0.0
                grid_out_by_day[row["day"]] = row["grid_out_today"] or 0.0
            # All snapshots ordered by day then ts (for actual-delta computation)
            snap_rows = list(conn.execute(
                f"SELECT ts, grid_w, grid_importing, grid_exporting, "
                f"date(ts,'unixepoch','localtime') AS day "
                f"FROM snapshots "
                f"WHERE date(ts,'unixepoch','localtime') IN ({ph}) "
                f"ORDER BY day, ts",
                dates,
            ))
    except Exception as exc:
        log.warning("_batch_compute_and_store_costs DB error: %s", exc)
        return
    prev_by_day: dict = {}
    for row in snap_rows:
        day = row["day"]
        if day not in prev_by_day:
            prev_by_day[day] = row["ts"]
            continue
        delta = min(row["ts"] - prev_by_day[day], 120.0)
        prev_by_day[day] = row["ts"]
        kwh = (row["grid_w"] or 0) / 1000.0 * delta / 3600.0
        if _in_tou_window(row["ts"]):
            if row["grid_importing"]:
                imp_tou_by_day[day]     += kwh * _tariff_import_rate(row["ts"])
                tou_imp_kwh_by_day[day] += kwh
            elif row["grid_exporting"]:
                exp_tou_by_day[day]     += kwh * _tariff_export_rate(row["ts"])
                tou_exp_kwh_by_day[day] += kwh
    rows_to_insert = []
    for day in dates:
        flat_imp = max(0.0, grid_in_by_day[day]  - tou_imp_kwh_by_day[day]) * TARIFF_IMPORT_P
        flat_exp = max(0.0, grid_out_by_day[day] - tou_exp_kwh_by_day[day]) * TARIFF_EXPORT_P
        imp = imp_tou_by_day[day] + flat_imp
        exp = exp_tou_by_day[day] + flat_exp
        if imp > 0 or exp > 0 or grid_in_by_day[day] > 0:
            rows_to_insert.append((day, round(imp, 4), round(exp, 4), TARIFF_STANDING_P))
    if rows_to_insert:
        try:
            with _db() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO daily_costs "
                    "(date, import_cost_p, export_income_p, standing_charge_p) VALUES (?,?,?,?)",
                    rows_to_insert,
                )
                conn.commit()
        except Exception as exc:
            log.warning("Failed to store batch daily costs: %s", exc)


def _ensure_daily_costs(dates: list):
    """Check daily_costs for missing past dates and compute them in batch."""
    if not dates or not _tariff_configured():
        return
    ph = ",".join("?" * len(dates))
    try:
        with _db() as conn:
            cached = {r[0] for r in conn.execute(
                f"SELECT date FROM daily_costs WHERE date IN ({ph})", dates
            )}
    except Exception:
        cached = set()
    missing = [d for d in dates if d not in cached]
    if missing:
        _batch_compute_and_store_costs(missing)


def _get_costs_for_period(period_type: str, period_key: str) -> dict:
    """Aggregate cost totals for a history period using daily_costs cache.
    Returns dict with import/export/standing/total/net keys (all in pence)."""
    if not _tariff_configured():
        return {}
    dates = _dates_for_period(period_type, period_key)
    if not dates:
        return {}
    today = datetime.now().strftime("%Y-%m-%d")
    past_dates = [d for d in dates if d != today]
    _ensure_daily_costs(past_dates)
    imp = exp = sc = 0.0
    if past_dates:
        ph = ",".join("?" * len(past_dates))
        try:
            with _db() as conn:
                r = conn.execute(
                    f"SELECT COALESCE(SUM(import_cost_p),0), "
                    f"COALESCE(SUM(export_income_p),0), "
                    f"COALESCE(SUM(standing_charge_p),0) "
                    f"FROM daily_costs WHERE date IN ({ph})",
                    past_dates,
                ).fetchone()
            imp += r[0]; exp += r[1]; sc += r[2]
        except Exception:
            pass
    # Today: read from the live cache (thread-safe via _lock)
    if today in dates:
        with _lock:
            imp += _cached.get("import_cost_p",   0) or 0
            exp += _cached.get("export_income_p", 0) or 0
        sc += TARIFF_STANDING_P
    return {
        "import_cost_p":     round(imp, 2),
        "export_income_p":   round(exp, 2),
        "standing_charge_p": round(sc,  2),
        "total_cost_p":      round(imp + sc, 2),
        "net_cost_p":        round(imp + sc - exp, 2),
    }


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

# SOC spike filter — maximum believable SOC change in one poll.
# At max charge rate ~2600 W on a 9.5 kWh battery, SOC can change at most
# ~0.08 % per 10 s poll.  A threshold of 5 % is ~60× the physical maximum,
# so this will never suppress real movement while catching corrupt IR59 reads.
_SOC_MAX_DELTA = 5
_last_soc:       int   | None = None

# BMS splice guard — track last good voltage and temperature so we can detect
# the sub-bus corruption signature documented in givenergy-modbus #256:
# the dongle re-frames corrupt RS485 data with a valid CRC, causing v_battery
# and t_battery to drop to 0 simultaneously while SOC also zeroes.
_last_v_battery: float | None = None
_last_t_battery: float | None = None

def _smooth(data: dict) -> dict:
    """Return a copy of data with zero-blips, SOC spikes, and BMS splice corruption suppressed."""
    global _last_soc, _last_v_battery, _last_t_battery
    out = dict(data)

    # Zero-blip debounce for power fields
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

    # SOC spike filter — reject single-poll jumps that exceed the physical maximum
    soc = out.get("soc")
    if soc is not None:
        if _last_soc is not None and _last_soc > 0 and abs(soc - _last_soc) > _SOC_MAX_DELTA:
            log.warning("SOC spike suppressed: %d%% → %d%% (held at %d%%)",
                        _last_soc, soc, _last_soc)
            out["soc"] = _last_soc
        elif soc == 0 and not _last_soc:
            # IR59 stuck at 0 with no good prior value: estimate from the BMS
            # capacity registers (rate-limited). _last_soc stays unseeded so a
            # real IR59 value is accepted the moment one appears.
            est = _bms_soc_fallback_value()
            if est:
                out["soc"] = est
                out["soc_source"] = "bms"
        else:
            _last_soc = soc

    # BMS splice guard — simultaneous zero of v_battery + t_battery is the
    # sub-bus splice corruption signature (givenergy-modbus #256). Genuine
    # readings cannot both be zero at the same time: no battery reads 0 V while
    # also at exactly 0 °C. Hold last-good for the whole cohort and also hold
    # SOC if it zeroed in the same poll.
    v_bat = out.get("v_battery")
    t_bat = out.get("t_battery")
    if (v_bat is not None and v_bat <= 0 and _last_v_battery
            and t_bat is not None and t_bat <= 0 and _last_t_battery is not None):
        log.warning("BMS splice suspected: v_battery=%.2f t_battery=%.1f "
                    "-- holding last-good (%.2f V / %.1f C)",
                    v_bat, t_bat, _last_v_battery, _last_t_battery)
        out["v_battery"] = _last_v_battery
        out["t_battery"] = _last_t_battery
        if out.get("soc") == 0 and _last_soc:
            out["soc"] = _last_soc
    else:
        if v_bat and v_bat > 0:
            _last_v_battery = v_bat
        if t_bat is not None and t_bat != 0:
            _last_t_battery = t_bat

    return out

# ── Weather ────────────────────────────────────────────────────────────────────
_weather_cached: dict = {}
_last_weather_ts: float = 0.0

# ── Solar forecast ─────────────────────────────────────────────────────────────
_sf_cached: dict = {}
_last_sf_ts: float = 0.0
_sf_fetch_active = False

def _weather_interval():
    return max(5, WEATHER_POLL_MINS) * 60

def _fetch_weather() -> dict:
    """Fetch latest land observation from Met Office DataHub."""
    url = f"https://data.hub.api.metoffice.gov.uk/observation-land/1/{MET_GEOHASH}"
    log.debug("Weather fetch: GET %s", url)
    req = urllib.request.Request(
        url, headers={"apikey": MET_API_KEY, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        obs = json.loads(r.read())
    latest = next(
        (o for o in reversed(obs) if "temperature" in o and "weather_code" in o), None)
    if not latest:
        raise ValueError("No complete observation in response")
    log.debug("Weather fetch: %d observation(s), using %s",
              len(obs) if isinstance(obs, list) else 0, latest.get("datetime", ""))
    return {
        "ok":           True,
        "temp":         round(latest["temperature"], 1),
        "weather_code": latest["weather_code"],
        "wind_dir":     latest.get("wind_direction", ""),
        "wind_mph":     round((latest.get("wind_speed") or 0) * 2.237),
        "humidity":     latest.get("humidity"),
        "updated":      latest.get("datetime", ""),
    }


def _met_nearest_station(lat: float, lng: float) -> dict:
    """Call the Met Office /nearest endpoint to find the closest observation station.
    Returns the first result dict, e.g. {"geohash": "gcj8ds", "area": "Devon", ...}.
    Raises on any network or API error."""
    # Met Office /nearest requires at most 2 decimal places on lat/lon
    url = (f"https://data.hub.api.metoffice.gov.uk/observation-land/1/nearest"
           f"?lat={lat:.2f}&lon={lng:.2f}")
    log.debug("Met Office /nearest: GET %s", url)
    req = urllib.request.Request(
        url, headers={"apikey": MET_API_KEY, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        results = json.loads(r.read())
    if not results:
        raise ValueError("No station found near that location")
    log.debug("Met Office /nearest: %d result(s), nearest geohash=%s",
              len(results), results[0].get("geohash", ""))
    return results[0]

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
        "soc":           max(0, min(100, g(59))),         # BATTERY_PERCENT — clamp uint16 to valid %
        "charge_status": g(14),                        # CHARGE_STATUS (0=idle 2=charging 3=finishing 5=discharging)
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
        "soc":           _gateway_soc,
        "charge_status": None,
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
        "soc":           soc,
        "charge_status": int(iv.charge_status or 0) if iv.charge_status is not None else None,
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
    global _cached, _error, _today_import_cost_p, _today_export_income_p
    global _tariff_dirty, _last_cost_poll_ts
    global _today_register_date, _prev_grid_in_today, _prev_grid_out_today
    data = _smooth(data)             # suppress zero-blips before DB write and display
    _log_snapshot(data)              # smoothed values to DB

    # ── Cost estimation ─────────────────────────────────────────────────────────
    # Flat rate: inverter register × rate (authoritative, unaffected by restarts).
    # TOU: snapshot accumulator with register correction for accuracy.
    _today_date = datetime.now().strftime("%Y-%m-%d")

    # End-of-day capture: when the date rolls over, persist yesterday's final cost
    _use_accumulator = bool(TARIFF_TOU) or TARIFF_SOURCE in _VARIABLE_RATE_SOURCES
    if _tariff_configured() and _today_register_date and _today_date != _today_register_date:
        if _use_accumulator:
            _save_day_end_cost(_today_register_date,
                               _today_import_cost_p, _today_export_income_p)
        else:
            _save_day_end_cost(
                _today_register_date,
                _prev_grid_in_today  * TARIFF_IMPORT_P,
                _prev_grid_out_today * TARIFF_EXPORT_P,
            )
    _today_register_date = _today_date

    if _tariff_configured():
        if not _use_accumulator:
            # Flat rate: inverter register × rate — always accurate
            data["import_cost_p"]   = data.get("grid_in_today",  0) * TARIFF_IMPORT_P
            data["export_income_p"] = data.get("grid_out_today", 0) * TARIFF_EXPORT_P
        else:
            # TOU / Agile accumulator with register-corrected recompute.
            # On a true day rollover (not a config change or first-run), don't
            # pass the inverter register to the recompute — the inverter may not
            # have reset its daily counter yet (midnight carryover bug).
            if _tariff_dirty or _today_date != _today_cost_date:
                _trust_reg = _tariff_dirty or (_today_cost_date == "")
                _recompute_today_cost(
                    grid_in_today  = float(data.get("grid_in_today",  0) or 0) if _trust_reg else 0.0,
                    grid_out_today = float(data.get("grid_out_today", 0) or 0) if _trust_reg else 0.0,
                )
                _tariff_dirty      = False
                _last_cost_poll_ts = 0.0
            else:
                now   = time.time()
                delta = (min(now - _last_cost_poll_ts, 120.0)
                         if _last_cost_poll_ts > 0 else float(POLL_INTERVAL))
                kwh   = data.get("grid_w", 0) / 1000.0 * delta / 3600.0
                if data.get("grid_importing"):
                    _today_import_cost_p   += kwh * _tariff_import_rate(now)
                elif data.get("grid_exporting"):
                    _today_export_income_p += kwh * _tariff_export_rate(now)
                _last_cost_poll_ts = now

            # Register-consistency guard — catches any carryover that still
            # slipped through (e.g. service was already running when this fix
            # was deployed).  Runs every poll so it self-heals within one cycle.
            _gin      = float(data.get("grid_in_today",  0) or 0)
            _gout     = float(data.get("grid_out_today", 0) or 0)
            # For Agile the cap is 100p/kWh; for TOU use the highest window rate
            _max_rate = (100.0 if TARIFF_SOURCE in _VARIABLE_RATE_SOURCES
                         else (max([TARIFF_IMPORT_P] + [w["rate_p"] for w in TARIFF_TOU]) or 100.0))
            if _gout < 0.01 and _today_export_income_p > 1.0:
                # Register shows no export today → income must be zero
                _today_export_income_p = 0.0
            if (_gin < 0.01 and _today_import_cost_p > 1.0) or \
               (_gin > 0  and _today_import_cost_p > _gin * _max_rate * 1.1):
                # Cost implies more energy than the register recorded → recompute
                _recompute_today_cost(_gin, _gout)

            data["import_cost_p"]   = _today_import_cost_p
            data["export_income_p"] = _today_export_income_p
        data["currency_symbol"]   = TARIFF_CURRENCY
        data["standing_charge_p"] = TARIFF_STANDING_P

    # Track previous-poll register values for flat-rate end-of-day capture
    _prev_grid_in_today  = float(data.get("grid_in_today",  0) or 0)
    _prev_grid_out_today = float(data.get("grid_out_today", 0) or 0)

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

_weather_fetch_active = False   # guard: only one background fetch at a time

def _maybe_weather():
    """Schedule a background weather fetch if the interval has elapsed.
    Non-blocking: the HTTP call runs in a daemon thread so the listen loop
    is never stalled waiting for a network response."""
    global _weather_cached, _last_weather_ts, _weather_fetch_active
    if not (MET_API_KEY and MET_GEOHASH):
        return
    if time.time() - _last_weather_ts <= _weather_interval():
        return
    if _weather_fetch_active:
        return
    _weather_fetch_active = True
    _last_weather_ts = time.time()  # pre-mark so the interval doesn't re-fire immediately

    def _fetch_bg():
        global _weather_cached, _weather_fetch_active
        try:
            wx = _fetch_weather()
            with _lock:
                _weather_cached = wx
            log.info("Weather: %s°C code=%s", wx["temp"], wx["weather_code"])
        except Exception as exc:
            log.warning("Weather fetch failed: %s", exc)
        finally:
            _weather_fetch_active = False

    threading.Thread(target=_fetch_bg, daemon=True).start()

def _fetch_solar_forecast() -> dict:
    """Fetch 2-day forecast from Forecast.Solar and return a normalised result.

    The live API returns each series as a dict keyed by date/timestamp, e.g.
    ``"watt_hours_day": {"2026-06-13": 24989, ...}`` and
    ``"watt_hours_period": {"2026-06-13 06:00:00": 73, ...}`` — NOT the
    list-of-objects shape shown in the docs.  We normalise the time-keyed series
    into sorted lists so the frontend has a stable contract:
      watt_hours_day    -> [{"date": "...",      "value": N}, ...]
      watt_hours_period -> [{"timestamp": "...", "value": N}, ...]
    """
    if SF_API_KEY:
        url = (f"https://api.forecast.solar/{SF_API_KEY}/estimate"
               f"/{SF_LAT}/{SF_LON}/{SF_TILT}/{SF_AZ}/{SF_KWP}")
    else:
        url = (f"https://api.forecast.solar/estimate"
               f"/{SF_LAT}/{SF_LON}/{SF_TILT}/{SF_AZ}/{SF_KWP}")
    log.debug("Solar forecast fetch: GET %s",
              url.replace(SF_API_KEY, "<key>") if SF_API_KEY else url)
    data = _http_get_json(url, timeout=15)
    if data.get("message", {}).get("code", -1) != 0:
        raise ValueError(f"Forecast.Solar error: {data.get('message')}")
    rl = data.get("ratelimit", {})
    if rl.get("remaining", 1) == 0:
        raise ValueError("Forecast.Solar rate limit reached")
    raw = data.get("result", {})
    log.debug("Solar forecast fetch: ratelimit remaining=%s, %d result series",
              rl.get("remaining"), len(raw) if isinstance(raw, dict) else 0)

    def _to_list(series, key):
        """Convert a {key_str: value} dict to a sorted [{key: ..., value: ...}]."""
        if isinstance(series, dict):
            return [{key: k, "value": v} for k, v in sorted(series.items())]
        if isinstance(series, list):
            return series   # already in list form (defensive)
        return []

    return {
        "watt_hours_day":    _to_list(raw.get("watt_hours_day"),    "date"),
        "watt_hours_period": _to_list(raw.get("watt_hours_period"), "timestamp"),
        "watts":             _to_list(raw.get("watts"),             "timestamp"),
        "watt_hours":        _to_list(raw.get("watt_hours"),        "timestamp"),
    }


_SF_OK_INTERVAL   = 3600   # re-fetch hourly after a success
_SF_FAIL_INTERVAL = 300    # retry after 5 min on failure (rate limit is 12/hour)

def _maybe_solar_forecast():
    """Schedule a background solar forecast fetch if the interval has elapsed.
    Non-blocking: HTTP call runs in a daemon thread.  Re-fetches hourly on
    success; retries after 5 min on failure (well within the 12/hour limit)."""
    global _sf_cached, _last_sf_ts, _sf_fetch_active
    if not _sf_configured():
        return
    interval = _SF_OK_INTERVAL if _sf_cached else _SF_FAIL_INTERVAL
    if time.time() - _last_sf_ts <= interval:
        return
    if _sf_fetch_active:
        return
    _sf_fetch_active = True
    _last_sf_ts = time.time()   # pre-mark so the interval doesn't re-fire immediately

    def _fetch_bg():
        global _sf_cached, _sf_fetch_active
        try:
            result = _fetch_solar_forecast()
            with _lock:
                _sf_cached = result
            days = result.get("watt_hours_day", [])
            log.info("Solar forecast updated: %d day(s), %d period(s)",
                     len(days), len(result.get("watt_hours_period", [])))
        except Exception as exc:
            log.warning("Solar forecast fetch failed: %s", exc)
        finally:
            _sf_fetch_active = False

    threading.Thread(target=_fetch_bg, daemon=True).start()


# ── Predictive charge target ──────────────────────────────────────────────────

def _get_tomorrow_forecast_kwh() -> "float | None":
    """Return tomorrow's total solar forecast in kWh from the cache, or None."""
    if not _sf_cached:
        return None
    days  = _sf_cached.get("watt_hours_day", [])
    today = datetime.now().date().isoformat()
    entry = next((d for d in days if d["date"] > today), None)
    return round(entry["value"] / 1000.0, 2) if entry else None


_MORNING_START = 6    # 06:00 -- typical end of overnight charge slot
_MORNING_END   = 13   # 13:00 -- solar usually exceeds load by this point
_DEMAND_DAYS   = 60   # rolling window for historical demand
_DEMAND_TOP_PC = 0.25 # fraction of highest-demand days to average


def _compute_morning_demand_kwh() -> "float | None":
    """Return expected morning demand (kWh) from historical snapshots.

    Sums home_w over the morning window (_MORNING_START to _MORNING_END) for
    each of the last _DEMAND_DAYS days, then averages the top _DEMAND_TOP_PC
    fraction (highest-demand days) to give a conservatively high estimate.
    Returns None if fewer than 3 days of data are available."""
    try:
        with _db() as conn:
            rows = list(conn.execute(
                "SELECT date(ts,'unixepoch','localtime') AS day, ts, home_w "
                "FROM snapshots "
                "WHERE ts >= unixepoch('now','localtime',? || ' days') "
                "  AND CAST(strftime('%H',ts,'unixepoch','localtime') AS INTEGER) "
                "      BETWEEN ? AND ? "
                "ORDER BY ts",
                (f"-{_DEMAND_DAYS}", _MORNING_START, _MORNING_END - 1)
            ))
    except Exception:
        return None

    # Accumulate kWh per day using trapezoidal-style gap-capped integration
    days: "dict[str, list]" = {}
    for row in rows:
        days.setdefault(row["day"], []).append(row)

    daily_kwh = []
    for day_rows in days.values():
        kwh = 0.0
        prev_ts = None
        for row in sorted(day_rows, key=lambda r: r["ts"]):
            if prev_ts is not None:
                delta = min(row["ts"] - prev_ts, 120.0)
                kwh  += (row["home_w"] or 0) / 1000.0 * delta / 3600.0
            prev_ts = row["ts"]
        if kwh > 0.01:
            daily_kwh.append(kwh)

    if len(daily_kwh) < 3:
        return None

    daily_kwh.sort(reverse=True)
    n = max(1, round(len(daily_kwh) * _DEMAND_TOP_PC))
    return round(sum(daily_kwh[:n]) / n, 2)


def _compute_predictive_target() -> "int | None":
    """Compute the overnight charge SOC target from tomorrow's solar forecast
    and historical morning demand.  Returns None if forecast or battery capacity
    is not configured, so callers fall back to the rule's fixed target_soc."""
    if PRED_BATTERY_KWH <= 0:
        return None
    kwh = _get_tomorrow_forecast_kwh()
    if kwh is None:
        return None
    morning_demand = _compute_morning_demand_kwh()
    if morning_demand is None:
        morning_demand = PRED_EVENING_RESERVE   # fallback until enough history
    usable = kwh * PRED_PESSIMISM - morning_demand
    raw    = 100.0 * (1.0 - usable / PRED_BATTERY_KWH)
    return int(max(PRED_MIN_SOC, min(100, round(raw))))


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
        log.debug("Update check: GET %s", _GH_API)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        latest_tag  = data.get("tag_name", "")
        latest_name = data.get("name", latest_tag)[:80]   # release title, capped
        available   = _parse_version(latest_tag) > _parse_version(APP_VERSION)
        log.debug("Update check: latest_tag=%s name=%s available=%s",
                  latest_tag, latest_name, available)
        _update_info = {
            "available":    available,
            "current":      APP_VERSION,
            "latest":       latest_tag.lstrip("v"),
            "release_name": latest_name,
            "url":          "https://software.andrewcampbell.co.uk/release-notes.html",
            "checked_at":   int(time.time()),
        }
        log.info("Update check: current v%s, latest %s (%s)",
                 APP_VERSION, latest_tag or "?",
                 "update available" if available else "up to date")
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
    if _LIB is None:
        # _data_loop falls back to listen mode before calling us, so this only
        # fires if that guard is ever lost — fail with a clear message rather
        # than a NameError from the missing library shims (issue #2).
        raise RuntimeError(
            "Poll mode requires the givenergy-modbus library, which is not "
            "installed. Set mode=listen in config.ini or install the library.")
    fail = 0
    FAIL_THRESHOLD = 3   # tolerate brief Modbus blips before flagging offline
    if _API_V2:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    while True:
        try:
            data = loop.run_until_complete(_fetch_v2()) if _API_V2 else _fetch_v0()
            _handle_reading(data, st)
            log.debug("Polled: solar=%dW soc=%d%%", data["solar_w"], data["soc"])
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
        _maybe_solar_forecast()
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
    if _inverter_profile and _inverter_profile != "unknown":
        return   # already successfully detected
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
                # Read whatever has arrived
                try:
                    chunk = s.recv(8192)
                    if not chunk:  # EOF -- dongle closed the connection
                        raise ConnectionError("inverter connection closed")
                except socket.timeout:
                    chunk = b""
                if chunk:
                    buf.extend(chunk)
                    for frame in _pop_data_frames(buf):
                        if _is_heartbeat_frame(frame):
                            last_frame = _note_heartbeat()  # reset watchdog, send nothing
                            if not st.get("hb_seen"):
                                log.debug("Heartbeat received — connection alive")
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
                    log.debug("Listen: solar=%dW soc=%d%%", latest["solar_w"], latest["soc"])
                    last_proc = now
                _maybe_weather()
                _maybe_solar_forecast()
                _maybe_check_update()
                _maybe_quick_action_tick()
                # Service any pending snapshot request from the QA thread.
                # Must run on this thread (socket owner) to avoid concurrent access.
                if _snap_req.is_set() and _inverter_slave:
                    global _snap_hr_data, _snap_poll_reconnect
                    _snap_req.clear()
                    try:
                        _snap_hr_data = _hr_read_on_socket(s, _inverter_slave, 20, 97, timeout=7.5)
                        _snap_done.set()
                    except Exception as exc:
                        log.warning("Listen-socket snapshot failed: %s -- trying poll fallback", exc)
                        # Gen1 firmware only exposes HR 0-43 on the listen socket.
                        # Close it, read via a fresh connection, then let the loop reconnect.
                        _snap_hr_data = None
                        try:
                            s.close()
                        except Exception:
                            pass
                        try:
                            time.sleep(0.5)
                            # Gen1 only responds to standard Modbus reads at base=0 and
                            # base=60 (page-aligned).  Read both pages -- same calls the
                            # control page makes -- then slice out HR 20-116.
                            _r0  = _hr_read(_inverter_slave,  0, 60, timeout=5.0)
                            _r60 = _hr_read(_inverter_slave, 60, 60, timeout=5.0)
                            _snap_hr_data = (_r0 + _r60)[20:117]
                            log.debug("Snapshot poll fallback: read %d regs", len(_snap_hr_data))
                        except Exception as exc2:
                            log.warning("Snapshot poll fallback failed: %s", exc2)
                        _snap_done.set()
                        _snap_poll_reconnect = True
                        raise ConnectionError("reconnect after snapshot poll")
                # Offline watchdog: no decodable frame for 30s.
                # We poke every 10s so frames should arrive every ~10s.
                # 30s = 3x poke interval -- enough margin, fast enough recovery.
                if now - last_frame > 30:
                    raise ConnectionError("no inverter data for 30s")
        except Exception as exc:
            msg = str(exc)
            with _lock:
                _error = msg
                if _cached:
                    _cached["ok"] = False
            if _snap_poll_reconnect:
                # Intentional disconnect for snapshot poll read -- reconnect quickly,
                # no fault event (the listen loop will be back within a couple of seconds).
                _snap_poll_reconnect = False
                time.sleep(1)
            elif not st.get("offline"):
                log.warning("Lost inverter data stream: %s", msg)
                _log_event("fault", f"Lost connection to inverter: {msg}")
                st["offline"] = True
                time.sleep(5)
            else:
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

    t0 = time.time()
    log.debug("HR read: slave=0x%02x base=%d count=%d", slave, base, count)
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
                log.debug("HR read: OK base=%d count=%d (%.0fms)",
                          base, count, (time.time() - t0) * 1000)
                return [(f[42 + i*2] << 8) | f[43 + i*2] for i in range(count)]
    finally:
        s.close()
    raise TimeoutError(f"HR read timeout: slave=0x{slave:02x} base={base} count={count}")


_HR_ON_SOCKET_CHUNK = 24   # Gen1 firmware rejects HR reads larger than ~22 registers on the
                           # listen socket (the in-socket detect reads exactly 22 and works;
                           # 60 times out).  24 is just above the proven-safe value, giving
                           # a useful error message if the limit turns out to be exactly 22.

def _hr_read_on_socket(s, slave: int, base: int, count: int, timeout: float = 5.0) -> list:
    """Like _hr_read but uses an already-open socket rather than creating a new
    connection.  Called by the listen loop to service snapshot requests from the
    QA thread -- Gen2 dongles are single-client and ignore reads on a 2nd conn.
    Automatically splits large reads into _HR_ON_SOCKET_CHUNK-register chunks so
    Gen1 firmware limits do not cause silent timeouts."""
    if count > _HR_ON_SOCKET_CHUNK:
        n_chunks = (count + _HR_ON_SOCKET_CHUNK - 1) // _HR_ON_SOCKET_CHUNK
        chunk_timeout = timeout / n_chunks
        result = []
        off = 0
        while off < count:
            n = min(_HR_ON_SOCKET_CHUNK, count - off)
            result.extend(_hr_read_on_socket(s, slave, base + off, n, chunk_timeout))
            off += n
        return result

    serial  = b"AB1234G567"
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([slave, 0x03]) + base.to_bytes(2, "big") + count.to_bytes(2, "big")
    crc     = _crc16(inner)
    payload = serial + padding + inner + crc
    length  = len(payload) + 2
    frame   = b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload
    orig_timeout = s.gettimeout()
    s.settimeout(1.0)
    try:
        s.sendall(frame)
        buf = bytearray()
        t0  = time.time()
        while time.time() - t0 < timeout:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf.extend(chunk)
            for f in _pop_data_frames(buf):
                if len(f) < 44 or f[7] != 0x02 or f[27] != 0x03:
                    continue
                rx_base  = (f[38] << 8) | f[39]
                rx_count = (f[40] << 8) | f[41]
                if rx_base != base or rx_count != count:
                    continue
                if len(f) < 42 + count * 2:
                    continue
                return [(f[42 + i*2] << 8) | f[43 + i*2] for i in range(count)]
    finally:
        s.settimeout(orig_timeout)
    raise TimeoutError(f"HR read timeout (on-socket): slave=0x{slave:02x} base={base} count={count}")


_DONGLE_BUSY_CODE  = 0x43  # GivEnergy Modbus exception: dongle handling another request
_WRITE_MAX_ATTEMPTS = 7    # 1 initial attempt + 6 retries (matches psylsph behaviour)


def _hr_write(slave: int, reg: int, value: int, timeout: float = 5.0,
              attempts: int = _WRITE_MAX_ATTEMPTS,
              timeout_retries: int = 1) -> None:
    """Write a single holding register.  Verifies the echo response.
    Retries on exception code 67 (dongle busy) up to `attempts` times (default 7,
    as the manual controls use). The scheduler passes attempts=1 to FAIL FAST: the
    sustained busy-retry hammering is what disrupted the Gen2 listen stream, so it
    aborts on busy and lets the 15s re-queue try again instead.
    Retries on timeout up to `timeout_retries` times (default 1, 1.5 s wait) --
    the first write in a sequence can catch the inverter in broadcast/listen mode.
    Stale echo frames (a previous write's confirmation re-emitted by the dongle)
    are ignored; only the echo matching this reg/val confirms success.
    Raises on timeout (no matching echo), dongle-busy exhaustion, or exhausted
    retries."""
    serial  = b"AB1234G567"
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([slave, 0x06]) + reg.to_bytes(2, "big") + value.to_bytes(2, "big")
    crc     = _crc16(inner)
    payload = serial + padding + inner + crc
    length  = len(payload) + 2
    frame   = b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload

    for t_try in range(timeout_retries + 1):
        if t_try:
            log.warning("HR write: timeout, retrying after 1.5 s "
                        "(slave=0x%02x reg=%d val=%d) ...", slave, reg, value)
            time.sleep(1.5)
        timed_out = False
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
                        if echo_reg == reg and echo_val == value:
                            log.debug("HR write: reg=%d val=%d OK", reg, value)
                            return  # success
                        # Stale echo: some dongles (notably the AIO) keep
                        # re-emitting confirmation frames for earlier writes in a
                        # batch.  Opening a fresh socket for the next write can
                        # therefore read a leftover echo from a previous register.
                        # Skip it and keep reading for OUR echo within the deadline
                        # rather than failing the (already-successful) write.
                        log.debug("HR write: ignoring stale echo reg=%d val=%d "
                                  "(awaiting reg=%d val=%d)", echo_reg, echo_val, reg, value)
                        continue
                    if busy:
                        break
            finally:
                s.close()
            if not busy:
                timed_out = True
                break  # exit busy-retry loop; let t_try handle the timeout
        else:
            raise OSError(f"HR write: dongle still busy after {attempts} attempt(s) "
                          f"(reg={reg} val={value})")
        if timed_out:
            continue  # retry after brief pause
    raise TimeoutError(f"HR write timeout: slave=0x{slave:02x} reg={reg} val={value}")


# ── Generation / profile detection ────────────────────────────────────────────
#
# Profile drives which register map and slot count to use.  Detection reads
# HR[0] (device_type_code) and HR[21] (arm_firmware_version) from the inverter
# and classifies using the same logic as the givenergy-modbus library.
#
# Profiles:
#   single_phase_2slot     – Hybrid Gen 1/Gen 2, Gen 3 with ARM fw ≤302
#   single_phase_extended  – Gen 3 (ARM fw >302), HV Gen 3 (0x81xx), All in One (0x8xxx), 0x83xx
#   three_phase_aio        – true three-phase inverters only (0x4xxx / 0x6xxx)
#   unknown                – detection failed, unrecognised DTC, or no-battery device (String Inverter)

_inverter_profile: str = ""     # "" = not yet detected
_inverter_model:   str = ""     # human-readable model name
_detect_lock = threading.Lock()

# DTC first-byte → profile (before firmware disambiguation)
_DTC_PREFIX_PROFILE = {
    "2": "single_phase_2slot",       # HYBRID family — fw disambiguates Gen2/Gen3
    "3": "single_phase_ac_coupled",  # AC single-phase (GIV-AC3.0 etc.) — 1 charge slot only
    "4": "three_phase_aio",          # three-phase hybrid
    "5": "single_phase_2slot",       # EMS
    "6": "three_phase_aio",          # three-phase AC
    "7": "single_phase_2slot",       # GATEWAY
    "8": "single_phase_extended",    # ALL_IN_ONE family (0x8xxx): single-phase, 10-slot
                                     # extended. Confirmed on AIO 0x8001 (Gen1), 11 Jun 2026.
}

# Specific two-digit DTC prefixes that override the coarse map
_DTC_TWO_PREFIX_PROFILE = {
    "21": "single_phase_2slot",    # Polar
    "23": "unknown",               # String Inverter Gen 3 — no battery, cannot use battery controls
    "41": "three_phase_aio",       # AIO Commercial
    "51": "single_phase_2slot",    # EMS Commercial
    "70": "gateway_aio",           # Gateway / Giv-Gateway (DTC 0x70xx) — live data at IR base=1600
    "81": "single_phase_extended", # Hybrid Inverter Gen 3 HV (single-phase, 8/10 kW)
    "82": "single_phase_extended", # All in One 2 (AIO2 + MPPT): single-phase 10-slot, as 0x80xx
    "83": "single_phase_extended", # DTC 0x83xx -- identity unconfirmed; no GivEnergy "Gen 4" product exists
}

# ARM firmware century → generation for DTC prefix "20" (HYBRID family)
# Century 3xx → Gen3, 8xx/9xx → Gen2, else → Gen1 (2-slot)
_FW_CENTURY_GEN = {3: "gen3", 8: "gen2", 9: "gen2"}


_DTC_TWO_MODEL_NAME = {
    "21": "Polar",
    "23": "String Inverter Gen 3",
    "41": "AIO Commercial",
    "51": "EMS Commercial",
    "70": "Gateway",
    "81": "Hybrid Inverter Gen 3 HV",
    "82": "All in One 2",
    "83": "Unknown (0x83)",
}
_DTC_ONE_MODEL_NAME = {
    "3": "AC Coupled Inverter",
    "4": "Three-Phase Hybrid Inverter",
    "5": "Energy Management System",
    "6": "Three-Phase AC Inverter",
    "7": "Gateway",
    "8": "All in One",
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
            model   = "Hybrid Inverter Gen 3"
        elif gen == "gen2":
            profile = "single_phase_2slot"
            model   = "Hybrid Inverter Gen 2"
        else:
            profile = "single_phase_2slot"
            model   = "Hybrid Inverter Gen 1"
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
    Failed detections ("unknown") are NOT cached — every call retries until
    a real model is identified, so the refresh button is always useful.
    """
    global _inverter_profile, _inverter_model

    with _detect_lock:
        if _inverter_profile and _inverter_profile != "unknown":
            # Successfully detected — return cached values.
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

        # Only cache successful detections — failed ones stay empty so the
        # next call (or refresh button press) retries automatically.
        if profile != "unknown":
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
    "ENABLE_CHARGE_TARGET":  20,    # must be 1 for CHARGE_TARGET_SOC to take effect
    "CHARGE_TARGET_SOC":    116,   # global target; Gen3 also has per-slot at HR 242+
    "BATTERY_SOC_RESERVE":  110,
    "BATTERY_CHARGE_LIMIT": 111,
    "BATTERY_DISCHARGE_LIMIT": 112,
    "BATTERY_POWER_RESERVE": 114,
}

# Slot time-pair registers: (start_hr, end_hr).
# Slot 1 (94/95) is shared across all profiles. Charge slot 2 differs: the extended
# profile uses the contiguous block at 243/244 (confirmed on AIO 0x8001 hardware,
# 11 Jun 2026); 2-slot/gateway use 31/32 (see _CHARGE_SLOT_HR_2SLOT). Slots 3-10 are
# the extended-only contiguous block, matching the givenergy-modbus EXTENDED_SLOTS map.
_CHARGE_SLOT_HR = [          # single_phase_extended layout (Gen3/HV-Gen3/AIO 0x8xxx/0x83xx)
    (94, 95),    # slot 1
    (243, 244),  # slot 2  (contiguous extended block; confirmed on AIO 0x8001, 11 Jun 2026).
                 #          2-slot and gateway profiles use HR 31/32 instead, via the helper
                 #          below (they don't read the 240-299 block).
    (246, 247),  # slot 3  } extended (Gen3/HV-Gen3/AIO/0x83xx) only
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

# Charge slot start/end for 2-slot and gateway profiles: charge slot 2 stays at the
# legacy HR 31/32 (these profiles only read HR 0-119, not the 240-299 extended block).
# NOTE: GIV-AC3.0 (Gen2 AC) cannot write HR 31/32 (firmware silently times out the write,
# confirmed live 07 Jun 2026); it uses single_phase_ac_coupled with only 1 charge slot.
_CHARGE_SLOT_HR_2SLOT = [
    (94, 95),    # slot 1
    (31, 32),    # slot 2  (legacy address)
]

def _charge_slot_hrs(profile: str):
    """Charge slot start/end HR pairs for this profile.
    Only single_phase_extended (Gen3/HV/AIO 0x8xxx) keeps charge slot 2 in the
    contiguous block at HR 243/244 and reads the 240-299 range. Every other profile
    uses the legacy HR 31/32 map (and never reads beyond HR 119)."""
    return _CHARGE_SLOT_HR if profile == "single_phase_extended" else _CHARGE_SLOT_HR_2SLOT

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

# Charge and discharge slot counts per profile (may differ — e.g. AC-coupled
# inverters have only 1 usable charge slot but 2 discharge slots).
# Confirmed on GIV-AC3.0 D0.212, 07 Jun 2026: HR 31/32 (charge slot 2) is
# not writable; HR 56/57 and HR 44/45 (discharge slots 1 & 2) both work.
_CHARGE_SLOT_COUNT = {
    "single_phase_ac_coupled": 1,    # GIV-AC3.0: HR 94/95 only; HR 31/32 not writable
    "single_phase_2slot":      2,
    "single_phase_extended":  10,
    "three_phase_aio":         2,
    "gateway_aio":             2,
}
_DISCHARGE_SLOT_COUNT = {
    "single_phase_ac_coupled": 2,    # GIV-AC3.0: HR 56/57 and HR 44/45 both work
    "single_phase_2slot":      2,
    "single_phase_extended":  10,
    "three_phase_aio":         2,
    "gateway_aio":             2,
}

# Profiles that support app-held scheduling (three_phase_aio is read-only for
# writes, so it is excluded; unknown means detection hasn't run yet).
_SCHED_PROFILES = {"single_phase_ac_coupled", "single_phase_2slot",
                   "single_phase_extended", "gateway_aio"}


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
    # Retry up to 2 times with 2s backoff -- the dongle is single-client and
    # a competing TCP connection from _hr_read can race with the listen loop,
    # causing a timeout on the first attempt.  Waiting 2s gives the listen
    # loop time to re-establish before we try again.
    _HR_CTRL_RETRIES = 2
    _HR_CTRL_BACKOFF = 2.0
    for _attempt in range(_HR_CTRL_RETRIES + 1):
        try:
            regs_0  = _hr_read(slave, 0,  60)   # HR  0-59
            regs_60 = _hr_read(slave, 60, 60)   # HR 60-119
            break
        except TimeoutError:
            if _attempt < _HR_CTRL_RETRIES:
                log.warning("Control: HR read attempt %d/%d timed out -- retrying in %.0fs",
                            _attempt + 1, _HR_CTRL_RETRIES + 1, _HR_CTRL_BACKOFF)
                time.sleep(_HR_CTRL_BACKOFF)
            else:
                raise

    def hr(n):
        if n < 60:   return regs_0[n]
        if n < 120:  return regs_60[n - 60]
        raise IndexError(f"HR {n} not in base read range")

    if profile == "single_phase_extended":
        regs_240 = _hr_read(slave, 240, 60)   # HR 240-299
        regs_313 = _hr_read(slave, 313, 2)    # HR 313-314: AIO charge/discharge power limits (0-100%)
        def hr_ext(n):
            if 240 <= n < 300: return regs_240[n - 240]
            return hr(n)
    else:
        regs_313 = None
        def hr_ext(n):
            return hr(n)

    _aio_slots_ok = True
    if profile == "three_phase_aio":
        # Three-phase slot registers live in the 1100 range.
        # Some AIO models/firmware revisions do not respond to this range.
        # Retry up to 3 times (the inverter may be momentarily busy) before
        # giving up and falling back to empty slots, so the control page still
        # loads (read-only) rather than returning a hard error to the frontend.
        _AIO_READ_ATTEMPTS = 3
        regs_1100 = None
        for _attempt in range(_AIO_READ_ATTEMPTS):
            try:
                regs_1100 = _hr_read(slave, 1100, 22, timeout=3.0)   # HR 1100-1121
                break
            except Exception as exc:
                if _attempt < _AIO_READ_ATTEMPTS - 1:
                    log.warning("AIO: HR 1100-1121 read attempt %d/%d failed (%s) — retrying",
                                _attempt + 1, _AIO_READ_ATTEMPTS, exc)
                    time.sleep(0.5)
                else:
                    log.warning("AIO: HR 1100-1121 read failed after %d attempts (%s) "
                                "— slot registers unavailable on this model/firmware",
                                _AIO_READ_ATTEMPTS, exc)
                    _aio_slots_ok = False
        if regs_1100 is not None:
            def hr_3ph(n):
                if 1100 <= n < 1122: return regs_1100[n - 1100]
                return hr(n)
        else:
            def hr_3ph(n): return 0   # return 0 for all 3-phase slot registers

    # ── Build slot arrays ─────────────────────────────────────────────────────
    num_charge_slots    = _CHARGE_SLOT_COUNT[profile]
    num_discharge_slots = _DISCHARGE_SLOT_COUNT[profile]

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
        read_slot(_charge_slot_hrs(profile), _CHARGE_SOC_HR, i)
        for i in range(num_charge_slots)
    ]
    discharge_slots = [
        read_slot(_DISCHARGE_SLOT_HR, _DISCHARGE_SOC_HR, i)
        for i in range(num_discharge_slots)
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
        "charge_limit":    regs_313[0] if regs_313 else hr(_HR["BATTERY_CHARGE_LIMIT"]) * 2,
        "discharge_limit": regs_313[1] if regs_313 else hr(_HR["BATTERY_DISCHARGE_LIMIT"]) * 2,
        "power_reserve":        hr(_HR["BATTERY_POWER_RESERVE"]),
        "charge_slots":         charge_slots,
        "discharge_slots":      discharge_slots,
        # Power-display config (for the UI to convert % limits to watts)
        "power_units":     POWER_UNITS,
        "max_charge_w":    MAX_CHARGE_W,
        "max_discharge_w": MAX_DISCHARGE_W,
    }
    if not _aio_slots_ok:
        result["slot_read_warning"] = (
            "Slot registers (HR 1100+) did not respond on this inverter — "
            "charge and discharge slots cannot be read. This may indicate the "
            "registers are not supported on this firmware version."
        )
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

    # sync_time works on any profile — handle before detection
    if command == "sync_time":
        try:
            slave = _inverter_slave or 0x11
            now = datetime.now()
            # HR 35–40 = year (2-digit offset from 2000), month, day, hour, minute, second.
            # Confirmed: givenergy-local-modbus.json 'system_time' RW, reg 35-40;
            # givenergy-modbus library decodes as year+2000, so we write year-2000.
            _hr_write(slave, 35, now.year - 2000)
            _hr_write(slave, 36, now.month)
            _hr_write(slave, 37, now.day)
            _hr_write(slave, 38, now.hour)
            _hr_write(slave, 39, now.minute)
            _hr_write(slave, 40, now.second)
            synced = now.strftime("%H:%M:%S %d/%m/%Y")
            msg = f"Inverter clock set to {synced}"
            _log_control(command, params, True, msg)
            log.info("Control: %s", msg)
            return {"ok": True, "message": msg, "synced_to": synced}
        except Exception as exc:
            err = str(exc)
            _log_control(command, params, False, f"Control failed {command}: {err}")
            log.error("Control failed sync_time: %s", err)
            return {"ok": False, "message": err}

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
        log.info("Control: %s", msg)
        return {"ok": True, "message": msg}
    except Exception as exc:
        err = str(exc)
        _log_control(command, params, False, f"Control failed {command}: {err}")
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
        max_slots = _CHARGE_SLOT_COUNT[profile]
        if not 1 <= slot <= max_slots:
            raise ValueError(f"Charge slot {slot} out of range for this inverter (max {max_slots})")
        start_hr, end_hr = _charge_slot_hrs(profile)[slot - 1]
        wr(start_hr, _hhmm_to_bcd(params["start"]))
        wr(end_hr,   _hhmm_to_bcd(params["end"]))
        return f"Charge slot {slot} set to {params['start']}–{params['end']}"

    # ── Discharge slot ────────────────────────────────────────────────────────
    if command == "set_discharge_slot":
        slot = int(params.get("slot", 1))
        max_slots = _DISCHARGE_SLOT_COUNT[profile]
        if not 1 <= slot <= max_slots:
            raise ValueError(f"Discharge slot {slot} out of range for this inverter (max {max_slots})")
        start_hr, end_hr = _DISCHARGE_SLOT_HR[slot - 1]
        wr(start_hr, _hhmm_to_bcd(params["start"]))
        wr(end_hr,   _hhmm_to_bcd(params["end"]))
        return f"Discharge slot {slot} set to {params['start']}–{params['end']}"

    # ── Per-slot SOC target (extended profile only) ───────────────────────────
    if command == "set_charge_slot_soc":
        if profile != "single_phase_extended":
            raise ValueError("Per-slot charge SOC target is only available on Gen3 / HV Gen3 inverters")
        slot = int(params.get("slot", 1))
        if not 1 <= slot <= 10:
            raise ValueError(f"Slot {slot} out of range (max 10)")
        val = max(4, min(100, int(params["value"])))
        wr(_CHARGE_SOC_HR[slot - 1], val)
        return f"Charge slot {slot} target SOC set to {val}%"

    if command == "set_discharge_slot_soc":
        if profile != "single_phase_extended":
            raise ValueError("Per-slot discharge SOC target is only available on Gen3 / HV Gen3 inverters")
        slot = int(params.get("slot", 1))
        if not 1 <= slot <= 10:
            raise ValueError(f"Slot {slot} out of range (max 10)")
        val = max(4, min(100, int(params["value"])))
        wr(_DISCHARGE_SOC_HR[slot - 1], val)
        return f"Discharge slot {slot} target SOC set to {val}%"

    # ── Scalar settings ───────────────────────────────────────────────────────
    if command == "set_charge_target_soc":
        val = max(4, min(100, int(params["value"])))
        wr(_HR["ENABLE_CHARGE_TARGET"], 0 if val == 100 else 1)
        wr(_HR["CHARGE_TARGET_SOC"], val)
        return f"Charge target SOC set to {val}%"

    if command == "set_soc_reserve":
        val = max(4, min(100, int(params["value"])))
        wr(_HR["BATTERY_SOC_RESERVE"], val)
        return f"SOC reserve set to {val}%"

    if command == "set_charge_limit":
        val = max(0, min(100, int(params["value"])))
        if profile == "single_phase_extended":
            wr(313, val)
        else:
            wr(_HR["BATTERY_CHARGE_LIMIT"], val // 2)
        return f"Charge power limit set to {val}%"

    if command == "set_discharge_limit":
        val = max(0, min(100, int(params["value"])))
        if profile == "single_phase_extended":
            wr(314, val)
        else:
            wr(_HR["BATTERY_DISCHARGE_LIMIT"], val // 2)
        return f"Discharge power limit set to {val}%"

    if command == "set_discharge_mode":
        val = max(0, min(1, int(params["value"])))
        wr(_HR["BATTERY_POWER_MODE"], val)
        label = "Max Power / Export" if val == 0 else "Demand only"
        return f"Discharge mode set to {label}"

    raise ValueError(f"Unknown command: {command}")


# ── Scheduler engine (app-held 48 half-hour block reconciler) ─────────────────
# The scheduler issues the same register writes as the manual control buttons —
# no inverter slot registers are touched. Rules define charge/export/hold windows;
# unscheduled blocks restore the baseline mode (eco or storage) including the
# configured baseline SOC reserve. The thread evaluates the block every 15s and
# applies changes via _hr_write to the detected slave, with the listen loop left
# open (the combination confirmed to work on Gen2). DELTA writes (only changed
# registers) keep a typical apply to 1–4 writes. Failures retry on the next 15s cycle.

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

def _mins_to_hhmm(mins: int) -> int:
    """Convert minutes-from-midnight to the HHMM integer format used by slot registers.
    Clamps 0 (== 00:00 == 'disabled' on the inverter) to 1 (00:01) so the slot stays
    active.  Values 1–1439 pass through unchanged after mod-1440 normalisation."""
    h, m = divmod(int(mins) % 1440, 60)
    val = h * 100 + m
    return val if val != 0 else 1    # 00:00 means "disabled" — use 00:01 instead


def _sched_desired_state(rules: list, weekday: int, t_min: int) -> dict:
    """Pure: pick the winning action for this moment, else baseline.
    `rules` are /api/schedules-shaped dicts; weekday Mon=0 … Sun=6 (matches days_mask).
    Slot start/end (minutes from midnight) are passed through so _sched_compute_writes
    can write the required charge/discharge slot registers."""
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
        tgt = winner["target_soc"]
        pred_used = False
        if winner.get("predictive"):
            pred_tgt = _compute_predictive_target()
            if pred_tgt is not None:
                tgt       = pred_tgt
                pred_used = True
        return {"mode": "charge",
                "target_soc": tgt,
                "power_pct":  winner.get("power_pct", 50),
                "slot_start": winner["start"],
                "slot_end":   winner["end"],
                "predictive": pred_used}
    if winner["action"] == "export":
        return {"mode": "export",
                "stop_soc":  winner.get("target_soc") or 4,
                "power_pct": winner.get("power_pct", 50),
                "slot_start": winner["start"],   # minutes from midnight
                "slot_end":   winner["end"]}
    return {"mode": "hold"}

def _sched_load_rules() -> list:
    with _db() as conn:
        rows = conn.execute(
            "SELECT action, start_hhmm, end_hhmm, days_mask, target_soc, power_pct, predictive "
            "FROM schedules WHERE enabled=1").fetchall()
    return [{"enabled": True, "action": r["action"], "start": r["start_hhmm"],
             "end": r["end_hhmm"], "days_mask": r["days_mask"],
             "target_soc": r["target_soc"], "power_pct": r["power_pct"],
             "predictive": bool(r["predictive"])} for r in rows]

def _sched_compute_writes(desired: dict):
    """Translate a desired-state dict into (slave, [(reg,val),...], summary).
    Writes charge/discharge slot registers so the firmware will actually allow
    forced grid charge/export — the scheduler therefore requires EXCLUSIVE
    inverter control (not compatible with cloud integrations like Octopus
    Intelligent Flux or Predbat, which own the slot registers).
    The register writes are absolute sets, so re-applying the full list is idempotent
    (a partial apply self-heals on the next retry). Raises on unsupported hardware."""
    slave, profile, model = _detect_inverter()
    if profile not in _SCHED_PROFILES:
        raise RuntimeError(f"Scheduler is not supported on this inverter ({model})")

    mode = desired["mode"]
    pct  = max(0, min(50, int(desired.get("power_pct", 50))))
    # Power-limit register/value differ by profile:
    # Gen 1/2: HR 111/112 take 0-50 (half-percent); pct is already in this range from the DB.
    # AIO (single_phase_extended): HR 313/314 take 0-100 (full-percent); multiply by 2.
    if profile == "single_phase_extended":
        _chrg_limit_reg, _chrg_limit_val = 313, pct * 2
        _dchg_limit_reg, _dchg_limit_val = 314, pct * 2
    else:
        _chrg_limit_reg, _chrg_limit_val = _HR["BATTERY_CHARGE_LIMIT"],    pct
        _dchg_limit_reg, _dchg_limit_val = _HR["BATTERY_DISCHARGE_LIMIT"], pct

    if mode == "charge":
        target = max(4, min(100, int(desired.get("target_soc", 100))))
        # Slot writes FIRST so the inverter sees a valid active slot before
        # ENABLE_CHARGE=1 — the inverter rejects the enable if no slot exists.
        # (Live-tested on GIV-AC3.0 D0.212 — test C2 proved this, 07 Jun 2026.)
        # slot_start / slot_end are "HH:MM" strings from the rule row;
        # convert via minutes so _mins_to_hhmm can apply the 00:00→00:01 clamp.
        cs = _mins_to_hhmm(_hhmm_to_min(desired.get("slot_start", "00:01")))
        ce = _mins_to_hhmm(_hhmm_to_min(desired.get("slot_end",   "00:01")))
        w  = [(_CHARGE_SLOT_HR[0][0], cs),   # HR 94  charge slot 1 start
              (_CHARGE_SLOT_HR[0][1], ce),   # HR 95  charge slot 1 end
              (_HR["ENABLE_DISCHARGE"],                          0),
              (_HR["BATTERY_POWER_MODE"],                        1),   # demand / eco
              (_HR["ENABLE_CHARGE_TARGET"], 0 if target == 100 else 1),
              (_HR["CHARGE_TARGET_SOC"],                    target),
              (_chrg_limit_reg,                       _chrg_limit_val),
              (_HR["ENABLE_CHARGE"],                             1)]
        if desired.get("predictive"):
            kwh = _get_tomorrow_forecast_kwh()
            kwh_str = f"{kwh:.1f} kWh" if kwh is not None else "forecast"
            demand = _compute_morning_demand_kwh()
            demand_src = f"{demand:.1f} kWh historical demand" if demand is not None else f"{PRED_EVENING_RESERVE:.1f} kWh fallback reserve"
            summary = f"Predictive charge: {kwh_str} tomorrow -- {demand_src} → target {target}% at {pct}% power"
        else:
            summary = f"Charge to {target}% at {pct}% power"

    elif mode == "export":
        stop = max(4, min(100, int(desired.get("stop_soc", 4))))
        # Discharge slot FIRST — firmware requires an active discharge slot for
        # forced grid export (eco OFF + HR59=1 without a slot = idle, not export).
        dstart = _mins_to_hhmm(_hhmm_to_min(desired.get("slot_start", "00:01")))
        dend   = _mins_to_hhmm(_hhmm_to_min(desired.get("slot_end",   "00:01")))
        w  = [(_DISCHARGE_SLOT_HR[0][0], dstart),   # HR 56  discharge slot 1 start
              (_DISCHARGE_SLOT_HR[0][1], dend),      # HR 57  discharge slot 1 end
              (_HR["ENABLE_CHARGE"],               0),
              (_HR["BATTERY_POWER_MODE"],           0),   # 0 = export / max-power
              (_HR["BATTERY_SOC_RESERVE"],       stop),
              (_dchg_limit_reg,           _dchg_limit_val),
              (_HR["ENABLE_DISCHARGE"],             1)]
        summary = f"Export at {pct}% power, stop at {stop}% SOC"

    elif mode == "hold":
        w = [(_HR["ENABLE_CHARGE"],        0),
             (_HR["ENABLE_CHARGE_TARGET"], 0),
             (_HR["ENABLE_DISCHARGE"],     0),
             (_CHARGE_SLOT_HR[0][0],    0),   # HR 94  charge slot 1 start → disabled
             (_CHARGE_SLOT_HR[0][1],    0),   # HR 95  charge slot 1 end   → disabled
             (_DISCHARGE_SLOT_HR[0][0], 0),   # HR 56  discharge slot 1 start → disabled
             (_DISCHARGE_SLOT_HR[0][1], 0)]   # HR 57  discharge slot 1 end   → disabled
        summary = "Hold charge"

    else:   # baseline (also used for cleanup when master is switched off)
        reserve = max(4, min(100, SCHEDULER_BASELINE_SOC_RESERVE))
        slot_clears = [
            (_CHARGE_SLOT_HR[0][0],    0),   # HR 94  charge slot 1 start → disabled
            (_CHARGE_SLOT_HR[0][1],    0),   # HR 95  charge slot 1 end   → disabled
            (_DISCHARGE_SLOT_HR[0][0], 0),   # HR 56  discharge slot 1 start → disabled
            (_DISCHARGE_SLOT_HR[0][1], 0),   # HR 57  discharge slot 1 end   → disabled
        ]
        if SCHEDULER_BASELINE == "storage":
            w = slot_clears + [
                (_HR["ENABLE_CHARGE"],        0),
                (_HR["ENABLE_CHARGE_TARGET"], 0),
                (_HR["ENABLE_DISCHARGE"],     0),
                (_HR["BATTERY_SOC_RESERVE"], reserve)]
            summary = "Baseline: Storage (hold charge)"
        else:   # eco
            w = slot_clears + [
                (_HR["ENABLE_CHARGE"],        0),
                (_HR["ENABLE_CHARGE_TARGET"], 0),
                (_HR["BATTERY_POWER_MODE"],   1),   # demand / eco
                (_HR["BATTERY_SOC_RESERVE"], reserve),
                (_HR["ENABLE_DISCHARGE"],     1)]
            summary = "Baseline: Eco"

    return slave, w, summary


_SCHED_WRITE_GAP   = 0.5     # small gap between writes (manual control works back-to-back;
                            # a little spacing is just belt-and-braces).
_sched_applied_sig  = None        # signature of the last successfully-applied desired
_sched_applied_regs: dict = {}    # reg → last value we wrote, for delta writes
_sched_was_enabled  = False
_sched_last_status  = "idle"      # surfaced on /api/data: idle | applying | summary | error
_CLEANUP_DESIRED    = {"mode": "baseline"}
_CLEANUP_SIG        = json.dumps(_CLEANUP_DESIRED, sort_keys=True)

# Quick-action runtime state (epoch timestamps; 0 = inactive)
_quick_charge_until    = 0.0
_quick_discharge_until = 0.0
# Snapshot of register values captured just before a quick action starts.
# Used by _quick_action_revert() to restore the user's pre-action state.
_quick_action_snapshot: dict = {}   # {HR_number: value, ...}

# Inter-thread snapshot request: QA flask thread posts the request;
# _run_listen() services it on the existing socket (avoids opening a 2nd
# connection which Gen2 dongles -- single-client -- ignore).
_snap_req            = threading.Event()   # QA thread sets to request a read
_snap_done           = threading.Event()   # listen loop sets when complete
_snap_hr_data        = None               # list of raw HR values, written by listen loop
_snap_poll_reconnect = False              # True when listen socket was closed for a poll snapshot


def _sched_task_active() -> bool:
    """True when the scheduler has a live charge or export task applied."""
    if not SCHEDULER_ENABLED or _sched_applied_sig is None:
        return False
    try:
        return json.loads(_sched_applied_sig).get("mode") in ("charge", "export")
    except Exception:
        return False


# ── Quick-action disk persistence ────────────────────────────────────────────

def _qa_save_state(state: dict) -> None:
    try:
        _QA_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception as exc:
        log.warning("Quick action: failed to save state: %s", exc)

def _qa_clear_state() -> None:
    try:
        _QA_STATE_PATH.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("Quick action: failed to clear state file: %s", exc)

def _qa_load_state() -> dict:
    try:
        return json.loads(_QA_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("Quick action: failed to read state file: %s", exc)
        return None


# ── Free-slot selection (task #12) ────────────────────────────────────────────
# On extended profiles (10 slots) write to a free slot so the user's schedule
# is never touched. On 2-slot/gateway profiles fall back to slot 1 with
# snapshot-restore (the only slot available).

def _qa_find_free_discharge_slot(slave: int, profile: str, snap_raw) -> tuple:
    """Return (start_hr, end_hr, is_free) for the discharge quick action.
    is_free=True means we picked an unused slot; False means slot 1 with snapshot restore."""
    if profile != "single_phase_extended":
        return _DISCHARGE_SLOT_HR[0][0], _DISCHARGE_SLOT_HR[0][1], False

    # Discharge slot 2 (44/45) is in the snapshot range (HR 20-116).
    s2_hr, e2_hr = _DISCHARGE_SLOT_HR[1]
    snap_base = 20
    if snap_raw is not None:
        s2_free = (snap_raw[s2_hr - snap_base] == 0 and snap_raw[e2_hr - snap_base] == 0)
    else:
        try:
            r = _hr_read(slave, 2, s2_hr, timeout=3.0)
            s2_free = len(r) >= 2 and r[0] == 0 and r[1] == 0
        except Exception:
            s2_free = False
    if s2_free:
        return s2_hr, e2_hr, True

    # Check slots 3-10 (HR 276-298) in one read.
    try:
        ext = _hr_read(slave, 23, 276, timeout=3.0)
        for idx in range(2, len(_DISCHARGE_SLOT_HR)):
            s_hr, e_hr = _DISCHARGE_SLOT_HR[idx]
            if ext[s_hr - 276] == 0 and ext[e_hr - 276] == 0:
                return s_hr, e_hr, True
    except Exception as exc:
        log.warning("Quick action: discharge slot scan failed (%s) - using slot 1", exc)

    log.warning("Quick action: no free discharge slot on extended profile - using slot 1")
    return _DISCHARGE_SLOT_HR[0][0], _DISCHARGE_SLOT_HR[0][1], False


def _qa_find_free_charge_slot(slave: int, profile: str) -> tuple:
    """Return (start_hr, end_hr, is_free) for the charge quick action."""
    slot_hrs = _charge_slot_hrs(profile)
    if profile != "single_phase_extended":
        return slot_hrs[0][0], slot_hrs[0][1], False

    # Read extended charge slots 2-10 (HR 243-268) in one read.
    try:
        ext = _hr_read(slave, 26, 243, timeout=3.0)
        for idx in range(1, len(slot_hrs)):
            s_hr, e_hr = slot_hrs[idx]
            if ext[s_hr - 243] == 0 and ext[e_hr - 243] == 0:
                return s_hr, e_hr, True
    except Exception as exc:
        log.warning("Quick action: charge slot scan failed (%s) - using slot 1", exc)

    log.warning("Quick action: no free charge slot on extended profile - using slot 1")
    return slot_hrs[0][0], slot_hrs[0][1], False


def _quick_action_do(action: str):
    """Write the registers for a quick charge or discharge.
    Returns (slot_start_hhmm, slot_end_hhmm, end_epoch) on success; raises on error."""
    global _quick_action_snapshot
    from datetime import datetime
    slave, profile, model = _detect_inverter()
    if profile not in _SCHED_PROFILES:
        raise RuntimeError(f"Quick actions are not supported on this inverter ({model})")

    # Snapshot current register state so we can restore it exactly after the action.
    # Abort if the read fails -- proceeding with fallback values would silently restore
    # wrong defaults (e.g. ENABLE_CHARGE=0, BATTERY_SOC_RESERVE=4) instead of the
    # user's actual pre-action settings.
    #
    # Gen2 dongles are single-client: opening a second TCP connection to read the
    # snapshot causes timeouts. In listen mode we post the request to the listen
    # loop thread which services it on the existing socket (_snap_req / _snap_done).
    snap_raw = None
    if _active_mode == "listen":
        _snap_done.clear()
        _snap_hr_data_local = None
        _snap_req.set()
        if _snap_done.wait(timeout=25.0):
            snap_raw = _snap_hr_data   # written by listen loop before setting _snap_done
        if snap_raw is None:
            log.warning("Quick action: listen-socket snapshot failed - aborting")
    else:
        _SNAP_ATTEMPTS = 3
        for _attempt in range(_SNAP_ATTEMPTS):
            try:
                snap_raw = _hr_read(slave, 20, 97, timeout=3.0)   # HR 20-116
                break
            except Exception as exc:
                if _attempt < _SNAP_ATTEMPTS - 1:
                    log.warning("Quick action: snapshot attempt %d/%d failed (%s) - retrying",
                                _attempt + 1, _SNAP_ATTEMPTS, exc)
                    time.sleep(0.5)
                else:
                    log.warning("Quick action: snapshot failed after %d attempts (%s) - aborting",
                                _SNAP_ATTEMPTS, exc)

    if snap_raw is None:
        raise RuntimeError("Could not read inverter state -- please try again in a moment")

    def _snap(n, fallback=0):   # fallback param kept for call-site compat; snap_raw guaranteed non-None
        return snap_raw[n - 20]

    # AIO power-limit registers (HR 313-314) are outside the HR 20-116 snap range.
    aio_limits = None
    if profile == "single_phase_extended":
        try:
            aio_limits = _hr_read(slave, 313, 2)
        except Exception as exc:
            log.warning("Quick action: HR 313-314 read failed (%s) -- will restore to 100%%", exc)
            aio_limits = [100, 100]

    _quick_action_snapshot = {
        _HR["ENABLE_CHARGE_TARGET"]:    _snap(_HR["ENABLE_CHARGE_TARGET"]),
        _HR["BATTERY_POWER_MODE"]:      _snap(_HR["BATTERY_POWER_MODE"]),
        _DISCHARGE_SLOT_HR[0][0]:       _snap(_DISCHARGE_SLOT_HR[0][0]),
        _DISCHARGE_SLOT_HR[0][1]:       _snap(_DISCHARGE_SLOT_HR[0][1]),
        _HR["ENABLE_DISCHARGE"]:        _snap(_HR["ENABLE_DISCHARGE"]),
        _CHARGE_SLOT_HR[0][0]:          _snap(_CHARGE_SLOT_HR[0][0]),
        _CHARGE_SLOT_HR[0][1]:          _snap(_CHARGE_SLOT_HR[0][1]),
        _HR["ENABLE_CHARGE"]:           _snap(_HR["ENABLE_CHARGE"]),
        _HR["BATTERY_SOC_RESERVE"]:     _snap(_HR["BATTERY_SOC_RESERVE"]),
        _HR["BATTERY_CHARGE_LIMIT"]:    _snap(_HR["BATTERY_CHARGE_LIMIT"]),
        _HR["BATTERY_DISCHARGE_LIMIT"]: _snap(_HR["BATTERY_DISCHARGE_LIMIT"]),
        _HR["CHARGE_TARGET_SOC"]:       _snap(_HR["CHARGE_TARGET_SOC"]),
    }
    log.info("Quick action: snapshot saved (%d registers)", len(_quick_action_snapshot))

    now_dt     = datetime.now()
    start_mins = now_dt.hour * 60 + now_dt.minute
    end_mins   = min(start_mins + 60, 23 * 60 + 59)
    cs         = _mins_to_hhmm(start_mins)
    ce         = _mins_to_hhmm(end_mins)

    if action == "charge":
        s_hr, e_hr, free_slot = _qa_find_free_charge_slot(slave, profile)
        if profile == "single_phase_extended":
            chrg_limit_reg     = 313
            pct                = max(0, min(100, QUICK_CHARGE_POWER_PCT))
            chrg_limit_restore = aio_limits[0] if aio_limits else 100
        else:
            chrg_limit_reg     = _HR["BATTERY_CHARGE_LIMIT"]   # HR 111, 0-50
            pct                = max(0, min(50, QUICK_CHARGE_POWER_PCT * 50 // 100))
            chrg_limit_restore = _snap(_HR["BATTERY_CHARGE_LIMIT"], 50)
        target = QUICK_CHARGE_TARGET_SOC
        writes = [
            (s_hr,                                                   cs),
            (e_hr,                                                   ce),
            (_HR["ENABLE_DISCHARGE"],                                 0),
            (_HR["BATTERY_POWER_MODE"],                               1),
            (_HR["ENABLE_CHARGE_TARGET"],   0 if target == 100 else 1),
            (_HR["CHARGE_TARGET_SOC"],                           target),
            (chrg_limit_reg,                                        pct),
            (_HR["ENABLE_CHARGE"],                                    1),
        ]
        restore_regs = [
            [s_hr,                              0 if free_slot else _snap(s_hr)],
            [e_hr,                              0 if free_slot else _snap(e_hr)],
            [_HR["ENABLE_DISCHARGE"],           _snap(_HR["ENABLE_DISCHARGE"],          1)],
            [_HR["BATTERY_POWER_MODE"],         _snap(_HR["BATTERY_POWER_MODE"],        1)],
            [_HR["ENABLE_CHARGE_TARGET"],       _snap(_HR["ENABLE_CHARGE_TARGET"],      0)],
            [_HR["CHARGE_TARGET_SOC"],          _snap(_HR["CHARGE_TARGET_SOC"],       100)],
            [chrg_limit_reg,                    chrg_limit_restore],
            [_HR["ENABLE_CHARGE"],              _snap(_HR["ENABLE_CHARGE"],             0)],
        ]
        display_pct = pct if profile == "single_phase_extended" else pct * 2
        label = f"Quick charge started: slot {cs:04d}-{ce:04d}, target {target}%, power {display_pct}%"
    else:
        s_hr, e_hr, free_slot = _qa_find_free_discharge_slot(slave, profile, snap_raw)
        if profile == "single_phase_extended":
            dchg_limit_reg     = 314
            pct                = max(0, min(100, QUICK_DISCHARGE_POWER_PCT))
            dchg_limit_restore = aio_limits[1] if aio_limits else 100
        else:
            dchg_limit_reg     = _HR["BATTERY_DISCHARGE_LIMIT"]   # HR 112, 0-50
            pct                = max(0, min(50, QUICK_DISCHARGE_POWER_PCT * 50 // 100))
            dchg_limit_restore = _snap(_HR["BATTERY_DISCHARGE_LIMIT"], 50)
        stop  = max(4, SCHEDULER_BASELINE_SOC_RESERVE)
        writes = [
            (s_hr,                                                   cs),
            (e_hr,                                                   ce),
            (_HR["ENABLE_CHARGE"],                                    0),
            (_HR["BATTERY_POWER_MODE"],                               0),
            (_HR["BATTERY_SOC_RESERVE"],                           stop),
            (dchg_limit_reg,                                        pct),
            (_HR["ENABLE_DISCHARGE"],                                 1),
        ]
        restore_regs = [
            [s_hr,                              0 if free_slot else _snap(s_hr)],
            [e_hr,                              0 if free_slot else _snap(e_hr)],
            [_HR["ENABLE_CHARGE"],              _snap(_HR["ENABLE_CHARGE"],              0)],
            [_HR["BATTERY_POWER_MODE"],         _snap(_HR["BATTERY_POWER_MODE"],         1)],
            [_HR["BATTERY_SOC_RESERVE"],        _snap(_HR["BATTERY_SOC_RESERVE"],        4)],
            [dchg_limit_reg,                    dchg_limit_restore],
            [_HR["ENABLE_DISCHARGE"],           _snap(_HR["ENABLE_DISCHARGE"],           1)],
        ]
        display_pct = pct if profile == "single_phase_extended" else pct * 2
        label = f"Quick discharge started: slot {cs:04d}-{ce:04d}, stop {stop}% SOC, power {display_pct}%"

    for reg, val in writes:
        _hr_write(slave, reg, val)
        time.sleep(_SCHED_WRITE_GAP)
    _log_control("quick_action", {"action": action, "slot_start": cs, "slot_end": ce}, True, label)
    log.warning(label)

    end_epoch = time.time() + 3600
    _qa_save_state({
        "action":       action,
        "slot_pair":    [s_hr, e_hr],
        "free_slot":    free_slot,
        "slot_start":   cs,
        "end_epoch":    end_epoch,
        "restore_regs": restore_regs,
    })
    return cs, ce, end_epoch


def _quick_action_revert(state: dict = None, trigger: str = "auto"):
    """Restore inverter to pre-action state. Best-effort: each write is individually
    retried via _hr_write, and a write failure is logged but does not abort the rest.

    state:   dict from _qa_load_state() with a 'restore_regs' list, or None.
    trigger: 'auto' (timer expiry), 'manual' (user pressed stop), 'startup' (boot recovery).
    """
    try:
        slave, profile, _model = _detect_inverter()
        if profile not in _SCHED_PROFILES:
            _qa_clear_state()
            return

        if state is not None and "restore_regs" in state:
            restore_list = [(int(r), v) for r, v in state["restore_regs"]]
            src = "state-file"
        elif _quick_action_snapshot:
            def _sv(reg, fallback=0):
                return _quick_action_snapshot.get(reg, fallback)
            restore_list = [
                (_CHARGE_SLOT_HR[0][0],        _sv(_CHARGE_SLOT_HR[0][0])),
                (_CHARGE_SLOT_HR[0][1],        _sv(_CHARGE_SLOT_HR[0][1])),
                (_DISCHARGE_SLOT_HR[0][0],     _sv(_DISCHARGE_SLOT_HR[0][0])),
                (_DISCHARGE_SLOT_HR[0][1],     _sv(_DISCHARGE_SLOT_HR[0][1])),
                (_HR["ENABLE_CHARGE"],         _sv(_HR["ENABLE_CHARGE"])),
                (_HR["ENABLE_CHARGE_TARGET"],  _sv(_HR["ENABLE_CHARGE_TARGET"])),
                (_HR["BATTERY_POWER_MODE"],    _sv(_HR["BATTERY_POWER_MODE"], 1)),
                (_HR["BATTERY_SOC_RESERVE"],   _sv(_HR["BATTERY_SOC_RESERVE"], 4)),
                (_HR["ENABLE_DISCHARGE"],      _sv(_HR["ENABLE_DISCHARGE"], 1)),
            ]
            src = "in-memory"
        else:
            reserve = max(4, min(100, SCHEDULER_BASELINE_SOC_RESERVE))
            restore_list = [
                (_CHARGE_SLOT_HR[0][0],       0),
                (_CHARGE_SLOT_HR[0][1],       0),
                (_DISCHARGE_SLOT_HR[0][0],    0),
                (_DISCHARGE_SLOT_HR[0][1],    0),
                (_HR["ENABLE_CHARGE"],         0),
                (_HR["ENABLE_CHARGE_TARGET"],  0),
                (_HR["BATTERY_POWER_MODE"],    1),
                (_HR["BATTERY_SOC_RESERVE"], reserve),
                (_HR["ENABLE_DISCHARGE"],      1),
            ]
            src = "safe-baseline"

        ok_count = fail_count = verify_fail_count = 0
        for reg, val in restore_list:
            try:
                _hr_write(slave, reg, val)
                time.sleep(_SCHED_WRITE_GAP)
                ok_count += 1
                # Read back to confirm the value actually landed (catches silent cloud overrides)
                try:
                    readback = _hr_read(slave, reg, 1, timeout=3.0)
                    if readback[0] != val:
                        verify_fail_count += 1
                        log.warning("Quick action revert: HR%d verify failed "
                                    "(wrote %d, read back %d -- possible cloud sync conflict)",
                                    reg, val, readback[0])
                except Exception as vexc:
                    log.warning("Quick action revert: HR%d verify read failed (%s)", reg, vexc)
            except Exception as exc:
                fail_count += 1
                log.warning("Quick action revert: HR%d write failed (%s)", reg, exc)

        if fail_count == 0 and verify_fail_count == 0:
            msg = (f"Quick action reverted and verified "
                   f"({ok_count} regs, src={src}, trigger={trigger})")
        elif fail_count == 0:
            msg = (f"Quick action revert: writes ok but {verify_fail_count} register(s) did not hold "
                   f"-- check GivEnergy app is not overriding local control "
                   f"({ok_count} written, src={src}, trigger={trigger})")
        else:
            msg = (f"Quick action revert partial "
                   f"({ok_count} ok, {fail_count} failed, {verify_fail_count} verify-fail, "
                   f"src={src}, trigger={trigger})")
        log.warning(msg)
        _log_control("quick_action",
                     {"action": "revert", "trigger": trigger, "src": src},
                     fail_count == 0 and verify_fail_count == 0, msg)
    except Exception as exc:
        msg = f"Quick action revert failed (trigger={trigger}): {exc}"
        log.warning(msg)
        _log_control("quick_action", {"action": "revert", "trigger": trigger}, False, msg)
    finally:
        _qa_clear_state()


def _maybe_quick_action_tick():
    """Called every poll cycle. Reverts any quick action whose 1-hour window has expired."""
    global _quick_charge_until, _quick_discharge_until
    now = time.time()
    if _quick_charge_until > 0 and now >= _quick_charge_until:
        log.warning("Quick charge: 1-hour window expired - reverting")
        _quick_charge_until = 0.0
        state = _qa_load_state()
        threading.Thread(target=_quick_action_revert,
                         args=(state, "auto"), daemon=True).start()
    elif _quick_discharge_until > 0 and now >= _quick_discharge_until:
        log.warning("Quick discharge: 1-hour window expired - reverting")
        _quick_discharge_until = 0.0
        state = _qa_load_state()
        threading.Thread(target=_quick_action_revert,
                         args=(state, "auto"), daemon=True).start()


def _qa_startup_recovery():
    """Check for a persisted quick-action state left over from a previous process.
    Called as a background thread at startup. Waits for inverter detection first so
    the recovery write doesn't race with the listen socket opening."""
    global _quick_charge_until, _quick_discharge_until
    state = _qa_load_state()
    if state is None:
        return
    # Wait up to 30 s for the inverter to be detected on the listen socket.
    for _ in range(60):
        if _inverter_profile and _inverter_profile != "unknown":
            break
        time.sleep(0.5)
    else:
        log.warning("Quick action startup recovery: inverter not detected in 30 s - skipped")
        return

    now        = time.time()
    end_epoch  = state.get("end_epoch", 0)
    action     = state.get("action", "unknown")
    if now >= end_epoch:
        log.warning("Quick action startup recovery: window expired (action=%s) - running housekeeping", action)
        _quick_action_revert(state, trigger="startup")
    else:
        remaining = int(end_epoch - now)
        log.warning("Quick action startup recovery: re-arming %s timer (%d s remaining)", action, remaining)
        if action == "charge":
            _quick_charge_until    = end_epoch
            _quick_discharge_until = 0.0
        elif action == "discharge":
            _quick_discharge_until = end_epoch
            _quick_charge_until    = 0.0


def _sched_apply(desired: dict) -> None:
    """Apply a desired state EXACTLY like a manual control button — _hr_write to the
    detected slave (0x32 on this Gen2) with the listen loop left OPEN, from this (scheduler)
    thread, concurrently with the listen loop. Confirmed on the Pi: that combination writes
    reliably; writing to 0x11 or with the listen socket closed returns 'dongle busy'.
    Only registers whose value differs from what we last wrote are sent (DELTA) — usually
    1–4 writes per block change. On failure _sched_applied_sig is left unchanged so the 15s
    loop retries (writes are idempotent absolute sets, and the delta means a retry only
    re-sends what hasn't landed yet)."""
    global _sched_applied_sig, _sched_last_status
    try:
        slave, writes, summary = _sched_compute_writes(desired)
    except Exception as exc:
        _sched_last_status = f"error: {exc}"
        log.warning("Scheduler apply failed (will retry): %s", exc)
        return
    pending = [(r, v) for r, v in writes if _sched_applied_regs.get(r) != v]   # delta
    if not pending:
        _sched_applied_sig = json.dumps(desired, sort_keys=True)
        return                                        # already in the wanted state

    _sched_last_status = "applying"
    ok = True
    try:
        for i, (reg, val) in enumerate(pending):
            if i:
                time.sleep(_SCHED_WRITE_GAP)
            _hr_write(slave, reg, val)                # default 7 attempts — IDENTICAL to the
                                                      # manual controls, which write reliably;
                                                      # the dongle-busy is intermittent so the
                                                      # wider retry window catches a free moment
            _sched_applied_regs[reg] = val            # remember what we wrote (delta state)
    except Exception as exc:
        ok = False
        _sched_last_status = "busy — will retry"
        log.warning("Scheduler apply failed (will retry): %s", exc)

    if ok:
        _sched_applied_sig = json.dumps(desired, sort_keys=True)
        _sched_last_status = summary
        _log_control("scheduler", desired, True, "Scheduler: " + summary)
        log.info("Scheduler applied: %s", summary)

def _group_regs(reg_keys, gap=5):
    """Group register numbers into contiguous read ranges, merging gaps <= gap registers.
    Returns [(base, count), ...] — minimises the number of _hr_read calls."""
    if not reg_keys:
        return []
    keys = sorted(reg_keys)
    ranges, start, end = [], keys[0], keys[0]
    for k in keys[1:]:
        if k <= end + gap:
            end = k
        else:
            ranges.append((start, end - start + 1))
            start = end = k
    ranges.append((start, end - start + 1))
    return ranges


def _sched_drift_check():
    """Read back registers the scheduler last wrote and re-apply if any have drifted.
    Runs every 5 minutes so external changes (GE app, HA, cloud portal, inverter reset)
    are corrected within that window rather than waiting for the next block boundary."""
    global _sched_applied_sig
    if not SCHEDULER_ENABLED or not _sched_applied_regs or _sched_applied_sig is None:
        return
    snap = dict(_sched_applied_regs)   # thread-safe snapshot
    slave = _inverter_slave if _inverter_slave else 0x11
    drifted = []
    try:
        for base, count in _group_regs(snap.keys()):
            vals = _hr_read(slave, base, count, timeout=3.0)
            for i, reg in enumerate(range(base, base + count)):
                if reg in snap and i < len(vals) and vals[i] != snap[reg]:
                    drifted.append((reg, snap[reg], vals[i]))
    except Exception as exc:
        log.warning("Scheduler drift check read failed: %s", exc)
        return
    if drifted:
        detail = ", ".join(f"HR{r}={a}(exp {e})" for r, e, a in drifted)
        log.warning("Scheduler: state drift detected — re-applying (%s)", detail)
        _log_event("info", "Scheduler: state drift detected — re-applying")
        _sched_applied_sig = None   # force full re-apply on next 15 s tick


def _sched_drift_loop():
    """Every 5 minutes verify the scheduler's last-written registers still match the inverter."""
    time.sleep(60)   # let startup and first apply settle
    while True:
        try:
            _sched_drift_check()
        except Exception as exc:
            log.error("Scheduler drift loop: %s", exc)
        time.sleep(300)


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
                    and _inverter_profile in _SCHED_PROFILES):
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
    data["scheduler_active"]      = SCHEDULER_ENABLED
    data["scheduler_status"]      = _sched_last_status
    data["scheduler_task_active"] = _sched_task_active()
    # Quick-action state (unauthenticated — just display flags and countdown)
    _now = time.time()
    _qc  = _quick_charge_until    > _now
    _qd  = _quick_discharge_until > _now
    data["quick_actions_enabled"]      = QUICK_ACTIONS_ENABLED
    data["quick_charge_active"]        = _qc
    data["quick_discharge_active"]     = _qd
    data["quick_charge_remaining"]     = max(0, int(_quick_charge_until    - _now)) if _qc else 0
    data["quick_discharge_remaining"]  = max(0, int(_quick_discharge_until - _now)) if _qd else 0
    # Inverter profile — needed by the frontend to hide quick-action bar on unsupported profiles
    data["inverter_profile"] = _inverter_profile or ""
    # Running backend version — drives the footer so it can never lag the deployed code.
    data["app_version"] = APP_VERSION
    data["solar_forecast_configured"] = _sf_configured()
    # Lets the frontend skip the password modal for control panels when the
    # local-only convenience mode is on.
    data["require_auth"] = REQUIRE_AUTH
    return jsonify(data)

@app.route("/api/control", methods=["GET"])
def get_control():
    if not _action_authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    try:
        state = _read_control_state()
        log.info("Control: GET completed, profile=%s model=%s",
                 state.get("profile", "?"), state.get("model", "?"))
        resp = make_response(jsonify(state))
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as exc:
        log.warning("Control state read failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/control", methods=["POST"])
def post_control():
    if not _action_authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    data    = (request.get_json(silent=True) or {})
    command = data.get("command", "")
    params  = data.get("params", {})
    if not isinstance(params, dict):
        return jsonify({"ok": False, "error": "params must be an object"}), 400
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
        "power_pct":  r["power_pct"],
        "predictive": bool(r["predictive"]),
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
    elif action == "export":
        target_soc = max(4, min(100, int(data.get("target_soc", 4))))
    power_pct  = max(0, min(50, int(data.get("power_pct", 50))))
    predictive = bool(data.get("predictive", False)) and action == "charge"
    return {"action": action, "start": start, "end": end,
            "days_mask": days_mask, "target_soc": target_soc,
            "power_pct": power_pct, "predictive": predictive}

@app.route("/api/schedules", methods=["GET"])
def get_schedules():
    if not _action_authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    with _db() as conn:
        rows = [_rule_row(r) for r in conn.execute(
            "SELECT * FROM schedules ORDER BY start_hhmm, id")]
    return jsonify({
        "ok":                     True,
        "master_enabled":         SCHEDULER_ENABLED,
        "baseline":               SCHEDULER_BASELINE,
        "baseline_soc_reserve":   SCHEDULER_BASELINE_SOC_RESERVE,
        "power_units":            POWER_UNITS,
        "max_charge_w":           MAX_CHARGE_W,
        "max_discharge_w":        MAX_DISCHARGE_W,
        "rules":                  rows,
    })

@app.route("/api/schedules", methods=["POST"])
def save_schedule():
    """Create (no id) or update (id present) a single rule."""
    if not _action_authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    data = (request.get_json(silent=True) or {})
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
                "days_mask=?, target_soc=?, power_pct=?, predictive=? WHERE id=?",
                (enabled, clean["action"], clean["start"], clean["end"],
                 clean["days_mask"], clean["target_soc"], clean["power_pct"],
                 int(clean["predictive"]), int(rid)))
        else:
            cur = conn.execute(
                "INSERT INTO schedules (enabled, action, start_hhmm, end_hhmm, "
                "days_mask, target_soc, power_pct, predictive, created) VALUES (?,?,?,?,?,?,?,?,?)",
                (enabled, clean["action"], clean["start"], clean["end"],
                 clean["days_mask"], clean["target_soc"], clean["power_pct"],
                 int(clean["predictive"]), int(time.time())))
            rid = cur.lastrowid
        conn.commit()
    return jsonify({"ok": True, "id": rid})

@app.route("/api/schedules/<int:rid>", methods=["DELETE"])
def delete_schedule(rid):
    if not _action_authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    with _db() as conn:
        conn.execute("DELETE FROM schedules WHERE id=?", (rid,))
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/schedules/config", methods=["POST"])
def save_schedule_config():
    """Set the master on/off switch, baseline mode, and baseline SOC reserve."""
    global SCHEDULER_ENABLED, SCHEDULER_BASELINE, SCHEDULER_BASELINE_SOC_RESERVE
    if not _action_authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    data = (request.get_json(silent=True) or {})
    cfg  = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(_CONFIG_PATH)
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
    if "baseline_soc_reserve" in data:
        val = max(4, min(100, int(data["baseline_soc_reserve"])))
        SCHEDULER_BASELINE_SOC_RESERVE = val
        cfg.set("scheduler", "baseline_soc_reserve", str(val))
    with open(_CONFIG_PATH, "w") as f:
        cfg.write(f)
    return jsonify({"ok":                   True,
                    "master_enabled":        SCHEDULER_ENABLED,
                    "baseline":              SCHEDULER_BASELINE,
                    "baseline_soc_reserve":  SCHEDULER_BASELINE_SOC_RESERVE})

@app.route("/api/quick_action", methods=["POST"])
def quick_action_endpoint():
    """Start or cancel a 1-hour quick charge / discharge action."""
    global _quick_charge_until, _quick_discharge_until
    if not _action_authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    if not QUICK_ACTIONS_ENABLED:
        return jsonify({"ok": False, "error": "Quick actions are not enabled in settings"})

    data   = (request.get_json(silent=True) or {})
    action = data.get("action", "")          # "charge" or "discharge"
    start  = bool(data.get("start", True))

    if action not in ("charge", "discharge"):
        return jsonify({"ok": False, "error": "action must be 'charge' or 'discharge'"})

    if not start:
        # Manual cancel: load persisted state so revert only touches registers
        # the action set. Slot time regs are cleared first so the firmware stops
        # the export/charge immediately (firmware will not stop early on its own).
        state = _qa_load_state()
        if action == "charge":
            _quick_charge_until = 0.0
        else:
            _quick_discharge_until = 0.0
        threading.Thread(target=_quick_action_revert,
                         args=(state, "manual"), daemon=True).start()
        return jsonify({"ok": True, "message": f"Quick {action} cancelled"})

    # Start
    try:
        cs, ce, end_epoch = _quick_action_do(action)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

    if action == "charge":
        _quick_charge_until    = end_epoch
        _quick_discharge_until = 0.0
    else:
        _quick_discharge_until = end_epoch
        _quick_charge_until    = 0.0

    return jsonify({"ok": True, "message": f"Quick {action} started (slot {cs:04d}-{ce:04d})"})


@app.route("/api/logs")
def get_logs():
    if not _action_authorised():
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
    CRC handling (the fix for Gen3 — confirmed against givenergy_modbus v2.0.4, which
    reads every device with one slave-inclusive CRC): try the **slave-inclusive LSB-first**
    CRC first — Gen3 strictly validates CRC and silently drops wrong-CRC frames, and Gen2
    accepts it too — then fall back to the legacy **MSB-first, no-slave** BMS CRC (the Gen2
    listen-poke convention) in case a module only answers that. Returns a decoded dict or
    None if the module responds to neither."""
    base, count = 60, 60
    serial  = b"AB1234G567"
    padding = b"\x00" * 7 + b"\x08"
    inner   = bytes([slave, 0x04]) + base.to_bytes(2, "big") + count.to_bytes(2, "big")
    crc_variants = (
        _crc16(inner),                  # slave-inclusive LSB-first (Gen3 + Gen2)
        _bms_crc16(0x04, base, count),  # legacy MSB-first, no slave (Gen2-only)
    )

    for crc in crc_variants:
        payload = serial + padding + inner + crc
        length  = len(payload) + 2
        frame   = b"\x59\x59\x00\x01" + length.to_bytes(2, "big") + b"\x01\x02" + payload
        s.sendall(frame)
        buf      = bytearray()
        deadline = time.time() + 3.0
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
                    # Capacity registers (uint32 pairs, high word first — only the
                    # remaining/design RATIO is consumed, so word order cannot skew it)
                    "cap_design":    (g(26) << 16) | g(27),   # IR86-87
                    "cap_remaining": (g(28) << 16) | g(29),   # IR88-89
                }
        # no decodable response with this CRC — try the next variant
    return None


# ── Capacity-weighted SOC fallback ─────────────────────────────────────────────
# When IR59 reads 0 with no good prior value (corrupt/blank reads from startup),
# estimate SOC from the LV battery modules' capacity registers instead:
# sum(cap_remaining) / sum(cap_design) × 100.  Only profiles with LV modules.
_BMS_SOC_PROFILES   = ("single_phase_2slot", "single_phase_ac_coupled",
                       "single_phase_extended")
_BMS_SOC_RETRY_SECS = 300.0   # opens a second socket (kicks the listen loop) — be sparing
_bms_soc_cache = {"ts": 0.0, "soc": None}


def _capacity_weighted_soc(modules) -> "int | None":
    """Capacity-weighted SOC % across decoded battery module dicts.
    Pure ratio math — modules with a missing or zero design capacity are skipped."""
    total_rem = total_des = 0
    for m in modules:
        rem, des = m.get("cap_remaining"), m.get("cap_design")
        if rem is None or not des:
            continue
        total_rem += rem
        total_des += des
    if total_des <= 0:
        return None
    return max(0, min(100, round(total_rem * 100 / total_des)))


def _bms_soc_fallback_value() -> "int | None":
    """Rate-limited capacity-weighted SOC estimate from the battery BMS.
    Returns the cached estimate between reads; None when unavailable.
    The BMS read opens a fresh socket, which briefly kicks the listen loop
    (recovers in ~5 s) — hence the long retry interval."""
    if _inverter_profile not in _BMS_SOC_PROFILES:
        return None
    now = time.time()
    if now - _bms_soc_cache["ts"] < _BMS_SOC_RETRY_SECS:
        return _bms_soc_cache["soc"]
    _bms_soc_cache["ts"] = now          # failures also wait out the full interval
    modules = []
    try:
        s = socket.create_connection((INVERTER_IP, INVERTER_PORT), timeout=5)
        s.settimeout(3)
        try:
            for i in range(max(1, NUM_BATTERIES)):
                m = _read_battery_module(s, 0x32 + i)
                if not m:
                    break
                modules.append(m)
        finally:
            s.close()
    except OSError as exc:
        log.warning("BMS SOC fallback read failed: %s", exc)
    est = _capacity_weighted_soc(modules)
    _bms_soc_cache["soc"] = est
    if est is not None:
        log.warning("SOC fallback: IR59 reads 0 — using BMS capacity estimate %d%%", est)
    return est


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
        "weather_postcode":    WEATHER_POSTCODE,
        "weather_geohash":     MET_GEOHASH,
        "weather_poll_mins":   WEATHER_POLL_MINS,
        "backup_enabled":      BACKUP_ENABLED,
        "backup_keep_days":    BACKUP_KEEP_DAYS,
        "last_backup":         _last_backup_info()[1] or "none yet",
        "check_for_updates":        CHECK_UPDATES,
        "require_auth":             REQUIRE_AUTH,
        "app_version":              APP_VERSION,
        "chart_colors":             CHART_COLORS,
        "quick_actions_enabled":     QUICK_ACTIONS_ENABLED,
        "quick_charge_power_pct":    QUICK_CHARGE_POWER_PCT,
        "quick_discharge_power_pct": QUICK_DISCHARGE_POWER_PCT,
        "quick_charge_target_soc":   QUICK_CHARGE_TARGET_SOC,
        "sf_postcode":               SF_POSTCODE,
        "sf_latitude":               SF_LAT,
        "sf_longitude":              SF_LON,
        "sf_panel_tilt":             SF_TILT,
        "sf_panel_azimuth":          SF_AZ,
        "sf_panel_kwp":              SF_KWP,
        "solar_forecast_configured": _sf_configured(),
        "pred_battery_kwh":          PRED_BATTERY_KWH,
        "pred_pessimism_factor":     PRED_PESSIMISM,
        "pred_evening_reserve":      PRED_EVENING_RESERVE,
        "pred_minimum_soc":          PRED_MIN_SOC,
        "pred_current_target":       _compute_predictive_target(),
        "pred_tomorrow_kwh":         _get_tomorrow_forecast_kwh(),
        "pred_morning_demand_kwh":   _compute_morning_demand_kwh(),
        "pred_demand_days":          _DEMAND_DAYS,
        "log_level":          LOG_LEVEL,
        "log_retention_days": LOG_RETENTION_DAYS,
        # API keys are intentionally never returned to the browser
    })

@app.route("/api/settings", methods=["POST"])
def save_settings():
    global INVERTER_IP, INVERTER_PORT, NUM_BATTERIES, POLL_INTERVAL
    global DATA_RETENTION_DAYS, MET_API_KEY, MET_GEOHASH, WEATHER_POSTCODE, _last_weather_ts
    global ADMIN_HASH, WEATHER_POLL_MINS
    global POWER_UNITS, MAX_CHARGE_W, MAX_DISCHARGE_W
    global BACKUP_ENABLED, BACKUP_KEEP_DAYS, CHECK_UPDATES, CHART_COLORS
    global QUICK_ACTIONS_ENABLED, QUICK_CHARGE_POWER_PCT, QUICK_DISCHARGE_POWER_PCT, QUICK_CHARGE_TARGET_SOC
    global SF_LAT, SF_LON, SF_TILT, SF_AZ, SF_KWP, SF_API_KEY, SF_POSTCODE, _last_sf_ts
    global REQUIRE_AUTH
    global LOG_LEVEL, LOG_RETENTION_DAYS

    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401

    data = (request.get_json(silent=True) or {})

    # -- input validation --
    if "inverter_ip" in data:
        ip = str(data["inverter_ip"]).strip()
        if not ip or len(ip) > 253 or not re.match(r'^[a-zA-Z0-9._:\[\]-]+$', ip):
            return jsonify({"ok": False, "error": "Invalid inverter IP/hostname"}), 400
    if "new_password" in data and len(str(data["new_password"])) > 128:
        return jsonify({"ok": False, "error": "Password too long (max 128 characters)"}), 400
    if "weather_api_key" in data and len(str(data["weather_api_key"])) > 256:
        return jsonify({"ok": False, "error": "API key too long"}), 400
    if "weather_postcode" in data and len(str(data["weather_postcode"])) > 10:
        return jsonify({"ok": False, "error": "Postcode too long"}), 400
    if "sf_postcode" in data and len(str(data["sf_postcode"])) > 10:
        return jsonify({"ok": False, "error": "Postcode too long"}), 400
    if "sf_api_key" in data and len(str(data["sf_api_key"])) > 256:
        return jsonify({"ok": False, "error": "API key too long"}), 400

    cfg  = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(_CONFIG_PATH)

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
    if "require_auth" in data:
        REQUIRE_AUTH = bool(data["require_auth"]); _set("server", "require_auth", "yes" if REQUIRE_AUTH else "no")

    if not cfg.has_section("weather"): cfg.add_section("weather")
    if "weather_poll_mins" in data:
        WEATHER_POLL_MINS = max(5, int(data["weather_poll_mins"]))
        cfg.set("weather", "poll_interval_mins", str(WEATHER_POLL_MINS))
    new_key = (data.get("weather_api_key") or "").strip()
    if new_key:
        MET_API_KEY = new_key;  cfg.set("weather", "met_api_key", MET_API_KEY); _last_weather_ts = 0
    if "weather_postcode" in data:
        WEATHER_POSTCODE = (data["weather_postcode"] or "").strip().upper()
        cfg.set("weather", "postcode", WEATHER_POSTCODE)
    if "weather_geohash" in data:
        gh = data["weather_geohash"].strip()
        if gh and len(gh) != 6:
            return jsonify({"ok": False, "error": f"Geohash must be exactly 6 characters (got {len(gh)})"}), 400
        MET_GEOHASH = gh; cfg.set("weather", "geohash", MET_GEOHASH); _last_weather_ts = 0

    if isinstance(data.get("chart_colors"), dict):
        if not cfg.has_section("colours"): cfg.add_section("colours")
        for k, default_v in _COLOUR_DEFAULTS.items():
            v = str(data["chart_colors"].get(k, "")).strip()
            if _valid_hex(v):
                CHART_COLORS[k] = v
                cfg.set("colours", k, v)

    if "quick_actions_enabled" in data:
        QUICK_ACTIONS_ENABLED = bool(data["quick_actions_enabled"])
        _set("quick_actions", "enabled", "yes" if QUICK_ACTIONS_ENABLED else "no")
    if "quick_charge_power_pct" in data:
        QUICK_CHARGE_POWER_PCT = max(1, min(100, int(data["quick_charge_power_pct"])))
        _set("quick_actions", "charge_power_pct", QUICK_CHARGE_POWER_PCT)
    if "quick_discharge_power_pct" in data:
        QUICK_DISCHARGE_POWER_PCT = max(1, min(100, int(data["quick_discharge_power_pct"])))
        _set("quick_actions", "discharge_power_pct", QUICK_DISCHARGE_POWER_PCT)
    if "quick_charge_target_soc" in data:
        QUICK_CHARGE_TARGET_SOC = max(4, min(100, int(data["quick_charge_target_soc"])))
        _set("quick_actions", "charge_target_soc", QUICK_CHARGE_TARGET_SOC)

    # ── Solar forecast settings ────────────────────────────────────────────────
    _sf_section = "solar_forecast"
    _sf_dirty = False
    if "sf_postcode" in data:
        SF_POSTCODE = (data["sf_postcode"] or "").strip().upper()
        if not cfg.has_section(_sf_section):
            cfg.add_section(_sf_section)
        cfg.set(_sf_section, "postcode", SF_POSTCODE)
    for _sfkey, _sfattr, _sfcfg in (
            ("sf_latitude",     "SF_LAT",  "latitude"),
            ("sf_longitude",    "SF_LON",  "longitude"),
            ("sf_panel_tilt",   "SF_TILT", "panel_tilt"),
            ("sf_panel_azimuth","SF_AZ",   "panel_azimuth"),
            ("sf_panel_kwp",    "SF_KWP",  "panel_kwp"),
    ):
        if _sfkey in data:
            val = str(data[_sfkey]).strip()
            if val:
                try:
                    float(val)
                except ValueError:
                    return jsonify({"ok": False, "error": f"{_sfkey} must be a number"}), 400
            if not cfg.has_section(_sf_section):
                cfg.add_section(_sf_section)
            cfg.set(_sf_section, _sfcfg, val)
            globals()[_sfattr] = val
            _sf_dirty = True
    new_sf_key = (data.get("sf_api_key") or "").strip()
    if new_sf_key:
        if not cfg.has_section(_sf_section):
            cfg.add_section(_sf_section)
        SF_API_KEY = new_sf_key
        cfg.set(_sf_section, "api_key", SF_API_KEY)
        _sf_dirty = True
    if _sf_dirty:
        _last_sf_ts = 0   # force a fetch on next poll cycle

    # ── Predictive charge settings ────────────────────────────────────────────
    global PRED_BATTERY_KWH, PRED_PESSIMISM, PRED_EVENING_RESERVE, PRED_MIN_SOC
    _pred_section = "predictive_charge"
    for _pk, _pattr, _pcfg, _pfn in (
            ("pred_battery_kwh",     "PRED_BATTERY_KWH",     "battery_kwh",        float),
            ("pred_pessimism_factor","PRED_PESSIMISM",        "pessimism_factor",   float),
            ("pred_evening_reserve", "PRED_EVENING_RESERVE", "evening_reserve_kwh",float),
            ("pred_minimum_soc",     "PRED_MIN_SOC",         "minimum_soc",        int),
    ):
        if _pk in data:
            try:
                _pv = _pfn(data[_pk])
            except (ValueError, TypeError):
                return jsonify({"ok": False, "error": f"{_pk} must be a number"}), 400
            globals()[_pattr] = _pv
            if not cfg.has_section(_pred_section):
                cfg.add_section(_pred_section)
            cfg.set(_pred_section, _pcfg, str(_pv))

    new_pw = (data.get("new_password") or "").strip()
    if new_pw:
        if not cfg.has_section("admin"): cfg.add_section("admin")
        ADMIN_HASH = _hash_password(new_pw)
        cfg.set("admin", "password_hash", ADMIN_HASH)

    if "log_level" in data:
        lvl = str(data["log_level"]).lower()
        if lvl in ("debug", "info", "warning"):
            _set_log_level(lvl)
            _set("logging", "log_level", lvl)
    if "log_retention_days" in data:
        LOG_RETENTION_DAYS = max(1, min(30, int(data["log_retention_days"])))
        if _log_file_handler:
            _log_file_handler.backupCount = LOG_RETENTION_DAYS
        _set("logging", "log_retention_days", LOG_RETENTION_DAYS)

    with open(_CONFIG_PATH, "w") as f:
        cfg.write(f)
    return jsonify({"ok": True,
                    "weather_configured": bool(MET_API_KEY and MET_GEOHASH),
                    "solar_forecast_configured": _sf_configured()})


@app.route("/api/tariff", methods=["GET"])
def get_tariff():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    return jsonify({
        "ok":                True,
        "currency_symbol":   TARIFF_CURRENCY,
        "standing_charge_p": TARIFF_STANDING_P,
        "export_rate_p":     TARIFF_EXPORT_P,
        "import_rate_p":     TARIFF_IMPORT_P,
        "tou_windows":       TARIFF_TOU,
        # Auto-tariff fields
        "tariff_source":     TARIFF_SOURCE,
        "octopus_region":    OCTOPUS_REGION,
        "octopus_postcode":  OCTOPUS_POSTCODE,
        "rates_last_fetched": _RATES_LAST_FETCHED,
        "fetch_status":      _fetch_status,
    })


@app.route("/api/tariff", methods=["POST"])
def save_tariff():
    global TARIFF_CURRENCY, TARIFF_STANDING_P, TARIFF_EXPORT_P, TARIFF_IMPORT_P
    global TARIFF_TOU, _tariff_dirty
    global TARIFF_SOURCE, OCTOPUS_REGION, OCTOPUS_POSTCODE
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    data = (request.get_json(silent=True) or {})

    # -- input validation --
    if "tariff_source" in data and str(data["tariff_source"]) not in _TARIFF_SOURCES:
        return jsonify({"ok": False, "error": "Unknown tariff source"}), 400
    if "octopus_region" in data:
        rgn = str(data["octopus_region"]).upper().strip()
        if rgn and not re.match(r'^[A-P]$', rgn):
            return jsonify({"ok": False, "error": "Region must be a single letter A-P"}), 400
    if "octopus_postcode" in data and len(str(data["octopus_postcode"])) > 8:
        return jsonify({"ok": False, "error": "Postcode too long"}), 400
    if isinstance(data.get("tou_windows"), list):
        for w in data["tou_windows"][:_MAX_TOU_WINDOWS]:
            if len(str(w.get("name", "")).strip()) > 40:
                return jsonify({"ok": False, "error": "TOU window name too long (max 40 characters)"}), 400

    cfg  = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(_CONFIG_PATH)
    if not cfg.has_section("tariff"):
        cfg.add_section("tariff")

    source_changed = False
    if "tariff_source" in data:
        new_src = str(data["tariff_source"])
        if new_src != TARIFF_SOURCE:
            source_changed = True
        TARIFF_SOURCE = new_src
        cfg.set("tariff", "tariff_source", TARIFF_SOURCE)
    if "octopus_region" in data:
        OCTOPUS_REGION = str(data["octopus_region"]).upper().strip()
        cfg.set("tariff", "octopus_region", OCTOPUS_REGION)
    if "octopus_postcode" in data:
        OCTOPUS_POSTCODE = str(data["octopus_postcode"]).strip()
        cfg.set("tariff", "octopus_postcode", OCTOPUS_POSTCODE)

    if "currency_symbol" in data:
        TARIFF_CURRENCY = str(data["currency_symbol"])[:4] or "£"
        cfg.set("tariff", "currency_symbol", TARIFF_CURRENCY)
    if "standing_charge_p" in data:
        TARIFF_STANDING_P = max(0.0, float(data["standing_charge_p"]))
        cfg.set("tariff", "standing_charge_p", str(TARIFF_STANDING_P))
    if "export_rate_p" in data:
        TARIFF_EXPORT_P = max(0.0, float(data["export_rate_p"]))
        cfg.set("tariff", "export_rate_p", str(TARIFF_EXPORT_P))
    if "import_rate_p" in data:
        TARIFF_IMPORT_P = max(0.0, float(data["import_rate_p"]))
        cfg.set("tariff", "import_rate_p", str(TARIFF_IMPORT_P))

    windows = data.get("tou_windows")
    if isinstance(windows, list):
        TARIFF_TOU = []
        for i in range(1, _MAX_TOU_WINDOWS + 1):
            cfg.remove_option("tariff", f"tou_{i}_name")
            cfg.remove_option("tariff", f"tou_{i}_start")
            cfg.remove_option("tariff", f"tou_{i}_end")
            cfg.remove_option("tariff", f"tou_{i}_rate_p")
            cfg.remove_option("tariff", f"tou_{i}_export_rate_p")
        for i, w in enumerate(windows[:_MAX_TOU_WINDOWS], 1):
            name       = str(w.get("name",   "")).strip()
            start      = str(w.get("start",  "")).strip()
            end        = str(w.get("end",    "")).strip()
            rate       = max(0.0, float(w.get("rate_p",        0)))
            export_rt  = max(0.0, float(w.get("export_rate_p", 0)))
            if name and start and end:
                TARIFF_TOU.append({"name": name, "start": start, "end": end,
                                   "rate_p": rate, "export_rate_p": export_rt})
                cfg.set("tariff", f"tou_{i}_name",           name)
                cfg.set("tariff", f"tou_{i}_start",          start)
                cfg.set("tariff", f"tou_{i}_end",            end)
                cfg.set("tariff", f"tou_{i}_rate_p",         str(rate))
                cfg.set("tariff", f"tou_{i}_export_rate_p",  str(export_rt))

    with open(_CONFIG_PATH, "w") as f:
        cfg.write(f)

    _tariff_dirty = True   # signal poll thread to recompute today's cost totals
    # Clear cached daily costs so history is recomputed with the new rates
    try:
        with _db() as conn:
            conn.execute("DELETE FROM daily_costs")
            conn.commit()
    except Exception:
        pass
    # If the source or region changed, kick off a background fetch
    if source_changed and TARIFF_SOURCE not in ("manual", "") and OCTOPUS_REGION:
        threading.Thread(target=_do_rate_fetch, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/tariff/octopus-status")
def tariff_octopus_status():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    now_ts = time.time()
    cur_rate = nxt_rate = cur_exp = nxt_exp = None
    if TARIFF_SOURCE in _VARIABLE_RATE_SOURCES:
        cur_rate = round(_agile_rate_at(now_ts,         "import"), 4)
        nxt_rate = round(_agile_rate_at(now_ts + 1800,  "import"), 4)
        cur_exp  = round(_agile_rate_at(now_ts,         "export"), 4)
    now_utc  = datetime.utcnow()
    today_s  = now_utc.strftime("%Y-%m-%d")
    tom_s    = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
    st = sm  = 0
    try:
        with _db() as conn:
            st = conn.execute("SELECT COUNT(*) FROM agile_rates WHERE slot_start LIKE ?",
                              (today_s + "%",)).fetchone()[0]
            sm = conn.execute("SELECT COUNT(*) FROM agile_rates WHERE slot_start LIKE ?",
                              (tom_s + "%",)).fetchone()[0]
    except Exception:
        pass
    return jsonify({
        "ok": True, "source": TARIFF_SOURCE, "region": OCTOPUS_REGION,
        "current_rate_p": cur_rate, "next_rate_p": nxt_rate,
        "current_export_p": cur_exp,
        "slots_today": st, "slots_tomorrow": sm,
        "fetched_at": _RATES_LAST_FETCHED,
        "fetch_status": _fetch_status,
    })


@app.route("/api/tariff/fetch-now", methods=["POST"])
def tariff_fetch_now():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    result = _do_rate_fetch()
    return jsonify(result)


@app.route("/api/tariff/lookup-region", methods=["POST"])
def tariff_lookup_region():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    data     = (request.get_json(silent=True) or {})
    postcode = data.get("postcode", "").strip()
    if not postcode:
        return jsonify({"ok": False, "error": "Postcode required"}), 400
    if len(postcode) > 8:
        return jsonify({"ok": False, "error": "Invalid postcode"}), 400
    try:
        region = _lookup_region(postcode)
        if region:
            return jsonify({"ok": True, "region": region})
        return jsonify({"ok": False, "error": "Region not found for that postcode"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/log")
def get_log():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    log_path = _LOG_PATH
    if not log_path.exists():
        return jsonify({"ok": False, "error": "No log file yet"}), 404
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(str(log_path), as_attachment=True,
                     download_name=f"dashboard_{stamp}.log", mimetype="text/plain")


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

_BACKUP_IMPORT_RAW_MAX  = 10 * 1024 * 1024   # 10 MB compressed upload limit
_BACKUP_IMPORT_GUNZ_MAX = 100 * 1024 * 1024  # 100 MB decompressed limit (gzip-bomb guard)


@app.route("/api/backup/import", methods=["POST"])
def backup_import():
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    data = f.read(_BACKUP_IMPORT_RAW_MAX + 1)
    if len(data) > _BACKUP_IMPORT_RAW_MAX:
        return jsonify({"ok": False, "error": "Upload too large (max 10 MB)"}), 413
    # Transparently accept .gz or a raw .db
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except Exception:
            return jsonify({"ok": False, "error": "Could not decompress file"}), 400
        if len(data) > _BACKUP_IMPORT_GUNZ_MAX:
            return jsonify({"ok": False, "error": "Decompressed backup too large (max 100 MB)"}), 413
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

@app.route("/api/solar_forecast")
def api_solar_forecast():
    if not _sf_configured():
        return jsonify({"ok": False, "error": "not configured"})
    with _lock:
        cached = dict(_sf_cached)
    if not cached:
        return jsonify({"ok": False, "error": "not yet fetched"}), 503
    return jsonify({"ok": True, "result": cached,
                    "solar_forecast_configured": True})


@app.route("/api/weather/lookup_postcode", methods=["POST"])
def weather_lookup_postcode():
    """Resolve a UK postcode to a Met Office observation station geohash.
    Step 1: postcodes.io → lat/lng.  Step 2: Met Office /nearest → station geohash.
    Requires admin auth and a saved Met Office API key.
    Returns {ok, geohash, area, lat, lng} or {ok:False, error}."""
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    if not MET_API_KEY:
        return jsonify({"ok": False,
                        "error": "Met Office API key not configured — "
                                 "enter and save your API key first, then retry Lookup"}), 400
    data = (request.get_json(silent=True) or {})
    raw = (data.get("postcode") or "").strip().upper().replace(" ", "")
    if not raw:
        return jsonify({"ok": False, "error": "No postcode provided"}), 400
    if len(raw) > 8:
        return jsonify({"ok": False, "error": "Invalid postcode"}), 400
    # Step 1: postcode → lat/lng via postcodes.io (free, no auth)
    try:
        url = "https://api.postcodes.io/postcodes/" + urllib.parse.quote(raw)
        log.debug("Postcode lookup: GET %s", url)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        res = body.get("result") or {}
        lat = res.get("latitude")
        lng = res.get("longitude")
        if lat is None or lng is None:
            return jsonify({"ok": False, "error": "Postcode lookup returned no coordinates"}), 400
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return jsonify({"ok": False, "error": "Postcode not found — check and try again"}), 404
        return jsonify({"ok": False, "error": f"Postcode service error ({exc.code})"}), 502
    except Exception as exc:
        log.warning("Postcode lookup failed: %s", exc)
        return jsonify({"ok": False, "error": "Could not reach postcode service — enter geohash manually"}), 502
    # Step 2: lat/lng → nearest Met Office station via /nearest
    try:
        station = _met_nearest_station(float(lat), float(lng))
        geohash = station["geohash"]
        area    = station.get("area", "")
        log.info("Postcode %s → %.4f, %.4f → station %s (%s)", raw, lat, lng, geohash, area)
        return jsonify({"ok": True, "geohash": geohash, "area": area, "lat": lat, "lng": lng})
    except Exception as exc:
        log.warning("Met Office /nearest failed: %s", exc)
        return jsonify({"ok": False, "error": "Could not find nearest Met Office station — enter geohash manually"}), 502

@app.route("/api/solar_forecast/lookup_postcode", methods=["POST"])
def sf_lookup_postcode():
    """Resolve a UK postcode to lat/lng via postcodes.io (free, no API key).
    Independent of the Met Office weather lookup so the solar forecast can be
    configured without any weather setup.  Returns {ok, lat, lng, area}."""
    if not _authorised():
        return jsonify({"ok": False, "error": "Unauthorised"}), 401
    data = (request.get_json(silent=True) or {})
    raw = (data.get("postcode") or "").strip().upper().replace(" ", "")
    if not raw:
        return jsonify({"ok": False, "error": "No postcode provided"}), 400
    if len(raw) > 8:
        return jsonify({"ok": False, "error": "Invalid postcode"}), 400
    try:
        url = "https://api.postcodes.io/postcodes/" + urllib.parse.quote(raw)
        log.debug("Postcode lookup: GET %s", url)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        res = body.get("result") or {}
        lat = res.get("latitude")
        lng = res.get("longitude")
        if lat is None or lng is None:
            return jsonify({"ok": False, "error": "Postcode lookup returned no coordinates"}), 400
        area = res.get("admin_ward") or res.get("admin_district") or ""
        return jsonify({"ok": True, "lat": lat, "lng": lng, "area": area})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return jsonify({"ok": False, "error": "Postcode not found — check and try again"}), 404
        return jsonify({"ok": False, "error": f"Postcode service error ({exc.code})"}), 502
    except Exception as exc:
        log.warning("SF postcode lookup failed: %s", exc)
        return jsonify({"ok": False, "error": "Could not reach postcode service"}), 502

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

    # ── Cost estimates (if tariff configured) ───────────────────────────────────
    if result and _tariff_configured():
        row  = result[0]
        pkey = row.get("period", "")
        if not TARIFF_TOU:
            # Flat rate: costs derive directly from the kWh columns already in the result
            n_days   = len(_dates_for_period(period, pkey))
            imp_cost = round((row.get("grid_in_kwh",  0) or 0) * TARIFF_IMPORT_P, 2)
            exp_inc  = round((row.get("grid_out_kwh", 0) or 0) * TARIFF_EXPORT_P, 2)
            standing = round(TARIFF_STANDING_P * n_days, 2)
            row["cost"] = {
                "currency_symbol":   TARIFF_CURRENCY,
                "import_cost_p":     imp_cost,
                "export_income_p":   exp_inc,
                "standing_charge_p": standing,
                "total_cost_p":      round(imp_cost + standing, 2),
                "net_cost_p":        round(imp_cost + standing - exp_inc, 2),
            }
        else:
            # TOU: aggregate from per-day cost cache (computing any missing days)
            c = _get_costs_for_period(period, pkey)
            if c:
                c["currency_symbol"] = TARIFF_CURRENCY
                row["cost"] = c

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


# ── Year in Review ────────────────────────────────────────────────────────────

_yr_cache: "dict[int, tuple]" = {}   # year -> (data_dict, fetched_ts)
_YR_TTL_CURRENT = 60      # 60 s for current year so today's live records track in near-real-time
_YR_TTL_PAST    = 86400   # 24 h for past years (historical, rarely changes)

# Carbon-offset badge factors.
# Every solar kWh generated displaces grid electricity that would otherwise have
# been imported, so total generation x grid factor = CO2 avoided.
_CO2_KG_PER_KWH  = 0.207   # UK grid average (DEFRA), kgCO2 per kWh electricity
_CO2_KG_PER_TREE = 21.0    # CO2 a mature tree absorbs in a year, kg
_CO2_KG_PER_MILE = 0.27    # tailpipe CO2 for an average petrol car, kg per mile


def _year_review_data(year: int) -> dict:
    year_str = str(year)
    with _db() as conn:
        # Daily totals: last snapshot per day holds the inverter's final register values.
        # Same "last per day" pattern as api_history to avoid midnight carryover.
        # Today IS included here. The per-metric split below decides which tiles
        # may use today's partial (still-accumulating) totals: "record high"
        # tiles count today, "lowest day" tiles exclude it so a part-day doesn't
        # masquerade as the minimum.
        daily_rows = list(conn.execute(
            "SELECT date(ts,'unixepoch','localtime') AS day, "
            "       solar_today AS s, grid_in_today AS gi, grid_out_today AS go_, "
            "       bat_chg_today AS bc, bat_dis_today AS bd "
            "FROM snapshots "
            "WHERE ts IN ("
            "    SELECT MAX(ts) FROM snapshots "
            "    GROUP BY date(ts,'unixepoch','localtime')"
            ") AND strftime('%Y', date(ts,'unixepoch','localtime')) = ? "
            "ORDER BY day",
            (year_str,)
        ))
        cost_rows = list(conn.execute(
            "SELECT date, import_cost_p, export_income_p FROM daily_costs "
            "WHERE strftime('%Y', date) = ?",
            (year_str,)
        ))
        hourly_rows = list(conn.execute(
            "SELECT CAST(strftime('%H',ts,'unixepoch','localtime') AS INTEGER) AS hr, "
            "       ROUND(AVG(home_w),  1) AS avg_home_w, "
            "       ROUND(AVG(solar_w), 1) AS avg_solar_w "
            "FROM snapshots "
            "WHERE strftime('%Y',ts,'unixepoch','localtime') = ? "
            "  AND home_w IS NOT NULL "
            "GROUP BY hr ORDER BY hr",
            (year_str,)
        ))

    costs = {r["date"]: dict(r) for r in cost_rows}

    # daily_costs only gets today's row written at end of day, so for the current
    # year splice in the live in-memory accumulators so today's tiles can show
    # their running £ income / cost too.
    today = datetime.now().strftime("%Y-%m-%d")
    if year == datetime.now().year:
        costs[today] = {"import_cost_p":   _today_import_cost_p,
                        "export_income_p": _today_export_income_p}

    def _home(r):
        s  = r["s"]  or 0; gi = r["gi"] or 0; go = r["go_"] or 0
        bc = r["bc"] or 0; bd = r["bd"] or 0
        return max(0.0, s + gi + bd - go - bc)

    def _pick(pairs, top):
        if not pairs:
            return None
        day, val = (max if top else min)(pairs, key=lambda x: x[1])
        c = costs.get(day, {})
        return {"date": day, "kwh": round(val, 2),
                "import_cost_p":   round(float(c.get("import_cost_p",   0) or 0), 2),
                "export_income_p": round(float(c.get("export_income_p", 0) or 0), 2)}

    # Two candidate sets. "record high" tiles (best solar/export, worst import,
    # highest demand) count today; "lowest day" tiles (worst solar, most
    # efficient home) exclude it so a still-accumulating part-day can't win.
    def _rows(include_today):
        return daily_rows if include_today else [r for r in daily_rows if r["day"] != today]

    _all  = _rows(True)
    _past = _rows(False)

    solar_best  = [(r["day"], r["s"]  ) for r in _all  if (r["s"]  or 0) > 0.1]
    solar_worst = [(r["day"], r["s"]  ) for r in _past if (r["s"]  or 0) > 0.1]
    import_days = [(r["day"], r["gi"] ) for r in _all  if (r["gi"] or 0) > 0.1]
    export_days = [(r["day"], r["go_"]) for r in _all  if (r["go_"]or 0) > 0.1]
    home_high   = [(r["day"], _home(r)) for r in _all  if _home(r)      > 0.1]
    home_eff    = [(r["day"], _home(r)) for r in _past if _home(r)      > 0.1]

    # Carbon offset: total solar generated this year (today included) displaces
    # grid electricity, giving CO2 avoided and its tree / car-mile equivalents.
    total_solar = sum((r["s"] or 0) for r in daily_rows)
    co2_kg      = total_solar * _CO2_KG_PER_KWH

    return {
        "year":       year,
        "data_days":  len(daily_rows),
        "solar":      {"best": _pick(solar_best,  True),  "worst": _pick(solar_worst, False)},
        "export":     {"best": _pick(export_days, True)},
        "import":     {"worst": _pick(import_days, True)},
        "home":       {"best": _pick(home_eff,    False), "worst": _pick(home_high,   True)},
        "carbon":     {"solar_kwh": round(total_solar, 0),
                       "co2_kg":    round(co2_kg, 1),
                       "trees":     round(co2_kg / _CO2_KG_PER_TREE),
                       "miles":     round(co2_kg / _CO2_KG_PER_MILE)},
        "hourly_profile": [{"hour": r["hr"], "home_w": r["avg_home_w"],
                            "solar_w": r["avg_solar_w"]} for r in hourly_rows],
        "currency_symbol": TARIFF_CURRENCY,
    }


@app.route("/api/year_review")
def api_year_review():
    now_year = datetime.now().year
    try:
        year = int(request.args.get("year", now_year))
    except ValueError:
        return jsonify({"ok": False, "error": "invalid year"}), 400
    ttl = _YR_TTL_CURRENT if year == now_year else _YR_TTL_PAST
    cached, fetched = _yr_cache.get(year, (None, 0))
    if cached and (time.time() - fetched) < ttl:
        return jsonify(cached)
    try:
        data = _year_review_data(year)
    except Exception as exc:
        log.warning("Year review query failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    _yr_cache[year] = (data, time.time())
    return jsonify(data)


@app.route("/api/power")
def api_power():
    """Per-minute averaged power (W) for a single day.
    Signed: battery charging and grid export are negative (cost to user)."""
    day = request.args.get("day", "")
    if not day:
        return jsonify({"ok": False, "error": "day required"}), 400

    with _db() as conn:
        rows = conn.execute("""
            SELECT
                (ts / 60) * 60                                          AS t,
                ROUND(AVG(solar_w))                                     AS solar_w,
                ROUND(AVG(home_w))                                      AS home_w,
                ROUND(AVG(CASE WHEN battery_charging = 1
                               THEN -CAST(battery_w AS REAL)
                               ELSE  CAST(battery_w AS REAL) END))     AS battery_w,
                ROUND(AVG(CASE WHEN grid_importing = 1
                               THEN -CAST(grid_w AS REAL)
                               ELSE  CAST(grid_w AS REAL) END))        AS grid_w,
                ROUND(AVG(CASE WHEN soc BETWEEN 0 AND 100 THEN soc END)) AS soc
            FROM snapshots
            WHERE date(ts, 'unixepoch', 'localtime') = ?
            GROUP BY ts / 60
            ORDER BY t
        """, (day,)).fetchall()

    return jsonify({"ok": True, "day": day, "points": [dict(r) for r in rows]})

@app.route("/healthz")
def healthz():
    """Liveness probe for Docker / orchestrators. Returns 200 whenever the web
    app is up, regardless of inverter connectivity — inverter state is reported
    separately by /api/data (which returns 503 when the inverter is unreachable,
    making it unsuitable as a container health check)."""
    return jsonify({"ok": True, "version": APP_VERSION}), 200


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
def add_security_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # CSP: allow inline scripts/styles (single-page app requirement) but block
    # external origins, object embeds, and framing from outside.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    return response


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _apply_pending_import()   # swap in a staged restore before opening the DB
    init_db()

    t = threading.Thread(target=_data_loop, daemon=True)
    t.start()

    # Scheduler block engine — inert until the master switch is enabled.
    threading.Thread(target=_scheduler_loop,    daemon=True).start()
    threading.Thread(target=_sched_drift_loop,  daemon=True).start()

    # Quick-action startup recovery: re-arm the in-memory timer or run housekeeping
    # if a quick action was active when the process last stopped.
    threading.Thread(target=_qa_startup_recovery, daemon=True).start()

    # Auto-tariff rate fetcher — startup check + daily 16:30 / 20:00 cron.
    threading.Thread(target=_rate_fetch_loop, daemon=True).start()

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
