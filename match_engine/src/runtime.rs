//! Messages and shared state that connect the runtime components (D19).
//!
//! Components talk through tokio channels. The channels into the journal and
//! into the engines are unbounded so that no component can block another:
//! the journal never waits on an engine, and an engine never waits on the
//! journal. Their backlog is exposed as a metric instead.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};

use tokio::sync::mpsc::UnboundedSender;
use tokio::sync::{mpsc, watch, Notify};

use crate::config::Config;
use crate::metrics::Metrics;
use crate::store::Kind;
use crate::types::{Command, Output, Seq, Snapshot, Timestamp};

/// Source of `ts` for commands. Only the journal thread calls it (D1).
pub type Clock = Arc<dyn Fn() -> Timestamp + Send + Sync>;

pub fn system_clock() -> Clock {
    Arc::new(|| {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as Timestamp)
            .unwrap_or(0)
    })
}

/// Into an engine task. Commands arrive already journaled (D1).
#[derive(Debug)]
pub enum EngineMsg {
    Command {
        seq: Seq,
        ts: Timestamp,
        cmd: Command,
    },
    DayClose {
        seq: Seq,
        ts: Timestamp,
    },
    /// Finish everything queued, report, and exit.
    Stop,
}

/// Into the journal thread.
#[derive(Debug)]
pub enum JournalMsg {
    /// A validated command from D. `payload` is the frame as received.
    Command {
        symbol: String,
        kind: Kind,
        command: Command,
        payload: String,
    },
    /// What an engine produced for one or more commands.
    EngineOutput {
        symbol: String,
        outputs: Vec<Output>,
    },
    EngineStopped {
        symbol: String,
    },
    /// An engine has handled a `DayClose` and sent everything it produced.
    DayCloseDone {
        symbol: String,
    },
    /// Check whether the trading day has ended (sent by the end-of-day timer).
    CloseCheck,
    /// Begin an orderly stop: drain the engines, write everything, exit.
    Shutdown,
}

/// Sender into the journal that keeps the backlog gauge honest.
#[derive(Clone)]
pub struct JournalTx {
    tx: UnboundedSender<JournalMsg>,
    metrics: Arc<Metrics>,
}

impl JournalTx {
    pub fn new(tx: UnboundedSender<JournalMsg>, metrics: Arc<Metrics>) -> Self {
        Self { tx, metrics }
    }

    /// Returns false if the journal has stopped.
    pub fn send(&self, msg: JournalMsg) -> bool {
        self.metrics.journal_backlog.inc();
        if self.tx.send(msg).is_err() {
            self.metrics.journal_backlog.dec();
            return false;
        }
        true
    }
}

/// The D connection's side of the outbound queue.
struct Sink {
    tx: mpsc::Sender<Arc<str>>,
    /// Wakes the connection so it closes now.
    close: Arc<Notify>,
    generation: u64,
}

/// Where the journal delivers outputs for D. There is at most one D
/// connection; a new one replaces the old (D21).
#[derive(Default)]
pub struct Hub {
    sink: Mutex<Option<Sink>>,
    next_generation: AtomicU64,
}

pub struct Attached {
    pub generation: u64,
    pub close: Arc<Notify>,
}

impl Hub {
    /// Registers `tx` as the destination for live outputs and closes the
    /// previous connection, if any.
    pub fn attach(&self, tx: mpsc::Sender<Arc<str>>) -> Attached {
        let close = Arc::new(Notify::new());
        let generation = self.next_generation.fetch_add(1, Ordering::Relaxed) + 1;
        let old = self.sink.lock().unwrap().replace(Sink {
            tx,
            close: close.clone(),
            generation,
        });
        if let Some(old) = old {
            old.close.notify_one();
        }
        Attached { generation, close }
    }

    /// Removes the sink if it is still the one registered as `generation`.
    pub fn detach(&self, generation: u64) {
        let mut guard = self.sink.lock().unwrap();
        if guard.as_ref().is_some_and(|s| s.generation == generation) {
            *guard = None;
        }
    }

    /// Queues `msg` for D without ever waiting. If the queue is full the
    /// connection is closed instead; D resumes by handshake (D16, D21).
    /// Returns true if the connection was closed for being full.
    pub fn deliver(&self, msg: &Arc<str>) -> bool {
        let mut guard = self.sink.lock().unwrap();
        let Some(sink) = guard.as_ref() else {
            return false;
        };
        match sink.tx.try_send(msg.clone()) {
            Ok(()) => false,
            Err(mpsc::error::TrySendError::Full(_)) => {
                sink.close.notify_one();
                *guard = None;
                true
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                *guard = None;
                false
            }
        }
    }

    pub fn is_attached(&self) -> bool {
        self.sink.lock().unwrap().is_some()
    }

    /// Outputs queued for the current D connection and not yet written.
    pub fn backlog(&self) -> usize {
        self.sink
            .lock()
            .unwrap()
            .as_ref()
            .map_or(0, |s| s.tx.max_capacity() - s.tx.capacity())
    }
}

/// The day the journal is writing to, for resend and the resume check.
#[derive(Clone, Debug)]
pub struct CurrentDay {
    pub day: String,
    pub path: PathBuf,
}

/// State shared by the HTTP/WebSocket handlers and the other components.
pub struct AppState {
    pub config: Config,
    pub metrics: Arc<Metrics>,
    pub hub: Arc<Hub>,
    pub journal: JournalTx,
    pub current_day: Arc<RwLock<CurrentDay>>,
    pub symbols: HashSet<String>,
    /// Latest snapshot per symbol, published by the engine tasks.
    pub snapshots: HashMap<String, watch::Receiver<Snapshot>>,
    /// False while recovering; D and market-data clients are refused (D10).
    pub ready: AtomicBool,
    /// Set when a stop begins: no new connections, no new commands.
    pub shutting_down: AtomicBool,
    /// Flips to true when a stop begins; connections watch it and close.
    pub shutdown: watch::Sender<bool>,
}

impl AppState {
    pub fn is_ready(&self) -> bool {
        self.ready.load(Ordering::Acquire) && !self.shutting_down.load(Ordering::Acquire)
    }

    /// Healthy means ready to take orders and with a journal that is still
    /// writing (D9, D23).
    pub fn is_healthy(&self) -> bool {
        self.is_ready() && self.metrics.journal_failed.get() == 0
    }
}

/// Resolves once a stop has begun. Returning `()` instead of the watch guard
/// keeps callers' futures `Send`.
pub async fn stopped(rx: &mut watch::Receiver<bool>) {
    let _ = rx.wait_for(|stop| *stop).await;
}
