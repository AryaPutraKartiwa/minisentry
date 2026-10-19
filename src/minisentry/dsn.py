"""Parsing of DSNs (Data Source Names).

A DSN tells the SDK where to send events and how to authenticate:

    http://<public_key>@<host>[:port]/<project_id>
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Dsn:
    scheme: str
    public_key: str
    host: str
    port: int | None
    project_id: str

    @classmethod
    def parse(cls, value: str) -> Dsn:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"Unsupported DSN scheme: {parts.scheme!r}")
        if not parts.username:
            raise ValueError("DSN is missing the public key")
        if not parts.hostname:
            raise ValueError("DSN is missing the host")
        project_id = parts.path.strip("/")
        if not project_id.isdigit():
            raise ValueError("DSN must end with a numeric project id, e.g. /1")
        return cls(parts.scheme, parts.username, parts.hostname, parts.port, project_id)

    @property
    def events_url(self) -> str:
        netloc = self.host if self.port is None else f"{self.host}:{self.port}"
        return f"{self.scheme}://{netloc}/api/{self.project_id}/events"
