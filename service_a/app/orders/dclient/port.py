"""What A needs from D. Tests replace the HTTP adapter with a fake that can fail on demand."""
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Delivery(Enum):
    """The kind of answer decides what A may tell the user (D12)."""

    ACK = "ack"                  # D received it
    NOT_TAKEN = "not_taken"      # certainly not received: no connection, or D's 503 (no record)
    INVALID = "invalid"          # D's 400: no record kept
    CONFLICT = "conflict"        # D's 409: a bug in A
    UNKNOWN = "unknown"          # sent, no usable reply: D may or may not have it
    NOT_IMPLEMENTED = "not_implemented"


@dataclass(frozen=True)
class Reply:
    delivery: Delivery
    detail: str = ""


@dataclass(frozen=True)
class OrderQuery:
    state: str | None
    last_notify_seq: int | None
    found: bool


class DClient(Protocol):
    async def place(self, order: dict) -> Reply: ...
    async def cancel(self, user_id: int, order_id: str) -> Reply: ...
    async def modify(self, user_id: int, order_id: str, price: int | None, qty: int) -> Reply: ...
    async def query(self, user_id: int, order_id: str) -> OrderQuery | None: ...
    async def rewind(self, from_seq: int) -> bool: ...
    async def close(self) -> None: ...
