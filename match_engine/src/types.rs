/// Price as an integer number of ticks (D5). No floating point anywhere.
pub type Price = i64;
pub type Qty = i64;
/// Caller-chosen order id, unique per user per trading day (D2).
pub type OrderId = u64;
pub type UserId = u64;
/// Day-scoped sequence number assigned by the journal (D1). An order is
/// identified by the `seq` of its `NewOrder` record.
pub type Seq = u64;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    Buy,
    Sell,
}

/// A resting or incoming limit order. `qty` is the remaining quantity.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Order {
    pub seq: Seq,
    pub order_id: OrderId,
    pub user_id: UserId,
    pub side: Side,
    pub price: Price,
    pub qty: Qty,
    pub filled: Qty,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NewOrder {
    pub order_id: OrderId,
    pub user_id: UserId,
    pub side: Side,
    pub price: Price,
    pub qty: Qty,
}

/// A cancel names its target either by `(user_id, order_id)` or by order seq.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CancelTarget {
    ById { user_id: UserId, order_id: OrderId },
    BySeq(Seq),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Command {
    New(NewOrder),
    Cancel(CancelTarget),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Accepted,
    PartiallyFilled,
    Filled,
    Cancelled,
    Rejected,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reason {
    /// Cancel of an order that is not resting: unknown, filled or cancelled (D7).
    OrderNotFound,
}

/// The outcome for one order. For a rejected cancel only the ids the cancel
/// itself named are set.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OrderEvent {
    pub order_seq: Option<Seq>,
    pub order_id: Option<OrderId>,
    pub user_id: Option<UserId>,
    pub status: Status,
    pub filled: Qty,
    pub remaining: Qty,
    pub reason: Option<Reason>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Trade {
    pub price: Price,
    pub qty: Qty,
    pub taker_seq: Seq,
    pub maker_seq: Seq,
    pub taker_user_id: UserId,
    pub maker_user_id: UserId,
}

/// Engine output, without the journal's own `seq` / `prev_seq` (D10). For one
/// match the order is: `Trade`, taker `OrderEvent`, maker `OrderEvent`; a new
/// order's `Accepted` always comes first.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Output {
    Order(OrderEvent),
    Trade(Trade),
}
