"""Candles and the day open, aggregated from settled trades when asked (D22, D23)."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..config import Catalog, from_units
from ..errors import AppError
from ..ports import Clock
from .schemas import CandleOut, CandlesOut, SymbolSummaryOut, SummaryOut, Watermark

INTERVAL_MS = {"5s": 5000, "15s": 15000, "1m": 60000, "5m": 300000}
CACHE_MS = 1000
CACHE_MAX = 256


class MarketService:
    def __init__(self, engine: AsyncEngine, catalog: Catalog, clock: Clock):
        self._engine = engine
        self._catalog = catalog
        self._clock = clock
        self._cache: dict[tuple, tuple[int, CandlesOut]] = {}

    async def candles(self, symbol: str, interval: str, limit: int, after_day: str | None, after_seq: int | None) -> CandlesOut:
        """Viewers of one chart share a short cache, so polling public data stays cheap."""
        sym = self._catalog.symbols.get(symbol)
        if sym is None:
            raise AppError(400, "INVALID_SYMBOL", f"{symbol!r} is not a trading pair")
        key = (symbol, interval, limit, after_day, after_seq)
        now = self._clock.now_ms()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < CACHE_MS:
            return cached[1]
        if len(self._cache) >= CACHE_MAX:
            self._cache.clear()
        reply = await self._query(sym, interval, limit, after_day, after_seq)
        self._cache[key] = (now, reply)
        return reply

    async def _query(self, sym, interval: str, limit: int, after_day: str | None, after_seq: int | None) -> CandlesOut:
        size = INTERVAL_MS[interval]
        incremental = after_day is not None and after_seq is not None
        async with self._engine.connect() as conn:
            mark = (await conn.execute(text(
                "SELECT b_day, b_seq FROM trades WHERE symbol = :s ORDER BY b_day DESC, b_seq DESC LIMIT 1"), {"s": sym.symbol})).first()
            since = 0
            if incremental:
                # A resend after an outage lands in old buckets: restart from the earliest one touched.
                oldest = (await conn.execute(text(
                    "SELECT min(ts) FROM trades WHERE symbol = :s AND (b_day, b_seq) > (:d, :q)"),
                    {"s": sym.symbol, "d": after_day, "q": after_seq})).scalar()
                if oldest is None:
                    return self._reply(sym, interval, [], mark, False)
                since = oldest // size * size
            # Within a bucket, order by B's sequence: trades of one command share one ts.
            rows = (await conn.execute(text("""
                SELECT ts / :size * :size AS bucket,
                       (array_agg(price ORDER BY b_day, b_seq))[1] AS o,
                       max(price) AS h, min(price) AS l,
                       (array_agg(price ORDER BY b_day DESC, b_seq DESC))[1] AS c,
                       sum(qty) AS v
                FROM trades WHERE symbol = :s AND ts >= :since
                GROUP BY 1 ORDER BY 1 DESC LIMIT :limit"""),
                {"size": size, "s": sym.symbol, "since": since, "limit": limit})).all()
        pd, qd = sym.price_decimals, sym.qty_decimals
        candles = [CandleOut(t=r.bucket, o=from_units(r.o, pd), h=from_units(r.h, pd), l=from_units(r.l, pd),
                             c=from_units(r.c, pd), v=from_units(int(r.v), qd)) for r in reversed(rows)]
        return self._reply(sym, interval, candles, mark, not incremental)

    @staticmethod
    def _reply(sym, interval: str, candles: list[CandleOut], mark, replace_all: bool) -> CandlesOut:
        watermark = Watermark(b_day=mark.b_day, b_seq=mark.b_seq) if mark else None
        return CandlesOut(symbol=sym.symbol, interval=interval, candles=candles, watermark=watermark, replace_all=replace_all)

    async def summary(self) -> SummaryOut:
        out = []
        async with self._engine.connect() as conn:
            for sym in self._catalog.symbols.values():
                row = (await conn.execute(text("""
                    SELECT b_day, price FROM trades
                    WHERE symbol = :s AND b_day = (SELECT max(b_day) FROM trades WHERE symbol = :s)
                    ORDER BY b_seq LIMIT 1"""), {"s": sym.symbol})).first()
                out.append(SymbolSummaryOut(
                    symbol=sym.symbol, base=sym.base, quote=sym.quote,
                    tick=from_units(sym.tick, sym.price_decimals), lot=from_units(sym.lot, sym.qty_decimals),
                    price_decimals=sym.price_decimals, qty_decimals=sym.qty_decimals,
                    day=row.b_day if row else None,
                    day_open=from_units(row.price, sym.price_decimals) if row else None,
                ))
        return SummaryOut(symbols=out)
