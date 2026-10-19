"""Load test for the ingest endpoint.

    pip install -e ".[bench]"
    docker compose up --build -d          # or: python -m minisentry_server.dev
    locust -f bench/locustfile.py --host http://127.0.0.1:8000 \
        --headless -u 50 -r 10 -t 60s

Each request carries BATCH_SIZE events, like the SDK does under load.
Events/second = requests/second x BATCH_SIZE.
"""

from __future__ import annotations

import os
import sys

from locust import HttpUser, constant, task

sys.path.insert(0, os.path.dirname(__file__))
from sample_events import make_event

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "20"))
KEY = os.environ.get("MINISENTRY_KEY", "dev-public-key")


class SdkUser(HttpUser):
    wait_time = constant(0)

    @task
    def send_batch(self) -> None:
        self.client.post(
            "/api/1/events",
            json={"events": [make_event() for _ in range(BATCH_SIZE)]},
            headers={"X-Minisentry-Key": KEY},
            name="POST /api/1/events",
        )
