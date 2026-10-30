# Service D: Risk Control — Architecture

**Owner:** Zongyan Yang · **The one question it answers:** Should this order be allowed? · **Status:** draft, for team discussion

This document describes the design of Service D. The reason for each choice is recorded in [service_d_tech_decisions.md](service_d_tech_decisions.md) (decision IDs RD1, RD2, …; the R prefix avoids confusion with the D1, D2 IDs in the B and C documents). The interfaces are in [service_d_protocol.md](service_d_protocol.md).

## 1. Role and scope

D is the risk gate between A and B. For every order A hands over, D either forwards it to B or rejects it back to A. D is entirely rule-based: the same inputs always give the same decision, and no AI is involved. D sits on the order path, so its latency and availability affect order placement for the whole exchange.

- **In scope:** two rules (self-trade, wash trading); trader features built from B's outputs; idempotent decisions and forwarding; rule versioning; failing closed; passing A's cancels to B and controlling when they are sent (section 7); rule evaluation; failure-injection scripts and runbook; leading the ADR for the synchronous risk gate.
- **Out of scope:** forwarding B's events to A (A subscribes to B directly, RD6); AI or machine learning; a human review queue; balance, size-limit and rate-limit checks (A); matching (B); news and Q&A (C); automatic account suspension; liquidation and overnight margin checks (stretch goal, not in the core, RD20).

## 2. Relationship with the other services

```
              ① HTTP: place order, cancel
   A ───────────────────────────────► D ══════ ② WS /d (commands + B's outputs) ══════ B
   ▲                                  │                                               │
   │  ④ HTTP: linked accounts (at D's start-up)                                       │
   └──────────────────────────────────┘                                               │
   ▲                                                                                  │
   └═════════════ ③ WS: B's event subscription (read-only, A connects to B directly) ═┘

   C: no business data with D; Prometheus scrapes D's /metrics
```

| Link | Method | Content |
| --- | --- | --- |
| ① A → D | HTTP + JSON | Place order, cancel |
| ② D ↔ B | WebSocket + JSON (B document sections 2 and 6) | D sends commands; B sends all its outputs to D, which uses them for features and to match replies |
| ③ B → A | WebSocket + JSON, read-only subscription | A gets `OrderEvent` / `Trade` directly from B for settlement; D is not involved |
| ④ D → A | HTTP + JSON | D loads linked accounts at start-up |

**B sends events to A directly (RD6).** In B's current design (B document D13), B talks only to D, and A's events have to be forwarded by D. We propose that B also offers a **read-only event subscription** that A connects to directly, using the same resume handshake as D. Reasons:

- "How fills reach A" is not a risk question, so D should not own it.
- If D forwards events, settlement stops for orders that have already traded whenever D is down. With a direct subscription, a D outage only affects new orders.
- D does not need to keep a second copy of B's outputs or implement resume a second time.
- It matches the original proposal ("A, C and D each consume B's events").
- B can already resend from its event store by seq (B document D21), so adding one more read-only subscriber is a small change.

**Commands still go only through D (RD7).** B has only one command entry, which is D, so A's orders and cancels both pass through D. Cancels are not checked against the rules, but D controls when they are sent (section 7).

## 3. Rules

Both rules are pure functions `decide(order, feature snapshot, rule version) → allow / reject (reason)` (RD1).

Notation: the **linked group** G(u) of user u is u plus every account linked to u; if u has no linked accounts, G(u) = {u}. For a given symbol, u's **opposite-side orders** are u's orders on the other side that are **resting in B's book**, plus **in-flight orders** that D has approved and B has not yet answered (RD4).

### R1 Self-trade (SELF_TRADE)
Reject a new order if it could trade against an opposite-side order of u **itself**.

- **Limit buy (price P):** reject if u has a resting sell at a price ≤ P. For a limit sell, the opposite: u has a resting buy at a price ≥ P.
- **Market order (quantity Q):** simulate filling Q on D's book view in price-time order; reject if the walk reaches one of u's own orders.

Limit orders are judged by "do the prices cross". This is conservative, but it is simple, easy to explain, and does not depend on the accuracy of other users' orders in the book view. Market orders have no price, so they must be simulated (RD3).

D's check is best-effort: there is always a gap between in-flight orders and B's actual state. The proposal says B's matcher is the atomic backstop, but **B's document does not include it**, so this needs to be confirmed with Bo (section 15).

### R2 Wash trading (WASH_TRADE)
The same check as R1, but "u itself" is replaced by "the accounts in G(u) other than u" (RD2). The linked group is treated as one trader for the self-trade check. A user with no linked accounts can never trigger R2.

