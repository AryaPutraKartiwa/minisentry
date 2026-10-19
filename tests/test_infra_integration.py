"""Runs against real Redis and Postgres. Skipped unless the env vars are set:

    docker compose up -d redis postgres
    MINISENTRY_TEST_REDIS_URL=redis://localhost:6379/0 \
    MINISENTRY_TEST_DATABASE_URL=postgresql+psycopg://minisentry:minisentry@localhost/minisentry \
    pytest -m integration
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone

import pytest

from minisentry_server.queue import RedisStreamQueue
from minisentry_server.storage import ProcessedEvent, Storage, events, issues

REDIS_URL = os.environ.get("MINISENTRY_TEST_REDIS_URL")
DATABASE_URL = os.environ.get("MINISENTRY_TEST_DATABASE_URL")

pytestmark = pytest.mark.integration


@pytest.fixture
def redis_queue() -> RedisStreamQueue:
    if not REDIS_URL:
        pytest.skip("MINISENTRY_TEST_REDIS_URL not set")
    q = RedisStreamQueue(REDIS_URL, stream=f"test:{uuid.uuid4().hex}", consumer="c1")
    yield q
    q._redis.delete(q.stream)


@pytest.fixture
def pg_storage() -> Storage:
    if not DATABASE_URL:
        pytest.skip("MINISENTRY_TEST_DATABASE_URL not set")
    storage = Storage(DATABASE_URL)
    with storage.engine.begin() as conn:
        conn.execute(events.delete())
        conn.execute(issues.delete())
    return storage


def test_redis_stream_publish_consume_ack(redis_queue: RedisStreamQueue) -> None:
    redis_queue.publish([json.dumps({"n": i}) for i in range(5)])
    messages = redis_queue.consume(count=10, block_ms=100)
    assert [json.loads(p)["n"] for _, p in messages] == [0, 1, 2, 3, 4]
    assert redis_queue.consume(count=10, block_ms=10) == []  # delivered once per group

    # Not acked yet -> another consumer can reclaim them after they go idle.
    other = RedisStreamQueue(REDIS_URL or "", stream=redis_queue.stream, consumer="c2")
    reclaimed = other.reclaim_stale(min_idle_ms=0, count=10)
    assert len(reclaimed) == 5
    other.ack([mid for mid, _ in reclaimed])
    assert other.reclaim_stale(min_idle_ms=0, count=10) == []


def _event(fingerprint: str) -> ProcessedEvent:
    now = datetime.now(timezone.utc)
    return ProcessedEvent(
        event_id=uuid.uuid4().hex,
        project_id=1,
        fingerprint=fingerprint,
        title="ValueError: x",
        culprit="app in f",
        level="error",
        timestamp=now,
        received_at=now,
        payload={},
    )


def test_postgres_concurrent_upserts_do_not_lose_counts(pg_storage: Storage) -> None:
    """8 threads x 25 batches hitting the same two issues: the total must be exact."""

    def work() -> None:
        for _ in range(25):
            pg_storage.save_events([_event("a" * 64), _event("b" * 64), _event("a" * 64)])

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    counts = {i["fingerprint"][0]: i["times_seen"] for i in pg_storage.list_issues(1)}
    assert counts == {"a": 8 * 25 * 2, "b": 8 * 25}
