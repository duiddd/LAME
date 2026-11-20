# Service A: Trading & Accounts — Architecture

**Owner:** Pin-Yun Kuo · **The one question it answers:** Are balances and assets correct?

This document describes the design of Service A. The rationale behind each choice is recorded in [service_a_tech_decisions.md](service_a_tech_decisions.md) (decision IDs such as D1 are referenced below). How A fits the other services' current implementations is in section 14.

## 1. Role and scope

A is the user-facing entry point of the exchange and the single authority for users, balances, holds and order state.

- **In scope:** user registration and login; simulated deposits; accounts, holdings and a ledger; synchronous balance holds; pre-checks (rate limiting, order size, price protection); placing, modifying and cancelling orders through D; order state, settlement and reconciliation from D's notifications; candles and the market summary; pushing each user's own updates to the frontend; the frontend application shell and the trading page.
- **Out of scope:** matching (B), behavioural risk rules (D), news, Q&A and the chat box (C), identity verification (KYC), password recovery, real personal data, withdrawals, leverage and margin, fees, stop orders.

## 2. Interfaces to users

All amounts on the API are decimal strings; inside A they are integers (D8). Login sets two HttpOnly cookies, a short-lived access token and a refresh token (D20). Register, login, refresh, `/api/market/*`, `/healthz` and `/metrics` need no access token; every other endpoint does.

| Endpoint | Method | Description |
| --- | --- | --- |
| `/api/auth/register` | `POST` | `username`, `password`. No email or other personal data |
| `/api/auth/login` | `POST` | `username`, `password` → sets the access and refresh cookies |
| `/api/auth/refresh` | `POST` | Rotates the refresh token and issues a new access token; reuse of a spent refresh token revokes its whole family |
| `/api/auth/me` | `GET` | The signed-in user |
| `/api/auth/logout` | `POST` | Revokes the refresh token family and clears both cookies; an access token already copied elsewhere stays valid until it expires (at most 15 minutes) |
| `/api/accounts/me` | `GET` | Available, held and total balance per asset (also the "holdings" tab) |
| `/api/accounts/deposits` | `POST` | Simulated deposit: `asset`, `amount`, `Idempotency-Key` header. Credited at once (D26) |
| `/api/orders` | `POST` | Place an order (section 7.1). Requires an `Idempotency-Key` header (the `client_order_id`). Answers `202`, or `503` when the request certainly did not reach D (section 10) |
| `/api/orders/{order_id}` | `PUT` | Modify an order's remaining `qty`, up or down; the price cannot change; same `order_id` (D24) |
| `/api/orders/{order_id}` | `DELETE` | Cancel an order |
| `/api/orders` | `GET` | Open orders (`?status=open`) and order history |
| `/api/orders/{order_id}` | `GET` | One order, including its fills |
| `/api/fills` | `GET` | The user's fills (the "trade history" tab) |
| `/api/market/candles` | `GET` | `symbol`, `interval` (`5s`, `15s`, `1m`, `5m`), optional `limit` (default 300) and `after_seq` → OHLCV candles and the watermark for the next poll (D22) |
| `/api/market/summary` | `GET` | Per symbol: day open price and price precision (D23) |
| `/api/stream` | `GET` | Server-Sent Events: the user's own `order_update` and `balance_update`; resumes from `Last-Event-ID` or `?last_event_id=` (D14) |
| `/healthz` | `GET` | Ready once the database is reachable and migrations have run; also reports each background task's state |
| `/metrics` | `GET` | Prometheus metrics (section 12) |

Rejections carry a machine-readable `reason`: `RATE_LIMITED` (429), `INVALID_TICK`, `INVALID_LOT`, `QTY_TOO_SMALL`, `QTY_TOO_LARGE`, `NOTIONAL_TOO_LARGE`, `PRICE_OUT_OF_BAND`, `NO_FRESH_PRICE`, `NO_LIQUIDITY`, `INSUFFICIENT_BALANCE`, `MODIFY_UNAVAILABLE`, `SYSTEM_BUSY` (503), `UNAUTHORIZED` (401), `USERNAME_TAKEN` (409), `INVALID_SYMBOL`, `ORDER_NOT_FOUND` (404), `ORDER_ALREADY_FINAL` (409), `ORDER_NOT_MODIFIABLE` (409), `INVALID_ASSET`, `INVALID_AMOUNT`, `DEPOSIT_TOO_LARGE`, `FORBIDDEN_ORIGIN` (403).

After a `503`, the frontend starts a new attempt with a new `Idempotency-Key`, because the earlier attempt is finished. A retry of an order still being confirmed reuses the same key and returns the existing order.

State-changing endpoints accept only `application/json`, refuse a foreign `Origin` (a request with no `Origin`, such as a bot's, is allowed), and A enables no CORS. `SameSite` alone is not enough: different ports on one host count as the same site (D20).

## 3. Interfaces to other services

| Link | Direction | Method | Content |
| --- | --- | --- | --- |
| Commands | A → D | HTTP + JSON: `POST /v1/orders`, `/v1/cancels`, `/v1/modifies` on port 8400 | Place, cancel and modify orders. `user_id` is an integer; `price` and `qty` are integers; `price` is null or absent for `MARKET`. D's synchronous reply is an **acknowledgement of receipt only**; it does not mean the order has been checked or sent to B (D3) |
| Status query | A → D | HTTP + JSON: `GET /v1/orders/{order_id}?user_id=` | The order's state as D knows it; used only for reconciliation (D5) |
| Resend | A → D | HTTP + JSON: `POST /v1/notifications/rewind` | Asks D to resend from a `notify_seq` (section 9) |
| Notifications | D → A | HTTP + JSON, A's endpoint `POST /internal/order-events` | D calls A for every outcome: risk rejection, B accepted (with B's `day` and `order_seq`), trades, cancels, rejections, expiry, end of day (D21) |
| Market data | B → browser, B → A | WebSocket `ws://<B>:9999/md/<symbol>` | `{"bids":[{"price","qty"}],"asks":[...],"last_price","volume","last_ms"}`: up to three levels per side, best first, possibly empty; `last_price` is null before the day's first trade; no `symbol` field, since each connection is one pair. A closed connection, `503` during B's recovery and `404` for an unknown pair all mean retry with backoff. The browser draws the order book from it; A uses it for price protection (D11) |
| Linked accounts | Shared file | `linked_accounts.json` | Seeded account groups, read by A and D (D16) |
| News | browser → C | HTTP, C's `GET /api/news?symbol=&limit=`, no cookies | Drawn by A in the "market news" panel of the trading page, refreshed every 15 s |
| Chat box | browser → C | `<iframe>` of C's `/widget` | C owns the UI; A passes no parameters, since a question need not concern the selected pair (D15) |

