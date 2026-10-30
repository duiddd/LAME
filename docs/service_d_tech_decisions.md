# Service D (Risk Control) Tech Decisions Record

Owner: Zongyan Yang · Status: draft, for team discussion

This document records D's decisions, the reasons for them and their consequences. IDs use the prefix RD (Risk Decision) to avoid confusion with the D1, D2 IDs in the B and C documents. The design is in [service_d_architecture.md](service_d_architecture.md), and the interfaces are in [service_d_protocol.md](service_d_protocol.md).

## 1. Rules

### RD1. Only two rules, and the rules are deterministic pure functions

- **Decision:** v1 has only R1 (self-trade) and R2 (wash trading). A rule is `decide(order, feature snapshot, rule version)`; it reads no clock, uses no randomness and does no I/O.
- **Reason:** D is on the critical path of every order, so each extra rule adds latency and false positives. Determinism is required for replay and audit. After instructor feedback, the proposal already cut the rule set to two.
- **Consequence:** more complex manipulation (for example quotes close to the other side that do not cross) is not caught. We report this honestly in the evaluation.

### RD2. Wash trading = the self-trade check applied to the linked group as one trader

- **Decision:** reject a new order if it could trade against an opposite-side order of **another account** in the same linked group. History conditions such as "k trades within window W" are only an optional extension, not part of v1.
- **Reason:** wash trading is, by definition, the same controlling person trading on both sides. The links are given explicitly in the seed data, so no guessing is needed. It shares the logic of R1, which keeps implementation and testing simple.
- **Alternative (not chosen):** block only after past trades reach a threshold. Then the first wash trades always go through, and there are two more parameters to tune.
- **Consequence:** normal trades between linked accounts are also blocked. The linked accounts in the seed data exist for the wash-trading scenarios, so we expect little impact; the wrongly-blocked rate will check this.

### RD3. Limit orders are judged by price crossing; market orders by a simulated fill

- **Decision:** for a limit buy at price P, reject if the user (or their linked group) has a sell at a price ≤ P; the opposite for a sell. For a market order, simulate filling it on the book view in price-time order, and reject if the walk reaches an order of the user (or their linked group).
- **Reason:** crossing prices is the direct meaning of "could trade". It is simple, easy to explain, and does not depend on the accuracy of other users' quantities. Market orders have no price, so they can only be simulated.
- **Alternative (not chosen):** simulate limit orders too, and reject only those that would really reach the user's own order. This gives fewer false positives, but the result depends on the accuracy of every order in the book view, and in-flight orders always make the view slightly wrong.
- **Consequence:** limit orders are judged conservatively. Some orders are rejected even though another user's order would have filled first; these count towards the wrongly-blocked rate.

### RD4. In-flight orders count as resting orders

- **Decision:** orders that D has approved and B has not yet answered are treated as resting orders in R1 and R2.
- **Reason:** a manipulator can send two opposite orders quickly, so that when the second reaches D, B has not yet confirmed the first. Without counting in-flight orders, R1 and R2 could be bypassed this way.
- **Consequence:** if an in-flight order is later rejected by B, some orders in between may have been rejected unnecessarily. The window is very short, so this is acceptable.

### RD5. The book view is built from D's own decision records and B's outputs

- **Decision:** every order that reaches B passes through D, so D knows each order's price, side and user. B's `OrderEvent` gives the `order_seq` and the remaining quantity, and `Trade` gives the fills. From these D keeps a complete book view.
- **Reason:** no need to ask B for its book; B's market data only has three levels, which is not enough.
- **Consequence:** this depends on every order passing through D.

## 2. Division of work with A and B

### RD6. B sends its events to A directly; D does not forward them

- **Decision:** B offers a read-only event subscription that A connects to directly, with the same resume handshake as D. D receives B's outputs only on its own connection to B, uses them for features and to match replies, and forwards them to no one.
- **Reason:**
  - "How fills reach A" is not a risk question.
  - If D forwards events, settlement stops for orders that have already traded whenever D is down. With a direct subscription, a D outage only affects new orders.
  - D does not need to keep a second copy of B's outputs or implement resume a second time.
  - It matches the original proposal ("A, C and D each consume B's events").
  - B can already resend from its event store by seq (B document D21), so one more read-only subscriber is a small change.
