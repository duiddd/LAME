# Service A: Trading & Accounts — Architecture

**Owner:** Pin-Yun Kuo · **The one question it answers:** Are balances and assets correct?

This document describes the design of Service A. The rationale behind each choice is recorded in [service_a_tech_decisions.md](service_a_tech_decisions.md) (decision IDs such as D1 are referenced below). Items that need changes in other services' documents are listed in section 14.

## 1. Role and scope

A is the user-facing entry point of the exchange and the single authority for users, balances, holds and order state.

- **In scope:** user registration and login; simulated deposits; accounts, holdings and a ledger; synchronous balance holds; pre-checks (rate limiting, order size, price protection); placing, modifying and cancelling orders through D; order state, settlement and reconciliation from D's notifications; candles and the market summary; pushing each user's own updates to the frontend; the frontend application shell and the trading page.
- **Out of scope:** matching (B), behavioural risk rules (D), news, Q&A and the chat box (C), identity verification (KYC), password recovery, real personal data, withdrawals, leverage and margin, fees, stop orders.

## 2. Interfaces to users

All amounts on the API are decimal strings; inside A they are integers (D8). Every endpoint except register and login requires the token returned by login (D20).

| Endpoint | Method | Description |
| --- | --- | --- |
| `/api/auth/register` | `POST` | `username`, `password`. No email or other personal data |
| `/api/auth/login` | `POST` | `username`, `password` → token |
| `/api/auth/logout` | `POST` | Invalidate the token |
| `/api/accounts/me` | `GET` | Available, held and total balance per asset (also the "holdings" tab) |
| `/api/accounts/deposits` | `POST` | Simulated deposit: `asset`, `amount`, `Idempotency-Key` header. Credited at once (D26) |
| `/api/orders` | `POST` | Place an order (section 7.1). Requires an `Idempotency-Key` header (the `client_order_id`) |
| `/api/orders/{order_id}` | `PUT` | Modify an order: new `price` and/or `qty`, same `order_id` (D24) |
| `/api/orders/{order_id}` | `DELETE` | Cancel an order |
| `/api/orders` | `GET` | Open orders (`?status=open`) and order history |
| `/api/orders/{order_id}` | `GET` | One order, including its fills |
| `/api/fills` | `GET` | The user's fills (the "trade history" tab) |
| `/api/market/candles` | `GET` | `symbol`, `interval` (`5s`, `15s`, `1m`, `5m`) → OHLCV candles (D22) |
| `/api/market/summary` | `GET` | Per symbol: day open price and price precision (D23) |
| `/api/stream` | `GET` | Server-Sent Events: the user's own `order_update` and `balance_update` (D14) |
| `/healthz` | `GET` | Ready once the database is reachable and migrations have run |
| `/metrics` | `GET` | Prometheus metrics (section 12) |

Rejections carry a machine-readable `reason`: `RATE_LIMITED` (429), `INVALID_TICK`, `INVALID_LOT`, `QTY_TOO_SMALL`, `QTY_TOO_LARGE`, `NOTIONAL_TOO_LARGE`, `PRICE_OUT_OF_BAND`, `NO_FRESH_PRICE`, `INSUFFICIENT_BALANCE`, `SYSTEM_BUSY` (503), `UNAUTHORIZED` (401), `USERNAME_TAKEN` (409).

## 3. Interfaces to other services

| Link | Direction | Method | Content |
| --- | --- | --- | --- |
| Commands | A → D | HTTP + JSON | Place, modify and cancel orders. D's synchronous reply is an **acknowledgement of receipt only**; it does not mean the order has been checked or sent to B (D3) |
| Status query | A → D | HTTP + JSON | The order's state as D knows it; used only for reconciliation (D5) |
| Notifications | D → A | HTTP + JSON, A's endpoint `POST /internal/order-events` | D calls A for every outcome: risk rejection, B accepted (with B's `day` and `order_seq`), trades, cancels, rejections, expiry, end of day (D21) |
| Market data | B → browser, B → A | WebSocket `ws://<B>:9999/md/<symbol>` | Full snapshots of the top three bid and ask levels plus `last_price`. The browser draws the order book from it; A uses it for price protection (D11) |
| Linked accounts | A → D | To be decided (endpoint or shared seed file) | Seeded account groups for D's wash-trading rule (D16) |
| News | browser → C | HTTP, C's `GET /api/news?symbol=` | Shown in the "market news" panel of the trading page |

