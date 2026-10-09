# COMP41720 Week 5 Proposal

Oct 9, 2026 · @Yuchen

## Option and system

**Group:** \[Member A\], \[Member B\], \[Member C\], \[Member D\]

**Option A**: a traditional distributed system with an AI component. We will build a simulated exchange with a built-in market terminal. Users trade on a matching engine, and the terminal shows live prices, news, and an AI analyst that explains market moves. The AI component is a generative analyst (RAG) that answers questions about the market. Risk control on the trading path is deliberately rule-based and deterministic, so the matching engine can replay exactly.

**Scope.** Two trading pairs; limit, market and cancel orders; leveraged trading with real-time liquidation and an overnight margin check; synthetic users and template-generated news. Simulated trading bots generate load, and some are scripted market manipulators, which gives us labelled data for evaluating the risk rules. Everything runs offline via `docker compose up`, using Ollama or a simulated model. Out of scope: real authentication, deposits and withdrawals, interest and funding fees, an insurance fund, and stop orders.

## Service sketch

Four services, one owner each. Each service answers exactly one business question and owns its own database.

| Service | Owner | The one question it answers | Key patterns |
| --- | --- | --- | --- |
| A. Trading & Accounts | \[Member A\] | Are balances and assets correct? | Trading API and gateway; idempotent order placement; strongly consistent fund holds and margin; idempotent settlement; user-private notifications |
| B. Matching & Market Data | \[Member B\] | Who trades with whom, at what price? | Deterministic price-time matching; total order per symbol; crash recovery by replay; sequenced market data feed; trading bots and load tests |
| C. News & AI Analyst | \[Member C\] | What is happening in the market, and why? | Asynchronous news pipeline; versioned searchable index; RAG answers with verified citations, freshness labels and explicit "insufficient evidence"; prompt-injection screening |
| D. Risk Control | \[Member D\] | Should this order be allowed? | Rule-based pre-trade gate that fails closed; trader features built from the trade stream; real-time liquidation; overnight margin call; rule-versioned decisions |

\[Architecture diagram: four services and their interactions, to be added\]

**Communication.** A hands every order to D first. If it passes, D forwards it to B; if not, D rejects it straight back to A. B publishes order events and trades. A (settlement), C (prices for answers) and D (trader features, margin monitoring) each consume these asynchronously, and D also reads the account data it needs from A. D can originate forced-liquidation orders to B on its own. C also consumes the news feed.

**Consistency.** Strong consistency only for balances and holds in A. Order sequencing is total per symbol. Everything else is eventually consistent, with lag measured and shown on the dashboard. Every consumer is idempotent, because delivery is at-least-once and the engine may replay. D is on the critical path and fails closed: an order D cannot decide on in time is rejected.

**Shared work.** A: compose setup and README. B: trading bots and load tests. C: Prometheus and Grafana dashboard (rate, errors, latency, token cost). D: failure-injection scripts and runbook.

## Service A: Trading & Accounts

**Owner:** \[Member A\] · **The one question it answers:** Are balances and assets correct?

**Role.** The user-facing entry point of the exchange, and the single authority for account balances and order state. Its users are discretionary traders placing orders by hand. Bots only act as load sources; A provides no features specific to programmatic trading.

**Responsibilities**

1. **Multi-user accounts.** Users are isolated from each other. Identity is lightweight (synthetic users, no real authentication). Each user has available balance, held balance and positions, and every change leaves a ledger entry. Initial assets come from seed data; there are no deposits or withdrawals.
2. **Fund and margin management.** Funds are held synchronously when an order is placed, and settled or released correctly on fill, cancel, modify or reject. Settlement is idempotent. Users may trade with leverage up to a configured limit; A tracks margin used and available per account, and provides D with the account data it needs (collateral, positions, leverage). A remains the authority for balances. A delivers margin calls and liquidation notices to the affected user, and accepts system-originated liquidation orders from D, settling them like any other order.
3. **Order functions.** Limit orders, market orders, cancel, and modify (modeled as cancel the old order and place a new one). Placement carries an idempotency key, so retries never create duplicate orders.
4. **Order state.** A is the only maintainer of order state. It updates orders and accounts from events emitted by the matching side, and lets users query open orders, order history and fills.
5. **Preliminary risk control.** Before handing an order to the risk service (D), A runs cheap checks that need only the single request and the user's own account: sufficient balance or margin, per-order size limit, price protection, rate limiting and self-trade prevention. Orders that pass go to D, which either forwards them to B or rejects them back to A. On a rejection, A releases the hold and reports the reason to the user.
6. **API gateway.** The entry point for all trading and account operations. It handles validation, rate limiting and idempotency, and pushes each user's own order and balance updates to the frontend. Market data does not pass through A; the frontend obtains it directly from B.
7. **Frontend.** One frontend shared with C. A owns the overall application shell; C owns the news and Q&A parts.

