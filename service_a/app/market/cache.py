"""The newest snapshot per pair. Written by B's consumer, read by the price check."""
from dataclasses import dataclass

from ..ports import Clock


@dataclass(frozen=True)
class Snapshot:
    best_bid: int | None
    best_ask: int | None
    last_price: int | None
    last_ms: int

    @property
    def reference(self) -> int | None:
        """last_price, otherwise the midpoint, otherwise the one best price that exists."""
        if self.last_price is not None:
            return self.last_price
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) // 2
        return self.best_bid if self.best_bid is not None else self.best_ask


class PriceCache:
    def __init__(self, clock: Clock, stale_after_s: float):
        self._clock = clock
        self._stale_ms = stale_after_s * 1000
        self._snapshots: dict[str, Snapshot] = {}
        self._received: dict[str, int] = {}

    def update(self, symbol: str, message: dict) -> None:
        """Any message counts for freshness, because a heartbeat repeats last_ms unchanged."""
        self._received[symbol] = self._clock.now_ms()
        current = self._snapshots.get(symbol)
        if current is not None and message["last_ms"] < current.last_ms:
            return
        bids, asks = message["bids"], message["asks"]
        self._snapshots[symbol] = Snapshot(
            best_bid=bids[0]["price"] if bids else None,
            best_ask=asks[0]["price"] if asks else None,
            last_price=message["last_price"],
            last_ms=message["last_ms"],
        )

    def forget_order(self, symbol: str) -> None:
        """B may have restarted with a new day, whose last_ms starts from 0 again."""
        snapshot = self._snapshots.get(symbol)
        if snapshot is not None:
            self._snapshots[symbol] = Snapshot(snapshot.best_bid, snapshot.best_ask, snapshot.last_price, -1)

    def age_s(self, symbol: str) -> float | None:
        received = self._received.get(symbol)
        return None if received is None else (self._clock.now_ms() - received) / 1000

    def fresh(self, symbol: str) -> Snapshot | None:
        received = self._received.get(symbol)
        if received is None or self._clock.now_ms() - received > self._stale_ms:
            return None
        return self._snapshots.get(symbol)
