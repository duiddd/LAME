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

## Operating notes

- Reset A's and D's data together. `notify_seq` restarts when D's data is reset, and A would take
  every notification for a duplicate, leaving holds frozen.
- `lame_a_notify_failures_total` rising and `lame_a_notify_cursor_age_seconds` growing mean a
  notification stopped the line. Settlement waits until it is fixed. A never skips one.
- Seeded users are `user<ID>`, with the password in `SEED_PASSWORD`. Set `SEED_BALANCES=false` to
  seed them without funds.
- Price and quantity decimals in `config/symbols.json` are provisional until the team defines them.
