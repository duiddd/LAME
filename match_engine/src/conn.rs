//! The D channel (D13, D21): one WebSocket connection carrying commands from D
//! and outputs back to D.
//!
//! The read half and the write half run independently, so a slow reader on D's
//! side never stops commands from being received.

use std::sync::Arc;
use std::time::Duration;

use anyhow::anyhow;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use futures_util::stream::SplitSink;
use futures_util::{SinkExt, StreamExt};
use tokio::sync::mpsc;
use tokio::time::{interval, timeout};

use crate::runtime::{stopped, AppState, JournalMsg};
use crate::store::{Kind, Reader};
use crate::types::Command;
use crate::wire::{error_json, parse_command, resume_ok_json, Inbound};

/// B pings this often to notice half-open connections sooner. Correctness
/// never depends on it: resume always starts from D's own `from_seq`.
const PING_EVERY: Duration = Duration::from_secs(5);
/// A connection that sends nothing, not even a pong, for this long is closed.
const IDLE_LIMIT: Duration = Duration::from_secs(15);

pub async fn d_handler(ws: WebSocketUpgrade, State(app): State<Arc<AppState>>) -> Response {
    // During recovery B takes no orders (D10); once a stop has begun, no new
    // connections either.
    if !app.is_ready() {
        return (StatusCode::SERVICE_UNAVAILABLE, "not ready").into_response();
    }
    ws.on_upgrade(move |socket| serve(socket, app))
}

async fn refuse(mut write: SplitSink<WebSocket, Message>, code: &str, message: &str) {
    let _ = write.send(Message::Text(error_json(code, message))).await;
    let _ = write.close().await;
}

async fn serve(socket: WebSocket, app: Arc<AppState>) {
    let (write, mut read) = socket.split();

    // The first message must be the resume handshake (D21).
    let first = match timeout(IDLE_LIMIT, read.next()).await {
        Ok(Some(Ok(Message::Text(text)))) => text,
        _ => return,
    };
    let from_seq = match serde_json::from_str::<Inbound>(&first) {
        Ok(Inbound::Resume { from_seq }) => from_seq,
        _ => {
            return refuse(
                write,
                "expected_resume",
                "the first message must be {\"type\":\"resume\",\"from_seq\":N}",
            )
            .await
        }
    };

    let current = app.current_day.read().unwrap().clone();
    let reader = match Reader::open(&current.path) {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(error = %e, "cannot open the event store for resend");
            return refuse(write, "internal", "event store unavailable").await;
        }
    };
    match reader.max_seq() {
        // D claims to have seen more than B ever wrote: B lost data. A person
        // has to look at this, so refuse instead of guessing (D21).
        Ok(max) if from_seq > max => {
            return refuse(
                write,
                "resume_ahead_of_store",
                "from_seq is beyond the last record of the day",
            )
            .await
        }
        Ok(_) => {}
        Err(e) => {
            tracing::error!(error = %e, "cannot read the event store for resend");
            return refuse(write, "internal", "event store unavailable").await;
        }
    }

    // Two queues. Live outputs go into one and the resend into the other, so a
    // long resend can never fill the queue that live outputs need: a live
    // output that finds its own queue full means D really is too slow, and the
    // connection is closed (D16, D21). The resend, by contrast, simply waits
    // when its queue is full. The writer takes from both; the two streams may
    // interleave, each is in ascending order, and D deduplicates by `seq`.
    let cap = app.config.outbound_queue_cap;
    let (live_tx, mut live_rx) = mpsc::channel::<Arc<str>>(cap);
    let (resend_tx, mut resend_rx) = mpsc::channel::<Arc<str>>(cap);

    // Register for live outputs first, then start the resend: anything
    // committed after this point reaches the live queue, anything before it is
    // covered by the resend.
    let attached = app.hub.attach(live_tx.clone());
    // Read after attaching: every output stored up to here is either in the
    // resend (which reads everything after `from_seq`) or has been sent live.
    let through_seq = match reader.last_output_seq() {
        Ok(s) => s,
        Err(e) => {
            tracing::error!(error = %e, "cannot read the event store for resend");
            app.hub.detach(attached.generation);
            return refuse(write, "internal", "event store unavailable").await;
        }
    };
    let resend = tokio::task::spawn_blocking(move || {
        reader.scan_outputs(from_seq, |rec| {
            resend_tx
                .blocking_send(Arc::from(rec.payload.as_str()))
                .map_err(|_| anyhow!("connection closed"))
        })
    });

    let mut shutdown = app.shutdown.subscribe();
    let writer = async {
        let mut write = write;
        // First on the wire, ahead of any record: where the catch-up ends.
        if write
            .send(Message::Text(resume_ok_json(from_seq, through_seq)))
            .await
            .is_err()
        {
            return;
        }
        let mut ping = interval(PING_EVERY);
        ping.tick().await;
        let mut resending = true;
        loop {
            tokio::select! {
                msg = live_rx.recv() => match msg {
                    Some(text) => {
                        if write.send(Message::Text(text.to_string())).await.is_err() {
                            break;
                        }
                    }
                    None => break,
                },
                msg = resend_rx.recv(), if resending => match msg {
                    Some(text) => {
                        if write.send(Message::Text(text.to_string())).await.is_err() {
                            break;
                        }
                    }
                    // The resend has reached the end of the day's outputs.
                    None => resending = false,
                },
                _ = ping.tick() => {
                    if write.send(Message::Ping(Vec::new())).await.is_err() {
                        break;
                    }
                }
                _ = attached.close.notified() => break,
            }
        }
        let _ = write.close().await;
    };
    let reading = async {
        loop {
            match timeout(IDLE_LIMIT, read.next()).await {
                Ok(Some(Ok(Message::Text(text)))) => on_text(&app, &live_tx, text),
                Ok(Some(Ok(Message::Close(_)))) | Ok(None) | Ok(Some(Err(_))) | Err(_) => break,
                Ok(Some(Ok(_))) => {}
            }
        }
    };
    tokio::select! {
        _ = writer => {}
        _ = reading => {}
        _ = stopped(&mut shutdown) => {}
    }
    app.hub.detach(attached.generation);
    drop(resend);
}

/// Validates one frame from D and hands the command to the journal.
fn on_text(app: &AppState, replies: &mpsc::Sender<Arc<str>>, text: String) {
    let reject = |code: &str, message: &str| {
        app.metrics.bad_frames_total.inc();
        let _ = replies.try_send(Arc::from(error_json(code, message).as_str()));
    };
    let parsed = match parse_command(&text) {
        Ok(p) => p,
        Err(message) => return reject("bad_message", &message),
    };
    if !app.symbols.contains(&parsed.symbol) {
        return reject(
            "unknown_symbol",
            &format!("unknown symbol {}", parsed.symbol),
        );
    }
    if app.shutting_down.load(std::sync::atomic::Ordering::Acquire) {
        return;
    }
    let kind = match parsed.command {
        Command::New(_) => Kind::NewOrder,
        Command::Cancel(_) => Kind::CancelOrder,
        Command::DayClose => unreachable!("D never sends a day close"),
    };
    app.metrics
        .commands_total
        .with_label_values(&[&parsed.symbol])
        .inc();
    app.journal.send(JournalMsg::Command {
        symbol: parsed.symbol,
        kind,
        command: parsed.command,
        payload: text,
    });
}
