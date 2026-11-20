# ADR-002: Funds remain held until notification from D settles the order.

**Status:** Accepted.

**Context.** A exclusively manages balances, and spot trading prohibits overdrafts: `CHECK (available >= 0)`.
This is the final constraint; any settlement that would violate it halts settlement for all users. A places the hold in
PostgreSQL, then transmits the order to D via HTTP. D’s `202` response only confirms receipt; the outcome
(rejected by D, accepted by B, each trade, each cancel) is communicated later as a notification with a contiguous
`notify_seq`, which D retains in its outbox and resends until A responds. A call that times out is therefore
ambiguous: the order may not have reached D, or B may already hold it. A applies notifications one at a
time, measured at 29 to 46 per second on the test laptop.

This decision would be reconsidered if D provided the final outcome within the HTTP call; if B committed to refusing any
command older than A’s 2-second timeout, so that a timeout would indicate “not placed”; or if users valued the ability to
spend a frozen balance during an outage more than the infrequent overdraft that would require manual correction.

**Decision.** Verdict: Accepted. Pin-Yun Kuo determined that once a request may have reached D, A moves funds only
on D’s notifications, never on a timeout or a `202` response. A request that certainly never left A (the connection
was refused, or D indicated that it kept no record) is rejected and released immediately. A request sent without
a reply becomes `UNKNOWN`: the hold remains, A resends the same `order_id` (D deduplicates) for up to 5 seconds, then
cancels, and the result of the cancel releases the funds. Correctness was prioritized over availability: a frozen balance
recovers automatically when D returns, whereas an overdrawn balance requires manual intervention. Releasing on a timeout
was rejected because a fill could then settle against funds already spent. A two-phase commit across A, D, and B was
rejected because it would hold A’s row locks while B’s single matching thread waits.

**Consequences.**

- No fill settles against released funds, maintaining the zero-overdraft constraint: 220 A tests, including 100
  concurrent holds on a single balance, and after the 20 November fault and consistency runs, a books check over 253
  orders found zero balances unexplained by the ledger, zero holds without an open order, and zero orders left in doubt.
- Funds for orders in flight are frozen while D is unreachable, as opposed to a demonstration where a user waits minutes, not
  hours: with D’s container terminated for approximately 49 seconds, every new order was refused before any funds were held, and
  A resumed acceptance 1.2 seconds after D was ready. An outage exceeding the 5-second resend window converts an in-flight
  order into a cancel: with D paused for 8 seconds, the order ended as `CANCELLED` and its hold was returned 1.2 seconds after D
  became available again.
- Users see “Confirming” instead of a plain failure and must not place the order again, in accordance with the rule
  that one click results in at most one order: the user interface enforces this, and the cost is one additional order state and its
  transition tests (every status and notification pair is enumerated).
- Settlement follows D’s ordered delivery, which limits A to approximately 10 to 15 trades per second on the laptop
  (up to three notifications per trade): sufficient for the demonstration’s bots, but not for heavy bot load. Applying a
  contiguous batch of notifications in a single transaction is the planned remedy and maintains this decision.
- If D lost its outbox, held funds would remain held until manual intervention, contrary to the rule that no
  funds disappear silently: `lame_a_orders_unsettled` and `lame_a_notify_cursor_age_seconds` expose this condition.
