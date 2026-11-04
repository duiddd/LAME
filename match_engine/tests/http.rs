//! `/healthz`, `/metrics`, the allow-list, and not taking orders during recovery.

mod support;

use std::sync::Arc;

use match_engine::runtime::Hub;
use match_engine::service::{self, Hooks};
use support::*;
use tokio::sync::{oneshot, Notify};

#[tokio::test]
async fn healthz_is_ok_once_ready() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    assert_eq!(
        http_get(svc.ctrl_addr, "/healthz").await,
        (200, "ok".into())
    );
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn nothing_is_served_while_recovering() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let (listening_tx, listening_rx) = oneshot::channel();
    let hold = Arc::new(Notify::new());
    let hooks = Hooks {
        listening: Some(listening_tx),
        hold_recovery: Some(hold.clone()),
    };
    let starting = tokio::spawn(service::start_with(
        config(dir.path()),
        clock.clock(),
        hooks,
    ));
    let (ctrl, md) = listening_rx.await.unwrap();

    // The health check answers, and says no.
    assert_eq!(http_get(ctrl, "/healthz").await.0, 503);
    // D and market-data clients are refused, not queued.
    assert!(tokio_tungstenite::connect_async(format!("ws://{ctrl}/d"))
        .await
        .is_err());
    assert!(
        tokio_tungstenite::connect_async(format!("ws://{md}/md/AAA"))
            .await
            .is_err()
    );
    // Metrics are available throughout.
    assert_eq!(http_get(ctrl, "/metrics").await.0, 200);

    hold.notify_one();
    let svc = starting.await.unwrap().unwrap();
    assert_eq!(http_get(svc.ctrl_addr, "/healthz").await.0, 200);
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 1, "BUY", 5, 1, IN_SESSION)).await;
    assert_eq!(d.recv().await.unwrap()["status"], "ACCEPTED");
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn healthz_turns_unhealthy_when_a_stop_begins() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    svc.state
        .shutting_down
        .store(true, std::sync::atomic::Ordering::SeqCst);
    assert_eq!(http_get(svc.ctrl_addr, "/healthz").await.0, 503);
    assert!(
        tokio_tungstenite::connect_async(format!("ws://{}/d", svc.ctrl_addr))
            .await
            .is_err()
    );
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn metrics_report_commands_outputs_and_the_journal() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let mut d = Client::connect(svc.ctrl_addr, 0).await;
    d.send(limit("AAA", 1, 100, "SELL", 50, 5, IN_SESSION))
        .await;
    d.send(limit("AAA", 2, 200, "BUY", 50, 3, IN_SESSION)).await;
    d.send_text("garbage").await;
    d.recv_n(5).await;
    d.recv().await; // the error reply

    let (status, body) = http_get(svc.ctrl_addr, "/metrics").await;
    assert_eq!(status, 200);
    assert!(
        body.contains("lame_b_commands_total{symbol=\"AAA\"} 2"),
        "{body}"
    );
    assert!(
        body.contains("lame_b_outputs_total{kind=\"Trade\"} 1"),
        "{body}"
    );
    assert!(
        body.contains("lame_b_outputs_total{kind=\"OrderEvent\"} 4"),
        "{body}"
    );
    assert!(body.contains("lame_b_bad_frames_total 1"), "{body}");
    for name in [
        "lame_b_journal_backlog",
        "lame_b_outbound_backlog",
        "lame_b_replay_duration_seconds",
        "lame_b_md_subscribers",
        "lame_b_journal_failed 0",
    ] {
        assert!(body.contains(name), "missing {name}\n{body}");
    }
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn sources_outside_the_allow_list_are_refused() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let mut cfg = config(dir.path());
    cfg.allowed_cidrs = vec!["10.0.0.0/8".parse().unwrap()];
    let svc = start(cfg, &clock).await;
    assert_eq!(http_get(svc.ctrl_addr, "/healthz").await.0, 403);
    assert_eq!(http_get(svc.ctrl_addr, "/metrics").await.0, 403);
    assert!(
        tokio_tungstenite::connect_async(format!("ws://{}/d", svc.ctrl_addr))
            .await
            .is_err()
    );
    // Market data is public: no allow-list there.
    assert!(
        tokio_tungstenite::connect_async(format!("ws://{}/md/AAA", svc.md_addr))
            .await
            .is_ok()
    );
    svc.shutdown().await.unwrap();
}

#[tokio::test]
async fn the_healthcheck_subcommand_logic_matches_healthz() {
    let dir = tempfile::tempdir().unwrap();
    let clock = TestClock::at(IN_SESSION);
    let svc = start(config(dir.path()), &clock).await;
    let addr = svc.ctrl_addr;
    assert!(
        tokio::task::spawn_blocking(move || match_engine::healthcheck::check(addr))
            .await
            .unwrap()
    );
    svc.state
        .shutting_down
        .store(true, std::sync::atomic::Ordering::SeqCst);
    assert!(
        !tokio::task::spawn_blocking(move || match_engine::healthcheck::check(addr))
            .await
            .unwrap()
    );
    svc.shutdown().await.unwrap();
}

#[test]
fn a_full_outbound_queue_closes_the_connection_instead_of_waiting() {
    use std::sync::Arc;
    use tokio::sync::mpsc;
    let hub = Hub::default();
    let (tx, mut rx) = mpsc::channel::<Arc<str>>(2);
    let attached = hub.attach(tx);
    let msg: Arc<str> = Arc::from("x");
    assert!(!hub.deliver(&msg));
    assert!(!hub.deliver(&msg));
    assert_eq!(hub.backlog(), 2);
    // The third does not fit: the delivery reports it closed the connection,
    // and the sink is gone.
    assert!(hub.deliver(&msg));
    assert!(!hub.is_attached());
    // What was already queued is still there to be written out.
    assert!(rx.try_recv().is_ok());
    assert!(rx.try_recv().is_ok());
    drop(attached);
}

#[test]
fn a_newer_connection_detaches_only_itself() {
    use tokio::sync::mpsc;
    let hub = Hub::default();
    let (tx1, _rx1) = mpsc::channel::<Arc<str>>(1);
    let old = hub.attach(tx1);
    let (tx2, _rx2) = mpsc::channel::<Arc<str>>(1);
    let new = hub.attach(tx2);
    // The old connection ending must not remove the new one's sink.
    hub.detach(old.generation);
    assert!(hub.is_attached());
    hub.detach(new.generation);
    assert!(!hub.is_attached());
}
