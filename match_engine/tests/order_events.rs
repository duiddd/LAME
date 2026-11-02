//! Order events, sequence numbers, user ids and cancel targeting (D2, D4, D7).

use match_engine::{
    CancelTarget, Command, MatchEngine, NewOrder, OrderEvent, Output, Reason, Side, Status, Trade,
};

fn new(order_id: u64, user_id: u64, side: Side, price: i64, qty: i64) -> Command {
    Command::New(NewOrder {
        order_id,
        user_id,
        side,
        price: Some(price),
        qty,
    })
}

fn ev(
    seq: u64,
    order_id: u64,
    user_id: u64,
    status: Status,
    filled: i64,
    remaining: i64,
) -> Output {
    Output::Order(OrderEvent {
        order_seq: Some(seq),
        order_id: Some(order_id),
        user_id: Some(user_id),
        status,
        filled,
        remaining,
        reason: None,
    })
}

fn rejected(
    seq: Option<u64>,
    order_id: Option<u64>,
    user_id: Option<u64>,
    reason: Reason,
) -> Output {
    Output::Order(OrderEvent {
        order_seq: seq,
        order_id,
        user_id,
        status: Status::Rejected,
        filled: 0,
        remaining: 0,
        reason: Some(reason),
    })
}

fn out(e: &mut MatchEngine) -> Vec<Output> {
    e.drain_outputs().collect()
}

#[test]
fn every_new_order_is_accepted_first() {
    let mut e = MatchEngine::new();
    e.process(10, new(7, 1, Side::Sell, 100, 5));
    assert_eq!(out(&mut e), [ev(10, 7, 1, Status::Accepted, 0, 5)]);
}

#[test]
fn match_emits_accepted_trade_taker_then_maker() {
    let mut e = MatchEngine::new();
    e.process(1, new(100, 11, Side::Sell, 50, 5));
    out(&mut e);
    e.process(2, new(200, 22, Side::Buy, 50, 3));
    assert_eq!(
        out(&mut e),
        [
            ev(2, 200, 22, Status::Accepted, 0, 3),
            Output::Trade(Trade {
                price: 50,
                qty: 3,
                taker_seq: 2,
                maker_seq: 1,
                taker_user_id: 22,
                maker_user_id: 11,
            }),
            ev(2, 200, 22, Status::Filled, 3, 0),
            ev(1, 100, 11, Status::PartiallyFilled, 3, 2),
        ]
    );
}

#[test]
fn filled_counts_accumulate_across_trades() {
    let mut e = MatchEngine::new();
    e.process(1, new(1, 1, Side::Sell, 10, 2));
    e.process(2, new(2, 2, Side::Sell, 10, 2));
    out(&mut e);
    e.process(3, new(3, 3, Side::Buy, 10, 5));
    let events = out(&mut e);
    // Taker events after each of the two trades, then it rests with 1 left.
    assert!(events.contains(&ev(3, 3, 3, Status::PartiallyFilled, 2, 3)));
    assert!(events.contains(&ev(3, 3, 3, Status::PartiallyFilled, 4, 1)));
    e.process(4, Command::Cancel(CancelTarget::BySeq(3)));
    assert_eq!(out(&mut e), [ev(3, 3, 3, Status::Cancelled, 4, 1)]);
}

#[test]
fn cancel_by_user_and_order_id() {
    let mut e = MatchEngine::new();
    e.process(1, new(9, 5, Side::Buy, 10, 4));
    out(&mut e);
    e.process(
        2,
        Command::Cancel(CancelTarget::ById {
            user_id: 5,
            order_id: 9,
        }),
    );
    assert_eq!(out(&mut e), [ev(1, 9, 5, Status::Cancelled, 0, 4)]);
}