Optional extension (not in the v1 rules): if accounts in a linked group trade with each other k times within a window W, raise the blocking level. Whether to enable it depends on the evaluation results.

### Evaluation order and reason codes
R1 first, then R2; stop at the first match.

| Reason code | Meaning |
| --- | --- |
| `SELF_TRADE` | R1 matched |
| `WASH_TRADE` | R2 matched |
| `RISK_TIMEOUT` | No decision within `GATE_DEADLINE` |
| `RISK_UNAVAILABLE` | D is not ready (section 8) |
| `CANCELLED_BEFORE_SUBMIT` | A cancel for this order ID arrived earlier (RD14) |

## 4. Internal structure

D is one Python process (FastAPI + uvicorn, **a single worker**) and runs as a single instance (RD19). It must be a single instance because B accepts only one D connection at a time and D keeps its state in memory.

```
A ──HTTP──► [API layer] ──► [decision queue] ──► [decision task] ──reads──► [features / book view]
               │                                      │                            ▲
               │ cancel                               │ decision record            │ B's outputs
               ▼                                      ▼                            │
        [cancel scheduler] ──────────────────► [writer thread] ──► SQLite d.db     │
               │                                      │ after commit               │
               │                                      ▼                            │
               └──────────────────────────────► [B connector] ══ WS /d ════════════╪══► B
                                                      ▲                            │
                                                      └────────────────────────────┘
```

- **API layer:** validates requests and checks idempotency; puts new orders into the decision queue and waits for the result; hands cancels to the cancel scheduler.
- **Decision task:** one asyncio task that processes the decision queue in order, so D's decisions have a total order (RD10). Rule checks read memory only and do no I/O.
- **Writer thread:** a dedicated thread, the only writer of SQLite. A decision is committed before it is sent (RD8).
- **Cancel scheduler:** decides when a cancel is sent to B, following the rules in section 7.
- **B connector:** keeps the single WebSocket connection to B, does the resume handshake, deduplicates by `seq` and checks gaps with `prev_seq`; uses B's outputs to update the features and to wake up order requests that are waiting for a reply.

## 5. State and storage

One SQLite file `/data/d.db` in WAL mode, on a named volume. D does **not** keep a copy of B's outputs (RD15). Sketch of the tables (not final):

