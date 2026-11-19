# Service D (Risk Control) Tech Decisions Record

Owner: Zongyan Yang · Status: draft, updated on 2026-11-06 after the team discussion and after reading A's and B's latest code (A's `feature/trading`, B's `resume_ok`).

This document records D's decisions, their reasons and their consequences. The IDs use RD (Risk Decision) to avoid confusion with D1, D2 in the A, B and C documents. The design is in [service_d_architecture.md](service_d_architecture.md) and the interfaces in [service_d_protocol.md](service_d_protocol.md).

## 1. Rules

### RD1. Only two rules, and the rules are deterministic pure functions

- **Decision:** v1 has only R1 (self-trade) and R2 (wash trading). A rule is `decide(order, feature snapshot, rule version)`: it reads no clock, uses no randomness and does no I/O.
- **Reason:** D is on the critical path of every order, and every extra rule adds latency and false positives. Determinism is needed for replay and audit. After the instructor's feedback, the proposal already cut the rules down to two.
- **Consequence:** more complex manipulation (for example quotes placed close to each other without crossing) is not caught. We report this honestly in the evaluation.

### RD2. Wash trading = the self-trade check applied to the linked group as one trader

- **Decision:** reject a new order if it could trade against an opposite order of **another account** in the user's linked group. History conditions such as "k trades within a window W" are only an optional extension, not part of v1.
- **Reason:** wash trading is the same real owner trading on both sides. The links are given explicitly in the seed data, so no guessing is needed. It shares the logic of R1, so it is simple to build and test.
- **Alternative (not chosen):** block only after past trades reach a threshold. Then the first wash trades always go through, and there are two more parameters to tune.
- **Consequence:** normal trades between linked accounts are also blocked. The linked accounts in the seed data exist for the wash-trading scenarios, so we expect little effect; the false positive rate will show it.

### RD3. Limit orders are judged by price crossing; market orders by a simulated fill

- **Decision:** for a limit buy at price P, reject if the user (or the linked group) has a sell at a price ≤ P; the mirror case for sells. A market order is simulated against the book view in price-time priority, and rejected if it reaches an order of the user (or the linked group).
- **Reason:** crossing prices is the direct definition of "could trade". It is simple, easy to explain, and does not depend on the exact quantities of other users' orders. A market order has no price, so it can only be simulated.
- **Alternative (not chosen):** simulate limit orders too, and reject only orders that would really reach the user's own order. Fewer false positives, but the result depends on every order in the book view being exact, and in-flight orders always make the view a little wrong.
- **Consequence:** limit orders are judged conservatively. Some orders that "would have filled against someone else first" are rejected, and these count as false positives.

### RD4. In-flight orders count as resting orders

- **Decision:** orders D has approved that B has not answered yet count as resting orders in R1 and R2.
- **Reason:** a manipulator can send two opposite orders in quick succession, so that the second reaches D before B has answered the first. Without counting in-flight orders, R1 and R2 could be bypassed this way.
- **Consequence:** if an in-flight order is later rejected by B, some orders in between may have been rejected without need. The window is short, so we accept this.

### RD5. The book view is built from D's own records and B's outputs

- **Decision:** every order that enters B passes through D, so D knows each order's price, side and user. B's `order_event` gives the `order_seq` and the remaining quantity, and `trade` gives the fills. From these D keeps a full book view.
- **Reason:** no need to ask B for its book; B's market data has only three levels, which is not enough. For the same reason D does not subscribe to B's market data on 9999.
- **Consequence:** depends on "every order passes through D".

## 2. Division of work with A and B

### RD6. D forwards B's results to A as notifications

- **Decision:** for every output from B, D translates it to A's order ID and sends it to A's `POST /internal/order-events` as a notification. Risk rejections, cancel results and modify results use the same notification channel. A does not connect to B directly.
- **Reason:**
  - B's implementation allows only one `/d` connection (`runtime.md`), and both A's and B's documents have D forwarding (A document D3 and D21, B document D13).
  - All results reach A from one source, D, so there is only one set of ordering and deduplication rules.
  - The ID translation (RD24) and modify (RD27) are inside D anyway; B's events must pass through D to be translated before A can recognise them.
