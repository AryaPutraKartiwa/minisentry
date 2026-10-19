"""Removes sensitive values (passwords, tokens, cookies...) before an event leaves the process.

Scrubbing happens client-side on purpose: data that is never sent can never leak.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

FILTERED = "[Filtered]"

DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "session",
        "csrf",
        "credit_card",
        "card_number",
        "private_key",
    }
)

_MAX_DEPTH = 20


def is_sensitive(key: str, sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS) -> bool:
    """Substring match, so ``X-Auth-Token`` and ``refresh_token`` are caught too."""
    normalized = key.lower().replace("-", "_")
    return any(s in normalized for s in sensitive_keys)


def scrub(
    data: Any,
    sensitive_keys: frozenset[str] = DEFAULT_SENSITIVE_KEYS,
    _depth: int = 0,
) -> Any:
    """Return a copy of ``data`` with sensitive dict values replaced by ``[Filtered]``."""
    if _depth > _MAX_DEPTH:
        return FILTERED
    if isinstance(data, dict):
        return {
            k: FILTERED
            if isinstance(k, str) and is_sensitive(k, sensitive_keys)
            else scrub(v, sensitive_keys, _depth + 1)
            for k, v in data.items()
        }
    if isinstance(data, list | tuple):
        return [scrub(item, sensitive_keys, _depth + 1) for item in data]
    return data
