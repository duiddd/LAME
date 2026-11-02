use crate::book::OrderBook;
use crate::types::{
    CancelTarget, Command, NewOrder, Order, OrderEvent, Output, Price, Reason, Seq, Side, Status,
    SymbolSpec,
};

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

    /// Applies one command. `seq` is the journal's sequence number for it; for
    /// a `New` command it becomes the order's identity.
    pub fn process(&mut self, seq: Seq, cmd: Command) {
        match cmd {
            Command::New(new) => self.new_order(seq, new),
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

    fn new_order(&mut self, seq: Seq, new: NewOrder) {
        if let Some(reason) = self.grid_violation(&new) {
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
