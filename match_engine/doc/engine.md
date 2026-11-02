# Matching engine

The engine is one order book for one trading pair. It is pure computation: no
I/O, no clock, no randomness, and it never assigns sequence numbers. The same
sequence of `(seq, command)` pairs always produces the same outputs, which is
what lets B rebuild its state by replaying the journal (D1, D10, D18).

## Layout

```
src/
├── lib.rs      public exports
├── types.rs    ids, Order, commands, OrderEvent / Trade / Output, SymbolSpec
├── book.rs     OrderBook: price levels, matching, resting, removal
├── engine.rs   MatchEngine: validation, command dispatch, output queue
└── main.rs     placeholder binary
```

## Public API

```rust
let mut engine = MatchEngine::new(SymbolSpec::new(tick, lot));
engine.process(seq, ts, Command::New(NewOrder { order_id, user_id, side, price, qty, sent_at }));
engine.process(seq, ts, Command::Cancel(CancelTarget::ById { user_id, order_id }));
engine.process(seq, ts, Command::Cancel(CancelTarget::BySeq(order_seq)));
engine.process(seq, ts, Command::DayClose);
for output in engine.drain_outputs() { /* Output::Order(..) | Output::Trade(..) */ }
```

- `seq` is the journal's day-scoped sequence number for the command. For a
  `New` command it becomes the order's identity everywhere (events, trades,
  cancel-by-seq). The engine does not check that `seq` is fresh.
- `ts` is the journal's receive time for the command (Unix ms). The engine
  uses it only to compare with a `New` command's `sent_at`; it never reads a
  clock (see Expiry).
- `price: None` is a market order.
- `outputs()` peeks at the queue; `drain_outputs()` hands it over and empties it.

## Types

| Type | Meaning |
| --- | --- |
| `Price`, `Qty` | `i64`. No floating point anywhere (D5) |
| `Timestamp` | `i64`, Unix milliseconds |
| `Seq`, `OrderId`, `UserId` | `u64`. `OrderId` is chosen by the caller and unique per user per day (D2) |
| `SymbolSpec { tick, lot }` | The pair's grid. `new` panics unless both are positive; `UNIT` is 1/1 for tests |
| `Order` | An incoming or resting order. `qty` is the remaining quantity, `filled` the executed quantity |

## Commands

| Command | Fields |
| --- | --- |
| `New(NewOrder)` | `order_id`, `user_id`, `side`, `price: Option<Price>`, `qty`, `sent_at` (when D issued it) |
| `Cancel(ById { user_id, order_id })` | target named by the caller's ids |
| `Cancel(BySeq(seq))` | target named by the order's seq |
| `DayClose` | no fields. End of the trading day, see below |

## Outputs

`Output::Order(OrderEvent)` and `Output::Trade(Trade)`, without the journal's
own `seq` / `prev_seq`; the journal thread adds those when it writes them (D10).

**`OrderEvent`**

| Field | Notes |
| --- | --- |
| `order_seq`, `order_id`, `user_id` | `Option`s. Always set for a live order; for a rejected cancel only the ids the cancel named are set |
| `status` | `Accepted`, `PartiallyFilled`, `Filled`, `Cancelled`, `Rejected` |
| `filled`, `remaining` | Cumulative executed quantity and quantity still open |
| `reason` | Set on rejections, see below |

**`Trade`**: `price`, `qty`, `taker_seq`, `maker_seq`, `taker_user_id`,
`maker_user_id`. A trade has no id of its own; `(taker_seq, maker_seq)`
identifies it (D3).

**Reasons**

| Reason | When |
| --- | --- |
| `Expired` | The command's age (`ts - sent_at`) exceeds `MAX_COMMAND_AGE` |
| `EndOfDay` | A resting order cancelled at close (`Cancelled`), or a new order that arrived after close (`Rejected`, produced by the journal layer) |
| `InvalidPrice` | Limit price is not positive or not a multiple of the tick |
| `InvalidQty` | Quantity is not positive or not a multiple of the lot |
| `SelfTradePrevented` | Matching reached a resting order of the same user |
| `OrderNotFound` | Cancel of an order that is not resting: unknown, filled, cancelled or never accepted |

## Processing a new order

1. **Check expiry.** If `ts - sent_at > MAX_COMMAND_AGE`, emit one
   `Rejected(Expired)` and stop.
2. **Validate the grid.** Price first (limit orders only), then quantity. A
   failure emits one `Rejected` event and stops; the order is not accepted and
   never touches the book.
3. **Emit `Accepted`** with `filled = 0`, `remaining = qty`.
4. **Match** against the opposite side in price-time priority (best price
   first, oldest first within a price). Each match emits, in this order:
   `Trade`, the taker's `PartiallyFilled`/`Filled`, then the maker's.
   Trades execute at the resting order's price.
5. **Stop conditions and the remainder:**

   | Why matching stopped | Remainder |
   | --- | --- |
   | Fully filled | none |
   | Next order in line belongs to the same user | `Rejected(SelfTradePrevented)`; the resting order is left alone |
   | Limit order, no more crossing orders | rests at its own price |
   | Market order, no more liquidity | `Cancelled`; a market order never rests |

   Fills against other users before a self-trade stop stand, so an aggressive
   order can be partially filled and its remainder then rejected. In that
   case the `Rejected` event carries the non-zero `filled`.

A market order is matched as a limit order at a price that crosses everything.

## Expiry (D15)

A `New` command that sat too long between D sending it and B journaling it is
refused, so a late arrival of an order that D already gave up on cannot trade.

