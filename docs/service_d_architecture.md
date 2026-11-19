# Service D: Risk Control — Architecture

**Owner:** Zongyan Yang · **The one question it answers:** Should this order be let through? · **Status:** draft, updated on 2026-11-06 after the team discussion and after reading A's and B's latest code (A's `feature/trading`, B's `resume_ok`).

This document describes the design of Service D. The reason behind each choice is in [service_d_tech_decisions.md](service_d_tech_decisions.md) (decision IDs RD1, RD2, …; the R prefix avoids confusion with D1, D2 in the A, B and C documents). The interfaces are in [service_d_protocol.md](service_d_protocol.md).

## 1. Role and scope

D is the risk gate between A and B. For every order from A, D either lets it through to B or rejects it. D is purely rule-based: the same input always gives the same decision, and no AI is used. D is on the critical path of order placement, so its latency and availability affect order placement for the whole exchange.

- **In scope:**
  - Two rules (self-trade, wash trade); trader features built from B's outputs; versioned rules; failing closed.
  - Taking A's orders, cancels and modifies idempotently; the synchronous reply only means "received" (RD11).
  - Notifying A of the risk decision and of all of B's results, **in order and reliably** (RD6, RD25).
  - Translating between A's ULID and B's integer order IDs (RD24).
  - A status query by `order_id` for A's reconciliation (RD26).
  - Turning a modify into "cancel + new order" (RD27).
  - Making sure every order reaches a final state (automatic cancel after a timeout, section 7).
  - Rule evaluation; failure-injection scripts and runbook (shared work in the proposal); leading the ADR for the synchronous risk gate.
- **Out of scope:** AI or machine learning; a human review queue; balance, order-size and rate-limit checks, and load shedding (A); matching and trading hours (B); news and Q&A (C); automatic account suspension; forced liquidation and overnight margin (a stretch goal, not in the core, RD20).

## 2. Relationship with the other services

```
              ① HTTP: place, cancel, modify (the synchronous reply only means "received")
              ④ HTTP: query by order_id
   A ─────────────────────────────────► D ══════ ② WS /d (commands + all of B's outputs) ══════ B
   ▲                                    │
   └──────── ③ HTTP: notify every outcome ┘

   Shared config files: linked accounts, trading pairs (read by A, B and D)
   C: no business data with D; Prometheus scrapes D's /metrics
```