- **Alternative (not chosen):** B offers A a read-only event subscription and A connects to B directly (the earlier draft). The benefit is that existing orders could still settle while D is down. But B would need one more interface, and B's events carry the integer IDs assigned by D, which A could not match to its own orders.
- **Consequence:**
  - **While D is down, settlement of existing orders also pauses**, and funds stay frozen until D is back. The proposal already accepts the cost "frozen funds in exchange for never trading against released funds". This goes into the ADR.
  - D must keep the notifications that are not yet acknowledged (the outbox, RD25).

### RD7. Commands go only through D: A's orders, cancels and modifies all pass through D

- **Decision:** D is B's only command entry. A's cancels go through D's `POST /v1/cancels` and modifies through `POST /v1/modifies`. Cancels are not checked against the rules.
- **Reason:** B accepts only one command connection. Passing cancels through D lets D control when they are sent (RD13) and keep tombstones for out-of-order requests from A (RD14).
- **Consequence:** while D is down, A can neither place nor cancel orders. Funds stay frozen, but nothing goes wrong. This goes into the ADR.

## 3. Idempotency, determinism and replay

### RD8. Write before sending; the idempotency key is (user_id, order_id)

- **Decision:** an order is stored as `RECEIVED` when it arrives; the decision (and the assigned `b_order_id`) is committed before sending to B; a notification is written to the outbox before sending to A. For a repeated `(user_id, order_id)`, D replies 202 with the current state when the content is the same, and 409 when it is different. Modifies use a separate endpoint.
- **Reason:** meets acceptance criterion 3: an approved order reaches B at most once, and a retry gets the same result. The same idea as B's "write before sending".
- **Consequence:** a few more SQLite commits per order. In WAL mode each usually takes well under a millisecond.
- **If D crashes:** the decision is committed, but it is not known whether `new_order` was sent. After a restart, such orders are treated as `TIMED_OUT`: if their outputs arrive while D catches up, they become `LIVE`; otherwise the automatic cancel of RD13 settles them. D never resends a `new_order` (B document D13: a command that timed out has failed and is not resent).

### RD9. Every decision records decision_no, features_seq and rule_version

- **Decision:** `decision_no` is D's own increasing decision number; `features_seq` is the largest `(day, seq)` of B's outputs applied at decision time; `rule_version` is described in RD18.
- **Reason:** a decision depends not only on the order itself, but also on the features and the in-flight orders at that moment. Recording these three lets a replay restore the state at decision time and get the same result (acceptance criterion 1).

### RD10. Decisions are processed in order by one asyncio task

- **Decision:** all new orders go into one queue, and one task decides them in order.
- **Reason:** decisions have a total order, and `decision_no` is that order, so replay is simple. The rules only read memory and each decision is short, so serial processing is not a bottleneck.
- **Consequence:** if one decision is unexpectedly slow, every order behind it waits. `GATE_DEADLINE` (reject with `RISK_TIMEOUT`) and the queue limit (503 `RISK_BUSY` when full) cover this, and the queue length is monitored.

## 4. Order fate

### RD11. The synchronous reply to an order only means "received"; orders D does not take get 503 and no record

- **Decision:**
  - D stores the order and replies 202 at once, meaning "received". The risk decision and B's results all arrive as notifications (RD6).
  - When D is not ready it replies 503 `RISK_UNAVAILABLE`, and when the decision queue is full 503 `RISK_BUSY`. On a 503 D keeps no record, so when A resends the same `order_id` later, D treats it as a new order.
- **Reason:**
  - This is the result of the discussion with Pin-Yun, and matches A document D3. A's order request returns quickly and is not held up by risk checks and matching.
  - An order D will certainly not take must be refused in the synchronous reply. Replying 202 first and rejecting later would make A hold funds for nothing and wait for one more notification.
  - Because a 503 leaves no record, it is safe for A to treat it as "no acknowledgement" under its current rules (resend, then cancel, and the cancel gets `TOMBSTONED`). **A needs no change.**
- **Alternative (not chosen):** D waits for B's first reply before answering (the earlier draft). A has one intermediate state less to handle, but the order request has to wait for the risk check and matching.
- **Consequence:**
  - A's load shedding can no longer be based on the latency of D's 202 (it is always short). It should be based on the time from placement to the risk result. Pin-Yun needs to know this.
  - The proposal calls D a "synchronous risk gate", and this still holds: no order reaches B without D's decision. Only D's reply to A has become asynchronous. The ADR should make this difference clear.

