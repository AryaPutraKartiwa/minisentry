"""A small FastAPI shop with bugs on purpose, instrumented with minisentry.

docker compose up --build          # ingest API on :8000
uvicorn demo_app:app --app-dir examples --port 8001
curl localhost:8001/users/1        # KeyError   -> one issue, however many ids you try
curl localhost:8001/divide?b=0     # ZeroDivisionError
curl -X POST localhost:8001/login  # logs an error; the password is scrubbed
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

import minisentry
from minisentry.integrations.asgi import MiniSentryMiddleware

# Configure logging *before* init: basicConfig does nothing once the root logger
# has a handler, and init adds minisentry's handler to it.
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("demo_shop")

minisentry.init(
    os.environ.get("MINISENTRY_DSN", "http://dev-public-key@127.0.0.1:8000/1"),
    environment="demo",
    release="demo-shop@0.1.0",
)

app = FastAPI(title="demo shop")
app.add_middleware(MiniSentryMiddleware)

USERS = {0: "arya"}


@app.get("/users/{user_id}")
def get_user(user_id: int) -> dict[str, str]:
    log.info("looking up user %s", user_id)  # becomes a breadcrumb
    return {"name": USERS[user_id]}  # KeyError for anything but 0


@app.get("/divide")
def divide(a: float = 1, b: float = 0) -> dict[str, float]:
    return {"result": a / b}


@app.post("/login")
def login() -> dict[str, str]:
    minisentry.capture_message(
        "login failed",
        level="warning",
        extra={"username": "arya", "password": "hunter2"},  # sent as [Filtered]
    )
    return {"status": "denied"}
