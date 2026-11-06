use std::collections::{BTreeMap, HashMap};

use crate::types::{
    BookLevel, Order, OrderEvent, OrderId, Output, Price, Qty, Reason, Seq, Side, Snapshot, Status,
    Trade, UserId, DEPTH,
};

const NIL: u32 = u32::MAX;

/// Orders live in a slab and are chained per price level in arrival order, so
/// a cancel unlinks in O(1) without invalidating other positions.
struct Node {
    order: Order,
    prev: u32,
    next: u32,
}

#[derive(Clone, Copy)]
struct Level {
    head: u32,
    tail: u32,
    /// Sum of the remaining quantity of the orders at this price, kept up to
    /// date on every rest, fill and removal.
    total_qty: Qty,
}

#[derive(Default)]
pub struct OrderBook {
    // Both sides ascend by price: best bid is the last key, best ask the first.
    bids: BTreeMap<Price, Level>,
    asks: BTreeMap<Price, Level>,
    slab: Vec<Node>,
    free: Vec<u32>,
    index: HashMap<Seq, u32>,
    /// `(user_id, order_id)` -> seq, resting orders only (D2). Callers must not
    /// reuse an id within a day; if they do, the latest resting order wins.
    by_key: HashMap<(UserId, OrderId), Seq>,
    /// Price of the most recent trade, `None` before the first one.
    last_price: Option<Price>,
    /// Total quantity traded since the book was last reset.
    volume: Qty,
    /// Set whenever the market view may have changed; cleared by `take_dirty`.
    dirty: bool,
}

impl OrderBook {
    pub fn new() -> Self {
        Self::default()
    }

    /// Best `DEPTH` levels per side with their aggregated quantity, plus the
    /// last trade price and the traded volume.
    pub fn snapshot(&self) -> Snapshot {
        fn level((price, l): (&Price, &Level)) -> BookLevel {
            BookLevel {
                price: *price,
                qty: l.total_qty,
            }
        }
        let mut bids = [None; DEPTH];
        for (slot, entry) in bids.iter_mut().zip(self.bids.iter().rev()) {
            *slot = Some(level(entry));
        }
        let mut asks = [None; DEPTH];
        for (slot, entry) in asks.iter_mut().zip(self.asks.iter()) {
            *slot = Some(level(entry));
        }
        Snapshot {
            bids,
            asks,
            last_price: self.last_price,
            volume: self.volume,
            last_ms: 0,
        }
    }

    /// Returns whether the market view may have changed since the last call.
    pub fn take_dirty(&mut self) -> bool {
        std::mem::take(&mut self.dirty)
    }

    pub fn contains(&self, seq: Seq) -> bool {
        self.index.contains_key(&seq)
    }

    /// Matches `order` against the opposite side. What happens to the
    /// remainder depends on why matching stopped:
    /// - it met a resting order of the same user: the remainder is rejected
    ///   (`SelfTradePrevented`) and the resting order is left alone (D24);
    /// - a limit order's remainder rests at its own price;
    /// - a market order's remainder is cancelled, it never rests.
    ///
    /// `order.seq` must not already be in the book.
    pub fn add(&mut self, mut order: Order, out: &mut Vec<Output>) {
        let self_trade = self.match_incoming(&mut order, out);
        if self_trade {
            out.push(Output::Order(OrderEvent {
                status: Status::Rejected,
                reason: Some(Reason::SelfTradePrevented),
                ..unfilled_event(&order)
            }));
        } else if order.qty > 0 {
            if order.market {
                out.push(Output::Order(OrderEvent {
                    status: Status::Cancelled,
                    ..unfilled_event(&order)
                }));
            } else {
                self.rest(order);
            }
        }
    }

    /// Cancels every resting order in ascending `seq` order, emitting
    /// `Cancelled` with `EndOfDay` for each, then resets the book completely:
    /// no orders, no levels, no index entries, no retained slab storage.
    pub fn close_day(&mut self, out: &mut Vec<Output>) {
        let mut seqs: Vec<Seq> = self.index.keys().copied().collect();
        seqs.sort_unstable();
        for seq in seqs {
            if let Some(o) = self.remove(seq) {
                out.push(Output::Order(OrderEvent {
                    status: Status::Cancelled,
                    reason: Some(Reason::EndOfDay),
                    ..unfilled_event(&o)
                }));
            }
        }
        *self = OrderBook::default();
        self.dirty = true;
    }

    /// Removes a resting order by `(user_id, order_id)`.
    pub fn remove_by_key(&mut self, user_id: UserId, order_id: OrderId) -> Option<Order> {
        let seq = *self.by_key.get(&(user_id, order_id))?;
        self.remove(seq)
    }

