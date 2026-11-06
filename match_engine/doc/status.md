# Status

## Implemented

**Matching core** (`engine.md`)

- Price-time priority matching for limit and market orders, one book per pair.
- Journal-assigned `seq` as the order identity; `user_id` and `order_id`
  passed through to every event and trade.
- Full `OrderEvent` lifecycle: `Accepted`, `PartiallyFilled`, `Filled`,
  `Cancelled`, `Rejected` with reasons.
- Cancel by `(user_id, order_id)` or by seq; `OrderNotFound` when nothing rests.
- Self-trade prevention (D24): matching stops at the user's own resting order
  and the remainder is rejected.
- Market orders never rest; leftover quantity is cancelled.
- Tick/lot grid validation (D5) with `InvalidPrice` / `InvalidQty`.
- Expiry of stale `New` commands (D15): a pure timestamp comparison against a
  build-time `MAX_COMMAND_AGE` (default 1000 ms).
- Session check: new orders outside 00:00 to 23:00 Dublin time are rejected as
  `EndOfDay` (D22).
- End-of-day `DayClose`: cancels all resting orders in `seq` order, then fully
  resets the book.
- Market-data snapshot (D6): best three levels, `last_price`, `volume`,
  `last_ms` generated from the command `ts`.

**Service** (`runtime.md`)

- Connection task on `/d` with the resume handshake, resend and live delivery,
  one connection at a time, a bounded outbound queue that closes the connection
  when full, ping and idle timeout.
- Journal thread: sole writer of the event store, assigns `ts` and `seq`,
  commits before delivering, batches, day close, day rollover, stops the
  process on a write failure.
- Event store: one SQLite database per day, WAL, lock file.
- One engine task per pair.
- Market-data publisher on `/md/<pair>`: current view on connect, 1 ms pacing,
  skip unchanged, heartbeat.
- Control port with `/healthz`, `/metrics` and an IP allow-list;
  `--healthcheck` subcommand.
- End-of-day timer at 23:00 Dublin time.
- Start-up recovery: replay, per-pair comparison with stored outputs, writing
  outputs a crash left unwritten, refusing to start on any disagreement. Nothing
  is served until it finishes.
- `SIGTERM` / `SIGINT`: orderly stop.

## Not implemented yet

| Item | Where it is specified |
| --- | --- |
| Reading the pairs and their tick/lot from the mounted config file. **Temporary arrangement:** they come from the `SYMBOLS` environment variable (`NAME:tick:lot,...`), see `runtime.md`. Only `Config::from_lookup` changes when the file arrives | D23. The file format is not decided |
| Dockerfile, compose snippet, read-only root file system, non-root user, resource limits, log rotation | D23 |
| Bots and load tests | Separate image |
| A `DayEnd` record after the end-of-day cancels | Open item in the decision record |
| Weekends and holidays | Deliberately not handled at this layer |

## Points to be aware of

- A `Rejected(SelfTradePrevented)` event can carry a non-zero `filled`.
  Consumers must not treat every `Rejected` as "nothing traded".
- Reusing an `order_id` for the same user within a day is undefined upstream
  (D2). The engine does not detect it; a cancel by id targets the latest
  resting order.
- The engine does not check that `seq` values are fresh or increasing; that
  is the journal's guarantee.
- `MAX_COMMAND_AGE` is fixed at build time, so changing it means rebuilding the
  image. The value must be kept consistent with D's command timeout (D15).
- Heartbeat (D6): the market-data heartbeat re-sends the last message with the
  **same** `last_ms`, because the engine only produces a new `last_ms` when the
  view changes. Clients must therefore drop only snapshots that are strictly
  older than the newest they hold, and may use an equal one as a liveness signal.
  The decision record says "not newer"; that wording needs to change.
- The `END_OF_DAY` rejection of orders outside the session is made by the
  engine from the command timestamp, not by the journal, so that it stays in the
  pair's own stream of outputs in the same order on replay.
- Frames that cannot become commands (not JSON, unknown pair) get an `error`
  reply and are not journaled.
- The market-data hand-over is a `watch` cell, not the lock-free queue named in
  D14. The effect is the same and the subscriber side can await it.
- If the timer never closed a day, the next day's first command closes it, and
  its cancels are still written into the old day's store.
- A mismatch at start-up stops the process with an error; a container restart
  policy will keep restarting it until someone looks.
- The binary has not been run inside a container yet.
