"""ASGI middleware (FastAPI, Starlette, ...): reports unhandled request errors.

app = FastAPI()
app.add_middleware(MiniSentryMiddleware)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from minisentry.client import Client

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def request_info(scope: Scope) -> dict[str, Any]:
    headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
    host = headers.get("host") or "unknown"
    return {
        "method": scope.get("method"),
        "url": f"{scope.get('scheme', 'http')}://{host}{scope.get('path', '')}",
        "query_string": scope.get("query_string", b"").decode("latin-1"),
        "headers": headers,  # sensitive ones (Authorization, Cookie) are scrubbed by the client
    }


class MiniSentryMiddleware:
    def __init__(self, app: ASGIApp, client: Client | None = None) -> None:
        self.app = app
        self._client = client

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from minisentry import get_client

        client = self._client or get_client()
        if client is not None:
            client.add_breadcrumb(
                f"{scope.get('method')} {scope.get('path')}", category="http.request"
            )
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            if client is not None:
                client.capture_exception(
                    exc,
                    request=request_info(scope),
                    tags={"transaction": str(scope.get("path", ""))},
                )
            raise
