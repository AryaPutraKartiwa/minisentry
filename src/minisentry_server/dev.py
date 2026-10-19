"""Run the whole pipeline in one process, without Redis or Postgres:

    python -m minisentry_server.dev            # http://127.0.0.1:8000

The API and a worker thread share an in-memory queue; data goes to SQLite.
Good for trying things out. Events still queued are lost on restart, so
use docker compose for anything serious.
"""

from __future__ import annotations

import logging
import os
import threading

from fastapi import FastAPI

from minisentry_server.app import create_app
from minisentry_server.config import Settings
from minisentry_server.queue import InMemoryQueue
from minisentry_server.storage import Storage
from minisentry_server.worker import Worker


def create_dev_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    queue = InMemoryQueue()
    storage = Storage(settings.database_url)
    app = create_app(settings, queue, storage)

    worker = Worker(queue, storage, block_ms=200)
    stop = threading.Event()
    threading.Thread(
        target=worker.run_forever, args=(stop,), name="minisentry-dev-worker", daemon=True
    ).start()
    app.state.worker = worker
    app.state.stop_worker = stop
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_dev_app(),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
