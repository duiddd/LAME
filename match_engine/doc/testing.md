# Testing

```bash
cargo test                                    # everything except the dataset run
cargo clippy --all-targets -- -D warnings     # lint gate
cargo fmt --check
```

## Suites

| File | Covers |
| --- | --- |
| `tests/matching.rs` | Price-time priority on both sides, FIFO at one price, partial fills keeping priority, remainders resting at their own price, cancel at head/middle/tail/last-of-level, cancel after partial fill, finished orders unavailable |
| `tests/order_events.rs` | `Accepted` first, event order for a match, cumulative `filled`, cancel by `(user_id, order_id)` and by seq, same `order_id` for different users, `OrderNotFound` for unknown and finished orders, rejected cancel leaves the book alone, replay determinism |
| `tests/self_trade.rs` | Aggressive limit and market orders partially filled then rejected at the user's own order (both sides), rejection with no fill, own order out of price range, same-side own orders, other users unaffected, market remainder cancelled when liquidity runs out |
| `tests/validation.rs` | On-grid acceptance, price off tick, non-positive price, quantity off lot, non-positive quantity, market order checks quantity only, price reported before quantity, rejected order never reaches the book, invalid `SymbolSpec` panics |
| `tests/expiry.rs` | Age exactly at the threshold accepted, one past it expired, fresh orders, `sent_at` after receipt not expired, extreme timestamps without overflow, market orders expire, expiry checked before the grid, expired order neither trades nor rests, cancels never expire, replay determinism |
| `tests/day_close.rs` | Cancels in `seq` order regardless of price/side, partially filled orders report `filled`/`remaining`, empty book emits nothing, full reset (old orders unreachable on both sides), engine reusable with `seq` restarting at 1, repeated close, replay determinism |
| `tests/snapshot.rs` | Empty book, fewer than three levels, best three only and best first, per-price aggregation, partial fills, exhausted levels, sweeps, `volume` in size units, cancels at head/middle/tail, rejections leaving the view untouched, day-close reset, `last_ms` equal to the command `ts`, strictly increasing for equal or backwards `ts`, unchanged by rejected commands, determinism, JSON shape |
| `tests/session.rs` | Daylight-saving rules for several years, the 09:30 to 16:00 window in winter and summer, day ids across local midnight, the next close across clock changes |
| `tests/pipeline.rs` | The service end to end over a real WebSocket: a trade, commands and outputs in one total order in the store, resume from the start and the middle, outputs written with D disconnected, a new connection replacing the old, bad frames, handshake errors, cancel by id and seq, independent pairs with their own grid, expiry from the journal timestamp, shutdown writing everything queued |
| `tests/market_data.rs` | Current view on connect, snapshots with volume and generation time, separate streams per pair, best three levels, `last_ms` ordering, unknown pair refused, heartbeat, a subscriber that never reads |
| `tests/http.rs` | `/healthz`, nothing served while recovering, unhealthy once a stop begins, metrics, the allow-list, the `--healthcheck` logic, the outbound queue closing the connection when full |
| `tests/end_of_day.rs` | The timer's delay, cancels in `seq` order at the close, rejection after the close and before the open, a command closing the day without the timer, one `DayClose` only, rollover to the next day's store, resume covering only the current day |
| `tests/recovery.rs` | Restart restoring the book, numbering and market data; outputs a crash left unwritten; each kind of disagreement refusing to start; a second instance refused; start after the close; a day never closed; commands waiting for the next day keeping their order |
| `tests/resend.rs` | A long resend (12 000 records through a queue of 8) with D not reading: the connection survives, everything arrives, new orders, trades and cancels are handled while the resend is in progress |
| `tests/process.rs` | The real binary: `SIGTERM`, `SIGKILL` then restart, `--healthcheck`, refusing to start twice or without a symbol list |
| `tests/csv_protocol.rs` | The CSV request parser and output formatter used by the test adapter |
| `tests/datasets.rs` | Dataset regression, see below |

## Test adapter

`tests/common/mod.rs` is test-only code and is not part of the service. It
parses a line format (`0,id,side,qty,price` to add, `1,id` to cancel, prices
with up to four decimals) and drives the engine through `Harness`:

- sequence numbers are handed out in arrival order;
- each order's `user_id` equals its `order_id`, so no two orders of the same
  user ever meet;
- `drain_lines` reduces engine outputs to three line events (`Trade`, `Fill`,
  `PartialFill`), which is what the matching and dataset tests assert on.

## Dataset regression

`tests/datasets.rs` replays large CSV datasets and checks the number of
events and trades against each dataset's `manifest.json`. The CSV files are
too large to commit, so point the test at a directory that holds them:

```bash
LAME_DATASET_DIR=/path/to/datasets cargo test --release --test datasets -- --nocapture
```

Without `LAME_DATASET_DIR` the test prints a message and passes. The
`--nocapture` output also reports events, trades and elapsed time per file.
In release mode the 10-million-line mixed dataset takes a few seconds on a
laptop; that figure includes reading and parsing the file, so it is not a
per-order latency.

The service tests move a fake clock by hand (`TestClock` in
`tests/support/mod.rs`) and wait only by awaiting messages. The two exceptions
are the market-data heartbeat test, which shortens the heartbeat to 30 ms, and
`tests/process.rs`, which runs the real binary on the real clock and so counts
records without depending on whether an order was inside the session.
