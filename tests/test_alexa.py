"""Alexa skill endpoint for the Echo Show.

The endpoint is reachable from the internet through the Cloudflare Tunnel, so
the verification tests matter most: a request that is not signed by Amazon for
our own skill must never reach the handler (which can start quick actions).

A throwaway CA stands in for Amazon's chain - the real one cannot sign test
requests - by replacing the trust roots and the downloaded chain.
"""
import base64
import datetime as dt
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SKILL_ID  = "amzn1.ask.skill.test-0000"
CERT_URL  = "https://s3.amazonaws.com/echo.api/echo-api-cert-99.pem"


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _make_chain(san="echo-api.amazon.com"):
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = (x509.CertificateBuilder()
          .subject_name(_name("Test Root")).issuer_name(_name("Test Root"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                         critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (x509.CertificateBuilder()
            .subject_name(_name(san)).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=10))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False),
                           critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    return ca, leaf, key


@pytest.fixture(scope="module")
def chain():
    return _make_chain()


@pytest.fixture(scope="module")
def ds():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="alexa-test-"))
    import dashboard_server as mod
    importlib.reload(mod)
    mod.init_db()
    return mod


@pytest.fixture(autouse=True)
def setup(ds, chain, monkeypatch):
    ca, leaf, _ = chain
    monkeypatch.setattr(ds, "ALEXA_SKILL_ID", SKILL_ID)
    monkeypatch.setattr(ds, "ALEXA_ALLOW_CONTROL", False)
    monkeypatch.setattr(ds, "_alexa_roots", [ca])
    monkeypatch.setattr(ds, "_alexa_chain_cache", {CERT_URL: (leaf, [])})
    monkeypatch.setattr(ds, "_battery_capacity_kwh", lambda: 19.0)
    monkeypatch.setattr(ds, "_cached", {
        "soc": 53, "battery_w": 729, "battery_charging": False, "battery_discharging": True,
        "solar_w": 1200, "solar_today": 12.5, "home_w": 703, "grid_w": 3,
        "grid_importing": False, "grid_exporting": True,
        "grid_in_today": 6.0, "grid_out_today": 2.5,
        "import_cost_p": 145.57, "export_income_p": 60.75, "currency_symbol": "£",
    })
    yield


