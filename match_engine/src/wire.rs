//! JSON messages on the D channel (D13, D17, D21).
//!
//! Inbound, from D: a `resume` handshake first, then `new_order` and
//! `cancel_order`. Outbound, to D: `order_event` and `trade` records exactly as
//! stored in the event store, plus `error` for rejected handshakes and
//! malformed messages. The message kind is `type`; the order kind is
//! `order_type`, so the two never clash.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::types::{
    CancelTarget, Command, NewOrder, OrderEvent, OrderId, Output, Price, Qty, Reason, Seq, Side,
    Status, Timestamp, UserId,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum WireSide {
    Buy,
    Sell,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum OrderType {
    Limit,
    Market,
}

/// Where an order comes from. Liquidation is not used in the core (RD20) but
/// is accepted so the field can stay in the contract.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Origin {
    #[default]
    User,
    Liquidation,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct NewOrderMsg {
    pub symbol: String,
    pub order_id: OrderId,
    pub user_id: UserId,
    pub side: WireSide,
    pub order_type: OrderType,
    /// Required for a limit order, ignored for a market order.
    #[serde(default)]
    pub price: Option<Price>,
    pub qty: Qty,
    #[serde(default)]
    pub origin: Origin,
    /// When D sent the command, Unix ms (D15).
    pub sent_at: Timestamp,
}

/// Names the target either as `order_seq` or as `user_id` plus `order_id`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CancelOrderMsg {
    pub symbol: String,
    #[serde(default)]
    pub user_id: Option<UserId>,
    #[serde(default)]
    pub order_id: Option<OrderId>,
    #[serde(default)]
    pub order_seq: Option<Seq>,
    #[serde(default)]
    pub origin: Origin,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Inbound {
    /// Must be the first message on a connection: send every output of today
    /// with `seq` greater than `from_seq` (0 = from the start of the day).
    Resume {
        from_seq: Seq,
    },
    NewOrder(NewOrderMsg),
    CancelOrder(CancelOrderMsg),
}

/// A validated command and the symbol it is for.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Parsed {
    pub symbol: String,
    pub command: Command,
}

/// Parses one text frame into a command. `resume` is not a command and is an
/// error here; the connection handles it separately.
pub fn parse_command(text: &str) -> Result<Parsed, String> {
    let msg: Inbound = serde_json::from_str(text).map_err(|e| format!("malformed message: {e}"))?;
    match msg {
        Inbound::Resume { .. } => Err("resume is only allowed as the first message".into()),
        Inbound::NewOrder(m) => {
            let price = match m.order_type {
                OrderType::Limit => Some(m.price.ok_or("a limit order needs a price")?),
                OrderType::Market => None,
            };
            Ok(Parsed {
                symbol: m.symbol,
                command: Command::New(NewOrder {
                    order_id: m.order_id,
                    user_id: m.user_id,
                    side: match m.side {
                        WireSide::Buy => Side::Buy,
                        WireSide::Sell => Side::Sell,
                    },
                    price,
                    qty: m.qty,
                    sent_at: m.sent_at,
                }),
            })
        }
        Inbound::CancelOrder(m) => {
            let target = match (m.order_seq, m.user_id, m.order_id) {
                (Some(seq), None, None) => CancelTarget::BySeq(seq),
                (None, Some(user_id), Some(order_id)) => CancelTarget::ById { user_id, order_id },
                _ => {
                    return Err(
                        "a cancel names either order_seq alone, or user_id and order_id".into(),
                    )
                }
            };
            Ok(Parsed {
                symbol: m.symbol,
                command: Command::Cancel(target),
            })
        }
    }
}

/// An output record as stored and as sent to D.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Outbound {
    OrderEvent {
        day: String,
        seq: Seq,
        prev_seq: Seq,
        symbol: String,
        order_seq: Option<Seq>,
        order_id: Option<OrderId>,
        user_id: Option<UserId>,
        status: Status,
        filled: Qty,
        remaining: Qty,
        reason: Option<Reason>,
    },
    Trade {
        day: String,
        seq: Seq,
        prev_seq: Seq,
        symbol: String,
        price: Price,
        qty: Qty,
        taker_seq: Seq,
        maker_seq: Seq,
        taker_user_id: UserId,
        maker_user_id: UserId,
    },
}

impl Outbound {
    pub fn new(day: &str, seq: Seq, prev_seq: Seq, symbol: &str, out: &Output) -> Self {
        match *out {
            Output::Order(OrderEvent {
                order_seq,
                order_id,
                user_id,
                status,
                filled,
                remaining,
                reason,
            }) => Outbound::OrderEvent {
                day: day.into(),
                seq,
                prev_seq,
                symbol: symbol.into(),
                order_seq,
                order_id,
                user_id,
                status,
                filled,
                remaining,
                reason,
            },
            Output::Trade(t) => Outbound::Trade {
                day: day.into(),
                seq,
                prev_seq,
                symbol: symbol.into(),
                price: t.price,
                qty: t.qty,
                taker_seq: t.taker_seq,
                maker_seq: t.maker_seq,
                taker_user_id: t.taker_user_id,
                maker_user_id: t.maker_user_id,
            },
        }
    }

    pub fn to_json(&self) -> String {
        serde_json::to_string(self).expect("outbound records always serialize")
    }
}

/// The content of an output record with its own `seq` and `prev_seq` removed.
/// Recovery compares this, because those two depend on how commands and
/// outputs interleaved when the record was written (D1, D10).
pub fn content(record_json: &str) -> Result<Value, serde_json::Error> {
    let mut v: Value = serde_json::from_str(record_json)?;
    if let Value::Object(map) = &mut v {
        map.remove("seq");
        map.remove("prev_seq");
    }
    Ok(v)
}

/// What an engine output looks like once stored, without `seq`/`prev_seq`.
pub fn expected_content(day: &str, symbol: &str, out: &Output) -> Value {
    content(&Outbound::new(day, 0, 0, symbol, out).to_json()).expect("just serialized")
}

/// The reply to an accepted `resume`, always the first message on the
/// connection. `through_seq` is the `seq` of the last output stored when the
/// handshake was served: once D holds every output up to it, the catch-up is
/// over and everything after it is live.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ResumeOk {
    #[serde(rename = "type")]
    pub kind: &'static str,
    pub from_seq: Seq,
    pub through_seq: Seq,
}

pub fn resume_ok_json(from_seq: Seq, through_seq: Seq) -> String {
    serde_json::to_string(&ResumeOk {
        kind: "resume_ok",
        from_seq,
        through_seq,
    })
    .expect("resume replies always serialize")
}

/// A message to D that is not a record: a refused handshake or a bad frame.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ErrorMsg<'a> {
    #[serde(rename = "type")]
    pub kind: &'static str,
    pub code: &'a str,
    pub message: &'a str,
}

pub fn error_json(code: &str, message: &str) -> String {
    serde_json::to_string(&ErrorMsg {
        kind: "error",
        code,
        message,
    })
    .expect("error messages always serialize")
}
