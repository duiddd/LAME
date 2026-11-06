//! Market-data snapshot (D6): best three levels per side with aggregated
//! quantity, last trade price and the day's traded volume.

use match_engine::{
    BookLevel, CancelTarget, Command, MatchEngine, NewOrder, Side, Snapshot, SymbolSpec, DEPTH,
};

fn engine() -> MatchEngine {
    MatchEngine::new(SymbolSpec::UNIT)
}

fn place(e: &mut MatchEngine, seq: u64, user: u64, side: Side, price: Option<i64>, qty: i64) {
    e.process(
        seq,
        0,
        Command::New(NewOrder {
            order_id: seq,
            user_id: user,
            side,
            price,
            qty,
            sent_at: 0,
        }),
    );
}

fn place_at(
    e: &mut MatchEngine,
    seq: u64,
    ts: i64,
    user: u64,
    side: Side,
    price: Option<i64>,
    qty: i64,
) {
    e.process(
        seq,
        ts,
        Command::New(NewOrder {
            order_id: seq,
            user_id: user,
            side,
            price,
            qty,
            sent_at: ts,
        }),
    );
}

fn cancel(e: &mut MatchEngine, seq: u64, target: u64) {
    e.process(seq, 0, Command::Cancel(CancelTarget::BySeq(target)));
}

fn lv(price: i64, qty: i64) -> Option<BookLevel> {
    Some(BookLevel { price, qty })
}

fn levels(l: &[Option<BookLevel>]) -> [Option<BookLevel>; DEPTH] {
    let mut out = [None; DEPTH];
    out[..l.len()].copy_from_slice(l);
    out
}

#[test]
fn empty_book() {
    let e = engine();
    assert_eq!(
        e.snapshot(),
        Snapshot {
            bids: [None; DEPTH],
            asks: [None; DEPTH],
            last_price: None,
            volume: 0,
            last_ms: 0,
        }
    );
}

#[test]
fn fewer_than_three_levels_leaves_the_rest_empty() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Buy, Some(9), 4);
    place(&mut e, 2, 2, Side::Sell, Some(11), 5);
    place(&mut e, 3, 3, Side::Sell, Some(12), 6);
    let s = e.snapshot();
    assert_eq!(s.bids, levels(&[lv(9, 4)]));
    assert_eq!(s.asks, levels(&[lv(11, 5), lv(12, 6)]));
}

#[test]
fn only_the_best_three_levels_are_reported_best_first() {
    let mut e = engine();
    for (i, p) in [5, 9, 7, 8, 6].into_iter().enumerate() {
        place(&mut e, i as u64 + 1, 1, Side::Buy, Some(p), 1);
    }
    for (i, p) in [20, 14, 18, 12, 16].into_iter().enumerate() {
        place(&mut e, i as u64 + 10, 2, Side::Sell, Some(p), 1);
    }
    let s = e.snapshot();
    assert_eq!(s.bids, levels(&[lv(9, 1), lv(8, 1), lv(7, 1)]));
    assert_eq!(s.asks, levels(&[lv(12, 1), lv(14, 1), lv(16, 1)]));
}

#[test]
fn quantity_is_aggregated_per_price() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, Some(10), 2);
    place(&mut e, 2, 2, Side::Sell, Some(10), 3);
    place(&mut e, 3, 3, Side::Sell, Some(10), 4);
    assert_eq!(e.snapshot().asks, levels(&[lv(10, 9)]));
}

#[test]
fn partial_fill_reduces_the_level_and_sets_last_price_and_volume() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, Some(10), 5);
    place(&mut e, 2, 2, Side::Buy, Some(10), 2);
    let s = e.snapshot();
    assert_eq!(s.asks, levels(&[lv(10, 3)]));
    assert_eq!(s.bids, [None; DEPTH]);
    assert_eq!((s.last_price, s.volume), (Some(10), 2));
}

#[test]
fn exhausted_level_disappears_and_the_next_one_moves_up() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, Some(10), 2);
    place(&mut e, 2, 2, Side::Sell, Some(11), 3);
    place(&mut e, 3, 3, Side::Buy, Some(10), 2);
    assert_eq!(e.snapshot().asks, levels(&[lv(11, 3)]));
}

