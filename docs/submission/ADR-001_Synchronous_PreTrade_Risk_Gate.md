# ADR-001 — A synchronous, rule-based risk gate (D) is positioned between A and B and is configured to fail closed.

**Status:** accepted

**Context.** LAME is required to prevent self-trading (R1) and wash trading between linked accounts (R2). Once an order
reaches B and is executed, the fill is final. Ownership cannot be reversed without violating price-time priority for
the counterparty. B replays its daily journal (`events-<day>.db`) exactly after a crash and accepts only one
command connection (`/d`). Therefore, any order-checking mechanism must precede B, operate deterministically, and conform to that
single connection. Under overload or failure, the gate must either allow unchecked orders (fail open) or
refuse them (fail closed).

This decision will be reconsidered if, under the expected bot traffic, load shedding results in the refusal of more legitimate orders than
the system can tolerate, or if B acquires multiple command channels and a mechanism to reverse trades post-execution.

**Decision.** Verdict: accept. Zongyan Yang determined that D (`:8400`) will operate as a single-instance, synchronous,
rule-based gate between A and B, and that all trading commands (new, cancel, modify) must pass through it. R1 and R2
do not access clocks, randomness, or external I/O; they rely solely on the order, D’s in-memory view of the book, and
`linked_accounts.json`; each decision is stored with `(decision_no, features_seq, rule_version)` before the order is
forwarded to B. D returns a `202` response once it has stored an order, or immediately refuses (`503 RISK_UNAVAILABLE` /
`RISK_BUSY`, with nothing stored), and reports every outcome through its notification outbox. Under load, D maintains both
constraints: a queue of 1,000 and a 500 ms gate deadline (`RISK_TIMEOUT`), with A shedding new orders when its latency
budget is exceeded. The design prioritizes integrity over availability. Post-trade surveillance was rejected because a
self-trade or wash trade would already be final when detected. Machine learning or large language model classifiers were
rejected due to their latency and model drift, which disrupt replay; reducing to R1 only under load was rejected because it
would open the wash-trading window precisely when an attacker floods the gate.

**Consequences.**

- No unchecked order reaches B, in accordance with the rule that fills are final. An approved order is sent to B at most
  once, and a rejected order is never sent (`(user_id, order_id)` idempotency). On 20 Nov, an order A sent three times
  during an 8-second outage was stored once and reached B once. A self-trade was rejected, and its hold was released.
- The gate introduces minimal latency relative to A’s 1-second shedding threshold. The 99th percentile decision time was 1 ms at 200 orders/s and
  2 ms at 1,000 orders/s in D’s standalone load test (`tools/loadtest.py`).
- While D is unavailable, no orders can be placed, canceled, or modified, and settlement is delayed, consistent with the fail-closed rule. With
  D terminated for approximately 49 seconds on 20 Nov, A refused every new order in a median of 24 ms with nothing held, and resumed acceptance
  1.2 seconds after D was restored.
- If D is slow, it rejects rather than queues orders, in accordance with the 500 ms deadline. With 2 seconds added to each decision, D rejected
  the late orders as `RISK_TIMEOUT`, and A shed new orders within 4.8 seconds, ensuring no order waited on a stale decision.
- Evaluating crossing limit prices without simulating the queue may result in the refusal of legitimate trades between linked
  accounts, contrary to the goal of blocking only manipulative activity. `d/evaluate.py` reports these cases.
