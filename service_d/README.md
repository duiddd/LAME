# Service D — Risk Control

The risk gate between A and B. Every order from A is checked by two rules (self-trade,
wash trade) before it may reach B's matching engine. D also forwards every outcome
(risk decision, B's results, trades, cancel results) to A as ordered notifications.

Design: `docs/service_d_architecture.md`, `docs/service_d_protocol.md`,
`docs/service_d_tech_decisions.md`.

## Layout

| Path | What it is |
| --- | --- |
| `d/api.py` | HTTP interface on :8400 |
| `d/engine.py` | Orders, decisions, cancels, timeouts, B outputs → notifications |
| `d/rules.py` | The two rules, pure functions |
| `d/bclient.py` | The one WebSocket to B's `/d`: resume, dedup, chain order, catch-up |
| `d/notifier.py` | Outbox sender to A's `POST /internal/order-events` |
| `d/store.py` | SQLite (WAL): orders, decisions, id map, cancels, outbox |
| `config/` | Linked accounts and trading pairs (to be shared with A and B) |
| `rules/v1.json` | Rule switches; its name and hash give `rule_version` |
| `d/book.py` | D's book view from B's outputs, shared by the engine and the replay |
| `d/replay.py` | Replay tool: recompute every rule decision and compare (acceptance criterion 1) |
| `d/evaluate.py` | Precision, recall, false positive rate and decision latency from bot labels (criterion 2) |
| `tools/loadtest.py` | N orders per second for T seconds; reply and decision p50/p99 (criterion 5) |
| `tools/fault.py` | Triggers the failure scenarios; see `RUNBOOK.md` |
| `tests/` | Unit tests and end-to-end tests against a fake B and a fake A |

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
B_CTRL_URL=ws://localhost:11659/d A_NOTIFY_URL=http://localhost:8000/internal/order-events .venv/bin/python -m d.main
```

Tools:

```bash
.venv/bin/python -m d.replay --d-db data/d.db --b-events /path/to/events-2026-11-05.db
.venv/bin/python -m d.evaluate --d-db data/d.db --labels labels.jsonl
.venv/bin/python -m tools.loadtest --local --rate 200 --seconds 10
.venv/bin/python -m tools.fault status
```

A local load test (fake B and fake A in the same process) on a laptop gave a decision p99
of 1 ms at 200 orders/s and 2 ms at 1000 orders/s. These numbers do not include B or the network.

## Endpoints

| Method and path | Purpose |
| --- | --- |
| `POST /v1/orders` | Place an order. `202` = received; `503` = not taken, no record kept |
| `POST /v1/cancels` | Cancel; the result arrives as a `CANCEL_RESULT` notification |
| `POST /v1/modifies` | **Not implemented yet** (`501`), pending the team discussion |
| `GET /v1/orders/{order_id}?user_id=` | Order state, for A's reconciliation |
| `POST /v1/notifications/rewind` | Resend notifications from `from_seq` |
| `GET /healthz`, `GET /metrics` | Readiness and Prometheus metrics |
| `POST /admin/faults` | Fault switches, only with `FAULTS_ENABLED=true` |

## Configuration

`D_PORT` (8400), `B_CTRL_URL`, `A_NOTIFY_URL`, `DATA_DIR`, `LINKED_ACCOUNTS_FILE`,
`SYMBOLS_FILE`, `RULES_FILE`, `MAX_COMMAND_AGE_MS` (must equal B's, 1000),
`D_TIMEOUT_MS` (≥ `MAX_COMMAND_AGE_MS`), `CANCEL_MARGIN_MS`, `GATE_DEADLINE_MS`,
`DECISION_QUEUE_CAP`, `CATCHUP_QUIET_MS`, `GAP_TIMEOUT_MS`, `FAULTS_ENABLED`.

## Open items

- Modify (cancel + new order inside D): waiting for the team discussion.
- Caught-up detection: B sends no "resend finished" marker and does not mark live
  records, so D treats itself as caught up when its output chain is contiguous and
  nothing new has arrived for `CATCHUP_QUIET_MS`. A marker from B would make this exact.
- `config/` should become one shared folder read by A, B and D.
- Not yet tested against B's real engine, and not yet in the root `docker-compose.yml`.
- The replay does not cover modify yet (modify is not implemented).
