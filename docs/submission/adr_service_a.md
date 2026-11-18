# ADR-A — Funds stay held until D's notification settles the order's fate

**Status:** accepted

**Context.** A holds a user's funds when an order is placed, then sends it to D over HTTP; D runs
its risk check and passes the order to B. D's synchronous `202` only says that D received the
order. The outcome — rejected by D, accepted, filled, cancelled — arrives later as a notification,
which D stores and resends until A confirms it. A timeout on the call is ambiguous: the order may
never have reached D, or B may already hold it. Spot trading allows no overdraft; the balance
`CHECK (available >= 0)` is the last line, and if a settlement hits it, A stops applying
notifications for every user (the line stops by design).

This decision would reopen if D offered a synchronous answer that is final (decision and B's
result within the call), if B could guarantee rejecting orders older than the timeout so that
"timed out" meant "not placed", or if frozen funds during an outage cost users more than an
occasional overdraft corrected by hand.

**Decision.** Pin-Yun Kuo decided that once a request may have reached D, A releases or settles
funds only on D's notifications, never on a timeout or an acknowledgement. A request that
certainly never left A — the connection could not be made, or D answered that it kept no record —
is rejected and released at once. A request sent without a reply becomes `UNKNOWN`: the hold stays,
A resends the same `order_id` (D deduplicates) for up to `RESEND_WINDOW` (5 s), then cancels; the
cancel's result releases the funds. Orders stuck in `SENT` are queried at D with backoff (10, 20,
40, then every 60 s), and a query never moves funds.

**Consequences.**

- No fill can settle against released money: verified by the fake-D tests and 26 end-to-end
  checks against the real D and B, where every finished order closed with nothing held.
- Funds freeze while D is unreachable. With D killed and restarted, A recovered in 15.1 s (D ready
  4.2 s after restart, A accepting 9.0 s after it); orders placed meanwhile were refused, so the
  freeze only covers orders already in flight. Against a demo that runs for minutes this is
  acceptable; an outage over the 5 s resend window turns in-flight orders into cancels.
- Users see "Confirming" instead of a plain failure for an unanswered request, and must not
  re-submit; the UI says so. The cost is one extra state in the order lifecycle and its tests.
- Settlement depends on D's delivery being ordered: A applies notifications one at a time, which
  limits it to about 29–46 notifications a second on this laptop, roughly 10–15 trades a second.
  That covers a classroom demo, not the bot load of the defining scenario; batching contiguous
  notifications in one transaction is the planned remedy and keeps this decision intact.
- If D loses its outbox, held funds stay held until someone intervenes: A will not guess. The
  `lame_a_orders_unsettled` and `lame_a_notify_cursor_age_seconds` metrics expose it.