### 3.1 Notification contract A requires from D

- Each notification carries a contiguous `notify_seq` assigned by D, and, when it comes from B, B's `(day, seq)`.
- D sends notifications **one at a time, in order**, and resends the same one until A answers 2xx.
- A answers `200` after committing, `200` again for a duplicate (`notify_seq` already applied), and `409` with `expected_seq` when a notification arrives ahead of a gap; D then resends from `expected_seq`.
- Notification types A handles: `RISK_REJECTED`, `ACCEPTED` (B's first result, carrying `day` and `order_seq`), `TRADE`, `ORDER_UPDATE` (CANCELLED / REJECTED / EXPIRED / FILLED, with the cumulative filled quantity and a reason such as `END_OF_DAY` or `SELF_TRADE_PREVENTED`), `CANCEL_RESULT` (including `TOMBSTONED` and `ORDER_NOT_FOUND`), `MODIFY_RESULT`.

## 4. Architecture

A is one Python process (FastAPI on uvicorn, a single worker, D2) with a PostgreSQL database.

```
Browser ──REST──► [auth] ─► [api] ─► [precheck: rate limit → size → price protection] ── fail → 4xx
                                          │ pass
                                          ▼
                              [breaker / shedding check] ── open or shedding → 503 SYSTEM_BUSY
                                          │
                                          ▼
                              [order + ledger] DB TX: order PENDING, hold funds, ledger entry
                                          │
                                          ▼
                              [dclient] ──HTTP──► D   ack → SENT  |  no ack → UNKNOWN (hold kept)
                                                  │
                     D ──HTTP POST /internal/order-events──► [notifications] dedupe, order check
                                                                     │
                                                                     ▼
                                                [order + ledger] DB TX: status, settle, release
                                                                     │
Browser ◄──SSE── [push] ◄────────────────────────────────────────────┘
Browser ◄──WS 9999── B (order book)          [reconcile] queries D for orders stuck in SENT
Browser ──HTTP──► C (news, chat box)         [market] candles from settled trades
```

| Module | Responsibility | Interacts with | Edge cases |
| --- | --- | --- | --- |
| `auth` | Register, login, logout, token check (D20) | api | Duplicate username; wrong password; expired token |
| `api` | Routes, Pydantic validation, decimal ↔ integer conversion | precheck, order | Price or qty off the tick/lot grid |
| `precheck.ratelimit` | Per-user token bucket (D9) | api | Burst, refill after idle, reset on restart |
| `precheck.size` | Lot multiple, min/max qty, max notional per symbol (D10) | shared symbol config | qty ≤ 0; market orders priced at the protection price |
| `precheck.price` | Price band for limit orders, freshness and cap for market orders (D11) | B market data | No fresh price → reject market orders |
| `ledger` | Deposit, hold, release, settle; one ledger entry per change (D6, D7) | order, notifications | Insufficient balance; repeated effects |
| `order` | Order state machine (section 5), modify (D24) | ledger, push | Illegal transitions are refused and logged |
| `dclient` | Calls D with timeouts, circuit breaker, latency samples (D12) | shedding | No ack is *unknown*, never *rejected* |
| `shedding` | Sliding-window p99 of D's ack latency; decides whether to accept new orders (D13) | dclient, api | Hysteresis to avoid flapping |
| `notifications` | Receives D's notifications; deduplication and gap handling (D21) | ledger, order, market | Duplicates; out-of-order arrival |
| `reconcile` | Resends unacknowledged orders; queries D about orders stuck in `SENT` (D5) | dclient | D down: keep waiting, keep the hold |
| `market` | Candles and day open from settled trades (D22, D23) | notifications | Intervals with no trades |
| `push` | SSE per user (D14) | order, ledger | Snapshot on reconnect |
| `seed` | Demo users and linked accounts (D16) | — | Deterministic on every start |
| `metrics` | Prometheus exposition (section 12) | — | — |

## 5. Order lifecycle

### 5.1 States

```
 PENDING ──D ack──► SENT ──ACCEPTED (B's order_seq)──► PROCESSING ──► PARTIALLY_FILLED ──► FILLED
    │                │                                      │                │
    │ no ack         │ RISK_REJECTED                        └───────┬────────┘
    ▼                ▼                                              ▼
 UNKNOWN          REJECTED (hold released)            CANCELLED / REJECTED / EXPIRED
 (hold kept; resend the same order_id, then cancel)
```

- `PENDING`: the hold is placed and the order is being sent to D.
- `SENT`: D has acknowledged receipt. The hold is **not** released; D has not decided yet.
- `PROCESSING`: D's notification says B accepted the order. From here A also knows B's `(day, order_seq)`, a second unique key for the order.
- Terminal statuses: FILLED, CANCELLED, REJECTED, EXPIRED. END_OF_DAY and SELF_TRADE_PREVENTED are reasons for CANCELLED.
- An order is **closed** only when its status is terminal **and** the quantity settled from trades equals its cumulative filled quantity (D4).
- `UNKNOWN`: the request to D got no acknowledgement. A resends the same `order_id` (D deduplicates it) with backoff for up to `RESEND_WINDOW`; if still no acknowledgement, A sends a cancel and waits for its result (D5).

### 5.2 Releasing a hold

Funds move only on D's notifications, never on a timeout or an acknowledgement (D5):

| Notification | Effect on the hold |
| --- | --- |
| `RISK_REJECTED` | Release the whole hold (D never sends a rejected order to B) |
| `ORDER_UPDATE` REJECTED / EXPIRED for the order itself | Release the whole hold |
| `ORDER_UPDATE` CANCELLED (including END_OF_DAY, SELF_TRADE_PREVENTED) | Release the hold for the **unfilled** quantity only; the hold for the filled quantity stays until its trades are settled |
| `ORDER_UPDATE` FILLED | Nothing is released yet; all of it waits for settlement |
| `CANCEL_RESULT` TOMBSTONED or ORDER_NOT_FOUND | The order never reached B and never will; release the remaining hold |
| `TRADE` | Settle that quantity (5.3); it never releases the unfilled remainder |

Example: a limit buy of 3 at 100 holds 300. One unit fills, then the order is cancelled. A releases 200 on CANCELLED (filled 1) and keeps 100 for the trade. When the trade at 98 is settled, A takes 98 and releases 2.

### 5.3 Settling a trade

One trade is settled for both sides in one transaction:

- **Buyer:** held quote decreases by `qty × limit price` (or by its share of the market-order hold); available quote increases by the price improvement `qty × (limit price − trade price)`; available base increases by `qty`.
- **Seller:** held base decreases by `qty`; available quote increases by `qty × trade price`.
- There are no fees.

The same transaction records B's `(day, seq)` for the trade and advances `notify_seq`, so a repeated notification has no effect (D7). The trade is also stored for candles (D22).

### 5.4 Modifying an order (D24)

`PUT /api/orders/{order_id}` sends the new `price` and/or `qty` to D under the same `order_id`; D decides how to carry it out towards B. `qty` is the new **remaining** quantity.

- The new values pass the same pre-checks as a new order.
- If the new values need **more** funds, A holds the difference before sending. If they need **less**, the difference is released only after `MODIFY_RESULT` confirms the modification, because the old order may still fill at its old size until then.
- While a modification is pending, the order shows `MODIFYING`; a rejected modification leaves the order as it was and releases any extra hold.

### 5.5 Restart

On start-up, an order still `PENDING` may or may not have reached D, so it becomes `UNKNOWN` and is handled by `reconcile`. The last applied `notify_seq` is read from the database; D's next notification continues from it.

## 6. Accounts and ledger

Tables (illustrative, not final):

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `users(user_id, username, password_hash, created_at)` | Registered and seeded users | `UNIQUE (username)` |
| `accounts(user_id, asset, available, held)` | Current balances | `CHECK (available >= 0 AND held >= 0)` |
| `ledger_entries(id, user_id, asset, delta_available, delta_held, reason, ref_type, ref_id, created_at)` | Every balance change (deposit, hold, release, settle) | Append-only; `UNIQUE (ref_type, ref_id, user_id, asset, reason)` makes each effect idempotent |
| `deposits(deposit_id, user_id, client_request_id, asset, amount, created_at)` | Simulated deposits | `UNIQUE (user_id, client_request_id)` |
| `orders(order_id, user_id, client_order_id, symbol, side, type, price, qty, filled_qty, settled_qty, held_amount, status, reason, b_day, b_order_seq, ...)` | Order state | `PRIMARY KEY (order_id)`; `UNIQUE (user_id, client_order_id)`; `UNIQUE (b_day, b_order_seq)` |
| `trades(b_day, b_seq, symbol, price, qty, taker_order_id, maker_order_id, ts)` | Settled trades; the source of candles | `PRIMARY KEY (b_day, b_seq)` |
| `notify_cursor(last_notify_seq)` | Last applied notification from D | One row |
| `linked_accounts(group_id, user_id)` | Seeded account links for the wash-trading scenarios | — |

A hold is a single conditional update: `UPDATE accounts SET available = available - :amt, held = held + :amt WHERE user_id = :u AND asset = :a AND available >= :amt`. Zero rows updated means insufficient balance (D6). The order insert, the hold and its ledger entry commit in one transaction.

New users start with zero balances and use simulated deposits (D26). Invariant checked by tests: for each asset, the sum of `available + held` over all users equals the seeded total plus all deposits, because spot trades only move assets between users.

## 7. Orders and pre-checks

### 7.1 Order request

| Field | Required | Notes |
| --- | --- | --- |
| `Idempotency-Key` header | Yes | The `client_order_id`, generated by the frontend |
| `symbol` | Yes | A pair in the shared symbol config |
| `side` | Yes | `BUY` / `SELL` |
| `type` | Yes | `LIMIT` / `MARKET` |
| `price` | LIMIT only | Must be absent for MARKET |
| `qty` | Yes | Positive, a multiple of the lot |

A adds `user_id` (from the token), `order_id` (a ULID) and `created_at`. There is no time-in-force choice: a limit order rests until it fills, is cancelled or is cancelled at the end of the day; a market order's unfilled remainder is cancelled by B.

### 7.2 Pre-checks

Run in order; the first failure rejects the request before anything is written.

1. **Rate limit (D9).** One token bucket per user, in memory: an initial rate of 5 orders per second with a burst of 10. Cancels draw from a separate, larger bucket so that a user can always withdraw orders. The values are configuration, to be tuned from the Week 6 baseline.
2. **Size (D10).** Per symbol, from the symbol config shared with B: `tick`, `lot`, `min_qty`, `max_qty`, `max_notional`. The price must be a multiple of the tick and the qty a multiple of the lot.
3. **Price protection (D11).** A limit price must lie within ±10% of the reference price. A market order needs a reference price no older than 3 seconds; it is valued at a protection price (best ask × 1.05 for a buy, best bid × 0.95 for a sell) for the notional check and for the hold.
4. **Balance.** Checked atomically by the hold itself (section 6), not by a separate read.

## 8. Idempotency

Three layers, each against a different duplicate (D4):

| Layer | Duplicate it prevents | Key | Where enforced |
| --- | --- | --- | --- |
| Request | A double click or a resent HTTP request | `client_order_id` from the frontend (`Idempotency-Key`); deposits use their own key | `UNIQUE (user_id, client_order_id)`; a duplicate returns the existing order |
| Order | The same order submitted twice after A resends it to D | `order_id`, a ULID generated by A, carried unchanged to D | D deduplicates on it; the same `order_id` with new values is a modification (D24) |
| Notification | The same notification or trade applied twice | `notify_seq`, and B's `(day, seq)` for trades; never `order_id` (one order can have several fills) | `notify_cursor`, `trades` primary key and `ledger_entries` uniqueness, all in the same transaction |

## 9. Reconciliation of orders stuck in SENT (D5)

D's notifications are the primary path. Reconciliation only covers a notification that never arrives.

- An order still in `SENT` after 10 seconds is queried at D; further queries follow at 20 s, 40 s and then every 60 s.
- If D reports an outcome A has not received, A treats it as a missing notification and requests a resend from its `notify_seq`.
- If D reports that it is still deciding or still waiting for B, A keeps waiting and shows the order as "processing delayed".
- The query result itself never releases funds; only the notification does.
- The user can cancel a `SENT` order at any time.

## 10. Resilience

- **Timeouts (D12).** Every call to D has a connect timeout and a total timeout. Because D only acknowledges receipt, the timeout covers the acknowledgement, not the risk decision.
- **Retries.** A new order without an acknowledgement is resent with the same `order_id` (safe, because D deduplicates it) with exponential backoff, for up to `RESEND_WINDOW`; after that A sends a cancel instead, so that an order is not placed long after the user asked. Cancels are resent until acknowledged.
- **Circuit breaker (D12).** Opens after 5 consecutive failures or timeouts, stays open for 10 s, then lets one trial request through. While it is open, new orders are rejected with `SYSTEM_BUSY` before any hold is placed. Cancels bypass it and are queued for resend.
- **Load shedding (D13).** A keeps a 30-second sliding window of D's acknowledgement latency. If the p99 exceeds D's share of the budget in three consecutive one-second evaluations, A rejects new orders with `SYSTEM_BUSY`; it resumes when the p99 falls below 80% of the threshold. Cancels are never shed.
- **Notification endpoint.** Idempotent; answers only after the database commit.

## 11. Consistency and failure model

- **Strongly consistent:** registration, deposits, balances, holds and order acceptance, each within one PostgreSQL transaction.
- **Eventually consistent:** order status and settlement, which follow D's notifications; the lag is measured (section 12).
- **Effectively once:** every notification is applied in the same transaction that advances `notify_seq`, so D's resends produce no double effects.

| Failure | Behaviour |
| --- | --- |
| D slow | Load shedding rejects new orders |
| D down | Circuit breaker opens; new orders fail fast; **settlement also pauses**, because every outcome reaches A through D; holds stay in place and nothing is released until D is back |
| No acknowledgement from D | Order becomes `UNKNOWN`; hold kept; same `order_id` resent, then cancelled |
| Acknowledged, no notification | Reconciliation (section 9) |
| A notification arrives ahead of a gap | A answers `409` with `expected_seq`; D resends from there |
| A crashes | Database is durable; `PENDING` orders become `UNKNOWN`; D resends unacknowledged notifications |
| B market data stale (> 3 s) | Market orders rejected with `NO_FRESH_PRICE`; the frontend marks the order book as interrupted |
| C down | The news panel and chat box show "unavailable"; trading is unaffected |
| PostgreSQL down | `/healthz` unhealthy; all writes fail with a clear error; no funds move |

We accept that funds stay frozen while D is unavailable, in return for never settling a trade against released funds.

## 12. Metrics

| Metric | Meaning |
| --- | --- |
| `lame_a_orders_total{result}` | Orders by outcome: accepted, each pre-check reason, `SYSTEM_BUSY`, risk rejected |
| `lame_a_http_request_duration_seconds{endpoint}` | API latency |
| `lame_a_d_ack_latency_seconds` | Latency of D's acknowledgement (the input to load shedding) |
| `lame_a_order_completion_seconds` | Time from placement to D's ACCEPTED or RISK_REJECTED notification |
| `lame_a_circuit_state` / `lame_a_shedding_active` | Protection state over time |
| `lame_a_orders_by_status{status}` | In particular `UNKNOWN` and `SENT` |
| `lame_a_notification_lag_seconds` | Time from an event at B to its settlement at A |
| `lame_a_notification_gaps_total` | Gaps answered with `409` |
| `lame_a_duplicates_ignored_total{layer}` | Duplicates caught by each idempotency layer |
| `lame_a_auth_failures_total` | Failed logins |

## 13. Frontend (D15)

React + Vite + TypeScript, built to static files and served by an nginx container. The layout follows a typical spot-trading page; LAME uses its own name and logo.

| Page | Owner |
| --- | --- |
| Trade (main page) | A |
| News & Analyst | C (A provides the route and an empty page) |

Trade page regions:

| Region | Content | Data source |
| --- | --- | --- |
| Left | Order book: three ask and three bid levels, last price | Browser → B WebSocket 9999 |
| Centre top | Candlestick chart with MA(7), MA(25), MA(99) and volume; intervals 5s, 15s, 1m, 5m | A `/api/market/candles` |
| Centre bottom | Order form: limit and market, buy and sell | A `/api/orders` |
| Right top | Market list (the two pairs): last price, change since the day open | Last price from B's snapshot; day open and precision from A `/api/market/summary` |
| Right middle | Chat box slot (replaces the "recent trades / my trades" panel) | C's module |
| Right bottom | Market news for the selected pair | C `GET /api/news?symbol=` |
| Bottom | Tabs: open orders, order history, trade history, holdings | A `/api/orders`, `/api/fills`, `/api/accounts/me`; live updates over SSE |

## 14. Needed from other services

These require changes in other services' documents; A only states what it needs.

**D (Zongyan)**
1. `POST /v1/orders` acknowledges receipt only; the risk decision and B's result arrive as notifications (current D protocol 2.1 replies synchronously with B's result).
2. D notifies A of every outcome through `POST /internal/order-events`, with the contract in section 3.1 (current D documents RD6 and RD15 say A subscribes to B directly and D keeps no copy of B's outputs; contiguous `notify_seq`, in-order sending and resends need D to keep its notifications until A acknowledges them).
3. A status query by `order_id` for reconciliation (section 9).
4. Modification under the same `order_id` (current D document RD8 answers 409 to the same key with different content; B does not allow an `order_id` to be reused within a day, B D2).
5. A cancel result, including `TOMBSTONED`, also arrives as a notification.
6. Linked accounts: endpoint or shared seed file.

**B (Bo)**
1. Market data on 9999 for the browser and for A.
2. A price cap for market orders; without one, a market buy on a thin book can cost more than A held for it.
3. The shared symbol config (tick, lot, asset precision).

**C (Rushabh)**
1. The chat box module and the News & Analyst page plug into the React shell.
2. A common `symbol` format for `GET /api/news?symbol=` (C's examples use `BTC`, B and D use `BTC-USDT`).
3. Prometheus scrapes A's `8000/metrics`.

**Everyone:** ownership of the root `docker-compose.yml`; the trading day (A needs the day boundary for the day open and for B's `(day, seq)`).

## 15. Deployment

| Aspect | Design |
| --- | --- |
| Image | `python:3.12-slim`, multi-stage, pinned dependencies, non-root user |
| Process | `uvicorn` with **one worker** (D2) |
| Database | PostgreSQL container with a named volume; Alembic migrations run before the app reports healthy |
| Ports | 8000: API, SSE, `/internal/order-events`, `/healthz`, `/metrics`. `/internal/*` accepts calls only from the compose network |
| Config | Mounted symbol config (shared with B); env vars for database URL, token secret, rate-limit values, timeouts, shedding threshold, `RESEND_WINDOW` |
| Frontend | nginx container serving the React build |
| Seed | Demo users and linked accounts, loaded on first start; deterministic |

## 16. How the design meets the acceptance criteria

| Criterion | Mechanism | Test |
| --- | --- | --- |
| Balances, holds and ledger stay consistent under concurrent trading | Conditional-update holds; settlement in one transaction; ledger invariant | 100 concurrent orders against one account: total held ≤ balance; ledger sums match seed + deposits |
| Duplicate requests or notifications never take effect twice | Three idempotency layers (section 8) | Same `Idempotency-Key` twice → one order; same notification twice → one settlement |
| No hold released before the order's fate is known | Release only on notifications (5.2) | Drop D's acknowledgement: order `UNKNOWN`, hold kept; delay D's notification: order stays `SENT`, hold kept |
| A detects a gap and recovers | `notify_seq` and `409 expected_seq` | Deliver notification N+2 before N+1: A answers 409; after the resend, balances are correct |
| A user completes the full flow in the UI | React shell, REST, SSE | Register, log in, deposit, place, modify and cancel orders, see balances and history change |
| Rate limiting and size limits behave as configured | Token bucket; per-symbol limits | 20 orders in one second → 10 pass, rest `RATE_LIMITED`; each size violation returns its reason |
| Modification adjusts holds correctly | Hold more before sending, release less only after confirmation | Modify up and down, with and without a fill before the confirmation |
| Candles match the trades | OHLCV computed from stored trades | Known trade sequence → expected open, high, low, close and volume |
| Load shedding protects the gate | Sliding-window p99 with hysteresis | Delay D's acknowledgement → `SYSTEM_BUSY`; remove the delay → recovery |
