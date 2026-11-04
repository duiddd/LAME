# COMP41720 Week 5 Proposal

Oct 9, 2026 · Revised Oct 23, 2026 after instructor feedback

## Option and system

**Group:** \[Pin-Yun Kuo\], \[Bo Wu\], \[Rushabh Mutha\], \[Zongyan Yang\]

**Option A**: a traditional distributed system with an AI component. We will build a simulated exchange with a built-in market terminal. Users trade on a matching engine, and the terminal shows live prices, news, and an AI analyst that explains market moves. The AI component is a generative analyst (RAG) that answers questions about the market. Risk control on the trading path is deliberately rule-based and deterministic, so the matching engine can replay exactly.

**Scope.** Two trading pairs; spot trading only, with limit, market and cancel orders settled against balance holds; synthetic users and a scripted news file that is replayed into the system. Simulated trading bots generate load, and some are scripted market manipulators (self-trading and wash trading), which gives us labelled data for evaluating the risk rules. Everything runs offline via `docker compose up`, using Ollama or a simulated model. Out of scope: real authentication, deposits and withdrawals, leverage and margin, and stop orders. Stretch goal (Week 9, only if the core is solid): leverage with forced liquidation.

## Service sketch

Four services, one owner each. Each service answers exactly one business question and owns its own database.

| Service | Owner | The one question it answers | Key patterns |
| --- | --- | --- | --- |
| A. Trading & Accounts | \[Pin-Yun Kuo\] | Are balances and assets correct? | Trading API and gateway; registration, login and simulated deposits; idempotent order IDs; strongly consistent spot holds; never release a hold on a timeout or an acknowledgement; idempotent settlement from D's notifications with sequence-number deduplication and gap recovery; load shedding; candles from trades; user-private notifications; trading page of the frontend |
| B. Matching & Market Data | \[Bo Wu\] | Who trades with whom, at what price? | Deterministic price-time matching, one sequential matcher per symbol; durable log as source of truth, appended before publishing; crash recovery by replay; order lookup and cancel-by-ID; sequenced market data feed; trading bots and load tests |
| C. News & AI Analyst | \[Rushabh Mutha\] | What is happening in the market, and why? | Scripted news replay; versioned searchable index; RAG answers with verified citations, freshness labels and explicit "insufficient evidence"; prompt-injection screening; dashboard with model latency, token use and refusal rate |
| D. Risk Control | \[Zongyan Yang\] | Should this order be allowed? | Rule-based pre-trade gate that fails closed; two rules (self-trade prevention, wash trading); trader features built from the trade stream; rule-versioned decisions |

\[Architecture diagram: four services and their interactions, to be added\]

**Communication.** A hands every order to D first. If it passes, D forwards it to B; if not, D rejects it straight back to A. B appends each order event and trade to its log, then publishes it. A (settlement), C (prices for answers) and D (trader features) each consume these asynchronously, and D also reads linked-account data from A. B is the only source of market data. C also consumes the replayed news feed.

**Order lifecycle and the ambiguous timeout.** A call from A to D to B can time out after B has already accepted the order. A must not release a hold until it knows the order's fate.

1. A creates the order ID (the idempotency key) at the gateway, stores the order as `PENDING` and places the hold. D and B handle a repeated ID idempotently.
2. If A's call times out, the order becomes `UNKNOWN` and **A keeps the hold**.
3. A resolves the order by asking B for its state by ID, or by waiting for B's sequenced event for that ID. If B has the order, A follows B's events and settles or releases from them.
4. If B has no record, the order may still be in flight. A sends B a cancel-by-ID, which B records as a tombstone, so that a late arrival of that ID is rejected. Only after B acknowledges does A release the hold.
5. A user cancel on an `UNKNOWN` order is the same message: A records the intent and retries the cancel-by-ID until B acknowledges. If B is down or still replaying, the order stays `UNKNOWN` and the funds stay held. We accept frozen funds during a B outage in return for never trading against released funds.
6. We test this by dropping the D to B response after B has accepted the order, and by killing B while an order is `UNKNOWN`.

