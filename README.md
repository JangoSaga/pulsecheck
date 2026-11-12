# PulseCheck

Uptime monitoring backend. Users register HTTP endpoints as monitors. PulseCheck checks
them on a schedule, records every result, opens and resolves incidents using
consecutive-failure thresholds, emails the owner, and reports uptime analytics.

Stack: Python 3.12, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, PostgreSQL 16,
Redis 7, httpx, APScheduler, aiosmtplib, Pydantic v2, pytest, ruff, uv, Docker Compose.

## Quick start

Requires Docker. From this directory:

```bash
docker compose up -d --build        # builds one image, starts 8 containers
docker compose ps                   # all should be (healthy)
python tools/smoke_test.py          # end-to-end check against the running stack
```

| Service | Host port | Purpose |
|---|---|---|
| api | 8001 | REST API. Docs at http://localhost:8001/docs |
| postgres | 5433 | PostgreSQL |
| redis | 6380 | Job queue, locks, worker heartbeats |
| mailpit | 8025 (UI), 1025 (SMTP) | Dev mail catcher: http://localhost:8025 |
| scheduler, check-worker, notification-worker | none | Background processes |
| target | none | Controlled test target used by the smoke test (`/status/<code>`, `/delay/<s>`) |

The compose project name is `pulsecheck`. The API container runs `alembic upgrade head`
on start.

Compose runs with `ALLOW_PRIVATE_TARGETS=true` so that monitors can point at containers
on the compose network (this is how the smoke test reaches `target`). That is a
development setting. See "SSRF protection".

## Development

```bash
uv sync                                   # Python >=3.12, tested on 3.12 (image) and 3.14 (host)
docker compose up -d postgres redis mailpit
uv run ruff check . && uv run ruff format --check .
uv run pytest                             # needs the postgres and redis above
```

Tests use their own database `pulsecheck_test` (created automatically) and Redis db 15,
so they never touch application data. Override with `TEST_DATABASE_URL` and
`TEST_REDIS_URL`. CI (`.github/workflows/ci.yml`) runs lint, migrations (`alembic
upgrade head` + `alembic check`), tests, then builds the stack and runs the smoke test.

Run a process outside Docker: `uv run uvicorn app.main:app`, `uv run python -m
app.workers.scheduler`, `uv run python -m app.workers.check_worker`, `uv run python -m
app.workers.notification_worker`.

## Configuration

All settings are environment variables (see `.env.example` and `app/config.py`).

| Variable | Default | Meaning |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` refuses a weak `JWT_SECRET` and `ALLOW_PRIVATE_TARGETS=true` at startup |
| `DATABASE_URL` | `postgresql+asyncpg://pulsecheck:pulsecheck@localhost:5433/pulsecheck` | |
| `REDIS_URL` | `redis://localhost:6380/0` | |
| `JWT_SECRET` | dev value | Required, 32+ chars, in production |
| `JWT_EXPIRE_MINUTES` | 60 | |
| `ALLOW_PRIVATE_TARGETS` | `false` | Dev only, see SSRF section |
| `MIN_INTERVAL_SECONDS` | 10 (compose: 5) | Lowest allowed monitor interval |
| `MAX_CONCURRENT_CHECKS` | 20 | Per check-worker process |
| `SCHEDULER_TICK_SECONDS` | 2 (compose: 1) | How often due monitors are claimed |
| `STALE_JOB_SECONDS` | 180 | Visibility timeout for jobs held by dead workers |
| `RETENTION_DAYS` | 30 | Check history older than this is deleted |
| `RETENTION_INTERVAL_SECONDS` | 3600 | Cleanup cadence |
| `NOTIFY_MAX_ATTEMPTS` | 5 | Delivery attempts before a notification is `failed` |
| `NOTIFY_RETRY_BASE_SECONDS` | 30 | Backoff base, doubles per attempt |
| `SMTP_HOST`, `SMTP_PORT` | `localhost`, 1025 | Compose: `mailpit`, 1025 |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | empty | Production credentials, from the environment only |
| `SMTP_STARTTLS`, `SMTP_USE_TLS` | `false` | Enable one of them for a real provider |
| `MAIL_FROM` | `PulseCheck <alerts@pulsecheck.local>` | |

Secrets are only read from the environment and never logged (the JSON log formatter also
redacts any field whose name contains `password`, `secret`, `token` or `authorization`).

## Architecture

