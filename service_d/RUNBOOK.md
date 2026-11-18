# Service D — Runbook

One section per failure scenario (architecture document section 12). Each has: how to trigger
it for a test, how to detect it, how to handle it, and how to confirm recovery.

`python -m tools.fault status` prints D's health and the metrics used below. Fault switches
need `FAULTS_ENABLED=true` and must never be on in a normal run.

Useful metrics: `lame_d_ready`, `lame_d_b_connected`, `lame_d_decision_queue`,
`lame_d_decision_seconds` (histogram), `lame_d_decisions_total{decision,reason}`,
`lame_d_refused_total{reason}`, `lame_d_timed_out_orders`, `lame_d_cancels_waiting`,
`lame_d_outbox_backlog`.

## 1. D is slow

- **Trigger:** `python -m tools.fault d-slow 800` (0 turns it off).
- **Detect:** `lame_d_decision_seconds` p99 goes up; `lame_d_decision_queue` grows;
  `lame_d_decisions_total{reason="RISK_TIMEOUT"}` rises; with a full queue,
  `lame_d_refused_total{reason="RISK_BUSY"}` rises. A's load shedding starts (A's `SYSTEM_BUSY`).
- **Handle:** nothing is wrong with correctness: late orders are rejected with `RISK_TIMEOUT`
  and never sent to B, and A releases their holds. Find the cause (CPU, a slow disk for
  SQLite, a very large book view). The rules stay on; D never weakens them under load.
- **Confirm recovery:** the queue drains to 0, the p99 is back inside D's share of the
  budget, and A stops shedding.

## 2. D is down

- **Trigger:** `python -m tools.fault d-down` (`docker kill`; compose restarts it).
- **Detect:** D's `/healthz` does not answer; A's circuit breaker opens; A's orders become
  `UNKNOWN` or stay `SENT`; A's settlement stops moving.
- **Handle:** wait for the restart (`restart: unless-stopped`). If D does not come back, check
  its logs and the `/data` volume. Do not release any holds by hand: an order may already be in B.
- **What D does on restart:** reads its database, resumes from B at `from_seq = 0`, rebuilds
  the book view, and becomes ready only after catching up. Outputs it had already turned
  into notifications produce none again. The outbox continues from the last notification
  A acknowledged.
- **Confirm recovery:** `lame_d_ready = 1`; `lame_d_outbox_backlog` drains to 0; A's
  `UNKNOWN`/`SENT` orders all reach a final state (A's reconciliation queries
  `GET /v1/orders/{order_id}` and gets answers).

## 3. Stale features

- **Trigger:** `python -m tools.fault stale-features on`, then `off`.
- **Detect:** `lame_d_ready = 0` while `lame_d_b_connected = 1`; `/healthz` shows
  `caught_up: false`; new orders get 503 `RISK_UNAVAILABLE` (`lame_d_refused_total`).
- **Handle:** D refuses to decide on stale features, so nothing wrong is let through. If this
  happens without the switch, the chain of B's outputs has a gap: D reconnects by itself
  after `GAP_TIMEOUT_MS`. If it keeps happening, check B's logs.
- **Confirm recovery:** `caught_up: true`, `lame_d_ready = 1`, orders get 202 again.

## 4. B accepted the order, but the reply was lost

- **Trigger:** `python -m tools.fault reply-lost`, then place one order.
- **Detect:** D's log shows the B connection closing and a resume; `lame_d_b_connected`
  drops briefly.
- **Handle:** nothing to do. On reconnect B resends its outputs, D sees `ACCEPTED` (and any
  trades) for the order, drops the automatic cancel and notifies A as usual.
- **Confirm recovery:** the order is `LIVE` (or filled) in `GET /v1/orders/{order_id}`; no
  `cancel_order` was sent for it; A received `ACCEPTED`.

## 5. B is down while an order is TIMED_OUT

- **Trigger:** place an order, make it time out (for example `reply-lost` while B is being
  killed), then `python -m tools.fault b-down`.
- **Detect:** `lame_d_b_connected = 0`, `lame_d_ready = 0`; `lame_d_timed_out_orders > 0`;
  `lame_d_cancels_waiting > 0`. New orders get 503 `RISK_UNAVAILABLE`.
- **Handle:** wait for B to come back. Holds stay in place; nothing is released on a timeout.
- **What D does when B is back:** reconnects, catches up, and only then decides. If the order
  had reached B, its outputs arrive in the resend and the automatic cancel is dropped.
  Otherwise D sends the cancel after the age limit, B answers `ORDER_NOT_FOUND`, and D
  notifies `ORDER_UPDATE REJECTED NOT_DELIVERED`.
- **Confirm recovery:** `lame_d_timed_out_orders = 0` and `lame_d_cancels_waiting = 0`; the
  order has a final state.

## 6. A notification is lost

- **Trigger:** `python -m tools.fault lose-notification`, then place an order.
- **Detect:** A answers `409 {"expected_seq": N}`; D's log shows "A expects notify_seq N".
- **Handle:** nothing to do. D resends from N. If A finds a missing notification some other way
  (its reconciliation), it calls `POST /v1/notifications/rewind {"from_seq": N}`.
- **Confirm recovery:** `lame_d_outbox_backlog = 0`; A's last applied `notify_seq` equals D's
  newest; balances are correct.

## 7. A is down

- **Detect:** `lame_d_outbox_backlog` grows; D's log shows "A unreachable".
- **Handle:** nothing in D. D keeps deciding and talking to B; notifications wait in the
  outbox and are sent in order when A is back.
- **Confirm recovery:** the backlog drains to 0.
