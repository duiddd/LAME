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