**Consistency.** Balances, holds and order acceptance are strongly consistent. Settlement and order-state updates are eventually consistent, with measurable lag. A failure of D must not take A down; waiting on D is always bounded, and an order D does not answer is treated as rejected.

**Out of scope.** Real authentication, deposits and withdrawals, interest and funding fees, an insurance fund, stop orders, programmatic-trading features, matching, and behavioral risk rules and liquidation decisions (D).

**Acceptance criteria**

1. After concurrent trading by many users, balances, holds and ledger entries are consistent.
2. Duplicate requests or duplicate trade notifications never take effect twice.
3. When D times out or fails, A rejects the order with a clear reason within a bounded time and releases its hold.
4. A user can complete the full flow in the UI: view the market, place, modify and cancel orders, and see the account change.

## Service B: Matching & Market Data

**Owner:** \[Member B\] · **The one question it answers:** Who trades with whom, at what price?

**Role.** The matching core of the exchange and the single source of market data. B cares only about orders and trades, not accounts or funds. Every order it receives comes from D, either a user order that A has validated and D has approved, or a forced-liquidation order that D originated itself.

**Responsibilities**

1. **Matching.** Price-time priority matching per trading pair. Supports limit orders, market orders and cancels. A modify arrives as a cancel plus a new order, so B needs no separate handling. A market order that cannot be fully filled for lack of liquidity has its unfilled remainder cancelled; it never rests on the book.
2. **Sequencing.** All orders within one trading pair have a single, deterministic order. No ordering is required across pairs.
3. **Order events and trades.** The outcome of every order is published: accepted, partially filled, filled, cancelled, rejected. Every trade has a unique identifier. A uses these events to update orders and accounts; C uses the trade stream for prices, and D uses it with the order events for trader behavior and margin monitoring.
4. **Determinism and recovery.** The same order sequence always produces the same result. After a crash or restart, the engine returns to its pre-crash state without losing orders or duplicating trades. Recovery and replay make no calls to other services.
5. **Market data.** B is the only publisher of market data: order book depth, latest trades, last price and candles. All other components obtain market data through B. Every update carries a sequence number, so clients can detect gaps and resynchronize. A slow client must not hold back the others.
6. **Trading bots and load testing.** Simulated bots generate traffic, including scripted market manipulators with ground-truth labels for evaluating D's risk rules. B also owns system-wide load testing.

**Consistency.** Orders are totally ordered within a pair and matching is deterministic. Order events and trades are delivered at least once, and consumers deduplicate on unique identifiers. Market data may lag but must never be reordered or silently lost.

**Out of scope.** Account and fund checks (A), risk control (D), news and Q&A (C), authentication, other order types such as stop orders, and auctions.

**Acceptance criteria**

1. Replaying the same order sequence twice produces identical trades.
2. After a forced engine kill and restart, the order book matches its pre-kill state, with no lost or duplicated trades.
3. A slow or disconnected client does not affect matching or other clients' market data.
4. Under bot load, the system produces labelled manipulation data that D can be evaluated against.

## Service C: News & AI Analyst

**Owner:** \[Member C\] · **The one question it answers:** What is happening in the market, and why?

**Role.** The generative AI component, which explains the market to users. It is not on the trading path: if C fails or slows down, order placement and matching must be unaffected. It explains the market only; it gives no investment advice and makes no trading decisions.

**Responsibilities**

1. **News ingestion.** News is synthetic and template-generated. C owns both the news generator and the asynchronous ingestion of its output. Items that fail processing are set aside, so they are neither lost nor block other news.
2. **Searchable knowledge base.** News is organized into a searchable index. The index is versioned, so every answer can be traced to the index version it used.
3. **Question answering (RAG).** Users ask questions, for example why a trading pair moved. Answers are based on news combined with prices obtained from B. Every answer must:
   - **Cite verifiably:** each citation points to a news item that was actually retrieved, never an invented source.
   - **State freshness:** tell the user how recent the information is.
   - **Admit insufficient evidence:** say so explicitly instead of guessing.
4. **Safety.** News content may contain instructions meant to manipulate the model (prompt injection). C detects and isolates such content before it is used.
5. **Frontend part.** Provides the news feed and Q&A interface inside the frontend shared with A.
6. **System monitoring dashboard.** Owns the dashboard shared by the whole system, covering request rate, errors, latency and token cost.

**Consistency.** News and prices are eventually consistent and may lag, but answers state the time of the data they use. News processing is at-least-once, and reprocessing creates no duplicate content. When the model or index is unavailable, Q&A returns a clear failure rather than an unfounded answer.

**Out of scope.** Investment advice, trading decisions, risk control (D), matching and market data publishing (B), accounts (A), and real news sources.

