"""Breadcrumbs: the trail of things that happened before an error.

Stored in a fixed-size ring buffer (``collections.deque`` with ``maxlen``):
appending is O(1) and the oldest crumb is dropped automatically, so memory
stays bounded no matter how long the process runs.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class Breadcrumbs:
    def __init__(self, max_breadcrumbs: int = 100) -> None:
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max_breadcrumbs)
        self._lock = threading.Lock()

    def add(
        self,
        message: str,
        *,
        category: str = "default",
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> None:
        crumb = {
            "timestamp": time.time(),
            "category": category,
            "level": level,
            "message": message,
            "data": data or {},
        }
        with self._lock:
            self._buffer.append(crumb)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._buffer)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def __len__(self) -> int:
        return len(self._buffer)
