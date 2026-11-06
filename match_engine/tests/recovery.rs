//! Start-up recovery: replay of the day's store, comparison with the stored
//! outputs, and what happens when they disagree.

mod support;

use std::path::Path;

use match_engine::runtime::JournalMsg;
use match_engine::service::RunningService;
use match_engine::store::{db_path, EventStore, Kind};
use serde_json::{json, Value};
use support::*;

const NEXT_DAY: i64 = IN_SESSION + 24 * 3_600_000;

/// Places a resting sell of 5 and a crossing buy of 3 on AAA and returns the
/// five outputs, with the two commands strictly in order so the stored
/// numbering is always: 1 command, 2 accepted, 3 command, 4 accepted, 5 trade,
/// 6 filled, 7 partially filled.
async fn trade_once(svc: &RunningService) -> Vec<Value> {
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "SELL", 50, 5, IN_SESSION))
        .await;
    let mut out = d.recv_n(1).await;
    d.send(limit("AAA", 2, 200, "BUY", 50, 3, IN_SESSION)).await;
    out.extend(d.recv_n(4).await);
    out
}

fn db(dir: &Path, day: &str, f: impl FnOnce(&rusqlite::Connection)) {
    let conn = rusqlite::Connection::open(db_path(dir, day)).unwrap();
    f(&conn);
}

async fn start_error(dir: &Path, clock: &TestClock) -> String {
    match match_engine::service::start(config(dir), clock.clock()).await {
        Ok(_) => panic!("start-up should have been refused"),
        Err(e) => format!("{e:#}"),
    }
}

fn kinds(dir: &Path, day: &str) -> Vec<Kind> {
    let store = EventStore::open(dir, day).unwrap();
    let mut out = Vec::new();
    store
        .scan(0, |r| {
            out.push(r.kind);
            Ok(())
        })
        .unwrap();
    out
}

#[tokio::test]
async fn a_restart_restores_the_book_and_continues_the_numbering() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let before = trade_once(&svc).await;
    let mut md = MdClient::connect(svc.md_addr, "AAA").await;
    let snapshot_before = md.recv_until(|s| s["volume"] == 3).await;
    svc.shutdown().await.unwrap();

    // A minute later: replay must use the stored timestamps, not this clock.
    clock.set(IN_SESSION + 60_000);
    let svc = start(config(dir.path()), &clock).await;
    assert_eq!(
        svc.state.metrics.replay_records.get(),
        7,
        "every stored record was replayed"
    );

    // The market data is exactly what it was, `last_ms` included.
    let mut md = MdClient::connect(svc.md_addr, "AAA").await;
    assert_eq!(md.recv().await.unwrap(), snapshot_before);

    // The surviving 2 of the sell can still be hit, and numbering carries on.
    let last = before[4]["seq"].as_u64().unwrap();
    let mut d = Client::connect(svc.ctrl_addr, last).await;
    d.send(limit("AAA", 3, 300, "BUY", 50, 2, IN_SESSION + 60_000))
        .await;
    let out = d.recv_n(4).await;
    assert_eq!(statuses(&out), ["ACCEPTED", "TRADE", "FILLED", "FILLED"]);
    assert_eq!(out[1]["maker_user_id"], 100);
    assert_eq!(out[1]["maker_seq"], 1);
    assert_eq!(out[0]["prev_seq"], last, "the chain of outputs continues");
    assert_eq!(out[0]["seq"], 9, "seq 8 is the new command");
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn after_a_restart_d_can_resume_and_gets_the_same_records() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let before = trade_once(&svc).await;
    svc.shutdown().await.unwrap();

    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    assert_eq!(d.recv_n(5).await, before);
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn outputs_a_crash_left_unwritten_are_written_on_start() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let before = trade_once(&svc).await;
    svc.shutdown().await.unwrap();

    // The process died after journaling the second command and its first
    // output, before the rest of that command's outputs reached the store.
    db(dir.path(), DAY, |c| {
        c.execute("DELETE FROM events WHERE seq > 4", []).unwrap();
    });

    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 4).await;
    // Exactly what would have been written, with the same numbering.
    assert_eq!(d.recv_n(3).await, before[2..]);
    // The book is right too: 2 of the sell remain.
    d.send(limit("AAA", 3, 300, "BUY", 50, 2, IN_SESSION)).await;
    let out = d.recv_n(4).await;
    assert_eq!(statuses(&out), ["ACCEPTED", "TRADE", "FILLED", "FILLED"]);
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn a_stored_output_that_replay_does_not_reproduce_stops_start_up() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    trade_once(&svc).await;
    svc.shutdown().await.unwrap();

    db(dir.path(), DAY, |c| {
        c.execute(
            "UPDATE events SET payload = replace(payload, '\"remaining\":5', '\"remaining\":6') WHERE seq = 2",
            [],
        )
        .unwrap();
    });
    let error = start_error(dir.path(), &clock).await;
    assert!(
        error.contains("replay mismatch at seq 2 for AAA"),
        "{error}"
    );
}

