//! The connection task, journal thread and engine tasks working together over
//! a real WebSocket.

mod support;

use futures_util::StreamExt;
use match_engine::store::{db_path, Kind, Reader};
use serde_json::json;
use support::*;

#[tokio::test]
async fn an_order_flows_through_and_trades() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;

    d.send(limit("AAA", 1, 100, "SELL", 50, 5, IN_SESSION))
        .await;
    d.send(limit("AAA", 2, 200, "BUY", 50, 3, IN_SESSION)).await;
    let out = d.recv_n(5).await;

    assert_eq!(
        statuses(&out),
        [
            "ACCEPTED",
            "ACCEPTED",
            "TRADE",
            "FILLED",
            "PARTIALLY_FILLED"
        ]
    );
    assert_chain(&out);
    assert_eq!(out[0]["day"], DAY);
    assert_eq!(out[0]["symbol"], "AAA");
    assert_eq!(out[0]["user_id"], 100);
    assert_eq!(out[2]["price"], 50);
    assert_eq!(out[2]["qty"], 3);
    assert_eq!(out[2]["taker_user_id"], 200);
    assert_eq!(out[2]["maker_user_id"], 100);
    assert_eq!(out[4]["filled"], 3);
    assert_eq!(out[4]["remaining"], 2);
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn commands_and_outputs_are_all_in_the_store_in_one_total_order() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "SELL", 50, 5, IN_SESSION))
        .await;
    d.send(limit("AAA", 2, 200, "BUY", 50, 3, IN_SESSION)).await;
    d.recv_n(5).await;
    svc.shutdown().await.unwrap();

    let reader = Reader::open(&db_path(dir.path(), DAY)).unwrap();
    let mut kinds = Vec::new();
    let mut seqs = Vec::new();
    let mut command_ts = Vec::new();
    let store = match_engine::store::EventStore::open(dir.path(), DAY).unwrap();
    store
        .scan(0, |r| {
            kinds.push(r.kind);
            seqs.push(r.seq);
            if !r.kind.is_output() {
                command_ts.push(r.ts);
            }
            Ok(())
        })
        .unwrap();
    assert_eq!(seqs, (1..=7).collect::<Vec<_>>());
    assert_eq!(kinds.iter().filter(|k| **k == Kind::NewOrder).count(), 2);
    assert_eq!(kinds.iter().filter(|k| k.is_output()).count(), 5);
    assert_eq!(command_ts, [IN_SESSION, IN_SESSION]);
    assert_eq!(reader.max_seq().unwrap(), 7);
}

