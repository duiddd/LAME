# Status

## Implemented

- Price-time priority matching for limit and market orders, one book per pair.
- Journal-assigned `seq` as the order identity; `user_id` and `order_id`
  passed through to every event and trade.
- Full `OrderEvent` lifecycle: `Accepted`, `PartiallyFilled`, `Filled`,
  `Cancelled`, `Rejected` with reasons.
- Cancel by `(user_id, order_id)` or by seq; `OrderNotFound` when nothing rests.
- Self-trade prevention (D24): matching stops at the user's own resting
  order and the remainder is rejected.
- Market orders never rest; leftover quantity is cancelled.
- Tick/lot grid validation (D5) with `InvalidPrice` / `InvalidQty`.
- Expiry of stale `New` commands (D15): a pure timestamp comparison against a
  build-time `MAX_COMMAND_AGE` (default 1000 ms), checked before the grid.

## Not implemented yet

| Item | Where it is specified |
| --- | --- |
| End-of-day cancel of all resting orders (`DayClose`, reason `END_OF_DAY`) | D22 |
| Top-three-level snapshot with `last_price` | D6 |
| Loading tick/lot per symbol from the mounted config file | D23. `SymbolSpec` is built by hand; see the TODO in `src/types.rs` |
| Unknown-symbol rejection | Connection layer, one engine instance per symbol |
| Event journal (SQLite), journal thread, replay and output comparison | D1, D9–D12, D19, D20 |
| WebSocket channel to D, resume handshake, backpressure | D13, D16, D21 |
| Market-data publisher thread, lock-free queue, heartbeat | D14 |
| `/healthz`, `/metrics`, container image, compose snippet | D23 |
| Bots and load tests | Separate image |

## Points to be aware of

- A `Rejected(SelfTradePrevented)` event can carry a non-zero `filled`.
  Consumers must not treat every `Rejected` as "nothing traded".
- Reusing an `order_id` for the same user within a day is undefined upstream
  (D2). The engine does not detect it; a cancel by id targets the latest
  resting order.
- `MAX_COMMAND_AGE` is fixed at build time, so changing it means rebuilding the
  image. The value must be kept consistent with D's command timeout (D15).
- The engine does not check that `seq` values are fresh or increasing; that
  is the journal's guarantee.
- `main.rs` is still a placeholder.
