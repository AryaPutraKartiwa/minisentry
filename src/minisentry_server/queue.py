"""The buffer between the HTTP API and the workers.

The API only validates and enqueues, then answers ``202 Accepted``. The heavy
work (grouping, database writes) happens in workers. A traffic spike makes
the queue longer instead of making requests slow or failing them.

Redis Streams give us consumer groups (several workers share the load),
acknowledgements (a message stays "pending" until a worker confirms it was
stored) and reclaiming (messages held by a crashed worker are taken over).
That is at-least-once delivery; the worker de-duplicates by ``event_id``.
"""

from __future__ import annotations

import itertools
import os
import socket
import threading
from collections import deque
from typing import Any, Protocol

Message = tuple[str, str]  # (message id, JSON payload)


class EventQueue(Protocol):
    def publish(self, payloads: list[str]) -> None: ...
    def consume(self, count: int, block_ms: int) -> list[Message]: ...
    def ack(self, message_ids: list[str]) -> None: ...
    def reclaim_stale(self, min_idle_ms: int, count: int) -> list[Message]: ...


class InMemoryQueue:
    """Single-process stand-in for Redis, used by tests and local development."""

    def __init__(self) -> None:
        self._messages: deque[Message] = deque()
        self._pending: dict[str, str] = {}
        self._ids = itertools.count(1)
        self._cond = threading.Condition()

    def publish(self, payloads: list[str]) -> None:
        with self._cond:
            for payload in payloads:
                self._messages.append((f"{next(self._ids)}-0", payload))
            self._cond.notify_all()

    def consume(self, count: int, block_ms: int) -> list[Message]:
        with self._cond:
            if not self._messages and block_ms > 0:
                self._cond.wait(block_ms / 1000)
            batch: list[Message] = []
            while self._messages and len(batch) < count:
                message = self._messages.popleft()
                self._pending[message[0]] = message[1]
                batch.append(message)
            return batch

    def ack(self, message_ids: list[str]) -> None:
        with self._cond:
            for message_id in message_ids:
                self._pending.pop(message_id, None)

    def reclaim_stale(self, min_idle_ms: int, count: int) -> list[Message]:
        return []

    def __len__(self) -> int:
        return len(self._messages)

    @property
    def pending_count(self) -> int:
        return len(self._pending)


class RedisStreamQueue:
    def __init__(
        self,
        url: str,
        stream: str = "minisentry:events",
        group: str = "workers",
        consumer: str | None = None,
        maxlen: int = 1_000_000,
    ) -> None:
        import redis

        self._redis: Any = redis.Redis.from_url(url, decode_responses=True)
        self.stream = stream
        self.group = group
        self.consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self.maxlen = maxlen
        self._group_ready = False

    def _ensure_group(self) -> None:
        if self._group_ready:
            return
        import redis

        try:
            self._redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as err:
            if "BUSYGROUP" not in str(err):
                raise
        self._group_ready = True

    def publish(self, payloads: list[str]) -> None:
        # One round-trip for the whole batch instead of one per event.
        pipe = self._redis.pipeline(transaction=False)
        for payload in payloads:
            # Approximate trimming caps memory if workers fall far behind.
            pipe.xadd(self.stream, {"e": payload}, maxlen=self.maxlen, approximate=True)
        pipe.execute()

    def consume(self, count: int, block_ms: int) -> list[Message]:
        self._ensure_group()
        response = self._redis.xreadgroup(
            self.group, self.consumer, {self.stream: ">"}, count=count, block=block_ms
        )
        return [(mid, fields["e"]) for _, entries in response or [] for mid, fields in entries]

    def ack(self, message_ids: list[str]) -> None:
        if message_ids:
            self._redis.xack(self.stream, self.group, *message_ids)

    def reclaim_stale(self, min_idle_ms: int, count: int) -> list[Message]:
        """Take over messages another worker read but never acknowledged (it probably died)."""
        self._ensure_group()
        result = self._redis.xautoclaim(
            self.stream, self.group, self.consumer, min_idle_time=min_idle_ms, count=count
        )
        entries = result[1] if len(result) > 1 else []
        return [(mid, fields["e"]) for mid, fields in entries if fields]

    def ping(self) -> bool:
        return bool(self._redis.ping())
