//! Tick and lot grid validation at the engine's entrance (D5).

use match_engine::{
    CancelTarget, Command, MatchEngine, NewOrder, OrderEvent, Output, Reason, Side, Status,
    SymbolSpec,
};

const TICK: i64 = 5;
const LOT: i64 = 10;

fn engine() -> MatchEngine {
    MatchEngine::new(SymbolSpec::new(TICK, LOT))
}

fn send(e: &mut MatchEngine, seq: u64, side: Side, price: Option<i64>, qty: i64) -> Vec<Output> {
    e.process(
        seq,
        Command::New(NewOrder {
            order_id: seq,
            user_id: seq, // distinct users: no self-trade interference
            side,
            price,
            qty,
        }),
    );
    e.drain_outputs().collect()
}

fn rejected(seq: u64, remaining: i64, reason: Reason) -> Vec<Output> {
    vec![Output::Order(OrderEvent {
        order_seq: Some(seq),
        order_id: Some(seq),
        user_id: Some(seq),
        status: Status::Rejected,
        filled: 0,
        remaining,
        reason: Some(reason),
    })]
}

fn accepted(out: &[Output]) -> bool {
    matches!(out.first(), Some(Output::Order(ev)) if ev.status == Status::Accepted)
}

#[test]
fn on_grid_orders_are_accepted() {
    let mut e = engine();
    assert!(accepted(&send(&mut e, 1, Side::Buy, Some(100), 20)));
    assert!(accepted(&send(&mut e, 2, Side::Sell, Some(105), 10)));
    assert!(accepted(&send(&mut e, 3, Side::Buy, None, 30)));
}

#[test]
fn price_off_the_tick_is_rejected() {
    let mut e = engine();
    for price in [101, 104, 1] {
        let out = send(&mut e, 1, Side::Buy, Some(price), 10);
        assert_eq!(out, rejected(1, 10, Reason::InvalidPrice), "price {price}");
    }
}

#[test]
fn non_positive_price_is_rejected() {
    let mut e = engine();
    for price in [0, -5, i64::MIN] {
        let out = send(&mut e, 1, Side::Sell, Some(price), 10);
        assert_eq!(out, rejected(1, 10, Reason::InvalidPrice), "price {price}");
    }
}

#[test]
fn quantity_off_the_lot_is_rejected() {
    let mut e = engine();
    for qty in [1, 15, 11] {
        let out = send(&mut e, 1, Side::Buy, Some(100), qty);
        assert_eq!(out, rejected(1, qty, Reason::InvalidQty), "qty {qty}");
    }
}

#[test]
fn non_positive_quantity_is_rejected() {
    let mut e = engine();
    for qty in [0, -10] {
        let out = send(&mut e, 1, Side::Buy, Some(100), qty);
        assert_eq!(out, rejected(1, qty, Reason::InvalidQty), "qty {qty}");
    }
}

#[test]
fn market_order_checks_quantity_only() {
    let mut e = engine();
    assert_eq!(
        send(&mut e, 1, Side::Buy, None, 15),
        rejected(1, 15, Reason::InvalidQty)
    );
    assert_eq!(
        send(&mut e, 2, Side::Sell, None, 0),
        rejected(2, 0, Reason::InvalidQty)
    );
}

#[test]
fn price_is_reported_before_quantity_when_both_are_wrong() {
    let mut e = engine();
    let out = send(&mut e, 1, Side::Buy, Some(101), 15);
    assert_eq!(out, rejected(1, 15, Reason::InvalidPrice));
}

#[test]
fn rejected_order_never_reaches_the_book() {
    let mut e = engine();
    send(&mut e, 1, Side::Sell, Some(100), 10);
    // Off-grid buy would have crossed the resting sell had it been accepted.
    assert_eq!(
        send(&mut e, 2, Side::Buy, Some(101), 10),
        rejected(2, 10, Reason::InvalidPrice)
    );
    // Nothing traded, the sell is still resting, and the rejected order cannot
    // be cancelled because it was never known.
    e.process(3, Command::Cancel(CancelTarget::BySeq(2)));
    assert_eq!(
        e.drain_outputs().collect::<Vec<_>>(),
        [Output::Order(OrderEvent {
            order_seq: Some(2),
            order_id: None,
            user_id: None,
            status: Status::Rejected,
            filled: 0,
            remaining: 0,
            reason: Some(Reason::OrderNotFound),
        })]
    );
    let out = send(&mut e, 4, Side::Buy, Some(100), 10);
    assert!(out.iter().any(|o| matches!(o, Output::Trade(_))));
}

#[test]
fn spec_with_non_positive_tick_or_lot_is_refused() {
    assert!(std::panic::catch_unwind(|| SymbolSpec::new(0, 1)).is_err());
    assert!(std::panic::catch_unwind(|| SymbolSpec::new(1, -1)).is_err());
}
