"""The client builds events and hands them to a transport."""

from __future__ import annotations

import logging
import platform
import random
import socket
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol

from minisentry.breadcrumbs import Breadcrumbs
from minisentry.dsn import Dsn
from minisentry.ratelimit import TokenBucket
from minisentry.scrubber import DEFAULT_SENSITIVE_KEYS, scrub
from minisentry.stacktrace import exception_values
from minisentry.transport import HttpTransport
from minisentry.version import VERSION

logger = logging.getLogger("minisentry")

Event = dict[str, Any]
Hint = dict[str, Any]
BeforeSend = Callable[[Event, Hint], "Event | None"]

LEVELS = ("debug", "info", "warning", "error", "fatal")
# Event sections that can hold user data and therefore get scrubbed.
_SCRUBBED_SECTIONS = ("extra", "request", "breadcrumbs", "tags", "user")


class Transport(Protocol):
    def submit(self, event: Event) -> bool: ...
    def flush(self, timeout: float = ...) -> bool: ...
    def close(self, timeout: float = ...) -> None: ...


class Client:
    def __init__(
        self,
        dsn: str,
        *,
        environment: str = "production",
        release: str | None = None,
        server_name: str | None = None,
        sample_rate: float = 1.0,
        before_send: BeforeSend | None = None,
        max_breadcrumbs: int = 100,
        context_lines: int = 5,
        tags: Mapping[str, Any] | None = None,
        sensitive_keys: frozenset[str] = DEFAULT_SENSITIVE_KEYS,
        rate_limit_per_second: float | None = None,
        transport: Transport | None = None,
    ) -> None:
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError("sample_rate must be between 0 and 1")
        self.dsn = Dsn.parse(dsn)
        self.environment = environment
        self.release = release
        self.server_name = server_name or socket.gethostname()
        self.sample_rate = sample_rate
        self.before_send = before_send
        self.context_lines = context_lines
        self.tags: dict[str, str] = {k: str(v) for k, v in (tags or {}).items()}
        self.sensitive_keys = sensitive_keys
        self.breadcrumbs = Breadcrumbs(max_breadcrumbs)
        if transport is None:
            limiter = None
            if rate_limit_per_second:
                limiter = TokenBucket(rate_limit_per_second, capacity=rate_limit_per_second * 2)
            transport = HttpTransport(self.dsn, rate_limiter=limiter)
        self.transport: Transport = transport

    # ---- capture --------------------------------------------------------

    def capture_exception(
        self,
        exc: BaseException | None = None,
        *,
        level: str = "error",
        **kwargs: Any,
    ) -> str | None:
        """Capture an exception (defaults to the one currently being handled)."""
        if exc is None:
            exc = sys.exc_info()[1]
            if exc is None:
                return None
        try:
            event = self._base_event(level)
            event["exception"] = {"values": exception_values(exc, self.context_lines)}
            return self._finish(event, {"exc": exc}, **kwargs)
        except Exception:  # the SDK must never break the app it is monitoring
            logger.debug("minisentry: failed to capture exception", exc_info=True)
            return None

    def capture_message(self, message: str, *, level: str = "info", **kwargs: Any) -> str | None:
        try:
            event = self._base_event(level)
            event["message"] = message
            return self._finish(event, {"message": message}, **kwargs)
        except Exception:
            logger.debug("minisentry: failed to capture message", exc_info=True)
            return None

    def add_breadcrumb(self, message: str, **kwargs: Any) -> None:
        self.breadcrumbs.add(message, **kwargs)

    def flush(self, timeout: float = 2.0) -> bool:
        return self.transport.flush(timeout)

    def close(self, timeout: float = 2.0) -> None:
        self.transport.close(timeout)

    # ---- internals ------------------------------------------------------

    def _base_event(self, level: str) -> Event:
        if level not in LEVELS:
            raise ValueError(f"Unknown level {level!r}")
        return {
            "event_id": uuid.uuid4().hex,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "platform": "python",
            "environment": self.environment,
            "release": self.release,
            "server_name": self.server_name,
            "sdk": {"name": "minisentry.python", "version": VERSION},
            "contexts": {
                "runtime": {
                    "name": platform.python_implementation(),
                    "version": platform.python_version(),
                },
                "os": {"name": platform.system(), "version": platform.release()},
            },
            "breadcrumbs": self.breadcrumbs.snapshot(),
        }

    def _finish(
        self,
        event: Event,
        hint: Hint,
        *,
        tags: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
        user: Mapping[str, Any] | None = None,
        request: Mapping[str, Any] | None = None,
        fingerprint: Sequence[str] | None = None,
    ) -> str | None:
        if self.sample_rate < 1.0 and random.random() >= self.sample_rate:
            return None

        event["tags"] = {**self.tags, **{k: str(v) for k, v in (tags or {}).items()}}
        event["extra"] = dict(extra or {})
        if user:
            event["user"] = dict(user)
        if request:
            event["request"] = dict(request)
        if fingerprint:
            event["fingerprint"] = [str(part) for part in fingerprint]

        for section in _SCRUBBED_SECTIONS:
            if section in event:
                event[section] = scrub(event[section], self.sensitive_keys)

        if self.before_send is not None:
            try:
                processed = self.before_send(event, hint)
            except Exception:
                # Dropping is the safe choice: the hook may exist to strip private data.
                logger.warning("minisentry: before_send raised; dropping event", exc_info=True)
                return None
            if processed is None:
                return None
            event = processed

        event_id: str = event["event_id"]
        return event_id if self.transport.submit(event) else None
