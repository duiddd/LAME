from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

OrderStatus = Literal[
    "PENDING", "SENT", "UNKNOWN", "PROCESSING", "MODIFYING", "PARTIALLY_FILLED",
    "FILLED", "CANCELLED", "REJECTED", "EXPIRED",
]


class OrderIn(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    type: Literal["LIMIT", "MARKET"]
    price: Optional[str] = None
    qty: str


class ModifyIn(BaseModel):
    """A modify changes the remaining quantity only, up or down; the price stays (team decision).

    Extra fields are refused, so a client that still sends a price is told so instead of having
    it silently ignored.
    """

    model_config = ConfigDict(extra="forbid")
    qty: str


class OrderOut(BaseModel):
    order_id: str
    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    type: Literal["LIMIT", "MARKET"]
    price: Optional[str]
    qty: str
    filled_qty: str
    settled_qty: str
    avg_fill_price: Optional[str]
    status: OrderStatus
    reason: Optional[str]
    cancel_requested: bool
    created_at: int
    updated_at: int


class FillOut(BaseModel):
    b_day: str
    b_seq: int
    order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    role: Literal["TAKER", "MAKER"]
    price: str
    qty: str
    ts: int


class OrderDetailOut(OrderOut):
    fills: list[FillOut]


class OrderListOut(BaseModel):
    orders: list[OrderOut]


class FillListOut(BaseModel):
    fills: list[FillOut]
