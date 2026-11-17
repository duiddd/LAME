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

# With Docker:
JWT_SECRET=<a long random value> docker compose up --build
```

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
powershell -ExecutionPolicy Bypass -File service_a	oolsun_local.ps1
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

## Operating notes

- Reset A's and D's data together. `notify_seq` restarts when D's data is reset, and A would take
  every notification for a duplicate, leaving holds frozen.
- `lame_a_notify_failures_total` rising and `lame_a_notify_cursor_age_seconds` growing mean a
  notification stopped the line. Settlement waits until it is fixed. A never skips one.
- Seeded users are `user<ID>`, with the password in `SEED_PASSWORD`. Set `SEED_BALANCES=false` to
  seed them without funds.
- Price and quantity decimals in `config/symbols.json` are provisional until the team defines them.
