"""Background HTTP transport: batching, retries with backoff, and back-pressure.

Design goals, in priority order:

1. Never block or crash the host application. ``submit`` is non-blocking; if
   the in-memory queue is full the event is dropped and counted.
2. Don't hammer a struggling server. Failed batches are retried with
   exponential backoff + jitter, and a ``429 Retry-After`` pauses all sending.
3. Be efficient. A single worker thread drains the queue and sends events in
   batches (opportunistic batching: whatever is queued, up to ``batch_size``).

Retries are kept in a min-heap ordered by "next attempt time", so finding the
next due batch is O(1) and scheduling one is O(log n).
"""

from __future__ import annotations

import heapq
import itertools
import json
import logging
import queue
import random
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from minisentry.dsn import Dsn
from minisentry.ratelimit import TokenBucket
from minisentry.version import VERSION

logger = logging.getLogger("minisentry.transport")

Event = dict[str, Any]


@dataclass(frozen=True)
class SendResult:
    status: int  # HTTP status, or 0 for network errors
    retry_after: float | None = None


Sender = Callable[[list[Event]], SendResult]


@dataclass
class TransportStats:
    sent: int = 0
    retried: int = 0
    dropped_queue_full: int = 0
    dropped_rate_limited: int = 0
    dropped_rejected: int = 0
    dropped_retries_exhausted: int = 0
    batches: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def incr(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)


def parse_retry_after(value: str | None, default: float = 60.0) -> float:
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        return default  # HTTP-date form is valid too, but we keep it simple


class HttpTransport:
    def __init__(
        self,
        dsn: Dsn,
        *,
        batch_size: int = 50,
        flush_interval: float = 1.0,
        max_queue_size: int = 1000,
        max_retries: int = 5,
        base_backoff: float = 0.5,
        max_backoff: float = 30.0,
        timeout: float = 5.0,
        rate_limiter: TokenBucket | None = None,
        sender: Sender | None = None,
    ) -> None:
        self.dsn = dsn
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.timeout = timeout
        self.rate_limiter = rate_limiter
        self.stats = TransportStats()

        self._send_fn: Sender = sender or self._http_send
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=max_queue_size)
        # (due_at, tie_breaker, batch, attempt). The counter keeps tuples comparable.
        self._retry_heap: list[tuple[float, int, list[Event], int]] = []
        self._seq = itertools.count()
        self._paused_until = 0.0

        self._pending = 0  # events accepted but not yet delivered or dropped
        self._pending_cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="minisentry-transport", daemon=True)
        self._thread.start()

    # ---- public API -----------------------------------------------------

    def submit(self, event: Event) -> bool:
        if self._stop.is_set():
            return False
        if self.rate_limiter is not None and not self.rate_limiter.try_acquire():
            self.stats.incr("dropped_rate_limited")
            return False
        with self._pending_cond:
            self._pending += 1
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._done(1)
            self.stats.incr("dropped_queue_full")
            return False
        return True

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until every accepted event is delivered or dropped. True if done in time."""
        deadline = time.monotonic() + timeout
        with self._pending_cond:
            while self._pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._pending_cond.wait(remaining)
        return True

    def close(self, timeout: float = 2.0) -> None:
        self.flush(timeout)
        self._stop.set()
        self._thread.join(timeout=1.0)

    @property
    def pending(self) -> int:
        return self._pending

    # ---- worker thread --------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # pragma: no cover - last line of defence
                logger.exception("minisentry transport loop crashed; continuing")

    def _tick(self) -> None:
        batch = self._collect_batch(self._next_wait())
        if batch:
            self._attempt(batch, attempt=0)
        self._run_due_retries()

    def _next_wait(self) -> float:
        wait = self.flush_interval
        if self._retry_heap:
            wait = min(wait, self._retry_heap[0][0] - time.monotonic())
        return max(0.01, wait)

    def _collect_batch(self, wait: float) -> list[Event]:
        try:
            first = self._queue.get(timeout=wait)
        except queue.Empty:
            return []
        batch = [first]
        while len(batch) < self.batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _run_due_retries(self) -> None:
        now = time.monotonic()
        while self._retry_heap and self._retry_heap[0][0] <= now:
            _, _, batch, attempt = heapq.heappop(self._retry_heap)
            self._attempt(batch, attempt)

    def _attempt(self, batch: list[Event], attempt: int) -> None:
        now = time.monotonic()
        if now < self._paused_until:  # server told us to back off
            self._schedule_retry(batch, attempt, self._paused_until - now, count=False)
            return

        result = self._send_fn(batch)
        self.stats.incr("batches")
        if 200 <= result.status < 300:
            self.stats.incr("sent", len(batch))
            self._done(len(batch))
            return

        retryable = result.status == 0 or result.status == 429 or result.status >= 500
        if not retryable:
            logger.warning("minisentry: server rejected batch with HTTP %s", result.status)
            self.stats.incr("dropped_rejected", len(batch))
            self._done(len(batch))
            return

        if result.status == 429:
            self._paused_until = now + (
                result.retry_after if result.retry_after is not None else 60.0
            )
        if attempt >= self.max_retries:
            self.stats.incr("dropped_retries_exhausted", len(batch))
            self._done(len(batch))
            return
        delay = max(self._backoff(attempt), self._paused_until - now)
        self._schedule_retry(batch, attempt + 1, delay)

    def _schedule_retry(
        self, batch: list[Event], attempt: int, delay: float, count: bool = True
    ) -> None:
        if count:
            self.stats.incr("retried")
        heapq.heappush(
            self._retry_heap, (time.monotonic() + delay, next(self._seq), batch, attempt)
        )

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with "equal jitter": spreads out retries from many clients."""
        ceiling = min(self.max_backoff, self.base_backoff * (2.0**attempt))
        return ceiling / 2 + random.uniform(0, ceiling / 2)

    def _done(self, n: int) -> None:
        with self._pending_cond:
            self._pending -= n
            if self._pending <= 0:
                self._pending_cond.notify_all()

    # ---- HTTP -----------------------------------------------------------

    def _http_send(self, batch: list[Event]) -> SendResult:
        body = json.dumps({"events": batch}, default=str).encode("utf-8")
        request = urllib.request.Request(
            self.dsn.events_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Minisentry-Key": self.dsn.public_key,
                "User-Agent": f"minisentry-python/{VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return SendResult(response.status)
        except urllib.error.HTTPError as err:
            return SendResult(err.code, parse_retry_after(err.headers.get("Retry-After")))
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            logger.debug("minisentry: network error sending batch: %s", err)
            return SendResult(0)
