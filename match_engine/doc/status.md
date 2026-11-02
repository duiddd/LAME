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
- Market-data snapshot (D6): best three levels with aggregated quantity,
  `last_price`, `volume`, and `last_ms` generated from the command `ts`.
- End-of-day `DayClose`: cancels all resting orders in `seq` order with
  `EndOfDay`, then fully resets the book (D22).
- Expiry of stale `New` commands (D15): a pure timestamp comparison against a
  build-time `MAX_COMMAND_AGE` (default 1000 ms), checked before the grid.

## Not implemented yet

| Item | Where it is specified |
| --- | --- |
| Journal side of end of day: judging 09:30-16:00 from the epoch timestamp, writing `DayClose`, rejecting later orders with `EndOfDay`, switching to the next day's store | D22, journal layer |
| Delivering snapshots: bounded lock-free queue from the engine task, publisher thread, 1 ms rate limit, skip-unchanged, heartbeat, `/md/<symbol>` WebSocket, no publishing during recovery | D6, D14 |
| Loading tick/lot per symbol from the mounted config file | D23. `SymbolSpec` is built by hand; see the TODO in `src/types.rs` |
| Unknown-symbol rejection | Connection layer, one engine instance per symbol |
| Event journal (SQLite), journal thread, replay and output comparison | D1, D9–D12, D19, D20 |
| WebSocket channel to D, resume handshake, backpressure | D13, D16, D21 |
| Market-data publisher thread, lock-free queue, heartbeat | D14 |
| `/healthz`, `/metrics`, container image, compose snippet | D23 |
| Bots and load tests | Separate image |

## Points to be aware of

- Heartbeat (D6): a re-sent snapshot must carry a larger `last_ms` or clients
  will drop it as not newer. The engine only produces a new `last_ms` when the
  view changes, so the heartbeat either takes a fresh timestamp from the
  publisher's clock, or clients drop only strictly older snapshots. Undecided.

- The trading session is 09:30 to 16:00 in Dublin time (Europe/Dublin), judged
  from the epoch-millisecond timestamp by the journal layer, which is not
  built yet. Weekends and holidays are not handled at this layer.
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