```
              +-------------+      claim due monitors       +----------------+
 clients ---> |  API        |      (FOR UPDATE SKIP LOCKED) |  scheduler     |
  (JWT)       |  FastAPI    |                               |  APScheduler   |
              +------+------+                               +---+--------+---+
                     |                                          | LPUSH  | purge
                     v                                          v        v
              +-------------+   BLMOVE queue->processing  +-----------+  retention
              | PostgreSQL  | <------------------------- |  Redis    |
              | users       |                            |  queue    |
              | monitors    |        +-------------+     |  locks    |
              | checks      | <------+ check-worker| <---+  beats    |
              | incidents   |  result|  (N async)  |
              | notifications|       +-------------+
              +------+------+
                     ^   pending rows (SKIP LOCKED)
                     |
              +------+---------------+   SMTP    +---------+
              | notification-worker  | --------> | Mailpit |
              +----------------------+           +---------+
```

- **API** validates input, enforces ownership, stores configuration and serves history
  and analytics. It never performs a health check.
- **Scheduler** (separate process, APScheduler) claims due monitors and pushes jobs to
  Redis. It also requeues abandoned jobs and runs retention. It makes no HTTP requests.
- **Check workers** (separate process, scale with `--scale check-worker=N`) take jobs,
  run the check with explicit timeouts, and persist the result and state transition in
  one transaction. Concurrency is bounded per process by `MAX_CONCURRENT_CHECKS`.
- **Notification worker** polls for due notification rows and sends email over SMTP.
- **Redis** carries the job queue, per-monitor in-flight locks and worker heartbeats.
  PostgreSQL stays the source of truth: losing Redis loses no data.

Structured JSON logs go to stdout. Every API request has an `X-Request-ID` (accepted if
it is a sane alphanumeric token, otherwise generated, always echoed). The scheduler
generates a correlation ID per claimed job; the check worker restores it from the job
so the check, state change and incident log lines share one ID.

### Data model

`users`, `monitors`, `monitor_checks`, `incidents`, `notifications` as in the spec.
Additions: monitors carry runtime state (`status`, `consecutive_failures`,
`consecutive_successes`, `last_checked_at`, `next_check_at`), and notifications carry
`event` (`opened` or `resolved`) and `next_attempt_at`.

Indexes: `monitors(user_id)`; partial `monitors(next_check_at) WHERE is_active`;
`monitor_checks(monitor_id, checked_at)` and `monitor_checks(checked_at)` (retention);
`incidents(monitor_id, started_at)`; partial unique `incidents(monitor_id) WHERE status =
'open'` (open-incident lookup and the duplicate guard); unique `notifications(incident_id,
channel, event)` (dedup); partial `notifications(next_attempt_at) WHERE status =
'pending'`.

## API usage

Interactive docs: http://localhost:8001/docs. All monitor and analytics routes need
`Authorization: Bearer <token>`. A monitor that belongs to someone else returns 404, the
same as a missing one.

```bash
API=http://localhost:8001

curl -s $API/api/v1/auth/register -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"a-long-password"}'

TOKEN=$(curl -s $API/api/v1/auth/login -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"a-long-password"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s $API/api/v1/monitors -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
  "name": "Prod API", "target_url": "https://api.example.com/health",
  "http_method": "GET", "interval_seconds": 60, "timeout_seconds": 10,
  "expected_status_codes": [200, 204], "failure_threshold": 3, "recovery_threshold": 2}'
```

| Method | Endpoint | Notes |
|---|---|---|
| POST | `/api/v1/auth/register`, `/api/v1/auth/login` | JSON body `email`, `password` (8 to 72 chars). `GET /api/v1/auth/me` |
| POST | `/api/v1/monitors` | `name`, `target_url` required. Defaults: GET, 60s, 10s, `[200]`, thresholds 3 and 2. Methods: GET, HEAD, POST |
| GET | `/api/v1/monitors` | `limit` (1-200, default 50), `offset` |
| GET / PATCH / DELETE | `/api/v1/monitors/{id}` | PATCH is partial and re-validates the merged result. DELETE removes history too |
| POST | `/api/v1/monitors/{id}/pause`, `/resume` | Resume schedules an immediate check and resets streak counters |
| GET | `/api/v1/monitors/{id}/checks` | `from`, `to` (ISO 8601, default last 24h, max span 90 days), `outcome`, `limit`, `offset`, newest first |
| GET | `/api/v1/monitors/{id}/incidents` | `from`, `to` (by `started_at`), `status`, pagination. Includes notification attempts |
| GET | `/api/v1/analytics/uptime` | `monitor_id` (optional, default all of your monitors), `from`, `to` |
| GET | `/healthz`, `/readyz` | Liveness, and readiness (database and Redis) |

