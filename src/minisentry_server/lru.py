"""A fixed-capacity LRU (least recently used) cache.

Implemented from scratch with a hash map + doubly linked list:

* the dict maps key -> node, giving O(1) lookup;
* the linked list keeps nodes in recency order (most recent right after the
  head sentinel), so moving a node to the front or evicting the tail is O(1).

(``functools.lru_cache`` / ``OrderedDict`` do the same thing; writing it by
hand is the point of this file.)

Used by the worker to remember recently processed event ids, so that a batch
re-sent by an SDK retry is detected without a database round-trip.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class _Node(Generic[K, V]):
    __slots__ = ("key", "next", "prev", "value")

    def __init__(self, key: K | None, value: V | None) -> None:
        self.key = key
        self.value = value
        self.prev: _Node[K, V] = self
        self.next: _Node[K, V] = self


class LRUCache(Generic[K, V]):
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._map: dict[K, _Node[K, V]] = {}
        self._head: _Node[K, V] = _Node(None, None)  # sentinel; head.next = most recent

    def __len__(self) -> int:
        return len(self._map)

    def __contains__(self, key: object) -> bool:
        return key in self._map  # does not change recency

    def get(self, key: K, default: V | None = None) -> V | None:
        node = self._map.get(key)
        if node is None:
            return default
        self._unlink(node)
        self._push_front(node)
        return node.value

    def put(self, key: K, value: V) -> None:
        node = self._map.get(key)
        if node is not None:
            node.value = value
            self._unlink(node)
            self._push_front(node)
            return
        if len(self._map) >= self.capacity:
            lru = self._head.prev  # tail = least recently used
            self._unlink(lru)
            del self._map[lru.key]  # type: ignore[arg-type]
        node = _Node(key, value)
        self._map[key] = node
        self._push_front(node)

    def keys(self) -> list[K]:
        """Keys from most to least recently used (O(n), for debugging/tests)."""
        out: list[K] = []
        node = self._head.next
        while node is not self._head:
            out.append(node.key)  # type: ignore[arg-type]
            node = node.next
        return out

    def _unlink(self, node: _Node[K, V]) -> None:
        node.prev.next = node.next
        node.next.prev = node.prev

    def _push_front(self, node: _Node[K, V]) -> None:
        node.prev = self._head
        node.next = self._head.next
        self._head.next.prev = node
        self._head.next = node
