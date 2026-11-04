//! Runtime configuration from environment variables (D23).

use std::net::SocketAddr;
use std::path::PathBuf;

use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use ipnet::IpNet;

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
    /// Trading pairs with their tick and lot.
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
        // TODO: read the trading pairs and their tick/lot from the mounted
        // config file (D23). The file format is not decided yet, so for now
        // they come from SYMBOLS, formatted `NAME:tick:lot,NAME:tick:lot`.
        let symbols = parse_symbols(&get("SYMBOLS").ok_or_else(|| {
            anyhow!("SYMBOLS is not set (temporary stand-in for the symbol config file)")
        })?)?;
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

pub fn parse_symbols(text: &str) -> Result<Vec<(String, SymbolSpec)>> {
    let mut out: Vec<(String, SymbolSpec)> = Vec::new();
    for item in text.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        let parts: Vec<&str> = item.split(':').collect();
        let [name, tick, lot] = parts[..] else {
            return Err(anyhow!("symbol {item:?} is not NAME:tick:lot"));
        };
        let tick: i64 = tick.parse().with_context(|| format!("tick in {item:?}"))?;
        let lot: i64 = lot.parse().with_context(|| format!("lot in {item:?}"))?;
        if name.is_empty() || tick <= 0 || lot <= 0 {
            return Err(anyhow!(
                "symbol {item:?} needs a name and positive tick/lot"
            ));
        }
        if out.iter().any(|(n, _)| n == name) {
            return Err(anyhow!("symbol {name:?} listed twice"));
        }
        out.push((name.to_string(), SymbolSpec::new(tick, lot)));
    }
    if out.is_empty() {
        return Err(anyhow!("no symbols configured"));
    }
    Ok(out)
}
