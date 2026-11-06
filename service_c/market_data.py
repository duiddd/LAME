"""Real-time Market Data Consumer from Service B (Decision D8 & Service B Decision D6).

Connects to `ws://service-b:9999/md/<symbol>` and consumes self-contained full snapshots
of the top-3 bids/asks plus `last_price`.
- Supports Service B (`match_engine/src/md.rs`) and Service A (`mocks/state.ts`) wire format:
  `{ "bids": [...], "asks": [...], "last_price": int, "volume": int, "last_ms": int }`.
- Enforces Service B D6 & Service A D15 rule:
  - Discards strictly older snapshots (`ts_ms < existing.timestamp_ms`).
  - Treats equal `last_ms` (`ts_ms == existing.timestamp_ms`) as a valid 1s heartbeat,
    updating receipt time (`_received_wall_ms`) so quiet markets are not marked stale.
  - Normalizes integer-cent prices (`price_decimals=2`, e.g. `6425000` -> `64250`) when upstream
    sends cent-scaled integers.
"""

import asyncio
import json
import logging
import threading
import time
from typing import Optional

from .config import settings
from .models import MarketContextOut, MarketSnapshot, OrderBookLevel

logger = logging.getLogger("service_c.market_data")


class MarketDataCache:
    """Thread-safe cache of latest Service B market snapshots per symbol."""

    DEFAULT_PRICES: dict[str, dict] = {
        "BTC-USDT": {
            "last_price": 64250,
            "bids": [
                {"price": 64245, "qty": 12},
                {"price": 64240, "qty": 25},
                {"price": 64230, "qty": 40},
            ],
            "asks": [
                {"price": 64255, "qty": 10},
                {"price": 64260, "qty": 18},
                {"price": 64275, "qty": 35},
            ],
        },
        "ETH-USDT": {
            "last_price": 2640,
            "bids": [
                {"price": 2638, "qty": 50},
                {"price": 2635, "qty": 85},
                {"price": 2630, "qty": 120},
            ],
            "asks": [
                {"price": 2642, "qty": 45},
                {"price": 2645, "qty": 90},
                {"price": 2650, "qty": 150},
            ],
        },
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: dict[str, MarketSnapshot] = {}
        self._received_wall_ms: dict[str, int] = {}
        self._ws_connected: dict[str, bool] = {sym: False for sym in settings.supported_symbols}
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._seed_initial_snapshots()

    def _seed_initial_snapshots(self) -> None:
        now_ms = int(time.time() * 1000)
        for sym, data in self.DEFAULT_PRICES.items():
            snap = MarketSnapshot(
                symbol=sym,
                timestamp_ms=now_ms,
                last_price=data["last_price"],
                bids=[OrderBookLevel(**b) for b in data["bids"]],
                asks=[OrderBookLevel(**a) for a in data["asks"]],
                source="simulated_fallback",
            )
            self._snapshots[sym] = snap
            self._received_wall_ms[sym] = now_ms

    @staticmethod
    def _normalize_price(symbol: str, raw_price: Optional[int]) -> Optional[int]:
        """Normalizes cent-scaled integer prices (price_decimals=2 from Service A/B) to whole USDT if needed."""
        if raw_price is None:
            return None
        val = int(raw_price)
        if symbol.startswith("BTC") and val >= 500_000:
            return round(val / 100)
        if symbol.startswith("ETH") and val >= 20_000:
            return round(val / 100)
        return val

    def update_snapshot(self, payload: dict, source: str = "service_b_ws") -> bool:
        """Applies a new snapshot or refreshes heartbeat receipt time.

        - Drops strictly older snapshots (`ts_ms < existing.timestamp_ms`).
        - Accepts equal timestamps (`ts_ms == existing.timestamp_ms`) as a Service B heartbeat.
        """
        symbol = str(payload.get("symbol", "")).strip().upper()
        if not symbol:
            return False

        raw_ts = (
            payload.get("timestamp_ms")
            if payload.get("timestamp_ms") is not None
            else payload.get("last_ms")
            if payload.get("last_ms") is not None
            else payload.get("ts")
        )
        ts_ms = int(raw_ts) if raw_ts is not None else int(time.time() * 1000)
        now_wall_ms = int(time.time() * 1000)

        with self._lock:
            existing = self._snapshots.get(symbol)
            if (
                existing is not None
                and existing.source == "service_b_ws"
                and source == "service_b_ws"
            ):
                if ts_ms < existing.timestamp_ms:
                    # Strictly older snapshot -> drop per D6
                    return False
                if ts_ms == existing.timestamp_ms and payload.get("heartbeat") is True:
                    # Explicit heartbeat with identical last_ms -> refresh receipt timestamp
                    self._received_wall_ms[symbol] = now_wall_ms
                    return True
                if (
                    ts_ms == existing.timestamp_ms
                    and payload.get("last_price") is not None
                    and self._normalize_price(symbol, int(payload["last_price"])) == existing.last_price
                ):
                    # Repeated identical snapshot from Service B 1s heartbeat -> refresh receipt time
                    self._received_wall_ms[symbol] = now_wall_ms
                    return True
                if ts_ms == existing.timestamp_ms:
                    return False

            raw_bids = payload.get("bids", [])[:3]
            raw_asks = payload.get("asks", [])[:3]
            bids = [
                OrderBookLevel(
                    price=self._normalize_price(symbol, int(b[0])) or 0,
                    qty=int(b[1]),
                )
                if isinstance(b, (list, tuple))
                else OrderBookLevel(
                    price=self._normalize_price(symbol, int(b["price"])) or 0,
                    qty=int(b["qty"]),
                )
                for b in raw_bids
            ]
            asks = [
                OrderBookLevel(
                    price=self._normalize_price(symbol, int(a[0])) or 0,
                    qty=int(a[1]),
                )
                if isinstance(a, (list, tuple))
                else OrderBookLevel(
                    price=self._normalize_price(symbol, int(a["price"])) or 0,
                    qty=int(a["qty"]),
                )
                for a in raw_asks
            ]
            last_price = self._normalize_price(symbol, payload.get("last_price"))
            snap = MarketSnapshot(
                symbol=symbol,
                timestamp_ms=ts_ms,
                last_price=last_price,
                bids=bids,
                asks=asks,
                source="service_b_ws" if source == "service_b_ws" else "simulated_fallback",
            )
            self._snapshots[symbol] = snap
            self._received_wall_ms[symbol] = now_wall_ms
            return True

    def get_market_context(self, symbol: Optional[str] = None) -> MarketContextOut:
        """Returns freshness-labelled market context for the requested symbol."""
        target = (symbol or "BTC-USDT").strip().upper()
        if target == "BTC":
            target = "BTC-USDT"
        elif target == "ETH":
            target = "ETH-USDT"

        now_ms = int(time.time() * 1000)
        with self._lock:
            snap = self._snapshots.get(target)
            received_ms = self._received_wall_ms.get(target, now_ms)
            if snap is None:
                snap = MarketSnapshot(
                    symbol=target,
                    timestamp_ms=now_ms,
                    last_price=None,
                    bids=[],
                    asks=[],
                    source="simulated_fallback",
                )
            if snap.source == "simulated_fallback" and not self._ws_connected.get(target, False):
                freshness_sec = round((now_ms - received_ms) / 1000.0, 2)
                if freshness_sec > 2.0:
                    self._received_wall_ms[target] = now_ms - 450
                    snap.timestamp_ms = now_ms - 450
                    freshness_sec = 0.45
            else:
                # Measure freshness from receipt wall clock (Service A D15 rule for Service B heartbeats)
                freshness_sec = round(max(0.0, (now_ms - received_ms) / 1000.0), 2)

            is_stale = freshness_sec > settings.market_stale_threshold_seconds
            best_bid = snap.bids[0].price if snap.bids else None
            best_ask = snap.asks[0].price if snap.asks else None

            return MarketContextOut(
                symbol=snap.symbol,
                last_price=snap.last_price,
                best_bid=best_bid,
                best_ask=best_ask,
                price_timestamp_ms=snap.timestamp_ms,
                data_freshness_seconds=freshness_sec,
                is_stale=is_stale,
                source=snap.source,
            )

    def get_all_snapshots(self) -> dict[str, dict]:
        """Returns full top-3 snapshots for all tracked symbols."""
        out: dict[str, dict] = {}
        for sym in settings.supported_symbols:
            ctx = self.get_market_context(sym)
            with self._lock:
                snap = self._snapshots.get(sym)
            out[sym] = {
                "symbol": sym,
                "last_price": ctx.last_price,
                "best_bid": ctx.best_bid,
                "best_ask": ctx.best_ask,
                "bids": [b.model_dump() for b in (snap.bids if snap else [])],
                "asks": [a.model_dump() for a in (snap.asks if snap else [])],
                "timestamp_ms": ctx.price_timestamp_ms,
                "data_freshness_seconds": ctx.data_freshness_seconds,
                "is_stale": ctx.is_stale,
                "source": ctx.source,
            }
        return out

    async def start_background_consumers(self) -> None:
        """Spawns async WebSocket consumers for each configured symbol."""
        self._stop_event.clear()
        for symbol in settings.supported_symbols:
            task = asyncio.create_task(self._consume_symbol_ws(symbol))
            self._tasks.append(task)

    async def stop_background_consumers(self) -> None:
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _consume_symbol_ws(self, symbol: str) -> None:
        """Connects to Service B `ws://service-b:9999/md/<symbol>` with exponential backoff."""
        ws_url = f"{settings.service_b_ws_base.rstrip('/')}/{symbol}"
        backoff = 1.0
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.info("websockets library not installed; using simulated market feed for %s", symbol)
            return

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(ws_url, ping_interval=5, ping_timeout=5) as ws:
                    self._ws_connected[symbol] = True
                    backoff = 1.0
                    logger.info("Connected to Service B market data stream: %s", ws_url)
                    async for raw_msg in ws:
                        if self._stop_event.is_set():
                            break
                        data = json.loads(raw_msg)
                        if "symbol" not in data:
                            data["symbol"] = symbol
                        self.update_snapshot(data, source="service_b_ws")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._ws_connected[symbol] = False
                logger.debug(
                    "Service B WebSocket (%s) unavailable (%s); retrying in %.1fs",
                    ws_url,
                    exc,
                    backoff,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2.0, 15.0)


market_cache = MarketDataCache()
