"""Syslog forwarding (#58).

Requested by a tester who wanted BOTH the application log and the event log:
"issues are easier to spot and pin down". The two streams therefore carry
different syslog idents so they can be filtered apart at the collector.

Several of these bind a real UDP socket on localhost and assert on the bytes
that actually arrive, rather than trusting that a handler was constructed.
"""
import importlib
import logging
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("ACBC_DATA_DIR", tempfile.mkdtemp(prefix="syslog-test-"))
    import dashboard_server as ds
    importlib.reload(ds)
    ds.init_db()          # _log_event writes to event_log, so the schema must exist
    return ds


@pytest.fixture
def collector():
    """A throwaway UDP syslog listener on a free localhost port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    received = []

    def pump():
        while True:
            try:
                data, _ = sock.recvfrom(8192)
            except (socket.timeout, OSError):
                return
            received.append(data.decode("utf-8", "replace"))

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    yield sock.getsockname()[1], received
    sock.close()


def _enable(srv, port, streams="both", host="127.0.0.1", protocol="udp"):
    srv.SYSLOG_ENABLED  = True
    srv.SYSLOG_HOST     = host
    srv.SYSLOG_PORT     = port
    srv.SYSLOG_PROTOCOL = protocol
    srv.SYSLOG_STREAMS  = streams
    return srv._configure_syslog()


@pytest.fixture(autouse=True)
def _cleanup(srv):
    yield
    srv.SYSLOG_ENABLED = False
    srv._configure_syslog()


# ── Configuration ────────────────────────────────────────────────────────────

def test_disabled_by_default_attaches_nothing(srv):
    srv.SYSLOG_ENABLED = False
    assert srv._configure_syslog() == "disabled"
    assert srv._syslog_handlers == {}


def test_enabled_without_host_is_an_error_not_a_crash(srv):
    srv.SYSLOG_ENABLED = True
    srv.SYSLOG_HOST = ""
    status = srv._configure_syslog()
    assert status.startswith("error")
    assert srv._syslog_handlers == {}


def test_unreachable_tcp_collector_does_not_take_the_app_down(srv):
    """A TCP handler fails at construction if nothing is listening. That must
    surface as a status string, never an exception out of _configure_syslog."""
    srv.SYSLOG_ENABLED  = True
    srv.SYSLOG_HOST     = "127.0.0.1"
    srv.SYSLOG_PORT     = 9        # discard port, nothing listening on TCP
    srv.SYSLOG_PROTOCOL = "tcp"
    srv.SYSLOG_STREAMS  = "both"
    status = srv._configure_syslog()
    assert status.startswith("error")
    assert srv._syslog_handlers == {}, "partial setup must be torn down, not left half-attached"


def test_reconfiguring_does_not_stack_handlers(srv, collector):
    """Saving settings repeatedly must not end up sending each line many times."""
    port, _ = collector
    for _ in range(4):
        _enable(srv, port)
    assert len(srv._syslog_handlers) == 2
    assert len(srv.event_log.handlers) == 1


def test_stream_selection(srv, collector):
    port, _ = collector
    _enable(srv, port, streams="app")
    assert set(srv._syslog_handlers) == {"app"}
    _enable(srv, port, streams="events")
    assert set(srv._syslog_handlers) == {"events"}
    _enable(srv, port, streams="both")
    assert set(srv._syslog_handlers) == {"app", "events"}


# ── Delivery ─────────────────────────────────────────────────────────────────

def _wait_for(received, predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(predicate(m) for m in received):
            return True
        time.sleep(0.05)
    return False


def test_application_log_reaches_the_collector(srv, collector):
    port, received = collector
    _enable(srv, port, streams="app")
    srv.log.warning("kettle boiled at 3kW")
    assert _wait_for(received, lambda m: "kettle boiled" in m)


def test_event_log_reaches_the_collector(srv, collector):
    port, received = collector
    _enable(srv, port, streams="events")
    srv._log_event("info", "scheduler applied a charge rule")
    assert _wait_for(received, lambda m: "scheduler applied" in m)


def test_streams_carry_different_idents(srv, collector):
    """The whole point of two streams: they must be separable at the collector."""
    port, received = collector
    _enable(srv, port, streams="both")
    srv.log.warning("an application line")
    srv._log_event("info", "an event line")

    assert _wait_for(received, lambda m: "an application line" in m)
    assert _wait_for(received, lambda m: "an event line" in m)
    app_msg   = next(m for m in received if "an application line" in m)
    event_msg = next(m for m in received if "an event line" in m)
    assert "givenergy-events:" in event_msg
    assert "givenergy-events:" not in app_msg
    assert "givenergy:" in app_msg


def test_faults_are_sent_at_error_severity(srv, collector):
    """So a collector can alert on real problems without alerting on every
    status change."""
    port, received = collector
    _enable(srv, port, streams="events")
    srv._log_event("fault", "lost connection to inverter")
    assert _wait_for(received, lambda m: "lost connection" in m)
    msg = next(m for m in received if "lost connection" in m)
    assert "ERROR" in msg


def test_events_are_not_duplicated_into_the_app_stream(srv, collector):
    """event_log must not propagate: the root logger also has the app handler,
    so propagation would send every event twice when both streams are on."""
    port, received = collector
    _enable(srv, port, streams="both")
    srv._log_event("info", "a uniquely identifiable event 12345")
    assert _wait_for(received, lambda m: "12345" in m)
    time.sleep(0.3)
    assert sum(1 for m in received if "12345" in m) == 1


# ── Robustness ───────────────────────────────────────────────────────────────

def test_event_is_still_stored_when_the_handler_explodes(srv, collector, monkeypatch):
    """The database write is what matters. A logging handler must never be able
    to lose an event that is already safely recorded."""
    port, _ = collector
    _enable(srv, port, streams="events")

    def boom(*a, **k):
        raise RuntimeError("collector on fire")
    monkeypatch.setattr(srv.event_log, "log", boom)

    srv._log_event("info", "must survive a broken handler")
    with srv._db() as conn:
        row = conn.execute(
            "SELECT message FROM event_log ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
    assert row["message"] == "must survive a broken handler"


def test_teardown_detaches_cleanly(srv, collector):
    port, _ = collector
    _enable(srv, port, streams="both")
    root_before = len(logging.getLogger().handlers)
    srv.SYSLOG_ENABLED = False
    srv._configure_syslog()
    assert srv._syslog_handlers == {}
    assert srv.event_log.handlers == []
    assert len(logging.getLogger().handlers) == root_before - 1