#[test]
fn same_order_id_for_different_users_is_distinct() {
    let mut e = MatchEngine::new();
    e.process(1, new(1, 100, Side::Buy, 10, 1));
    e.process(2, new(1, 200, Side::Buy, 10, 1));
    out(&mut e);
    e.process(
        3,
        Command::Cancel(CancelTarget::ById {
            user_id: 200,
            order_id: 1,
        }),
    );
    assert_eq!(out(&mut e), [ev(2, 1, 200, Status::Cancelled, 0, 1)]);
    // The other user's order is still resting.
    e.process(4, new(2, 300, Side::Sell, 10, 1));
    let trades: Vec<_> = out(&mut e)
        .into_iter()
        .filter_map(|o| match o {
            Output::Trade(t) => Some(t.maker_seq),
            _ => None,
        })
        .collect();
    assert_eq!(trades, [1]);
}

#[test]
fn cancel_by_reused_id_targets_the_latest_resting_order() {
    let mut e = MatchEngine::new();
    e.process(1, new(1, 1, Side::Buy, 10, 1));
    e.process(2, new(1, 1, Side::Buy, 10, 1)); // same (user, id): undefined upstream, latest wins
    out(&mut e);
    e.process(
        3,
        Command::Cancel(CancelTarget::ById {
            user_id: 1,
            order_id: 1,
        }),
    );
    assert_eq!(out(&mut e), [ev(2, 1, 1, Status::Cancelled, 0, 1)]);
}

#[test]
fn cancel_of_unknown_order_is_rejected() {
    let mut e = MatchEngine::new();
    e.process(
        1,
        Command::Cancel(CancelTarget::ById {
            user_id: 5,
            order_id: 9,
        }),
    );
    e.process(2, Command::Cancel(CancelTarget::BySeq(77)));
    assert_eq!(
        out(&mut e),
        [
            rejected(None, Some(9), Some(5), Reason::OrderNotFound),
            rejected(Some(77), None, None, Reason::OrderNotFound),
        ]
    );
}

#[test]
fn cancel_of_finished_order_is_rejected_as_not_found() {
    let mut e = MatchEngine::new();
    e.process(1, new(1, 1, Side::Sell, 10, 1));
    e.process(2, new(2, 2, Side::Buy, 10, 1)); // both filled
    e.process(3, new(3, 3, Side::Buy, 9, 1));
    e.process(4, Command::Cancel(CancelTarget::BySeq(3))); // cancelled
    out(&mut e);

    e.process(
        5,
        Command::Cancel(CancelTarget::ById {
            user_id: 1,
            order_id: 1,
        }),
    );
    e.process(6, Command::Cancel(CancelTarget::BySeq(2)));
    e.process(7, Command::Cancel(CancelTarget::BySeq(3)));
    assert_eq!(
        out(&mut e),
        [
            rejected(None, Some(1), Some(1), Reason::OrderNotFound),
            rejected(Some(2), None, None, Reason::OrderNotFound),
            rejected(Some(3), None, None, Reason::OrderNotFound),
        ]
    );
}

#[test]
fn rejected_cancel_leaves_the_book_untouched() {
    let mut e = MatchEngine::new();
    e.process(1, new(1, 1, Side::Sell, 10, 2));
    e.process(2, Command::Cancel(CancelTarget::BySeq(99)));
    out(&mut e);
    e.process(3, new(2, 2, Side::Buy, 10, 2));
    let trades = out(&mut e)
        .into_iter()
        .filter(|o| matches!(o, Output::Trade(_)))
        .count();
    assert_eq!(trades, 1);
}

#[test]
fn replaying_the_same_commands_gives_identical_outputs() {
    let cmds = [
        (1, new(1, 1, Side::Sell, 10, 3)),
        (2, new(2, 2, Side::Buy, 10, 1)),
        (3, new(3, 3, Side::Buy, 11, 5)),
        (4, Command::Cancel(CancelTarget::BySeq(3))),
        (5, new(4, 4, Side::Sell, 9, 2)),
    ];
    let run = || {
        let mut e = MatchEngine::new();
        for (seq, c) in cmds {
            e.process(seq, c);
        }
        out(&mut e)
    };
    assert_eq!(run(), run());
}
