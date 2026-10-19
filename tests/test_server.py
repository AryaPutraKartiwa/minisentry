"""API + worker + storage, end to end (SQLite and the in-memory queue, no Docker needed)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from minisentry.client import Client
from minisentry.dsn import Dsn
from minisentry.transport import HttpTransport, SendResult
from minisentry_server.app import create_app
from minisentry_server.config import Settings, parse_project_keys
from minisentry_server.queue import InMemoryQueue
from minisentry_server.storage import Storage
from minisentry_server.worker import Worker

KEY = {"X-Minisentry-Key": "secret"}


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def queue() -> InMemoryQueue:
    return InMemoryQueue()


@pytest.fixture
def settings() -> Settings:
    return Settings(project_keys={1: "secret", 2: "other"}, rate_limit_burst=1000)


@pytest.fixture
def api(settings: Settings, queue: InMemoryQueue, storage: Storage) -> Iterator[TestClient]:
    with TestClient(create_app(settings, queue, storage)) as client:
        yield client


@pytest.fixture
def worker(queue: InMemoryQueue, storage: Storage) -> Worker:
    return Worker(queue, storage, block_ms=0)


def make_event(
    function: str = "checkout",
    value: str = "boom",
    ts: datetime | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id or uuid.uuid4().hex,
        "timestamp": (ts or datetime.now(timezone.utc)).isoformat(),
        "level": "error",
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "value": value,
                    "stacktrace": {
                        "frames": [{"module": "shop.views", "function": function, "in_app": True}]
                    },
                }
            ]
        },
    }


def post(api: TestClient, events: list[dict[str, Any]], project: int = 1) -> Any:
    return api.post(f"/api/{project}/events", json={"events": events}, headers=KEY)


# ---- ingest endpoint ----------------------------------------------------


def test_requires_valid_key(api: TestClient) -> None:
    body = {"events": [make_event()]}
    assert api.post("/api/1/events", json=body).status_code == 401
    assert (
        api.post("/api/1/events", json=body, headers={"X-Minisentry-Key": "x"}).status_code == 401
    )
    assert api.post("/api/2/events", json=body, headers=KEY).status_code == 401  # wrong project
    assert api.post("/api/99/events", json=body, headers=KEY).status_code == 401


def test_accepts_and_enqueues(api: TestClient, queue: InMemoryQueue) -> None:
    response = post(api, [make_event(), make_event()])
    assert response.status_code == 202
    assert response.json() == {"accepted": 2, "rejected": []}
    assert len(queue) == 2


def test_partial_batch_rejects_only_bad_events(api: TestClient, queue: InMemoryQueue) -> None:
    bad = make_event()
    bad["event_id"] = "not-hex"
    response = post(api, [make_event(), bad])
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] == 1
    assert body["rejected"][0]["index"] == 1
    assert len(queue) == 1


def test_all_invalid_is_400(api: TestClient) -> None:
    assert post(api, [{"nope": True}]).status_code == 400


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"events": []}', b'{"events": "x"}'])
def test_malformed_bodies(api: TestClient, body: bytes) -> None:
    response = api.post("/api/1/events", content=body, headers=KEY)
    assert response.status_code == 400


def test_too_many_events(api: TestClient) -> None:
    assert post(api, [make_event() for _ in range(101)]).status_code == 413


def test_rate_limit_returns_429_with_retry_after(queue: InMemoryQueue, storage: Storage) -> None:
    settings = Settings(project_keys={1: "secret"}, rate_limit_per_second=0.001, rate_limit_burst=3)
    with TestClient(create_app(settings, queue, storage)) as api:
        assert post(api, [make_event() for _ in range(3)]).status_code == 202
        response = post(api, [make_event()])
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "1"


def test_queue_failure_returns_503(settings: Settings, storage: Storage) -> None:
    class BrokenQueue(InMemoryQueue):
        def publish(self, payloads: list[str]) -> None:
            raise ConnectionError("redis down")

    with TestClient(create_app(settings, BrokenQueue(), storage)) as api:
        assert post(api, [make_event()]).status_code == 503


# ---- worker + storage ---------------------------------------------------


def test_events_are_grouped_into_issues(api: TestClient, worker: Worker) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    post(api, [make_event(value=f"order {i}", ts=t0 + timedelta(minutes=i)) for i in range(5)])
    post(api, [make_event(function="refund")])
    while worker.process_once():
        pass

    issues = api.get("/api/1/issues", headers=KEY).json()
    assert len(issues) == 2
    checkout = next(i for i in issues if i["culprit"] == "shop.views in checkout")
    assert checkout["times_seen"] == 5
    assert checkout["first_seen"].startswith("2026-01-01T00:00")
    assert checkout["last_seen"].startswith("2026-01-01T00:04")
    assert checkout["title"] == "ValueError: order 4"

    detail = api.get(f"/api/1/issues/{checkout['id']}", headers=KEY).json()
    assert detail["times_seen"] == 5
    events = api.get(f"/api/1/issues/{checkout['id']}/events?limit=2", headers=KEY).json()
    assert len(events) == 2
    assert events[0]["payload"]["exception"]["values"][0]["value"] == "order 4"


def test_title_follows_newest_event(api: TestClient, worker: Worker) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Same timestamp for every event (coarse clock): the last one to arrive wins.
    post(api, [make_event(value=f"order {i}", ts=t0) for i in range(3)])
    worker.process_once()
    # A late batch with an *older* event must not overwrite the title.
    post(api, [make_event(value="old order", ts=t0 - timedelta(hours=1))])
    worker.process_once()

    [issue] = api.get("/api/1/issues", headers=KEY).json()
    assert issue["title"] == "ValueError: order 2"
    assert issue["times_seen"] == 4
    assert issue["first_seen"].startswith("2025-12-31T23:00")


def test_counts_accumulate_across_batches(api: TestClient, worker: Worker) -> None:
    for _ in range(3):
        post(api, [make_event(), make_event()])
        worker.process_once()
    [issue] = api.get("/api/1/issues", headers=KEY).json()
    assert issue["times_seen"] == 6


def test_duplicate_event_ids_are_counted_once(
    api: TestClient, worker: Worker, storage: Storage
) -> None:
    event = make_event()
    post(api, [event, event])  # duplicate inside one batch
    worker.process_once()
    post(api, [event])  # SDK retry of an already-stored event (LRU hit)
    worker.process_once()
    worker.recent_ids = type(worker.recent_ids)(10)  # simulate a worker restart
    post(api, [event])  # now only the DB unique constraint can catch it
    worker.process_once()

    [issue] = api.get("/api/1/issues", headers=KEY).json()
    assert issue["times_seen"] == 1
    assert storage.count_events() == 1
    assert worker.stats.duplicates == 3


def test_projects_are_isolated(api: TestClient, worker: Worker) -> None:
    post(api, [make_event()], project=1)
    api.post(
        "/api/2/events", json={"events": [make_event()]}, headers={"X-Minisentry-Key": "other"}
    )
    worker.process_once()
    assert len(api.get("/api/1/issues", headers=KEY).json()) == 1
    assert api.get("/api/1/issues/999", headers=KEY).status_code == 404


def test_malformed_queue_message_is_dropped_and_acked(queue: InMemoryQueue, worker: Worker) -> None:
    queue.publish(["{not json", json.dumps({"project_id": 1})])
    assert worker.process_once() == 2
    assert worker.stats.invalid == 2
    assert queue.pending_count == 0


def test_failed_storage_leaves_messages_unacked(queue: InMemoryQueue, storage: Storage) -> None:
    class BrokenStorage(Storage):
        def save_events(self, batch: Any) -> int:
            raise ConnectionError("db down")

    broken = BrokenStorage(str(storage.engine.url))
    queue.publish(
        [
            json.dumps(
                {"project_id": 1, "received_at": "2026-01-01T00:00:00Z", "event": make_event()}
            )
        ]
    )
    with pytest.raises(ConnectionError):
        Worker(queue, broken, block_ms=0).process_once()
    assert queue.pending_count == 1, "message must stay pending so it can be retried"


# ---- full pipeline: real SDK -> API -> worker ---------------------------


def test_sdk_to_issue_end_to_end(api: TestClient, worker: Worker) -> None:
    def sender(batch: list[dict[str, Any]]) -> SendResult:
        response = api.post("/api/1/events", json={"events": batch}, headers=KEY)
        return SendResult(response.status_code)

    dsn = "http://secret@testserver/1"
    transport = HttpTransport(Dsn.parse(dsn), sender=sender, flush_interval=0.02)
    client = Client(dsn, transport=transport)

    def load_user(user_id: int) -> None:
        raise KeyError(f"user-{user_id}")

    for user_id in range(4):
        try:
            load_user(user_id)
        except KeyError:
            client.capture_exception()
    assert client.flush(timeout=3)
    client.close()

    while worker.process_once():
        pass
    [issue] = api.get("/api/1/issues", headers=KEY).json()
    assert issue["times_seen"] == 4
    assert issue["culprit"].endswith("in load_user")


def test_parse_project_keys() -> None:
    assert parse_project_keys("1:a, 2:b") == {1: "a", 2: "b"}
    with pytest.raises(ValueError):
        parse_project_keys("x:y")


# ---- dashboard + stats ----------------------------------------------------


def test_dashboard_page_is_served(api: TestClient) -> None:
    response = api.get("/dashboard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "minisentry" in response.text


def test_stats_counts_events_per_minute(api: TestClient, worker: Worker) -> None:
    post(api, [make_event(), make_event(function="refund"), make_event()])
    api.post(  # another project's event must not be counted
        "/api/2/events", json={"events": [make_event()]}, headers={"X-Minisentry-Key": "other"}
    )
    while worker.process_once():
        pass

    assert api.get("/api/1/stats").status_code == 401
    stats = api.get("/api/1/stats?minutes=10", headers=KEY).json()
    assert stats["total_events"] == 3
    assert len(stats["per_minute"]) == 10
    assert sum(p["count"] for p in stats["per_minute"]) == 3
    assert stats["per_minute"][-1]["count"] == 3  # received just now -> last bucket
