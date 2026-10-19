from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from minisentry.client import Client
from minisentry.integrations import excepthook
from minisentry.integrations.asgi import MiniSentryMiddleware
from minisentry.integrations.logging import MiniSentryHandler
from minisentry.scrubber import FILTERED

from .conftest import FakeTransport


@pytest.fixture
def log_handler(global_client: Client) -> Iterator[logging.Logger]:
    logger = logging.getLogger("tests.app")
    logger.setLevel(logging.INFO)
    handler = MiniSentryHandler()
    logger.addHandler(handler)
    yield logger
    logger.removeHandler(handler)


def test_logging_error_becomes_event(log_handler: logging.Logger, transport: FakeTransport) -> None:
    log_handler.error("payment provider timed out after %ss", 30)
    [event] = transport.events
    assert event["message"] == "payment provider timed out after 30s"
    assert event["extra"]["logger"] == "tests.app"


def test_logging_exception_captures_traceback(
    log_handler: logging.Logger, transport: FakeTransport
) -> None:
    try:
        int("abc")
    except ValueError:
        log_handler.exception("could not parse")
    assert transport.events[0]["exception"]["values"][0]["type"] == "ValueError"


def test_logging_info_becomes_breadcrumb(
    log_handler: logging.Logger, global_client: Client, transport: FakeTransport
) -> None:
    log_handler.info("cart loaded")
    assert transport.events == []
    assert global_client.breadcrumbs.snapshot()[-1]["message"] == "cart loaded"


def test_excepthook_reports_and_calls_previous_hook(
    global_client: Client, transport: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: called.append("previous"))
    monkeypatch.setattr(threading, "excepthook", lambda args: called.append("thread-previous"))
    monkeypatch.setattr(excepthook, "_installed", False)
    excepthook.install()

    try:
        raise RuntimeError("crash")
    except RuntimeError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)

    thread = threading.Thread(target=lambda: 1 / 0)
    thread.start()
    thread.join()

    assert called == ["previous", "thread-previous"]
    assert [e["level"] for e in transport.events] == ["fatal", "fatal"]
    assert [e["exception"]["values"][-1]["type"] for e in transport.events] == [
        "RuntimeError",
        "ZeroDivisionError",
    ]


def test_excepthook_ignores_keyboard_interrupt(
    global_client: Client, transport: FakeTransport
) -> None:
    excepthook._report(KeyboardInterrupt())
    assert transport.events == []


def test_asgi_middleware_reports_request_errors(client: Client, transport: FakeTransport) -> None:
    app = FastAPI()
    app.add_middleware(MiniSentryMiddleware, client=client)

    @app.get("/orders/{order_id}")
    def get_order(order_id: int) -> dict[str, int]:
        raise LookupError(f"order {order_id} not found")

    @app.get("/ok")
    def ok() -> dict[str, str]:
        return {"status": "ok"}

    http = TestClient(app, raise_server_exceptions=False)
    assert http.get("/ok").status_code == 200
    response = http.get("/orders/7?debug=1", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 500

    [event] = transport.events
    assert event["exception"]["values"][-1]["value"] == "order 7 not found"
    assert event["request"]["method"] == "GET"
    assert event["request"]["url"].endswith("/orders/7")
    assert event["request"]["query_string"] == "debug=1"
    assert event["request"]["headers"]["authorization"] == FILTERED
    assert event["tags"]["transaction"] == "/orders/7"
    assert [c["category"] for c in event["breadcrumbs"]] == ["http.request", "http.request"]
