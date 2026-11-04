//! The journal thread (D1, D9, D19, D20): the only writer of the event store
//! and the only place sequence numbers and command timestamps are assigned.
//!
//! It is a dedicated OS thread, not a tokio task, because SQLite writes block.
//! Two orderings hold for everything it does:
//!
//! 1. a command is committed to the store before an engine sees it;
//! 2. an output is committed before it is sent to D.
//!
//! So nothing D has seen is missing from the store, and nothing an engine acted
//! on is unrecorded. Messages are handled in batches: one transaction per
//! batch, deliveries after the commit.

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::PathBuf;
use std::sync::{Arc, RwLock};

use anyhow::{Context, Result};
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender};

use crate::metrics::Metrics;
use crate::runtime::{Clock, CurrentDay, EngineMsg, Hub, JournalMsg};
use crate::session;
use crate::store::{db_path, DirLock, EventStore, Kind, Record};
use crate::types::{Output, Seq, Timestamp};
use crate::wire::Outbound;

/// Most messages folded into one transaction.
const MAX_BATCH: usize = 256;

/// Where the journal starts: a fresh day, or the state recovery rebuilt.
pub struct JournalInit {
    pub dir: PathBuf,
    pub day: String,
    pub store: EventStore,
    pub next_seq: Seq,
    pub last_output_seq: Seq,
    pub closed_today: bool,
    pub lock: DirLock,
}

pub struct Journal {
    dir: PathBuf,
    day: String,
    store: EventStore,
    next_seq: Seq,
    last_output_seq: Seq,
    /// Whether `DayClose` has been written for `day`.
    closed_today: bool,
    _lock: DirLock,
    clock: Clock,
    engines: HashMap<String, UnboundedSender<EngineMsg>>,
    hub: Arc<Hub>,
    metrics: Arc<Metrics>,
    current_day: Arc<RwLock<CurrentDay>>,
    draining: bool,
    stopped: HashSet<String>,
    /// Engines that have not yet reported the end of today's `DayClose`.
    pending_close: HashSet<String>,
    /// Commands that arrived for a later day while today's close was still
    /// being written; handled once it has been, in order.
    held: VecDeque<JournalMsg>,
}

enum Delivery {
    Engine(String, EngineMsg),
    Out(Arc<str>),
}

#[derive(Default)]
struct Batch {
    records: Vec<Record>,
    deliveries: Vec<Delivery>,
}

#[derive(PartialEq, Eq)]
enum Control {
    Continue,
    Exit,
}

impl Journal {
    pub fn new(
        init: JournalInit,
        clock: Clock,
        engines: HashMap<String, UnboundedSender<EngineMsg>>,
        hub: Arc<Hub>,
        metrics: Arc<Metrics>,
        current_day: Arc<RwLock<CurrentDay>>,
    ) -> Self {
        *current_day.write().unwrap() = CurrentDay {
            day: init.day.clone(),
            path: init.store.path().to_path_buf(),
        };
        Self {
            dir: init.dir,
            day: init.day,
            store: init.store,
            next_seq: init.next_seq,
            last_output_seq: init.last_output_seq,
            closed_today: init.closed_today,
            _lock: init.lock,
            clock,
            engines,
            hub,
            metrics,
            current_day,
            draining: false,
            stopped: HashSet::new(),
            pending_close: HashSet::new(),
            held: VecDeque::new(),
        }
    }

    /// Runs until `Shutdown` has drained the engines, or until a write fails.
    /// A write failure ends the thread with an error: B must stop taking
    /// orders rather than carry on without a record (D9).
    pub fn run(mut self, mut rx: UnboundedReceiver<JournalMsg>) -> Result<()> {
        let mut batch = Batch::default();
        while let Some(first) = rx.blocking_recv() {
            self.metrics.journal_backlog.dec();
            let mut control = self.handle(first, &mut batch)?;
            let mut taken = 1;
            while control == Control::Continue && taken < MAX_BATCH {
                let Ok(next) = rx.try_recv() else { break };
                self.metrics.journal_backlog.dec();
                control = self.handle(next, &mut batch)?;
                taken += 1;
            }
            self.flush(&mut batch)?;
            if control == Control::Exit {
                break;
            }
        }
        Ok(())
    }

