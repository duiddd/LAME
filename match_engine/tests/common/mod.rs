//! Test-only adapter for a line-oriented CSV protocol
//! (`0,id,side,qty,price` / `1,id`, four-decimal prices). It is not part of
//! the service; it lets fixtures and datasets written in that format drive the
//! engine.
#![allow(dead_code)]

use match_engine::{CancelTarget, Command, MatchEngine, NewOrder, Output, Side, Status};

#[derive(Debug, PartialEq, Eq)]
pub enum Request {
    Add {
        id: u64,
        side: Side,
        price: i64,
        qty: i64,
    },
    Cancel {
        id: u64,
    },
}

const PRECISION: usize = 4;

fn parse_price(text: &str) -> Option<i64> {
    let (int_part, frac_part) = match text.find('.') {
        Some(dot) => (&text[..dot], &text[dot + 1..]),
        None => (text, ""),
    };
    if int_part.is_empty()
        || frac_part.len() > PRECISION
        || (text.contains('.') && frac_part.is_empty())
    {
        return None;
    }
    let mut scaled: i64 = 0;
    let digits = int_part
        .chars()
        .chain(frac_part.chars())
        .map(Some)
        .chain(std::iter::repeat_n(Some('0'), PRECISION - frac_part.len()));
    for c in digits {
        let d = c?.to_digit(10)? as i64;
        scaled = scaled.checked_mul(10)?.checked_add(d)?;
    }
    (scaled != 0).then_some(scaled)
}

fn parse_uint<T: std::str::FromStr>(text: &str) -> Option<T> {
    if text.is_empty() || !text.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

pub fn parse_request(line: &str) -> Option<Request> {
    const WS: &[char] = &[' ', '\t', '\r', '\n', '\x0c', '\x0b'];
    let fields: Vec<&str> = line.split(',').map(|f| f.trim_matches(WS)).collect();
    if fields.iter().any(|f| f.is_empty()) {
        return None;
    }
    match (fields.len(), fields[0]) {
        (5, "0") => {
            let id: i32 = parse_uint(fields[1])?;
            let side: i32 = parse_uint(fields[2])?;
            let qty: i64 = parse_uint(fields[3])?;
            if id <= 0 || qty <= 0 {
                return None;
            }
            let side = match side {
                0 => Side::Buy,
                1 => Side::Sell,
                _ => return None,
            };
            Some(Request::Add {
                id: id as u64,
                side,
                price: parse_price(fields[4])?,
                qty,
            })
        }
        (2, "1") => {
            let id: i32 = parse_uint(fields[1])?;
            (id > 0).then_some(Request::Cancel { id: id as u64 })
        }
        _ => None,
    }
}

fn format_price(scaled: i64) -> String {
    let mut s = format!("{:0>width$}", scaled, width = PRECISION + 1);
    s.insert(s.len() - PRECISION, '.');
    let s = s.trim_end_matches('0');
    s.trim_end_matches('.').to_string()
}

/// The three-event view printed by the CSV protocol.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LineEvent {
    Trade { price: i64, qty: i64 },
    Fill { order_id: u64 },
    PartialFill { order_id: u64, qty_remain: i64 },
}

pub fn format_event(e: &LineEvent) -> String {
    match e {
        LineEvent::Trade { price, qty } => format!("2,{qty},{}", format_price(*price)),
        LineEvent::Fill { order_id } => format!("3,{order_id}"),
        LineEvent::PartialFill {
            order_id,
            qty_remain,
        } => format!("4,{order_id},{qty_remain}"),
    }
}

/// Drives the engine from CSV requests: sequence numbers are handed out
/// in arrival order and each order's `user_id` equals its `order_id`, so the
/// tests never involve two orders of one user.
#[derive(Default)]
pub struct Harness {
    pub engine: MatchEngine,
    next_seq: u64,
}

impl Harness {
    pub fn new() -> Self {
        Self::default()
    }

    fn seq(&mut self) -> u64 {
        self.next_seq += 1;
        self.next_seq
    }

    pub fn add(&mut self, id: u64, side: Side, price: i64, qty: i64) {
        let seq = self.seq();
        self.engine.process(
            seq,
            Command::New(NewOrder {
                order_id: id,
                user_id: id,
                side,
                price,
                qty,
            }),
        );
    }

    /// Returns false if the engine rejected the cancel.
    pub fn cancel(&mut self, id: u64) -> bool {
        let seq = self.seq();
        let before = self.engine.outputs().len();
        self.engine.process(
            seq,
            Command::Cancel(CancelTarget::ById {
                user_id: id,
                order_id: id,
            }),
        );
        !self.engine.outputs()[before..]
            .iter()
            .any(|o| matches!(o, Output::Order(e) if e.status == Status::Rejected))
    }

    /// Drains the queue, keeping only what the CSV protocol prints.
    pub fn drain_lines(&mut self) -> Vec<LineEvent> {
        self.engine
            .drain_outputs()
            .filter_map(|o| match o {
                Output::Trade(t) => Some(LineEvent::Trade {
                    price: t.price,
                    qty: t.qty,
                }),
                Output::Order(e) => {
                    let order_id = e.order_id?;
                    match e.status {
                        Status::Filled => Some(LineEvent::Fill { order_id }),
                        Status::PartiallyFilled => Some(LineEvent::PartialFill {
                            order_id,
                            qty_remain: e.remaining,
                        }),
                        _ => None,
                    }
                }
            })
            .collect()
    }
}

/// Feeds one input line to the harness. Returns false for an invalid or
/// rejected request.
pub fn apply_line(h: &mut Harness, line: &str) -> bool {
    match parse_request(line) {
        Some(Request::Add {
            id,
            side,
            price,
            qty,
        }) => {
            h.add(id, side, price, qty);
            true
        }
        Some(Request::Cancel { id }) => h.cancel(id),
        None => false,
    }
}
