//! Price-time priority matching core for Service B.
//!
//! The core is pure computation: no I/O, no clock, no randomness (design
//! decisions D1, D18). Sequence numbers come from the caller.

pub mod book;
pub mod engine;
pub mod types;

pub use book::OrderBook;
pub use engine::MatchEngine;
pub use types::{
    CancelTarget, Command, NewOrder, Order, OrderEvent, OrderId, Output, Price, Qty, Reason, Seq,
    Side, Status, SymbolSpec, Trade, UserId,
};