**Source of truth.** B's log is the source of truth for orders and trades. B appends to the log durably before it publishes, so anything published can be recovered by replay. After a replay, A deduplicates on the sequence number. A tracks the last sequence number it has applied per symbol; when it sees a gap, it asks B to replay from the missing number and catches up. We demonstrate this by dropping one event and showing A detect the gap and recover.

**Latency budget and threading model.** Order placement crosses A, D and B synchronously, so p99 compounds across three hops. The defining scenario is: bots drive N orders per second across the two pairs, with an end-to-end p99 target of T ms for placement. T is split into a share for each of the three hops, and we test each share separately and end to end. N, T and the shares are not fixed yet. Bo measures a baseline in Week 6 without the risk rules, and we then record the numbers here. B uses one sequential matcher per symbol; with two pairs this is two independent matchers, and there is no ordering across pairs.

**Consistency.** Strong consistency only for balances and holds in A. Order sequencing is total per symbol, and B's log is the source of truth. Everything else is eventually consistent, with lag measured and shown on the dashboard. Every consumer is idempotent, because delivery is at-least-once and the engine may replay. D is on the critical path and fails closed: an order D cannot decide on in time is not allowed through. A timeout on A's side, however, is not a rejection until B has confirmed the order's fate.

**Shared work.** A: seed data including linked accounts. B: trading bots and load tests. C: compose setup, README, and the Prometheus and Grafana dashboard (rate, errors, per-hop latency, model latency, token use, refusal rate). D: failure-injection scripts and runbook.

## Service A: Trading & Accounts

**Owner:** \[Pin-Yun Kuo\] · **The one question it answers:** Are balances and assets correct?

**Role.** The user-facing entry point of the exchange, and the single authority for users, account balances and order state. Its users are discretionary traders placing orders by hand. Bots only act as load sources; A provides no features specific to programmatic trading.

**Responsibilities**

1. **Users and accounts.** Users register and log in with a username and password; no email or other personal data is collected, and there is no identity verification. Users are isolated from each other. Each user has available balance, held balance and holdings, and every change leaves a ledger entry. New users start with zero balances and add funds by simulated deposits in the UI; there are no withdrawals. Seed data adds demo users, including some linked accounts that the wash-trading scenarios use.
2. **Fund management.** Funds are held synchronously when an order is placed, and settled or released correctly on fill, cancel, modify or reject. Settlement is idempotent. This is spot trading only: there is no leverage or margin. A provides D with the linked-account data it needs. A remains the authority for balances.
3. **Order functions.** Limit orders, market orders, cancel, and modify (sent to D under the same order ID, with the new price or remaining quantity). A creates the order ID at the gateway, and it serves as the idempotency key, so retries never create duplicate orders.
4. **Order state and order fate.** A is the only maintainer of order state. All commands go to D, and D's reply only acknowledges receipt (`SENT`). D then notifies A of every outcome: its risk decision, B's acceptance with B's order sequence number (`PROCESSING`), fills, cancels, rejections and expiry. A updates orders and accounts only from these notifications, never from a timeout or an acknowledgement. If D does not acknowledge, the order becomes `UNKNOWN`, its hold is kept, and A resends the same order ID and then cancels it. If an acknowledged order gets no notification, A queries D with backoff and keeps the hold until the outcome arrives. Users can query open orders, order history, fills and holdings.
5. **Preliminary risk control.** Before handing an order to the risk service (D), A runs cheap checks that need only the single request and the user's own account: sufficient balance, per-order size limit, price protection and rate limiting. Orders that pass go to D. On a rejection, A releases the hold and reports the reason to the user.
6. **Load shedding.** If D's recent p99 latency exceeds its share of the budget, A rejects new orders with a clear reason instead of weakening D's rules. The threshold is set from the Week 6 baseline.
7. **Settlement from D's notifications.** D sends its notifications in order with a contiguous sequence number and resends each one until A confirms it. A deduplicates on that number and on B's `(day, seq)` for trades; when a notification arrives ahead of a gap, A tells D the number it expects and D resends from there.
8. **API gateway.** The entry point for all trading and account operations. It handles authentication, validation, rate limiting and idempotency, and pushes each user's own order and balance updates to the frontend. Market data does not pass through A; the frontend obtains the order book directly from B.
9. **Market views.** A computes candles (5 s, 15 s, 1 min and 5 min) from the trades it settles, and the change since the day's opening price for the market list.
10. **Frontend.** One frontend shared with C. A owns the application shell and the trading page: order book, candlestick chart, order form, market list, and tabs for open orders, order history, trade history and holdings. The trading page reserves a slot for C's chat box and shows C's news for the selected pair; C owns the News & Analyst page.