### RD12. An order D rejects is never sent to B

- **Decision:** any order D rejects (including `RISK_TIMEOUT` and `CANCELLED_BEFORE_SUBMIT`) is never sent to B, not even on a retry.
- **Reason:** then A can release the hold as soon as it receives `RISK_REJECTED`, without going through the order-fate flow.
- **Consequence:** `RISK_TIMEOUT` must be decided before sending; once an order is sent, it can only have been approved.

### RD13. D cancels timed-out orders automatically, and only after the age limit has passed

- **Decision:**
  - If D receives no output for a `new_order` within `D_TIMEOUT`, the order becomes `TIMED_OUT`, and D cancels it **automatically** after `sent_at + MAX_COMMAND_AGE + CANCEL_MARGIN`. If an output arrives before the cancel is sent, the automatic cancel is dropped.
  - A cancel from A for an `IN_FLIGHT` / `TIMED_OUT` order also waits until after the age limit; for an order that is already `LIVE` it is sent at once.
  - When B answers `ORDER_NOT_FOUND`, D decides itself: if it has received outputs for the order, the order is final; if not, the order never reached B and never will, and A gets `ORDER_UPDATE REJECTED NOT_DELIVERED`.
- **Reason:**
  - B has no tombstone, but it answers `EXPIRED` to a `new_order` that is too old (B document D15). After the age limit, the same `new_order` is rejected even if it arrives late, which has the same effect as a tombstone, and B does not need to change anything.
  - B's document requires D to cancel after a timeout (B document D15). With D cancelling automatically, A does not need to handle "the D to B hop timed out" at all, only "the A to D hop".
  - B's implementation has no `ORDER_ALREADY_FINAL`: both "not found" and "already final" are answered with `ORDER_NOT_FOUND`. D receives all of B's outputs for the day, so it can tell the two apart.
- **Assumptions:** D's and B's clocks can be compared (same host; B document D15 already relies on this); `D_TIMEOUT ≥ MAX_COMMAND_AGE`, and in B's implementation `MAX_COMMAND_AGE` is 1000 ms, fixed at build time.
- **Consequence:** a timed-out order gets its answer only after `MAX_COMMAND_AGE + CANCEL_MARGIN`, and the hold stays during that time. An order the user wanted is cancelled because of a timeout, and the reason `D_TIMEOUT` is shown to the user.

### RD14. D keeps local cancel tombstones

- **Decision:** when A cancels an order ID that D has never seen, D records a tombstone, replies `TOMBSTONED`, and notifies `CANCEL_RESULT TOMBSTONED`. A later order request with this ID is rejected with `CANCELLED_BEFORE_SUBMIT`.
- **Reason:** A → D is HTTP, so when A retries, a cancel can reach D before the order. Every order passes through D, so D's tombstone covers the A → D hop; the D → B hop is covered by RD13.

## 5. State and availability

### RD15. D keeps no copy of B's outputs, but keeps the notifications built from them; after a restart it rebuilds features from the start of B's day

- **Decision:** D's database stores orders, decisions, the ID map, cancel intents, the outbox and cursors, but not B's raw outputs. After a restart D sends `{"type":"resume","from_seq":0}` to B and rebuilds the book view from the first output of the day. A cursor records "the last B output that was already turned into notifications"; during the replay, outputs up to the cursor only update the features and build no notifications.
- **Reason:** B's event store is the source of truth for orders and trades (proposal), so a second copy in D is not needed. Every trading day starts with an empty book (B document D22), so only the current day is needed. Notifications must be kept, because they cannot be lost before A acknowledges them (RD25).
- **Consequence:** after a restart D is not ready until the rebuild is finished. At course scale the day's data is small, so we expect this to be fast; it will be measured in the load test.

### RD16. Readiness conditions and failing closed