    fn handle(&mut self, msg: JournalMsg, batch: &mut Batch) -> Result<Control> {
        match msg {
            JournalMsg::Command {
                symbol,
                kind,
                command,
                payload,
            } => {
                if self.draining || !self.engines.contains_key(&symbol) {
                    return Ok(Control::Continue);
                }
                // D1: `ts` is read when the command enters the journal.
                let ts = (self.clock)();
                if !self.advance(ts, batch)? {
                    self.held.push_back(JournalMsg::Command {
                        symbol,
                        kind,
                        command,
                        payload,
                    });
                    return Ok(Control::Continue);
                }
                let seq = self.alloc_seq();
                batch.records.push(Record {
                    seq,
                    kind,
                    symbol: Some(symbol.clone()),
                    ts,
                    prev_seq: None,
                    payload,
                });
                batch.deliveries.push(Delivery::Engine(
                    symbol,
                    EngineMsg::Command {
                        seq,
                        ts,
                        cmd: command,
                    },
                ));
            }
            JournalMsg::EngineOutput { symbol, outputs } => {
                for output in &outputs {
                    self.record_output(&symbol, output, batch);
                }
            }
            JournalMsg::EngineStopped { symbol } => {
                self.stopped.insert(symbol);
                if self.draining && self.stopped.len() == self.engines.len() {
                    return Ok(Control::Exit);
                }
            }
            JournalMsg::CloseCheck => {
                let ts = (self.clock)();
                self.advance(ts, batch)?;
            }
            JournalMsg::DayCloseDone { symbol } => {
                self.pending_close.remove(&symbol);
                if self.pending_close.is_empty() {
                    // Today's close is fully written: the commands that were
                    // waiting for the next day can go ahead.
                    for msg in std::mem::take(&mut self.held) {
                        if self.handle(msg, batch)? == Control::Exit {
                            return Ok(Control::Exit);
                        }
                    }
                }
            }
            JournalMsg::Shutdown => {
                self.draining = true;
                if self.engines.is_empty() {
                    return Ok(Control::Exit);
                }
                for symbol in self.engines.keys() {
                    batch
                        .deliveries
                        .push(Delivery::Engine(symbol.clone(), EngineMsg::Stop));
                }
            }
        }
        Ok(Control::Continue)
    }

    fn alloc_seq(&mut self) -> Seq {
        let seq = self.next_seq;
        self.next_seq += 1;
        seq
    }

    fn record_output(&mut self, symbol: &str, output: &Output, batch: &mut Batch) {
        let seq = self.alloc_seq();
        let prev_seq = self.last_output_seq;
        self.last_output_seq = seq;
        let rec = Outbound::new(&self.day, seq, prev_seq, symbol, output);
        let kind = match rec {
            Outbound::OrderEvent { .. } => Kind::OrderEvent,
            Outbound::Trade { .. } => Kind::Trade,
        };
        self.metrics
            .outputs_total
            .with_label_values(&[kind.as_str()])
            .inc();
        let payload = rec.to_json();
        batch
            .deliveries
            .push(Delivery::Out(Arc::from(payload.as_str())));
        batch.records.push(Record {
            seq,
            kind,
            symbol: Some(symbol.to_string()),
            ts: 0,
            prev_seq: Some(prev_seq),
            payload,
        });
    }

    /// Writes `DayClose` once the session has ended, and rolls the store over
    /// to a new day when `ts` falls on one. Returns false if the command that
    /// brought `ts` has to wait: the old day's close is still being written,
    /// and its cancels must land in the old day's store before the switch.
    fn advance(&mut self, ts: Timestamp, batch: &mut Batch) -> Result<bool> {
        let day = session::day_id(ts);
        if day > self.day {
            // A day ended without its close (the timer did not run). Close it
            // before leaving it behind, so no order carries over.
            if !self.closed_today {
                self.write_day_close(ts, batch);
            }
            if !self.pending_close.is_empty() {
                return Ok(false);
            }
            self.flush(batch)?;
            self.switch_day(day.clone())?;
        }
        if day == self.day && session::after_close(ts) && !self.closed_today {
            self.write_day_close(ts, batch);
        }
        Ok(true)
    }

    /// D22: `DayClose` goes into the store first, then every engine cancels.
    fn write_day_close(&mut self, ts: Timestamp, batch: &mut Batch) {
        let seq = self.alloc_seq();
        self.closed_today = true;
        self.pending_close = self.engines.keys().cloned().collect();
        batch.records.push(Record {
            seq,
            kind: Kind::DayClose,
            symbol: None,
            ts,
            prev_seq: None,
            payload: "{}".to_string(),
        });
        for symbol in self.engines.keys() {
            batch.deliveries.push(Delivery::Engine(
                symbol.clone(),
                EngineMsg::DayClose { seq, ts },
            ));
        }
    }

    /// A new trading day: a new store, `seq` from 1, nothing closed yet.
    fn switch_day(&mut self, day: String) -> Result<()> {
        self.store = EventStore::open(&self.dir, &day)?;
        debug_assert_eq!(self.store.path(), db_path(&self.dir, &day));
        self.next_seq = self.store.max_seq()? + 1;
        self.last_output_seq = self.store.last_output_seq()?;
        self.closed_today = false;
        *self.current_day.write().unwrap() = CurrentDay {
            day: day.clone(),
            path: self.store.path().to_path_buf(),
        };
        self.day = day;
        Ok(())
    }

    /// Commits the batch, and only then performs its deliveries.
    fn flush(&mut self, batch: &mut Batch) -> Result<()> {
        if !batch.records.is_empty() {
            if let Err(e) = self.store.append(&batch.records) {
                self.metrics.journal_failed.set(1);
                return Err(e).context("writing the event store");
            }
            batch.records.clear();
        }
        for delivery in batch.deliveries.drain(..) {
            match delivery {
                Delivery::Engine(symbol, msg) => {
                    if let Some(tx) = self.engines.get(&symbol) {
                        let _ = tx.send(msg);
                    }
                }
                Delivery::Out(text) => {
                    if self.hub.deliver(&text) {
                        self.metrics.outbound_overflows_total.inc();
                    }
                }
            }
        }
        Ok(())
    }
}