### 3.1 Notification contract (verified against D's code)

- Each notification carries a contiguous `notify_seq`, which starts at 1 and is not reset across trading days. Notifications from B also carry `b_day` and `b_seq`.
- D sends **one at a time, in order** and resends the same one until A answers 2xx, with a 2-second timeout, so A must finish within that. For notifications from B, the order is B's `seq`: one match gives `TRADE`, then the taker's and the maker's `ORDER_UPDATE`.
- A answers `200` after committing, and `200` again for a duplicate.
- On a gap, A answers `409` with a top-level body `{"expected_seq": N}`, and D resends from N. The body must not be nested: D reads that key directly, so FastAPI's `HTTPException` (`{"detail": ...}`) would break D's sender.
- A answers `503` for a temporary failure and `422` for a notification it cannot apply (section 5.6). D treats both alike and keeps resending the same one every few seconds, never skipping it. So `422` stops the line, and the alert comes from A's metrics.

Common fields: `notify_seq`, `type`, `order_id`, `user_id`, `symbol`, `b_day`, `b_seq`, `ts` (when D generated the notification, Unix ms).

| Type | Own fields | What A does |
| --- | --- | --- |
| `RISK_REJECTED` | `reason` (`SELF_TRADE`, `WASH_TRADE`, `RISK_TIMEOUT`, `CANCELLED_BEFORE_SUBMIT`), `rule_version` | `REJECTED`, release |
| `ACCEPTED` | `order_seq`, `rule_version` | `PROCESSING`, store `(b_day, order_seq)` |
| `TRADE` | `price`, `qty`, `taker` and `maker`, each with `order_id`, `user_id`, `side`; the common `order_id` and `user_id` are null | Settle both sides |
| `ORDER_UPDATE` | `status`, `reason`, `filled_qty` (cumulative), `remaining_qty` | Section 5.2 |
| `CANCEL_RESULT` | `result` (`CANCELLED`, `TOMBSTONED`, `NOT_SENT_TO_B`, `ORDER_NOT_FOUND`, `ALREADY_FINAL`) | Section 5.2 |
| `MODIFY_RESULT` | `result` (`APPLIED`, `REJECTED`), `price`, `qty`, `reason` | Section 5.4; D does not send it yet |

`ORDER_UPDATE` statuses are `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED` and `EXPIRED`. Reasons include `END_OF_DAY`, `SELF_TRADE_PREVENTED`, `D_TIMEOUT`, `NOT_DELIVERED`, `INVALID_PRICE` and `INVALID_QTY`.

## 4. Architecture

A is one Python process (FastAPI on uvicorn, a single worker, D2) with a PostgreSQL database.

```
Browser ──REST──► [auth] ─► [router] ─► [orders.precheck: rate limit → size → price protection] ── fail → 4xx
                                          │ pass
                                          ▼
                              [breaker / shedding check] ── open or shedding → 503 SYSTEM_BUSY
                                          │
                                          ▼
                              [orders + accounts] DB TX: order PENDING, hold funds, ledger entry
                                          │
                                          ▼
                              [orders.dclient] ──HTTP──► D   ack → SENT
                                                  │          certainly not sent → REJECTED, hold released, 503
                                                  │          sent, no reply → UNKNOWN, hold kept, 202
                     D ──HTTP POST /internal/order-events──► [orders.events] cursor lock, dedupe, order check
                                                                     │
                                                                     ▼
                                    [orders + accounts] DB TX: status, settle, release, user_events, cursor
                                                                     │ after commit
Browser ◄──SSE── [push] ◄────────────────────────────────────────────┘
Browser ◄──WS 9999── B (order book)          [orders.reconcile] queries D for orders stuck in SENT
Browser ──HTTP──► C (news); iframe (chat)    [market] candles from settled trades
```

| Module | Responsibility | Interacts with | Edge cases |
| --- | --- | --- | --- |
| `auth` | Register, login, refresh rotation, logout, access-token check, Argon2id hashing with a concurrency cap (D20) | all routers | Duplicate username; wrong password; expired token; refresh token reuse |
| `accounts` | Deposit, hold, release, settle, holdings; one ledger entry per change; the only module that writes balances (D6, D7) | orders | Insufficient balance; repeated effects |
| `orders` | Routes, order service, state-transition table (section 5), modify (D24) | accounts, push | Illegal transitions are refused |
| `orders.precheck` | Rate limit (D9); size per symbol (D10); price band and freshness (D11) | market, shared symbol config | Burst and refill; qty ≤ 0; no fresh price → reject |
| `orders.dclient` | Port and HTTP adapter for D: timeouts, failure classification, circuit breaker, latency samples (D12) | shedding | Sent without a reply is *unknown*, never *rejected* |
| `orders.events` | `POST /internal/order-events`: cursor lock, deduplication, gap handling, applying one notification (D21, D28) | accounts, market, push | Duplicates; gaps; concurrent resends; a poison notification stops the line |
| `orders.reconcile` | Background loop: resends unacknowledged orders; queries D about orders stuck in `SENT` (D5); resends the cancel of a market buy's remainder (section 7.3) | orders.dclient | D down: keep waiting, keep the hold |
| `market` | Port and adapter for B's WebSocket, price cache (D11); candles and day open from settled trades (D22, D23) | orders.precheck, orders.events | Stale feed; intervals with no trades; backfilled trades |
| `push` | SSE per user, replay from `user_events` (D14) | orders.events | Slow client; `event_id` too old to replay |
| `shedding` | Sliding-window p99 of the time from sending an order to its first outcome; decides whether to accept new orders (D13) | orders.dclient, orders.events, orders | Hysteresis to avoid flapping; stalled orders still count |
| `seed` | Demo users and linked accounts (D16) | — | Deterministic on every start |
| `metrics` | Prometheus exposition (section 12) | — | — |

Structure (D27):