**Acceptance criteria**

1. Every citation in an answer can be found among the retrieved news; there are no fabricated sources.
2. When nothing relevant is retrieved, the answer says the evidence is insufficient instead of inventing one.
3. News containing injected instructions does not change the answer's behavior.
4. Stopping C entirely does not affect order placement or matching.
5. The dashboard shows request rate, errors, latency and token cost for every service.

## Service D: Risk Control

**Owner:** \[Member D\] · **The one question it answers:** Should this order be allowed?

**Role.** The gatekeeper between A and B, and the guardian of leveraged accounts. D is entirely rule-based: the same inputs always produce the same decision, and no AI is involved. It has two parts: **real-time risk control**, which sits on the order path and watches margin continuously, and **overnight risk control**, which checks margin at the end of each simulated trading day.

**Responsibilities**

*Real-time risk control*

1. **Order gate.** Every order A hands over is checked against the rules. If it passes, D forwards it to B; if not, D rejects it straight back to A with a clear reason. Every order gets exactly one outcome.
2. **Behavioral rules.** D owns the rules that need history across orders or users, such as an abnormal order-to-cancel ratio, suspected wash trading between related accounts, spoofing or layering, bursts that push the price in one direction, and abnormal order size. Checks that need only the single request and the user's own account stay in A, so the two do not overlap.
3. **Trader features.** D builds each trader's recent behavior asynchronously from B's order events and trade stream, for the rules to use.
4. **Real-time liquidation.** D continuously compares each leveraged account's equity, at live prices from B, with its maintenance margin. When an account blows up, D forces liquidation: it submits closing orders on the user's behalf directly to B, marked as system-originated and exempt from the normal order gate. The resulting trades settle in A like any other, and the user is notified. A breach triggers liquidation exactly once.

*Overnight risk control*

5. **Overnight margin check.** At the end of each simulated trading day, D evaluates every leveraged account against the overnight margin requirement, which is stricter than the intraday one. For each account that falls short, D issues a margin call through A, stating the shortfall. A margin call is cleared once the account meets the requirement again. It is a warning only: an unmet call triggers no automatic action, but the account is still subject to real-time liquidation if it later falls below maintenance margin.

*Cross-cutting*

6. **Rule versions.** The rule set is versioned, and every decision carries the version it used, so it can be audited and replayed.
7. **Evaluation.** Using the labelled manipulators from B's bots, we measure the rules' precision and recall, the share of legitimate trades wrongly blocked, and the p99 latency of the order gate under load.
8. **Failure injection and runbook.** Scripts simulate D's failures (slowdown, outage, stale features), and a runbook documents the response.

**Consistency.** D sits on the critical path, so it fails closed: if D cannot decide within a bounded time, the order is rejected. A retried order gets the same decision and is forwarded to B at most once. Trader features may lag, but the lag is measured and reported. Liquidation and margin-call decisions are idempotent.

**Out of scope.** AI or machine learning, a human review queue, account and fund checks (A), matching (B), news and Q&A (C), and automatic account suspension.

**Acceptance criteria**

1. Replaying the same order sequence through D produces identical decisions.
2. Against the scripted manipulators, we report precision, recall and the share of legitimate trades wrongly blocked.
3. Each approved order reaches B exactly once, and each rejected order reaches A exactly once.
4. When an account's equity falls below maintenance margin, it is liquidated exactly once and its trades settle correctly in A.
5. At the end of a simulated day, every under-margined account receives a margin call, and well-margined accounts do not.
6. With D forcibly stopped, orders are rejected within a bounded time and none are left hanging.

## Riskiest decision

**We put a rule-based risk gate, and forced liquidation, on the critical path of every order.** Every order passes through D before it can reach the matching engine, and D can also inject orders into the engine on its own. We contain this with a fast, deterministic rule set that fails closed.

**Why it is the riskiest.** It couples exchange availability to D: if D is down or slow, nobody can trade. It makes correctness depend on D's view of the market. Liquidation acts on live prices and on account data that is eventually consistent, so a stale or wrong view can liquidate a healthy account or miss a failing one. Forced orders add load exactly when the market is falling, and a burst of liquidations can feed on itself. False positives block legitimate trades.

**Rejected alternatives.** Post-trade risk checks only: simple and off the critical path, but bad orders have already traded. Putting an AI model in the gate: it catches subtler manipulation, but it is slow and non-deterministic, which conflicts with a matching engine that must replay exactly.

**Validation and fallback.** By Week 8 we will measure the order gate's p99 latency under bot load, precision and recall against the scripted manipulators, and liquidation correctness in a simulated price crash, including when features are stale. If the synchronous gate exceeds its latency budget, we reduce the inline rules to the cheapest ones and run the behavioral rules on a delayed basis.
