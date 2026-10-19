"""Sends a handful of errors through the real SDK, then prints the resulting issues.

docker compose up --build -d
python examples/trigger_errors.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

import minisentry

DSN = os.environ.get("MINISENTRY_DSN", "http://dev-public-key@127.0.0.1:8000/1")
client = minisentry.init(DSN, release="trigger@0.1.0", default_integrations=False)


def load_order(order_id: int) -> dict[str, int]:
    orders: dict[int, dict[str, int]] = {}
    return orders[order_id]


def parse_price(raw: str) -> float:
    return float(raw)


for order_id in range(10):
    try:
        load_order(order_id)
    except KeyError:
        minisentry.capture_exception()

for raw in ["12,50", "abc", "1.2.3"]:
    try:
        parse_price(raw)
    except ValueError:
        minisentry.capture_exception(tags={"input": raw})

minisentry.add_breadcrumb("cache warmed", category="startup")
minisentry.capture_message("payment provider slow: 2300 ms", level="warning")
minisentry.capture_message("payment provider slow: 4100 ms", level="warning")

print("flushed:", minisentry.flush(timeout=5))
time.sleep(1.5)  # give the worker a moment

request = urllib.request.Request(
    f"http://{client.dsn.host}:{client.dsn.port}/api/{client.dsn.project_id}/issues",
    headers={"X-Minisentry-Key": client.dsn.public_key},
)
with urllib.request.urlopen(request) as response:
    for issue in json.load(response):
        print(f"{issue['times_seen']:>4}x  {issue['title']:<45} {issue['culprit'] or ''}")
