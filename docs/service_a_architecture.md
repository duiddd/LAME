# Service A: Trading & Accounts — Architecture

**Owner:** Pin-Yun Kuo · **The one question it answers:** Are balances and assets correct?

This document describes the internal design of Service A. The rationale behind each choice is recorded in [service_a_tech_decisions.md](service_a_tech_decisions.md) (decision IDs such as D1 are referenced below). How A talks to the other services (protocols, message formats, who forwards what) will be agreed by the whole team later; this document only states what A needs from those channels, through three ports (section 3), and lists the open questions in section 13.

## 1. Role and scope

A is the user-facing entry point of the exchange and the single authority for balances, holds and order state.

- **In scope:** the trading API and gateway; synthetic multi-user accounts and a ledger; synchronous balance holds; pre-checks (rate limiting, order size, price protection); order state and order-fate resolution; idempotent settlement; load shedding; pushing each user's own updates to the frontend; the frontend application shell.
- **Out of scope:** matching (B), behavioural risk rules such as self-trade and wash trading (D), news and Q&A (C), real authentication, deposits and withdrawals, leverage and margin, fees, stop orders, native order modification (a modify is a cancel plus a new order).

## 2. Interfaces to users

All amounts on the API are decimal strings; inside A they are integers (D8). Identity is a synthetic `X-User-Id` header; there is no real authentication.

| Endpoint | Method | Description |
| --- | --- | --- |
| `/api/orders` | `POST` | Place an order. Requires an `Idempotency-Key` header (the `client_request_id`). Body: `symbol`, `side`, `type` (LIMIT / MARKET), `price` (LIMIT only), `qty` |
| `/api/orders/{order_id}` | `DELETE` | Cancel an order |
| `/api/orders/{order_id}` | `PUT` | Modify: cancel the old order, then place a new one with a new `Idempotency-Key` |
| `/api/orders` | `GET` | Open orders and order history (`?status=open`) |
| `/api/orders/{order_id}` | `GET` | One order, including its fills |
| `/api/fills` | `GET` | The user's fills |
| `/api/accounts/me` | `GET` | Available balance, held balance and holdings per asset |
| `/api/stream` | `GET` | Server-Sent Events: the user's own `order_update` and `balance_update` (D14) |
| `/healthz` | `GET` | Ready once the database is reachable and migrations have run |
| `/metrics` | `GET` | Prometheus metrics (section 11) |

