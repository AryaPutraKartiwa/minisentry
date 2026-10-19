"""Turns Python exceptions into JSON-friendly stack traces.

Frames are ordered oldest call first (same as Python's traceback output and
Sentry's event format). Each frame carries a few lines of source context and
an ``in_app`` flag that tells the server which frames belong to the user's own
code - the server groups errors by those frames.
"""

from __future__ import annotations

import linecache
import os
import sys
import sysconfig
from types import TracebackType
from typing import Any

MAX_FRAMES = 100
MAX_CHAINED_EXCEPTIONS = 10


def _library_paths() -> tuple[str, ...]:
    paths = set()
    for name in ("stdlib", "platstdlib", "purelib", "platlib"):
        path = sysconfig.get_paths().get(name)
        if path:
            paths.add(os.path.normcase(os.path.abspath(path)))
    for prefix in (sys.prefix, sys.base_prefix):
        paths.add(os.path.normcase(os.path.join(os.path.abspath(prefix), "lib")))
    return tuple(paths)


_LIBRARY_PATHS = _library_paths()


def is_in_app(filename: str | None, module: str | None) -> bool:
    if not filename or filename.startswith("<"):
        return False
    if module and (module == "minisentry" or module.startswith("minisentry.")):
        return False  # never blame the SDK's own frames
    path = os.path.normcase(os.path.abspath(filename))
    if "site-packages" in path or "dist-packages" in path:
        return False
    return not path.startswith(_LIBRARY_PATHS)


def _source_lines(filename: str, lineno: int, context: int) -> dict[str, Any]:
    def line(n: int) -> str | None:
        raw = linecache.getline(filename, n)
        return raw.rstrip("\r\n") if raw else None  # "" means past end of file

    pre = [ln for n in range(max(1, lineno - context), lineno) if (ln := line(n)) is not None]
    post = []
    for n in range(lineno + 1, lineno + context + 1):
        ln = line(n)
        if ln is None:
            break
        post.append(ln)
    return {"pre_context": pre, "context_line": line(lineno), "post_context": post}


def frames_from_traceback(tb: TracebackType | None, context_lines: int = 5) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while tb is not None:
        frame = tb.tb_frame
        filename = frame.f_code.co_filename
        module = frame.f_globals.get("__name__")
        entry: dict[str, Any] = {
            "filename": os.path.basename(filename),
            "abs_path": filename,
            "module": module,
            "function": frame.f_code.co_name,
            "lineno": tb.tb_lineno,
            "in_app": is_in_app(filename, module),
        }
        if context_lines > 0 and tb.tb_lineno:
            entry.update(_source_lines(filename, tb.tb_lineno, context_lines))
        frames.append(entry)
        tb = tb.tb_next
    if len(frames) > MAX_FRAMES:
        # Keep both ends: where it started and where it blew up.
        half = MAX_FRAMES // 2
        frames = frames[:half] + frames[-half:]
    return frames


def exception_chain(exc: BaseException) -> list[BaseException]:
    """Return ``exc`` and its causes, oldest cause first, the raised exception last."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < MAX_CHAINED_EXCEPTIONS:
        seen.add(id(current))
        chain.append(current)
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    chain.reverse()
    return chain


def exception_values(exc: BaseException, context_lines: int = 5) -> list[dict[str, Any]]:
    return [
        {
            "type": type(e).__name__,
            "module": type(e).__module__,
            "value": str(e),
            "stacktrace": {"frames": frames_from_traceback(e.__traceback__, context_lines)},
        }
        for e in exception_chain(exc)
    ]