#[test]
fn sweep_across_levels_reports_the_last_price_and_total_volume() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, Some(10), 2);
    place(&mut e, 2, 2, Side::Sell, Some(11), 2);
    place(&mut e, 3, 3, Side::Sell, Some(12), 2);
    place(&mut e, 4, 4, Side::Buy, None, 5); // market: 2@10, 2@11, 1@12
    let s = e.snapshot();
    assert_eq!(s.asks, levels(&[lv(12, 1)]));
    assert_eq!((s.last_price, s.volume), (Some(12), 5));
}

#[test]
fn volume_counts_size_not_currency() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, Some(1000), 3);
    place(&mut e, 2, 2, Side::Buy, Some(1000), 3);
    assert_eq!(e.snapshot().volume, 3); // not 3000
}

#[test]
fn cancels_update_levels_at_head_middle_and_tail() {
    let mut e = engine();
    for seq in 1..=5 {
        place(&mut e, seq, seq, Side::Buy, Some(10), seq as i64); // 1+2+3+4+5
    }
    assert_eq!(e.snapshot().bids, levels(&[lv(10, 15)]));
    cancel(&mut e, 6, 1); // head
    assert_eq!(e.snapshot().bids, levels(&[lv(10, 14)]));
    cancel(&mut e, 7, 5); // tail
    assert_eq!(e.snapshot().bids, levels(&[lv(10, 9)]));
    cancel(&mut e, 8, 3); // middle
    assert_eq!(e.snapshot().bids, levels(&[lv(10, 6)]));
    cancel(&mut e, 9, 2);
    cancel(&mut e, 10, 4);
    assert_eq!(e.snapshot().bids, [None; DEPTH]);
}

#[test]
fn cancel_after_partial_fill_removes_only_the_remainder() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, Some(10), 5);
    place(&mut e, 2, 2, Side::Buy, Some(10), 2);
    cancel(&mut e, 3, 1);
    let s = e.snapshot();
    assert_eq!(s.asks, [None; DEPTH]);
    assert_eq!((s.last_price, s.volume), (Some(10), 2)); // history stands
}

#[test]
fn rests_after_matching_at_the_aggressors_own_price() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, Some(10), 2);
    place(&mut e, 2, 2, Side::Buy, Some(12), 5); // 2 fill, 3 rest at 12
    let s = e.snapshot();
    assert_eq!(s.bids, levels(&[lv(12, 3)]));
    assert_eq!(s.asks, [None; DEPTH]);
}

#[test]
fn rejections_and_market_remainders_do_not_touch_the_snapshot() {
    let mut e = MatchEngine::new(SymbolSpec::new(5, 10));
    place(&mut e, 1, 1, Side::Sell, Some(100), 20);
    place(&mut e, 2, 2, Side::Buy, Some(95), 10);
    let before = e.snapshot();

    place(&mut e, 3, 3, Side::Buy, Some(101), 10); // off tick
    place(&mut e, 4, 4, Side::Buy, Some(100), 15); // off lot
    place(&mut e, 5, 1, Side::Buy, Some(100), 10); // self-trade, rejected
    cancel(&mut e, 6, 99); // unknown
    assert_eq!(e.snapshot(), before);

    // A market order that exhausts liquidity leaves no remainder in the book.
    place(&mut e, 7, 5, Side::Buy, None, 30);
    let s = e.snapshot();
    assert_eq!(s.asks, [None; DEPTH]);
    assert_eq!(s.bids, levels(&[lv(95, 10)]));
    assert_eq!((s.last_price, s.volume), (Some(100), 20));
}

#[test]
fn day_close_clears_levels_last_price_and_volume() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, Some(10), 5);
    place(&mut e, 2, 2, Side::Buy, Some(10), 2);
    place(&mut e, 3, 3, Side::Buy, Some(9), 4);
    e.process(4, 0, Command::DayClose);
    assert!(e.snapshot().same_view(&engine().snapshot()));
}

#[test]
fn identical_commands_give_identical_snapshots() {
    let run = || {
        let mut e = engine();
        place(&mut e, 1, 1, Side::Sell, Some(10), 5);
        place(&mut e, 2, 2, Side::Buy, Some(10), 2);
        place(&mut e, 3, 3, Side::Buy, Some(9), 4);
        cancel(&mut e, 4, 3);
        e.snapshot()
    };
    assert_eq!(run(), run());
}

