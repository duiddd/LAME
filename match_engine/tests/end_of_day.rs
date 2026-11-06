//! End of day through the whole service: the timer's check, the journal's
//! `DayClose`, the engines' cancels, rejection after the close, and rollover to
//! the next day's store.

mod support;

use match_engine::eod::delay_until_close;
use match_engine::session;
use match_engine::store::{EventStore, Kind};
use serde_json::json;
use support::*;

/// 22:59 on the day, one minute before the close.
const JUST_BEFORE_CLOSE: i64 = IN_SESSION + 12 * 3_600_000 + 59 * 60_000;
/// 10:00 the next day.
const NEXT_DAY: i64 = IN_SESSION + 24 * 3_600_000;

fn kinds_in(dir: &std::path::Path, day: &str) -> Vec<Kind> {
    let store = EventStore::open(dir, day).unwrap();
    let mut kinds = Vec::new();
    store
        .scan(0, |r| {
            kinds.push(r.kind);
            Ok(())
        })
        .unwrap();
    kinds
}

#[test]
fn the_timer_waits_until_the_next_twenty_three_hundred() {
    // 10:00 UTC in winter is 10:00 Dublin: thirteen hours to go.
    assert_eq!(delay_until_close(IN_SESSION).as_millis(), 13 * 3_600_000);
    // A hair before: a millisecond.
    assert_eq!(
        delay_until_close(IN_SESSION + 13 * 3_600_000 - 1).as_millis(),
        1
    );
    // At the close itself: tomorrow's.
    let close = session::next_close(IN_SESSION);
    assert_eq!(delay_until_close(close).as_millis(), 24 * 3_600_000);
}

#[tokio::test]
async fn at_the_close_every_resting_order_is_cancelled_in_seq_order() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    let mut md = MdClient::connect(svc.md_addr, "AAA").await;
    md.recv().await;

    d.send(limit("AAA", 1, 100, "SELL", 60, 5, IN_SESSION))
        .await;
    d.send(limit("AAA", 2, 200, "BUY", 40, 7, IN_SESSION)).await;
    d.send(limit("AAA", 3, 300, "SELL", 55, 2, IN_SESSION))
        .await;
    d.recv_n(3).await;
    md.recv_until(|s| s["asks"].as_array().is_some_and(|a| a.len() == 2))
        .await;

    clock.set(AFTER_CLOSE);
    svc.state
        .journal
        .send(match_engine::runtime::JournalMsg::CloseCheck);
    let out = d.recv_n(3).await;

    // In order of the orders' seq, not of price.
    assert_eq!(statuses(&out), ["CANCELLED", "CANCELLED", "CANCELLED"]);
    assert_eq!(
        out.iter()
            .map(|r| r["order_id"].as_u64().unwrap())
            .collect::<Vec<_>>(),
        [1, 2, 3]
    );
    assert!(out.iter().all(|r| r["reason"] == "END_OF_DAY"));
    assert_eq!(out[1]["remaining"], 7);
    assert_chain(&out);

    // The book is gone from the market data too.
    md.recv_until(|s| s["asks"] == json!([]) && s["bids"] == json!([]))
        .await;
    svc.shutdown().await.unwrap();

    // DayClose is in the store, after the commands and before the cancels.
    let kinds = kinds_in(dir.path(), DAY);
    let close_at = kinds.iter().position(|k| *k == Kind::DayClose).unwrap();
    assert_eq!(kinds.iter().filter(|k| **k == Kind::DayClose).count(), 1);
    let cancels_after: usize = kinds[close_at..].iter().filter(|k| k.is_output()).count();
    assert_eq!(cancels_after, 3);
}