**Consistency.** Registration, deposits, balances, holds and order acceptance are strongly consistent. Settlement and order-state updates are eventually consistent, with measurable lag. A failure of D must not take A down; waiting on D is always bounded. While D is down, new orders and settlement both pause, and funds stay held until D's notifications arrive.

**Out of scope.** Identity verification, password recovery, real personal data, withdrawals, leverage and margin, stop orders, programmatic-trading features, matching, and behavioral risk rules (D).

**Acceptance criteria**

1. After concurrent trading by many users, balances, holds and ledger entries are consistent.
2. Duplicate requests or duplicate notifications never take effect twice.
3. When D or B times out, or is killed, no hold is released before D's notification gives the order's fate, and no fill ever uses released funds. Each order ends as filled, cancelled or rejected.
4. When a notification arrives ahead of a gap, A detects it from the sequence numbers and recovers.
5. A user can complete the full flow in the UI: register, log in, deposit, view the market, place, modify and cancel orders, and see the account change.

## Service B: Matching & Market Data

**Owner:** \[Bo Wu\] · **The one question it answers:** Who trades with whom, at what price?

**Role.** The matching core of the exchange and the single source of market data. B cares only about orders and trades, not accounts or funds. Every order it receives comes from D, as a user order that A has validated and D has approved.

**Responsibilities**

1. **Matching.** Price-time priority matching per trading pair. Supports limit orders, market orders and cancels. A modify arrives as a cancel plus a new order, so B needs no separate handling. A market order that cannot be fully filled for lack of liquidity has its unfilled remainder cancelled; it never rests on the book. As a backstop for D's self-trade rule, the matcher never matches an order against the same user's resting order; it cancels the incoming order instead.
2. **Sequencing and threading.** All orders within one trading pair have a single, deterministic order, processed by one sequential matcher per symbol. With two pairs there are two independent matchers. No ordering is required across pairs.
3. **Log as source of truth.** B appends every order, cancel and trade to a durable log before it publishes. The log is the source of truth for orders and trades.
4. **Order events and trades.** The outcome of every order is published: accepted, partially filled, filled, cancelled, rejected. Every event carries a per-symbol sequence number, and every trade has a unique identifier. A uses these events to update orders and accounts; C uses the trade stream for prices, and D uses it with the order events for trader features.
5. **Order lookup and cancel-by-ID.** B answers a lookup of an order's state by ID. A cancel-by-ID cancels the order if B has it. If B has never seen the ID, B records a tombstone in its log, and a later arrival of that ID is rejected.
6. **Determinism and recovery.** The same order sequence always produces the same result. After a crash or restart, the engine rebuilds its state by replaying its log, without losing orders or duplicating trades. Recovery and replay make no calls to other services. A consumer can ask B to replay from a given sequence number.
7. **Market data.** B is the only publisher of market data: order book depth, latest trades, last price and candles. All other components obtain market data through B. Every update carries a sequence number, so clients can detect gaps and resynchronize. A slow client must not hold back the others.
8. **Trading bots and load testing.** Background bots generate traffic. Scripted manipulators perform self-trading and wash trading, with ground-truth labels for evaluating D's rules. B also owns system-wide load testing: it measures the Week 6 baseline and runs the defining latency scenario.

