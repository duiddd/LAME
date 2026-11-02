//! Self-trade prevention (D24): an incoming order never trades with a resting
//! order of the same user. Matching stops there and the remainder is rejected.

use match_engine::{
    Command, MatchEngine, NewOrder, OrderEvent, Output, Reason, Seq, Side, Status, SymbolSpec,
    Trade,
};

const ME: u64 = 1;
const OTHER: u64 = 2;
const THIRD: u64 = 3;

fn order(
    e: &mut MatchEngine,
    seq: Seq,
    user_id: u64,
    side: Side,
    price: Option<i64>,
    qty: i64,
) -> Vec<Output> {
    e.process(
        seq,
        Command::New(NewOrder {
            order_id: seq,
            user_id,
            side,
            price,
            qty,
        }),
    );
    e.drain_outputs().collect()
}

fn trade(price: i64, qty: i64, taker: (Seq, u64), maker: (Seq, u64)) -> Output {
    Output::Trade(Trade {
        price,
        qty,
        taker_seq: taker.0,
        maker_seq: maker.0,
        taker_user_id: taker.1,
        maker_user_id: maker.1,
    })
}

fn event(
    seq: Seq,
    user_id: u64,
    status: Status,
    filled: i64,
    remaining: i64,
    reason: Option<Reason>,
) -> Output {
    Output::Order(OrderEvent {
        order_seq: Some(seq),
        order_id: Some(seq),
        user_id: Some(user_id),
        status,
        filled,
        remaining,
        reason,
    })
}

const SELF_TRADE: Option<Reason> = Some(Reason::SelfTradePrevented);

/// OTHER sells 2 @10 (seq 1), ME sells 3 @10 (seq 2), and ME then sends an
/// aggressive buy for 5. Price-time priority reaches OTHER's order first, so
/// 2 fill; the next order in line is ME's own, so the other 3 are rejected.
fn aggressive_buy_hits_own_order(price: Option<i64>) {
    let mut e = MatchEngine::new(SymbolSpec::UNIT);
    order(&mut e, 1, OTHER, Side::Sell, Some(10), 2);
    order(&mut e, 2, ME, Side::Sell, Some(10), 3);

    let out = order(&mut e, 3, ME, Side::Buy, price, 5);
    assert_eq!(
        out,
        [
            event(3, ME, Status::Accepted, 0, 5, None),
            trade(10, 2, (3, ME), (1, OTHER)),
            event(3, ME, Status::PartiallyFilled, 2, 3, None),
            event(1, OTHER, Status::Filled, 2, 0, None),
            event(3, ME, Status::Rejected, 2, 3, SELF_TRADE),
        ]
    );

    // The rejected remainder did not rest: a sell finds no buyer. ME's resting
    // sell is untouched and still first in line, so a buy trades against it.
    let out = order(&mut e, 4, THIRD, Side::Sell, Some(10), 10);
    assert_eq!(out, [event(4, THIRD, Status::Accepted, 0, 10, None)]);
    let out = order(&mut e, 5, THIRD, Side::Buy, Some(10), 3);
    assert_eq!(
        out,
        [
            event(5, THIRD, Status::Accepted, 0, 3, None),
            trade(10, 3, (5, THIRD), (2, ME)),
            event(5, THIRD, Status::Filled, 3, 0, None),
            event(2, ME, Status::Filled, 3, 0, None),
        ]
    );
}

#[test]
fn limit_order_partially_filled_then_remainder_rejected() {
    aggressive_buy_hits_own_order(Some(10));
}

#[test]
fn market_order_partially_filled_then_remainder_rejected() {
    aggressive_buy_hits_own_order(None);
}

