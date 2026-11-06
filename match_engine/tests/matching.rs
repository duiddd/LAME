//! Matching and cancel behaviour of the order book. Prices are plain ticks.

mod common;

use common::{Harness, LineEvent};
use match_engine::Side;

use LineEvent::{Fill, PartialFill, Trade};
use Side::{Buy, Sell};

fn t(price: i64, qty: i64) -> LineEvent {
    Trade { price, qty }
}
fn f(order_id: u64) -> LineEvent {
    Fill { order_id }
}
fn pf(order_id: u64, qty_remain: i64) -> LineEvent {
    PartialFill {
        order_id,
        qty_remain,
    }
}

/// Asserts the queued events, then empties the queue.
fn expect(e: &mut Harness, want: &[LineEvent]) {
    assert_eq!(e.drain_lines(), want);
}

#[test]
fn same_price_asks_match_in_arrival_order() {
    let mut e = Harness::new();
    e.add(30, Sell, 100, 2);
    e.add(10, Sell, 100, 3);
    e.add(20, Sell, 100, 4);
    e.add(40, Buy, 100, 6);
    expect(
        &mut e,
        &[
            t(100, 2),
            pf(40, 4),
            f(30),
            t(100, 3),
            pf(40, 1),
            f(10),
            t(100, 1),
            f(40),
            pf(20, 3),
        ],
    );
}

#[test]
fn same_price_bids_match_in_arrival_order() {
    let mut e = Harness::new();
    e.add(30, Buy, 100, 2);
    e.add(10, Buy, 100, 3);
    e.add(20, Buy, 100, 4);
    e.add(40, Sell, 100, 6);
    expect(
        &mut e,
        &[
            t(100, 2),
            pf(40, 4),
            f(30),
            t(100, 3),
            pf(40, 1),
            f(10),
            t(100, 1),
            f(40),
            pf(20, 3),
        ],
    );
}

#[test]
fn buy_matches_lowest_ask_before_older_worse_ask() {
    let mut e = Harness::new();
    e.add(1, Sell, 102, 2);
    e.add(2, Sell, 100, 2);
    e.add(3, Sell, 101, 2);
    e.add(4, Buy, 101, 5);
    expect(
        &mut e,
        &[t(100, 2), pf(4, 3), f(2), t(101, 2), pf(4, 1), f(3)],
    );
    // The ask beyond the buy limit must still be available.
    e.add(5, Buy, 102, 2);
    expect(&mut e, &[t(102, 2), f(5), f(1)]);
}

#[test]
fn sell_matches_highest_bid_before_older_worse_bid() {
    let mut e = Harness::new();
    e.add(1, Buy, 98, 2);
    e.add(2, Buy, 100, 2);
    e.add(3, Buy, 99, 2);
    e.add(4, Sell, 99, 5);
    expect(
        &mut e,
        &[t(100, 2), pf(4, 3), f(2), t(99, 2), pf(4, 1), f(3)],
    );
    e.add(5, Sell, 98, 2);
    expect(&mut e, &[t(98, 2), f(5), f(1)]);
}

#[test]
fn aggressive_buy_remainder_rests_at_its_own_price() {
    let mut e = Harness::new();
    e.add(1, Sell, 100, 2);
    e.add(2, Buy, 105, 5);
    expect(&mut e, &[t(100, 2), pf(2, 3), f(1)]);
    e.add(3, Sell, 104, 3);
    expect(&mut e, &[t(105, 3), f(3), f(2)]);
}

#[test]
fn aggressive_sell_remainder_rests_at_its_own_price() {
    let mut e = Harness::new();
    e.add(1, Buy, 105, 2);
    e.add(2, Sell, 100, 5);
    expect(&mut e, &[t(105, 2), pf(2, 3), f(1)]);
    e.add(3, Buy, 101, 3);
    expect(&mut e, &[t(100, 3), f(3), f(2)]);
}

#[test]
fn non_crossing_orders_remain_available() {
    let mut e = Harness::new();
    e.add(1, Buy, 99, 2);
    e.add(2, Sell, 101, 3);
    expect(&mut e, &[]);
    e.add(3, Buy, 101, 3);
    expect(&mut e, &[t(101, 3), f(3), f(2)]);
    e.add(4, Sell, 99, 2);
    expect(&mut e, &[t(99, 2), f(4), f(1)]);
}

