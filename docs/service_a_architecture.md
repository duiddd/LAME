# Service A: Trading & Accounts — Architecture

**Owner:** Pin-Yun Kuo · **Primary objective:** to ensure the balances and assets are correct.

## 1. Role and scope

Service A functions as the exchange’s entry point and serves as the sole authority on users, balances, holds, and order state. Its responsibilities include:
registration and login, simulated deposits, the ledger, holds, pre-checks, placing, modifying and cancelling
orders through D, settlement from D’s notifications, candles, each user’s live updates, and the frontend’s shell
and trading page. Order matching is handled by Service B, risk rules are managed by Service D, and news and the chat box are provided by Service C. The system does not include personal data, KYC, withdrawal, margin, fee, or stop order functionalities.

## 2. Interfaces

**User-facing interface:** Amounts are represented as decimal strings in the API and as integers within Service A. The login process sets two HttpOnly cookies:
a 15 minute access token and a 7 day refresh token.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/auth/register`, `/login`, `/refresh`, `/logout`; `GET /api/auth/me` | Authentication uses only username and password. The refresh token rotates on each use; reusing an invalidated token revokes its entire family. The `/me` endpoint returns the user role (`USER` or `ADMIN`). |
| `GET /api/accounts/me`; `POST /api/accounts/deposits` | Provides balances (available, held, total) and supports simulated deposits using an `Idempotency-Key`. |
| `POST /api/orders`; `PUT`, `DELETE`, `GET /api/orders/{id}`; `GET /api/orders`, `/api/fills` | Place (with `Idempotency-Key` = `client_order_id`), modify the remaining quantity, cancel, list |
| `GET /api/market/candles`, `/api/market/summary` | Provides OHLCV data from settled trades, including each trading pair’s daily opening price and decimal precision. |
| `GET /api/stream` | Delivers Server-Sent Events for user order and balance changes, with support for resumption from the `Last-Event-ID`. |
| `GET /healthz`, `/metrics` | Exposes readiness information (database, background tasks, circuit breaker, load shedding) and Prometheus-compatible metrics. |

A refusal response includes a specific reason, such as `INSUFFICIENT_BALANCE`, `PRICE_OUT_OF_BAND`, or `NO_FRESH_PRICE`.
Other possible reasons include `INVALID_LOT` or `SYSTEM_BUSY`. A successful placement returns HTTP `202`; HTTP `503` is returned only if the request did not reach Service D.
Write endpoints accept only JSON payloads and reject requests with a foreign `Origin`. Cross-Origin Resource Sharing (CORS) is not enabled.

**Interfaces to other services:**

| With | How | What |
| --- | --- | --- |
| D | HTTP `POST /v1/orders`, `/v1/cancels`, `/v1/modifies`; `GET /v1/orders/{id}`; `POST /v1/notifications/rewind` | Commands with A’s `order_id`. D’s answer only means “received” |
| D → A | HTTP `POST /internal/order-events` (compose network only) | One outcome at a time, each one numbered in sequence by a contiguous `notify_seq` |
| B | WebSocket `/md/<pair>` (read only) | Top 3 book and last price for price checks (the browser also draws the order book from this) |
| Service C | The browser embeds Service C’s chat widget and news feed. Service C retrieves metrics from Service A’s `/metrics` endpoint for its dashboard. | Service A does not initiate calls to Service C, ensuring that Service C’s state does not impact trading operations. |
| Shared files | `linked_accounts.json` (owned by Service D) and the trading pair grid in B’s `pairs.json` | These files define seeded users, their identifiers, and the interpretation of a single integer unit. |

**D’s notifications.** D sends one at a time and resends it until A answers `2xx`. A answers `200` after
committing (also for a duplicate), `409 {"expected_seq": N}` when it sees a gap (D resends from N) and `422` for
a notification it cannot apply, which stops the line. The six types are `RISK_REJECTED`, `ACCEPTED` (with B’s
`order_seq`), `TRADE` (both sides), `ORDER_UPDATE`, `CANCEL_RESULT` and `MODIFY_RESULT`.

## 3. Structure

The architecture consists of a single Python process (using FastAPI with one uvicorn worker) and a single PostgreSQL database.

```
Browser ─► auth ─► orders.precheck (rate limit → size → price) ─ fail ─► 4xx
                          │ pass        breaker open or shedding ─► 503, nothing held
                          ▼
            one DB transaction: order PENDING + hold + ledger entry
                          ▼
            orders.dclient ─HTTP─► D    received ─► SENT
                                        certainly not sent ─► REJECTED, released, 503
                                        sent, no reply ─► UNKNOWN, hold kept, 202
