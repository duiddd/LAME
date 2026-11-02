/// Price as an integer number of ticks (D5). No floating point anywhere.
pub type Price = i64;
pub type Qty = i64;
/// Caller-chosen order id, unique per user per trading day (D2).
pub type OrderId = u64;
pub type UserId = u64;
/// Day-scoped sequence number assigned by the journal (D1). An order is
/// identified by the `seq` of its `NewOrder` record.
pub type Seq = u64;
/// Unix time in milliseconds. Taken from the command, never from a clock.
pub type Timestamp = i64;

/// Grid of one trading pair: prices are multiples of `tick`, quantities are
/// multiples of `lot` (D5).
///
/// TODO: load tick/lot per symbol from the mounted config file (D23). The file
/// format is not decided yet, so callers build this by hand for now.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SymbolSpec {
    pub tick: Price,
    pub lot: Qty,
}

impl SymbolSpec {
    /// Panics if `tick` or `lot` is not positive: a bad config must stop
    /// start-up, not reach the matcher.
    pub fn new(tick: Price, lot: Qty) -> Self {
        assert!(tick > 0 && lot > 0, "tick and lot must be positive");
        Self { tick, lot }
    }

    /// Every integer price and quantity is on the grid.
    pub const UNIT: SymbolSpec = SymbolSpec { tick: 1, lot: 1 };
}

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
    /// A market order has no limit price (its `price` is a sentinel that
    /// crosses everything) and never rests.
    pub market: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NewOrder {
    pub order_id: OrderId,
    pub user_id: UserId,
    pub side: Side,
    /// `None` is a market order.
    pub price: Option<Price>,
    pub qty: Qty,
    /// When the sender (D) issued the command (D15).
    pub sent_at: Timestamp,
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
    /// End of the trading day (D22). Written to the journal by B itself, not
    /// sent by D. Cancels every resting order, then resets the book.
    DayClose,
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
    /// The order met a resting order of the same user; its remainder is
    /// rejected instead of trading (D24).
    SelfTradePrevented,
    /// The command waited longer than `MAX_COMMAND_AGE` between `sent_at` and
    /// the journal's `ts` (D15).
    Expired,
    /// The trading day is over: a resting order cancelled at close (D22), or
    /// a new order that arrived after it.
    EndOfDay,
    /// Limit price is not positive or not a multiple of the tick (D5).
    InvalidPrice,
    /// Quantity is not positive or not a multiple of the lot (D5).
    InvalidQty,
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

/// Number of price levels per side in a snapshot (D6).
pub const DEPTH: usize = 3;

/// One price level of a snapshot: the price and the total remaining quantity
/// of every order resting there.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
pub struct BookLevel {
    pub price: Price,
    pub qty: Qty,
}

/// The engine's view for market data (D6): the best three levels per side,
/// the last trade price and the day's traded volume. Self-contained and cheap
/// to copy and compare, so the publisher can skip a snapshot equal to the last
/// one it sent (see `same_view`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Snapshot {
    /// Best (highest) price first.
    pub bids: [Option<BookLevel>; DEPTH],
    /// Best (lowest) price first.
    pub asks: [Option<BookLevel>; DEPTH],
    /// Price of the most recent trade today; `None` before the first trade.
    pub last_price: Option<Price>,
    /// Total quantity traded today, in size units (not currency).
    pub volume: Qty,
    /// Unix milliseconds at which this view was generated: the `ts` of the
    /// command that last changed it, pushed forward by at least 1 ms over the
    /// previous snapshot so it is strictly increasing (D6). It comes from the
    /// journal's timestamps, not from a clock. 0 before the first change.
    pub last_ms: Timestamp,
}

impl Snapshot {
    /// True if the market view is the same, ignoring `last_ms`. A publisher
    /// that skips unchanged snapshots (D6) compares with this.
    pub fn same_view(&self, other: &Snapshot) -> bool {
        self.bids == other.bids
            && self.asks == other.asks
            && self.last_price == other.last_price
            && self.volume == other.volume
    }

    /// The message that goes on the wire. Levels are listed best first and
    /// only as many as exist.
    pub fn into_market_data(self) -> MarketData {
        MarketData {
            bids: self.bids.into_iter().flatten().collect(),
            asks: self.asks.into_iter().flatten().collect(),
            last_price: self.last_price,
            volume: self.volume,
            last_ms: self.last_ms,
        }
    }
}

/// A market-data message as sent to clients (JSON, D17). Levels are listed best
/// first and only as many as exist.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct MarketData {
    pub bids: Vec<BookLevel>,
    pub asks: Vec<BookLevel>,
    pub last_price: Option<Price>,
    pub volume: Qty,
    pub last_ms: Timestamp,
}
