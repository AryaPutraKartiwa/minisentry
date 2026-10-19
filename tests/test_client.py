from __future__ import annotations

from typing import Any

import pytest

from minisentry.client import Client
from minisentry.scrubber import FILTERED
from minisentry.stacktrace import exception_chain, exception_values

from .conftest import DSN, FakeTransport


def _raise_value_error() -> None:
    raise ValueError("bad value 42")


def test_capture_exception_builds_a_full_event(client: Client, transport: FakeTransport) -> None:
    client.add_breadcrumb("user clicked checkout", category="ui")
    try:
        _raise_value_error()
    except ValueError as exc:
        event_id = client.capture_exception(exc, tags={"feature": "checkout"})

    assert event_id is not None
    [event] = transport.events
    assert event["event_id"] == event_id
    assert event["level"] == "error"
    assert event["release"] == "test@1.0.0"
    assert event["tags"] == {"feature": "checkout"}
    assert event["breadcrumbs"][0]["message"] == "user clicked checkout"

    [value] = event["exception"]["values"]
    assert value["type"] == "ValueError"
    assert value["value"] == "bad value 42"
    innermost = value["stacktrace"]["frames"][-1]
    assert innermost["function"] == "_raise_value_error"
    assert innermost["in_app"] is True
    assert "raise ValueError" in innermost["context_line"]
    assert innermost["pre_context"], "should include source lines before the error"


def test_capture_exception_uses_current_exception(client: Client, transport: FakeTransport) -> None:
    try:
        {}["missing"]
    except KeyError:
        client.capture_exception()
    assert transport.events[0]["exception"]["values"][0]["type"] == "KeyError"


def test_capture_exception_outside_except_block_is_noop(client: Client) -> None:
    assert client.capture_exception() is None


def test_capture_message(client: Client, transport: FakeTransport) -> None:
    client.capture_message("disk almost full", level="warning")
    assert transport.events[0]["message"] == "disk almost full"
    assert transport.events[0]["level"] == "warning"


def test_sensitive_data_is_scrubbed(client: Client, transport: FakeTransport) -> None:
    client.capture_message(
        "login failed",
        extra={"password": "hunter2", "email": "a@b.c"},
        request={"headers": {"Cookie": "session=abc"}},
    )
    event = transport.events[0]
    assert event["extra"] == {"password": FILTERED, "email": "a@b.c"}
    assert event["request"]["headers"]["Cookie"] == FILTERED


def test_before_send_can_modify_or_drop(transport: FakeTransport) -> None:
    def before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
        if "ignore me" in event.get("message", ""):
            return None
        event["tags"]["checked"] = "yes"
        return event

    client = Client(DSN, transport=transport, before_send=before_send)
    assert client.capture_message("ignore me please") is None
    client.capture_message("keep me")
    assert [e["message"] for e in transport.events] == ["keep me"]
    assert transport.events[0]["tags"]["checked"] == "yes"


def test_before_send_crash_drops_event_but_not_app(transport: FakeTransport) -> None:
    def broken(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("bug in hook")

    client = Client(DSN, transport=transport, before_send=broken)
    assert client.capture_message("hello") is None
    assert transport.events == []


def test_sample_rate_zero_sends_nothing(transport: FakeTransport) -> None:
    client = Client(DSN, transport=transport, sample_rate=0.0)
    for _ in range(20):
        client.capture_message("x")
    assert transport.events == []


def test_invalid_sample_rate() -> None:
    with pytest.raises(ValueError):
        Client(DSN, transport=FakeTransport(), sample_rate=1.5)


def test_custom_fingerprint_is_sent(client: Client, transport: FakeTransport) -> None:
    client.capture_message("payment failed", fingerprint=["payments", 503])
    assert transport.events[0]["fingerprint"] == ["payments", "503"]


def test_returns_none_when_transport_rejects(client: Client, transport: FakeTransport) -> None:
    transport.accept = False
    assert client.capture_message("dropped") is None


# ---- exception chains ---------------------------------------------------


def test_chained_exceptions_are_captured_oldest_first() -> None:
    try:
        try:
            raise ConnectionError("db down")
        except ConnectionError as inner:
            raise RuntimeError("could not load user") from inner
    except RuntimeError as outer:
        values = exception_values(outer)
    assert [v["type"] for v in values] == ["ConnectionError", "RuntimeError"]


def test_suppressed_context_is_not_captured() -> None:
    try:
        try:
            raise KeyError("a")
        except KeyError:
            raise ValueError("b") from None
    except ValueError as exc:
        assert [type(e).__name__ for e in exception_chain(exc)] == ["ValueError"]


def test_cyclic_chain_terminates() -> None:
    a, b = ValueError("a"), ValueError("b")
    a.__cause__, b.__cause__ = b, a
    assert len(exception_chain(a)) == 2