- **Alternatives (not chosen):**
  - D forwards B's events to A (the current B document D13): D's responsibilities get mixed, and D becomes a single point of failure for settlement.
  - B publishes events to a message queue: one more piece of infrastructure on the settlement path; B's event store and the queue become two "sources of truth", and B has more to change.
- **Consequence:** Bo needs to change D13 in the B document and add a read-only subscription endpoint.

### RD7. Commands still go only through D: A's orders and cancels both pass through D

- **Decision:** D is B's only command entry. A's cancels are also passed on through D's `POST /v1/cancels`, without rule checks.
- **Reason:** B accepts only one command connection. Passing cancels through D lets D control when they are sent (RD13) and keep tombstones for out-of-order requests from A (RD14).
- **Consequence:** while D is down, A can neither place nor cancel orders. Holds stay frozen, but nothing goes wrong. This goes into the ADR.

## 3. Idempotency, determinism and replay

### RD8. Write the decision before sending; the idempotency key is (user_id, order_id)

- **Decision:** every decision (approve or reject) is committed to SQLite before it is returned to A or sent to B. For a repeated `(user_id, order_id)`, the same content returns the recorded result, and different content returns 409.
- **Reason:** this meets acceptance criterion 3: an approved order reaches B at most once, and a retry gets the same decision. It follows the same idea as B's "write before send".
- **Consequence:** one extra SQLite commit per order. In WAL mode this is usually below a millisecond, and the writer thread can batch commits.
- **If D crashes:** the decision is committed, but it is unknown whether the `NewOrder` was sent. After a restart such orders are treated as `TIMED_OUT`: if B resends events for them during resume, they become `LIVE`; otherwise they wait for A's cancel and are resolved by RD13. D does not resend the `NewOrder` (B document D13: a timed-out command has failed and is not resent).

### RD9. Every decision records decision_no, features_seq and rule_version

- **Decision:** `decision_no` is D's own increasing decision number; `features_seq` is the largest `(day, seq)` of B's outputs already applied at decision time; `rule_version` is described in RD18.
- **Reason:** a decision depends not only on the order but also on the features and in-flight orders at that moment. With these three values, replay can restore the state at decision time and get the same result (acceptance criterion 1).

### RD10. Decisions are processed in order by one asyncio task

- **Decision:** all new orders go into one queue, and one task decides them in order.
- **Reason:** decisions get a total order, and `decision_no` is that order, which keeps replay simple. Rule checks read memory only and each takes very little time, so serial processing is not a bottleneck.
- **Consequence:** if one decision is unexpectedly slow, every order behind it waits. `GATE_DEADLINE` is the safety net (returning `RISK_TIMEOUT`), and the queue length is monitored.

## 4. Order fate

### RD11. The placement call waits for B's first reply; on timeout it returns UNKNOWN

- **Decision:** after approving, D waits for B's first `OrderEvent` for the order, for at most `D_TIMEOUT`. If none arrives, it returns `b_result = UNKNOWN`.
- **Reason:** with one call, A knows whether the order entered B. This matches the proposal's design of three synchronous hops (A → D → B) with an end-to-end p99 budget.
- **Alternative (not chosen):** D returns right after deciding, and all of B's results come from A's subscription. D's endpoint would be faster, but A would have to match its order request to the events it receives before it could tell the user whether the order was placed.
- **Consequence:** `UNKNOWN` is not a rejection, and A must keep the hold. The same result reaches A by two paths (D's response and A's subscription), and A deduplicates by `(day, seq)`.

### RD12. An order D rejects is never sent to B

- **Decision:** no order with `decision = REJECTED` (including `RISK_TIMEOUT` and `RISK_UNAVAILABLE`) is ever sent to B, not even on a later retry.
- **Reason:** A can then release the hold as soon as D rejects, without going through the order-fate process.
- **Consequence:** `RISK_TIMEOUT` must be decided before sending; once an order is sent, it can only be `APPROVED`.