- **Dependencies point one way:** router → service → repository or adapter. A service does not import FastAPI. Modules call each other's services, never each other's repositories.
- **Transactions:** the outermost service opens the transaction and passes the connection down, so several modules can commit together.
- **Composition root:** the database engine, D client, circuit breaker, rate limiter, price cache, SSE hub and settings are built once in the FastAPI lifespan. Routers reach them through thin `Depends` functions; background tasks receive them when created.
- **Ports:** only D, B's WebSocket, the clock and the ID generator have a port, so tests can replace them. PostgreSQL has none; tests use a real one.
- **Background tasks:** the B market-data consumer, the reconciliation loop and the shedding evaluation are asyncio tasks in this process. A supervisor logs a crash, restarts the task with backoff and reports its state to `/healthz` and the metrics. A malformed market-data message is skipped and counted, so it cannot cause a restart loop.

## 5. Order lifecycle

### 5.1 States

```
 PENDING ──D ack──► SENT ──ACCEPTED (B's order_seq)──► PROCESSING ──► PARTIALLY_FILLED ──► FILLED
    │                │                                      │                │
    │ no reply       │ risk or B refusal                    └───────┬────────┘
    ▼                ▼                                              ▼
 UNKNOWN          REJECTED / EXPIRED (hold released)  CANCELLED / REJECTED / EXPIRED
 (hold kept; resend the same order_id, then cancel)
```

- `PENDING`: the hold is placed and the order is being sent to D. If D certainly did not take the request (section 10), the order becomes `REJECTED` with reason `D_UNAVAILABLE` and the hold is released at once.
- `REJECTED` also covers D's and B's rejections. When B refuses a new order it sends no `ACCEPTED`, so an order can go directly from `PENDING`, `SENT` or `UNKNOWN` to `REJECTED` or `EXPIRED`.
- `SENT`: D has acknowledged receipt. The hold is **not** released; D has not decided yet.
- `PROCESSING`: D's notification says B accepted the order. From here A also knows B's `(day, order_seq)`, a second unique key for the order.
- Terminal statuses: FILLED, CANCELLED, REJECTED, EXPIRED. END_OF_DAY and SELF_TRADE_PREVENTED are reasons for CANCELLED.
- An order is **closed** only when its status is terminal **and** the quantity settled from trades equals its cumulative filled quantity (D4). Both numbers come from D: `filled_qty` is overwritten with the cumulative quantity in `ORDER_UPDATE`, and `settled_qty` accumulates as A settles each `TRADE`. B emits a match's `Trade` before its `OrderEvent`, so a terminal update normally finds them equal; A does not rely on this, since the release rule in 5.2 also works if the terminal update arrives first. At close the order's `held_amount` must be zero.
- `UNKNOWN`: the request to D may have been sent, but no acknowledgement came back. `ACCEPTED` or `RISK_REJECTED` resolves it, because either proves that D received the order. A resends the same `order_id` (D deduplicates it) with backoff for up to `RESEND_WINDOW`; if still no acknowledgement, A sends a cancel and waits for its result (D5).

### 5.2 Releasing a hold

Once a request may have reached D, funds move only on D's notifications, never on a timeout or an acknowledgement (D5). The single exception is a request D certainly did not take (section 10).

| Notification | Effect on the hold |
| --- | --- |
| `RISK_REJECTED` | Release the whole hold (D never sends a rejected order to B) |
| `ORDER_UPDATE` CANCELLED, REJECTED or EXPIRED | Release the hold for `qty − filled_qty`; the hold for the filled quantity stays until its trades are settled. One rule for all three, because `REJECTED` with `SELF_TRADE_PREVENTED` can follow a partial fill |
| `ORDER_UPDATE` PARTIALLY_FILLED or FILLED | Nothing is released; update `filled_qty` |
| `CANCEL_RESULT` TOMBSTONED or ORDER_NOT_FOUND | The order never reached B and never will; release what remains of the hold |
| `CANCEL_RESULT` CANCELLED, NOT_SENT_TO_B or ALREADY_FINAL | No effect on funds; the other notification already covered it |
| `TRADE` | Settle that quantity (5.3); it never releases the unfilled remainder |

A release is limited by the order's `held_amount`, so a second notification about the same outcome moves nothing. For example, `ORDER_UPDATE REJECTED NOT_DELIVERED` can be followed by `CANCEL_RESULT ORDER_NOT_FOUND`.

Example: a limit buy of 3 at 100 holds 300. One unit fills, then the order is cancelled. A releases 200 on CANCELLED (filled 1) and keeps 100 for the trade. When the trade at 98 is settled, A takes 98 and releases 2.

### 5.3 Settling a trade

One trade is settled for both sides in one transaction:

- **Buyer:** held quote decreases by `qty × hold price`; available quote increases by the price improvement `qty × (hold price − trade price)`; available base increases by `qty`. The hold price is the limit price, or the cap of a market buy (section 7.3).
- **Seller:** held base decreases by `qty`; available quote increases by `qty × trade price`.
- There are no fees.

The same transaction records B's `(day, seq)` for the trade and advances `notify_seq`, so a repeated notification has no effect (D7). The trade is also stored for candles (D22).

### 5.4 Modifying an order (D24)

`PUT /api/orders/{order_id}` takes only the new **remaining** `qty`, larger or smaller; the team decided on 11-18 that a modify does not change the price. A sends it to D's `POST /v1/modifies` under the same `order_id` without a price, which D's protocol (2.3) reads as unchanged; D carries it out as a cancel and a new order at B. A body that still carries a price is refused with `422`. Sending changed content to `POST /v1/orders` would get `409`.

- The new quantity passes the size checks and the value limit. The price band is not checked again, since the price has not changed and already passed it.
- If the new quantity needs **more** funds, A holds the difference before sending, and refuses the modify with `INSUFFICIENT_BALANCE` if it cannot. If it needs **less**, the difference is released only after `MODIFY_RESULT` confirms the modification, because the old order may still fill at its old size until then.
- While a modification is pending, the order shows `MODIFYING`; a rejected modification leaves the order as it was and releases any extra hold.
- A trade that arrives while `MODIFYING` settles at the old hold price, which stays in effect until `MODIFY_RESULT`.
- Market orders cannot be modified, and neither can an order that is `PENDING` or `UNKNOWN`, because a resend must carry identical content.
- If D refuses the modify (for example `501`), A releases any extra hold and answers `MODIFY_UNAVAILABLE`. A's alternative, cancel plus a new `order_id` at the same price, stays available through configuration (D24).

A's answers to the questions in D's protocol document (2.3):

