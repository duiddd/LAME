# Report input — Service A: Trading & Accounts (owner: Pin-Yun Kuo)

Text for the team's architecture report (at most six pages in total). The sections below map onto
the report's sections; each is written to be cut down rather than padded. Numbers come from
`service_a/tools/measure_local.py` and `bench_notifications.py`, run on 18 Nov 2026 against the
real D and B on one Windows laptop (Docker Desktop for PostgreSQL and B), so they are a floor for
what a Linux host would show, not a target.

## A in the architecture (≈ ¼ page)

A is the only writer of users, balances, holds and order state, and the browser's only way in.
It answers one question: are balances correct? Every command goes to D over HTTP; D's `202` only
acknowledges receipt. Every outcome — D's risk decision, B's acceptance, each trade, each cancel —
comes back as a notification D pushes to `POST /internal/order-events`, one at a time, in a
contiguous `notify_seq`. A never talks to B's command channel; it only reads B's public market
data to price-check orders. A runs as one process with one worker, because its rate limiter,
circuit breaker and shedding window live in memory (tech decision D2).

## Consistency (≈ ½ page)

| Where | Guarantee | Mechanism |
| --- | --- | --- |
| Placing an order: insert, hold, ledger entry | Strong, one PostgreSQL transaction | Conditional `UPDATE … WHERE available >= amount`; `CHECK (available >= 0)` as a backstop. 100 concurrent holds on one balance never overdraw (`test_concurrent_holds_never_overdraw`) |
| Order status and settlement | Eventual: they follow D's notifications | Measured lag from placing to `PROCESSING`: p50 228 ms, p95 505 ms |
| Each notification's effect | Effectively once | Idempotent consumer: the cursor row is locked `FOR UPDATE` and advanced in the same transaction as the settlement, so a resend after a crash is a duplicate (`test_crash_before_answer`, `test_concurrent_copies_apply_once`) |
| A user's double click, A's resend to D, D's resend to A | No double effect | Three idempotency keys: `client_order_id`, A's `order_id` (D deduplicates), `notify_seq` plus B's `(day, seq)` for trades |
| What the browser shows | Eventual, with replay | The event log row commits with the change; SSE replays from `Last-Event-ID` after a disconnect |

A rule follows from this, and is the decision recorded in ADR-002: **once a request may have
reached D, funds move only on D's notification**, never on a timeout or an acknowledgement. Only
a request that certainly never left A (no connection, or D's own "not taken") is released at once.

## Resilience, with evidence (≈ ½ page)

| Injected failure | What A did | Measured |
| --- | --- | --- |
| D's process killed | New orders refused with `503 SYSTEM_BUSY`; nothing held; A stayed healthy | First 5 refusals took 580–598 ms each (the 0.5 s connect timeout); then the breaker opened and refusals took 15–18 ms |
| D restarted | D resumed from B; A's breaker let a trial through after its 10 s window | D ready after 4.2 s; A accepting again after 9.0 s; 15.1 s from kill to recovery |
| Request sent, no reply | Order `UNKNOWN`, hold kept, same `order_id` resent, cancelled after `RESEND_WINDOW` | Unit tests with a fake D (`test_unknown_*`) |
| Notification ahead of a gap | `409 {"expected_seq": N}`; D resends from N | `test_gap_answers_expected_seq_at_top_level` |
| A notification A cannot apply | `422`, the line stops, nothing applied | `test_illegal_transition_stops_the_line` |
| B's feed silent for 3 s | Limit and market orders refused with `NO_FRESH_PRICE` | `test_stale_market_refuses_both_types` |

End to end against the real D and B (`local_e2e.py`, 26 checks): a match settles both users in one
transaction, D's self-trade rule rejects and releases, a quantity-only modify grows and shrinks the
hold, a cancel releases only the unfilled part, a market buy fills what the book offers and
releases the rest, and the ledger explains every balance.

**What breaks first under load:** applying notifications. They are applied one at a time by
design, and each takes 18 ms (p50) for an `ACCEPTED` and 31 ms for a cancel that releases funds:
about 46 and 29 notifications per second here. One trade produces up to three notifications, so A
settles roughly 10–15 trades a second before D's outbox starts to queue. The first remedy is to
apply a contiguous batch of notifications in one transaction, which keeps the ordering guarantee.

## Input to the trade-off section (≈ ⅓ page)

**Release a hold on a timeout, or only on D's notification?** Releasing on a timeout frees funds
within the 2 s call timeout, but D may already have passed the order to B: a fill would then
settle against money the user has spent again, and the balance `CHECK` would stop all settlement.
Holding until the notification never lets that happen, at the price of funds frozen for as long
as D is unreachable — about 15 s in the test above, longer if D stays down — and of a user-visible
"Confirming" state. We chose correctness, because a frozen balance recovers by itself and an
overdrawn one needs a person.

**Modify the price and quantity, or the quantity only?** The team decided on 18 Nov that a modify
changes only the remaining quantity, up or down. D carries out any modify as a cancel and a new
order at B, so the order loses its time priority either way; keeping the price fixed means the
price band does not need re-checking and the hold changes in one direction per modify, while a
price change remains possible as cancel and re-place.
