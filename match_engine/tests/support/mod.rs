//! Helpers for tests that run the whole service in-process on ephemeral ports.
#![allow(dead_code)]

use std::net::SocketAddr;
use std::path::Path;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;

use futures_util::{SinkExt, StreamExt};
use match_engine::config::Config;
use match_engine::runtime::Clock;
use match_engine::service::{self, RunningService};
use match_engine::types::SymbolSpec;
use serde_json::{json, Value};
use tokio::net::TcpStream;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};

/// 2026-11-04 10:00:00 UTC. Dublin is on winter time, so this is inside the
/// 00:00 to 23:00 session.
pub const IN_SESSION: i64 = 1_793_786_400_000;
/// 23:30 the same day: after the close.
pub const AFTER_CLOSE: i64 = IN_SESSION + 13 * 3_600_000 + 30 * 60_000;
pub const DAY: &str = "2026-11-04";

/// A clock the test moves by hand.
#[derive(Clone)]
pub struct TestClock(pub Arc<AtomicI64>);

impl TestClock {
    pub fn at(ts: i64) -> Self {
        Self(Arc::new(AtomicI64::new(ts)))
    }
    pub fn set(&self, ts: i64) {
        self.0.store(ts, Ordering::SeqCst);
    }
    pub fn clock(&self) -> Clock {
        let inner = self.0.clone();
        Arc::new(move || inner.load(Ordering::SeqCst))
    }
}

pub fn config(dir: &Path) -> Config {
    Config {
        md_addr: "127.0.0.1:0".parse().unwrap(),
        ctrl_addr: "127.0.0.1:0".parse().unwrap(),
        allowed_cidrs: vec!["127.0.0.0/8".parse().unwrap()],
        event_db_dir: dir.to_path_buf(),
        outbound_queue_cap: 1000,
        md_heartbeat: std::time::Duration::from_secs(1),
        symbols: vec![
            ("AAA".into(), SymbolSpec::UNIT),
            ("BBB".into(), SymbolSpec::new(5, 10)),
        ],
    }
}

pub async fn start(cfg: Config, clock: &TestClock) -> RunningService {
    service::start(cfg, clock.clock())
        .await
        .expect("service starts")
}

/// Like D, it deduplicates output records by `(day, seq)`: resend and live delivery
/// may overlap (D21).
pub struct Client {
    ws: WebSocketStream<MaybeTlsStream<TcpStream>>,
    seen: std::collections::HashSet<(String, u64)>,
}

impl Client {
    /// Connects to `/d` and sends the resume handshake.
    pub async fn connect(addr: SocketAddr, from_seq: u64) -> Self {
        let mut c = Self::connect_raw(addr).await;
        c.send(json!({"type": "resume", "from_seq": from_seq}))
            .await;
        c
    }

    pub async fn connect_raw(addr: SocketAddr) -> Self {
        let (ws, _) = connect_async(format!("ws://{addr}/d"))
            .await
            .expect("connects");
        Self {
            ws,
            seen: Default::default(),
        }
    }

    pub async fn send(&mut self, v: Value) {
        self.ws.send(Message::Text(v.to_string())).await.unwrap();
    }

    pub async fn send_text(&mut self, text: &str) {
        self.ws.send(Message::Text(text.into())).await.unwrap();
    }

    /// The next record or error as JSON (skipping repeats of a `seq` already
    /// seen), or `None` once the connection is closed.
    pub async fn recv(&mut self) -> Option<Value> {
        loop {
            match self.ws.next().await? {
                Ok(Message::Text(t)) => {
                    let v: Value = serde_json::from_str(&t).unwrap();
                    // `seq` restarts every day, so a record is `(day, seq)`.
                    match (v["day"].as_str(), v["seq"].as_u64()) {
                        (Some(day), Some(seq)) if !self.seen.insert((day.into(), seq)) => continue,
                        _ => return Some(v),
                    }
                }
                Ok(Message::Close(_)) | Err(_) => return None,
                Ok(_) => {}
            }
        }
    }

    pub async fn recv_n(&mut self, n: usize) -> Vec<Value> {
        let mut out = Vec::new();
        for _ in 0..n {
            out.push(self.recv().await.expect("connection stayed open"));
        }
        out
    }
}

pub fn limit(
    symbol: &str,
    order_id: u64,
    user_id: u64,
    side: &str,
    price: i64,
    qty: i64,
    sent_at: i64,
) -> Value {
    json!({
        "type": "new_order", "symbol": symbol, "order_id": order_id, "user_id": user_id,
        "side": side, "order_type": "LIMIT", "price": price, "qty": qty, "sent_at": sent_at
    })
}

pub fn market(
    symbol: &str,
    order_id: u64,
    user_id: u64,
    side: &str,
    qty: i64,
    sent_at: i64,
) -> Value {
    json!({
        "type": "new_order", "symbol": symbol, "order_id": order_id, "user_id": user_id,
        "side": side, "order_type": "MARKET", "qty": qty, "sent_at": sent_at
    })
}

pub fn cancel_by_id(symbol: &str, user_id: u64, order_id: u64) -> Value {
    json!({"type": "cancel_order", "symbol": symbol, "user_id": user_id, "order_id": order_id})
}

pub fn statuses(records: &[Value]) -> Vec<String> {
    records
        .iter()
        .map(|r| match r["type"].as_str().unwrap() {
            "trade" => "TRADE".to_string(),
            _ => r["status"].as_str().unwrap().to_string(),
        })
        .collect()
}

/// Every output's `prev_seq` is the `seq` of the output before it.
pub fn assert_chain(records: &[Value]) {
    let mut prev = records[0]["prev_seq"].as_u64().unwrap();
    for r in records {
        assert_eq!(r["prev_seq"].as_u64().unwrap(), prev, "broken chain at {r}");
        prev = r["seq"].as_u64().unwrap();
    }
}

/// A market-data subscriber.
pub struct MdClient {
    ws: WebSocketStream<MaybeTlsStream<TcpStream>>,
}

impl MdClient {
    pub async fn connect(addr: SocketAddr, symbol: &str) -> Self {
        let (ws, _) = connect_async(format!("ws://{addr}/md/{symbol}"))
            .await
            .expect("connects");
        Self { ws }
    }

    pub async fn recv(&mut self) -> Option<Value> {
        loop {
            match self.ws.next().await? {
                Ok(Message::Text(t)) => return Some(serde_json::from_str(&t).unwrap()),
                Ok(Message::Close(_)) | Err(_) => return None,
                Ok(_) => {}
            }
        }
    }

    /// Reads until a snapshot satisfies `done`; snapshots are coalesced, so
    /// intermediate states may be skipped.
    pub async fn recv_until(&mut self, done: impl Fn(&Value) -> bool) -> Value {
        loop {
            let v = self.recv().await.expect("connection stayed open");
            if done(&v) {
                return v;
            }
        }
    }
}

/// A bare `GET` over a fresh connection: `(status, body)`.
pub async fn http_get(addr: SocketAddr, path: &str) -> (u16, String) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let mut stream = TcpStream::connect(addr).await.expect("connects");
    let request = format!("GET {path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).await.unwrap();
    let status = response.split_whitespace().nth(1).unwrap().parse().unwrap();
    let body = response.split("\r\n\r\n").nth(1).unwrap_or("").to_string();
    (status, body)
}
