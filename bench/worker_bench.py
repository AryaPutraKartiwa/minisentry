"""Measures worker throughput (grouping + DB writes) for different batch sizes.

    python bench/worker_bench.py                       # SQLite in a temp dir
    python bench/worker_bench.py --db postgresql+psycopg://minisentry:minisentry@localhost/minisentry

batch_size=1 is the naive "one transaction per event" design; larger batches
use one transaction + one upsert per distinct issue (see storage.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from sample_events import make_event

from minisentry_server.queue import InMemoryQueue
from minisentry_server.storage import Storage, events, issues
from minisentry_server.worker import Worker


def run(db_url: str, n_events: int, batch_size: int) -> float:
    storage = Storage(db_url)
    with storage.engine.begin() as conn:
        conn.execute(events.delete())
        conn.execute(issues.delete())
    queue = InMemoryQueue()
    received_at = datetime.now(timezone.utc).isoformat()
    queue.publish(
        [
            json.dumps({"project_id": 1, "received_at": received_at, "event": make_event()})
            for _ in range(n_events)
        ]
    )
    worker = Worker(queue, storage, batch_size=batch_size, block_ms=0)
    start = time.perf_counter()
    while worker.process_once():
        pass
    elapsed = time.perf_counter() - start
    assert worker.stats.stored == n_events, worker.stats
    storage.engine.dispose()
    return n_events / elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="SQLAlchemy URL (default: SQLite in a temp dir)")
    parser.add_argument("--events", type=int, default=5000)
    parser.add_argument("--batch-sizes", default="1,10,50,200")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        db_url = args.db or f"sqlite:///{os.path.join(tmp, 'bench.db')}"
        print(f"{args.events} events, 25 distinct issues, db={db_url.split(':')[0]}")
        print(f"{'batch size':>10} | {'events/s':>10}")
        print(f"{'-' * 10}-+-{'-' * 10}")
        for size in (int(s) for s in args.batch_sizes.split(",")):
            print(f"{size:>10} | {run(db_url, args.events, size):>10,.0f}")


if __name__ == "__main__":
    main()
