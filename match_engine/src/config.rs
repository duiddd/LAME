//! Runtime configuration (D23): environment variables, plus the trading pairs
//! from the pair config file shared with A (`PAIRS_FILE`).

use std::net::SocketAddr;
use std::path::PathBuf;

use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use ipnet::IpNet;
use serde::Deserialize;

use crate::types::SymbolSpec;

#[derive(Clone, Debug)]
pub struct Config {
    /// Market-data WebSocket, `MD_ADDR`.
    pub md_addr: SocketAddr,
    /// D channel, `/healthz` and `/metrics`, `CTRL_ADDR`.
    pub ctrl_addr: SocketAddr,
    /// Sources allowed to connect to `CTRL_ADDR`, `CTRL_ALLOWED_CIDRS`. It limits
    /// the source only; it is not authentication.
    pub allowed_cidrs: Vec<IpNet>,
    /// Where the day's event stores live, `EVENT_DB_DIR`.
    pub event_db_dir: PathBuf,
    /// Outbound queue limit per D connection, `OUTBOUND_QUEUE_CAP`.
    pub outbound_queue_cap: usize,
    /// A subscriber that has heard nothing for this long is sent the last
    /// message again (D6). One second; tests shorten it.
    pub md_heartbeat: Duration,
    /// Trading pairs with their tick and lot, in the order the file lists them.
    pub symbols: Vec<(String, SymbolSpec)>,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Self> {
        let addr = |key: &str, default: &str| -> Result<SocketAddr> {
            get(key)
                .unwrap_or_else(|| default.into())
                .parse()
                .with_context(|| format!("{key} is not a socket address"))
        };
        // Loopback for the health check plus the private ranges Docker networks use.
        let cidrs = get("CTRL_ALLOWED_CIDRS").unwrap_or_else(|| {
            "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16".into()
        });
        let allowed_cidrs = cidrs
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|s| s.parse().with_context(|| format!("bad CIDR {s:?}")))
            .collect::<Result<Vec<IpNet>>>()?;
        let cap: usize = match get("OUTBOUND_QUEUE_CAP") {
            Some(v) => v.parse().context("OUTBOUND_QUEUE_CAP is not a number")?,
            None => 10_000,
        };
        if cap == 0 {
            return Err(anyhow!("OUTBOUND_QUEUE_CAP must be positive"));
        }
        let pairs_file = get("PAIRS_FILE").unwrap_or_else(|| DEFAULT_PAIRS_FILE.into());
        let text = std::fs::read_to_string(&pairs_file)
            .with_context(|| format!("reading the pair config file {pairs_file}"))?;
        let symbols =
            parse_pairs(&text).with_context(|| format!("pair config file {pairs_file}"))?;
        Ok(Self {
            md_addr: addr("MD_ADDR", "0.0.0.0:9999")?,
            ctrl_addr: addr("CTRL_ADDR", "0.0.0.0:11659")?,
            allowed_cidrs,
            event_db_dir: get("EVENT_DB_DIR").unwrap_or_else(|| "/data".into()).into(),
            outbound_queue_cap: cap,
            md_heartbeat: Duration::from_secs(1),
            symbols,
        })
    }
}

/// Where the pair config file is mounted unless `PAIRS_FILE` says otherwise.
pub const DEFAULT_PAIRS_FILE: &str = "/etc/lame/pairs.json";

/// The only `version` of the pair config file this build understands.
pub const PAIRS_FILE_VERSION: u32 = 1;

/// The pair config file, as far as B reads it. The file is shared with A and
/// carries more (`assets`, `base`, `quote`, the decimals, A's size limits);
/// fields B does not name here are ignored, so others can add theirs freely.
#[derive(Deserialize)]
struct PairsFile {
    version: u32,
    pairs: Vec<PairEntry>,
}

#[derive(Deserialize)]
struct PairEntry {
    symbol: String,
    /// Price grid, in the pair's integer price units.
    tick: i64,
    /// Quantity grid, in the pair's integer quantity units.
    lot: i64,
}

/// Parses and checks the pair config file. Any problem is an error, and the
/// process refuses to start: a wrong grid would reject or accept the wrong
/// orders for the whole day.
pub fn parse_pairs(text: &str) -> Result<Vec<(String, SymbolSpec)>> {
    let file: PairsFile = serde_json::from_str(text).context("not a valid pair config file")?;
    if file.version != PAIRS_FILE_VERSION {
        return Err(anyhow!(
            "version {} is not supported (expected {PAIRS_FILE_VERSION})",
            file.version
        ));
    }
    let mut out: Vec<(String, SymbolSpec)> = Vec::new();
    for p in file.pairs {
        // The name is a path segment in `/md/<symbol>`.
        let well_formed = !p.symbol.is_empty()
            && p.symbol
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'));
        if !well_formed {
            return Err(anyhow!(
                "symbol {:?} must be letters, digits, '-', '_' or '.'",
                p.symbol
            ));
        }
        if p.tick <= 0 || p.lot <= 0 {
            return Err(anyhow!("{}: tick and lot must be positive", p.symbol));
        }
        if out.iter().any(|(n, _)| *n == p.symbol) {
            return Err(anyhow!("symbol {:?} listed twice", p.symbol));
        }
        out.push((p.symbol, SymbolSpec::new(p.tick, p.lot)));
    }
    if out.is_empty() {
        return Err(anyhow!("no pairs configured"));
    }
    Ok(out)
}
