from __future__ import annotations

import os
from dataclasses import dataclass, field


def parse_project_keys(value: str) -> dict[int, str]:
    """``"1:key-one,2:key-two"`` -> ``{1: "key-one", 2: "key-two"}``"""
    keys: dict[int, str] = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        project_id, sep, key = item.partition(":")
        if not sep or not project_id.strip().isdigit() or not key.strip():
            raise ValueError(f"Invalid PROJECT_KEYS entry: {item!r}")
        keys[int(project_id)] = key.strip()
    return keys


@dataclass
class Settings:
    database_url: str = "sqlite:///./minisentry.db"
    redis_url: str = "redis://localhost:6379/0"
    project_keys: dict[int, str] = field(default_factory=lambda: {1: "dev-public-key"})
    rate_limit_per_second: float = 1000.0  # per project
    rate_limit_burst: float = 5000.0
    max_body_bytes: int = 1_000_000
    max_events_per_request: int = 100

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        return cls(
            database_url=os.environ.get("DATABASE_URL", defaults.database_url),
            redis_url=os.environ.get("REDIS_URL", defaults.redis_url),
            project_keys=parse_project_keys(os.environ["PROJECT_KEYS"])
            if os.environ.get("PROJECT_KEYS")
            else defaults.project_keys,
            rate_limit_per_second=float(
                os.environ.get("RATE_LIMIT_PER_SECOND", defaults.rate_limit_per_second)
            ),
            rate_limit_burst=float(os.environ.get("RATE_LIMIT_BURST", defaults.rate_limit_burst)),
        )
