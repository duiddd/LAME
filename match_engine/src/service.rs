//! Assembles the runtime (D19): the journal thread, one engine task per pair,
//! and the servers, and brings it up and down in the right order.

use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, RwLock};
use std::time::Instant;

use anyhow::{anyhow, Context, Result};
use tokio::net::TcpListener;
use tokio::sync::{mpsc, oneshot, watch, Notify};
use tokio::task::JoinHandle;

use crate::config::Config;
use crate::engine_task::{self, EngineTask};
use crate::eod;
use crate::http;
use crate::journal::Journal;
use crate::md;
use crate::metrics::Metrics;
use crate::recovery;
use crate::runtime::{AppState, Clock, CurrentDay, Hub, JournalMsg, JournalTx};
use crate::types::Snapshot;

pub struct RunningService {
    pub state: Arc<AppState>,
    /// The addresses actually bound (useful when the config asked for port 0).
    pub ctrl_addr: SocketAddr,
    pub md_addr: SocketAddr,
    journal_done: oneshot::Receiver<Result<()>>,
    engine_tasks: Vec<JoinHandle<()>>,
    servers: Vec<JoinHandle<()>>,
    timer: JoinHandle<()>,
}

/// Ways for a test to observe or pause start-up.
#[derive(Default)]
pub struct Hooks {
    /// Told the bound `(ctrl, md)` addresses as soon as the servers listen,
    /// before recovery.
    pub listening: Option<oneshot::Sender<(SocketAddr, SocketAddr)>>,
    /// If set, recovery waits for this to be notified.
    pub hold_recovery: Option<Arc<Notify>>,
}

/// Brings the service up. The control port answers first so the health check
/// can report "not ready"; commands are refused until recovery has finished.
pub async fn start(config: Config, clock: Clock) -> Result<RunningService> {
    start_with(config, clock, Hooks::default()).await
}