### RD13. A cancel for an unconfirmed order waits until the age limit has passed; ORDER_NOT_FOUND means "never reached B"

- **Decision:** for an order in `IN_FLIGHT` / `TIMED_OUT`, D sends `CancelOrder` to B only after `sent_at + MAX_COMMAND_AGE + CANCEL_MARGIN`. If B then answers `ORDER_NOT_FOUND`, the order never reached B and will never be accepted. For a confirmed (`LIVE`) order, the cancel is sent at once.
- **Reason:** the proposal relies on B's tombstone, but B has no tombstone. What B has is answering `EXPIRED` to an old `NewOrder` (B document D15). After the age limit, the same `NewOrder` is rejected even if it arrives late, which has the same effect as a tombstone, and B does not need any change.
- **Assumptions:** D's and B's clocks can be compared (same host; B document D15 already depends on this); `D_TIMEOUT ≥ MAX_COMMAND_AGE`.
- **Consequence:** a cancel for a timed-out order is delayed by at most `MAX_COMMAND_AGE + CANCEL_MARGIN`, and the hold stays frozen during that time.

### RD14. D keeps local cancel tombstones

- **Decision:** when A cancels an order ID that D has never seen, D records a tombstone and returns `TOMBSTONED`; a later order request with that ID is rejected with `CANCELLED_BEFORE_SUBMIT`.
- **Reason:** A → D is HTTP, and when A retries, a cancel may reach D before the order. Every order passes through D, so D's tombstone covers the A → D hop; RD13 covers the D → B hop.

## 5. State and availability

### RD15. D keeps no copy of B's outputs; after a restart it rebuilds features from the start of B's day

- **Decision:** D's database stores only decisions, cancel intents and linked accounts. After a restart, D sends `{"type":"resume","from_seq":0}` to B and rebuilds the book view from the first output of the day.
- **Reason:** B's event store is the source of truth for orders and trades (proposal), so a second copy in D is not needed. Since D no longer forwards events to A (RD6), it does not need to serve resume for anyone else either. Every trading day starts with an empty book (B document D22), so only the current day is needed.
- **Consequence:** after a restart, D is not ready until the rebuild finishes. At course scale the day's data is small, so we expect this to be fast; we will measure it in the Week 6 load test.

### RD16. Readiness conditions and failing closed

- **Decision:** D forwards orders only when all of these hold: linked accounts are loaded; D is connected to B and the handshake is done; D has caught up (the `prev_seq` chain has no gaps up to the largest seq seen); the last write succeeded. Otherwise it answers `RISK_UNAVAILABLE`.
- **Reason:** deciding on stale features means the rules do not work. The proposal requires D to reject when it cannot decide.
- **Consequence:** B does not send an "end of resend" marker, so D can only judge catching up by "the chain has no gaps". If B can add this marker, the check becomes clearer (to confirm with Bo).

### RD17. Linked accounts are loaded from A once at start-up; D is not ready before that

- **Decision:** at start-up D calls A's `GET /internal/linked-groups` and becomes ready only after it succeeds. It does not refresh while running.
- **Reason:** links are static seed data and there is no need for them to change at runtime, so loading at start-up is the simplest. Forwarding orders without the links would switch R2 off, which breaks failing closed.
- **Alternative:** A and D read the same seed file. One less endpoint, but two services sharing a file blurs the boundary between them. To confirm with Pin-Yun.

### RD18. Rule version = the rule config file

- **Decision:** rule switches and parameters are kept in a file such as `rules/v1.yaml`; `rule_version` = file name + the first few characters of the content hash. The version does not change while running.
- **Reason:** changing any parameter automatically gives a new version, so "same version, different rules" cannot happen.

## 6. Implementation and deployment

### RD19. Python + FastAPI; one process, one worker, one instance; SQLite in WAL mode with one writer thread

- **Reason:**
  - D's rules are in-memory lookups with very little computation; the cost is in networking and JSON, so Python is enough. It is the same stack as C, so team members can help each other.
  - B accepts only one D connection at a time and D keeps its state in memory, so there can only be one instance, and uvicorn runs a single worker.
  - SQLite needs no separate database container. One writer thread follows B's approach, and D knows immediately when a write fails.
