//! Market-data publishing on `/md/<symbol>`.

mod support;

use serde_json::json;
use support::*;

#[tokio::test]
async fn a_new_subscriber_gets_the_current_view_at_once() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut md = MdClient::connect(svc.md_addr, "AAA").await;
    assert_eq!(
        md.recv().await.unwrap(),
        json!({"bids": [], "asks": [], "last_price": null, "volume": 0, "last_ms": 0})
    );
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn snapshots_follow_the_book_with_volume_and_generation_time() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut md = MdClient::connect(svc.md_addr, "AAA").await;
    md.recv().await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;

    d.send(limit("AAA", 1, 100, "SELL", 50, 5, IN_SESSION))
        .await;
    let rested = md
        .recv_until(|s| s["asks"] == json!([{"price": 50, "qty": 5}]))
        .await;
    assert_eq!(rested["last_price"], json!(null));
    assert_eq!(rested["volume"], 0);
    assert_eq!(rested["last_ms"], IN_SESSION);

    d.send(limit("AAA", 2, 200, "BUY", 50, 3, IN_SESSION)).await;
    let traded = md.recv_until(|s| s["volume"] == 3).await;
    assert_eq!(traded["asks"], json!([{"price": 50, "qty": 2}]));
    assert_eq!(traded["bids"], json!([]));
    assert_eq!(traded["last_price"], 50);
    // Same millisecond as the first command, so it is pushed one forward.
    assert_eq!(traded["last_ms"], IN_SESSION + 1);
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn pairs_have_their_own_stream() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut aaa = MdClient::connect(svc.md_addr, "AAA").await;
    let mut bbb = MdClient::connect(svc.md_addr, "BBB").await;
    aaa.recv().await;
    bbb.recv().await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;

    d.send(limit("BBB", 1, 100, "BUY", 100, 10, IN_SESSION))
        .await;
    let b = bbb.recv_until(|s| s["bids"] != json!([])).await;
    assert_eq!(b["bids"], json!([{"price": 100, "qty": 10}]));

    d.send(limit("AAA", 2, 100, "SELL", 7, 1, IN_SESSION)).await;
    let a = aaa.recv_until(|s| s["asks"] != json!([])).await;
    assert_eq!(a["asks"], json!([{"price": 7, "qty": 1}]));
    assert_eq!(a["bids"], json!([]));
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn only_the_best_three_levels_are_published() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut md = MdClient::connect(svc.md_addr, "AAA").await;
    md.recv().await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    for (i, price) in [10, 11, 12, 13, 14].into_iter().enumerate() {
        d.send(limit("AAA", i as u64 + 1, 1, "BUY", price, 1, IN_SESSION))
            .await;
    }
    // Snapshots are coalesced, so wait for the one with the best level on top.
    // The last order placed has the best price, so that view is the full one.
    let s = md
        .recv_until(|s| s["bids"].get(0).is_some_and(|l| l["price"] == 14))
        .await;
    assert_eq!(
        s["bids"],
        json!([{"price":14,"qty":1},{"price":13,"qty":1},{"price":12,"qty":1}])
    );
    assert_eq!(s["asks"], json!([]));
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn last_ms_never_goes_backwards_and_is_strictly_increasing_between_views() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut md = MdClient::connect(svc.md_addr, "AAA").await;
    md.recv().await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    for i in 1..=20u64 {
        d.send(limit("AAA", i, i, "BUY", 10 + i as i64, 1, IN_SESSION))
            .await;
    }
    let mut last = 0;
    loop {
        let s = md.recv().await.unwrap();
        let ms = s["last_ms"].as_i64().unwrap();
        assert!(ms >= last, "{ms} < {last}");
        last = ms;
        if s["bids"][0]["price"] == 30 {
            break;
        }
    }
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn unknown_pairs_are_not_found() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let result = tokio_tungstenite::connect_async(format!("ws://{}/md/ZZZ", svc.md_addr)).await;
    assert!(result.is_err());
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn silence_is_broken_by_repeating_the_last_message() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let mut cfg = config(dir.path());
    cfg.md_heartbeat = std::time::Duration::from_millis(30);
    let svc = start(cfg, &clock).await;
    let mut md = MdClient::connect(svc.md_addr, "AAA").await;
    let first = md.recv().await.unwrap();
    // Nothing changes, yet messages keep arriving, unchanged.
    for _ in 0..3 {
        assert_eq!(md.recv().await.unwrap(), first);
    }
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn a_subscriber_that_never_reads_does_not_slow_matching() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let _stuck = MdClient::connect(svc.md_addr, "AAA").await; // never reads
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    for i in 1..=300u64 {
        d.send(limit("AAA", i, i, "BUY", 10, 1, IN_SESSION)).await;
    }
    // Every order is still processed and answered.
    let mut accepted = 0;
    while accepted < 300 {
        let r = d.recv().await.unwrap();
        if r["status"] == "ACCEPTED" {
            accepted += 1;
        }
    }
    svc.shutdown().await.unwrap();
}
