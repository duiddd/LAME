//! Expiry of stale `NewOrder` commands (D15). Everything here is a comparison
//! of timestamps given to the engine; no test waits or reads a clock.

use match_engine::{
    CancelTarget, Command, MatchEngine, NewOrder, OrderEvent, Output, Reason, Side, Status,
    SymbolSpec, Timestamp, MAX_COMMAND_AGE,
};

const T0: Timestamp = 1_000_000;

fn engine() -> MatchEngine {
    MatchEngine::new(SymbolSpec::UNIT)
}

fn new_order(
    user_id: u64,
    side: Side,
    price: Option<i64>,
    qty: i64,
    sent_at: Timestamp,
) -> Command {
    Command::New(NewOrder {
        order_id: user_id,
        user_id,
        side,
        price,
        qty,
        sent_at,
    })
}

fn send(e: &mut MatchEngine, seq: u64, ts: Timestamp, cmd: Command) -> Vec<Output> {
    e.process(seq, ts, cmd);
    e.drain_outputs().collect()
}

fn expired(seq: u64, remaining: i64) -> Vec<Output> {
    vec![Output::Order(OrderEvent {
        order_seq: Some(seq),
        order_id: Some(seq),
        user_id: Some(seq),
        status: Status::Rejected,
        filled: 0,
        remaining,
        reason: Some(Reason::Expired),
    })]
}

fn is_accepted(out: &[Output]) -> bool {
    matches!(out.first(), Some(Output::Order(ev)) if ev.status == Status::Accepted)
}

#[test]
fn default_threshold_is_one_second() {
    // Overridden at build time with the MAX_COMMAND_AGE environment variable.
    if option_env!("MAX_COMMAND_AGE").is_none() {
        assert_eq!(MAX_COMMAND_AGE, 1000);
    }
}

#[test]
fn age_equal_to_the_threshold_is_not_expired() {
    let mut e = engine();
    let cmd = new_order(1, Side::Buy, Some(10), 1, T0 - MAX_COMMAND_AGE);
    assert!(is_accepted(&send(&mut e, 1, T0, cmd)));
}

#[test]
fn age_one_past_the_threshold_is_expired() {
    let mut e = engine();
    let cmd = new_order(1, Side::Buy, Some(10), 3, T0 - MAX_COMMAND_AGE - 1);
    assert_eq!(send(&mut e, 1, T0, cmd), expired(1, 3));
}

#[test]
fn fresh_orders_are_accepted() {
    let mut e = engine();
    assert!(is_accepted(&send(
        &mut e,
        1,
        T0,
        new_order(1, Side::Buy, Some(10), 1, T0)
    )));
    assert!(is_accepted(&send(
        &mut e,
        2,
        T0,
        new_order(2, Side::Sell, Some(11), 1, T0 - 1)
    )));
}

#[test]
fn sent_after_receipt_counts_as_not_expired() {
    let mut e = engine();
    let cmd = new_order(1, Side::Buy, Some(10), 1, T0 + 5_000);
    assert!(is_accepted(&send(&mut e, 1, T0, cmd)));
}

#[test]
fn extreme_timestamps_do_not_overflow() {
    let mut e = engine();
    // Hugely old: expired. Hugely in the future: not expired.
    let old = new_order(1, Side::Buy, Some(10), 1, i64::MIN);
    assert_eq!(send(&mut e, 1, i64::MAX, old), expired(1, 1));
    let future = new_order(2, Side::Buy, Some(10), 1, i64::MAX);
    assert!(is_accepted(&send(&mut e, 2, i64::MIN, future)));
}

#[test]
fn market_orders_expire_too() {
    let mut e = engine();
    let cmd = new_order(1, Side::Buy, None, 2, T0 - MAX_COMMAND_AGE - 1);
    assert_eq!(send(&mut e, 1, T0, cmd), expired(1, 2));
}

#[test]
fn expiry_is_checked_before_the_grid() {
    let mut e = MatchEngine::new(SymbolSpec::new(5, 10));
    // Off the tick and off the lot, and also too old: reported as expired.
    let cmd = new_order(1, Side::Buy, Some(7), 3, T0 - MAX_COMMAND_AGE - 1);
    assert_eq!(send(&mut e, 1, T0, cmd), expired(1, 3));
    // Fresh and off the grid: reported as a grid violation instead.
    let cmd = new_order(2, Side::Buy, Some(7), 3, T0);
    let out = send(&mut e, 2, T0, cmd);
    assert!(matches!(
        out.as_slice(),
        [Output::Order(OrderEvent {
            reason: Some(Reason::InvalidPrice),
            ..
        })]
    ));
}

#[test]
fn expired_order_neither_trades_nor_rests() {
    let mut e = engine();
    send(&mut e, 1, T0, new_order(1, Side::Sell, Some(10), 5, T0));
    // Would cross the resting sell if it were live.
    let stale = new_order(2, Side::Buy, Some(10), 5, T0 - MAX_COMMAND_AGE - 1);
    assert_eq!(send(&mut e, 2, T0, stale), expired(2, 5));
    // It is not known to the engine, so it cannot be cancelled...
    let out = send(&mut e, 3, T0, Command::Cancel(CancelTarget::BySeq(2)));
    assert!(matches!(
        out.as_slice(),
        [Output::Order(OrderEvent {
            reason: Some(Reason::OrderNotFound),
            ..
        })]
    ));
    // ...and the resting sell is untouched.
    let out = send(&mut e, 4, T0, new_order(3, Side::Buy, Some(10), 5, T0));
    assert!(out.iter().any(|o| matches!(o, Output::Trade(_))));
}

#[test]
fn cancels_are_never_expired() {
    let mut e = engine();
    send(&mut e, 1, T0, new_order(1, Side::Buy, Some(10), 4, T0));
    // Far later than the threshold: a cancel carries no sent_at and still works.
    let out = send(
        &mut e,
        2,
        T0 + 100 * MAX_COMMAND_AGE,
        Command::Cancel(CancelTarget::BySeq(1)),
    );
    assert!(matches!(
        out.as_slice(),
        [Output::Order(OrderEvent {
            status: Status::Cancelled,
            remaining: 4,
            ..
        })]
    ));
}

#[test]
fn replay_gives_identical_outputs_including_expiries() {
    let history = [
        (1, T0, new_order(1, Side::Sell, Some(10), 3, T0)),
        (
            2,
            T0,
            new_order(2, Side::Buy, Some(10), 3, T0 - MAX_COMMAND_AGE - 1),
        ),
        (3, T0 + 1, new_order(3, Side::Buy, Some(10), 2, T0)),
        (4, T0 + 2, Command::Cancel(CancelTarget::BySeq(1))),
    ];
    let run = || {
        let mut e = engine();
        for (seq, ts, cmd) in history {
            e.process(seq, ts, cmd);
        }
        e.drain_outputs().collect::<Vec<_>>()
    };
    assert_eq!(run(), run());
}