| Link | Method | Content |
| --- | --- | --- |
| ① A → D | HTTP + JSON | Place, cancel, modify. D answers 202 for "received" and 503 for "not taken" |
| ② D ↔ B | WebSocket + JSON (B's `runtime.md`) | D sends commands; B sends all of its outputs to D |
| ③ D → A | HTTP + JSON, A's `POST /internal/order-events` | Risk rejections, B's acceptance, trades, state changes, cancel results, modify results. With a contiguous `notify_seq`, sent in order, resent until A acknowledges |
| ④ A → D | HTTP + JSON | A queries an order's state during reconciliation |

**D forwards B's events to A (RD6).** An earlier draft suggested that B give A a read-only subscription. But both A's and B's documents have D forwarding, and B's implementation only allows one `/d` connection. So the design is now: for every output from B, D translates it to A's order ID and sends it to A as a notification. The cost is that while D is down, A's settlement also pauses and funds stay frozen. This goes into the ADR.

**Commands go only through D (RD7).** B has only one command entry, which is D, so A's orders, cancels and modifies all pass through D. Cancels are not checked against the rules, but D controls when they are sent (section 7).

## 3. Rules

Both rules are pure functions `decide(order, feature snapshot, rule version) → allow / reject (reason)` (RD1).

Notation: the **linked group** G(u) of user u is u and all accounts linked to it; with no linked accounts, G(u) = {u}. For a trading pair, u's **opposite orders** are u's orders on the other side that are **resting in B's book**, plus **orders D has approved that B has not answered yet** (in flight, RD4).

### R1 Self-trade (SELF_TRADE)
Reject a new order if it could trade against one of u's **own** opposite orders.

- **Limit buy at price P:** reject if u has a resting sell at a price ≤ P. A limit sell is the mirror case: u has a resting buy at a price ≥ P.
- **Market order for quantity Q:** simulate filling it against D's book view in price-time priority until Q is used up; reject if it reaches one of u's own orders on the way.

Limit orders are judged by whether the prices cross. This is conservative, but it is simple, easy to explain, and does not depend on the other orders in the book view being exact. Market orders have no price, so they can only be simulated (RD3).

D's check is best effort: there is always some lag between in-flight orders and B's real state. B's matcher has an atomic backstop (B document D24): when an order meets a resting order of the same `user_id`, B cancels the rest of the incoming order with reason `SELF_TRADE_PREVENTED`. D forwards this to A as an ordinary cancel result.

### R2 Wash trading (WASH_TRADE)
The same check as R1, but with "u itself" replaced by "the accounts in G(u) other than u" (RD2). The linked group is treated as one trader for the self-trade check. A user with no linked accounts never triggers R2. B's backstop only compares `user_id` and ignores linked accounts, so only D does R2.

Optional extension (not in the v1 rules): after k trades inside a linked group within a window W, raise the level of blocking. Whether to enable it depends on the evaluation.

### Evaluation order and reason codes
R1 first, then R2; stop at the first match.

| Reason code | Meaning |
| --- | --- |
| `SELF_TRADE` | R1 matched |
| `WASH_TRADE` | R2 matched |
| `RISK_TIMEOUT` | No decision within `GATE_DEADLINE` |
| `CANCELLED_BEFORE_SUBMIT` | A cancel for this order ID arrived earlier (RD14) |

When D is not ready or its decision queue is full, the order never enters D: D replies 503 `RISK_UNAVAILABLE` / `RISK_BUSY` at once. This is not a rejection (section 8).

## 4. Internal structure

D is one Python process (FastAPI on uvicorn, **a single worker**), running as one instance (RD19). It must be a single instance because B accepts only one D connection at a time, and D's state is in memory.

```
A ──HTTP──► [API layer] ──202──► A
               │ new orders / modifies                cancels
               ▼                                        ▼
          [decision queue] ──► [decision task] ──read──► [features / book view] ◄── B's outputs
                                     │                        ▲
                                     │ decision, b_order_id   │
                                     ▼                        │
         [cancel scheduler] ──► [store] ──► SQLite d.db       │
               │                  │ after commit              │
               ▼                  ▼                           │
          [B connector] ══════ WS /d ═════════════════════════╪══► B
               │                                              │
               └── B's outputs ──► [notification builder] ──► outbox (d.db) ──► [notification sender] ──HTTP──► A
```

- **API layer:** validates requests and checks idempotency; stores a new order as `RECEIVED`, puts it into the decision queue and replies 202 at once; replies 503 when the queue is full or D is not ready; hands cancels to the cancel scheduler; answers queries.
- **Decision task:** one asyncio task that takes orders from the decision queue one by one, so D's decisions have a total order (RD10). The rules read only memory and do no I/O.
- **Store:** one SQLite connection, read and written synchronously inside the event loop (each call takes well under a millisecond). The process has only this one connection, so it is the only writer. Decisions, `b_order_id`s and generated notifications are committed before they are sent (RD8).
- **Cancel scheduler:** decides when each cancel is sent to B, following section 7, including the automatic cancels for timed-out orders.
- **B connector:** keeps the single WebSocket to B; does the resume handshake, deduplicates by `(day, seq)`, applies outputs in chain order and decides when D has caught up; updates the features from B's outputs.
- **Notification builder:** translates each output from B to A's order ID, builds the matching notification, and writes it to the outbox together with "how far into B's outputs D has got" (in the same transaction).
- **Notification sender:** one asyncio task that sends notifications to A one by one in `notify_seq` order, and sends the next only after A answers 2xx; handles `409 expected_seq` (RD25).

## 5. State and storage

One SQLite file `/data/d.db` in WAL mode, on a named volume. D does **not** keep a copy of B's outputs (RD15), but it keeps the notifications built from them. Tables:

| Table | Main fields | Purpose |
| --- | --- | --- |
| `orders` | `user_id`, `order_id`, order content, `state`, `filled_qty`, `received_at` | A's orders and their state inside D; idempotency; queries |
| `decisions` | `decision_no` (D's own counter), `order_id`, `decision`, `reason`, `rule_version`, `features_seq`, `decided_at` | Audit, replay (RD9) |
| `b_orders` | `b_order_id` (D's global counter), `order_id`, `user_id`, `sent_at`, `b_day`, `order_seq` | The ID map; after a modify, one `order_id` has several rows (RD24) |
| `cancels` | `order_id`, `user_id`, `origin` (A or TIMEOUT), `tombstone`, `b_order_id`, `send_not_before`, `sent_at`, `result` | Cancel intents, automatic cancels and tombstones (RD13, RD14) |
| `outbox` | `notify_seq`, `order_id`, `type`, `payload` | The outbox (RD25) |
| `meta` | `b_cursor` (the `(day, seq)` of the last B output that was turned into notifications), `delivered_seq` (the last notification A acknowledged) | No duplicate notifications built or sent after a restart |

State in memory:

- **Book view:** per trading pair, `order_seq → (user_id, side, price, remaining)`. The price and side come from D's own records (every order that enters B passes through D), and the remaining quantity from B's `order_event`s (RD5).
- **In-flight orders:** orders D has approved that have no output from B yet.
- **Linked groups:** `user_id → group`, read from the shared file at start-up.

**Recovery after a restart:**

1. Read the orders, decisions, ID map, cancel intents, outbox and cursors back from `d.db`.
2. Send `{"type":"resume","from_seq":0}` to B and apply again from the first output of the day, rebuilding the book view.
3. During the replay, outputs up to the cursor only update the features and build no notifications; later outputs build notifications as usual.
4. D is ready only after catching up (section 8). Unacknowledged notifications in the outbox continue to be sent to A.

At course scale the day's outputs are small, so the rebuild is fast (to be measured in the load test).

At the end of the day (after B's `END_OF_DAY` cancels), the book view and in-flight orders empty naturally; linked groups, past decisions and the outbox are kept.

## 6. Order states inside D

```
                       ┌──► REJECTED_BY_D (final; guaranteed never sent to B)
RECEIVED ──decision─────┤
                       └──► IN_FLIGHT ──B's first output──► LIVE ──► FILLED / CANCELLED (final)
                                 │                      │   └──► REJECTED_BY_B (final, incl. EXPIRED)
                                 │ no output within      │
                                 │ D_TIMEOUT             └──modify──► MODIFYING ──► LIVE (new b_order_id)
                                 ▼
                             TIMED_OUT ──output arrives later──► LIVE / REJECTED_BY_B
                                 │ automatic cancel gets ORDER_NOT_FOUND, and no output was ever seen
                                 ▼
                          NOT_DELIVERED (final)

(cancel arrives before the order) CANCEL_TOMBSTONE ──order arrives──► REJECTED_BY_D (CANCELLED_BEFORE_SUBMIT)
```

The state is returned to A by the query endpoint (protocol document 2.4). A decides an order's fate from D's notifications.

## 7. Timeouts and order fate

B has no "lookup by ID" and no "tombstone". What B has is this: a `new_order` carries `sent_at`, and when B records it with `ts`, it answers `REJECTED(EXPIRED)` if `ts − sent_at > MAX_COMMAND_AGE` (B document D15). In B's implementation `MAX_COMMAND_AGE` is **1000 ms**, fixed at build time. D uses this to get the effect of a tombstone (RD13):

1. When D sends a `new_order`, it records `sent_at`. If an output for the order arrives within `D_TIMEOUT` (≥ 1000 ms), the order becomes `LIVE`.
2. If not, the order becomes `TIMED_OUT`. B's document requires D to cancel after a timeout, so D sends `cancel_order` **automatically** after `sent_at + MAX_COMMAND_AGE + CANCEL_MARGIN`. **A's hold stays in place meanwhile.**
3. If an output for the order arrives before the cancel is sent (B was only slow), the automatic cancel is dropped.
4. B's answer:
   - `CANCELLED`: the order was on the book and is now cancelled. A gets `ORDER_UPDATE CANCELLED` with reason `D_TIMEOUT`.
   - `REJECTED(ORDER_NOT_FOUND)`: B's implementation does not tell "never arrived" from "already final", so D decides itself. If D has received outputs for the order, it is final and those outputs stand. If not, **the order never reached B and never will** (the age limit has passed, so a late `new_order` would get `EXPIRED`), and A gets `ORDER_UPDATE REJECTED` with reason `NOT_DELIVERED`.
5. While D is disconnected from B, automatic cancels wait. After reconnecting and catching up, D checks again: if it has seen outputs for the order by then, it does not cancel.
6. D and B run on the same host, so their clocks can be compared. B's D15 already relies on this. `CANCEL_MARGIN` is a safety margin.

Cancels from A: for an order that is already `LIVE`, the cancel is sent to B at once; for an `IN_FLIGHT` / `TIMED_OUT` order, it also waits until after the age limit.

D also guarantees that **an order D rejected is never sent to B** (RD12). So A can release the hold as soon as it receives `RISK_REJECTED`.

As long as D is running, every order reaches a final state: decisions have `GATE_DEADLINE`, and orders sent to B have the automatic cancel. A only waits indefinitely while D is down.

## 8. Readiness and failing closed

D takes new orders only when it is **ready**; otherwise it replies 503 `RISK_UNAVAILABLE` at once and keeps no record (RD16). Ready means all of:

1. The linked accounts and trading pairs have been read from the shared files.
2. The WebSocket to B is connected and the resume handshake is done.
3. Caught up: the `prev_seq` chain has no gap up to `through_seq` from B's `resume_ok` (the last output B had stored when the handshake was served). For a B without `resume_ok`, D falls back to a quiet period of `CATCHUP_QUIET_MS`.
4. The last write to SQLite succeeded. If a write fails, D stops taking orders; it does not degrade.

If the decision queue is full, D replies 503 `RISK_BUSY` at once. If an order waits in the queue past `GATE_DEADLINE` without a decision, it is rejected with `RISK_TIMEOUT`, and A is notified.

Cancels do not depend on readiness. While D is disconnected from B, cancels are queued and sent after reconnecting and catching up.

A being down does not affect readiness: notifications queue in the outbox and are sent when A is back.

## 9. Determinism and replay

The decision for an order depends on "which of B's outputs the features had reached at decision time" and "which orders were in flight then". So every decision records `decision_no`, `features_seq` (the largest `(day, seq)` of B's outputs applied at decision time) and `rule_version` (RD9).

Replay: process the decisions in `decision_no` order; before each one, apply B's outputs up to its `features_seq` from B's event store to the features; in-flight orders follow from the earlier decisions and the outputs applied so far. Each recomputed decision should match the recorded one exactly. This covers acceptance criterion 1. B's event store is the source of truth for orders and trades, so the replay uses B's data, and D does not need its own copy.

## 10. Rule versions

The rules and their parameters are in a config file (for example `rules/v1.json`: which rules are enabled, and the parameters of the optional extension). `rule_version` = the file name + the first characters of the content hash. Changing any parameter gives a new version (RD18). D loads one version at start-up and does not switch while running.

## 11. Evaluation

- **Data:** in the proposal, the bots (including the labelled manipulators) belong to B, and they are not written yet. D only sets the label format: one record per order, `{order_id, user_id, label: LEGIT | SELF_TRADE | WASH_TRADE, scenario_id}`, with `order_id` as A's ULID. The bots place orders through A's API, so the orders pass through D. Bo designs the scenarios and D takes no part, so the evaluation is independent; the scenarios can include cases D will not catch.
- **Metrics:** join the `decisions` table with the labels on `(user_id, order_id)`, and compute offline:
  - precision and recall for each rule;
  - false positive rate = rejected LEGIT orders / all LEGIT orders;
  - decision latency p99 (from receiving the order to writing the decision), and D's hop in the end-to-end placement latency.
- **Scenario:** the defining scenario in the proposal (bots sending N orders per second across two pairs). N and D's share of the latency budget will be set after the baseline.

## 12. Failure injection and runbook

D's fault-injection switches exist only in the test profile (RD21). Scenarios:

| Scenario | How it is triggered | Expected result |
| --- | --- | --- |
| D slow | D switch: extra delay of X ms per decision | Orders past `GATE_DEADLINE` get `RISK_TIMEOUT`; when the queue is full, `RISK_BUSY`; A starts shedding load based on the latency of the risk result |
| D down | `docker kill` D | A's order calls fail, orders become `UNKNOWN`, holds stay; **settlement also pauses**; after D restarts and catches up, unacknowledged notifications are sent again and every order reaches a final state |
| Stale features | D switch: stop applying B's outputs | D sees that it has not caught up and answers 503 `RISK_UNAVAILABLE`; no decision is made on stale features |
| B accepted, reply lost | D switch: drop the B connection right after sending `new_order`, reconnect after X seconds | When D resumes it receives `ACCEPTED` and the trades, drops the automatic cancel, and notifies A as usual |
| B killed while an order is `TIMED_OUT` | `docker kill` B after an order is `TIMED_OUT` | The automatic cancel waits in D, holds stay; after B recovers, D catches up and gets a definite result |
| Lost notification | D switch: skip one `notify_seq` | A answers `409 expected_seq`, D resends from there, and balances are correct |

The runbook for each scenario has three parts: how to detect it (which metrics and logs), how to handle it, and how to confirm recovery.

## 13. Deployment, configuration and monitoring

| Aspect | Design |
| --- | --- |
| Image | `python:3.12-slim`, pinned dependencies, non-root user |
| Port | **8400**: the HTTP interface for A, `/healthz`, `/metrics`. Open only inside the compose network |
| Storage | Named volume `/data`, holding `d.db` |
| Config files | The shared folder `config/` (linked accounts, trading pairs) and `rules/` are mounted |
| Start-up order | `depends_on` B with `service_healthy`; `/healthz` is unhealthy until ready |
| Restart | `restart: unless-stopped`; recovery as in section 5 |
| Network | D's network must be in B's `CTRL_ALLOWED_CIDRS` |

Configuration (environment variables): `D_PORT`, `B_CTRL_URL`, `A_NOTIFY_URL`, `D_TIMEOUT_MS`, `MAX_COMMAND_AGE_MS` (must equal B's, currently 1000), `CANCEL_MARGIN_MS`, `GATE_DEADLINE_MS`, `DECISION_QUEUE_CAP`, `CATCHUP_QUIET_MS`, `GAP_TIMEOUT_MS`, `LINKED_ACCOUNTS_FILE`, `SYMBOLS_FILE`, `RULES_FILE`, `DATA_DIR`, `FAULTS_ENABLED`.

Metrics: decision latency (histogram), rejections by reason, number of 503s, decision queue length, number of `TIMED_OUT` orders, cancels waiting to be sent, outbox backlog, notifications acknowledged, connection and readiness state, feature lag.

## 14. How the design meets the acceptance criteria

| Acceptance criterion | Mechanism |
| --- | --- |
| 1. Replaying the same order sequence gives the same decisions | The rules are pure functions; every decision records `features_seq` and `rule_version`; replay as in section 9 |
| 2. Report precision, recall and false positive rate | The offline evaluation in section 11 |
| 3. An approved order reaches B at most once; a rejected order is reported to A exactly once | Idempotency key `(user_id, order_id)`; decide and write before sending; notifications have contiguous numbers and A deduplicates by `notify_seq` (RD8, RD25) |
| 4. No order is left hanging when D is force-stopped | All state is in the database; after a restart D catches up with B, and the automatic cancel gives every order a final state (section 7); unacknowledged notifications are sent again |
| 5. Decision p99 within D's share of the budget | The rules read only memory; decisions are serial but short; monitored by metrics; the share is set after the baseline |

## 15. Needed from other services

- **A (Pin-Yun):**
  - Base load shedding on the time from placement to the risk result (A's `lame_a_order_completion_seconds`), not on the latency of D's 202.
  - Confirm that `POST /v1/notifications/rewind` meets A's need to "ask D to resend" during reconciliation.
  - Done: A reads the same `linked_accounts.json` and uses integer `user_id`s; A answered the modify questions (A document 5.4).
- **B (Bo):**
  - Progress on the bots (including the labelled manipulators), and labels in D's format.
  - The wash-trading bots use the accounts in the shared linked-accounts file.
  - Done: `resume_ok` with `through_seq`.
- **C (Rushabh):** Prometheus scrapes D's `/metrics` (8400), and the dashboard adds D's metrics.
- **Everyone (later):** who owns the root `docker-compose.yml`; the ADR.

## 16. Open items

1. **Timeout values:** `D_TIMEOUT` (≥ 1000 ms), `CANCEL_MARGIN`, `GATE_DEADLINE`, `DECISION_QUEUE_CAP`, set after the baseline.
2. **Optional extension of R2:** whether to enable the window condition depends on the evaluation.
3. **One shared pair file:** B reads `match_engine/config/pairs.json`, and D's `config/symbols.json` copies its tick and lot (11-19). D can read B's file directly through `SYMBOLS_FILE` once the file has a shared place in the root compose.
