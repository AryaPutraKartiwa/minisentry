"""Persistence for issues and events (SQLAlchemy Core; Postgres in prod, SQLite in tests).

An *issue* is a group of events with the same fingerprint. Instead of a
read-modify-write ("SELECT the issue, then UPDATE count+1"), which races when
several workers handle the same error, we use a single atomic upsert:

    INSERT ... ON CONFLICT (project_id, fingerprint)
    DO UPDATE SET times_seen = issues.times_seen + excluded.times_seen, ...

The worker also pre-aggregates each batch: 200 events of the same error
become one upsert with ``times_seen = 200``, not 200 upserts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    case,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

issues = Table(
    "issues",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Integer, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("title", String(255), nullable=False),
    Column("culprit", String(255)),
    Column("level", String(16), nullable=False),
    Column("times_seen", BigInteger, nullable=False),
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("last_seen", DateTime(timezone=True), nullable=False),
    UniqueConstraint("project_id", "fingerprint", name="uq_issues_project_fingerprint"),
)

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(32), nullable=False, unique=True),
    Column("project_id", Integer, nullable=False),
    Column("fingerprint", String(64), nullable=False, index=True),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)


@dataclass(frozen=True)
class ProcessedEvent:
    event_id: str
    project_id: int
    fingerprint: str
    title: str
    culprit: str | None
    level: str
    timestamp: datetime
    received_at: datetime
    payload: dict[str, Any]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


class Storage:
    def __init__(self, url: str, create_tables: bool = True) -> None:
        self.engine: Engine = create_engine(url, pool_pre_ping=True)
        # Both dialects offer the same on_conflict_* API, so the rest of the code is shared.
        self._insert: Any
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            self._insert = pg_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            self._insert = sqlite_insert
        if create_tables:
            metadata.create_all(self.engine)

    def save_events(self, batch: list[ProcessedEvent]) -> int:
        """Store a batch atomically. Returns how many events were new (not duplicates)."""
        if not batch:
            return 0
        with self.engine.begin() as conn:
            # 1. Insert events; already-stored event_ids (SDK retries) are skipped.
            insert_events = (
                self._insert(events)
                .values(
                    [
                        {
                            "event_id": e.event_id,
                            "project_id": e.project_id,
                            "fingerprint": e.fingerprint,
                            "timestamp": _utc(e.timestamp),
                            "received_at": _utc(e.received_at),
                            "payload": e.payload,
                        }
                        for e in batch
                    ]
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
                .returning(events.c.event_id)
            )
            new_ids = {row[0] for row in conn.execute(insert_events)}

            # 2. Aggregate new events per issue, then one upsert per issue.
            groups: dict[tuple[int, str], list[ProcessedEvent]] = {}
            for e in batch:
                if e.event_id in new_ids:
                    groups.setdefault((e.project_id, e.fingerprint), []).append(e)

            # Sorted order = every worker locks rows in the same order = no deadlocks.
            for key in sorted(groups):
                group = groups[key]
                # Ties are common (coarse clocks), so on a tie take the event that arrived last.
                latest = max(reversed(group), key=lambda e: e.timestamp)
                stmt = self._insert(issues).values(
                    project_id=key[0],
                    fingerprint=key[1],
                    title=latest.title,
                    culprit=latest.culprit,
                    level=latest.level,
                    times_seen=len(group),
                    first_seen=_utc(min(e.timestamp for e in group)),
                    last_seen=_utc(latest.timestamp),
                )
                excluded = stmt.excluded
                # Title/culprit/level follow the newest event; a late, older batch must not
                # overwrite them.
                newer = excluded.last_seen >= issues.c.last_seen
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "fingerprint"],
                    set_={
                        "times_seen": issues.c.times_seen + excluded.times_seen,
                        "first_seen": case(
                            (excluded.first_seen < issues.c.first_seen, excluded.first_seen),
                            else_=issues.c.first_seen,
                        ),
                        "last_seen": case(
                            (excluded.last_seen > issues.c.last_seen, excluded.last_seen),
                            else_=issues.c.last_seen,
                        ),
                        "title": case((newer, excluded.title), else_=issues.c.title),
                        "culprit": case((newer, excluded.culprit), else_=issues.c.culprit),
                        "level": case((newer, excluded.level), else_=issues.c.level),
                    },
                )
                conn.execute(stmt)
        return len(new_ids)

    # ---- reads ----------------------------------------------------------

    def list_issues(self, project_id: int, limit: int = 50) -> list[dict[str, Any]]:
        query = (
            select(issues)
            .where(issues.c.project_id == project_id)
            .order_by(issues.c.last_seen.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            return [self._issue_row(row._mapping) for row in conn.execute(query)]

    def get_issue(self, project_id: int, issue_id: int) -> dict[str, Any] | None:
        query = select(issues).where(
            and_(issues.c.project_id == project_id, issues.c.id == issue_id)
        )
        with self.engine.connect() as conn:
            row = conn.execute(query).first()
        return self._issue_row(row._mapping) if row else None

    def list_issue_events(
        self, project_id: int, issue_id: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        query = (
            select(events.c.event_id, events.c.timestamp, events.c.payload)
            .join(
                issues,
                and_(
                    issues.c.project_id == events.c.project_id,
                    issues.c.fingerprint == events.c.fingerprint,
                ),
            )
            .where(and_(issues.c.project_id == project_id, issues.c.id == issue_id))
            .order_by(events.c.timestamp.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            return [
                {"event_id": r.event_id, "timestamp": _iso(r.timestamp), "payload": r.payload}
                for r in conn.execute(query)
            ]

    def event_stats(self, project_id: int, now: datetime, minutes: int = 60) -> dict[str, Any]:
        """Total stored events, plus events received per minute over the last ``minutes``."""
        now = _utc(now).replace(second=0, microsecond=0)
        start = now - timedelta(minutes=minutes - 1)
        counts = [0] * minutes
        with self.engine.connect() as conn:
            total = conn.execute(
                select(func.count()).select_from(events).where(events.c.project_id == project_id)
            ).scalar_one()
            recent = conn.execute(
                select(events.c.received_at).where(
                    and_(events.c.project_id == project_id, events.c.received_at >= start)
                )
            )
            # Bucketing in Python keeps this portable across SQLite and Postgres.
            for (received_at,) in recent:
                index = int((_utc(received_at) - start).total_seconds() // 60)
                if 0 <= index < minutes:
                    counts[index] += 1
        return {
            "total_events": int(total),
            "per_minute": [
                {"minute": (start + timedelta(minutes=i)).isoformat(), "count": c}
                for i, c in enumerate(counts)
            ],
        }

    def count_events(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(events)).scalar_one())

    @staticmethod
    def _issue_row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "fingerprint": row["fingerprint"],
            "title": row["title"],
            "culprit": row["culprit"],
            "level": row["level"],
            "times_seen": row["times_seen"],
            "first_seen": _iso(row["first_seen"]),
            "last_seen": _iso(row["last_seen"]),
        }