- Rule: expired when `ts - sent_at > MAX_COMMAND_AGE`. An age exactly equal to
  the threshold is accepted; a `sent_at` after `ts` (clock skew) gives a
  negative age and is accepted. The subtraction saturates, so extreme values
  cannot overflow.
- It is a pure comparison of two numbers carried by the command. There is no
  timer, wait or clock read anywhere on the hot path, so replay reproduces the
  decision exactly.
- It is checked before the tick/lot grid, so an old command is reported as
  `Expired` whatever else is wrong with it.
- An expired order gets only the `Rejected` event: no `Accepted`, not on the
  book, unknown to later cancels.
- Applies to market orders too. Cancels carry no `sent_at` and are never expired.
- `MAX_COMMAND_AGE` is a compile-time constant in milliseconds. Set the
  environment variable when building, for example
  `MAX_COMMAND_AGE=500 cargo build --release`; the default is 1000. A value
  that is not a non-negative integer fails the build. It must not exceed D's
  own command timeout.

## End of day (D22)

`DayClose` is a record the journal writes itself, not a command from D. It is
a normal input of the engine, so replay reproduces the close exactly.

1. Every resting order is cancelled in ascending order `seq` (not by price or
   side). Each one emits `Cancelled` with `reason = EndOfDay` and its
   cumulative `filled` and removed `remaining`.
2. The book is then reset completely: no orders, levels or index entries, and
   no retained slab storage. The engine and the process keep running; nothing
   needs restarting.
3. After the reset the engine behaves like a fresh one. The next day's `seq`
   may restart from 1 and ids may be reused.

A `DayClose` on an empty book, or a second one in a row, emits nothing.

What the engine does **not** do, because it belongs to the journal layer
(not built yet):

- deciding that the day is over: the session is 09:30 to 16:00 in Dublin time
  (Europe/Dublin), judged from the epoch-millisecond timestamp. Weekends and
  holidays are not handled at this layer. When the time is reached the journal
  writes `DayClose` first, then lets the engine cancel;
- rejecting new orders that arrive after the close with `Rejected(EndOfDay)`;
- switching to the next day's event store and resetting `seq`.

## Market-data snapshot (D6)

`engine.snapshot()` returns a `Snapshot` (cheap, `Copy`, comparable):

| Field | Meaning |
| --- | --- |
| `bids`, `asks` | Best three levels per side, best first. Each is `{price, qty}` with `qty` the total remaining quantity at that price; missing levels are `None` |
| `last_price` | Price of the most recent trade today; `None` before the first trade |
| `volume` | Total quantity traded today, in size units, not currency |
| `last_ms` | Unix ms at which this view was generated (see below) |

- Level quantities are kept as running totals per price level, updated on
  every rest, fill and removal, so taking a snapshot does not walk the order
  lists.
- `last_price` and `volume` are reset by `DayClose` together with the book.
- **`last_ms`** is the journal `ts` of the command that last changed the view
  (a rest, a trade, a removal or a day close). To stay strictly increasing it
  is `max(ts, previous + 1)`, so commands in the same millisecond, or a clock
  that steps back, still give a larger value. Commands that change nothing
  (rejections, unknown cancels) do not generate a snapshot and leave it
  alone. It is 0 before the first change and does not rewind at `DayClose`.
  It comes from the commands' timestamps, so the engine still reads no clock
  and the value is reproducible on replay.
- `Snapshot::same_view` compares everything except `last_ms`; a publisher that
  skips unchanged snapshots uses it.
- `Snapshot::into_market_data()` gives the wire message (`MarketData`, JSON):
  `bids` and `asks` as lists of `{price, qty}` with only the levels that
  exist, `last_price` (null if none), `volume`, `last_ms`.

Delivering snapshots (the bounded lock-free queue, the publisher thread,
rate limiting, heartbeat, the `/md/<symbol>` WebSocket) is not part of the
engine; see `status.md`.

## Cancels

- `ById` resolves through a `(user_id, order_id)` index that holds **resting
  orders only**. If a caller reuses an id within a day (not allowed upstream,
  D2), the latest resting order wins.
- A successful cancel emits `Cancelled` with the cumulative `filled` and the
  `remaining` quantity that was removed. Only the remaining quantity is
  removed; earlier fills stand.
- A cancel that finds nothing emits `Rejected(OrderNotFound)` and leaves the
  book unchanged. Finished and never-seen orders are not distinguished, which
  keeps the engine's memory proportional to the resting orders (D7).

## Order book

```
OrderBook
 ├─ bids: BTreeMap<Price, Level>   best bid = last key
 ├─ asks: BTreeMap<Price, Level>   best ask = first key
 ├─ slab: Vec<Node>                orders, each with prev/next links
 ├─ free: Vec<u32>                 reusable slab slots
 ├─ index: HashMap<Seq, u32>       seq -> slab slot
 └─ by_key: HashMap<(UserId, OrderId), Seq>

Level { head, tail }               orders at one price, oldest first
```

- Orders at a price form a doubly linked list through the slab, so a cancel
  unlinks in O(1) without disturbing the priority of other orders. A level
  is removed from the map when it empties.
- A partially filled resting order keeps its place in the queue.
- Looking up the price level during a cancel costs O(log levels).
- There is no `unsafe` code.

## Determinism

- Output depends only on the ordered commands and the `SymbolSpec`.
- Iteration order never leaks into outputs: matching walks the `BTreeMap` and
  the linked lists, and the hash maps are only used for point lookups.
- Replaying the same commands yields identical outputs (covered by a test).