- **Decision:** D takes new orders only when all of these hold: the shared config has been read; the connection to B is up and the handshake is done; D has caught up; the last write succeeded. Otherwise it replies 503 `RISK_UNAVAILABLE` at once.
- **Catching up:** B answers the resume with `resume_ok` carrying `through_seq`, the last output stored at that moment (added by Bo on 11-06). D has caught up once its `prev_seq` chain is contiguous up to `through_seq` in the day being resent.
- **Reason:** deciding on stale features means the rules do not work; the proposal requires D to reject when it cannot decide. Resend and live records interleave, so the order of arrival does not show when the resend is over; `through_seq` is a fixed target, so it does.
- **Fallback:** for a B without `resume_ok`, D uses the earlier rule: a contiguous chain and no new output for `CATCHUP_QUIET_MS` (300 ms by default).

### RD17. Linked accounts come from a shared seed file read by A, B and D

- **Decision:** the linked accounts are in a shared file `config/linked_accounts.json`, read by A, B and D. D reads it once at start-up and is not ready before that; it is not refreshed while running. `user_id` is A's integer user ID.
- **Reason:**
  - The links are static seed data and do not change while the system runs.
  - Reading a file is simpler than calling an A endpoint, and D does not have to wait for A to start.
  - B's wash-trading bots need the same list, and if all three read one file, the list is always the same.
- **Alternative (not chosen):** D calls A's `GET /internal/linked-groups` at start-up. A would need one more endpoint, D's start-up would depend on A, and the bots would need to get the list another way.
- **Consequence:** A agreed (11-06): A reads the same file and creates its seed users with these IDs (A document D16). B's bots still need to use it.

### RD18. Rule version = the rule config file

- **Decision:** rule switches and parameters are in a file such as `rules/v1.json`; `rule_version` = the file name + the first characters of the content hash. The version does not change while running.
- **Reason:** changing any parameter automatically gives a new version, so "same version, different rules" cannot happen.

## 6. Implementation and deployment

### RD19. Python + FastAPI; one process, one worker, one instance; SQLite in WAL mode with one connection

- **Reason:**
  - D's rules only do lookups in memory and very little computation; the bottleneck is the network and JSON, so Python is enough. A and C use the same stack, so we can help each other.
  - B accepts only one D connection at a time, and D's state is in memory, so D can only run as one instance; uvicorn runs one worker.
  - SQLite needs no separate database container. The process has only one connection, read and written synchronously inside the event loop, and each call takes well under a millisecond, so no separate writer thread is needed. It is the only writer, and a failed write is noticed at once.
- **Consequence:** D is a single point on the paths of both placement and settlement. `restart: unless-stopped` brings it back automatically. While D is down, orders can be neither placed nor cancelled, and settlement pauses (RD6). This is the "biggest risk" in the proposal, and it goes into the ADR.

### RD20. No liquidation or overnight margin checks in the core

- **Decision:** D sends no orders with `origin = LIQUIDATION`, does no overnight checks, and does not subscribe to B's market data on 9999. The `LIQUIDATION` value reserved in B's implementation is not used for now.
- **Reason:** after the instructor's feedback, the proposal made leverage and liquidation a stretch goal.

### RD21. Fault-injection switches exist only in the test profile