#[test]
fn into_market_data_lists_only_existing_levels_and_serializes_to_json() {
    let mut e = engine();
    place(&mut e, 1, 1, Side::Sell, Some(10), 5);
    place(&mut e, 2, 2, Side::Buy, Some(10), 2);
    place(&mut e, 3, 3, Side::Buy, Some(9), 4);
    place_at(&mut e, 4, 1_700_000_000_123, 4, Side::Buy, Some(8), 1);
    cancel(&mut e, 5, 4); // generates one more snapshot at +1 ms
    let msg = e.snapshot().into_market_data();
    assert_eq!(msg.bids, [BookLevel { price: 9, qty: 4 }]);
    assert_eq!(msg.asks, [BookLevel { price: 10, qty: 3 }]);
    assert_eq!(
        serde_json::to_value(&msg).unwrap(),
        serde_json::json!({
            "bids": [{"price": 9, "qty": 4}],
            "asks": [{"price": 10, "qty": 3}],
            "last_price": 10,
            "volume": 2,
            "last_ms": 1_700_000_000_124_i64,
        })
    );
    // No trade yet: last_price is null.
    let fresh = engine().snapshot().into_market_data();
    assert_eq!(
        serde_json::to_value(&fresh).unwrap()["last_price"],
        serde_json::Value::Null
    );
}

#[test]
fn last_ms_is_the_ts_of_the_command_that_generated_the_snapshot() {
    let mut e = engine();
    assert_eq!(e.snapshot().last_ms, 0);
    place_at(&mut e, 1, 1_000, 1, Side::Buy, Some(10), 1);
    assert_eq!(e.snapshot().last_ms, 1_000);
    place_at(&mut e, 2, 1_250, 2, Side::Sell, Some(11), 1);
    assert_eq!(e.snapshot().last_ms, 1_250);
}

#[test]
fn last_ms_is_strictly_increasing_for_same_ms_and_backwards_ts() {
    let mut e = engine();
    place_at(&mut e, 1, 5_000, 1, Side::Buy, Some(10), 1);
    place_at(&mut e, 2, 5_000, 2, Side::Buy, Some(9), 1); // same ms
    assert_eq!(e.snapshot().last_ms, 5_001);
    place_at(&mut e, 3, 4_000, 3, Side::Buy, Some(8), 1); // clock went back
    assert_eq!(e.snapshot().last_ms, 5_002);
    place_at(&mut e, 4, 9_000, 4, Side::Buy, Some(7), 1);
    assert_eq!(e.snapshot().last_ms, 9_000);
}

#[test]
fn commands_that_change_nothing_do_not_generate_a_snapshot() {
    let mut e = MatchEngine::new(SymbolSpec::new(5, 10));
    place_at(&mut e, 1, 1_000, 1, Side::Sell, Some(100), 20);
    let before = e.snapshot();
    assert_eq!(before.last_ms, 1_000);

    place_at(&mut e, 2, 2_000, 2, Side::Buy, Some(101), 10); // off tick
    place_at(&mut e, 3, 2_001, 3, Side::Buy, Some(100), 15); // off lot
    place_at(&mut e, 4, 2_002, 1, Side::Buy, Some(100), 10); // self-trade
    e.process(5, 2_003, Command::Cancel(CancelTarget::BySeq(99))); // unknown
    assert_eq!(e.snapshot(), before);
}

#[test]
fn day_close_generates_a_snapshot_and_last_ms_keeps_rising_across_it() {
    let mut e = engine();
    place_at(&mut e, 1, 10_000, 1, Side::Buy, Some(10), 1);
    e.process(2, 10_500, Command::DayClose);
    assert_eq!(e.snapshot().last_ms, 10_500);
    // Next day: seq restarts but the timestamp does not go back.
    place_at(&mut e, 1, 20_000, 1, Side::Buy, Some(10), 1);
    assert_eq!(e.snapshot().last_ms, 20_000);
}

#[test]
fn last_ms_depends_only_on_the_commands() {
    let run = || {
        let mut e = engine();
        place_at(&mut e, 1, 100, 1, Side::Sell, Some(10), 5);
        place_at(&mut e, 2, 100, 2, Side::Buy, Some(10), 2);
        cancel(&mut e, 3, 1);
        e.snapshot()
    };
    assert_eq!(run(), run());
}