**Consistency.** Orders are totally ordered within a pair and matching is deterministic. B appends to its log before publishing. Order events and trades are delivered at least once, and consumers deduplicate on sequence numbers and unique identifiers. Market data may lag but must never be reordered or silently lost.

**Out of scope.** Account and fund checks (A), risk control (D), news and Q&A (C), authentication, other order types such as stop orders, and auctions.

**Acceptance criteria**

1. Replaying the same order sequence twice produces identical trades.
2. After a forced engine kill and restart, the order book matches its pre-kill state, with no lost or duplicated trades.
3. A cancel-by-ID for an unseen order ID leaves a tombstone, and a later arrival of that ID is rejected, including after a restart.
4. A slow or disconnected client does not affect matching or other clients' market data.
5. Under bot load, the system produces labelled manipulation data that D can be evaluated against.
6. The latency baseline and the defining scenario are measured and reported.

## Service C: News & AI Analyst

**Owner:** \[Rushabh Mutha\] · **The one question it answers:** What is happening in the market, and why?

**Role.** The generative AI component, which explains the market to users. It is not on the trading path: if C fails or slows down, order placement and matching must be unaffected. It explains the market only; it gives no investment advice and makes no trading decisions.

**Responsibilities**

1. **News ingestion.** News comes from a scripted file that C replays into the system. C owns the replay and the asynchronous ingestion of its output. Items that fail processing are set aside, so they are neither lost nor block other news.
2. **Searchable knowledge base.** News is organized into a searchable index. The index is versioned, so every answer can be traced to the index version it used.
3. **Question answering (RAG).** Users ask questions, for example why a trading pair moved. Answers are based on news combined with prices obtained from B. Every answer must:
   - **Cite verifiably:** each citation points to a news item that was actually retrieved, never an invented source.
   - **State freshness:** tell the user how recent the information is.
   - **Admit insufficient evidence:** say so explicitly instead of guessing.
4. **Safety.** News content may contain instructions meant to manipulate the model (prompt injection). C detects and isolates such content before it is used.
5. **Frontend part.** Provides the news feed and Q&A interface inside the frontend shared with A.
6. **System monitoring dashboard.** Owns the dashboard shared by the whole system, covering request rate, errors and per-hop latency, plus model latency, token use and the refusal rate.
7. **Compose and README.** Owns the `docker compose up` setup and the README.
8. **Report section.** Writes the report section on the AI component and its architectural implications.

**Consistency.** News and prices are eventually consistent and may lag, but answers state the time of the data they use. News processing is at-least-once, and reprocessing creates no duplicate content. When the model or index is unavailable, Q&A returns a clear failure rather than an unfounded answer.

**Out of scope.** Investment advice, trading decisions, risk control (D), matching and market data publishing (B), accounts (A), live news generation, and real news sources.

**Acceptance criteria**

1. Every citation in an answer can be found among the retrieved news; there are no fabricated sources.
2. When nothing relevant is retrieved, the answer says the evidence is insufficient instead of inventing one.
3. News containing injected instructions does not change the answer's behavior.
4. Stopping C entirely does not affect order placement or matching.
5. The dashboard shows request rate, errors and latency for every service, and model latency, token use and refusal rate for C.

## Service D: Risk Control

**Owner:** \[Zongyan Yang\] · **The one question it answers:** Should this order be allowed?

**Role.** The gatekeeper between A and B. D is entirely rule-based: the same inputs always produce the same decision, and no AI is involved. It sits on the order path, so its latency and availability matter to the whole exchange.

**Responsibilities**

