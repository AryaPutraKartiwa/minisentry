# Design decisions

Short notes on the choices behind minisentry, what each one costs, and what
I would change at a larger scale.

## 1. The SDK has zero dependencies

**Decision:** stdlib only (`urllib`, `threading`, `json`, `linecache`).

**Why:** an error-tracking SDK is installed into *other people's* apps. Every
dependency could clash with a version the app already pins. `requests` or
`httpx` would be nicer to write, but the risk falls on the user, not on me.

**Cost:** no HTTP keep-alive or connection pooling, so each batch opens a new
connection. That's acceptable because batching already cuts the number of
requests a lot.

## 2. Never block or crash the host app

- `capture_*` never raises. Internal errors are logged at DEBUG and swallowed.
- `submit()` is non-blocking. If the 1,000-slot queue is full, the event is
  **dropped and counted** (`stats.dropped_queue_full`). Losing an error report
  is bad; slowing down or OOM-ing the app being monitored is worse.
- If `before_send` raises, the event is **dropped, not sent unmodified**. The
  hook might exist to remove private data, so sending the original would be
  the unsafe failure mode.

## 3. Retries: exponential backoff + jitter, kept in a heap

Failed batches (network error, 5xx, 429) are retried after
`min(max_backoff, base * 2^attempt)`, with random "equal jitter" so thousands
of clients that failed at the same moment don't all retry at the same moment.
4xx responses other than 429 are not retried: the request is wrong, and
sending it again won't fix it.

Pending retries live in a min-heap keyed by due time: peeking at the next one
is O(1), scheduling is O(log n), and the sender thread knows exactly how long
it can sleep.

A `429` with `Retry-After` pauses *all* sending, not just that one batch,
because the server is telling the whole client to slow down.

## 4. Ingest returns 202 and does nothing expensive

The API authenticates, rate-limits, validates and enqueues, nothing more.
Grouping and database writes happen in workers. This keeps request latency
flat during a traffic spike: the queue gets longer instead of requests
getting slower. That matters most when a customer's app is on fire and
sending 100x its normal error volume.

Validation is **per event**: one malformed event in a batch of 50 is reported
in `rejected` and the other 49 are still accepted.

## 5. Redis Streams instead of Celery (or a plain Redis list)

| Need | Redis list | Celery | Redis Streams |
|---|---|---|---|
| Several workers share load | yes | yes | yes (consumer groups) |
| Message survives a worker crash | no (popped = gone) | depends on config | yes (pending until `XACK`) |
| Take over a dead worker's messages | no | n/a | `XAUTOCLAIM` |
| Batch reads | manual | no (task = 1 message) | `XREADGROUP COUNT n` |
| Extra moving parts | none | broker + result backend + framework | none |

Celery is built for "run this function later". Here the workload is "stream
of small records, processed in batches", which is what Streams are for.
At real Sentry scale this role is played by Kafka (partitioning, replay,
retention), which would be the next step.

## 6. At-least-once delivery, made idempotent

A message is acked only **after** its batch is committed to Postgres. If the
worker dies in between, the message is reclaimed and processed again. So
duplicates are expected, and are handled twice:

1. **LRU cache** of the last 100k event ids in each worker. This is the fast
   path that catches SDK retries without a database query.
2. **`UNIQUE(event_id)`** + `INSERT ... ON CONFLICT DO NOTHING RETURNING`. This
   is the actual guarantee; only the rows that were really inserted count
   towards an issue.

Exactly-once delivery across a network isn't possible; at-least-once plus
idempotent processing gets the same result.

## 7. Grouping (fingerprinting)

Default fingerprint = SHA-256 of:

- each exception type in the chain, plus
- `module.function` of each **in-app** frame (library frames are ignored if
  any app frames exist), with consecutive repeats collapsed.

Deliberately **not** included:

- **line numbers**, because adding a comment above a bug shouldn't open a
  new issue;
- **the exception message**, because it usually contains ids
  (`KeyError: 'user-17'`).

When there's no stack trace, the message is used after numbers, hex and
UUIDs are replaced with placeholders.

**Known trade-offs:** renaming a function splits an issue in two; two
different bugs in the same function with the same exception type get merged.
Sentry handles this with merge/split tools and hierarchical grouping. Here,
a custom `fingerprint=[...]` from the SDK is the escape hatch.

## 8. Atomic upserts, pre-aggregated, in sorted order

Counting with "SELECT, then UPDATE count+1" loses updates when two workers
hit the same issue at once. Instead, a single statement does the whole thing:

```sql
INSERT INTO issues (...) VALUES (...)
ON CONFLICT (project_id, fingerprint)
DO UPDATE SET times_seen = issues.times_seen + excluded.times_seen, ...
```

- **Pre-aggregation:** a batch of 200 events for one bug becomes *one*
  upsert with `times_seen = 200`. Hot issues are exactly the ones that need
  this most.
- **Sorted order:** each transaction upserts issues sorted by
  `(project_id, fingerprint)`. Every worker takes row locks in the same order,
  which prevents deadlocks between workers.

`tests/test_infra_integration.py` checks this with 8 threads writing to the
same issues on real Postgres and requires the exact count.

## 9. Rate limiting with a token bucket (both sides)

The same `TokenBucket` class (O(1), refilled lazily from elapsed time, no
timer thread) is used:

- **in the SDK**, optionally, so an error loop in the app can't flood the
  network;
- **in the API**, per project, where it returns `429 Retry-After`, which the
  SDK respects.

A bucket allows bursts up to `capacity` while capping the average at `rate`.
That fits error traffic, which tends to arrive in bursts.

**Limitation:** the API's buckets are in-process, so with N API replicas the
effective limit is N x rate. A shared limit would need Redis (e.g. a Lua
script doing the same refill math atomically).

## Lesson learned: `localhost` cost 2 seconds per request

While testing the demo app on Windows, 6 errors went in and only 1 issue came
out. The grouping logic was fine; the events simply hadn't arrived yet. Each
batch took about 2 s to send:

```
urlopen("http://localhost:8000/healthz")   2.07 s
urlopen("http://127.0.0.1:8000/healthz")   0.002 s
```

`localhost` resolves to `::1` (IPv6) first. The server listened only on
IPv4, and Windows retries a refused connection for about 2 s before the
client falls back to `127.0.0.1`. Because the SDK opens a new connection for
every batch (no keep-alive, see section 1), it paid that 2 s every time.

What I changed: the examples use `127.0.0.1`. What it taught me: a
"zero-dependency" HTTP client also means no connection reuse and no
happy-eyeballs address racing. A connection pool is now on the roadmap.

## What I'd do next at larger scale

- Kafka instead of Redis Streams; partition by `project_id`.
- ClickHouse for event storage and queries; keep Postgres for issue metadata.
- gzip request bodies from the SDK (smaller payloads, less bandwidth).
- A shared, Redis-based rate limiter.
- Source context resolution and in-app detection configured per project.
