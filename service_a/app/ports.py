"""The small ports tests replace. D's and B's ports live with their adapters."""
import time
from typing import Protocol

from ulid import ULID


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)


class IdGenerator(Protocol):
    def order_id(self) -> str: ...


class UlidGenerator:
    def order_id(self) -> str:
        return str(ULID())