#[test]
fn sell_side_is_symmetric_for_limit_and_market() {
    for price in [Some(10), None] {
        let mut e = MatchEngine::new(SymbolSpec::UNIT);
        order(&mut e, 1, OTHER, Side::Buy, Some(10), 2);
        order(&mut e, 2, ME, Side::Buy, Some(10), 3);
        let out = order(&mut e, 3, ME, Side::Sell, price, 5);
        assert_eq!(
            out,
            [
                event(3, ME, Status::Accepted, 0, 5, None),
                trade(10, 2, (3, ME), (1, OTHER)),
                event(3, ME, Status::PartiallyFilled, 2, 3, None),
                event(1, OTHER, Status::Filled, 2, 0, None),
                event(3, ME, Status::Rejected, 2, 3, SELF_TRADE),
            ]
        );
    }
}

#[test]
fn rejected_when_the_first_order_in_line_is_its_own() {
    for price in [Some(10), None] {
        let mut e = MatchEngine::new(SymbolSpec::UNIT);
        order(&mut e, 1, ME, Side::Sell, Some(10), 2);
        order(&mut e, 2, OTHER, Side::Sell, Some(10), 2); // behind ME's order
        let out = order(&mut e, 3, ME, Side::Buy, price, 2);
        // No trade: ME's own order has time priority over OTHER's.
        assert_eq!(
            out,
            [
                event(3, ME, Status::Accepted, 0, 2, None),
                event(3, ME, Status::Rejected, 0, 2, SELF_TRADE),
            ]
        );
    }
}

#[test]
fn own_order_at_a_worse_price_does_not_block_better_prices() {
    let mut e = MatchEngine::new(SymbolSpec::UNIT);
    order(&mut e, 1, OTHER, Side::Sell, Some(10), 2);
    order(&mut e, 2, ME, Side::Sell, Some(11), 2);
    // A limit at 10 never reaches ME's sell at 11, so it is not a self trade.
    let out = order(&mut e, 3, ME, Side::Buy, Some(10), 2);
    assert_eq!(
        out,
        [
            event(3, ME, Status::Accepted, 0, 2, None),
            trade(10, 2, (3, ME), (1, OTHER)),
            event(3, ME, Status::Filled, 2, 0, None),
            event(1, OTHER, Status::Filled, 2, 0, None),
        ]
    );
}

#[test]
fn resting_own_order_on_the_same_side_is_not_a_conflict() {
    let mut e = MatchEngine::new(SymbolSpec::UNIT);
    order(&mut e, 1, ME, Side::Buy, Some(10), 2);
    let out = order(&mut e, 2, ME, Side::Buy, Some(10), 2);
    assert_eq!(out, [event(2, ME, Status::Accepted, 0, 2, None)]);
}

#[test]
fn orders_of_different_users_still_match() {
    let mut e = MatchEngine::new(SymbolSpec::UNIT);
    order(&mut e, 1, OTHER, Side::Sell, Some(10), 2);
    let out = order(&mut e, 2, ME, Side::Buy, Some(10), 2);
    assert!(out.iter().any(|o| matches!(o, Output::Trade(_))));
    assert!(!out.iter().any(|o| matches!(
        o,
        Output::Order(OrderEvent {
            status: Status::Rejected,
            ..
        })
    )));
}

#[test]
fn market_order_remainder_is_cancelled_when_liquidity_runs_out() {
    let mut e = MatchEngine::new(SymbolSpec::UNIT);
    order(&mut e, 1, OTHER, Side::Sell, Some(10), 2);
    let out = order(&mut e, 2, ME, Side::Buy, None, 5);
    assert_eq!(
        out,
        [
            event(2, ME, Status::Accepted, 0, 5, None),
            trade(10, 2, (2, ME), (1, OTHER)),
            event(2, ME, Status::PartiallyFilled, 2, 3, None),
            event(1, OTHER, Status::Filled, 2, 0, None),
            event(2, ME, Status::Cancelled, 2, 3, None),
        ]
    );
    // It did not rest: a later sell finds no buyer.
    let out = order(&mut e, 3, THIRD, Side::Sell, Some(1), 1);
    assert_eq!(out, [event(3, THIRD, Status::Accepted, 0, 1, None)]);
}
