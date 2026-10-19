"""HTTP API.

Ingest path (hot path, kept cheap on purpose):
    authenticate -> rate limit -> validate each event -> enqueue -> 202

Everything expensive happens in the worker. Run with:

    uvicorn minisentry_server.app:create_app --factory
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from minisentry.ratelimit import TokenBucket
from minisentry_server.config import Settings
from minisentry_server.queue import EventQueue
from minisentry_server.schemas import Event
from minisentry_server.storage import Storage

logger = logging.getLogger("minisentry_server.api")
DASHBOARD_HTML = Path(__file__).parent / "static" / "dashboard.html"


def create_app(
    settings: Settings | None = None,
    queue: EventQueue | None = None,
    storage: Storage | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if queue is None:
        from minisentry_server.queue import RedisStreamQueue

        queue = RedisStreamQueue(settings.redis_url)
    storage = storage or Storage(settings.database_url)
    limiters: dict[int, TokenBucket] = {}

    app = FastAPI(title="minisentry", version="0.1.0")
    app.state.settings = settings
    app.state.queue = queue
    app.state.storage = storage

    def authenticate(project_id: int, key: str | None) -> None:
        expected = settings.project_keys.get(project_id)
        # compare_digest takes the same time for any input, so it doesn't leak the key.
        if expected is None or key is None or not hmac.compare_digest(expected, key):
            raise HTTPException(status_code=401, detail="invalid project or key")

    def limiter_for(project_id: int) -> TokenBucket:
        if project_id not in limiters:
            limiters[project_id] = TokenBucket(
                settings.rate_limit_per_second, settings.rate_limit_burst
            )
        return limiters[project_id]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        # Static page: it asks for the project key and calls the API like any other client.
        return FileResponse(DASHBOARD_HTML)

    @app.get("/api/{project_id}/stats")
    def stats(
        project_id: int,
        minutes: int = 60,
        x_minisentry_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authenticate(project_id, x_minisentry_key)
        minutes = min(max(minutes, 1), 1440)
        return storage.event_stats(project_id, datetime.now(timezone.utc), minutes)

    @app.post("/api/{project_id}/events", status_code=202)
    async def ingest(
        project_id: int,
        request: Request,
        x_minisentry_key: str | None = Header(default=None),
    ) -> JSONResponse:
        authenticate(project_id, x_minisentry_key)

        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            raise HTTPException(status_code=413, detail="request body too large")
        body = await request.body()
        if len(body) > settings.max_body_bytes:
            raise HTTPException(status_code=413, detail="request body too large")

        try:
            data = json.loads(body)
        except ValueError:
            raise HTTPException(status_code=400, detail="body is not valid JSON") from None
        raw_events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(raw_events, list) or not raw_events:
            raise HTTPException(status_code=400, detail='expected {"events": [...]}')
        if len(raw_events) > settings.max_events_per_request:
            raise HTTPException(
                status_code=413,
                detail=f"at most {settings.max_events_per_request} events per request",
            )

        if not limiter_for(project_id).try_acquire(len(raw_events)):
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": "1"},
            )

        # Validate events one by one: one bad event shouldn't throw away the whole batch.
        received_at = datetime.now(timezone.utc).isoformat()
        payloads: list[str] = []
        rejected: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_events):
            try:
                event = Event.model_validate(raw)
            except ValidationError as err:
                rejected.append({"index": index, "errors": err.errors(include_url=False)[:3]})
                continue
            envelope = {
                "project_id": project_id,
                "received_at": received_at,
                "event": event.model_dump(mode="json"),
            }
            payloads.append(json.dumps(envelope, separators=(",", ":")))

        if not payloads:
            return JSONResponse(status_code=400, content={"accepted": 0, "rejected": rejected})

        try:
            await run_in_threadpool(queue.publish, payloads)
        except Exception:
            logger.exception("failed to enqueue events")
            # 503 tells the SDK to retry later; the events are not lost yet.
            raise HTTPException(status_code=503, detail="queue unavailable") from None

        return JSONResponse(
            status_code=202, content={"accepted": len(payloads), "rejected": rejected}
        )

    @app.get("/api/{project_id}/issues")
    def list_issues(
        project_id: int,
        limit: int = 50,
        x_minisentry_key: str | None = Header(default=None),
    ) -> list[dict[str, Any]]:
        authenticate(project_id, x_minisentry_key)
        return storage.list_issues(project_id, limit=min(max(limit, 1), 200))

    @app.get("/api/{project_id}/issues/{issue_id}")
    def get_issue(
        project_id: int,
        issue_id: int,
        x_minisentry_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authenticate(project_id, x_minisentry_key)
        issue = storage.get_issue(project_id, issue_id)
        if issue is None:
            raise HTTPException(status_code=404, detail="issue not found")
        return issue

    @app.get("/api/{project_id}/issues/{issue_id}/events")
    def list_issue_events(
        project_id: int,
        issue_id: int,
        limit: int = 20,
        x_minisentry_key: str | None = Header(default=None),
    ) -> list[dict[str, Any]]:
        authenticate(project_id, x_minisentry_key)
        return storage.list_issue_events(project_id, issue_id, limit=min(max(limit, 1), 100))

    return app
