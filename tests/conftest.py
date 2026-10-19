from __future__ import annotations

from typing import Any

import pytest

import minisentry
from minisentry.client import Client

DSN = "http://test-key@localhost:9/1"


class FakeTransport:
    """Records events instead of sending them."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.accept = True

    def submit(self, event: dict[str, Any]) -> bool:
        if self.accept:
            self.events.append(event)
        return self.accept

    def flush(self, timeout: float = 2.0) -> bool:
        return True

    def close(self, timeout: float = 2.0) -> None:
        pass


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def client(transport: FakeTransport) -> Client:
    return Client(DSN, transport=transport, release="test@1.0.0")


@pytest.fixture
def global_client(transport: FakeTransport, monkeypatch: pytest.MonkeyPatch) -> Client:
    """Installs ``client`` as the global client without touching sys.excepthook."""
    c = Client(DSN, transport=transport)
    monkeypatch.setattr(minisentry, "_client", c)
    return c
