"""Embedding the dashboard in an iframe (#61).

Requested by a tester running Home Assistant, whose "Webpage" dashboard was
refused because the portal forbids framing outright. Framing stays blocked by
default - it is what stops a hostile page overlaying the inverter controls -
and [server] frame_ancestors relaxes it for exact, listed origins only.

The header tests go through Flask's test client so they assert on the headers
a browser would actually receive, not on the parsed setting alone.
"""
import importlib
import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def ds():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="frame-test-"))
    import dashboard_server as mod
    importlib.reload(mod)
    return mod


# ── Parsing ───────────────────────────────────────────────────────────────────

def test_empty_setting_allows_nothing(ds):
    assert ds._parse_frame_ancestors("") == []
    assert ds._parse_frame_ancestors("   ") == []


def test_single_origin_with_port(ds):
    assert ds._parse_frame_ancestors("http://homeassistant.local:8123") == \
        ["http://homeassistant.local:8123"]


def test_space_and_comma_separated_list(ds):
    got = ds._parse_frame_ancestors("http://ha.local:8123, https://ha.example.com  http://192.168.1.20:8123")
    assert got == ["http://ha.local:8123", "https://ha.example.com", "http://192.168.1.20:8123"]


def test_trailing_slash_and_case_are_normalised(ds):
    assert ds._parse_frame_ancestors("HTTP://HA.Local:8123/") == ["http://ha.local:8123"]


def test_duplicates_collapse(ds):
    assert ds._parse_frame_ancestors("http://ha.local http://HA.local/") == ["http://ha.local"]


@pytest.mark.parametrize("bad", [
    "*",                                   # would allow every site
    "https://*.example.com",               # wildcard host
    "http://ha.local:8123/lovelace",       # a path is not an origin
    "ha.local:8123",                       # no scheme
    "ftp://ha.local",                      # not http/https
    "javascript:alert(1)",
    "'self'",                              # CSP keyword - injected verbatim it would change meaning
    "http://ha.local; script-src *",       # attempt to smuggle a directive
])
def test_invalid_entries_are_skipped_and_logged(ds, bad, caplog):
    with caplog.at_level(logging.WARNING, logger="dashboard"):
        assert ds._parse_frame_ancestors(bad) == []
    assert "frame_ancestors: ignoring" in caplog.text


def test_valid_entries_survive_alongside_invalid_ones(ds):
    assert ds._parse_frame_ancestors("* http://ha.local:8123 https://*.x.com") == ["http://ha.local:8123"]


# ── Headers actually sent ─────────────────────────────────────────────────────

def _headers(ds, monkeypatch, origins):
    monkeypatch.setattr(ds, "FRAME_ANCESTORS", origins)
    return ds.app.test_client().get("/healthz").headers


def test_default_blocks_all_framing(ds, monkeypatch):
    h = _headers(ds, monkeypatch, [])
    assert h.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]


def test_configured_origins_allow_only_those(ds, monkeypatch):
    h = _headers(ds, monkeypatch, ["http://homeassistant.local:8123"])
    csp = h["Content-Security-Policy"]
    assert "frame-ancestors 'self' http://homeassistant.local:8123" in csp
    assert "'none'" not in csp.split("frame-ancestors", 1)[1]
    # DENY would override the allow-list in some browsers, so it must be absent
    assert "X-Frame-Options" not in h


def test_rest_of_policy_unchanged_when_embedding(ds, monkeypatch):
    csp = _headers(ds, monkeypatch, ["http://ha.local"])["Content-Security-Policy"]
    for directive in ("default-src 'self'", "object-src 'none'", "base-uri 'self'", "connect-src 'self'"):
        assert directive in csp
