//! Market-data publishing (D6, D14): `/md/<symbol>` on its own port.
//!
//! The engine tasks publish their latest snapshot into a `watch` cell, which
//! keeps only the newest value and never makes the engine wait. Each
//! subscriber has its own task and its own cursor into that cell, so a slow
//! subscriber only misses its own older snapshots and cannot hold back the
//! engine or other subscribers.

use std::sync::Arc;
use std::time::Duration;

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;
use futures_util::{SinkExt, StreamExt};
use tokio::sync::watch;
use tokio::time::{interval, sleep_until, Instant, MissedTickBehavior};

use crate::runtime::{stopped, AppState};
use crate::types::Snapshot;

/// At most one snapshot per millisecond per subscriber (D6).
const MIN_GAP: Duration = Duration::from_millis(1);

pub fn md_router(app: Arc<AppState>) -> Router {
    Router::new()
        .route("/md/:symbol", get(md_handler))
        .with_state(app)
}

async fn md_handler(
    ws: WebSocketUpgrade,
    Path(symbol): Path<String>,
    State(app): State<Arc<AppState>>,
) -> Response {
    // Nothing is published during recovery (D6).
    if !app.is_ready() {
        return (StatusCode::SERVICE_UNAVAILABLE, "not ready").into_response();
    }
    let Some(cell) = app.snapshots.get(&symbol).cloned() else {
        return (StatusCode::NOT_FOUND, "unknown symbol").into_response();
    };
    ws.on_upgrade(move |socket| serve(socket, app, cell))
}

fn encode(snapshot: Snapshot) -> String {
    serde_json::to_string(&snapshot.into_market_data()).expect("market data always serializes")
}

struct Subscribed<'a>(&'a AppState);

impl<'a> Subscribed<'a> {
    fn new(app: &'a AppState) -> Self {
        app.metrics.md_subscribers.inc();
        Self(app)
    }
}

impl Drop for Subscribed<'_> {
    fn drop(&mut self) {
        self.0.metrics.md_subscribers.dec();
    }
}

async fn serve(socket: WebSocket, app: Arc<AppState>, mut cell: watch::Receiver<Snapshot>) {
    let _subscribed = Subscribed::new(&app);
    let (mut write, mut read) = socket.split();
    let mut shutdown = app.shutdown.subscribe();

    // A new subscriber first gets the current view.
    let mut last = *cell.borrow_and_update();
    let mut last_text = encode(last);
    if write.send(Message::Text(last_text.clone())).await.is_err() {
        return;
    }
    let mut next_send = Instant::now() + MIN_GAP;

    // Without this, "unchanged, so nothing sent" looks the same to a client as
    // "B is down". If a second passes in silence the last message is sent
    // again, unchanged (D6).
    let mut heartbeat = interval(app.config.md_heartbeat);
    heartbeat.set_missed_tick_behavior(MissedTickBehavior::Delay);
    heartbeat.tick().await;

    loop {
        tokio::select! {
            changed = cell.changed() => {
                if changed.is_err() {
                    break;
                }
                sleep_until(next_send).await;
                // Only the newest snapshot matters; older ones are skipped.
                let latest = *cell.borrow_and_update();
                if latest.same_view(&last) {
                    continue;
                }
                last = latest;
                last_text = encode(latest);
                if write.send(Message::Text(last_text.clone())).await.is_err() {
                    break;
                }
                next_send = Instant::now() + MIN_GAP;
                heartbeat.reset();
            }
            _ = heartbeat.tick() => {
                if write.send(Message::Text(last_text.clone())).await.is_err() {
                    break;
                }
            }
            frame = read.next() => match frame {
                None | Some(Err(_)) | Some(Ok(Message::Close(_))) => break,
                Some(Ok(_)) => {}
            },
            _ = stopped(&mut shutdown) => break,
        }
    }
    let _ = write.close().await;
}
