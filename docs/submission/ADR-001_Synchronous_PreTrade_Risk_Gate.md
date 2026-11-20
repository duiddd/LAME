# ADR-001 — A synchronous rule-based risk gate (D) sits between A and B and fails closed

**Status:** accepted

**Context.** LAME must stop self-trading (R1) and wash trading between linked accounts (R2). Once an order
reaches B and trades, the fill is final: ownership cannot be unwound without breaking price-time priority for
the counterparty. B replays its daily journal (`events-<day>.db`) bit for bit after a crash and accepts only one
command connection (`/d`), so whatever checks orders must sit in front of B, be deterministic and fit that
single connection. Under overload or failure the gate must either let unchecked orders through (fail open) or
refuse them (fail closed).

This decision would reopen if, under the target bot traffic, load shedding refused more legitimate orders than
the demo can accept; or if B gained several command channels and a way to reverse a trade after the fact.

**Decision.** Verdict: accept. Zongyan Yang decided that D (`:8400`) is a single-instance, synchronous,
rule-based gate between A and B, and that every trading command (new, cancel, modify) goes through it. R1 and R2
read no clock, no randomness and no I/O, only the order, D's in-memory view of the book and
`linked_accounts.json`; each decision is stored with `(decision_no, features_seq, rule_version)` before the
order goes to B. D answers `202` once it has stored an order, or refuses at once (`503 RISK_UNAVAILABLE` /
`RISK_BUSY`, nothing stored), and reports every outcome through its notification outbox. Under load D keeps
both rules: a queue of 1,000 and a 500 ms gate deadline (`RISK_TIMEOUT`), with A shedding new orders when its
latency budget is exceeded. The trade-off is integrity over availability. Post-trade surveillance was rejected
because a self or wash trade would already be final when found; an ML or LLM classifier because its latency
and drift break replay; dropping to R1 only under load because it opens the wash-trading window exactly when an
attacker floods the gate.

**Consequences.**

- No unchecked order reaches B, against the rule that fills are final: an approved order is sent to B at most
  once and a rejected one never (`(user_id, order_id)` idempotency). On 20 Nov an order A sent three times
  during an 8 s outage was stored once and reached B once, and a self-trade was rejected with its hold released.
- The gate costs little time, against A's 1 s shedding threshold: decision p99 was 1 ms at 200 orders/s and
  2 ms at 1,000 orders/s in D's standalone load test (`tools/loadtest.py`).
- While D is down nobody can place, cancel or modify, and settlement waits, against the fail-closed rule: with
  D killed for about 49 s on 20 Nov, A refused every new order in a median 24 ms with nothing held, and accepted
  again 1.2 s after D was ready.
- A slow D rejects rather than queues, against the 500 ms deadline: with 2 s added to each decision, D rejected
  the late orders `RISK_TIMEOUT` and A shed new orders within 4.8 s, so no order waited on a stale decision.
- Judging crossing limit prices without simulating the queue can refuse a legitimate trade between linked
  accounts, against the goal of blocking only manipulation; `d/evaluate.py` reports these cases.