| Table | Main fields | Purpose |
| --- | --- | --- |
| `decisions` | `decision_no` (D's own counter), `user_id`, `order_id`, order content, `decision`, `reason`, `rule_version`, `features_seq`, `decided_at`, `sent_at` | Idempotency, audit, replay (RD9) |
| `cancel_intents` | `user_id`, `order_id`, `received_at`, `sent_to_b_at` | Cancel intents and D's local tombstones (RD14) |
| `links` | `version`, `group_id`, `user_id` | Linked accounts loaded from A at start-up (RD17) |

In-memory state:

- **Book view:** per symbol, `order_seq → (user_id, side, price, remaining)`. Price and side come from D's own decision records (every order that reaches B passes through D); the remaining quantity comes from B's `OrderEvent` (RD5).
- **In-flight orders:** orders D has approved that have not yet received their first `OrderEvent` from B.
- **Order state inside D** (section 6), used by the cancel scheduler.
- **Linked groups:** `user_id → group_id`.

**Recovery after a restart:** D reads back its decisions and cancel intents from `d.db`, then sends `{"type":"resume","from_seq":0}` to B and re-applies the day's outputs from the first one to rebuild the book view. At course scale the day's output is small, so this is fast.

At the end of the day (after B sends the `END_OF_DAY` cancels), the book view and the in-flight orders become empty on their own; linked groups and past decisions are kept.

## 6. Order states inside D

```
                    ┌──► REJECTED_BY_D (final; guaranteed never sent to B)
RECEIVED ──decide────┤
                    └──► IN_FLIGHT ──B's first OrderEvent──► LIVE ──► FILLED / CANCELLED (final)
                              │                         └──► REJECTED_BY_B (final, incl. EXPIRED)
                              │ no reply within D_TIMEOUT
                              ▼
                          TIMED_OUT ──B's event arrives later──► LIVE / REJECTED_BY_B
                              │ cancel answered with ORDER_NOT_FOUND
                              ▼
                       NEVER_REACHED_B (final)

(cancel arrives before the order) CANCEL_TOMBSTONE ──order arrives──► REJECTED_BY_D (CANCELLED_BEFORE_SUBMIT)
```

These states are used only inside D to decide when to send cancels. A decides an order's fate from the records it receives on its own subscription to B (protocol document section 6).

## 7. Timeouts and order fate

The order-fate protocol in the proposal relies on B's "lookup by ID" and "tombstone", and B has neither. What B does have: a `NewOrder` carries `sent_at`, and when B records `ts`, it answers `REJECTED(EXPIRED)` if `ts − sent_at > MAX_COMMAND_AGE` (B document D15). D uses this to get the same effect as a tombstone (RD13):

1. When D sends a `NewOrder`, it records `sent_at` and waits for B's first `OrderEvent` for that order, for at most `D_TIMEOUT` (we require `D_TIMEOUT ≥ MAX_COMMAND_AGE`).
2. If the reply arrives, D returns B's result to A. If not, D returns `UNKNOWN` and the order becomes `TIMED_OUT`. **This is not a rejection; A must keep the hold.**
3. If A later cancels this order, D waits until `sent_at + MAX_COMMAND_AGE + CANCEL_MARGIN` has passed before sending the cancel to B.
4. B can then reply in only three ways, and A sees the reply on its own subscription:
   - `CANCELLED`: the order was on the book and is now cancelled.
   - `REJECTED(ORDER_ALREADY_FINAL)`: the order was already fully filled or cancelled; the fills are the record.
   - `REJECTED(ORDER_NOT_FOUND)`: **the order never reached B and never will.** The age limit has passed, so even if this `NewOrder` reaches B later, B will answer `EXPIRED`.
5. D and B run on the same host, so their clocks can be compared. B's D15 already depends on this. `CANCEL_MARGIN` is a safety margin.

For an order B has already confirmed (`LIVE`), the cancel is sent at once, without waiting.

D also guarantees: **an order that D answers with `REJECTED` is never sent to B** (RD12). So A can release the hold as soon as D rejects.

## 8. Readiness and failing closed

D forwards orders only when it is **ready**; otherwise it answers `RISK_UNAVAILABLE` (RD16). Ready means all of:

1. The linked accounts have been loaded from A.
2. The WebSocket to B is connected and the resume handshake is done.
3. D has caught up: the `prev_seq` chain of the outputs it received has no gaps up to the largest seq seen so far. (B does not tell D when the resend is finished, so this is the only way to judge. Whether B can add an end-of-resend marker is to be confirmed with Bo.)
4. The last SQLite write succeeded. If a write fails, D stops forwarding orders; it does not fall back to a weaker mode.

If a single order is not decided within `GATE_DEADLINE` (for example because the decision queue is too long), D answers `RISK_TIMEOUT`. This is D's own rejection: the order was not sent to B, so A can release the hold.

Cancels are not affected by readiness. While D is disconnected from B, cancels are queued and sent after reconnecting.

## 9. Determinism and replay

The decision for an order depends on "which of B's outputs the features had been updated to at decision time" and "which orders were in flight then". So every decision records `decision_no`, `features_seq` (the largest `(day, seq)` of B's outputs already applied at decision time) and `rule_version` (RD9).

How to replay: process decisions in `decision_no` order; before each decision, apply B's outputs from B's event store up to its `features_seq`; derive the in-flight orders from the earlier decisions and the outputs applied so far. The recomputed decisions must match the records exactly. This covers acceptance criterion 1. B's event store is the source of truth for orders and trades, so replay uses B's data and D does not need its own copy.

## 10. Rule versions

Rules and parameters are kept in a config file (for example `rules/v1.yaml`: which rules are enabled, parameters of the optional extension). `rule_version` = file name + the first few characters of the content hash, so changing any parameter gives a new version (RD18). D loads one version at start-up and does not switch while running.

## 11. Evaluation

- **Data:** B's bots produce labelled manipulation orders. The label format needs to be agreed with Bo; we suggest one record per order: `{order_id, user_id, label: LEGIT | SELF_TRADE | WASH_TRADE, scenario_id}`.
- **Metrics:** join the `decisions` table with the labels on `(user_id, order_id)` and compute offline:
  - precision and recall for each rule;
  - wrongly-blocked rate = rejected LEGIT orders / all LEGIT orders;
  - p99 decision latency, and the p99 of D's hop in the end-to-end placement latency.
- **Scenario:** the defining scenario from the proposal (bots sending N orders per second across two pairs). N and D's share of the latency budget will be set after the Week 6 baseline.

## 12. Failure injection and runbook

D's fault switches are enabled only in the test profile (RD21). The six scenarios from the proposal:

| Scenario | How it is triggered | Expected result |
| --- | --- | --- |
| D is slow | D switch: add X ms to every decision | Orders over `GATE_DEADLINE` get `RISK_TIMEOUT`; A starts shedding load based on D's p99 |
| D is down | `docker kill` D | A's placement call times out, the order becomes `UNKNOWN` and the hold is kept; **existing orders still settle** (A subscribes to B directly); after D restarts, A cancels and every order ends in a final state |
| Stale features | D switch: pause applying B's outputs | D sees it has not caught up and answers `RISK_UNAVAILABLE`; no decision is made on stale features |
| B accepted, reply lost | D switch: drop the connection to B right after sending a `NewOrder`, reconnect after X seconds | D returns `UNKNOWN` to A; A sees `ACCEPTED` and the fills on its own subscription and settles normally |
| B killed while an order is UNKNOWN | `docker kill` B after the order is `TIMED_OUT` | The cancel waits in D and the hold is kept; after B recovers, the cancel is sent and gets a clear result |
| One event dropped | Drop one record between B and A (a B switch or a network proxy, to be agreed with Bo and Pin-Yun) | A detects the gap from `prev_seq` and fetches again with resume |

Each runbook entry has three parts: how to detect it (which metrics and logs), how to respond, and how to confirm recovery.

## 13. Deployment, configuration and monitoring

| Aspect | Design |
| --- | --- |
| Image | `python:3.12-slim`, pinned dependencies; non-root user |
| Port | **8400**: A's HTTP interface, `/healthz`, `/metrics`. Open only inside the compose network |
| Storage | Named volume `/data`, holding `d.db` |
| Start-up order | `depends_on` B and A with `service_healthy`; `/healthz` reports unhealthy until D is ready |
| Restart | `restart: unless-stopped`; after a restart, recover as in section 5 |
| Network | D's subnet must be in B's `CTRL_ALLOWED_CIDRS` |

Configuration (environment variables): `D_PORT`, `B_CTRL_URL`, `A_BASE_URL`, `D_TIMEOUT_MS`, `MAX_COMMAND_AGE_MS` (must match B), `CANCEL_MARGIN_MS`, `GATE_DEADLINE_MS`, `RULES_FILE`, `DATA_DIR`, `FAULTS_ENABLED`.

Metrics: decision latency (histogram), rejections by reason, decision queue length, number of `TIMED_OUT` orders, number of pending cancels, connection and readiness state towards B, feature lag (time between the last applied output of B and now).

## 14. How the design meets the acceptance criteria

| Criterion | Mechanism |
| --- | --- |
| 1. Replaying the same order sequence gives identical decisions | Rules are pure functions; every decision records `features_seq` and `rule_version`; replay as in section 9 |
| 2. Report precision, recall and the wrongly-blocked rate | Offline evaluation in section 11 |
| 3. Each approved order reaches B at most once; each rejected order reaches A exactly once | Idempotency key `(user_id, order_id)`; decision written before sending; retries return the recorded result (RD8) |
| 4. With D forcibly stopped, orders do not hang | A keeps the hold; after D restarts, the delayed cancel gives every order a final state (section 7); settlement is not affected |
| 5. Gate p99 stays within D's latency share | Rules read memory only; decisions are serial but short; monitored by metrics; the share follows the Week 6 baseline |

## 15. Needed from other services

- **B (Bo):**
  - Offer a read-only event subscription to A (RD6) and update D13 in the B document; whether A can resume the previous day if it is down across the end of the day (B keeps past event stores).
  - Whether the matcher keeps the self-trade backstop (in the proposal, missing from B's document).
  - Agree to replace the tombstone with the age rule in section 7, and set `MAX_COMMAND_AGE` and `D_TIMEOUT` together.
  - When a cancel gets `ORDER_NOT_FOUND`, the `OrderEvent` must carry `user_id` and `order_id`, so A and D can match it to the order.
  - Whether B can send a marker when the resume resend is finished.
  - The bot label format.
  - In the core, D sends no orders with `origin = LIQUIDATION`.
- **A (Pin-Yun):**
  - Subscribe to B's events directly for settlement.
  - A generates `order_id` as a ULID (A document D4), so it never repeats for a user within a day (B's requirement).
  - Provide a linked-accounts endpoint, or agree that both sides read the same seed file.
  - Release holds by the rules in protocol document section 6: only on B's records and D's rejections, never on a timeout.
  - Shed load based on D's p99.
- **C (Rushabh):** Prometheus scrapes D's `/metrics` (8400), and the dashboard adds D's metrics.

## 16. Open items

1. **Timeout values:** `D_TIMEOUT`, `MAX_COMMAND_AGE`, `CANCEL_MARGIN`, `GATE_DEADLINE`, set after the Week 6 baseline.
2. **Trading day:** who defines it, the format of `day`, and what happens to orders that arrive during the end-of-day cancels (same as B's open item 1).
3. **Optional extension of R2:** whether to enable the window condition depends on the evaluation.
4. **Exact message fields:** waiting for the team to agree on common conventions.