- **Decision:** `POST /admin/faults` (decision delay, pause applying B's outputs, disconnect from B after sending, skip one `notify_seq`) is registered only when `FAULTS_ENABLED=true`.
- **Reason:** some failure scenarios in the proposal need precise timing that `docker kill` cannot give, and the switches must not exist in the normal configuration. Since D now sends the notifications, "a dropped event" can also be simulated inside D, without help from A or B.

### RD22. Decision IDs use the RD prefix

- **Reason:** the decision IDs of A, B and C are D1, D2, …, which are easy to confuse with Service D.

### RD23. Services use only HTTP and WebSocket, and all data is JSON

- **Decision:** request-response (place, cancel, modify, query, notifications) uses HTTP + JSON; continuous push (B's outputs) uses WebSocket + JSON with a resume handshake; monitoring uses HTTP `/metrics`.
- **Reason:**
  - HTTP fits request-response best and is the simplest; it can be tested with curl, and FastAPI supports it natively.
  - With HTTP, continuous push means polling or posting each item; a WebSocket with a resume handshake solves this naturally, and B already works this way.
  - D → A notifications use HTTP because A document D21 already specifies it: one at a time, in order, and the next only after A acknowledges. An HTTP request and its reply express "acknowledged" directly.
- **Alternatives (not chosen):**
  - gRPC: lower latency and strong typing, but B already uses JSON everywhere; Python and Rust would both need generated code, which is harder to debug; and serialisation is not the bottleneck.
  - A message queue: notifications have only one consumer, A, so it is not needed, and it would add one more component that can fail on the settlement path.

## 7. Decisions added on 2026-11-05

### RD24. D assigns the order IDs sent to B and keeps an ID map

- **Decision:** A's `order_id` stays a ULID string and B keeps integers. D assigns a `b_order_id` to every order it sends to B from a global counter that is never reset, and writes it to the ID map in the same transaction as the decision. D translates B's outputs back to A's `order_id`: an `order_event` uses the `b_order_id`, and a `trade` uses `order_seq` (recorded from the `ACCEPTED` event). `user_id` is A's integer ID, passed to B unchanged.
- **Reason:**
  - In B's implementation `order_id` is a `u64`, while A uses ULID strings, so the types do not match.
  - For a modify, the new order needs a new B order ID (B does not allow the same user to reuse an `order_id` within a day, B document D2), and only D can generate it. Since D has to manage some of B's IDs anyway, assigning all of them in D is the cleanest.
  - Neither A nor B needs to change code.
- **Alternatives (not chosen):**
  - Everyone uses integers and D passes them through: the new IDs D generates for modifies could collide with IDs A generates later, unless we split the number range in some awkward way.
  - Everyone uses ULID strings: B would have to change its Rust code and tests, and D would still have to remember which order a modify replaced.
- **Consequence:** D has one more table, and logs need to show both IDs. The counter is never reset, so day boundaries do not matter.

### RD25. Notifications are sent from an outbox: contiguous numbers, one at a time, resent until acknowledged

- **Decision:** each notification gets a `notify_seq` when it is built (contiguous across the system and never reset across days), and is written to the outbox in the same transaction as "how far into B's outputs D has got". One sender task sends them to A one by one in order, and sends the next only after A answers 2xx. When A answers `409 {"expected_seq": N}`, D resends from N; on a failure D resends the same one with backoff. A can call `POST /v1/notifications/rewind` to ask D to resend from a given number.
- **Reason:**
  - A defines the delivery rules (A document 3.1, D21), and D implements them.
  - The outbox pattern commits "the state change" and "the notification to send" together, so after a crash D neither loses nor duplicates notifications.
  - Sending one at a time in order is what lets A keep orderings such as "the order event comes before the cancel result that follows it".
- **Consequence:** one at a time limits the notification throughput. It should be enough at course scale; the load test will confirm. While A is down, the outbox grows, so its backlog is monitored.

### RD26. A status query by order_id

- **Decision:** `GET /v1/orders/{order_id}?user_id=...` returns the state inside D, the decision, the cumulative filled quantity and the last `notify_seq` about the order; 404 if D does not know it. Read-only.
- **Reason:** A's reconciliation needs it (A document D5 and section 9): when an order got a 202 but no notification for a long time, A needs to know whether D is still working on it or a notification was lost. Agreed in the discussion with Pin-Yun.
- **Consequence:** a query result never releases funds; only notifications do.

### RD27. D turns a modify into "cancel + new order"

- **Decision:** A modifies through `POST /v1/modifies` under the same `order_id`; `qty` is the new remaining quantity. D cancels the current `b_order_id`; after B answers `CANCELLED`, the new quantity is `qty` minus what filled during the cancel, D checks the new order against the rules, assigns a new `b_order_id` and sends it; when B accepts it, D notifies `MODIFY_RESULT APPLIED`. One `order_id` maps to several `b_order_id`s, and the cumulative filled quantity adds them together. The details of each failure case are in protocol document 2.3; they follow A's answers (A document 5.4).
- **Reason:** the proposal says a modify is sent to D under the same order ID (line 55), and reaches B as a cancel plus a new order (line 84), so the translation can only happen in D. The earlier draft said D has no modify endpoint, which did not match the proposal.
- **Why the rules are checked after the cancel, not before:** the new quantity depends on what filled during the cancel, and the check must see the book without the old order.
- **Consequence:** between the cancel and the new order, the order is briefly off the book and loses its time priority; A accepts this, since B has no amend. If the new order fails the rules, the old one is already gone, so A gets `ORDER_UPDATE CANCELLED` before `MODIFY_RESULT REJECTED`.

### RD28. Trading pairs are named like BTC-USDT, with tick/lot in a shared config file

- **Decision:** trading pairs are written like `BTC-USDT`. Names, tick and lot are in a shared file `config/symbols.json`, read by A, B and D. D uses it to check the symbol and the tick/lot grid when an order arrives.
- **Reason:** A, C and D already use `BTC-USDT` in their documents. With one config, the tick/lot values are always the same in every service. B's implementation currently uses the environment variable `SYMBOLS`, and `runtime.md` already says it will be replaced by a config file.
- **Consequence:** names follow B (11-06), and tick and lot follow B's `pairs.json` too (11-19): BTC-USDT lot 100, ETH-USDT lot 1000, tick 1. D's file copies these values; D can also read B's `pairs.json` directly, since it reads only `symbol`, `tick` and `lot`.

### RD29. D does not handle trading hours

- **Decision:** D does not check trading hours and does not define the trading day. B rejects new orders outside trading hours and cancels all resting orders at 23:00 (both with reason `END_OF_DAY`); D handles these as ordinary rejections and cancels and turns them into notifications. On a new trading day `seq` restarts from 1, D deduplicates by `(day, seq)`, and when its last applied day is not today it resumes from 0.
- **Reason:** trading hours are B's decision; D does not need to, and should not, check them a second time.
- **Consequence:** B's trading hours are now 00:00–23:00 Dublin time (11-06). B keeps a daily close because its recovery replays only the current day. B moves to a new day only when a command of that day arrives, and commands only come from D, so D still gets the old day's last outputs after being down over midnight; only B also restarting after midnight would lose them, which we accept.

### RD30. Load shedding stays in A, but A should base it on the latency of the risk result

- **Decision:** D does no load shedding itself, and its rules are always on (A document D13). D only replies 503 `RISK_BUSY` when its decision queue is full. We suggest A base its load shedding on "the time from placement to the risk result" (A's `lame_a_order_completion_seconds`) instead of "the latency of D's 202".
- **Reason:** since D only replies "received", the 202 is always fast; however long the risk queue gets, A cannot see it, so the proposal's "shed load when D's p99 exceeds its budget" (line 58) would never trigger.
- **Consequence:** A needs to change one metric; to tell Pin-Yun.

## 8. Open items

1. **Timeout values:** `D_TIMEOUT` (≥ 1000 ms), `CANCEL_MARGIN`, `GATE_DEADLINE`, `DECISION_QUEUE_CAP`, set after the baseline.
2. **Optional extension of R2:** whether to enable the window condition depends on the evaluation.
3. **One shared symbol file** once B reads pairs from a file (RD28).

## 9. Items to align with teammates

### With Pin-Yun (A)
1. Base load shedding on the latency of the risk result (RD30).
2. D keeps no record on a 503, so A needs no change; later A may treat a 503 as "certainly not received" and release the hold at once (RD11).
3. Whether `POST /v1/notifications/rewind` meets the need of reconciliation (RD25).

### With Bo (B)
1. When the bots (including the labelled manipulators) will be written. Label format: one record per order, `{order_id, user_id, label: LEGIT | SELF_TRADE | WASH_TRADE, scenario_id}`, with `order_id` as A's ULID. The bots place orders through A. Bo designs the scenarios, and they can include cases D will not catch.
2. Done: pair names, tick and lot follow B's `pairs.json` (RD28).
3. The wash-trading bots use the accounts in the shared linked-accounts file (RD17).
4. For information: D assigns the integer order IDs sent to B (RD24); D cancels automatically after a timeout (RD13).

### With Rushabh (C)
1. Prometheus scrapes D's `/metrics` (port 8400), and the dashboard adds D's metrics.

### Everyone, later
1. Who owns the root `docker-compose.yml` (A already has `service_a/docker-compose.yml`).
2. The ADR: D only replies "received"; while D is down, both placement and settlement pause and funds stay frozen.