| Question | A's answer |
| --- | --- |
| The old order fills while being cancelled | New quantity = requested `qty` minus what filled meanwhile; otherwise the total could exceed what the user intended. `MODIFY_RESULT` reports the actual `price` and `qty`, and A adjusts the hold from those |
| The old order is already filled | `MODIFY_RESULT REJECTED` with `ALREADY_FINAL`; A releases the extra hold |
| Time priority is lost | Acceptable, since B has no amend |
| The old order is cancelled, then the new one is refused (not in D's list) | `ORDER_UPDATE CANCELLED`, then `MODIFY_RESULT REJECTED`, so that A does not assume the old order is still on the book |

### 5.5 Restart

On start-up, an order still `PENDING` may or may not have reached D, so it becomes `UNKNOWN` and is handled by `reconcile`. The last applied `notify_seq` is read from the database; D's next notification continues from it.

### 5.6 Applying a notification (D28)

Each notification is applied synchronously in the handler, and A answers only after the commit:

```
D → POST /internal/order-events { notify_seq: N, type, ... }
 ├ schema invalid → 422
 └ begin transaction; SELECT notify_cursor FOR UPDATE → last
     ├ N ≤ last    → duplicate, no effect → 200
     ├ N > last+1  → gap → 409 { expected_seq: last+1 }
     └ N = last+1  → apply, cursor = N, write user_events, commit → 200
                     then wake the SSE hub
```

- The cursor lock makes a concurrent resend of the same notification wait, then see it as a duplicate. Correctness does not depend on A being a single instance.
- One transaction updates the order, inserts the trade, writes ledger entries, updates accounts, inserts `user_events` and advances the cursor.
- One table maps (current status, notification type) to the new status. Tests enumerate every combination.
- The acknowledgement and the notifications race, since D's sender runs independently. So a `202` moves an order to `SENT` only from `PENDING` or `UNKNOWN`, and those statuses accept any first outcome.
- Some late notifications are legal and have no effect: `RISK_REJECTED` after `CANCEL_RESULT TOMBSTONED` (D's protocol 8.5), and any `CANCEL_RESULT` on a terminal order. A `TRADE` on a terminal order is legal while `settled_qty + qty ≤ filled_qty`.
- Any other illegal transition, an unknown order or a settlement that would break a balance constraint answers `422` and stops the line. Because `notify_seq` is contiguous, later notifications wait too. A wrong ledger is harder to repair than a pause, so A never skips one.

## 6. Accounts and ledger

Tables (illustrative, not final):

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `users(user_id, username, password_hash, created_at)` | Registered and seeded users. `user_id` is a `BIGINT`, because D and B need an integer. Seeded users keep the IDs in `linked_accounts.json`; registered users get IDs from 100000 upwards | `UNIQUE (username)` |
| `refresh_tokens(token_hash, family_id, user_id, expires_at, used_at, revoked_at)` | Refresh token rotation and reuse detection (D20) | `PRIMARY KEY (token_hash)`; only the hash is stored |
| `accounts(user_id, asset, available, held)` | Current balances | `CHECK (available >= 0 AND held >= 0)` |
| `ledger_entries(id, user_id, asset, delta_available, delta_held, reason, ref_type, ref_id, created_at)` | Every balance change (deposit, hold, release, settle) | Append-only; `UNIQUE (ref_type, ref_id, user_id, asset, reason)` makes each effect idempotent. `ref` is the deposit, the trade `(b_day, b_seq)`, the `notify_seq`, or for a hold the order and its modification number, so that repeated modifications of one order do not collide |
| `deposits(deposit_id, user_id, client_request_id, asset, amount, created_at)` | Simulated deposits | `UNIQUE (user_id, client_request_id)` |
| `orders(order_id, user_id, client_order_id, symbol, side, type, price, hold_price, wire_type, qty, filled_qty, settled_qty, held_amount, status, reason, b_day, b_order_seq, ...)` | Order state | `PRIMARY KEY (order_id)`; `UNIQUE (user_id, client_order_id)`; `UNIQUE (b_day, b_order_seq)` |
| `trades(b_day, b_seq, symbol, price, qty, taker_order_id, maker_order_id, ts)` | Settled trades; the source of candles. `ts` is the notification's `ts` (D22) | `PRIMARY KEY (b_day, b_seq)`; index on `(symbol, ts)` |
| `notify_cursor(last_notify_seq)` | Last applied notification from D | One row, locked by `FOR UPDATE` per notification |
| `user_events(event_id, user_id, type, payload, created_at)` | Per-user event log for SSE replay (D14) | `event_id` increases; written in the same transaction as the change; rows older than 24 hours are deleted |

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

A adds `user_id` (from the token), `order_id` (a ULID) and `created_at`. There is no time-in-force choice: a limit order rests until it fills, is cancelled or is cancelled at the end of the day; a market order's unfilled remainder is cancelled (section 7.3).

### 7.2 Pre-checks

Run in order; the first failure rejects the request before anything is written.

1. **Rate limit (D9).** One token bucket per user, in memory: an initial rate of 5 orders per second with a burst of 10. Cancels draw from a separate, larger bucket so that a user can always withdraw orders. The values are configuration, to be tuned from the Week 6 baseline.
2. **Size (D10).** Per symbol, from A's symbol config (section 14): `tick`, `lot`, `min_qty`, `max_qty`, `max_notional`. The price must be a multiple of the tick and the qty a multiple of the lot.
3. **Price protection (D11).**
   - **Freshness:** the time since A last received *any* message for that pair, including B's heartbeat. If it exceeds 3 seconds, limit and market orders are rejected with `NO_FRESH_PRICE`. `last_ms` is not used for this, because a heartbeat repeats it, so a quiet but healthy market would look stale. It only orders snapshots, and A forgets it on reconnect.
   - **Reference price:** `last_price`, otherwise the midpoint of best bid and best ask, otherwise the one best price that exists.
   - **Limit orders:** the price must lie within ±10% of the reference price. If the feed is fresh and there is no price at all, as at the start of a day, the band is skipped; otherwise nobody could place the first order.
   - **Market orders:** valued at a protection price (best ask × 1.05 for a buy, best bid × 0.95 for a sell) for the notional check and for the hold. With no opposite side, they are rejected with `NO_LIQUIDITY`.
4. **Balance.** Checked atomically by the hold itself (section 6), not by a separate read.

### 7.3 How a market buy is sent (D11)

B's market orders have no price limit, so a market buy on a thin book could cost more than A held. That would drive a balance negative and stop all settlement (section 5.6). So A sends a market buy as a limit order at the cap:

1. The user sees and A stores `type = MARKET`. A computes the cap, best ask × 1.05 rounded down to the tick, and stores it as `hold_price`, with `wire_type = LIMIT`. A resend uses the same values, since D answers `409` to different content.
2. When `ACCEPTED` has committed, A sends `POST /v1/cancels` for the remainder. If the order has already filled, D answers `ALREADY_FINAL`, which has no effect. A crash before the cancel is covered by `orders.reconcile`, which resends it for any market buy still open.
3. `ORDER_UPDATE CANCELLED` releases the unfilled part as usual, and the order shows as a market order with its remainder cancelled.

The cost can never exceed the hold. The price is that the remainder rests for the short time before the cancel reaches B: it appears in the order book, and another order can trade against it at up to the cap, which can briefly move `last_price`. Market sells stay true `MARKET` orders, because a sell holds base and cannot overdraw; they have no price floor at B.

## 8. Idempotency

Three layers, each against a different duplicate (D4):

| Layer | Duplicate it prevents | Key | Where enforced |
| --- | --- | --- | --- |
| Request | A double click or a resent HTTP request | `client_order_id` from the frontend (`Idempotency-Key`); deposits use their own key | `UNIQUE (user_id, client_order_id)`; a duplicate returns the existing order |
| Order | The same order submitted twice after A resends it to D | `order_id`, a ULID generated by A, carried unchanged to D | D deduplicates on it, and D's own `b_order_id` is what reaches B; modifications go through `POST /v1/modifies` (D24) |
| Notification | The same notification or trade applied twice | `notify_seq`, and B's `(day, seq)` for trades; never `order_id` (one order can have several fills) | `notify_cursor`, `trades` primary key and `ledger_entries` uniqueness, all in the same transaction |

## 9. Reconciliation of orders stuck in SENT (D5)

D's notifications are the primary path. Reconciliation only covers a notification that never arrives.

- An order still in `SENT` after 10 seconds is queried at D; further queries follow at 20 s, 40 s and then every 60 s.
- If `last_notify_seq` in the reply is above A's cursor, A has missed a notification and calls `POST /v1/notifications/rewind` with its cursor + 1. Duplicates this causes are harmless.
- `404`: D does not know the order. A cancels it, which leaves a tombstone at D, and the `CANCEL_RESULT TOMBSTONED` releases the hold.
- If D reports that it is still deciding or still waiting for B, A keeps waiting and shows the order as "processing delayed".
- The query result itself never releases funds; only the notification does.
- The user can cancel a `SENT` order at any time.
- An order that is terminal but not closed, because a trade has not arrived, is counted and raises an alert. A never closes it by force, since that would lose the fill.

## 10. Resilience

- **Timeouts (D12).** Every call to D has a connect timeout and a total timeout. Because D only acknowledges receipt, the timeout covers the acknowledgement, not the risk decision.
- **Failure classification (D12).** The kind of failure decides what A may tell the user:

  | What happened | Did D receive it? | Order | Hold | Answer to the user |
  | --- | --- | --- | --- | --- |
  | Shedding or open breaker: no call made | Certainly not | None created | None placed | `503 SYSTEM_BUSY` |
  | Connection never established (`ConnectError`, `ConnectTimeout`, `PoolTimeout`) | Certainly not | `REJECTED` (`D_UNAVAILABLE`), never resent | Released at once | `503 SYSTEM_BUSY` |
  | First attempt answered `503` `RISK_UNAVAILABLE` or `RISK_BUSY` (D keeps no record) | Certainly not | `REJECTED` (`D_UNAVAILABLE`), never resent | Released at once | `503 SYSTEM_BUSY` |
  | First attempt answered `400` `INVALID_REQUEST` (D keeps no record; A's pre-checks should have caught it) | Certainly not | `REJECTED`, with an alert | Released at once | `400` |
  | `202` | Yes | `SENT` | Kept | `202` |
  | `409` `ORDER_ID_CONFLICT` | Yes, with other content: a bug in A | Unchanged, with an alert | Kept | `202`, shown as "Confirming" |
  | Sent, but no reply (`ReadTimeout`, connection dropped midway, any other 5xx) | Unknown | `UNKNOWN` | Kept | `202`, shown as "Confirming" |

  A resend of an `UNKNOWN` order that gets `503` or `400` stays `UNKNOWN`, because the earlier attempt may still be on its way to D. It ends through the cancel below.

  Telling a user "failed, try again" while D may hold the order would produce a second order, and releasing the hold could overdraw the account. Conversely, once A has said "failed" it must never resend that order.
- **Retries.** An `UNKNOWN` order is resent with the same `order_id` (safe, because D deduplicates it) with exponential backoff, for up to `RESEND_WINDOW`; after that A sends a cancel instead, so that an order is not placed long after the user asked. Cancels are resent until acknowledged.
- **Circuit breaker (D12).** Opens after 5 consecutive failures, timeouts or `RISK_UNAVAILABLE` answers, stays open for 10 s, then lets one trial request through. While it is open, new orders are rejected with `SYSTEM_BUSY` before any hold is placed. Cancels bypass it and are queued for resend.
- **Load shedding (D13).** D's `202` is always fast and does not show queueing inside D, so A measures the time from sending an order to its first outcome: `ACCEPTED`, `RISK_REJECTED`, or a refusal by B. A keeps a 30-second sliding window of these. An order still waiting counts at its current age, so stalled notifications also trigger shedding, and a `RISK_BUSY` answer counts as a sample over the threshold. If the p99 exceeds D's share of the budget in three consecutive one-second evaluations, A rejects new orders with `SYSTEM_BUSY`; it resumes when the p99 falls below 80% of the threshold. Cancels are never shed.
- **Notification endpoint.** Idempotent; answers only after the database commit (section 5.6).
- **Login protection (D20).** nginx limits each IP. Argon2id runs in a thread pool with a small concurrency cap, and excess attempts get `429` at once, so a login flood cannot exhaust the CPU or memory that order handling needs. Accounts are not locked, since an attacker could use that to lock out a victim.

## 11. Consistency and failure model

- **Strongly consistent:** registration, deposits, balances, holds and order acceptance, each within one PostgreSQL transaction.
- **Eventually consistent:** order status and settlement, which follow D's notifications; the lag is measured (section 12).
- **Effectively once:** every notification is applied in the same transaction that advances `notify_seq`, so D's resends produce no double effects.

| Failure | Behaviour |
| --- | --- |
| D slow | Load shedding rejects new orders |
| D down | Circuit breaker opens; new orders fail fast; **settlement also pauses**, because every outcome reaches A through D; holds stay in place and nothing is released until D is back |
| Connection to D cannot be established | Order `REJECTED` (`D_UNAVAILABLE`); hold released; `503`; never resent |
| Sent to D, no reply | Order becomes `UNKNOWN`; hold kept; same `order_id` resent, then cancelled |
| Poison notification | A answers `422` and raises an alert; D keeps resending it and never skips it; settlement pauses until it is fixed |
| D's data is reset but A's is not | `notify_seq` restarts at 1, and A takes every notification for a duplicate, so holds stay frozen. A's and D's volumes are always reset together |
| Order placed outside trading hours | B refuses with `END_OF_DAY`; the order ends `REJECTED`, shown as "Market closed" |
| Background task crashes | Supervisor restarts it with backoff; `/healthz` and metrics report it |
| Acknowledged, no notification | Reconciliation (section 9) |
| A notification arrives ahead of a gap | A answers `409` with `expected_seq`; D resends from there |
| A crashes | Database is durable; `PENDING` orders become `UNKNOWN`; D resends unacknowledged notifications, which the cursor recognises as duplicates if already committed; browsers reconnect and replay from `Last-Event-ID` |
| B market data stale (> 3 s) | New limit and market orders rejected with `NO_FRESH_PRICE`; the frontend marks the order book as interrupted |
| C down | The news panel and chat box show "unavailable"; trading is unaffected |
| PostgreSQL down | `/healthz` unhealthy; all writes fail with a clear error; no funds move |

We accept that funds stay frozen while D is unavailable, in return for never settling a trade against released funds.

## 12. Metrics and observability

### 12.1 A's metrics

`GET /metrics` exports these series in Prometheus format; `GET /healthz` reports readiness, the background tasks, the breaker and load shedding.

| Metric | Meaning |
| --- | --- |
| `lame_a_orders_total{result}` | Orders by outcome: accepted, each pre-check reason, `SYSTEM_BUSY`, risk rejected |
| `lame_a_http_request_duration_seconds{endpoint,method,status}` | API latency, and the error rate by status |
| `lame_a_d_ack_latency_seconds` | Latency of D's acknowledgement (monitoring only) |
| `lame_a_order_completion_seconds` | Time from placement to the order's first outcome (the input to load shedding) |
| `lame_a_circuit_state` / `lame_a_shedding_active` | Protection state over time |
| `lame_a_orders_by_status{status}` | In particular `UNKNOWN` and `SENT` |
| `lame_a_notification_lag_seconds` | Time from D generating a notification to its settlement at A; B's own time is not available (section 14) |
| `lame_a_notification_gaps_total` | Gaps answered with `409` |
| `lame_a_duplicates_ignored_total{layer}` | Duplicates caught by each idempotency layer |
| `lame_a_auth_failures_total` | Failed logins |
| `lame_a_notify_failures_total{reason}` | Notifications refused with `422` |
| `lame_a_notify_cursor_age_seconds` | Time since the cursor last advanced |
| `lame_a_orders_unsettled` | Terminal orders not yet closed |
| `lame_a_bg_task_restarts_total{task}` | Background task restarts |
| `lame_a_md_age_seconds{symbol}` | Time since A last received any message from B for that pair |

### 12.2 What the team dashboard computes from A's metrics

The team dashboard is Service C's: a Grafana dashboard fed by Prometheus, which scrapes `service-a:8000/metrics` every 5 s, and the frontend's `/dashboard` page, fed by C's `GET /api/observability`, which scrapes A's `/metrics` on each request (every 5 s while the page is open). A only exports the series; the numbers below are computed from them.

| Number shown | Where | Computed from A's series | How to read it |
| --- | --- | --- | --- |
| A request rate | Grafana | `sum(rate(lame_a_http_request_duration_seconds_count[1m]))` | All API requests per second over the last minute; the SSE stream is not counted |
| A request rate | `/dashboard` | Change of the same `_count` total between C's recent scrapes | The same, over C's own sampling window |
| A order errors | Grafana | `sum(rate(lame_a_orders_total{result=~"SYSTEM_BUSY\|RISK_REJECTED\|NO_FRESH_PRICE\|REJECTED"}[1m]))` | Orders refused because a dependency failed: D down or slow (`SYSTEM_BUSY`), B's feed stale (`NO_FRESH_PRICE`). Business refusals (balance, price band) are excluded. A never records `RISK_REJECTED` or `REJECTED` in this counter (those reach A as notifications), so those two terms are always empty |
| A errors and error % | `/dashboard` | Errors = the same `SYSTEM_BUSY`, `NO_FRESH_PRICE` counts plus `lame_a_auth_failures_total`, `lame_a_notify_failures_total`, `lame_a_md_bad_messages_total` and `lame_a_notification_gaps_total`; error % = errors ÷ all HTTP requests | Totals since A started, not a recent rate. A notification gap is counted although A heals it with `409` |
| A HTTP latency p50 / p95 / p99 | Grafana p95; `/dashboard` all three | `histogram_quantile` over `lame_a_http_request_duration_seconds_bucket`, summed over endpoint, method and status | Grafana: over the last minute; `/dashboard`: since A started. Includes fast reads, so it is lower than the latency of placing an order |
| A order completion p95 (p99) | Grafana; `/dashboard` | The same over `lame_a_order_completion_seconds_bucket` | Placing an order to D's first outcome: the number load shedding acts on (section 10). It rises when D is slow |
| Circuit breaker, load shedding | Grafana; `/dashboard` | `lame_a_circuit_state` (0 closed, 1 half open, 2 open), `lame_a_shedding_active` | `/dashboard` marks A `DEGRADED` while the breaker is open or shedding is on |
| Orders unsettled, notification cursor age | `/dashboard` | `lame_a_orders_unsettled`, `lame_a_notify_cursor_age_seconds` | A cursor age that keeps growing while D has notifications waiting means settlement has stopped. While nothing trades, the age also grows; read it with the request rate |
| A up or offline | `/dashboard` | Whether C's scrape of A's `/metrics` succeeded | Offline means A did not answer within C's 0.35 s scrape timeout |

The method and status labels of `lame_a_http_request_duration_seconds` were added for the dashboard, so that A's server errors (`status=~"5.."`) can be counted directly. The dashboard does not use them yet.

The failure-injection run of 20 Nov on the compose system and its dashboard captures are in `docs/submission/fault_injection.md`.

## 13. Frontend (D15)

React + Vite + TypeScript, built to static files and served by an nginx container on port 3000. The layout follows a typical spot-trading page; LAME uses its own name and logo.

Routes: `/login`, `/trade/:symbol`, `/news-analyst`, `/dashboard`.

| Page | Owner |
| --- | --- |
| Trade (main page) | A |
| News & Analyst | C (a full-page `<iframe>` of C's page inside A's shell) |
| Dashboard | C (system and AI observability from C's `/api/observability`; section 12.2) |

Trade page regions:

| Region | Content | Data source |
| --- | --- | --- |
| Left | Order book: three ask and three bid levels, last price | Browser → B WebSocket 9999 |
| Centre top | Candlestick chart with MA(7), MA(25), MA(99) and volume; intervals 5s, 15s, 1m, 5m | A `/api/market/candles` |
| Centre bottom | Order form: limit and market, buy and sell | A `/api/orders` |
| Right top | Market list (the two pairs): last price, change since the day open | Last price from B's snapshot; day open and precision from A `/api/market/summary` |
| Right middle | Chat box (replaces the "recent trades / my trades" panel) | `<iframe>` of C's `/widget`; A shows an "AI Analyst offline, trading unaffected" message when C cannot be reached |
| Right bottom | Market news for the selected pair | C `GET /api/news?symbol=` |
| Bottom | Tabs: open orders, order history, trade history, holdings | A `/api/orders`, `/api/fills`, `/api/accounts/me`; live updates over SSE |

Internal statuses stay complete, because each outcome moves funds differently. The UI shows simpler labels:

| Internal status | Label | Tab |
| --- | --- | --- |
| `PENDING`, `SENT` | Sending ("Processing delayed" after 10 s) | Open orders |
| `UNKNOWN` | Confirming: "Network problem. We are confirming this order; funds stay held. Do not place it again." | Open orders |
| `PROCESSING` | Open | Open orders |
| `MODIFYING` | Modifying | Open orders |
| `PARTIALLY_FILLED` | Partially filled 0.3 / 1.0 | Open orders |
| Cancel requested, result pending (UI only) | Cancelling | Open orders |
| `FILLED` | Filled | Order history |
| `CANCELLED` | Cancelled, with the filled quantity if any; a market buy's remainder shows "Remainder cancelled" | Order history |
| `REJECTED` | Rejected, with the reason; `END_OF_DAY` shows "Market closed" | Order history |
| `EXPIRED` | Expired | Order history |

Client rules (D15):

- **Data:** TanStack Query holds REST data. All viewers of a chart share one server cache, so polling public market data costs little.
- **Order book:** B may send about 1,000 snapshots per second per symbol. Only the latest is kept, and the view updates about every 100 ms through `useSyncExternalStore`. Both symbols stay connected, since the market list shows both, and each connection knows its pair because messages carry no `symbol`. A side can have zero to three levels, and `last_price` can be null. A message older than the newest held, by `last_ms`, is dropped, and one with an equal `last_ms` is a heartbeat; the client forgets `last_ms` on reconnect. The "interrupted" state comes from 3 seconds without any message.
- **Candles:** Lightweight Charts. The chart polls every second with `after_seq`; a response that changes older candles redraws the series. The chart shows "trade data delayed" while the feed runs but no trades arrive.
- **Authentication:** all calls share one client; a `401` triggers a single refresh shared by concurrent requests. The access token is refreshed before it expires, so the SSE reconnect after the server closes the stream succeeds and carries `Last-Event-ID`.
- **SSE fallback:** `EventSource` stops for good on a `401`, and a new one does not send `Last-Event-ID`. The client therefore records the last `event_id`, refreshes, and opens a new stream with `?last_event_id=`. If the ID is too old, it refetches everything.
- **Merging events:** an event carries the entity's full state and its `event_id`, and is applied only if newer than the cache, so a slow REST response cannot overwrite it.
- **Returning to a hidden tab:** on `visibilitychange`, check the market feed's age, poll candles and check the token.
- **Known limit:** over HTTP/1.1 a browser opens about six connections per host, and each tab's SSE uses one. This does not affect the demo, so it is not addressed.

## 14. How A fits the other services

A adapts to what D and B implement today; nothing here requires them to change. This was checked by reading `feature/risk_control` (`d1635ed`) and `feature/match_engine` (`8b2e3c5`), and by running all services together, as local processes and from the root compose file (18–20 Nov).

| What the service does | What A does |
| --- | --- |
| D keeps resending after `422` and does not alert | A's metrics raise the alert (section 12) |
| D implements modify (11-06) and keeps the price when none is sent | A sends the new quantity only (section 5.4) |
| D's `ts` is its generation time; B's `Trade` carries no timestamp | Candles use D's `ts`. Trades that arrive after an A outage land correctly; those after a D–B outage are bunched (D22) |
| B's market orders have no price cap, and D refuses a price on them | A sends a market buy as a capped limit order and cancels the remainder (section 7.3) |
| B's heartbeat repeats `last_ms` | Freshness is measured from receipt, not from `last_ms` (section 7.2) |
| B accepts orders 00:00–23:00 Dublin time (changed from 09:30–16:00 on 11-06) and cancels resting orders at 23:00 | The UI shows "Market closed" outside those hours |
| B's trading day is the Dublin date, `YYYY-MM-DD` | A uses `b_day` for the day open |
| B reads its pairs from `match_engine/config/pairs.json`: 2 price decimals, 6 quantity decimals, BTC-USDT lot 100 (0.0001 BTC), ETH-USDT lot 1000 (0.001 ETH). D copies `tick` and `lot` from it | One integer unit means the same in A, B and D (team decision, 19 Nov). A's `config/symbols.json` uses B's decimals, tick and lot, including USDT at 6 decimals, and A's own size and value limits. Every `price × qty` goes through one function that converts it to USDT units and refuses a remainder; it is exact because one tick (0.01) times one lot (0.0001 BTC, 0.001 ETH) is a whole number of USDT units, which the config loader checks. `tests/test_pairs.py` fails if A's file drifts from B's or D's |
| D's `linked_accounts.json` fixes user IDs (1024, 1025, 3001–3003) | A seeds those users from the same file; registered users get IDs from 100000 upwards |
| Bots go through A | Bots use the public API with a seeded account. There are no bot-specific features; the rate limit is configuration |
| C's widget sends the pair chosen in its own selector | Accepted as it is; A only embeds it |

**Agreed with the team:**

1. **Price and quantity decimals.** Until 18 Nov A used 3 quantity decimals and lot 1 while B used 6 and lot 100/1000, so B refused A's orders below 0.1 BTC with `INVALID_QTY`, and every quantity was read 1000 times apart. The integration test found it; on 19 Nov the team adopted B's grid. Changing units needs A's, B's and D's data reset together.
2. **Modify.** D's document asks A three questions; A's answers are in section 5.4.

## 15. Deployment

| Aspect | Design |
| --- | --- |
| Image | `python:3.12-slim`, multi-stage, pinned dependencies, non-root user |
| Process | `uvicorn` with **one worker** (D2) |
| Database | PostgreSQL container with a named volume; Alembic migrations run before the app reports healthy |
| Code layout | `service_a/` (backend, Alembic migrations, tests) and `frontend/`, next to the existing `service_c/` and `match_engine/` |
| Ports | 8000: API, SSE, `/internal/order-events`, `/healthz`, `/metrics`. `/internal/*` accepts calls only from the compose network. 3000: frontend |
| Config | A's symbol config and D's `linked_accounts.json`, mounted; env vars for database URL, token secret, rate-limit values, timeouts, shedding threshold, `RESEND_WINDOW` |
| Frontend | nginx container serving the React build and forwarding `/api` to A, so that cookies are same-origin. The browser reaches B and C directly. In development, Vite forwards `/api` the same way |
| Seed | Demo users and the users in `linked_accounts.json`, with starting balances, loaded on first start; deterministic. Bots log in as seeded users |

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
| Load shedding protects the gate | Sliding-window p99 with hysteresis | Delay D's decisions (`decision_delay_ms`) → `SYSTEM_BUSY`; remove the delay → recovery |
| A market buy cannot overdraw | Capped limit order and cancel (section 7.3) | Thin book: cost stays within the hold; remainder cancelled and released; crash before the cancel → `orders.reconcile` sends it |
| Legal late notifications do not stop the line | Transition table (section 5.6) | Late `RISK_REJECTED` after `TOMBSTONED` and any `CANCEL_RESULT` on a terminal order → `200`, no fund movement; notification before the `202` → status not overwritten |
| A partial fill then a self-trade refusal settles correctly | One release rule (section 5.2) | `REJECTED` `SELF_TRADE_PREVENTED` with `filled_qty > 0` releases only the unfilled part |
| The first order of the day is possible | Band skipped with no reference price (section 7.2) | Empty book and fresh feed: limit accepted, market `NO_LIQUIDITY`; stale feed: both `NO_FRESH_PRICE` |
| Only a request that never reached D is reported as failed | Failure classification (section 10) | Connection refused → `503`, hold released, no resend; read timeout → `202`, `UNKNOWN`, hold kept |
| A resent notification cannot be applied twice | Cursor lock, and the cursor commits with the settlement | Two concurrent copies of one notification → one effect; crash after commit and before the answer → the resend is a duplicate |
| A stolen refresh token is detected | Rotation and reuse detection | Reuse a spent refresh token → its family is revoked |
| A browser misses no event | `user_events` and replay | Disconnect, apply notifications, reconnect with the last ID → events replayed in order |

## 17. Implementation order and testing (D29)

| Stage | Content | Branch | Done when |
| --- | --- | --- | --- |
| M0 Sync and contract | Sync both branches with `main`; Pydantic schemas and routes answering `501`; TypeScript types generated from OpenAPI | Both | OpenAPI is generated and the frontend types compile |
| M1 Frontend | Shell, login, the whole trading page, tabs, deposits, C's iframe and news, against mocks | `feature/frontend` | The place-to-fill flow works on mocks; stopping C leaves the trading page working |
| M2 Auth and accounts | Migrations, JWT and refresh rotation, deposits, holdings, seed | `feature/trading` | The frontend uses the real login and deposits; refresh reuse revokes the family |
| M3 Order core | Pre-checks, holds, state machine, fake D, notifications, settlement, `user_events`, SSE | `feature/trading` | Duplicate, gapped and concurrent notifications leave the ledger correct; SSE replays after a disconnect |
| M4 Market | B consumer, candles, summary | Both | A known trade sequence gives the expected OHLCV; stale data rejects orders |
| M5 Resilience | Reconciliation, resends, circuit breaker, shedding, metrics | `feature/trading` | The failure cases in section 16 pass |
| M6 Integration | Real D and B, C's compose file, the proposal's validation scenarios | Both | The proposal's failure tests and dropped-event recovery pass |

M3 runs against a fake D that follows D's code on `feature/risk_control`, so failures can be injected. On 11-17, A, D, B, C and the frontend ran together locally (`service_a/tools/run_local.ps1`), and `service_a/tools/local_e2e.py` passed against the real B and D, including D's modify. Packaging them into the root compose file is still to do.

During M1, MSW mocks A's REST and SSE, a small script stands in for B's market data until the real service is used, and C is the real service.

| Layer | Tool | Covers | Database |
| --- | --- | --- | --- |
| Unit | pytest | Every state-transition combination, pre-checks, token bucket, circuit breaker with a fake clock | None |
| Integration | pytest | Hold races; duplicate, gapped and concurrent notifications; settlement; refresh rotation | PostgreSQL |
| Invariant | hypothesis | Random notification sequences keep balances ≥ 0 and ledger sums equal to accounts; held is 0 at close | PostgreSQL |
| Failure | pytest with fake D | Acknowledgement timeout, connection failure, slowness, crash after commit | PostgreSQL |
| Frontend logic | Vitest, Testing Library | Status labels, MA, order book throttling, SSE merging, shared refresh | None |
| End to end | `tools/integration_check.py`, and a browser pass | Real A against real D, with D's fake of B: placing, matching, settling, a rejection, a cancel, a market buy, D going down | PostgreSQL |

Integration tests use a real PostgreSQL from compose, because correctness rests on `FOR UPDATE`, `CHECK` and conditional updates, whose behaviour SQLite would not reproduce. Load tests and failure-injection scripts for the whole system belong to B and D, as the proposal assigns.

A's and the frontend's compose files are in `service_a/` and `frontend/` for now, on a shared `lame` network; their services move into the root compose file at the final merge, so that these branches do not conflict with it.