#[tokio::test]
async fn an_output_with_no_command_behind_it_stops_start_up() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    trade_once(&svc).await;
    svc.shutdown().await.unwrap();

    db(dir.path(), DAY, |c| {
        c.execute("DELETE FROM events WHERE kind = 'NewOrder'", [])
            .unwrap();
    });
    let error = start_error(dir.path(), &clock).await;
    assert!(error.contains("replay mismatch"), "{error}");
    assert!(error.contains("does not produce"), "{error}");
}

#[tokio::test]
async fn a_broken_chain_of_outputs_stops_start_up() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    trade_once(&svc).await;
    svc.shutdown().await.unwrap();

    db(dir.path(), DAY, |c| {
        c.execute("UPDATE events SET prev_seq = 99 WHERE seq = 4", [])
            .unwrap();
    });
    let error = start_error(dir.path(), &clock).await;
    assert!(
        error.contains("chain of outputs is broken at seq 4"),
        "{error}"
    );
}

#[tokio::test]
async fn commands_for_a_pair_that_is_no_longer_configured_stop_start_up() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("BBB", 1, 1, "BUY", 50, 10, IN_SESSION)).await;
    d.recv_n(1).await;
    svc.shutdown().await.unwrap();

    let mut cfg = config(dir.path());
    cfg.symbols.retain(|(name, _)| name == "AAA");
    let error = match match_engine::service::start(cfg, clock.clock()).await {
        Ok(_) => panic!("start-up should have been refused"),
        Err(e) => format!("{e:#}"),
    };
    assert!(error.contains("not a configured pair"), "{error}");
}

#[tokio::test]
async fn a_second_instance_on_the_same_directory_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let error = start_error(dir.path(), &clock).await;
    assert!(error.contains("another instance"), "{error}");
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn starting_after_the_close_closes_the_day_and_cancels_the_orders() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "SELL", 60, 5, IN_SESSION))
        .await;
    d.send(limit("AAA", 2, 200, "BUY", 40, 7, IN_SESSION)).await;
    let before = d.recv_n(2).await;
    svc.shutdown().await.unwrap();

    // B was down over the close.
    clock.set(AFTER_CLOSE);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, before[1]["seq"].as_u64().unwrap()).await;
    let out = d.recv_n(2).await;
    assert_eq!(statuses(&out), ["CANCELLED", "CANCELLED"]);
    assert!(out.iter().all(|r| r["reason"] == "END_OF_DAY"));
    svc.shutdown().await.unwrap();

    // Starting again must not close the day a second time, and the replayed
    // close and its cancels must agree with what was stored.
    let svc = start(config(dir.path()), &clock).await;
    svc.shutdown().await.unwrap();
    assert_eq!(
        kinds(dir.path(), DAY)
            .iter()
            .filter(|k| **k == Kind::DayClose)
            .count(),
        1
    );
}

#[tokio::test]
async fn a_day_that_was_never_closed_is_closed_before_the_next_one_starts() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "BUY", 50, 5, IN_SESSION)).await;
    d.recv_n(1).await;

    // No timer ran; the next morning's first order arrives.
    clock.set(NEXT_DAY);
    d.send(limit("AAA", 2, 100, "BUY", 50, 5, NEXT_DAY)).await;
    let out = d.recv_n(2).await;
    assert_eq!(statuses(&out), ["CANCELLED", "ACCEPTED"]);
    assert_eq!(
        out[0]["day"], DAY,
        "yesterday's cancel is yesterday's record"
    );
    assert_eq!(out[1]["day"], "2026-11-05");
    svc.shutdown().await.unwrap();

    assert!(kinds(dir.path(), DAY).contains(&Kind::DayClose));
    assert_eq!(*kinds(dir.path(), DAY).last().unwrap(), Kind::OrderEvent);
    assert_eq!(
        kinds(dir.path(), "2026-11-05"),
        [Kind::NewOrder, Kind::OrderEvent]
    );

    // And both days replay cleanly.
    let svc = start(config(dir.path()), &clock).await;
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn commands_waiting_for_the_next_day_keep_their_order() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "BUY", 50, 5, IN_SESSION)).await;
    d.recv_n(1).await;

    clock.set(NEXT_DAY);
    for i in 2..=6u64 {
        d.send(limit("AAA", i, 100 + i, "BUY", 40, 1, NEXT_DAY))
            .await;
    }
    let out = d.recv_n(6).await;
    let accepted: Vec<u64> = out
        .iter()
        .filter(|r| r["status"] == "ACCEPTED")
        .map(|r| r["order_id"].as_u64().unwrap())
        .collect();
    assert_eq!(accepted, [2, 3, 4, 5, 6]);
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn the_journal_can_be_told_to_check_the_close_at_any_time() {
    // Sending CloseCheck before the close does nothing.
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "BUY", 50, 5, IN_SESSION)).await;
    d.recv_n(1).await;
    svc.state.journal.send(JournalMsg::CloseCheck);
    d.send(json!({"type": "cancel_order", "symbol": "AAA", "user_id": 100, "order_id": 1}))
        .await;
    assert_eq!(d.recv().await.unwrap()["status"], "CANCELLED");
    assert!(!kinds(dir.path(), DAY).contains(&Kind::DayClose));
    svc.shutdown().await.unwrap();
}
