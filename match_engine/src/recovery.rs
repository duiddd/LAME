//! Start-up and recovery (D10): rebuild every engine from today's event store
//! before any order is taken.
//!
//! The commands in the store are replayed, in `seq` order, with the `seq` and
//! `ts` they were stored with. The outputs the engines produce are compared,
//! one by one and per pair, with the outputs already stored, ignoring those
//! outputs' own `seq` and `prev_seq` (D1). Three outcomes:
//!
//! - everything matches: the engines now hold exactly yesterday's state, so
//!   serving can begin;
//! - an engine produced fewer outputs than were stored, or a stored output
//!   differs: B refuses to start and says why, because carrying on from a
//!   state that disagrees with what was already published would be unfair;
//! - the store holds commands whose outputs were never written (a crash between
//!   the two): the missing outputs are now written, and D can pick them up with
//!   its resume handshake.
//!
//! Replay calls no other service and publishes nothing.

use std::collections::{HashMap, VecDeque};

use anyhow::{anyhow, bail, Context, Result};

use crate::config::Config;
use crate::engine::MatchEngine;
use crate::journal::JournalInit;
use crate::session;
use crate::store::{DirLock, EventStore, Kind, Record};
use crate::types::{Command, Output, Seq, Timestamp};
use crate::wire::{content, expected_content, parse_command, Outbound};

pub struct Recovered {
    pub engines: Vec<(String, MatchEngine)>,
    pub init: JournalInit,
    /// How many stored records were replayed.
    pub replayed_records: u64,
}

/// Outputs an engine has produced that the store has not yet matched.
type Expected = HashMap<String, VecDeque<Output>>;

pub fn recover(config: &Config, now: Timestamp) -> Result<Recovered> {
    let lock = DirLock::acquire(&config.event_db_dir)?;
    let day = session::day_id(now);
    let mut store = EventStore::open(&config.event_db_dir, &day)?;

    let mut engines: HashMap<String, MatchEngine> = config
        .symbols
        .iter()
        .map(|(name, spec)| {
            (
                name.clone(),
                MatchEngine::new(*spec).with_session(session::in_session),
            )
        })
        .collect();
    let mut expected: Expected = config
        .symbols
        .iter()
        .map(|(name, _)| (name.clone(), VecDeque::new()))
        .collect();

    let mut replayed = 0u64;
    let mut max_seq: Seq = 0;
    let mut last_output: Seq = 0;
    let mut closed_today = false;

    store.scan(0, |rec| {
        replayed += 1;
        max_seq = rec.seq;
        match rec.kind {
            Kind::NewOrder | Kind::CancelOrder => {
                let symbol = rec.symbol.clone().unwrap_or_default();
                let parsed = parse_command(&rec.payload)
                    .map_err(|e| anyhow!("stored command {} is unreadable: {e}", rec.seq))?;
                if parsed.symbol != symbol {
                    bail!(
                        "stored command {} names {} but is filed under {symbol}",
                        rec.seq,
                        parsed.symbol
                    );
                }
                let engine = engines.get_mut(&symbol).ok_or_else(|| {
                    anyhow!("the store has commands for {symbol}, which is not a configured pair")
                })?;
                engine.process(rec.seq, rec.ts, parsed.command);
                if let Some(queue) = expected.get_mut(&symbol) {
                    queue.extend(engine.drain_outputs());
                }
            }
            Kind::DayClose => {
                closed_today = true;
                for (symbol, engine) in engines.iter_mut() {
                    engine.process(rec.seq, rec.ts, Command::DayClose);
                    if let Some(queue) = expected.get_mut(symbol) {
                        queue.extend(engine.drain_outputs());
                    }
                }
            }
            Kind::OrderEvent | Kind::Trade => {
                check_output(&rec, &day, &mut expected, &mut last_output)?;
            }
        }
        Ok(())
    })?;

    // Outputs the engines produced that were never stored: write them now.
    let mut next_seq = max_seq + 1;
    let mut missing: Vec<Record> = Vec::new();
    for (symbol, _) in &config.symbols {
        let Some(queue) = expected.get_mut(symbol) else {
            continue;
        };
        for output in queue.drain(..) {
            let seq = next_seq;
            next_seq += 1;
            let rec = Outbound::new(&day, seq, last_output, symbol, &output);
            let kind = match rec {
                Outbound::OrderEvent { .. } => Kind::OrderEvent,
                Outbound::Trade { .. } => Kind::Trade,
            };
            missing.push(Record {
                seq,
                kind,
                symbol: Some(symbol.clone()),
                ts: 0,
                prev_seq: Some(last_output),
                payload: rec.to_json(),
            });
            last_output = seq;
        }
    }
    store
        .append(&missing)
        .context("writing the outputs that a crash left unwritten")?;
    if !missing.is_empty() {
        tracing::warn!(
            count = missing.len(),
            "wrote outputs that were missing from the store"
        );
    }

    // Engines in the order the pairs are configured.
    let engines = config
        .symbols
        .iter()
        .map(|(name, _)| (name.clone(), engines.remove(name).expect("one per pair")))
        .collect();
    Ok(Recovered {
        engines,
        init: JournalInit {
            dir: config.event_db_dir.clone(),
            day,
            store,
            next_seq,
            last_output_seq: last_output,
            closed_today,
            lock,
        },
        replayed_records: replayed,
    })
}

/// Checks one stored output against what replay produced for the same pair.
fn check_output(
    rec: &Record,
    day: &str,
    expected: &mut Expected,
    last_output: &mut Seq,
) -> Result<()> {
    let symbol = rec.symbol.clone().unwrap_or_default();
    if rec.prev_seq != Some(*last_output) {
        bail!(
            "the chain of outputs is broken at seq {}: prev_seq is {:?}, the previous output is {}",
            rec.seq,
            rec.prev_seq,
            last_output
        );
    }
    *last_output = rec.seq;

    let want = expected
        .get_mut(&symbol)
        .and_then(VecDeque::pop_front)
        .ok_or_else(|| {
            anyhow!(
                "replay mismatch at seq {} for {symbol}: the store has an output that replay does not produce",
                rec.seq
            )
        })?;
    let got = content(&rec.payload)
        .with_context(|| format!("stored output {} is unreadable", rec.seq))?;
    let want = expected_content(day, &symbol, &want);
    if got != want {
        bail!(
            "replay mismatch at seq {} for {symbol}: stored {got}, replay produced {want}",
            rec.seq
        );
    }
    Ok(())
}
