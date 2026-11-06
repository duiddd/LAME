"""A's connection to B's market data (`/md/<symbol>`), which feeds the price check."""
import asyncio
import json
import logging

import websockets

from ..metrics import Metrics
from .cache import PriceCache

log = logging.getLogger("a.market")


def apply_message(cache: PriceCache, metrics: Metrics, symbol: str, text: str) -> None:
    """A malformed message is skipped and counted; a task that crashed on it would crash forever."""
    try:
        message = json.loads(text)
        cache.update(symbol, {k: message[k] for k in ("bids", "asks", "last_price", "last_ms")})
    except (ValueError, KeyError, TypeError, IndexError) as e:
        metrics.md_bad.labels(symbol).inc()
        log.warning("skipped a malformed message for %s: %s", symbol, e)


async def follow_market(base_url: str, symbol: str, cache: PriceCache, metrics: Metrics) -> None:
    """Runs until cancelled. B answers 503 during recovery and closes on restart: retry both."""
    backoff = 0.25
    while True:
        try:
            async with websockets.connect(f"{base_url}/md/{symbol}", open_timeout=3) as ws:
                cache.forget_order(symbol)
                backoff = 0.25
                async for text in ws:
                    apply_message(cache, metrics, symbol, text)
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
            log.warning("market data for %s unavailable: %s", symbol, e)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 5)
