# ADR-002 — Funds stay held until D's notification settles the order

**Status:** accepted

**Context.** A is the only writer of balances, and spot trading allows no overdraft: `CHECK (available >= 0)`
is the last line, and a settlement that would break it stops settlement for every user. A places the hold in
PostgreSQL, then sends the order to D over HTTP. D's `202` only says that D received it; the outcome
(rejected by D, accepted by B, each trade, each cancel) arrives later as a notification with a contiguous
`notify_seq`, which D keeps in its outbox and resends until A answers. A call that times out is therefore
ambiguous: the order may never have reached D, or B may already hold it. A applies notifications one at a
time, measured at 29–46 a second on the test laptop.

This decision would reopen if D answered with the final outcome inside the HTTP call; if B promised to
refuse any command older than A's 2 s timeout, so that a timeout meant "not placed"; or if users valued
spending a frozen balance during an outage more than the rare overdraft that someone would then repair by hand.

**Decision.** Verdict: accept. Pin-Yun Kuo decided that once a request may have reached D, A moves funds only
on D's notifications, never on a timeout or a `202`. A request that certainly never left A (the connection
was refused, or D answered that it kept no record) is rejected and released at once. A request sent without
a reply becomes `UNKNOWN`: the hold stays, A resends the same `order_id` (D deduplicates) for up to 5 s, then
cancels, and the cancel's result releases the funds. I chose correctness over availability: a frozen balance
recovers by itself when D returns, an overdrawn one needs a person. Releasing on a timeout was rejected
because a fill could then settle against money already spent again; a two-phase commit across A, D and B was
rejected because it would hold A's row locks while B's single matching thread waits.

**Consequences.**

- No fill settles against released money, against the zero-overdraft constraint: 220 A tests, including 100
  concurrent holds on one balance, and after the 20 Nov fault and consistency runs a books check over 253
  orders found 0 balances unexplained by the ledger, 0 holds without an open order and 0 orders left in doubt.
- Funds of orders in flight freeze while D is unreachable, against a demo where a user waits minutes, not
  hours: with D's container killed for about 49 s, every new order was refused before anything was held, and
  A accepted again 1.2 s after D was ready. An outage longer than the 5 s resend window turns an in-flight
  order into a cancel: with D paused for 8 s, the order ended `CANCELLED` and its hold returned 1.2 s after D
  came back.
- Users see "Confirming" instead of a plain failure and must not place the order again, against the rule
  that one click makes at most one order: the UI says so, and the cost is one more order state and its
  transition tests (every status and notification pair is enumerated).
- Settlement follows D's ordered delivery, which limits A to about 10–15 trades a second on the laptop
  (up to three notifications per trade): enough for the demo's bots, not for heavy bot load. Applying a
  contiguous batch of notifications in one transaction is the planned remedy and keeps this decision.
- If D lost its outbox, held funds would stay held until someone intervenes, against the rule that no
  money disappears silently: `lame_a_orders_unsettled` and `lame_a_notify_cursor_age_seconds` expose it.
