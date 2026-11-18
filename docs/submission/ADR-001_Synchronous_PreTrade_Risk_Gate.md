# ADR-001: Synchronous Pre-Trade Risk Gate (Service D) Failing Closed on the Critical Path

**Status:** Accepted
**Date:** 2026-11-18
**Owners:** Zongyan Yang (Service D) & Team LAME (Pin-Yun Kuo, Bo Wu, Rushabh Mutha)

---

## Context

LAME must prevent self-trading (**R1**) and wash trading across seeded linked accounts (**R2**) on a deterministic price-time spot exchange (`A: Trading & Accounts` → `D: Risk Control` → `B: Matching Engine`). Three load-bearing constraints govern how risk checks attach to the order path:

1. **Zero Unauthorized Matches:** Once an order enters `B`'s matching thread and trades, spot ownership changes cannot be cleanly unwound without breaking price-time finality for counterparties.
2. **Deterministic Replay & Latency Budget:** `B` guarantees bitwise reproducible crash replay from daily SQLite journals (`events-<day>.db`). Adding a non-deterministic AI risk model (hundreds of milliseconds, stochastic outputs) on the critical path violates replay determinism and stalls `p99`. Meanwhile `B` accepts only **one** upstream command WebSocket (`/d`).
3. **Failure Behaviour & Degraded Mode:** At overload or gate failure, the system must choose between allowing unvetted manipulation (**fail-open**) or blocking orders (**fail-closed**).

*Flip conditions:* Revisit only if measured load shedding under target bot traffic rejects more legitimate orders than acceptable (or if `B` introduces multi-tenant command channels and post-trade reversal compensation).

---

## Decision

We place **Service D (`:8400`)** as a **single-instance, synchronous, rule-based pre-trade gate** directly between `A` and `B`, keeping all trading commands (`new`, `cancel`, `modify`) routed exclusively through `D`:

1. **Deterministic Pure Rules (`R1`, `R2`):** Rules read no system clock, no randomness, and no I/O — operating only on `(order, full in-memory book view + linked_accounts, rule_version)`. Every decision persists `(decision_no, features_seq, rule_version)` in SQLite WAL before forwarding to `B`.
2. **Fast `202` Ack + Ordered Notification Outbox:** `D` stores valid orders (`202` received) or refuses immediately (`503 RISK_UNAVAILABLE` / `RISK_BUSY` with **no** database record). Decisions and `B` events stream back to `A` via `D`'s durable notification outbox (`notify_seq`).
3. **Fail-Closed & Load Shedding (Degraded Mode):** `D` enforces a strict queue (`DECISION_QUEUE_CAP = 1000`) and gate deadline (`500 ms` → `RISK_TIMEOUT`), and **never disables R2 (wash trading)** under load; instead, `A` sheds excess traffic when recent `p99` exceeds budget.

---

## Consequences

**Positive (assessed against constraints)**
- **Pre-match integrity & replayability:** Approved orders reach `B` at most once (via `(user_id, order_id)` idempotency); `R1`/`R2` decisions replay deterministically (`d/replay.py`).
- **Predictable low overhead:** Standalone decision `p99` measured `1 ms` at `200 orders/s` and `2 ms` at `1,000 orders/s`; `A` circuit-breaker open refusals drop from `580–598 ms` to `15–18 ms` during `D` outages.

**Negative (knowingly accepted trade-offs)**
- **Single-point availability coupling:** Killing `D` halts new orders, cancels, modifies, and settlement pushes from `B` until `D` restarts and catches up (`4.2 s` D catch-up + `9.0 s` A breaker recovery measured).
- **Conservative limit-order false positives:** Judging crossing limit prices without full queue simulation occasionally blocks legitimate linked-account trades (reported openly in `d/evaluate.py`).

---

## Alternatives Considered

- **Asynchronous post-trade surveillance:** Removes `D` from critical-path latency, but malicious self/wash trades execute irreversibly in `B` before detection.
- **ML/LLM risk classifier inside D:** Catches subtler patterns, but adds `>100 ms` latency variance and non-deterministic decision drift incompatible with replay auditing.
- **Shed to R1-only during overload:** Preserves throughput during spikes, but exposes precisely the linked-account wash-trading window when attackers flood the gate.
