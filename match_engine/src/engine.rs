use crate::book::OrderBook;
use crate::types::{
    CancelTarget, Command, NewOrder, Order, OrderEvent, Output, Price, Reason, Seq, Side, Status,
    SymbolSpec, Timestamp,
};

/// Longest a `NewOrder` may spend between `sent_at` and the journal's `ts`
/// before it is rejected as `Expired` (D15), in milliseconds.
///
/// Fixed when the crate is compiled: set the `MAX_COMMAND_AGE` environment
/// variable (a non-negative integer, milliseconds) at build time, default 1000.
/// It must not exceed D's own command timeout.
pub const MAX_COMMAND_AGE: Timestamp = parse_age(option_env!("MAX_COMMAND_AGE"));

const fn parse_age(value: Option<&str>) -> Timestamp {
    let Some(text) = value else {
        return 1000;
    };
    let digits = text.as_bytes();
    assert!(!digits.is_empty(), "MAX_COMMAND_AGE must not be empty");
    let mut age: Timestamp = 0;
    let mut i = 0;
    while i < digits.len() {
        let d = digits[i];
        assert!(
            d.is_ascii_digit(),
            "MAX_COMMAND_AGE must be a non-negative integer (ms)"
        );
        age = match age.checked_mul(10) {
            Some(a) => a,
            None => panic!("MAX_COMMAND_AGE is too large"),
        };
        age = match age.checked_add((d - b'0') as Timestamp) {
            Some(a) => a,
            None => panic!("MAX_COMMAND_AGE is too large"),
        };
        i += 1;
    }
    age
}

/// One book plus its output queue. `process` is a pure function of the
/// command sequence it has been given.
pub struct MatchEngine {
    spec: SymbolSpec,
    book: OrderBook,
    outputs: Vec<Output>,
}

impl MatchEngine {
    pub fn new(spec: SymbolSpec) -> Self {
        Self {
            spec,
            book: OrderBook::default(),
            outputs: Vec::new(),
        }
    }

    /// Applies one command. `seq` and `ts` are the journal's sequence number
    /// and receive time for it; for a `New` command `seq` becomes the order's
    /// identity. `ts` is only compared with the command's own `sent_at`, so
    /// the result depends on the inputs alone, never on a clock.
    pub fn process(&mut self, seq: Seq, ts: Timestamp, cmd: Command) {
        match cmd {
            Command::New(new) => self.new_order(seq, ts, new),
            Command::Cancel(target) => self.cancel(target),
        }
    }

    pub fn outputs(&self) -> &[Output] {
        &self.outputs
    }

    /// Hands over everything produced so far and empties the queue.
    pub fn drain_outputs(&mut self) -> std::vec::Drain<'_, Output> {
        self.outputs.drain(..)
    }

    /// Why `new` is off the symbol's grid, if it is (D5). Price is checked
    /// first; a market order has no price to check.
    fn grid_violation(&self, new: &NewOrder) -> Option<Reason> {
        let SymbolSpec { tick, lot } = self.spec;
        if matches!(new.price, Some(p) if p <= 0 || p % tick != 0) {
            Some(Reason::InvalidPrice)
        } else if new.qty <= 0 || new.qty % lot != 0 {
            Some(Reason::InvalidQty)
        } else {
            None
        }
    }

    /// Why `new` must be refused before it reaches the book, if it must.
    /// Expiry is checked first (D15), then the grid (D5).
    fn refusal(&self, ts: Timestamp, new: &NewOrder) -> Option<Reason> {
        // `sent_at` after `ts` (clock skew) gives a negative age: not expired.
        if ts.saturating_sub(new.sent_at) > MAX_COMMAND_AGE {
            Some(Reason::Expired)
        } else {
            self.grid_violation(new)
        }
    }

    fn new_order(&mut self, seq: Seq, ts: Timestamp, new: NewOrder) {
        if let Some(reason) = self.refusal(ts, &new) {
            self.outputs.push(Output::Order(OrderEvent {
                order_seq: Some(seq),
                order_id: Some(new.order_id),
                user_id: Some(new.user_id),
                status: Status::Rejected,
                filled: 0,
                remaining: new.qty,
                reason: Some(reason),
            }));
            return;
        }
        self.outputs.push(Output::Order(OrderEvent {
            order_seq: Some(seq),
            order_id: Some(new.order_id),
            user_id: Some(new.user_id),
            status: Status::Accepted,
            filled: 0,
            remaining: new.qty,
            reason: None,
        }));
        self.book.add(
            Order {
                seq,
                order_id: new.order_id,
                user_id: new.user_id,
                side: new.side,
                // A market order crosses every price.
                price: new.price.unwrap_or(match new.side {
                    Side::Buy => Price::MAX,
                    Side::Sell => Price::MIN,
                }),
                qty: new.qty,
                filled: 0,
                market: new.price.is_none(),
            },
            &mut self.outputs,
        );
    }

    fn cancel(&mut self, target: CancelTarget) {
        let (removed, order_seq, order_id, user_id) = match target {
            CancelTarget::BySeq(seq) => (self.book.remove(seq), Some(seq), None, None),
            CancelTarget::ById { user_id, order_id } => (
                self.book.remove_by_key(user_id, order_id),
                None,
                Some(order_id),
                Some(user_id),
            ),
        };
        let event = match removed {
            Some(o) => OrderEvent {
                order_seq: Some(o.seq),
                order_id: Some(o.order_id),
                user_id: Some(o.user_id),
                status: Status::Cancelled,
                filled: o.filled,
                remaining: o.qty,
                reason: None,
            },
            None => OrderEvent {
                order_seq,
                order_id,
                user_id,
                status: Status::Rejected,
                filled: 0,
                remaining: 0,
                reason: Some(Reason::OrderNotFound),
            },
        };
        self.outputs.push(Output::Order(event));
    }
}