- **Consequence:** D is a single point of failure on the order path. `restart: unless-stopped` brings it back automatically. While D is down, orders can be neither placed nor cancelled, but settlement continues (RD6). This is the "riskiest decision" in the proposal and goes into the ADR.

### RD20. No liquidation or overnight margin checks in the core

- **Decision:** D sends no orders with `origin = LIQUIDATION` and runs no overnight checks. The interface reserved in B's document is not used for now.
- **Reason:** after instructor feedback, the proposal moved leverage and liquidation to a Week 9 stretch goal.

### RD21. Fault-injection switches exist only in the test profile

- **Decision:** `POST /admin/faults` (decision delay, pausing the application of B's outputs, dropping the connection to B after sending) is registered only when `FAULTS_ENABLED=true`. "One event dropped" happens between B and A, so it is done with a B switch or a network proxy, to be agreed with Bo and Pin-Yun.
- **Reason:** several failure scenarios in the proposal need precise timing that `docker kill` cannot give, and the switches must not exist in the normal configuration.

### RD22. Decision IDs use the RD prefix

- **Reason:** decision IDs in the B and C documents are D1, D2, …, which are easy to confuse with Service D. We suggest the team uses a common format such as `B-D1` and `C-D1`.

### RD23. Services use only HTTP and WebSocket, and all data is JSON

- **Decision:** request-response (placing orders, cancels, loading linked accounts) uses HTTP + JSON; continuous push (B's outputs, the event subscription) uses WebSocket + JSON, with a resume handshake where nothing may be lost; monitoring uses HTTP `/metrics`.
- **Reason:**
  - HTTP fits request-response best. It is the simplest, can be tested with curl, and FastAPI supports it directly.
  - With HTTP, continuous push means either polling or one POST per event. Polling wastes resources and adds latency; one POST per event means handling retries, reordering and resends by hand. WebSocket with a resume handshake solves these naturally, and B already works this way.
  - With only two transports, JSON everywhere and integer prices, client code can be reused and testing stays simple.
- **Alternatives (not chosen):**
  - gRPC: lower latency and strong typing, but B already uses JSON everywhere; Python and Rust both need generated code, which is harder to debug; and serialization is not the bottleneck.
  - A message queue: with only two consumers (A and D) it is not needed, and it would add one more component that can fail on the settlement path.

## 7. Open items

1. **Timeout values:** `D_TIMEOUT`, `MAX_COMMAND_AGE`, `CANCEL_MARGIN`, `GATE_DEADLINE`, after the Week 6 baseline.
2. **Source of linked accounts:** endpoint or shared seed file (RD17).
3. **Optional extension of R2:** whether to enable the window condition, based on the evaluation.
4. **Trading day:** its definition, the format of `day`, and what happens to orders that arrive during the end-of-day cancels.
5. **Exact message fields:** waiting for the team to agree on common conventions.

## 8. Items to align with teammates

### For Bo (B)
1. Provide the read-only B → A event subscription and update D13 in the B document (RD6); port, path, number of subscribers; whether A can resume the previous day if it is down across the end of the day.
2. Replace the tombstone with the age rule in RD13, and set `MAX_COMMAND_AGE` and `D_TIMEOUT` together.
3. When a cancel gets `ORDER_NOT_FOUND`, the `OrderEvent` carries `user_id` and `order_id`.
4. The clash between the message type and the order type, both called `type`, in `NewOrder`.
5. Whether B can send a marker when the resume resend is finished (RD16).
6. Whether the matcher keeps the self-trade backstop from the proposal (missing from B's document).
7. The bot label format (architecture document section 11).

### For Pin-Yun (A)
1. Subscribe to B's events directly for settlement (RD6).
2. A generates `order_id` as a ULID (A document D4).
3. Linked-accounts endpoint or shared file (RD17).
4. Release holds by the rules in protocol document section 6, never on a timeout.
5. Shed load based on D's p99.

### For Rushabh (C)
1. Prometheus scrapes D's `/metrics` (port 8400), and the dashboard adds D's metrics.