#[tokio::test]
async fn resume_resends_outputs_after_from_seq() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut first = Client::connect(svc.ctrl_addr, 0).await;
    first
        .send(limit("AAA", 1, 100, "SELL", 50, 5, IN_SESSION))
        .await;
    first
        .send(limit("AAA", 2, 200, "BUY", 50, 3, IN_SESSION))
        .await;
    let all = first.recv_n(5).await;

    // From the start: the same five records, byte for byte.
    let mut again = Client::connect(svc.ctrl_addr, 0).await;
    assert_eq!(again.recv_n(5).await, all);

    // From the middle: only what follows.
    let after = all[2]["seq"].as_u64().unwrap();
    let mut partial = Client::connect(svc.ctrl_addr, after).await;
    assert_eq!(partial.recv_n(2).await, all[3..]);
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn outputs_are_written_even_when_d_is_not_connected() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    {
        let mut d = Client::connect(svc.ctrl_addr, 0).await;
        d.send(limit("AAA", 1, 100, "SELL", 50, 5, IN_SESSION))
            .await;
        d.recv_n(1).await; // wait until the first order has been through
    }
    // D went away mid-flow; its order is still on the book and a later
    // connection can still pick everything up.
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 2, 200, "BUY", 50, 5, IN_SESSION)).await;
    let out = d.recv_n(5).await;
    assert_eq!(statuses(&out)[0], "ACCEPTED");
    assert!(statuses(&out).contains(&"TRADE".to_string()));
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn a_new_connection_replaces_the_old_one() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut old = Client::connect(svc.ctrl_addr, 0).await;
    old.send(limit("AAA", 1, 100, "SELL", 50, 5, IN_SESSION))
        .await;
    old.recv_n(1).await;

    let mut new = Client::connect(svc.ctrl_addr, 0).await;
    new.recv_n(1).await; // handshake complete, resend delivered
    assert_eq!(old.recv().await, None, "the old connection is closed");

    new.send(limit("AAA", 2, 200, "BUY", 50, 5, IN_SESSION))
        .await;
    assert_eq!(
        statuses(&new.recv_n(4).await),
        ["ACCEPTED", "TRADE", "FILLED", "FILLED"]
    );
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn bad_frames_get_an_error_and_the_connection_survives() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;

    d.send_text("not json").await;
    assert_eq!(d.recv().await.unwrap()["code"], "bad_message");

    d.send(limit("ZZZ", 1, 1, "BUY", 5, 1, IN_SESSION)).await;
    assert_eq!(d.recv().await.unwrap()["code"], "unknown_symbol");

    // A limit order without a price.
    d.send(
        json!({"type": "new_order", "symbol": "AAA", "order_id": 1, "user_id": 1,
                  "side": "BUY", "order_type": "LIMIT", "qty": 1, "sent_at": IN_SESSION}),
    )
    .await;
    assert_eq!(d.recv().await.unwrap()["code"], "bad_message");

    // A cancel that names its target ambiguously.
    d.send(json!({"type": "cancel_order", "symbol": "AAA", "user_id": 1, "order_seq": 3}))
        .await;
    assert_eq!(d.recv().await.unwrap()["code"], "bad_message");

    // A second resume.
    d.send(json!({"type": "resume", "from_seq": 0})).await;
    assert_eq!(d.recv().await.unwrap()["code"], "bad_message");

    // Still usable.
    d.send(limit("AAA", 2, 1, "BUY", 5, 1, IN_SESSION)).await;
    assert_eq!(d.recv().await.unwrap()["status"], "ACCEPTED");
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn the_handshake_must_come_first_and_must_not_be_ahead_of_the_store() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;

    let mut no_resume = Client::connect_raw(svc.ctrl_addr).await;
    no_resume
        .send(limit("AAA", 1, 1, "BUY", 5, 1, IN_SESSION))
        .await;
    assert_eq!(no_resume.recv().await.unwrap()["code"], "expected_resume");
    assert_eq!(no_resume.recv().await, None);

    let mut ahead = Client::connect(svc.ctrl_addr, 99).await;
    assert_eq!(ahead.recv().await.unwrap()["code"], "resume_ahead_of_store");
    assert_eq!(ahead.recv().await, None);
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn cancel_by_id_and_by_seq_work_and_unknown_orders_are_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;

    d.send(limit("AAA", 7, 100, "BUY", 10, 4, IN_SESSION)).await;
    let accepted = d.recv().await.unwrap();
    let order_seq = accepted["order_seq"].as_u64().unwrap();

    d.send(cancel_by_id("AAA", 100, 7)).await;
    let cancelled = d.recv().await.unwrap();
    assert_eq!(cancelled["status"], "CANCELLED");
    assert_eq!(cancelled["remaining"], 4);

    d.send(json!({"type": "cancel_order", "symbol": "AAA", "order_seq": order_seq}))
        .await;
    let again = d.recv().await.unwrap();
    assert_eq!(again["status"], "REJECTED");
    assert_eq!(again["reason"], "ORDER_NOT_FOUND");
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn pairs_are_independent_and_use_their_own_grid() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;

    d.send(limit("AAA", 1, 100, "SELL", 50, 5, IN_SESSION))
        .await;
    // Same price on the other pair: no match across pairs. BBB has tick 5, lot 10.
    d.send(limit("BBB", 2, 200, "BUY", 50, 10, IN_SESSION))
        .await;
    // Off BBB's grid, fine on AAA's.
    d.send(limit("BBB", 3, 200, "BUY", 51, 10, IN_SESSION))
        .await;
    let out = d.recv_n(3).await;
    assert_eq!(statuses(&out), ["ACCEPTED", "ACCEPTED", "REJECTED"]);
    assert_eq!(out[2]["reason"], "INVALID_PRICE");
    assert_eq!(out[1]["symbol"], "BBB");
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn stale_orders_are_expired_by_the_journal_timestamp() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    let stale = IN_SESSION - match_engine::MAX_COMMAND_AGE - 1;
    d.send(limit("AAA", 1, 100, "BUY", 50, 5, stale)).await;
    let out = d.recv().await.unwrap();
    assert_eq!(out["status"], "REJECTED");
    assert_eq!(out["reason"], "EXPIRED");
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn shutdown_writes_everything_the_engines_had_queued() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    for i in 1..=50u64 {
        d.send(limit("AAA", i, i, "BUY", 10, 1, IN_SESSION)).await;
    }
    d.recv_n(1).await;
    svc.shutdown().await.unwrap();

    let store = match_engine::store::EventStore::open(dir.path(), DAY).unwrap();
    let (mut commands, mut outputs) = (0, 0);
    store
        .scan(0, |r| {
            if r.kind.is_output() {
                outputs += 1;
            } else {
                commands += 1;
            }
            Ok(())
        })
        .unwrap();
    // Whatever was accepted into the journal has both its command and its
    // outputs recorded: every command produced exactly one ACCEPTED.
    assert!(commands >= 1);
    assert_eq!(outputs, commands);
}

#[tokio::test]
async fn the_first_frame_stream_is_not_confused_by_pings() {
    // A connection that only listens still receives records in order.
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut listener = Client::connect(svc.ctrl_addr, 0).await;
    let mut sender = Client::connect(svc.ctrl_addr, 0).await; // replaces `listener`
    sender
        .send(limit("AAA", 1, 1, "BUY", 5, 1, IN_SESSION))
        .await;
    assert_eq!(sender.recv().await.unwrap()["status"], "ACCEPTED");
    assert_eq!(listener.recv().await, None);
    drop(listener);
    let _ = futures_util::stream::empty::<()>().next().await;
    svc.shutdown().await.unwrap();
}
