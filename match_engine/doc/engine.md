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
engine.process(seq, Command::New(NewOrder { order_id, user_id, side, price, qty }));
engine.process(seq, Command::Cancel(CancelTarget::ById { user_id, order_id }));
engine.process(seq, Command::Cancel(CancelTarget::BySeq(order_seq)));
for output in engine.drain_outputs() { /* Output::Order(..) | Output::Trade(..) */ }
```

- `seq` is the journal's day-scoped sequence number for the command. For a
  `New` command it becomes the order's identity everywhere (events, trades,
  cancel-by-seq). The engine does not check that `seq` is fresh.
- `price: None` is a market order.
- `outputs()` peeks at the queue; `drain_outputs()` hands it over and empties it.

## Types

| Type | Meaning |
| --- | --- |
| `Price`, `Qty` | `i64`. No floating point anywhere (D5) |
| `Seq`, `OrderId`, `UserId` | `u64`. `OrderId` is chosen by the caller and unique per user per day (D2) |
| `SymbolSpec { tick, lot }` | The pair's grid. `new` panics unless both are positive; `UNIT` is 1/1 for tests |
| `Order` | An incoming or resting order. `qty` is the remaining quantity, `filled` the executed quantity |

## Commands

| Command | Fields |
| --- | --- |
| `New(NewOrder)` | `order_id`, `user_id`, `side`, `price: Option<Price>`, `qty` |
| `Cancel(ById { user_id, order_id })` | target named by the caller's ids |
| `Cancel(BySeq(seq))` | target named by the order's seq |

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
| `InvalidPrice` | Limit price is not positive or not a multiple of the tick |
| `InvalidQty` | Quantity is not positive or not a multiple of the lot |
| `SelfTradePrevented` | Matching reached a resting order of the same user |
| `OrderNotFound` | Cancel of an order that is not resting: unknown, filled, cancelled or never accepted |

## Processing a new order

1. **Validate the grid.** Price first (limit orders only), then quantity. A
   failure emits one `Rejected` event and stops; the order is not accepted and
   never touches the book.
2. **Emit `Accepted`** with `filled = 0`, `remaining = qty`.
3. **Match** against the opposite side in price-time priority (best price
   first, oldest first within a price). Each match emits, in this order:
   `Trade`, the taker's `PartiallyFilled`/`Filled`, then the maker's.
   Trades execute at the resting order's price.
4. **Stop conditions and the remainder:**

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