    /// Removes a resting order and returns it, or `None` if `seq` is not in
    /// the book.
    pub fn remove(&mut self, seq: Seq) -> Option<Order> {
        let idx = self.index.remove(&seq)?;
        let removed = self.slab[idx as usize].order;
        self.unindex_key(&removed);
        self.dirty = true;
        let (side, price) = (removed.side, removed.price);
        let levels = match side {
            Side::Buy => &mut self.bids,
            Side::Sell => &mut self.asks,
        };
        let mut level = levels[&price];
        level.total_qty -= removed.qty;
        let (prev, next) = {
            let n = &self.slab[idx as usize];
            (n.prev, n.next)
        };
        if prev == NIL {
            level.head = next;
        } else {
            self.slab[prev as usize].next = next;
        }
        if next == NIL {
            level.tail = prev;
        } else {
            self.slab[next as usize].prev = prev;
        }
        if level.head == NIL {
            levels.remove(&price);
        } else {
            levels.insert(price, level);
        }
        self.free.push(idx);
        Some(removed)
    }

    fn unindex_key(&mut self, o: &Order) {
        let key = (o.user_id, o.order_id);
        if self.by_key.get(&key) == Some(&o.seq) {
            self.by_key.remove(&key);
        }
    }

    fn best_opposite(&self, taker: Side) -> Option<(Price, Level)> {
        let entry = match taker {
            Side::Buy => self.asks.first_key_value(),
            Side::Sell => self.bids.last_key_value(),
        };
        entry.map(|(p, l)| (*p, *l))
    }

    /// Returns true if matching stopped at a resting order of the same user.
    fn match_incoming(&mut self, order: &mut Order, out: &mut Vec<Output>) -> bool {
        let mut self_trade = false;
        while order.qty > 0 {
            let Some((price, mut level)) = self.best_opposite(order.side) else {
                break;
            };
            let crosses = match order.side {
                Side::Buy => price <= order.price,
                Side::Sell => price >= order.price,
            };
            if !crosses {
                break;
            }

            while order.qty > 0 && level.head != NIL {
                let idx = level.head;
                if self.slab[idx as usize].order.user_id == order.user_id {
                    self_trade = true;
                    break;
                }
                let node = &mut self.slab[idx as usize];
                let traded = order.qty.min(node.order.qty);
                order.qty -= traded;
                order.filled += traded;
                node.order.qty -= traded;
                node.order.filled += traded;
                level.total_qty -= traded;
                self.last_price = Some(price);
                self.volume = self.volume.saturating_add(traded);
                self.dirty = true;

                out.push(Output::Trade(Trade {
                    price,
                    qty: traded,
                    taker_seq: order.seq,
                    maker_seq: node.order.seq,
                    taker_user_id: order.user_id,
                    maker_user_id: node.order.user_id,
                }));
                out.push(Output::Order(fill_event(order)));
                out.push(Output::Order(fill_event(&node.order)));
                let resting_seq = node.order.seq;
                let resting_qty = node.order.qty;
                let removed_order = node.order;

                if resting_qty == 0 {
                    let next = node.next;
                    level.head = next;
                    if next == NIL {
                        level.tail = NIL;
                    } else {
                        self.slab[next as usize].prev = NIL;
                    }
                    self.index.remove(&resting_seq);
                    self.unindex_key(&removed_order);
                    self.free.push(idx);
                }
            }

            let levels = match order.side {
                Side::Buy => &mut self.asks,
                Side::Sell => &mut self.bids,
            };
            if level.head == NIL {
                levels.remove(&price);
            } else {
                levels.insert(price, level);
            }
            if self_trade {
                break;
            }
        }
        self_trade
    }

    fn rest(&mut self, order: Order) {
        let node = Node {
            order,
            prev: NIL,
            next: NIL,
        };
        let idx = match self.free.pop() {
            Some(i) => {
                self.slab[i as usize] = node;
                i
            }
            None => {
                self.slab.push(node);
                (self.slab.len() - 1) as u32
            }
        };
        let levels = match order.side {
            Side::Buy => &mut self.bids,
            Side::Sell => &mut self.asks,
        };
        let level = levels.entry(order.price).or_insert(Level {
            head: NIL,
            tail: NIL,
            total_qty: 0,
        });
        level.total_qty += order.qty;
        if level.tail == NIL {
            level.head = idx;
        } else {
            self.slab[level.tail as usize].next = idx;
            self.slab[idx as usize].prev = level.tail;
        }
        level.tail = idx;
        self.dirty = true;
        self.index.insert(order.seq, idx);
        self.by_key
            .insert((order.user_id, order.order_id), order.seq);
    }
}

/// `PartiallyFilled` or `Filled` for an order that just traded.
fn fill_event(o: &Order) -> OrderEvent {
    OrderEvent {
        order_seq: Some(o.seq),
        order_id: Some(o.order_id),
        user_id: Some(o.user_id),
        status: if o.qty == 0 {
            Status::Filled
        } else {
            Status::PartiallyFilled
        },
        filled: o.filled,
        remaining: o.qty,
        reason: None,
    }
}

/// Event for an incoming order that stops matching with quantity left over.
fn unfilled_event(o: &Order) -> OrderEvent {
    OrderEvent {
        order_seq: Some(o.seq),
        order_id: Some(o.order_id),
        user_id: Some(o.user_id),
        status: Status::Cancelled,
        filled: o.filled,
        remaining: o.qty,
        reason: None,
    }
}
