"""Synthetic events shared by the benchmarks: 25 distinct "bugs", random ids and lines."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone
from typing import Any

SHAPES = [
    (exc, f"app.module_{i}", f"handler_{i}")
    for exc in ("KeyError", "ValueError", "TypeError", "TimeoutError", "ZeroDivisionError")
    for i in range(5)
]


def make_event() -> dict[str, Any]:
    exc_type, module, function = random.choice(SHAPES)
    return {
        "event_id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": "error",
        "platform": "python",
        "exception": {
            "values": [
                {
                    "type": exc_type,
                    "value": f"something failed for id {random.randint(1, 10_000)}",
                    "stacktrace": {
                        "frames": [
                            {"module": "app.main", "function": "dispatch", "in_app": True},
                            {
                                "module": module,
                                "function": function,
                                "in_app": True,
                                "lineno": random.randint(1, 300),
                            },
                        ]
                    },
                }
            ]
        },
        "tags": {"region": random.choice(["eu", "us", "ap"])},
    }
