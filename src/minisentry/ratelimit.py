"""Token bucket rate limiter.

The bucket holds up to ``capacity`` tokens and refills at ``rate`` tokens per
second. Each event costs one token. This allows short bursts (up to
``capacity``) while capping the long-run average at ``rate``.

Refilling is computed lazily from the elapsed time on each call, so there is
no background timer and every operation is O(1).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self,
        rate: float,
        capacity: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._tokens = capacity
        self._last = clock()
        self._lock = threading.Lock()

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    @property
    def available(self) -> float:
        with self._lock:
            elapsed = self._clock() - self._last
            return min(self.capacity, self._tokens + elapsed * self.rate)
