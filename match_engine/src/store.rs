//! The event store (D9 to D12): one SQLite database per trading day holding
//! the day's commands and outputs in a single total order by `seq`.
//!
//! Only the journal thread writes. Resend uses separate read-only
//! connections. A lock file guarantees a single writing instance.

use std::fs::{self, File};
use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use rusqlite::{params, Connection, OpenFlags};

use crate::types::{Seq, Timestamp};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    NewOrder,
    CancelOrder,
    DayClose,
    OrderEvent,
    Trade,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::NewOrder => "NewOrder",
            Kind::CancelOrder => "CancelOrder",
            Kind::DayClose => "DayClose",
            Kind::OrderEvent => "OrderEvent",
            Kind::Trade => "Trade",
        }
    }

    fn parse(s: &str) -> Result<Self> {
        Ok(match s {
            "NewOrder" => Kind::NewOrder,
            "CancelOrder" => Kind::CancelOrder,
            "DayClose" => Kind::DayClose,
            "OrderEvent" => Kind::OrderEvent,
            "Trade" => Kind::Trade,
            other => return Err(anyhow!("unknown record kind {other:?}")),
        })
    }

    pub fn is_output(self) -> bool {
        matches!(self, Kind::OrderEvent | Kind::Trade)
    }
}

/// One row of the store.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Record {
    pub seq: Seq,
    pub kind: Kind,
    pub symbol: Option<String>,
    pub ts: Timestamp,
    /// Set on outputs only: `seq` of the previous output, 0 for the first.
    pub prev_seq: Option<Seq>,
    /// JSON (D17).
    pub payload: String,
}

/// Holds an exclusive lock on `<dir>/b.lock` for as long as it lives.
pub struct DirLock {
    _file: File,
}

impl DirLock {
    pub fn acquire(dir: &Path) -> Result<Self> {
        fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
        let path = dir.join("b.lock");
        let file = File::options()
            .create(true)
            .truncate(false)
            .write(true)
            .open(&path)
            .with_context(|| format!("opening {}", path.display()))?;
        file.try_lock()
            .map_err(|e| anyhow!("another instance holds {}: {e}", path.display()))?;
        Ok(Self { _file: file })
    }
}

pub fn db_path(dir: &Path, day: &str) -> PathBuf {
    dir.join(format!("events-{day}.db"))
}

pub struct EventStore {
    conn: Connection,
    path: PathBuf,
}

impl EventStore {
    /// Opens (creating if needed) the store for `day` in `dir`.
    pub fn open(dir: &Path, day: &str) -> Result<Self> {
        fs::create_dir_all(dir)?;
        let path = db_path(dir, day);
        let conn =
            Connection::open(&path).with_context(|| format!("opening {}", path.display()))?;
        conn.query_row("PRAGMA journal_mode=WAL", [], |_| Ok(()))?;
        // Survives a process crash, not a power cut (D12).
        conn.execute_batch("PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY;")?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                symbol TEXT,
                ts INTEGER NOT NULL,
                prev_seq INTEGER,
                payload TEXT NOT NULL
            )",
        )?;
        Ok(Self { conn, path })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Appends `records` in one transaction. Either all of them are stored or
    /// none, so there is never a half-written record.
    pub fn append(&mut self, records: &[Record]) -> Result<()> {
        if records.is_empty() {
            return Ok(());
        }
        let tx = self.conn.transaction()?;
        {
            let mut stmt = tx.prepare_cached(
                "INSERT INTO events (seq, kind, symbol, ts, prev_seq, payload)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            )?;
            for r in records {
                stmt.execute(params![
                    r.seq as i64,
                    r.kind.as_str(),
                    r.symbol,
                    r.ts,
                    r.prev_seq.map(|s| s as i64),
                    r.payload
                ])?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    pub fn max_seq(&self) -> Result<Seq> {
        max_seq(&self.conn)
    }

    /// `seq` of the last output stored, 0 if there is none.
    pub fn last_output_seq(&self) -> Result<Seq> {
        last_output_seq(&self.conn)
    }

    /// Calls `f` for every record with `seq > after`, in `seq` order.
    pub fn scan(&self, after: Seq, f: impl FnMut(Record) -> Result<()>) -> Result<()> {
        scan(&self.conn, after, false, f)
    }
}

fn last_output_seq(conn: &Connection) -> Result<Seq> {
    let v: i64 = conn.query_row(
        "SELECT COALESCE(MAX(seq), 0) FROM events WHERE kind IN ('OrderEvent', 'Trade')",
        [],
        |r| r.get(0),
    )?;
    Ok(v as Seq)
}

fn max_seq(conn: &Connection) -> Result<Seq> {
    let v: i64 = conn.query_row("SELECT COALESCE(MAX(seq), 0) FROM events", [], |r| r.get(0))?;
    Ok(v as Seq)
}

fn scan(
    conn: &Connection,
    after: Seq,
    outputs_only: bool,
    mut f: impl FnMut(Record) -> Result<()>,
) -> Result<()> {
    let sql = if outputs_only {
        "SELECT seq, kind, symbol, ts, prev_seq, payload FROM events
         WHERE seq > ?1 AND kind IN ('OrderEvent', 'Trade') ORDER BY seq"
    } else {
        "SELECT seq, kind, symbol, ts, prev_seq, payload FROM events
         WHERE seq > ?1 ORDER BY seq"
    };
    let mut stmt = conn.prepare_cached(sql)?;
    let mut rows = stmt.query(params![after as i64])?;
    while let Some(row) = rows.next()? {
        let kind: String = row.get(1)?;
        f(Record {
            seq: row.get::<_, i64>(0)? as Seq,
            kind: Kind::parse(&kind)?,
            symbol: row.get(2)?,
            ts: row.get(3)?,
            prev_seq: row.get::<_, Option<i64>>(4)?.map(|s| s as Seq),
            payload: row.get(5)?,
        })?;
    }
    Ok(())
}

/// A read-only connection to a day's store, for resend (D21). It can run
/// while the journal thread writes.
pub struct Reader {
    conn: Connection,
}

impl Reader {
    pub fn open(path: &Path) -> Result<Self> {
        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .with_context(|| format!("opening {} read-only", path.display()))?;
        Ok(Self { conn })
    }

    pub fn max_seq(&self) -> Result<Seq> {
        max_seq(&self.conn)
    }

    /// `seq` of the last output stored, 0 if there is none.
    pub fn last_output_seq(&self) -> Result<Seq> {
        last_output_seq(&self.conn)
    }

    /// Calls `f` for every output with `seq > after`, in `seq` order.
    pub fn scan_outputs(&self, after: Seq, f: impl FnMut(Record) -> Result<()>) -> Result<()> {
        scan(&self.conn, after, true, f)
    }
}
