"""Small building blocks: DSN parsing, breadcrumbs, scrubbing, token bucket."""

from __future__ import annotations

import pytest

from minisentry.breadcrumbs import Breadcrumbs
from minisentry.dsn import Dsn
from minisentry.ratelimit import TokenBucket
from minisentry.scrubber import FILTERED, scrub


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# ---- DSN ----------------------------------------------------------------


def test_dsn_parses_all_parts() -> None:
    dsn = Dsn.parse("https://abc123@errors.example.com:8443/42")
    assert dsn.public_key == "abc123"
    assert dsn.host == "errors.example.com"
    assert dsn.port == 8443
    assert dsn.project_id == "42"
    assert dsn.events_url == "https://errors.example.com:8443/api/42/events"


def test_dsn_without_port() -> None:
    assert Dsn.parse("http://k@localhost/1").events_url == "http://localhost/api/1/events"


@pytest.mark.parametrize(
    "bad",
    ["ftp://k@host/1", "http://host/1", "http://k@host/", "http://k@host/abc", "not a url"],
)
def test_dsn_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        Dsn.parse(bad)


# ---- breadcrumbs --------------------------------------------------------


def test_breadcrumbs_keep_only_the_most_recent() -> None:
    crumbs = Breadcrumbs(max_breadcrumbs=3)
    for i in range(5):
        crumbs.add(f"step {i}")
    assert [c["message"] for c in crumbs.snapshot()] == ["step 2", "step 3", "step 4"]


def test_breadcrumb_snapshot_is_a_copy() -> None:
    crumbs = Breadcrumbs()
    crumbs.add("a")
    snapshot = crumbs.snapshot()
    crumbs.add("b")
    assert len(snapshot) == 1


# ---- scrubbing ----------------------------------------------------------


def test_scrub_replaces_sensitive_keys_recursively() -> None:
    data = {
        "username": "arya",
        "password": "hunter2",
        "headers": {"Authorization": "Bearer xyz", "Accept": "json"},
        "items": [{"api_key": "k"}, {"name": "ok"}],
        "X-Refresh-Token": "t",
    }
    result = scrub(data)
    assert result["username"] == "arya"
    assert result["password"] == FILTERED
    assert result["headers"] == {"Authorization": FILTERED, "Accept": "json"}
    assert result["items"] == [{"api_key": FILTERED}, {"name": "ok"}]
    assert result["X-Refresh-Token"] == FILTERED
    assert data["password"] == "hunter2", "input must not be mutated"


def test_scrub_stops_at_max_depth() -> None:
    deep: dict[str, object] = {}
    node = deep
    for _ in range(50):
        child: dict[str, object] = {}
        node["child"] = child
        node = child
    scrub(deep)  # must not hit RecursionError


# ---- token bucket -------------------------------------------------------


def test_token_bucket_allows_burst_then_limits() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=1, capacity=3, clock=clock)
    assert [bucket.try_acquire() for _ in range(4)] == [True, True, True, False]


def test_token_bucket_refills_over_time_up_to_capacity() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2, capacity=4, clock=clock)
    for _ in range(4):
        bucket.try_acquire()
    clock.now = 1.0  # 1s * 2 tokens/s
    assert bucket.try_acquire(2)
    assert not bucket.try_acquire()
    clock.now = 100.0
    assert bucket.available == 4  # never above capacity


def test_token_bucket_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=0, capacity=1)
