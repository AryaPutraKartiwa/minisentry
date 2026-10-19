"""Standard-library logging integration.

* Records at ``breadcrumb_level`` (INFO) and above become breadcrumbs.
* Records at ``event_level`` (ERROR) and above become events. If the record
  carries ``exc_info`` (``logger.exception(...)``) the exception is captured.
"""

from __future__ import annotations

import logging
import threading

_IGNORED_LOGGERS = ("minisentry", "urllib3")
_LEVEL_NAMES = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warning",
    logging.ERROR: "error",
    logging.CRITICAL: "fatal",
}

_handler: MiniSentryHandler | None = None


def _level_name(levelno: int) -> str:
    for threshold in sorted(_LEVEL_NAMES, reverse=True):
        if levelno >= threshold:
            return _LEVEL_NAMES[threshold]
    return "debug"


class MiniSentryHandler(logging.Handler):
    def __init__(
        self, event_level: int = logging.ERROR, breadcrumb_level: int = logging.INFO
    ) -> None:
        super().__init__(level=min(event_level, breadcrumb_level))
        self.event_level = event_level
        self.breadcrumb_level = breadcrumb_level
        self._guard = threading.local()  # stops recursion if capturing logs something

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_IGNORED_LOGGERS) or getattr(self._guard, "active", False):
            return
        from minisentry import get_client

        client = get_client()
        if client is None:
            return
        self._guard.active = True
        try:
            message = record.getMessage()
            level = _level_name(record.levelno)
            if record.levelno >= self.event_level:
                extra = {"logger": record.name, "log_message": message}
                if record.exc_info and record.exc_info[1] is not None:
                    client.capture_exception(record.exc_info[1], level=level, extra=extra)
                else:
                    client.capture_message(message, level=level, extra=extra)
            if record.levelno >= self.breadcrumb_level:
                client.add_breadcrumb(message, category=record.name, level=level)
        except Exception:
            self.handleError(record)
        finally:
            self._guard.active = False


def install(
    event_level: int = logging.ERROR, breadcrumb_level: int = logging.INFO
) -> MiniSentryHandler:
    global _handler
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
    _handler = MiniSentryHandler(event_level, breadcrumb_level)
    # We deliberately don't lower the root logger's level: that would change what the
    # app's own handlers print. Breadcrumbs only see records the app's config lets through.
    root.addHandler(_handler)
    return _handler
