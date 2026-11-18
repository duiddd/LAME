# ADR-002: Hold Funds Until Service D's Notification Settles Order Fate

**Status:** Accepted
**Date:** 2026-11-18
**Owners:** Pin-Yun Kuo (Service A) & Team LAME (Bo Wu, Rushabh Mutha, Zongyan Yang)

---

## Context

`Service A` (`:8000`, PostgreSQL) is the sole authority for user balances, holds, and order state. When a trader places an order, `A` atomically debits `available` into `held` and sends the order over HTTP to `Service D` (`:8400`), which checks `R1`/`R2` and forwards approved commands over `/d` to `Service B`.

Three load-bearing facts constrain this boundary:
1. **Asynchronous Settlement & Ambiguous Timeouts:** `D`'s synchronous `POST /v1/orders` response (`202`) confirms only queued receipt — actual outcomes (`RISK_REJECTED`, `PROCESSING`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`) arrive asynchronously via ordered notifications (`POST /internal/order-events`, `notify_seq`). An HTTP timeout or lost connection after sending leaves it unknown whether `D` or `B` accepted the order.
2. **Zero-Overdraft Spot Invariant:** Spot accounts allow zero margin/overdraft. `A`'s PostgreSQL ledger enforces `CHECK (available >= 0)` on every update; if a notification violates it, `A` halts notification settlement across all users rather than corrupting balances.
3. **End-to-End Flow:** `A` does not connect directly to `B`'s command stream, so `D`'s outbox is the single ordered source of settlement truth.

*Flip conditions:* Revisit only if `D` returns a final synchronous decision+matching outcome inside the HTTP request, `B` enforces sub-timeout expiry on all in-flight commands so timeouts guarantee non-execution, or operational policy prefers occasional manual overdraft resolution over temporary balance freezes during outages.

---

## Decision

Once an order request may have left `A`, **held funds are settled or released solely by `D`'s ordered notifications (`notify_seq`) — never on an HTTP timeout or `202` acknowledgement**:

1. **Immediate Release Only on Known Non-Delivery:** Connection refused before send, or `D` returning synchronous `503` (saving no state), releases the hold immediately.
2. **`UNKNOWN` Protocol on Ambiguous Timeout:** Unanswered requests transition to `UNKNOWN` with funds kept held (`"Confirming"` in the React UI); `A` resends with identical idempotent `order_id` for up to `RESEND_WINDOW` (`5 s`), then issues `cancel` via `D` (creating a `B` tombstone if unseen), releasing funds only when `D`'s notification confirms rejection or cancellation.
3. **Idempotent Ordered Consumer:** `A` locks its `notify_seq` cursor `FOR UPDATE`, returns `409 {"expected_seq": N}` on sequence gaps so `D` rewinds cleanly, and deduplicates trades by `B`'s `(day, seq)`.

---

## Consequences

**Positive (assessed against constraints)**
- **Zero overdrafts / zero double-spends:** Verified across `210` Service A unit tests (including `test_concurrent_holds_never_overdraw` with 100 concurrent holds) and `26` end-to-end checks (`local_e2e.py`) against live `D` and `B`, leaving zero leaked holds.
- **Crash-safe restart:** Killed `A` processes resume `PENDING` orders into `UNKNOWN` reconciliation without losing ledger invariants.

**Negative (knowingly accepted trade-offs)**
- **Temporary frozen balances during `D`/`B` outages:** Measured `15.1 s` from killing `D` to `A` resuming acceptance (`4.2 s` D catch-up + `9.0 s` breaker trial); outages longer than `RESEND_WINDOW` (`5 s`) cancel in-flight orders.
- **Single-file settlement throughput ceiling:** Sequential notification application measured `29–46 notifications/s` (`~10–15 trades/s` locally); batching contiguous notifications in one DB transaction is the documented scale-up path.

---

## Alternatives Considered

- **Release holds immediately on HTTP timeout:** Restores liquidity within `2 s`, but risks letting an in-flight order fill at `B` after cash is re-spent, tripping `CHECK (available >= 0)` and halting settlement.
- **Distributed Two-Phase Commit (2PC) across A, D, and B:** Eliminates `UNKNOWN` states conceptually, but blocks Rust matching threads behind PostgreSQL locks and is fragile across container crashes.