1. **Order gate.** Every order A hands over is checked against the rules. If it passes, D forwards it to B using the same order ID; if not, D rejects it straight back to A with a clear reason. A retried order gets the same decision and is forwarded to B at most once.
2. **Two rules.** We keep the rule set small so that each rule's cost stays inside the latency budget.
   - **Self-trade prevention (single-order rule).** D rejects an order that would match against the same user's resting order. D decides from its trader features, so this check is best-effort; B's matcher is the atomic backstop.
   - **Wash trading (cross-order rule).** D rejects orders that form suspected wash trading between linked accounts, using history across orders and accounts.
   Checks that need only the single request and the user's own account (balance, size limit, rate limit) stay in A, so the two do not overlap.
3. **Trader features.** D builds each trader's recent behavior asynchronously from B's order events and trade stream: open orders, recent trades, and the links between accounts (from A's seed data). The two rules use them.
4. **Rule versions.** The rule set is versioned, and every decision carries the version it used, so it can be audited and replayed.
5. **Evaluation.** Using the labelled manipulators from B's bots, we measure the rules' precision and recall, the share of legitimate trades wrongly blocked, and the p99 latency of the order gate under load.
6. **Failure injection and runbook.** Scripts simulate failures: D slowdown, D outage, stale features, a lost D to B response after B has accepted the order, B killed while an order is `UNKNOWN`, and a dropped event that A must detect as a gap. A runbook documents the response.
7. **Draft ADR.** D's owner leads the draft ADR for the synchronous risk gate (Week 10), with input from all four owners.

**Consistency.** D sits on the critical path, so it fails closed: if D cannot decide within a bounded time, the order is not allowed through. Because a timeout can hide an order that already reached B, A resolves the order's fate with B before releasing any hold. Trader features may lag, but the lag is measured and reported.

**Out of scope.** AI or machine learning, a human review queue, account and fund checks (A), matching (B), news and Q&A (C), automatic account suspension, and, unless we reach the stretch goal, leverage and forced liquidation.

**Acceptance criteria**

1. Replaying the same order sequence through D produces identical decisions.
2. Against the scripted manipulators, we report precision, recall and the share of legitimate trades wrongly blocked.
3. Each approved order reaches B at most once, and each rejected order reaches A exactly once.
4. With D forcibly stopped, orders do not hang: each ends as filled, cancelled or rejected once A has resolved it.
5. The p99 latency of the gate under the defining scenario stays within D's share of the budget.

## Riskiest decision

**We put a synchronous, rule-based risk gate on the critical path of every order.** Every order passes through D before it can reach the matching engine. We contain this with a small, fast, deterministic rule set that fails closed. Forced liquidation, which would have added a second source of orders, is now a stretch goal and not part of the core. This section will become the draft ADR for Week 10.

**Why it is the riskiest.** It couples exchange availability to D: if D is down or slow, nobody can trade. Order placement crosses A, D and B synchronously, so p99 compounds across three hops. A timeout is ambiguous: B may already hold an order whose response was lost, so rejecting on timeout and releasing the hold could let that order fill against released funds. We handle that with the order-fate protocol above, at the cost of funds frozen while B is unavailable. False positives block legitimate trades.

**Rejected alternatives.** Post-trade risk checks only: simple and off the critical path, but bad orders have already traded. Putting an AI model in the gate: it catches subtler manipulation, but its inference latency is hundreds of milliseconds and it is non-deterministic, which conflicts with a matching engine that must replay exactly.

**Degraded mode: a trade-off between protection and availability.** If the gate exceeds its latency budget, we shed load at A and keep both rules on. This protects against manipulation but rejects some users under load. The rejected alternative is to drop D to the single-order rule, which keeps trading available but switches off the wash-trading rule exactly when a manipulator would strike.

**The fact that would change our mind.** If, at the target load, shedding rejects more legitimate orders than we can accept (a threshold we will set from the Week 6 baseline), we switch to the rejected alternative.

**Validation.** By Week 8 we will measure the gate's p99 latency per hop and end to end under bot load, precision and recall against the scripted manipulators, and the two failure tests: a lost D to B response with B holding the order, and B killed while an order is `UNKNOWN`. We will also show A detecting and recovering from a dropped event.
