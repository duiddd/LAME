//! Resend after D has been away (D21): a long catch-up must not get in the way
//! of new commands or of the outputs they produce.

mod support;

use std::collections::HashSet;

use support::*;

const BACKLOG: u64 = 12_000;

/// Runs `BACKLOG` orders through a normally configured B, reading their
/// outputs as they come, and stops it. The store is left with `BACKLOG`
/// commands and `BACKLOG` outputs.
async fn build_backlog(dir: &std::path::Path, clock: &TestClock) {
    let svc = start(config(dir), clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    let mut id = 1;
    while id <= BACKLOG {
        let chunk = 100.min(BACKLOG - id + 1);
        for i in id..id + chunk {
            d.send(limit("AAA", i, i, "BUY", 10, 1, IN_SESSION)).await;
        }
        d.recv_n(chunk as usize).await;
        id += chunk;
    }
    drop(d);
    svc.shutdown().await.unwrap();
}

/// B started again with a queue far smaller than the backlog, so the resend
/// alone could fill it.
async fn restart_with_small_queue(
    dir: &std::path::Path,
    clock: &TestClock,
) -> match_engine::service::RunningService {
    let mut cfg = config(dir);
    cfg.outbound_queue_cap = 8;
    start(cfg, clock).await
}

#[tokio::test]
async fn a_long_resend_does_not_starve_new_outputs() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    build_backlog(dir.path(), &clock).await;
    let svc = restart_with_small_queue(dir.path(), &clock).await;

    // D comes back from the start and, for now, reads nothing: the resend
    // backs up behind it. A new order is sent in the meantime.
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit(
        "AAA",
        BACKLOG + 1,
        BACKLOG + 1,
        "BUY",
        10,
        1,
        IN_SESSION,
    ))
    .await;

    // Now D reads. The connection must survive, and everything must arrive:
    // the whole backlog and the new order's output.
    let mut orders = HashSet::new();
    while orders.len() < BACKLOG as usize + 1 {
        let r = d
            .recv()
            .await
            .expect("the connection was closed while D was catching up");
        orders.insert(r["order_id"].as_u64().unwrap());
    }
    assert!(orders.contains(&(BACKLOG + 1)));
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn new_commands_are_handled_while_a_resend_is_in_progress() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    build_backlog(dir.path(), &clock).await;
    let svc = restart_with_small_queue(dir.path(), &clock).await;

    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    // A crossing order and a cancel, sent straight away.
    // The seller is a different user from order 1's buyer, so it trades.
    d.send(limit("AAA", BACKLOG + 1, 99_999, "SELL", 10, 1, IN_SESSION))
        .await;
    d.send(cancel_by_id("AAA", 2, 2)).await;

    let mut trade = false;
    let mut cancelled = false;
    let mut seen = 0;
    while !(trade && cancelled && seen > BACKLOG) {
        let r = d.recv().await.expect("the connection stayed open");
        seen += 1;
        trade |=
            r["type"] == "trade" && r["taker_seq"].as_u64().is_some() && r["maker_user_id"] == 1;
        cancelled |= r["status"] == "CANCELLED" && r["order_id"] == 2;
    }
    svc.shutdown().await.unwrap();
}