def _payload(rtype="IntentRequest", intent=None, slots=None, screen=True,
             skill=SKILL_ID, ts=None, attrs=None):
    req = {"type": rtype, "requestId": "r1",
           "timestamp": ts or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if intent:
        req["intent"] = {"name": intent, "slots": slots or {}}
    iface = {"Alexa.Presentation.HTML": {"runtime": {"maxVersion": "1.1"}}} if screen else {}
    return {"version": "1.0",
            "session": {"new": False, "application": {"applicationId": skill},
                        "attributes": attrs or {}},
            "context": {"System": {"application": {"applicationId": skill},
                                   "device": {"supportedInterfaces": iface}}},
            "request": req}


def _post(ds, chain, payload, url=CERT_URL, tamper=False):
    _, _, key = chain
    body = json.dumps(payload).encode()
    sig = base64.b64encode(key.sign(body, padding.PKCS1v15(), hashes.SHA256())).decode()
    if tamper:
        body = body.replace(b"53", b"99") if b"53" in body else body + b" "
    client = ds.app.test_client()
    return client.post("/alexa", data=body, content_type="application/json",
                       headers={"SignatureCertChainUrl": url, "Signature-256": sig},
                       base_url="https://acbc.example.com")


def _speech(resp):
    return resp.get_json()["response"]["outputSpeech"]["text"]


# ── Verification ──────────────────────────────────────────────────────────────

def test_genuine_request_is_answered(ds, chain):
    r = _post(ds, chain, _payload(intent="BatteryIntent", screen=False))
    assert r.status_code == 200
    assert _speech(r).startswith("The battery is at 53 percent, about 10.1 kilowatt hours")


def test_disabled_without_skill_id(ds, chain, monkeypatch):
    monkeypatch.setattr(ds, "ALEXA_SKILL_ID", "")
    assert _post(ds, chain, _payload(intent="BatteryIntent")).status_code == 404
    assert ds.app.test_client().get("/echo").status_code == 404


def test_unsigned_request_rejected(ds):
    r = ds.app.test_client().post("/alexa", json=_payload(intent="BatteryIntent"))
    assert r.status_code == 400


def test_tampered_body_rejected(ds, chain):
    assert _post(ds, chain, _payload(intent="BatteryIntent"), tamper=True).status_code == 400


def test_other_skill_rejected(ds, chain):
    r = _post(ds, chain, _payload(intent="BatteryIntent", skill="amzn1.ask.skill.someone-else"))
    assert r.status_code == 400


def test_stale_timestamp_rejected(ds, chain):
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _post(ds, chain, _payload(intent="BatteryIntent", ts=old)).status_code == 400


def test_cert_from_untrusted_ca_rejected(ds, chain, monkeypatch):
    other_ca, _, _ = _make_chain()
    monkeypatch.setattr(ds, "_alexa_roots", [other_ca])
    assert _post(ds, chain, _payload(intent="BatteryIntent")).status_code == 400


def test_cert_for_wrong_host_rejected(ds, chain, monkeypatch):
    ca, leaf, key = _make_chain(san="evil.example.com")
    monkeypatch.setattr(ds, "_alexa_roots", [ca])
    monkeypatch.setattr(ds, "_alexa_chain_cache", {CERT_URL: (leaf, [])})
    assert _post(ds, (ca, leaf, key), _payload(intent="BatteryIntent")).status_code == 400


@pytest.mark.parametrize("url,ok", [
    ("https://s3.amazonaws.com/echo.api/echo-api-cert.pem", True),
    ("https://s3.amazonaws.com:443/echo.api/echo-api-cert.pem", True),
    ("HTTPS://S3.AMAZONAWS.COM/echo.api/../echo.api/echo-api-cert.pem", False),
    ("https://s3.amazonaws.com/EcHo.aPi/echo-api-cert.pem", False),
    ("http://s3.amazonaws.com/echo.api/echo-api-cert.pem", False),
    ("https://notamazon.com/echo.api/echo-api-cert.pem", False),
    ("https://s3.amazonaws.com/invalid.path/echo-api-cert.pem", False),
    ("https://s3.amazonaws.com:563/echo.api/echo-api-cert.pem", False),
])
def test_cert_url_rules(ds, url, ok):
    assert ds._alexa_cert_url_ok(url) is ok


# ── Answers and the screen ────────────────────────────────────────────────────

def test_launch_on_screen_opens_dashboard_and_keeps_session(ds, chain):
    r = _post(ds, chain, _payload(rtype="LaunchRequest")).get_json()
    start = r["response"]["directives"][0]
    assert start["type"] == "Alexa.Presentation.HTML.Start"
    assert start["request"]["uri"] == "https://acbc.example.com/echo?layout=classic"
    assert "shouldEndSession" not in r["response"]
    assert r["sessionAttributes"]["html_open"] is True


def test_show_view_messages_open_page(ds, chain):
    p = _payload(intent="ShowViewIntent", slots={"view": {"name": "view", "value": "History"}},
                 attrs={"html_open": True})
    d = _post(ds, chain, p).get_json()["response"]["directives"][0]
    assert d == {"type": "Alexa.Presentation.HTML.HandleMessage", "message": {"view": "history"}}


def test_show_view_uses_resolved_synonym(ds, chain):
    slot = {"name": "view", "value": "graphs", "resolutions": {"resolutionsPerAuthority": [
        {"status": {"code": "ER_SUCCESS_MATCH"}, "values": [{"value": {"name": "history"}}]}]}}
    p = _payload(intent="ShowViewIntent", slots={"view": slot}, attrs={"html_open": True})
    d = _post(ds, chain, p).get_json()["response"]["directives"][0]
    assert d["message"] == {"view": "history"}


def test_voice_only_device_ends_session(ds, chain):
    r = _post(ds, chain, _payload(intent="SolarIntent", screen=False)).get_json()
    assert r["response"]["shouldEndSession"] is True
    assert "directives" not in r["response"]
    assert "1.2 kilowatts" in r["response"]["outputSpeech"]["text"]


def test_today_includes_costs(ds, chain):
    s = _speech(_post(ds, chain, _payload(intent="TodayIntent")))
    assert "1 pound 46" in s and "61 pence" in s


# ── Calendar periods (UK dates) ───────────────────────────────────────────────

D = dt.date


@pytest.mark.parametrize("today,label,first,last", [
    # Saturday 31 Oct 2026
    (D(2026, 10, 31), "today",      D(2026, 10, 31), D(2026, 10, 31)),
    (D(2026, 10, 31), "yesterday",  D(2026, 10, 30), D(2026, 10, 30)),
    (D(2026, 10, 31), "this week",  D(2026, 10, 26), D(2026, 10, 31)),
    (D(2026, 10, 31), "last week",  D(2026, 10, 19), D(2026, 10, 25)),
    (D(2026, 10, 31), "this month", D(2026, 10, 1),  D(2026, 10, 31)),
    (D(2026, 10, 31), "last month", D(2026, 9, 1),   D(2026, 9, 30)),
    (D(2026, 10, 31), "this year",  D(2026, 1, 1),   D(2026, 10, 31)),
    (D(2026, 10, 31), "last year",  D(2025, 1, 1),   D(2025, 12, 31)),
    # A Monday: the week has only just started
    (D(2026, 11, 2),  "this week",  D(2026, 11, 2),  D(2026, 11, 2)),
    (D(2026, 11, 2),  "last week",  D(2026, 10, 26), D(2026, 11, 1)),
    # Month ends, including a leap February
    (D(2026, 3, 31),  "last month", D(2026, 2, 1),   D(2026, 2, 28)),
    (D(2024, 3, 31),  "last month", D(2024, 2, 1),   D(2024, 2, 29)),
    (D(2026, 3, 1),   "yesterday",  D(2026, 2, 28),  D(2026, 2, 28)),
    # Over the year end
    (D(2027, 1, 1),   "yesterday",  D(2026, 12, 31), D(2026, 12, 31)),
    (D(2027, 1, 1),   "this week",  D(2026, 12, 28), D(2027, 1, 1)),
    (D(2027, 1, 1),   "last week",  D(2026, 12, 21), D(2026, 12, 27)),
    (D(2027, 1, 1),   "last month", D(2026, 12, 1),  D(2026, 12, 31)),
    (D(2027, 1, 1),   "last year",  D(2026, 1, 1),   D(2026, 12, 31)),
])
def test_period_ranges(ds, today, label, first, last):
    p = ds._alexa_period(label, today)
    assert (p["start"], p["end"]) == (first, last)
    assert p["to_date"] == label.startswith(("today", "this"))


def test_period_keys_and_steps_back(ds):
    today = D(2026, 1, 15)
    assert ds._alexa_period("this month", today)["key"] == "2026-01"
    assert ds._alexa_period("last month", today)["key"] == "2025-12"
    assert ds._alexa_period("this week", today)["key"] == "2026-01-12"
    one = ds._alexa_period("this month", today, back=1)
    two = ds._alexa_period("last month", today, back=1)
    assert (one["label"], one["start"]) == ("last month", D(2025, 12, 1))
    assert (two["label"], two["start"], two["end"]) == ("November", D(2025, 11, 1), D(2025, 11, 30))


def _utc(*a):
    return dt.datetime(*a, tzinfo=dt.timezone.utc).timestamp()


@pytest.mark.parametrize("ts,local", [
    (_utc(2026, 3, 29, 0, 59),   dt.datetime(2026, 3, 29, 0, 59)),    # GMT, last minute
    (_utc(2026, 3, 29, 1, 0),    dt.datetime(2026, 3, 29, 2, 0)),     # clocks go forward
    (_utc(2026, 10, 25, 0, 59),  dt.datetime(2026, 10, 25, 1, 59)),   # still BST
    (_utc(2026, 10, 25, 1, 0),   dt.datetime(2026, 10, 25, 1, 0)),    # clocks go back
    (_utc(2026, 6, 30, 23, 30),  dt.datetime(2026, 7, 1, 0, 30)),     # BST moves the month on
    (_utc(2026, 12, 31, 23, 30), dt.datetime(2026, 12, 31, 23, 30)),  # GMT keeps the year
])
def test_uk_time_across_clock_changes(ds, ts, local):
    assert ds._alexa_now(ts) == local


def test_bst_midnight_starts_the_new_month(ds):
    today = ds._alexa_now(_utc(2026, 6, 30, 23, 30)).date()
    assert ds._alexa_period("this month", today)["start"] == D(2026, 7, 1)
    assert ds._alexa_period("last month", today)["end"] == D(2026, 6, 30)


def test_history_rows_reads_a_calendar_range(ds):
    """A day with no data must not shift the period: 'yesterday' is a date,
    not 'the second-latest day with data'."""
    with ds._db() as conn:
        conn.execute("DELETE FROM daily_totals")
        conn.executemany("INSERT INTO daily_totals (day, s, gi, go_, bc, bd, p, last_ts) "
                         "VALUES (?,?,?,?,?,?,?,0)",
                         [("2026-09-28", 5, 1, 0, 0, 0, 0), ("2026-09-30", 7, 2, 1, 0, 0, 0)])
        conn.commit()
    try:
        assert ds._history_rows("day", 1, ("2026-09-29", "2026-09-29")) == []
        row = ds._history_rows("week", 0, ("2026-09-28", "2026-10-04"))[0]
        assert (row["period"], row["solar_kwh"], row["grid_in_kwh"]) == ("2026-09-28", 12, 3)
    finally:
        with ds._db() as conn:
            conn.execute("DELETE FROM daily_totals")
            conn.commit()


@pytest.mark.parametrize("cur,pence,said", [
    ("£", 4210, "42 pounds 10"), ("£", 85, "85 pence"), ("£", 100, "1 pound"),
    ("£", 305, "3 pounds and 5 pence"), ("£", 101, "1 pound and 1 penny"),
    ("£", -250, "2 pounds 50"), ("£", 5230.5, "52 pounds 31"), ("€", 1999, "19 euros 99"),
    ("kr", 1250, "kr12.50"),
])
def test_money_is_spoken(ds, cur, pence, said):
    assert ds._say_money(cur, pence) == said


# ── Costs ─────────────────────────────────────────────────────────────────────

TODAY = dt.datetime(2026, 10, 31, 14, 0)


@pytest.fixture
def fixed_today(ds, monkeypatch):
    monkeypatch.setattr(ds, "_alexa_now", lambda ts=None: TODAY)


def _cost_row(**cost):
    row = {"period": "2026-10", "grid_in_kwh": 210.4, "grid_out_kwh": 95.0, "solar_kwh": 300,
           "home_kwh": 400}
    if cost:
        row["cost"] = {"currency_symbol": "£", **cost}
    return [row]


def _period_slot(spoken, canonical=None, name="period"):
    slot = {"name": name, "value": spoken}
    if canonical:
        slot["resolutions"] = {"resolutionsPerAuthority": [
            {"status": {"code": "ER_SUCCESS_MATCH"}, "values": [{"value": {"name": canonical}}]}]}
    return {name: slot}


def test_cost_breakdown_this_month(ds, chain, monkeypatch, fixed_today):
    asked = []
    monkeypatch.setattr(ds, "_history_rows", lambda p, o, days=None: asked.append((p, days)) or
                        _cost_row(import_cost_p=4210, export_income_p=1130,
                                  standing_charge_p=920, net_cost_p=4000))
    r = _post(ds, chain, _payload(intent="CostIntent",
                                  slots=_period_slot("month to date", "this month"),
                                  attrs={"html_open": True})).get_json()
    s = r["response"]["outputSpeech"]["text"]
    assert asked == [("month", ("2026-10-01", "2026-10-31"))]
    assert s == ("This month so far: import 42 pounds 10, export credit 11 pounds 30, "
                 "standing charge 9 pounds 20, so 40 pounds net. Want the energy figures?")
    # ... the Show jumps to that month's history, and "yes" gives the energy
    assert r["response"]["directives"][0]["message"] == \
        {"view": "history", "period": "month", "offset": 0}
    yes = _post(ds, chain, _payload(intent="AMAZON.YesIntent", attrs=r["sessionAttributes"]))
    assert _speech(yes) == "You imported 210 kilowatt hours and exported 95.0 kilowatt hours."


def test_cost_last_week_is_the_whole_previous_week(ds, chain, monkeypatch, fixed_today):
    asked = []
    monkeypatch.setattr(ds, "_history_rows", lambda p, o, days=None: asked.append((p, days)) or
                        _cost_row(import_cost_p=500, net_cost_p=500))
    s = _speech(_post(ds, chain, _payload(intent="CostIntent",
                                          slots=_period_slot("last week's", "last week"))))
    assert asked == [("week", ("2026-10-19", "2026-10-25"))]
    assert s.startswith("Last week: import 5 pounds, so 5 pounds net.")


def test_cost_defaults_to_today_and_says_so(ds, chain, monkeypatch, fixed_today):
    asked = []
    monkeypatch.setattr(ds, "_history_rows", lambda p, o, days=None: asked.append((p, days)) or
                        _cost_row(import_cost_p=120, total_cost_p=120, net_cost_p=120))
    r = _post(ds, chain, _payload(intent="CostIntent", screen=False)).get_json()
    s = r["response"]["outputSpeech"]["text"]
    assert asked == [("day", ("2026-10-31", "2026-10-31"))]
    assert s == ("I'll assume you mean today. Today so far: import 1 pound 20, so 1 pound 20 net. "
                 "Want this week, this month or this year?")
    assert r["response"]["shouldEndSession"] is False      # waiting for the answer


def test_period_follow_up_repeats_the_last_question(ds, chain, monkeypatch, fixed_today):
    monkeypatch.setattr(ds, "_history_rows", lambda p, o, days=None:
                        _cost_row(import_cost_p=100, export_income_p=2648, net_cost_p=-2548))
    s = _speech(_post(ds, chain, _payload(intent="PeriodOnlyIntent",
                                          slots=_period_slot("last month"),
                                          attrs={"last": "EarningsIntent"})))
    assert s == "Last month, exports earned 26 pounds 48 from 95.0 kilowatt hours."


def test_cost_in_credit(ds, chain, monkeypatch):
    monkeypatch.setattr(ds, "_history_rows", lambda p, o, days=None: _cost_row(
        import_cost_p=100, standing_charge_p=50, total_cost_p=150,
        export_income_p=400, net_cost_p=-250))
    s = _speech(_post(ds, chain, _payload(intent="CostIntent", slots=_period_slot("this week"))))
    assert "so you're 2 pounds 50 in credit." in s


def test_cost_without_tariff_or_data(ds, chain, monkeypatch):
    monkeypatch.setattr(ds, "_history_rows", lambda p, o, days=None: _cost_row())
    s = _speech(_post(ds, chain, _payload(intent="CostIntent", slots=_period_slot("yesterday"))))
    assert "no tariff set up" in s and "210 kilowatt hours" in s

    monkeypatch.setattr(ds, "_history_rows", lambda p, o, days=None: [])
    s = _speech(_post(ds, chain, _payload(intent="CostIntent", slots=_period_slot("last year"))))
    assert s == "I don't have any data for last year."


def test_solar_split(ds, chain, monkeypatch, fixed_today):
    monkeypatch.setattr(ds, "_history_rows", lambda p, o, days=None: [
        {"period": "2026-10-31", "solar_kwh": 12.0, "grid_out_kwh": 4.0,
         "grid_in_kwh": 2.0, "home_kwh": 10.0}])
    s = _speech(_post(ds, chain, _payload(intent="SolarSplitIntent")))
    assert s == ("Today so far, solar made 12.0 kilowatt hours. You used 8.0 of it yourselves, "
                 "67 percent, and exported 4.0. Solar and the battery covered 80 percent of "
                 "what the house used.")


def test_compare_defaults_to_the_period_before(ds, chain, monkeypatch, fixed_today):
    asked = []
    monkeypatch.setattr(ds, "_history_rows", lambda p, o, days=None: asked.append(days) or [
        {"solar_kwh": 9.0 if days[0] == "2026-10-19" else 5.0, "home_kwh": 50.0}])
    s = _speech(_post(ds, chain, _payload(intent="CompareIntent",
                                          slots=_period_slot("last week"))))
    assert asked == [("2026-10-19", "2026-10-25"), ("2026-10-12", "2026-10-18")]
    assert s.startswith("Last week: solar 9.0, house 50.0 kilowatt hours. The week before:")
    assert s.endswith("So last week made more solar.")


def test_history_route_unchanged_by_refactor(ds):
    r = ds.app.test_client().get("/api/history?period=month&offset=0")
    assert r.status_code == 200 and isinstance(r.get_json(), list)


# ── Controls: confirmed, and only without the dashboard password ─────────────

@pytest.fixture
def controls(ds, monkeypatch):
    monkeypatch.setattr(ds, "ALEXA_ALLOW_CONTROL", True)
    monkeypatch.setattr(ds, "REQUIRE_AUTH", False)
    ran = []
    monkeypatch.setattr(ds, "_execute_control",
                        lambda cmd, params: ran.append((cmd, params)) or {"ok": True, "message": ""})
    return ran


def test_quick_charge_refused_unless_allowed(ds, chain, monkeypatch):
    called = []
    monkeypatch.setattr(ds, "_quick_action_apply", lambda *a: called.append(a) or {"ok": True})
    s = _speech(_post(ds, chain, _payload(intent="QuickChargeIntent")))
    assert "switched off" in s and not called

    monkeypatch.setattr(ds, "ALEXA_ALLOW_CONTROL", True)
    monkeypatch.setattr(ds, "QUICK_ACTIONS_ENABLED", True)
    monkeypatch.setattr(ds, "REQUIRE_AUTH", True)
    s = _speech(_post(ds, chain, _payload(intent="QuickChargeIntent")))
    assert "only work in the app" in s and not called

    monkeypatch.setattr(ds, "REQUIRE_AUTH", False)
    r = _post(ds, chain, _payload(intent="QuickChargeIntent")).get_json()
    assert r["response"]["outputSpeech"]["text"] == \
        "Start a quick charge for an hour. Are you sure?" and not called
    s = _speech(_post(ds, chain, _payload(intent="AMAZON.YesIntent",
                                          attrs=r["sessionAttributes"])))
    assert called == [("charge", True)] and s == "Done, quick charge started for an hour."


def test_mode_change_asks_first_then_reads_back(ds, chain, controls):
    slot = _period_slot("storage", name="mode")
    r = _post(ds, chain, _payload(intent="ModeIntent", slots=slot)).get_json()
    assert r["response"]["outputSpeech"]["text"] == \
        "Switch the inverter to storage mode. Are you sure?"
    assert not controls
    s = _speech(_post(ds, chain, _payload(intent="AMAZON.YesIntent",
                                          attrs=r["sessionAttributes"])))
    assert controls == [("set_mode_storage", {})]
    assert s.startswith("Done, the inverter is now in storage mode.")


def test_no_or_another_question_cancels_a_control(ds, chain, controls):
    r = _post(ds, chain, _payload(intent="ModeIntent",
                                  slots=_period_slot("eco", name="mode"))).get_json()
    attrs = r["sessionAttributes"]
    assert _speech(_post(ds, chain, _payload(intent="AMAZON.NoIntent", attrs=attrs))) == \
        "Okay, I'll leave it."
    other = _post(ds, chain, _payload(intent="BatteryIntent", attrs=attrs)).get_json()
    assert "pending" not in other["sessionAttributes"]
    assert not controls


def test_confirmation_times_out(ds, chain, controls):
    attrs = {"pending": {"do": "mode", "value": "eco", "expires": time.time() - 1}}
    s = _speech(_post(ds, chain, _payload(intent="AMAZON.YesIntent", attrs=attrs)))
    assert "timed out" in s and not controls


def test_reserve_is_checked_and_set(ds, chain, controls):
    s = _speech(_post(ds, chain, _payload(intent="ReserveIntent",
                                          slots={"percent": {"name": "percent", "value": "2"}})))
    assert s == "The reserve has to be between 4 and 100 percent."
    r = _post(ds, chain, _payload(intent="ReserveIntent",
                                  slots={"percent": {"name": "percent", "value": "20"}})).get_json()
    s = _speech(_post(ds, chain, _payload(intent="AMAZON.YesIntent",
                                          attrs=r["sessionAttributes"])))
    assert controls == [("set_soc_reserve", {"value": 20})]
    assert s == "Done, the battery reserve is now 20 percent."


def test_hold_charge_needs_the_scheduler(ds, chain, controls, monkeypatch, fixed_today):
    slots = {"until": {"name": "until", "value": "18:00"}}
    monkeypatch.setattr(ds, "SCHEDULER_ENABLED", False)
    assert "switched off" in _speech(_post(ds, chain, _payload(intent="HoldChargeIntent",
                                                                slots=slots)))
    monkeypatch.setattr(ds, "SCHEDULER_ENABLED", True)
    monkeypatch.setattr(ds, "_sched_hold_until", 0.0)
    r = _post(ds, chain, _payload(intent="HoldChargeIntent", slots=slots)).get_json()
    assert r["response"]["outputSpeech"]["text"] == \
        "Hold the battery charge until 6 pm. Are you sure?"
    _post(ds, chain, _payload(intent="AMAZON.YesIntent", attrs=r["sessionAttributes"]))
    assert 3.9 * 3600 < ds._sched_hold_until - time.time() <= 4 * 3600


def test_until_picks_the_next_time_round(ds):
    now = dt.datetime(2026, 10, 31, 20, 0)
    assert ds._alexa_until("06:00", now) == dt.datetime(2026, 11, 1, 6, 0)
    assert ds._alexa_until("21:30", now) == dt.datetime(2026, 10, 31, 21, 30)
    assert ds._alexa_until("EV", now) == dt.datetime(2026, 11, 1, 17, 0)
    assert ds._alexa_until("soon", now) is None


# ── Other questions ───────────────────────────────────────────────────────────

def test_flat_tariff_rate(ds, chain, monkeypatch):
    monkeypatch.setattr(ds, "TARIFF_SOURCE", "manual")
    monkeypatch.setattr(ds, "TARIFF_TOU", [])
    monkeypatch.setattr(ds, "TARIFF_IMPORT_P", 24.5)
    s = _speech(_post(ds, chain, _payload(intent="TariffIntent")))
    assert s == ("Electricity costs 24.5 pence a kilowatt hour right now. "
                 "Your rate is the same all day.")
    assert "rates are flat" in _speech(_post(ds, chain, _payload(intent="CheapestTimeIntent")))


def test_cheapest_time_on_time_of_use(ds, chain, monkeypatch):
    monkeypatch.setattr(ds, "TARIFF_SOURCE", "manual")
    monkeypatch.setattr(ds, "TARIFF_IMPORT_P", 28.0)
    monkeypatch.setattr(ds, "TARIFF_TOU", [
        {"name": "cheap", "start": "00:30", "end": "04:30", "rate_p": 7.5, "export_rate_p": 15}])
    s = _speech(_post(ds, chain, _payload(intent="CheapestTimeIntent")))
    assert s.startswith("The cheapest two hours ") and "7.5 pence" in s


def test_good_time_when_exporting(ds, chain, monkeypatch):
    monkeypatch.setitem(ds._cached, "grid_w", 1500)
    s = _speech(_post(ds, chain, _payload(intent="GoodTimeIntent")))
    assert s == "Yes, you're exporting 1.5 kilowatts of spare solar right now."


def test_forecast_today_and_tomorrow(ds, chain, monkeypatch):
    today = dt.date.today()
    monkeypatch.setattr(ds, "_sf_configured", lambda: True)
    monkeypatch.setattr(ds, "_sf_cached", {
        "watt_hours_day": [{"date": today.isoformat(), "value": 8200},
                           {"date": (today + dt.timedelta(days=1)).isoformat(), "value": 12500}],
        "watt_hours_period": []})
    monkeypatch.setattr(ds, "_alexa_recent_solar", lambda: 10.0)
    s = _speech(_post(ds, chain, _payload(intent="ForecastIntent")))
    assert s == ("Forecast.Solar expects 8.2 kilowatt hours today, and it's mostly done, "
                 "and 12.5 kilowatt hours tomorrow, so tomorrow looks better.")
    s = _speech(_post(ds, chain, _payload(intent="ForecastIntent",
                                          slots=_period_slot("tomorrow", name="day"))))
    assert s.endswith("That's better than the last two weeks' average of 10.0.")


def test_weather(ds, chain, monkeypatch):
    monkeypatch.setattr(ds, "MET_API_KEY", "k")
    monkeypatch.setattr(ds, "MET_GEOHASH", "gcj8ds")
    monkeypatch.setattr(ds, "_weather_cached", {"ok": True, "temp": 11.2, "weather_code": 7,
                                                "wind_dir": "SW", "wind_mph": 9, "humidity": 87})
    s = _speech(_post(ds, chain, _payload(intent="WeatherIntent")))
    assert s == ("At the house it's 11.2 degrees and cloudy, with a 9 mile an hour "
                 "south-westerly wind and 87 percent humidity.")


def test_battery_runtime(ds, chain, monkeypatch, fixed_today):
    monkeypatch.setattr(ds, "_alexa_reserve", lambda: (4, True))
    s = _speech(_post(ds, chain, _payload(intent="BatteryRuntimeIntent")))
    # 19 kWh x (53 - 4)% = 9.3 kWh at 729 W
    assert s.startswith("At around 729 watts, the battery's 9.3 usable kilowatt hours "
                        "will last about 13 hours, until about 2:46 am tomorrow.")
    assert s.endswith("That should see you through the evening.")


def test_battery_health(ds, chain, monkeypatch):
    monkeypatch.setattr(ds, "_battery_modules", lambda: {"ok": True, "modules": [
        {"status_ok": True, "cycles": 412, "cells_mv": [3301, 3290, 3312]}]})
    s = _speech(_post(ds, chain, _payload(intent="BatteryDetailIntent")))
    assert s == ("The battery reports healthy, with no warnings. It's done 412 cycles, "
                 "and the cells are balanced to within 22 millivolts.")


def test_grid_voltage(ds, chain, monkeypatch):
    monkeypatch.setitem(ds._cached, "grid_v", 241.3)
    s = _speech(_post(ds, chain, _payload(intent="InverterStatusIntent",
                                          slots=_period_slot("voltage", name="inverter_part"))))
    assert s == "Grid voltage is 241 volts, which is normal."


def test_plugin_solar_not_set_up(ds, chain, monkeypatch):
    monkeypatch.setattr(ds, "ECOFLOW_ENABLED", False)
    assert _speech(_post(ds, chain, _payload(intent="PlugInSolarIntent"))) == \
        "Plug-in solar isn't set up in the dashboard."


def test_peak_usage_today(ds, chain):
    now = int(time.time())
    with ds._db() as conn:
        conn.executemany("INSERT INTO snapshots (ts, solar_w, home_w) VALUES (?,?,?)",
                         [(now - 120, 0, 900), (now - 60, 0, 7200)])
        conn.commit()
    try:
        s = _speech(_post(ds, chain, _payload(intent="RecordsIntent")))
        assert s.startswith("The highest house usage today was 7.2 kilowatts, at")
    finally:
        with ds._db() as conn:
            conn.execute("DELETE FROM snapshots")
            conn.execute("DELETE FROM daily_totals")
            conn.commit()


def test_scheduler_off(ds, chain, monkeypatch):
    monkeypatch.setattr(ds, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(ds, "_sched_load_rules", lambda: [])
    assert _speech(_post(ds, chain, _payload(intent="SchedulerIntent"))) == \
        "The scheduler is switched off."


def test_connection_drops_today(ds, chain):
    with ds._db() as conn:
        conn.execute("INSERT INTO event_log (ts, kind, message) VALUES (?,?,?)",
                     (int(time.time()) - 30, "fault", "Lost connection to inverter: timed out"))
        conn.commit()
    try:
        s = _speech(_post(ds, chain, _payload(intent="SystemHealthIntent",
                                              slots=_period_slot("connection", name="system_part"))))
        assert s.startswith("The connection to the inverter dropped 1 time today")
    finally:
        with ds._db() as conn:
            conn.execute("DELETE FROM event_log")
            conn.commit()


def test_help_and_fallback_give_examples(ds, chain):
    assert "what did this month cost" in _speech(_post(ds, chain,
                                                       _payload(intent="AMAZON.HelpIntent")))
    assert "Try asking:" in _speech(_post(ds, chain, _payload(intent="AMAZON.FallbackIntent")))


def test_show_forecast_and_schedule(ds, chain, monkeypatch):
    p = _payload(intent="ShowViewIntent", slots=_period_slot("forecast", name="view"),
                 attrs={"html_open": True})
    d = _post(ds, chain, p).get_json()["response"]["directives"][0]
    assert d["message"] == {"view": "live", "modal": "forecast"}
    monkeypatch.setattr(ds, "REQUIRE_AUTH", True)
    p = _payload(intent="ShowViewIntent", slots=_period_slot("schedule", name="view"),
                 attrs={"html_open": True})
    r = _post(ds, chain, p).get_json()["response"]
    assert "password" in r["outputSpeech"]["text"] and "directives" not in r


def test_echo_page_loads_alexa_sdk(ds):
    r = ds.app.test_client().get("/echo")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "alexa-html.js" in html and html.rstrip().endswith("</html>")
    assert "openSolarForecast()" in html and "openBattModal()" in html
    assert ds.ALEXA_HTML_CDN in r.headers["Content-Security-Policy"]
    # ... and only there
    assert ds.ALEXA_HTML_CDN not in ds.app.test_client().get("/").headers["Content-Security-Policy"]


def test_echo_page_works_without_browser_storage(ds):
    """The Echo's web view has localStorage switched off (null); the dashboard
    stopped at its first getItem until the page brought a stand-in, which has
    to be in place before the dashboard's own script runs."""
    html = ds.app.test_client().get("/echo").get_data(as_text=True)
    shim = html.find("Object.defineProperty(window,'localStorage'")
    assert 0 < shim < html.find("localStorage.getItem('slotVisible')")


def test_echo_report_is_rate_limited(ds, monkeypatch):
    monkeypatch.setattr(ds, "_echo_reports", [])
    logged = []
    monkeypatch.setattr(ds.log, "info", lambda *a: logged.append(a))
    client = ds.app.test_client()
    for _ in range(25):
        assert client.get("/echo/report?d=%7B%7D").status_code == 204
    assert len([a for a in logged if "Echo page report" in a[0]]) == 20

    monkeypatch.setattr(ds, "ALEXA_SKILL_ID", "")
    assert client.get("/echo/report?d=x").status_code == 404


def test_every_intent_in_the_model_is_answered(ds, chain, monkeypatch):
    """The model (alexa/build_model.py) and the handler must stay in step: an
    intent the code doesn't know falls through to 'Sorry, I didn't get that'."""
    model = json.loads((Path(__file__).resolve().parent.parent / "alexa" /
                        "interaction-model.en-GB.json").read_text(encoding="utf-8"))
    lm = model["interactionModel"]["languageModel"]
    monkeypatch.setattr(ds, "_alexa_control_state", lambda max_age=60: None)
    monkeypatch.setattr(ds, "_battery_modules", lambda: {"ok": False})
    monkeypatch.setattr(ds, "_year_review_data", lambda y: {"carbon": {}, "projection": {}})
    for i in lm["intents"]:
        if i["name"] in ("AMAZON.FallbackIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent",
                         "AMAZON.NavigateHomeIntent"):
            continue
        s = _speech(_post(ds, chain, _payload(intent=i["name"])))
        assert not s.startswith("Sorry, I didn't get that"), i["name"]
    # Slot values the code matches on are the model's canonical values
    values = {t["name"]: {v["name"]["value"] for v in t["values"]} for t in lm["types"]}
    assert values["COST_PERIOD"] == set(ds._ALEXA_PERIOD_BASE)
    assert values["VIEW_NAME"] <= set(ds._ALEXA_VIEWS)
    assert values["RECORD_KIND"] == set(ds._ALEXA_RECORDS)


def test_show_just_opens_and_speaker_gets_the_summary(ds, chain, monkeypatch):
    monkeypatch.setattr(ds, "TARIFF_SOURCE", "manual")
    monkeypatch.setattr(ds, "TARIFF_TOU", [])
    monkeypatch.setattr(ds, "TARIFF_IMPORT_P", 24.5)
    monkeypatch.setattr(ds, "TARIFF_STANDING_P", 55.5)
    r = _post(ds, chain, _payload(rtype="LaunchRequest")).get_json()
    assert "outputSpeech" not in r["response"]           # the Show says nothing
    assert r["response"]["directives"][0]["type"] == "Alexa.Presentation.HTML.Start"

    s = _speech(_post(ds, chain, _payload(rtype="LaunchRequest", screen=False)))
    # live cache: 145.57p import, 60.75p export; plus today's standing charge
    assert s.endswith("the house is using 703 watts. Today so far: import 1 pound 46, "
                      "standing charge 56 pence, export credit 61 pence, so 1 pound 40 net.")
    assert "right now" not in s


@pytest.mark.parametrize("width,height,layout", [
    (1920, 1080, "tiled"),     # Echo Show 15
    (1280, 800, "classic"),    # Echo Show 8 / 9 / 10: tiled is squashed
    (960, 480, "classic"),     # Echo Show 5
    (None, None, "classic"),   # no viewport reported
])
def test_echo_layout_follows_screen_size(ds, chain, width, height, layout):
    p = _payload(rtype="LaunchRequest")
    if width:
        p["context"]["Viewport"] = {"pixelWidth": width, "pixelHeight": height,
                                    "shape": "RECTANGLE"}
    start = _post(ds, chain, p).get_json()["response"]["directives"][0]
    assert start["request"]["uri"].endswith(f"/echo?layout={layout}")