#[test]
fn partially_filled_resting_order_keeps_priority() {
    let mut e = Harness::new();
    e.add(1, Sell, 100, 5);
    e.add(2, Sell, 100, 2);
    e.add(3, Buy, 100, 2);
    expect(&mut e, &[t(100, 2), f(3), pf(1, 3)]);
    e.add(4, Sell, 100, 2);
    e.add(5, Buy, 100, 4);
    expect(
        &mut e,
        &[t(100, 3), pf(5, 1), f(1), t(100, 1), f(5), pf(2, 1)],
    );
}

/// Three sells at one price, cancel `victim`, then a buy of 2 sweeps the rest.
fn cancel_among_three(victim: u64, want: &[LineEvent]) {
    let mut e = Harness::new();
    for id in 1..=3 {
        e.add(id, Sell, 100, 1);
    }
    assert!(e.cancel(victim));
    expect(&mut e, &[]);
    e.add(4, Buy, 100, 2);
    expect(&mut e, want);
}

#[test]
fn cancel_head_preserves_other_orders_and_priority() {
    cancel_among_three(1, &[t(100, 1), pf(4, 1), f(2), t(100, 1), f(4), f(3)]);
}

#[test]
fn cancel_middle_preserves_other_orders_and_priority() {
    cancel_among_three(2, &[t(100, 1), pf(4, 1), f(1), t(100, 1), f(4), f(3)]);
}

#[test]
fn cancel_tail_preserves_other_orders_and_priority() {
    cancel_among_three(3, &[t(100, 1), pf(4, 1), f(1), t(100, 1), f(4), f(2)]);
}

#[test]
fn cancel_last_ask_at_level_leaves_other_levels_usable() {
    let mut e = Harness::new();
    e.add(1, Sell, 100, 2);
    e.add(2, Sell, 101, 2);
    assert!(e.cancel(1));
    e.add(3, Buy, 101, 2);
    expect(&mut e, &[t(101, 2), f(3), f(2)]);
    e.add(4, Sell, 100, 1);
    e.add(5, Buy, 100, 1);
    expect(&mut e, &[t(100, 1), f(5), f(4)]);
}

#[test]
fn cancel_last_bid_at_level_leaves_other_levels_usable() {
    let mut e = Harness::new();
    e.add(1, Buy, 100, 2);
    e.add(2, Buy, 99, 2);
    assert!(e.cancel(1));
    e.add(3, Sell, 99, 2);
    expect(&mut e, &[t(99, 2), f(3), f(2)]);
    e.add(4, Buy, 100, 1);
    e.add(5, Sell, 100, 1);
    expect(&mut e, &[t(100, 1), f(5), f(4)]);
}

#[test]
fn cancel_removes_only_remaining_quantity() {
    let mut e = Harness::new();
    e.add(1, Sell, 100, 5);
    e.add(2, Buy, 100, 2);
    expect(&mut e, &[t(100, 2), f(2), pf(1, 3)]);
    assert!(e.cancel(1));
    e.add(3, Sell, 100, 1);
    e.add(4, Buy, 100, 1);
    expect(&mut e, &[t(100, 1), f(4), f(3)]);
}

#[test]
fn unknown_cancel_is_reported_without_changing_book() {
    let mut e = Harness::new();
    e.add(1, Sell, 100, 2);
    assert!(!e.cancel(99));
    expect(&mut e, &[]);
    e.add(2, Buy, 100, 2);
    expect(&mut e, &[t(100, 2), f(2), f(1)]);
}

#[test]
fn fully_filled_orders_are_unavailable_to_cancel_or_match() {
    let mut e = Harness::new();
    e.add(1, Sell, 100, 2);
    e.add(2, Buy, 100, 2);
    expect(&mut e, &[t(100, 2), f(2), f(1)]);
    assert!(!e.cancel(1));
    assert!(!e.cancel(2));
    e.add(3, Buy, 100, 1);
    expect(&mut e, &[]);
    e.add(4, Sell, 100, 1);
    expect(&mut e, &[t(100, 1), f(4), f(3)]);
}

#[test]
fn slab_slots_are_reused_after_removal() {
    let mut e = Harness::new();
    for round in 0..100u64 {
        let id = round * 2 + 1;
        e.add(id, Sell, 100, 1);
        e.add(id + 1, Buy, 100, 1);
        expect(&mut e, &[t(100, 1), f(id + 1), f(id)]);
    }
}
