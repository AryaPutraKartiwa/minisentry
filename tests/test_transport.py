from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from minisentry.dsn import Dsn
from minisentry.ratelimit import TokenBucket
from minisentry.transport import HttpTransport, SendResult, parse_retry_after

DSN = Dsn.parse("http://test-key@localhost:9/1")


class ScriptedSender:
    """Returns pre-programmed responses, then 200 forever."""

    def __init__(self, *statuses: int | SendResult) -> None:
        self.script = [s if isinstance(s, SendResult) else SendResult(s) for s in statuses]
        self.batches: list[list[dict[str, Any]]] = []
        self.lock = threading.Lock()

    def __call__(self, batch: list[dict[str, Any]]) -> SendResult:
        with self.lock:
            self.batches.append(batch)
            return self.script.pop(0) if self.script else SendResult(200)


def make(sender: ScriptedSender, **kwargs: Any) -> HttpTransport:
    options: dict[str, Any] = {"flush_interval": 0.02, "base_backoff": 0.01, "max_backoff": 0.05}
    options.update(kwargs)
    return HttpTransport(DSN, sender=sender, **options)


def events(n: int) -> list[dict[str, Any]]:
    return [{"event_id": f"{i:032x}"} for i in range(n)]


def test_delivers_events_in_batches() -> None:
    sender = ScriptedSender()
    transport = make(sender, batch_size=10)
    for e in events(25):
        assert transport.submit(e)
    assert transport.flush(timeout=2)
    assert transport.stats.sent == 25
    assert all(len(b) <= 10 for b in sender.batches)
    transport.close()


def test_retries_server_errors_with_backoff() -> None:
    sender = ScriptedSender(500, 503, 0)  # two 5xx, one network error, then success
    transport = make(sender)
    transport.submit(events(1)[0])
    assert transport.flush(timeout=2)
    assert transport.stats.sent == 1
    assert transport.stats.retried == 3
    assert len(sender.batches) == 4
    transport.close()


def test_gives_up_after_max_retries() -> None:
    sender = ScriptedSender(*([500] * 10))
    transport = make(sender, max_retries=2)
    transport.submit(events(1)[0])
    assert transport.flush(timeout=2)
    assert transport.stats.dropped_retries_exhausted == 1
    assert len(sender.batches) == 3  # first try + 2 retries
    transport.close()


def test_client_errors_are_not_retried() -> None:
    sender = ScriptedSender(400)
    transport = make(sender)
    transport.submit(events(1)[0])
    assert transport.flush(timeout=2)
    assert transport.stats.dropped_rejected == 1
    assert len(sender.batches) == 1
    transport.close()


def test_429_pauses_sending_for_retry_after() -> None:
    sender = ScriptedSender(SendResult(429, retry_after=0.2))
    transport = make(sender)
    transport.submit(events(1)[0])
    assert not transport.flush(timeout=0.1), "must still be waiting for Retry-After"
    assert transport.flush(timeout=2)
    assert transport.stats.sent == 1
    transport.close()


def test_drops_when_queue_is_full() -> None:
    gate = threading.Event()

    def slow_sender(batch: list[dict[str, Any]]) -> SendResult:
        gate.wait(2)
        return SendResult(200)

    transport = HttpTransport(DSN, sender=slow_sender, max_queue_size=2, batch_size=1)
    results = [transport.submit(e) for e in events(10)]
    assert results.count(False) >= 7  # 1 in flight + 2 queued, the rest dropped
    assert transport.stats.dropped_queue_full == results.count(False)
    gate.set()
    assert transport.flush(timeout=2)
    transport.close()


def test_client_side_rate_limit() -> None:
    sender = ScriptedSender()
    transport = make(sender, rate_limiter=TokenBucket(rate=0.001, capacity=3))
    accepted = [transport.submit(e) for e in events(5)]
    assert accepted == [True, True, True, False, False]
    assert transport.stats.dropped_rate_limited == 2
    transport.close()


def test_submit_after_close_is_rejected() -> None:
    transport = make(ScriptedSender())
    transport.close()
    assert transport.submit(events(1)[0]) is False


@pytest.mark.parametrize(
    ("value", "expected"), [("5", 5.0), ("0.5", 0.5), (None, 60.0), ("soon", 60.0), ("-3", 0.0)]
)
def test_parse_retry_after(value: str | None, expected: float) -> None:
    assert parse_retry_after(value) == expected


# ---- real HTTP ----------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    received: list[dict[str, Any]] = []  # noqa: RUF012 - shared on purpose for the test
    statuses: list[int] = []  # noqa: RUF012

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        type(self).received.append({"headers": dict(self.headers), "body": body})
        status = type(self).statuses.pop(0) if type(self).statuses else 202
        self.send_response(status)
        if status == 429:
            self.send_header("Retry-After", "0.1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def http_server() -> Iterator[HTTPServer]:
    _Handler.received = []
    _Handler.statuses = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def test_http_send_posts_json_with_auth_header(http_server: HTTPServer) -> None:
    _Handler.statuses = [429]
    port = http_server.server_address[1]
    transport = HttpTransport(
        Dsn.parse(f"http://secret-key@127.0.0.1:{port}/7"), flush_interval=0.02, base_backoff=0.01
    )
    transport.submit({"event_id": "a" * 32})
    assert transport.flush(timeout=3)
    assert transport.stats.sent == 1
    assert len(_Handler.received) == 2  # 429, then success
    request = _Handler.received[-1]
    assert request["headers"]["X-Minisentry-Key"] == "secret-key"
    assert request["body"] == {"events": [{"event_id": "a" * 32}]}
    transport.close()