Rejections carry a machine-readable `reason`: `RATE_LIMITED` (429), `INVALID_TICK`, `INVALID_LOT`, `QTY_TOO_SMALL`, `QTY_TOO_LARGE`, `NOTIONAL_TOO_LARGE`, `PRICE_OUT_OF_BAND`, `NO_FRESH_PRICE`, `INSUFFICIENT_BALANCE`, `SYSTEM_BUSY` (503), `RISK_REJECTED` (with D's reason).

Market data does not pass through A: the frontend takes it directly from the market data feed.

## 3. Ports to other services

A's core logic depends only on these interfaces. Each will get one adapter once the team agrees on the communication design (D3).

| Port | What A needs | Semantics A relies on |
| --- | --- | --- |
| `RiskGatewayPort` | Submit a new order or a cancel, identified by A's `order_id` | Each call ends in one of three outcomes: **passed** (handed on towards matching), **rejected** (with a reason), or **unknown** (timeout or connection lost after sending). A repeated `order_id` is handled idempotently downstream |
| `OrderOutcomeSource` | Order events: ACCEPTED, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED (including a rejected cancel), EXPIRED, END_OF_DAY cancels | Ordered as the matching engine produced them; carries `order_id` and the cumulative filled quantity; carries a contiguous number so A can detect gaps and request a resend |
| `TradeSource` | Trades: symbol, price, qty, taker and maker user and order | Each trade has a unique identifier; a gap or missing trade can be detected and resent |
| `MarketPriceSource` | A reference price per symbol (best bid, best ask, last price) with its timestamp | Used only for price protection (D11); stale data is detectable |

A also has to supply D with linked-account data from its seed (D16). The form is open.

## 4. Architecture

A is one Python process (FastAPI on uvicorn, a single worker, D2) with a PostgreSQL database.

```
Browser ──REST──► [api] ─► [precheck: rate limit → size → price protection]  ── fail → 4xx, nothing written
                               │ pass
                               ▼
                   [breaker / shedding check] ── open or shedding → 503 SYSTEM_BUSY, nothing written
                               │
                               ▼
                   [order + ledger] DB TX: insert order PENDING, hold funds, ledger entry
                               │
                               ▼
                   [riskclient] ──RiskGatewayPort──► (D)
                      ├─ passed   → SUBMITTED
                      ├─ rejected → DB TX: release hold, REJECTED
                      └─ unknown  → UNKNOWN (hold kept) ─► [fate] cancel with backoff
                               
(order events) ─OrderOutcomeSource─► [events] dedupe, gap check ─► [order + ledger] DB TX
(trades)       ─TradeSource───────►  [events] dedupe, gap check ─► [order + ledger] DB TX
                                                                        │
Browser ◄──SSE── [push] ◄───────────────────────────────────────────────┘
```

| Module | Responsibility | Interacts with | Edge cases |
| --- | --- | --- | --- |
| `api` | Routes, Pydantic validation, decimal ↔ integer conversion | precheck, order | Price or qty off the tick/lot grid |
| `precheck.ratelimit` | Per-user token bucket (D9) | api | Burst, refill after idle, reset on restart |
| `precheck.size` | Lot multiple, min/max qty, max notional per symbol (D10) | shared symbol config | qty ≤ 0; market orders priced at the protection price |
| `precheck.price` | Price band for limit orders, freshness and cap for market orders (D11) | `MarketPriceSource` | No fresh price → reject market orders |
| `ledger` | Hold, release, settle; one ledger entry per change (D6, D7) | order, events | Insufficient balance; repeated effects |
| `order` | Order state machine (section 5) | ledger, push | Illegal transitions are refused and logged |
| `riskclient` | Calls `RiskGatewayPort` with timeouts, circuit breaker, latency samples (D12) | shedding | A timeout is *unknown*, never *rejected* |
| `shedding` | Sliding-window p99 of risk-gateway latency; decides whether to accept new orders (D13) | riskclient, api | Hysteresis to avoid flapping |
| `fate` | Resolves `UNKNOWN` orders by repeated cancel (D5) | riskclient, events | Waits indefinitely if downstream is down |
| `events` | Consumes order events and trades; deduplication, gap detection, cursors (D7) | ledger, order | A new trading day resets upstream numbering |
| `push` | SSE per user (D14) | order, ledger | Snapshot on reconnect |
| `seed` | Synthetic users, initial assets, linked accounts (D16) | — | Deterministic on every start |
| `metrics` | Prometheus exposition (section 11) | — | — |

## 5. Order lifecycle

### 5.1 States

```
                 D rejects                   
   PENDING ───────────────► REJECTED (hold released)
      │ passed                      
      ├───────► SUBMITTED ──► OPEN ──► PARTIALLY_FILLED ──► FILLED
      │                         │              │
      │ unknown                 └──────┬───────┘
      └───────► UNKNOWN ───────────────┴──► CANCELLED / REJECTED / EXPIRED
                  (hold kept, cancel retried)
```

- Terminal statuses: FILLED, CANCELLED, REJECTED, EXPIRED. An END_OF_DAY cancel is CANCELLED with that reason.
- Each order also has `settlement_complete`. An order is **closed** only when both hold: its status is terminal, **and** the quantity settled from trades equals its cumulative filled quantity (D4).
- A user cancel on an `OPEN` order goes through `RiskGatewayPort`. On an `UNKNOWN` order it is recorded as intent; `fate` is already sending a cancel.

### 5.2 Releasing a hold

Funds are released only from order events and trades, never from a timeout (D5):

| Input | Effect on the hold |
| --- | --- |
| Rejection from the risk gateway | Release the whole hold |
| Terminal order event (CANCELLED / REJECTED / EXPIRED, including END_OF_DAY) | Release the hold for the **unfilled** quantity only; the hold for the filled quantity stays until its trades are settled |
| FILLED | Nothing is released yet; all of it waits for settlement |
| A rejected cancel saying the order was not found | The order never reached matching or has already ended. Since order events are ordered, any earlier fill has already been seen, so the remaining hold is released |
| A trade | Settle that quantity (5.3). It never releases the unfilled remainder |

Example: a limit buy of 3 at 100 holds 300. One unit fills, then the order is cancelled. If CANCELLED (filled 1, remaining 2) arrives before the trade, A releases 200 and keeps 100 for the trade. When the trade arrives at 98, A settles 98 and releases 2.

### 5.3 Settling a trade

One trade is settled for both sides in one transaction:

- **Buyer:** held quote decreases by `qty × limit price` (or by its share of the market-order hold); available quote increases by the price improvement `qty × (limit price − trade price)`; available base increases by `qty`.
- **Seller:** held base decreases by `qty`; available quote increases by `qty × trade price`.
- There are no fees.

The same transaction records the trade's identifier and advances the stream cursor, so a repeated trade has no effect (D7).

### 5.4 Restart

On start-up, an order still `PENDING` may or may not have been sent before the crash, so it becomes `UNKNOWN` and is handled by `fate`. Cursors are read from the database, and consumption resumes from them.

## 6. Accounts and ledger

Tables (illustrative, not final):

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `accounts(user_id, asset, available, held)` | Current balances | `CHECK (available >= 0 AND held >= 0)` |
| `ledger_entries(id, user_id, asset, delta_available, delta_held, reason, ref_type, ref_id, created_at)` | Every balance change | Append-only; `UNIQUE (ref_type, ref_id, user_id, asset, reason)` makes each effect idempotent |
| `orders(order_id, user_id, client_request_id, symbol, side, type, price, qty, filled_qty, settled_qty, held_amount, status, reason, ...)` | Order state | `PRIMARY KEY (order_id)`; `UNIQUE (user_id, client_request_id)` |
| `applied_trades(trade_key)` | Trades already settled | `PRIMARY KEY (trade_key)` |
| `stream_cursors(stream, day, last_no)` | Last applied number per input stream | One row per stream |
| `linked_accounts(group_id, user_id)` | Seeded account links for the wash-trading scenarios | — |

A hold is a single conditional update: `UPDATE accounts SET available = available - :amt, held = held + :amt WHERE user_id = :u AND asset = :a AND available >= :amt`. Zero rows updated means insufficient balance (D6). The order insert, the hold and its ledger entry commit in one transaction.

Invariant checked by tests: for each asset, the sum of `available + held` over all users equals the seeded total plus the net effect of settled trades, which is zero per asset because spot trades only move assets between users.

## 7. Pre-checks

Run in order; the first failure rejects the request before anything is written.

1. **Rate limit (D9).** One token bucket per user, in memory: an initial rate of 5 orders per second with a burst of 10. Cancels draw from a separate, larger bucket so that a user can always withdraw orders. The values are configuration, to be tuned from the Week 6 baseline.
2. **Size (D10).** Per symbol, from the symbol config shared with the matching engine: `tick`, `lot`, `min_qty`, `max_qty`, `max_notional`. The price must be a multiple of the tick and the qty a multiple of the lot.
3. **Price protection (D11).** A limit price must lie within ±10% of the reference price. A market order needs a reference price no older than 3 seconds; it is valued at a protection price (best ask × 1.05 for a buy, best bid × 0.95 for a sell) for the notional check and for the hold.
4. **Balance.** Checked atomically by the hold itself (section 6), not by a separate read.

## 8. Idempotency

Three layers, each against a different duplicate (D4):

| Layer | Duplicate it prevents | Key | Where enforced |
| --- | --- | --- | --- |
| Request | A double click or a resent HTTP request | `client_request_id` from the frontend (`Idempotency-Key`) | `UNIQUE (user_id, client_request_id)`; a duplicate returns the existing order |
| Order | The same order submitted twice towards matching after a retry | `order_id`, a ULID generated by A, carried unchanged through risk control to matching | Generated once per order; downstream deduplication on it is an open item (section 13) |
| Settlement | The same trade or order event applied twice | The trade's own unique identifier and the stream number, never `order_id` (one order can have several fills) | `applied_trades`, `ledger_entries` uniqueness and `stream_cursors`, all in the settlement transaction |

## 9. Resilience

- **Timeouts (D12).** Every risk-gateway call has a connect timeout and a total timeout. The total is A's share of the latency budget, set once the budget is fixed.
- **Retries.** A new order is retried only when the request certainly did not leave A (connection refused), with the same `order_id`. After a timeout it is not retried; it becomes `UNKNOWN`. Cancels are retried with exponential backoff (1 s, 2 s, 4 s, capped at 30 s) until a terminal order event arrives, because a repeated cancel is harmless.
- **Circuit breaker (D12).** Opens after 5 consecutive failures or timeouts, stays open for 10 s, then lets one trial request through. While it is open, new orders are rejected with `SYSTEM_BUSY` **before** any hold is placed. Cancels bypass it and are queued for retry instead.
- **Load shedding (D13).** A keeps a 30-second sliding window of risk-gateway latencies. If the p99 exceeds the risk gateway's share of the budget in three consecutive one-second evaluations, A rejects new orders with `SYSTEM_BUSY`; it resumes when the p99 falls below 80% of the threshold. Cancels are never shed. This keeps both of D's rules switched on under load, as the proposal's degraded-mode decision requires.

## 10. Consistency and failure model

- **Strongly consistent:** balances, holds and order acceptance, within one PostgreSQL transaction each.
- **Eventually consistent:** order status and settlement, which follow the input streams; the lag is measured (section 11).
- **Effectively once:** every input is applied in the same transaction that records its identifier and advances the cursor, so at-least-once delivery and upstream replays produce no double effects.

| Failure | Behaviour |
| --- | --- |
| Risk gateway slow | Load shedding rejects new orders; existing orders unaffected |
| Risk gateway down | Circuit breaker opens; new orders fail fast; `UNKNOWN` orders keep their holds and `fate` keeps retrying |
| Response lost after the order passed | Order becomes `UNKNOWN`; hold kept; cancel sent; released only from order events (5.2) |
| An input stream disconnects | Settlement lags; A reconnects and resumes from its cursor |
| A gap in an input stream | A stops applying that stream, requests the missing range, then continues |
| A trade missing although its order event arrived | Detected when the cumulative filled quantity exceeds the settled quantity for too long; A requests a resend |
| A crashes | Database is durable; `PENDING` orders become `UNKNOWN`; consumption resumes from the cursors |
| Upstream replays old events | Ignored by the uniqueness checks |
| PostgreSQL down | `/healthz` unhealthy; all writes fail with a clear error; no funds move |

We accept that funds stay frozen while downstream services are unavailable, in return for never settling a trade against released funds.

## 11. Metrics

| Metric | Meaning |
| --- | --- |
| `lame_a_orders_total{result}` | Orders by outcome: accepted, each precheck reason, `SYSTEM_BUSY`, risk rejected |
| `lame_a_http_request_duration_seconds{endpoint}` | API latency |
| `lame_a_risk_gateway_latency_seconds` | A's view of the risk-gateway hop (the input to load shedding) |
| `lame_a_circuit_state` / `lame_a_shedding_active` | Protection state over time |
| `lame_a_orders_unknown` | Orders waiting for their fate |
| `lame_a_settlement_lag_seconds{stream}` | Time from an event's creation to its settlement |
| `lame_a_stream_gaps_total{stream}` | Gaps detected and recovered |
| `lame_a_duplicates_ignored_total{layer}` | Duplicates caught by each idempotency layer |

## 12. Deployment

| Aspect | Design |
| --- | --- |
| Image | `python:3.12-slim`, multi-stage, pinned dependencies, non-root user |
| Process | `uvicorn` with **one worker** (D2) |
| Database | PostgreSQL container with a named volume; Alembic migrations run before the app reports healthy |
| Ports | 8000: API, SSE, `/healthz`, `/metrics` |
| Config | Mounted symbol config (shared with the matching engine); env vars for database URL, rate-limit values, timeouts, shedding threshold |
| Frontend | React + Vite + TypeScript, built to static files and served by an nginx container. A owns the shell, order entry, open orders and balances; C supplies the news and Q&A components (D15) |
| Seed | Loaded on first start; deterministic |

## 13. Open items for the cross-service discussion

These depend on how the services communicate, which the team will decide together. A only states what it needs.

1. **Adapters for the four ports** (section 3): transport, message format, timeout values.
2. **Where order events and trades come from**, and how a gap is recovered (which service resends, from which number).
3. **Contiguous numbering** for each input stream A consumes. The matching engine's current `seq` / `prev_seq` chain covers all of its outputs together, so a stream carrying only part of them needs its own contiguous number for gap detection.
4. **Deduplication on `order_id` downstream.** B's current design does not check repeated order IDs (B D2), so a retried order must be deduplicated before it reaches matching.
5. **Order-fate protocol.** The proposal describes an order lookup by ID and a cancel tombstone at B; B's current design has neither and relies on `MAX_COMMAND_AGE` instead (B D15). A's design in 5.2 needs a cancel for an unseen order to be answered, and a late arrival of that order to be refused, with `MAX_COMMAND_AGE` not greater than A's timeout.
6. **A price cap for market orders.** Without one, a market buy on a thin book can cost more than A held for it.
7. **Linked-account data for D:** form and channel.
8. **Ownership of the root `docker-compose.yml`:** B's document says A, C's document and the proposal say C.
9. **Trading day:** A needs to know the day boundary because upstream numbering restarts each day.

## 14. How the design meets the acceptance criteria

| Criterion (proposal) | Mechanism | Test |
| --- | --- | --- |
| Balances, holds and ledger stay consistent under concurrent trading | Conditional-update holds; settlement in one transaction; ledger invariant | 100 concurrent orders against one account: total held ≤ balance; ledger sums match |
| Duplicate requests or notifications never take effect twice | Three idempotency layers (section 8) | Same `Idempotency-Key` twice → one order; same trade twice → one settlement |
| No hold released before the order's fate is known | `UNKNOWN` state; release only from order events (5.2) | Drop the response after the order passed; order fills later from held funds |
| A detects a gap and recovers | Stream numbers and cursors; filled-vs-settled check | Drop one trade; drop one order event; balances converge |
| A user completes the full flow in the UI | React shell, REST, SSE | Manual and scripted end-to-end run |
| Rate limiting and size limits behave as configured | Token bucket; per-symbol limits | 20 orders in one second → 10 pass, rest `RATE_LIMITED`; each size violation returns its reason |
| Load shedding protects the gate | Sliding-window p99 with hysteresis | Inject delay in the risk gateway → `SYSTEM_BUSY`; remove it → recovery |