D ─POST /internal/order-events─► orders.events: lock cursor, dedupe, check order
                          ▼
            one DB transaction: status, settlement, release, user_events, cursor
                          ▼ after commit
Browser ◄─SSE─ push          orders.reconcile: resends, queries stuck orders
B ─WS─► market: price cache, candles from settled trades
```

| Module | Does |
| --- | --- |
| `auth` | Manages accounts, performs Argon2id password hashing within a capped thread pool, issues JWT access tokens, handles refresh token rotation, and manages user roles. |
| `accounts` | Solely responsible for writing balance changes; each modification is recorded as a ledger entry. |
| `orders` | Handles order placement, modification, and cancellation, and maintains the state-transition table. |
| `orders.precheck`, `orders.dclient` | Conduct pre-checks, initiate calls to Service D with timeouts, classify failures, and implement a circuit breaker mechanism. |
| `orders.events`, `orders.reconcile` | Apply notifications, resend unanswered orders, and query orders that are stuck. |
| `market`, `push`, `shedding` | Manage Service B’s market data feed and candles, handle Server-Sent Events, and perform load shedding. |

Dependencies are unidirectional (router to service to repository); modules interact through each other’s services rather than directly accessing each other’s tables.
Stateful components, such as the rate limiter, circuit breaker, price cache, and SSE hub, are instantiated once within `container.py`.
Background tasks are managed by a supervisor, which restarts them as needed and reports their status via the `/healthz` endpoint.

## 4. Orders and money

**States.**

```
PENDING ─received─► SENT ─ACCEPTED─► PROCESSING ─► PARTIALLY_FILLED ─► FILLED
   │ no reply                 └─ any time: CANCELLED / REJECTED / EXPIRED
   ▼
