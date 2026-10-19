"""minisentry: a tiny, dependency-free error tracking SDK.

import minisentry
minisentry.init("http://public-key@127.0.0.1:8000/1", release="myapp@1.0.0")

try:
    1 / 0
except ZeroDivisionError:
    minisentry.capture_exception()
"""

from __future__ import annotations

import atexit
import os
from typing import Any

from minisentry.client import Client
from minisentry.version import VERSION

__all__ = [
    "VERSION",
    "Client",
    "add_breadcrumb",
    "capture_exception",
    "capture_message",
    "flush",
    "get_client",
    "init",
]

_client: Client | None = None
_atexit_registered = False


def init(dsn: str | None = None, *, default_integrations: bool = True, **options: Any) -> Client:
    """Create the global client and install the default integrations.

    ``dsn`` falls back to the ``MINISENTRY_DSN`` environment variable.
    """
    global _client, _atexit_registered
    dsn = dsn or os.environ.get("MINISENTRY_DSN")
    if not dsn:
        raise ValueError("minisentry.init() needs a DSN (or set MINISENTRY_DSN)")
    if _client is not None:
        _client.close()
    _client = Client(dsn, **options)

    if default_integrations:
        from minisentry.integrations import excepthook, logging

        excepthook.install()
        logging.install()

    if not _atexit_registered:
        atexit.register(_shutdown)
        _atexit_registered = True
    return _client


def get_client() -> Client | None:
    return _client


def capture_exception(exc: BaseException | None = None, **kwargs: Any) -> str | None:
    return _client.capture_exception(exc, **kwargs) if _client else None


def capture_message(message: str, **kwargs: Any) -> str | None:
    return _client.capture_message(message, **kwargs) if _client else None


def add_breadcrumb(message: str, **kwargs: Any) -> None:
    if _client:
        _client.add_breadcrumb(message, **kwargs)


def flush(timeout: float = 2.0) -> bool:
    return _client.flush(timeout) if _client else True


def _shutdown() -> None:
    if _client is not None:
        _client.close(timeout=2.0)
