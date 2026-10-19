"""Reports uncaught exceptions from the main thread and from other threads."""

from __future__ import annotations

import sys
import threading
from types import TracebackType

_installed = False


def _report(exc: BaseException | None) -> None:
    from minisentry import get_client

    client = get_client()
    if client is None or exc is None or isinstance(exc, KeyboardInterrupt | SystemExit):
        return
    client.capture_exception(exc, level="fatal")
    client.flush(timeout=2.0)  # the process may be about to exit


def install() -> None:
    """Wrap (not replace) the existing hooks, so other tools that use them keep working."""
    global _installed
    if _installed:
        return
    _installed = True

    previous_excepthook = sys.excepthook

    def excepthook(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        try:
            _report(exc)
        finally:
            previous_excepthook(exc_type, exc, tb)

    previous_threading_hook = threading.excepthook

    def threading_excepthook(args: threading.ExceptHookArgs) -> None:
        try:
            _report(args.exc_value)
        finally:
            previous_threading_hook(args)

    sys.excepthook = excepthook
    threading.excepthook = threading_excepthook
