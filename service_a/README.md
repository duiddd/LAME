# Service A — Trading & Accounts

The user-facing entry point: users, balances, holds, orders, settlement from D's notifications,
candles and the event stream. Design: `docs/service_a_architecture.md`, `docs/service_a_tech_decisions.md`.

## Layout

| Path | What it is |
| --- | --- |
| `app/auth` | Register, login, JWT access tokens, refresh rotation, Argon2id |
| `app/accounts` | The only module that writes balances, and the ledger |
| `app/orders` | Placing, cancelling, modifying, the order state |
| `app/orders/precheck` | Rate limit, size, price band and freshness |
| `app/orders/dclient` | D's HTTP API, failure classification, circuit breaker |
| `app/orders/events` | `POST /internal/order-events` and the transition table |
| `app/orders/reconcile` | Resends, status queries, the metrics that need a scan |
| `app/market` | B's market data, the price cache, candles, day open |
| `app/push` | The per-user event log and the SSE stream |
| `app/shedding.py`, `app/supervisor.py` | Load shedding; keeping background tasks alive |
| `app/container.py` | The composition root: everything with state is built here once |
| `migrations/` | Alembic |
| `config/symbols.json` | Pairs, limits and decimals. `tick` and `lot` must match D's and B's |
| `tools/export_openapi.py` | Writes `openapi.json`, which the frontend generates its types from |
| `tools/integration_check.py` | Real A against real D, with D's fake of B |

## Run

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # on Linux or macOS: .venv/bin/pip

# Tests need a PostgreSQL; TEST_DATABASE_URL defaults to port 55432, user lame, database lame_test.
.venv/Scripts/python -m pytest -q

# A and its database on their own, with Docker:
docker compose -f service_a/docker-compose.yml up --build
```

### In the team's compose file

`docker-compose.yml` in this folder is written to be included by the root compose file, so each
owner keeps their own file and the root lists them:

```yaml
include:
  - service_a/docker-compose.yml
  - frontend/docker-compose.yml
```

A is `service-a` on port 8000, reachable only inside the project network; the frontend's nginx
on port 3000 is the way in for browsers. A expects D at `service-d:8400` and B at
`service-b:9999`, so those are the service names D's and B's files need to use. `JWT_SECRET` has
a development default so that one command starts everything; set it for anything shared.

The integration check needs a checkout of `feature/risk_control`. On Windows, D also needs the
`tzdata` package.

```bash
D_SOURCE=/path/to/service_d PYTHONPATH=. .venv/Scripts/python -m tools.integration_check
```

## Run all services locally

`tools/run_local.ps1` checks out each teammate's branch into a worktree under `..\LAME-local`
(next to this repository), then runs B and PostgreSQL in Docker and A, C, D and the frontend as
local processes. Nothing is merged and no teammate's code is changed. It needs Docker Desktop,
Python 3.12, Node and Git; it works from cmd or PowerShell:

```bash
powershell -ExecutionPolicy Bypass -File service_a\tools\run_local.ps1
```

| Option | What it does |
| --- | --- |
| (none) | Fetch, update the worktrees, install what is missing, start everything, wait until healthy |
| `-NoUpdate` | Start without fetching or moving the worktrees |
| `-Status` | Show which services answer |
| `-Check` | Run `tools/local_e2e.py`: orders through the real D and B, settlement, modify, cancel, market buy |
| `-Stop` | Stop the processes and the containers; data is kept |
| `-Reset` | Stop and erase A's, B's, C's and D's data together |

C runs on 8081 with RabbitMQ off, because 8080 and 5672 are often taken by other Windows
services. The first start compiles B in a `rust:1.90` container, which takes about 6 minutes.

## Runbook

**Observe.** `GET /healthz` reports the database, the background tasks, the circuit breaker (0
closed, 1 half open, 2 open) and load shedding. `GET /metrics` exports A's series; the ones that
show trouble first:

| Metric | What it tells you |
| --- | --- |
| `lame_a_orders_total{result}` | Orders by outcome; `SYSTEM_BUSY` rising means D is down or slow |
| `lame_a_order_completion_seconds` | Placing to D's first answer: the input to load shedding |
| `lame_a_circuit_state`, `lame_a_shedding_active` | Whether A is refusing orders to protect D |
| `lame_a_orders_by_status{status="UNKNOWN"}` | Orders sent without a reply, holding funds |
| `lame_a_notify_cursor_age_seconds`, `lame_a_notify_failures_total` | Settlement stalled or stopped |
| `lame_a_orders_unsettled` | Finished orders still waiting for a trade |
| `lame_a_md_age_seconds{symbol}` | Time since B's last market data |

**Break it, and what recovery looks like.** Each was run against the real D and B; the measured
numbers are in `docs/submission/report_service_a.md`.

| Break | How | Expected | Recovers by |
| --- | --- | --- | --- |
| D down | Stop D's process or container | New orders `503 SYSTEM_BUSY`, nothing held; after 5 failures the breaker opens and refusals are immediate | Start D: it resumes from B, the breaker lets a trial through after 10 s |
| D slow | D's `POST /admin/faults` with `decision_delay_ms` (needs `FAULTS_ENABLED=true` on D) | Load shedding refuses new orders; cancels still pass | Remove the delay; shedding ends below 80% of its threshold |
| A missed a notification | D's `skip_next_notification` fault | A answers the next one `409` with `expected_seq`; D resends | Automatic |
| B's market data stops | Stop B | Orders refused `NO_FRESH_PRICE` after 3 s; the frontend marks the book interrupted | Start B |
| A crashes mid-order | Kill A's process | On restart, `PENDING` orders become `UNKNOWN` and are resent or cancelled; D resends unconfirmed notifications, which the cursor applies once | Start A |

| Tool | What it does |
| --- | --- |
| `tools/fault_demo.py` | Steady traffic while D is killed, slowed and B stopped; marks each fault on Grafana |
| `tools/consistency_demo.py` | Double submit, lost notification, A crash with D's outbox pending, no reply from D; then checks the books |
| `tools/measure_local.py` | Latency, and the D-down test on its own |
| `tools/bench_notifications.py` | How fast notifications are applied |
| `tools/screenshot.py` | A dashboard as PNG over a fault run's time window |

The results of the 20 Nov run on the compose system are in `docs/submission/fault_injection.md`.

## Operating notes

- Reset A's and D's data together. `notify_seq` restarts when D's data is reset, and A would take
  every notification for a duplicate, leaving holds frozen.
- `lame_a_notify_failures_total` rising and `lame_a_notify_cursor_age_seconds` growing mean a
  notification stopped the line. Settlement waits until it is fixed. A never skips one.
- Seeded users are `user<ID>`, with the password in `SEED_PASSWORD`. Set `SEED_BALANCES=false` to
  seed them without funds.
- `config/symbols.json` follows B's `match_engine/config/pairs.json`: 6 quantity decimals, BTC lot
  0.0001, ETH lot 0.001, USDT 6 decimals. `tests/test_pairs.py` checks the three files agree. A
  change of units needs A's, B's and D's data reset together (`run_local.ps1 -Reset`).