UNKNOWN (hold kept; same order_id resent for 5 s, then cancelled)
```

According to rule ADR-002, once a request may have reached Service D, funds are moved exclusively in response to notifications from Service D. Funds are never moved based on a timeout, HTTP `202` response, or query.
Only requests that are confirmed to have never left Service A are released immediately.

| Notification | Effect on the hold |
| --- | --- |
| `RISK_REJECTED` | Release all |
| `ORDER_UPDATE` cancelled, rejected or expired | Release the unfilled part; the filled part waits for its trades |
| `CANCEL_RESULT` tombstoned or not found | Release what is left (D never had the order) |
| `TRADE` | Settle that quantity for both sides in one transaction; the buyer gets back any price improvement |

**Holds** are managed with a single SQL statement: `UPDATE accounts SET available = available - x, held = held + x WHERE … AND available >= x`. If zero rows are affected, this indicates insufficient funds. The constraint `CHECK (available >= 0 AND held >= 0)` serves as a safeguard.
An order is considered closed when its status is final and all filled quantities have been settled; at this point, no funds remain on hold.

**Amounts** are represented as integers on Service B’s grid, which is shared among Services A, B, and D. Prices use two decimal places, quantities use six, with a BTC lot size of 0.0001, an ETH lot size of 0.001, and USDT with six decimals.
Every price multiplied by quantity is processed by a single function that converts the result to USDT units and rejects any remainder.
One tick multiplied by one lot always results in a whole USDT unit, ensuring that no rounding occurs.
The script `tests/test_pairs.py` fails if Service A’s grid diverges from that of Service B or D.

**Pre-checks** are performed in the following order: a per-user token bucket (5 orders per second, burst capacity of 10; cancellations have a larger bucket); size constraints;
tick, lot, minimum, maximum, and value constraints; price validation, which requires that Service B’s feed has updated within the last three seconds, and that a limit price
must be within ±10% of the reference price (defined as the last trade price, or the mid-price if no trades have occurred; this check is skipped before the first price of the day). A **market buy** order is submitted as a limit order capped at best ask multiplied by 1.05, with any remainder cancelled,
because Service B’s market orders do not have a cap and could otherwise spend more than the held amount.

**Order modification** affects only the remaining quantity, as per team decision: increasing the quantity requires additional funds to be held first; decreasing the quantity results in funds being released only after confirmation from Service D, since the original order may still be filled.

**Applying a notification**: in one transaction A locks the cursor (`SELECT … FOR UPDATE`); a number at or below
it's a duplicate (`200`), one above the other there's a gap (`409`), the next one is applied with the cursor advanced.
If Service A crashes before responding, Service D will resend the notification, resulting in a duplicate. Each notification is therefore processed exactly once.
A notification that would violate a rule results in a `422` response and halts further processing. Maintaining ledger integrity is prioritized over continuous operation.

## 5. Idempotency, resilience and consistency

| Duplicate | Key | Enforced by |
| --- | --- | --- |
| Double click or resent request | `client_order_id` | `UNIQUE (user_id, client_order_id)`: the existing order is returned |
| A resending an order to D | A’s `order_id` (ULID) | D deduplicates |
| D resending a notification or trade | `notify_seq`; B’s `(day, seq)` for trades | The cursor, the `trades` key and the ledger’s unique key, in one transaction |

| Failure | What A does |
| --- | --- |
| Connection to D refused | Order `REJECTED`, hold released, `503`, never resent |
| Sent to D, no reply | `UNKNOWN`, hold kept, “Confirming” in the UI; same `order_id` resent with backoff for 5 s, then cancelled |
| D failing | Circuit breaker opens after 5 failures for 10 s: new orders get `503` before anything is held; cancels are queued |
| D slow | Load shedding: if the p99 time from sending to D’s first outcome stays above 1 s for three checks in a 30 s window, new orders get `503` until it falls below 80% |
| Accepted, then silent | Queried at D after 10, 20, 40 s, then every 60 s; a missed notification is resent with `rewind`. A query never moves funds |
| A crashes | `PENDING` orders become `UNKNOWN`; D resends what A did not confirm |
| B’s feed silent for 3 s | Orders refused `NO_FRESH_PRICE` |
| C down | Only the chat and news panels show “unavailable” |

The following operations are strongly consistent, each executed within a single PostgreSQL transaction: registration, deposits, balances, holds, and order acceptance.
Order status and settlement are eventually consistent, as they depend on notifications from Service D (transition from placing to `PROCESSING`
with a median latency of 228 ms measured on the test laptop). The browser achieves eventual consistency through replay: each change records a `user_events` row
in its transaction, and Server-Sent Events replay from the `Last-Event-ID` after a reconnect. While Service D is unavailable, funds associated with in-flight
orders remain frozen. This approach ensures that settlement never occurs against released funds.

## 6. Observability

`/metrics` exports, among others: `lame_a_orders_total{result}`, `lame_a_http_request_duration_seconds{endpoint,method,status}`, `lame_a_order_completion_seconds` (input to shedding), `lame_a_circuit_state`,
`lame_a_shedding_active`, `lame_a_orders_by_status{status}` (watch `UNKNOWN`), `lame_a_orders_unsettled`,
`lame_a_notify_cursor_age_seconds` and `lame_a_notify_failures_total` (settlement stalled or stopped),
`lame_a_md_age_seconds{symbol}`.

The team dashboard (implemented using Grafana with Prometheus as the data source, and the frontend’s `/dashboard` page from Service C’s aggregator) derives its metrics from these sources.
The monitored metrics include A’s request rate (measured by the request counter), errors (orders refused due to unavailability of D or B),
HTTP 5xx errors as indicated by the status label, p95 latency for both requests and order completion, breaker and shedding events,
and settlement pipeline metrics, including D’s outbox backlog and A’s orders that are in doubt or unsettled. The error rate displayed on the page represents
the total since A was started, rather than a recent or rolling rate.

Only users with the `ADMIN` role can view the Dashboard link; all other users attempting to access `/dashboard` are redirected to the trade page. The role is
stored as a column in the `users` table (assigned only to the seeded `admin`; registration does not grant this role) and is included in the access token
and the `/api/auth/me` endpoint. This approach hides the page but does not secure C’s data endpoint or Grafana.

## 7. Frontend and Deployment

The frontend utilizes React, Vite, and TypeScript, served by nginx on port 3000. Nginx forwards `/api` requests to A to ensure cookies remain same-origin.
The trading page displays the order book (sourced directly from B and updated approximately every 100 ms), candles with moving averages,
an order form, a market list, C’s chat box and news, and tabs for open orders, history, fills, and holdings, all of which are kept
live using Server-Sent Events (SSE). C’s page and the dashboard are implemented as separate routes.

A’s container image is based on `python:3.12-slim`, uses a multi-stage build, and runs as a non-root user. Database migrations are executed before the service reports as healthy. Port 8000 is not
published; browser traffic is routed through nginx, and `/internal/order-events` is accessible only within the Docker Compose
network. The root `docker-compose.yml` file includes A’s dedicated compose file along with its PostgreSQL instance.

## 8. Key Decisions

| Decision | Rationale | Rejected Alternative | Cost |
| --- | --- | --- | --- |
| Python 3.12, FastAPI | Consistent stack with C and D; asynchronous operations are suitable for database and D interactions | Java/Spring (higher memory usage, inconsistent with group) | Lower raw throughput, but sufficient for demonstration purposes |
| Single process, single worker | Rate limiter, breaker, and shedding window are maintained in memory | Multiple workers and Redis | Limited to one CPU core; notification handling is already safe for multiple instances |
| D only acknowledges; outcomes are delivered as notifications | Enables fast placement; ensures a single ordered source for each outcome | Synchronous final response from D | Settlement is paused during D outages |
| Release occurs only upon notifications (ADR-002) | A fill cannot settle against released funds | Release on timeout; two-phase commit | Funds remain frozen during D outages; introduces an `UNKNOWN` state |
| PostgreSQL with conditional-update holds | Utilizes row locks and `CHECK` constraints to prevent read-then-write races | `SELECT … FOR UPDATE`; optimistic versioning; SQLite | Requires one additional container |
| Integer amounts are used on B’s grid | Eliminates rounding errors; ensures the ledger balances exactly | Floating-point values; A’s own grid (previous drift: B refused A’s small orders) | Units must be updated consistently across A, B, and D |
| Apply notifications in order, one per transaction | Exactly-once effect; correctness kept by a database lock | Inbox table and worker; skipping bad ones | 29–46 notifications/s measured: the first bottleneck |
| Shedding is implemented in A, with D’s rules always enforced | Refusal occurs at the entry point rather than weakening risk checks | Disabling a rule under load | Some users are refused during periods of overload |
| Market buy orders are implemented as capped limit orders | Ensures cost never exceeds the held amount | Holding the entire balance; requires changes to B and D | The remaining balance is held briefly before cancellation |
| SSE is used from a per-user event log | Provides one-way push; ensures no events are missed after a reconnect | WebSocket; refetching all data | Requires one table and periodic cleanup |
| Candlestick data is derived from A’s settled trades | B does not maintain history; A observes every trade | Sampling snapshots; maintaining a candles table | The chart is initially empty at first startup |
| JWT access token, rotating refresh token, and Argon2id with a cap are used | Eliminates per-request lookups; enables theft detection; logins cannot block order processing | Server sessions; bcrypt | A copied access token remains valid for up to 15 minutes after logout |
| Admin role is stored in the database | Usernames alone cannot grant admin privileges | Admin names stored in an environment variable | Restriction is enforced only at the page level |

## 9. Testing and Evidence

A total of 220 tests are executed against a real PostgreSQL instance, as correctness depends on its locks and constraints. These tests cover every state transition, including concurrent holds (100 simultaneous holds never result in overdrawing), as well as duplicate, gapped, and concurrent notifications, random notification sequences that must maintain ledger consistency (using hypothesis), failure cases with a mock D, and the pair-grid contract. The `tools/local_e2e.py` script executes 26 checks against the real B and D; `tools/fault_demo.py` and `tools/consistency_demo.py` inject failures and verify ledger consistency. Their results from 20 November on the compose system are documented in `docs/submission/fault_injection.md`.
