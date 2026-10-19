"""Worker: pulls events off the queue, groups them, stores them.

Delivery is at-least-once: a message is acknowledged only after its batch is
committed. If the worker crashes before that, the message stays pending in
Redis and is reclaimed later. Duplicates are therefore expected and handled
in two layers:

1. an in-memory LRU of recently stored event ids (fast path, no DB query);
2. the ``UNIQUE(event_id)`` constraint in the database (the real guarantee).
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from minisentry_server.config import Settings
from minisentry_server.fingerprint import compute_fingerprint, culprit, issue_title
from minisentry_server.lru import LRUCache
from minisentry_server.queue import EventQueue, Message
from minisentry_server.storage import ProcessedEvent, Storage

logger = logging.getLogger("minisentry_server.worker")


@dataclass
class WorkerStats:
    messages: int = 0
    stored: int = 0
    duplicates: int = 0
    invalid: int = 0
    batches: int = 0


def parse_timestamp(value: str) -> datetime:
    # datetime.fromisoformat only accepts a trailing "Z" from Python 3.11 on.
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)


def to_processed(message_payload: str) -> ProcessedEvent:
    envelope = json.loads(message_payload)
    event: dict[str, Any] = envelope["event"]
    return ProcessedEvent(
        event_id=event["event_id"],
        project_id=int(envelope["project_id"]),
        fingerprint=compute_fingerprint(event),
        title=issue_title(event),
        culprit=culprit(event),
        level=event.get("level", "error"),
        timestamp=parse_timestamp(event["timestamp"]),
        received_at=parse_timestamp(envelope["received_at"]),
        payload=event,
    )


class Worker:
    def __init__(
        self,
        queue: EventQueue,
        storage: Storage,
        *,
        batch_size: int = 200,
        block_ms: int = 1000,
        dedupe_cache_size: int = 100_000,
        reclaim_every_s: float = 30.0,
        reclaim_idle_ms: int = 60_000,
    ) -> None:
        self.queue = queue
        self.storage = storage
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.recent_ids: LRUCache[str, bool] = LRUCache(dedupe_cache_size)
        self.reclaim_every_s = reclaim_every_s
        self.reclaim_idle_ms = reclaim_idle_ms
        self.stats = WorkerStats()
        self._last_reclaim = time.monotonic()

    def process_once(self) -> int:
        """Process one batch. Returns the number of queue messages handled."""
        messages = self._next_messages()
        if not messages:
            return 0
        self.stats.messages += len(messages)

        batch: list[ProcessedEvent] = []
        seen_in_batch: set[str] = set()
        for _message_id, payload in messages:
            try:
                processed = to_processed(payload)
            except (ValueError, KeyError, TypeError):
                # A malformed message would fail forever; log it and drop it ("poison pill").
                logger.warning("dropping malformed message: %.200s", payload)
                self.stats.invalid += 1
                continue
            if processed.event_id in seen_in_batch or processed.event_id in self.recent_ids:
                self.stats.duplicates += 1
                continue
            seen_in_batch.add(processed.event_id)
            batch.append(processed)

        # If this raises, nothing is acked and the messages will be retried.
        stored = self.storage.save_events(batch)
        self.stats.duplicates += len(batch) - stored
        self.stats.stored += stored
        self.stats.batches += 1
        for processed in batch:
            self.recent_ids.put(processed.event_id, True)
        self.queue.ack([message_id for message_id, _ in messages])
        return len(messages)

    def _next_messages(self) -> list[Message]:
        now = time.monotonic()
        if now - self._last_reclaim >= self.reclaim_every_s:
            self._last_reclaim = now
            reclaimed = self.queue.reclaim_stale(self.reclaim_idle_ms, self.batch_size)
            if reclaimed:
                logger.info("reclaimed %d stale messages", len(reclaimed))
                return reclaimed
        return self.queue.consume(self.batch_size, self.block_ms)

    def run_forever(self, stop: threading.Event) -> None:
        logger.info("worker started")
        while not stop.is_set():
            try:
                self.process_once()
            except Exception:
                logger.exception("batch failed; backing off before retrying")
                stop.wait(1.0)
        logger.info("worker stopped: %s", self.stats)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from minisentry_server.queue import RedisStreamQueue

    settings = Settings.from_env()
    worker = Worker(RedisStreamQueue(settings.redis_url), Storage(settings.database_url))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())  # finish the current batch, then exit
    worker.run_forever(stop)


if __name__ == "__main__":
    main()