pub async fn start_with(config: Config, clock: Clock, hooks: Hooks) -> Result<RunningService> {
    let metrics = Arc::new(Metrics::new());
    let hub = Arc::new(Hub::default());
    let (journal_tx, journal_rx) = mpsc::unbounded_channel::<JournalMsg>();
    let journal = JournalTx::new(journal_tx, metrics.clone());
    let (shutdown, _) = watch::channel(false);
    let current_day = Arc::new(RwLock::new(CurrentDay {
        day: String::new(),
        path: Default::default(),
    }));

    // Per pair: the channel into its engine task and the latest-snapshot cell.
    let mut engine_txs = HashMap::new();
    let mut engine_rxs = HashMap::new();
    let mut snapshot_txs = HashMap::new();
    let mut snapshots = HashMap::new();
    for (name, _) in &config.symbols {
        let (tx, rx) = engine_task::channel();
        engine_txs.insert(name.clone(), tx);
        engine_rxs.insert(name.clone(), rx);
        let (snap_tx, snap_rx) = watch::channel(empty_snapshot());
        snapshot_txs.insert(name.clone(), snap_tx);
        snapshots.insert(name.clone(), snap_rx);
    }

    let state = Arc::new(AppState {
        config: config.clone(),
        metrics: metrics.clone(),
        hub: hub.clone(),
        journal: journal.clone(),
        current_day: current_day.clone(),
        symbols: config
            .symbols
            .iter()
            .map(|(n, _)| n.clone())
            .collect::<HashSet<_>>(),
        snapshots,
        ready: AtomicBool::new(false),
        shutting_down: AtomicBool::new(false),
        shutdown,
    });

    let ctrl_listener = TcpListener::bind(config.ctrl_addr)
        .await
        .with_context(|| format!("binding {}", config.ctrl_addr))?;
    let ctrl_addr = ctrl_listener.local_addr()?;
    let md_listener = TcpListener::bind(config.md_addr)
        .await
        .with_context(|| format!("binding {}", config.md_addr))?;
    let md_addr = md_listener.local_addr()?;
    let servers = vec![
        serve(
            ctrl_listener,
            http::ctrl_router(state.clone()),
            state.shutdown.subscribe(),
        ),
        serve(
            md_listener,
            md::md_router(state.clone()),
            state.shutdown.subscribe(),
        ),
    ];

    if let Some(listening) = hooks.listening {
        let _ = listening.send((ctrl_addr, md_addr));
    }
    if let Some(hold) = hooks.hold_recovery {
        hold.notified().await;
    }

    // Recovery reads the whole day and replays it; it is blocking work.
    let started = Instant::now();
    let now = clock();
    let cfg = config.clone();
    let recovered = tokio::task::spawn_blocking(move || recovery::recover(&cfg, now))
        .await
        .context("recovery task panicked")??;
    metrics
        .replay_duration_seconds
        .set(started.elapsed().as_secs_f64());
    metrics
        .replay_records
        .set(recovered.replayed_records as i64);

    let mut engine_tasks = Vec::new();
    for (name, engine) in recovered.engines {
        let snapshot = snapshot_txs.remove(&name).expect("one per symbol");
        snapshot.send_replace(engine.snapshot());
        let task = EngineTask {
            symbol: name.clone(),
            engine,
            inbox: engine_rxs.remove(&name).expect("one per symbol"),
            journal: journal.clone(),
            snapshot,
        };
        engine_tasks.push(tokio::spawn(task.run()));
    }

    let timer_clock = clock.clone();
    let journal_thread = Journal::new(recovered.init, clock, engine_txs, hub, metrics, current_day);
    let (done_tx, journal_done) = oneshot::channel();
    std::thread::Builder::new()
        .name("journal".into())
        .spawn(move || {
            let result = journal_thread.run(journal_rx);
            if let Err(e) = &result {
                tracing::error!(error = %format!("{e:#}"), "journal stopped");
            }
            let _ = done_tx.send(result);
        })
        .context("starting the journal thread")?;

    // The timer wakes the journal at each close. One check right now covers a
    // start after the close, when the day must be closed before any order.
    let timer = tokio::spawn(eod::run(
        journal.clone(),
        timer_clock,
        state.shutdown.subscribe(),
    ));
    journal.send(JournalMsg::CloseCheck);

    state.ready.store(true, Ordering::Release);
    Ok(RunningService {
        state,
        ctrl_addr,
        md_addr,
        journal_done,
        engine_tasks,
        servers,
        timer,
    })
}

fn empty_snapshot() -> Snapshot {
    Snapshot {
        bids: [None; crate::types::DEPTH],
        asks: [None; crate::types::DEPTH],
        last_price: None,
        volume: 0,
        last_ms: 0,
    }
}

fn serve(
    listener: TcpListener,
    router: axum::Router,
    mut stop: watch::Receiver<bool>,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        let app = router.into_make_service_with_connect_info::<SocketAddr>();
        let result = axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                let _ = stop.wait_for(|stop| *stop).await;
            })
            .await;
        if let Err(e) = result {
            tracing::error!(error = %e, "server stopped");
        }
    })
}

impl RunningService {
    /// Resolves when the journal thread ends, which for a running service
    /// means it failed and B must stop taking orders (D9).
    pub async fn journal_stopped(&mut self) -> Result<()> {
        (&mut self.journal_done)
            .await
            .map_err(|_| anyhow!("journal thread vanished"))?
    }

    /// Orderly stop (D23): refuse new connections and commands, let the engines
    /// and the journal finish what they already hold, then close the servers.
    pub async fn shutdown(mut self) -> Result<()> {
        self.state.shutting_down.store(true, Ordering::Release);
        let _ = self.state.shutdown.send(true);
        self.state.journal.send(JournalMsg::Shutdown);
        let result = self.journal_stopped().await;
        for task in self.engine_tasks {
            let _ = task.await;
        }
        for server in self.servers {
            let _ = server.await;
        }
        let _ = self.timer.await;
        result
    }
}