Responses to list endpoints are `{items, total, limit, offset}`. Invalid ranges
(`from >= to`, span over 90 days), pagination bounds and unknown fields return 422.
Monitor limits: interval `MIN_INTERVAL_SECONDS` to 86400, timeout 1 to 60 and not above
the interval, 50 monitors per user.

## Check semantics

A check is **healthy** when the response status code is in `expected_status_codes`.
Everything else is **unhealthy** with an `error_type`: `unexpected_status`, `timeout`,
`connection_error`, `dns_error`, `tls_error`, `ssrf_blocked`, `invalid_url`. Error messages
are fixed, sanitised strings (no raw exception text, URLs or addresses). The timeout bounds
the whole check including DNS. Redirects are not followed: a 3xx is a status code like any
other (list `301` in `expected_status_codes` if that is the expected answer). Response
bodies are never read. `response_time_ms` is time to response headers.

### Incidents

Per monitor, under a row lock, so results apply in a strict order:

- A healthy result increments `consecutive_successes` and zeroes `consecutive_failures`;
  an unhealthy result does the opposite.
- With no open incident, reaching `failure_threshold` consecutive failures opens one
  (`failure_reason` is the failing check's reason) and queues one `opened` notification.
- With an open incident, reaching `recovery_threshold` consecutive successes resolves it
  and queues one `resolved` notification. A failure inside the recovery window resets the
  success streak.
- Duplicates are impossible three ways: the row lock serialises results, the open-incident
  check precedes the insert, and a partial unique index allows one open incident per
  monitor.
- `status` is `unknown` until the first healthy result, `up` after healthy results with no
  open incident, `down` while an incident is open. Failures below the threshold do not
  change it. Analytics report `paused` for paused monitors.
- Results that arrive for a paused or deleted monitor (a check that was in flight) are
  discarded.

### Notifications

One row per incident transition, enforced by the unique `(incident_id, channel, event)`
constraint and `INSERT ... ON CONFLICT DO NOTHING`. The worker claims due rows with
`FOR UPDATE SKIP LOCKED`, sends to the monitor owner's email, and records every attempt
(`attempt_count`, `last_error`, `sent_at`). Temporary failures (connection errors,
timeouts, SMTP 4xx) retry with exponential backoff `base * 2^(attempt-1)` (30s, 60s, 120s,
...) up to `NOTIFY_MAX_ATTEMPTS`, then the row becomes `failed`. Permanent failures
(SMTP 5xx) fail immediately. Delivery is at-least-once: a crash between the SMTP
acceptance and the commit can send one email twice. It never sends zero times without a
`failed` record.

## SSRF protection

Applied when a monitor is created or updated **and again on every check**:

- Only `http` and `https`; no credentials in the URL; no control characters or whitespace.
- The host is resolved at request time, and **every** address it returns must be globally
  routable. Blocked: loopback, private (RFC 1918, ULA), link-local, carrier-grade NAT
  (100.64.0.0/10), multicast, reserved, unspecified, and cloud metadata addresses
  (169.254.169.254, 169.254.170.2, fd00:ec2::254, 100.100.100.200, 192.0.0.192).
  IPv4-mapped, 6to4, Teredo and NAT64 IPv6 forms are unwrapped and checked. Names such as
  `localhost`, `*.localhost`, `*.internal`, `*.local` are rejected. Decimal and hex IP
  forms are caught because the resolved address is what is checked.
- The connection is pinned to the validated IP (the original name is kept in the `Host`
  header and as the TLS SNI and certificate name), so a name cannot resolve differently
  between validation and connection (DNS rebinding).
- Redirects are never followed, so a redirect cannot reach an internal address.
- `ALLOW_PRIVATE_TARGETS=true` permits private, loopback and link-local destinations for
  controlled development only. Cloud metadata addresses stay blocked even then, and
  `ENVIRONMENT=production` refuses to start with it enabled.
- A host that does not resolve yet is accepted at creation (the check reports
  `dns_error`); it is still guarded at request time.

Known limit: any port on a public host is allowed.

## Uptime calculation

`GET /api/v1/analytics/uptime` over the window `[from, to)` (default last 24 hours):

```
uptime_percentage = successful_checks / total_checks * 100
```

over stored checks with `from <= checked_at < to`, rounded to 4 decimals. A check is
successful when it is healthy.

**Missing checks.** Windows with no checks (monitor paused, PulseCheck itself down,
history already purged) are neither up nor down: they are left out of the ratio, so
missing data never inflates downtime or uptime. If there are no checks at all,
`uptime_percentage` is `null`, not 0 and not 100. The gap is made visible instead:

```
expected_checks     = floor(seconds in window, clipped to [monitor created, now]) / interval_seconds)
coverage_percentage = min(total_checks / expected_checks, 1) * 100
```

Treat low coverage as "this number is based on little data". Retention (30 days) bounds
how far back analytics can see; the maximum window is 90 days.

Other fields: `total_checks`, `successful_checks`, `failed_checks`;
`avg_response_time_ms`, `p50`, `p95`, `p99` are computed with PostgreSQL
`percentile_cont` (linear interpolation) over checks that recorded a response time, so
timeouts and connection failures are excluded from latency but included in uptime;
`incident_count` counts incidents overlapping the window and `total_incident_seconds`
sums their durations clipped to the window (open incidents run to now). Incident downtime
and check-based uptime are independent measures: the incident view lags by the failure
threshold (an incident only starts after N consecutive failures), the check view does not.

## Data retention

The scheduler deletes `monitor_checks` older than `RETENTION_DAYS` (default 30) every
`RETENTION_INTERVAL_SECONDS` (default hourly, and once at startup), in batches of 5000
so locks stay short. Incidents and notifications are kept (small, and the audit trail).
Deleting a monitor cascades to all of its data. The cleanup is idempotent and safe to
run from several schedulers.

## Scheduler and worker recovery

- **Claiming.** The scheduler runs
  `SELECT ... WHERE is_active AND next_check_at <= now ORDER BY next_check_at LIMIT n FOR
  UPDATE SKIP LOCKED`, sets `next_check_at = now + interval`, pushes the jobs to Redis, and
  commits. Several schedulers can run: SKIP LOCKED means no monitor is claimed twice at the
  same time. `next_check_at` advances from *now*, so after a scheduler outage monitors
  get one catch-up check, not a burst.
- **Scheduler crash.** State is in PostgreSQL. A restarted scheduler resumes with whatever
  is due. Jobs are pushed before commit: if the commit fails the monitor stays due and may
  be queued twice, never zero times. If Redis is down, the claim transaction rolls back
  and the monitors are retried on the next tick.
- **Reliable queue.** A worker moves a job from the queue list to a processing list
  (`BLMOVE`) and removes it when done. If a worker dies mid-check, the job stays in
  processing; the scheduler returns jobs older than `STALE_JOB_SECONDS` (180) to the queue.
  A graceful stop (SIGTERM) lets in-flight checks finish.
- **Safe to retry.** A per-monitor Redis lock (`SET NX EX`) makes a duplicate job for a
  monitor that is already being checked get dropped. Results are applied under the
  monitor row lock. At worst, a duplicate or requeued job produces one extra check row.
- **Redis data loss.** Queue contents are lost but nothing durable is: `next_check_at`
  is already advanced, so each monitor is simply checked again one interval later.
- **Paused or deleted mid-flight.** Workers skip inactive monitors before checking and the
  state machine discards late results.
- **Health.** Each worker writes a Redis heartbeat key (30s TTL, refreshed every 10s);
  the container healthchecks read it (`python -m app.healthcheck <name>`), and
  `restart: unless-stopped` restarts crashed processes.
- **Scale-out.** `docker compose up -d --scale check-worker=3` adds workers;
  notification workers also scale because rows are claimed with SKIP LOCKED.

## Project layout

```
app/
  main.py  config.py  db.py  models.py  schemas.py  security.py  deps.py  logging.py
  api/         auth.py  monitors.py  analytics.py
  services/    ssrf.py  checker.py  state.py  scheduling.py  queue.py
               notifier.py  analytics.py  retention.py
  workers/     scheduler.py  check_worker.py  notification_worker.py
alembic/       migrations
tests/         pytest suite (222 tests)
tools/         target_server.py (test target), smoke_test.py
```

## Tests

`uv run pytest` covers: valid and invalid monitor URLs; healthy, failing, timed-out and
refused endpoints; unexpected status codes; consecutive-failure and recovery thresholds;
duplicate-incident prevention (sequential and concurrent); paused and deleted monitors;
concurrent scheduling and check claiming (8 parallel claimers over 40 monitors); queue
recovery after a worker dies; notification deduplication, retry backoff, exhaustion and
permanent failure; ownership and authorization on every monitor route; SSRF (address
matrix, DNS rebinding, IP pinning, redirects, metadata with the dev override); analytics
and percentiles; pagination and date-range validation; retention; correlation IDs and log
redaction. Unit tests use `httpx.MockTransport`; integration tests use a real local HTTP
server and the real PostgreSQL and Redis.
