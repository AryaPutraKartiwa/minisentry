"""Fingerprinting and the LRU cache."""

from __future__ import annotations

from typing import Any

import pytest

from minisentry_server.fingerprint import (
    compute_fingerprint,
    culprit,
    issue_title,
    normalize_message,
)
from minisentry_server.lru import LRUCache


def frame(function: str, module: str = "app.views", lineno: int = 10, in_app: bool = True) -> dict:
    return {"module": module, "function": function, "lineno": lineno, "in_app": in_app}


def exc_event(type_: str, value: str, frames: list[dict], **extra: Any) -> dict[str, Any]:
    return {
        "exception": {
            "values": [{"type": type_, "value": value, "stacktrace": {"frames": frames}}]
        },
        **extra,
    }


def test_same_location_different_message_groups_together() -> None:
    a = exc_event("KeyError", "'user-1'", [frame("main"), frame("get_user")])
    b = exc_event("KeyError", "'user-2'", [frame("main"), frame("get_user")])
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_line_numbers_do_not_matter() -> None:
    a = exc_event("ValueError", "x", [frame("handler", lineno=10)])
    b = exc_event("ValueError", "x", [frame("handler", lineno=99)])
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_different_function_or_type_splits() -> None:
    base = exc_event("ValueError", "x", [frame("handler")])
    other_fn = exc_event("ValueError", "x", [frame("other_handler")])
    other_type = exc_event("TypeError", "x", [frame("handler")])
    assert len({compute_fingerprint(e) for e in (base, other_fn, other_type)}) == 3


def test_library_frames_are_ignored_when_app_frames_exist() -> None:
    a = exc_event("E", "", [frame("view"), frame("query", "sqlalchemy.engine", in_app=False)])
    b = exc_event("E", "", [frame("view"), frame("execute", "sqlalchemy.orm", in_app=False)])
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_recursion_depth_does_not_split_groups() -> None:
    shallow = exc_event("RecursionError", "", [frame("walk")] * 3)
    deep = exc_event("RecursionError", "", [frame("walk")] * 300)
    assert compute_fingerprint(shallow) == compute_fingerprint(deep)


def test_custom_fingerprint_wins() -> None:
    a = exc_event("A", "", [frame("x")], fingerprint=["payments"])
    b = exc_event("B", "", [frame("y")], fingerprint=["payments"])
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_messages_are_normalized() -> None:
    a = {"message": "timeout after 30s for order 1234"}
    b = {"message": "timeout after 5s for order 99"}
    assert compute_fingerprint(a) == compute_fingerprint(b)
    assert compute_fingerprint(a) != compute_fingerprint({"message": "disk full"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("user 42 failed", "user <num> failed"),
        ("at 0xdeadbeef", "at <hex>"),
        ("id 550e8400-e29b-41d4-a716-446655440000 gone", "id <uuid> gone"),
        ("  lots   of\nspace ", "lots of space"),
    ],
)
def test_normalize_message(raw: str, expected: str) -> None:
    assert normalize_message(raw) == expected


def test_title_and_culprit() -> None:
    event = exc_event(
        "KeyError",
        "'sku'\nmore lines",
        [frame("main", "app.cli"), frame("price", "app.cart"), frame("get", "dict", in_app=False)],
    )
    assert issue_title(event) == "KeyError: 'sku'"
    assert culprit(event) == "app.cart in price"
    assert issue_title({"message": "hello"}) == "hello"
    assert culprit({"message": "hello"}) is None


# ---- LRU ----------------------------------------------------------------


def test_lru_evicts_least_recently_used() -> None:
    cache: LRUCache[str, int] = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1  # "a" is now most recent
    cache.put("c", 3)  # evicts "b"
    assert "b" not in cache
    assert cache.keys() == ["c", "a"]


def test_lru_update_existing_key_refreshes_it() -> None:
    cache: LRUCache[str, int] = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)
    cache.put("c", 3)
    assert cache.get("a") == 10
    assert cache.get("b") is None
    assert len(cache) == 2


def test_lru_capacity_one_and_validation() -> None:
    cache: LRUCache[int, int] = LRUCache(1)
    for i in range(5):
        cache.put(i, i)
    assert cache.keys() == [4]
    with pytest.raises(ValueError):
        LRUCache(0)


def test_lru_matches_reference_implementation() -> None:
    """Randomized check against OrderedDict, which has well-known LRU semantics."""
    import random
    from collections import OrderedDict

    rng = random.Random(1234)
    cache: LRUCache[int, int] = LRUCache(8)
    reference: OrderedDict[int, int] = OrderedDict()
    for _ in range(5000):
        key = rng.randrange(20)
        if rng.random() < 0.5:
            cache.put(key, key * 2)
            reference[key] = key * 2
            reference.move_to_end(key)
            if len(reference) > 8:
                reference.popitem(last=False)
        else:
            expected = reference.get(key)
            if expected is not None:
                reference.move_to_end(key)
            assert cache.get(key) == expected
    assert cache.keys() == list(reversed(reference))