#[tokio::test]
async fn new_orders_after_the_close_are_rejected_as_end_of_day() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(AFTER_CLOSE);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "BUY", 50, 5, AFTER_CLOSE))
        .await;
    let out = d.recv().await.unwrap();
    assert_eq!(out["status"], "REJECTED");
    assert_eq!(out["reason"], "END_OF_DAY");
    assert_eq!(out["remaining"], 5);

    // A cancel is not an order: it just finds nothing.
    d.send(cancel_by_id("AAA", 100, 1)).await;
    assert_eq!(d.recv().await.unwrap()["reason"], "ORDER_NOT_FOUND");
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn the_session_closes_at_23_00_and_the_new_day_opens_at_midnight() {
    let dir = tempfile::tempdir().unwrap();
    // 23:59:59.999 on the day: closed. 00:00:00.000 the next day: open again.
    let last_ms_of_day = IN_SESSION + 14 * 3_600_000 - 1;
    let midnight = IN_SESSION + 14 * 3_600_000;
    let clock = TestClock::at(last_ms_of_day);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "BUY", 50, 5, last_ms_of_day))
        .await;
    let rejected = d.recv().await.unwrap();
    assert_eq!(rejected["reason"], "END_OF_DAY");
    assert_eq!(rejected["day"], DAY);

    clock.set(midnight);
    d.send(limit("AAA", 2, 100, "BUY", 50, 5, midnight)).await;
    let accepted = d.recv().await.unwrap();
    assert_eq!(accepted["status"], "ACCEPTED");
    assert_eq!(accepted["day"], "2026-11-05");
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn a_command_after_the_close_closes_the_day_first_even_without_the_timer() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(JUST_BEFORE_CLOSE);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "BUY", 50, 5, JUST_BEFORE_CLOSE))
        .await;
    assert_eq!(d.recv().await.unwrap()["status"], "ACCEPTED");

    clock.set(AFTER_CLOSE);
    d.send(limit("AAA", 2, 200, "SELL", 50, 5, AFTER_CLOSE))
        .await;
    let out = d.recv_n(2).await;
    // The resting buy is cancelled at the close; the late sell is rejected, so
    // nothing trades.
    assert_eq!(statuses(&out), ["CANCELLED", "REJECTED"]);
    assert_eq!(out[0]["reason"], "END_OF_DAY");
    assert_eq!(out[1]["reason"], "END_OF_DAY");
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn the_close_is_written_only_once() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(AFTER_CLOSE);
    let svc = start(config(dir.path()), &clock).await; // start already checked once
    for _ in 0..3 {
        svc.state
            .journal
            .send(match_engine::runtime::JournalMsg::CloseCheck);
    }
    svc.shutdown().await.unwrap();
    let kinds = kinds_in(dir.path(), DAY);
    assert_eq!(kinds.iter().filter(|k| **k == Kind::DayClose).count(), 1);
}

#[tokio::test]
async fn the_next_day_gets_its_own_store_and_seq_restarts_at_one() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "BUY", 50, 5, IN_SESSION)).await;
    d.recv_n(1).await;
    clock.set(AFTER_CLOSE);
    svc.state
        .journal
        .send(match_engine::runtime::JournalMsg::CloseCheck);
    d.recv_n(1).await; // the end-of-day cancel

    // Next morning.
    clock.set(NEXT_DAY);
    d.send(limit("AAA", 1, 100, "BUY", 50, 5, NEXT_DAY)).await; // same ids are fine
    let out = d.recv().await.unwrap();
    assert_eq!(out["status"], "ACCEPTED");
    assert_eq!(out["day"], "2026-11-05");
    // The command took seq 1 of the new day; its output is seq 2.
    assert_eq!(out["seq"], 2);
    assert_eq!(out["prev_seq"], 0);
    svc.shutdown().await.unwrap();

    assert_eq!(
        kinds_in(dir.path(), "2026-11-05"),
        [Kind::NewOrder, Kind::OrderEvent]
    );
    assert!(kinds_in(dir.path(), DAY).contains(&Kind::DayClose));
}

#[tokio::test]
async fn resume_only_covers_the_current_day() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "BUY", 50, 5, IN_SESSION)).await;
    d.recv_n(1).await;
    clock.set(AFTER_CLOSE);
    svc.state
        .journal
        .send(match_engine::runtime::JournalMsg::CloseCheck);
    d.recv_n(1).await; // the end-of-day cancel
    clock.set(NEXT_DAY);
    d.send(limit("AAA", 2, 100, "BUY", 50, 5, NEXT_DAY)).await;
    d.recv_n(1).await; // today's accept

    let mut fresh = Client::connect(svc.ctrl_addr, 0).await;
    let only = fresh.recv().await.unwrap();
    assert_eq!(only["day"], "2026-11-05");
    assert_eq!(only["order_id"], 2);
    svc.shutdown().await.unwrap();
}
