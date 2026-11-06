//! End-of-day close (D22): every resting order is cancelled in `seq` order and
//! the book is reset completely.

use match_engine::{
    CancelTarget, Command, MatchEngine, NewOrder, OrderEvent, Output, Reason, Side, Status,
    SymbolSpec, Timestamp,
};

const TS: Timestamp = 0;

fn engine() -> MatchEngine {
    MatchEngine::new(SymbolSpec::UNIT)
}

fn place(e: &mut MatchEngine, seq: u64, user_id: u64, side: Side, price: i64, qty: i64) {
    e.process(
        seq,
        TS,
        Command::New(NewOrder {
            order_id: seq,
            user_id,
            side,
            price: Some(price),
            qty,
            sent_at: TS,
        }),
    );
}

fn out(e: &mut MatchEngine) -> Vec<Output> {
    e.drain_outputs().collect()
}

fn eod_cancel(seq: u64, user_id: u64, filled: i64, remaining: i64) -> Output {
    Output::Order(OrderEvent {
        order_seq: Some(seq),
        order_id: Some(seq),
        user_id: Some(user_id),
        status: Status::Cancelled,
        filled,
        remaining,
        reason: Some(Reason::EndOfDay),
    })
}

#[test]
fn cancels_every_resting_order_in_seq_order_not_price_order() {
    let mut e = engine();
    // Prices deliberately out of order relative to seq, on both sides.
    place(&mut e, 1, 1, Side::Sell, 12, 3);
    place(&mut e, 2, 2, Side::Buy, 7, 4);
    place(&mut e, 3, 3, Side::Sell, 10, 5);
    place(&mut e, 4, 4, Side::Buy, 9, 6);
    place(&mut e, 5, 5, Side::Sell, 11, 1);
    out(&mut e);

    e.process(6, TS, Command::DayClose);
    assert_eq!(
        out(&mut e),
        [
            eod_cancel(1, 1, 0, 3),
            eod_cancel(2, 2, 0, 4),
            eod_cancel(3, 3, 0, 5),
            eod_cancel(4, 4, 0, 6),
            eod_cancel(5, 5, 0, 1),
        ]
    );
}

#[test]
fn partially_filled_orders_report_filled_and_remaining() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, 10, 5);
    place(&mut e, 2, 2, Side::Buy, 10, 2); // fills 2 of seq 1
    out(&mut e);

    e.process(3, TS, Command::DayClose);
    assert_eq!(out(&mut e), [eod_cancel(1, 1, 2, 3)]);
}

#[test]
fn close_on_an_empty_book_outputs_nothing() {
    let mut e = engine();
    e.process(1, TS, Command::DayClose);
    assert!(out(&mut e).is_empty());

    place(&mut e, 2, 1, Side::Buy, 10, 1);
    place(&mut e, 3, 2, Side::Sell, 10, 1); // fully fills both
    out(&mut e);
    e.process(4, TS, Command::DayClose);
    assert!(out(&mut e).is_empty());
}

#[test]
fn book_is_fully_reset_afterwards() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, 10, 5);
    place(&mut e, 2, 2, Side::Buy, 8, 5);
    out(&mut e);
    e.process(3, TS, Command::DayClose);
    out(&mut e);

    // Old orders cannot be cancelled, by seq or by id.
    e.process(4, TS, Command::Cancel(CancelTarget::BySeq(1)));
    e.process(
        5,
        TS,
        Command::Cancel(CancelTarget::ById {
            user_id: 2,
            order_id: 2,
        }),
    );
    let rejected = out(&mut e);
    assert_eq!(rejected.len(), 2);
    assert!(rejected.iter().all(|o| matches!(
        o,
        Output::Order(OrderEvent {
            status: Status::Rejected,
            reason: Some(Reason::OrderNotFound),
            ..
        })
    )));

    // A buy that would have crossed the old ask finds nothing to trade with.
    place(&mut e, 6, 3, Side::Buy, 10, 5);
    assert!(!out(&mut e).iter().any(|o| matches!(o, Output::Trade(_))));
}

#[test]
fn old_bids_do_not_survive_either() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Buy, 8, 5);
    out(&mut e);
    e.process(2, TS, Command::DayClose);
    out(&mut e);

    // A sell that would have crossed the old bid finds nothing to trade with.
    place(&mut e, 3, 3, Side::Sell, 8, 5);
    assert!(!out(&mut e).iter().any(|o| matches!(o, Output::Trade(_))));
}

#[test]
fn engine_keeps_working_and_seq_may_restart_from_one() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, 10, 2);
    e.process(2, TS, Command::DayClose);
    out(&mut e);

    // A new day: seq restarts at 1, same ids and users as before.
    place(&mut e, 1, 1, Side::Sell, 10, 2);
    place(&mut e, 2, 2, Side::Buy, 10, 2);
    let trades: Vec<_> = out(&mut e)
        .into_iter()
        .filter_map(|o| match o {
            Output::Trade(t) => Some((t.taker_seq, t.maker_seq)),
            _ => None,
        })
        .collect();
    assert_eq!(trades, [(2, 1)]);
}

#[test]
fn closing_twice_cancels_nothing_the_second_time() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Buy, 10, 2);
    out(&mut e);
    e.process(2, TS, Command::DayClose);
    assert_eq!(out(&mut e).len(), 1);
    e.process(3, TS, Command::DayClose);
    assert!(out(&mut e).is_empty());
}

#[test]
fn replay_gives_identical_outputs() {
    let run = || {
        let mut e = engine();
        place(&mut e, 1, 1, Side::Sell, 10, 4);
        place(&mut e, 2, 2, Side::Buy, 10, 1);
        place(&mut e, 3, 3, Side::Buy, 9, 2);
        e.process(4, TS, Command::DayClose);
        place(&mut e, 1, 1, Side::Sell, 10, 4);
        out(&mut e)
    };
    assert_eq!(run(), run());
}

#[test]
fn orders_outside_the_session_are_rejected_as_end_of_day() {
    fn open_only_in_the_middle(ts: Timestamp) -> bool {
        (1_000..2_000).contains(&ts)
    }
    let mut e = MatchEngine::new(SymbolSpec::UNIT).with_session(open_only_in_the_middle);
    let send = |e: &mut MatchEngine, seq: u64, ts: Timestamp| {
        e.process(
            seq,
            ts,
            Command::New(NewOrder {
                order_id: seq,
                user_id: seq,
                side: Side::Buy,
                price: Some(10),
                qty: 1,
                sent_at: ts,
            }),
        );
        out(e)
    };
    let rejected = |seq: u64| {
        vec![Output::Order(OrderEvent {
            order_seq: Some(seq),
            order_id: Some(seq),
            user_id: Some(seq),
            status: Status::Rejected,
            filled: 0,
            remaining: 1,
            reason: Some(Reason::EndOfDay),
        })]
    };
    assert_eq!(send(&mut e, 1, 999), rejected(1)); // before the session
    assert_eq!(send(&mut e, 2, 2_000), rejected(2)); // at and after the close
    let in_session = send(&mut e, 3, 1_500);
    assert!(matches!(
        in_session.as_slice(),
        [Output::Order(OrderEvent {
            status: Status::Accepted,
            ..
        })]
    ));
    // Cancels are not gated by the session.
    e.process(4, 5_000, Command::Cancel(CancelTarget::BySeq(3)));
    assert!(matches!(
        out(&mut e).as_slice(),
        [Output::Order(OrderEvent {
            status: Status::Cancelled,
            ..
        })]
    ));
}
