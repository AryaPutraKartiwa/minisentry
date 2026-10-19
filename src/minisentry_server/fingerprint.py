"""Grouping: deciding which events are "the same error" and belong to one issue.

Default strategy, in order:

1. If the SDK sent a custom ``fingerprint``, use it as-is.
2. If the event has an exception with a stack trace, use the exception type
   plus each **in-app** frame's ``module.function``. Line numbers are left
   out on purpose: adding a comment above the bug shouldn't create a new issue.
   The exception message is left out too, because it often contains ids
   (``KeyError: 'user-123'`` vs ``KeyError: 'user-456'`` are the same bug).
3. Otherwise fall back to the exception type / log message with variable
   parts (numbers, hex, UUIDs) replaced by placeholders.

Consecutive duplicate frames are collapsed so that recursion depth doesn't
split one bug into many issues.

The components are joined and hashed with SHA-256.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}\b"
)
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
# Digits not glued to a word ("30s" and "user-42" match, "md5" and "utf8" don't).
_NUMBER = re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?")
_MAX_MESSAGE = 200


def normalize_message(message: str) -> str:
    message = _UUID.sub("<uuid>", message)
    message = _HEX.sub("<hex>", message)
    message = _NUMBER.sub("<num>", message)
    return " ".join(message.split())[:_MAX_MESSAGE]


def _collapse_repeats(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if not out or out[-1] != item:
            out.append(item)
    return out


def grouping_components(event: dict[str, Any]) -> list[str]:
    custom = event.get("fingerprint")
    if custom:
        return ["custom", *map(str, custom)]

    values = (event.get("exception") or {}).get("values") or []
    if values:
        components: list[str] = []
        for value in values:  # the whole chain, so "X caused by Y" differs from "X"
            components.append(f"type:{value.get('type')}")
            frames = (value.get("stacktrace") or {}).get("frames") or []
            in_app = [f for f in frames if f.get("in_app")]
            chosen = in_app or frames
            if chosen:
                components.extend(
                    _collapse_repeats(
                        [
                            f"frame:{f.get('module') or f.get('filename')}.{f.get('function')}"
                            for f in chosen
                        ]
                    )
                )
            else:
                components.append(f"value:{normalize_message(value.get('value') or '')}")
        return components

    return ["message", normalize_message(event.get("message") or "")]


def compute_fingerprint(event: dict[str, Any]) -> str:
    joined = "\n".join(grouping_components(event))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def issue_title(event: dict[str, Any]) -> str:
    values = (event.get("exception") or {}).get("values") or []
    if values:
        last = values[-1]
        value = (last.get("value") or "").strip()
        title = f"{last.get('type')}: {value}" if value else str(last.get("type"))
    else:
        title = event.get("message") or "<no message>"
    return title.splitlines()[0][:255] if title else "<no message>"


def culprit(event: dict[str, Any]) -> str | None:
    """Where the error happened: the innermost in-app frame (or innermost frame)."""
    values = (event.get("exception") or {}).get("values") or []
    if not values:
        return None
    frames = (values[-1].get("stacktrace") or {}).get("frames") or []
    in_app = [f for f in frames if f.get("in_app")] or frames
    if not in_app:
        return None
    frame = in_app[-1]
    return f"{frame.get('module') or frame.get('filename')} in {frame.get('function')}"[:255]
