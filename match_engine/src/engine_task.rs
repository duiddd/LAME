//! One tokio task per trading pair (D18). It owns the engine, which only
//! computes in memory and never does I/O, so nothing here can be stalled by
//! disk or network.

use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender};
use tokio::sync::watch;

use crate::engine::MatchEngine;
use crate::runtime::{EngineMsg, JournalMsg, JournalTx};
use crate::types::{Command, Output, Snapshot};

/// Commands handled before the task yields back to the scheduler (D18).
const YIELD_EVERY: usize = 128;

pub struct EngineTask {
    pub symbol: String,
    pub engine: MatchEngine,
    pub inbox: UnboundedReceiver<EngineMsg>,
    pub journal: JournalTx,
    pub snapshot: watch::Sender<Snapshot>,
}

/// Creates the channel into an engine task.
pub fn channel() -> (UnboundedSender<EngineMsg>, UnboundedReceiver<EngineMsg>) {
    tokio::sync::mpsc::unbounded_channel()
}

impl EngineTask {
    pub async fn run(mut self) {
        let mut stopping = false;
        while !stopping {
            let Some(first) = self.inbox.recv().await else {
                break;
            };
            let mut outputs: Vec<Output> = Vec::new();
            let mut closes = 0;
            stopping = self.apply(first, &mut outputs, &mut closes);
            let mut handled = 1;
            while !stopping && handled < YIELD_EVERY {
                match self.inbox.try_recv() {
                    Ok(msg) => {
                        stopping = self.apply(msg, &mut outputs, &mut closes);
                        handled += 1;
                    }
                    Err(_) => break,
                }
            }
            if !outputs.is_empty() {
                self.journal.send(JournalMsg::EngineOutput {
                    symbol: self.symbol.clone(),
                    outputs,
                });
            }
            // After the outputs, so the journal knows nothing more is coming
            // for this close.
            for _ in 0..closes {
                self.journal.send(JournalMsg::DayCloseDone {
                    symbol: self.symbol.clone(),
                });
            }
            self.publish_snapshot();
            if handled == YIELD_EVERY {
                tokio::task::yield_now().await;
            }
        }
        self.journal.send(JournalMsg::EngineStopped {
            symbol: self.symbol.clone(),
        });
    }

    /// Applies one message, collecting its outputs and counting day closes.
    /// Returns true on `Stop`.
    fn apply(&mut self, msg: EngineMsg, outputs: &mut Vec<Output>, closes: &mut usize) -> bool {
        match msg {
            EngineMsg::Command { seq, ts, cmd } => self.engine.process(seq, ts, cmd),
            EngineMsg::DayClose { seq, ts } => {
                self.engine.process(seq, ts, Command::DayClose);
                *closes += 1;
            }
            EngineMsg::Stop => return true,
        }
        outputs.extend(self.engine.drain_outputs());
        false
    }

    /// Hands the latest view to the market-data side. It never waits: a
    /// `watch` keeps only the newest value, so a slow reader just misses
    /// older snapshots.
    fn publish_snapshot(&self) {
        let latest = self.engine.snapshot();
        self.snapshot.send_if_modified(|current| {
            if *current == latest {
                false
            } else {
                *current = latest;
                true
            }
        });
    }
}
