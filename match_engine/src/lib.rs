//! Price-time priority matching core for Service B.
//!
//! The core is pure computation: no I/O, no clock, no randomness (design
//! decisions D1, D18). Sequence numbers come from the caller.

pub mod book;
pub mod config;
pub mod conn;
pub mod engine;
pub mod engine_task;
pub mod eod;
pub mod healthcheck;
pub mod http;
pub mod journal;
pub mod md;
pub mod metrics;
pub mod recovery;
pub mod runtime;
pub mod service;
pub mod session;
pub mod store;
pub mod types;
pub mod wire;

pub use book::OrderBook;
pub use engine::{MatchEngine, MAX_COMMAND_AGE};
pub use types::{
    BookLevel, CancelTarget, Command, MarketData, NewOrder, Order, OrderEvent, OrderId, Output,
    Price, Qty, Reason, Seq, Side, Snapshot, Status, SymbolSpec, Timestamp, Trade, UserId, DEPTH,
};
