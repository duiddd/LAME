"""Chases orders whose outcome has not arrived (sections 9 and 10). It only asks and resends: funds
move on D's notifications alone, so there is one way for a release to happen.
"""
import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ...config import Catalog, Settings
from ...market.cache import PriceCache
from ...metrics import Metrics
from ...ports import Clock
from .. import repo
from ..dclient.breaker import CircuitBreaker
from ..dclient.port import DClient, Delivery
from ..events.service import EventsService
from ..service import OrdersService

log = logging.getLogger("a.reconcile")

QUERY_DELAYS_S = (10, 20, 40, 60)
PURGE_EVERY_MS = 60_000


class Reconciler:
    def __init__(self, engine: AsyncEngine, settings: Settings, catalog: Catalog, clock: Clock, orders: OrdersService, d: DClient,
                 events: EventsService, prices: PriceCache, breaker: CircuitBreaker, metrics: Metrics):
        self._engine = engine
        self._settings = settings
        self._catalog = catalog
        self._clock = clock
        self._orders = orders
        self._d = d
        self._events = events
        self._prices = prices
        self._breaker = breaker
        self._metrics = metrics
        self._kick = asyncio.Event()
        self._last_purge = 0

    def kick(self) -> None:
        self._kick.set()

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._kick.wait(), self._settings.reconcile_tick_s)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()
            await self.tick()

    async def tick(self) -> None:
        now = self._clock.now_ms()
        async with self._engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT * FROM orders WHERE status = ANY(:s) OR (cancel_wanted AND NOT cancel_acked AND status <> ALL(:t))"),
                {"s": ["PENDING", "UNKNOWN", "SENT"], "t": list(repo.TERMINAL)})).all()
            cursor = await repo.read_cursor(conn)
        for row in rows:
            if row.cancel_wanted and not row.cancel_acked:
                if row.next_attempt_at <= now:
                    await self._orders.send_cancel(row)
            elif row.status == "PENDING":
                await self._pending(row, now)
            elif row.status == "UNKNOWN":
                await self._unknown(row, now)
            elif row.status == "SENT":
                await self._sent(row, now, cursor)
        await self._report(now)

    async def _pending(self, row, now: int) -> None:
        """A live request finishes within its timeout; anything older outlived a restart of A."""
        if now - row.updated_at > self._settings.d_total_timeout_s * 2000:
            await self._orders.mark_unknown(row.order_id)

    async def _unknown(self, row, now: int) -> None:
        if row.next_attempt_at > now:
            return
        if now - row.created_at > self._settings.resend_window_s * 1000:
            # Too late to place it: an order must not take effect long after the user asked.
            await self._want_cancel(row, now)
            return
        reply = await self._d.place(self._orders.wire_order(row))
        if reply.delivery is Delivery.ACK:
            await self._orders.mark_sent(row.order_id)
            return
        # A 503 or 400 here does not prove anything: the earlier attempt may still be on its way.
        attempts = row.attempts + 1
        backoff = min(self._settings.resend_base_s * 2 ** attempts, 5.0)
        async with self._engine.begin() as conn:
            await repo.update(conn, row.order_id, row.updated_at, attempts=attempts, next_attempt_at=now + int(backoff * 1000))

    async def _want_cancel(self, row, now: int) -> None:
        async with self._engine.begin() as conn:
            await repo.update(conn, row.order_id, row.updated_at, cancel_wanted=True, cancel_acked=False, next_attempt_at=now)
        self.kick()

    async def _sent(self, row, now: int, cursor: int) -> None:
        if row.next_query_at is None or row.next_query_at > now:
            return
        reply = await self._d.query(row.user_id, row.order_id)
        delay = QUERY_DELAYS_S[min(row.queries + 1, len(QUERY_DELAYS_S) - 1)]
        async with self._engine.begin() as conn:
            await repo.update(conn, row.order_id, row.updated_at, queries=row.queries + 1, next_query_at=now + delay * 1000)
        if reply is None:
            return
        if not reply.found:
            await self._want_cancel(row, now)
        elif reply.last_notify_seq is not None and reply.last_notify_seq > cursor:
            log.warning("order %s: D has notifications up to %s, A has %s; asking D to resend", row.order_id, reply.last_notify_seq, cursor)
            await self._d.rewind(cursor + 1)

    async def _report(self, now: int) -> None:
        self._metrics.circuit.set(self._breaker.state)
        self._metrics.cursor_age.set((now - self._events.last_advanced_ms) / 1000)
        for symbol in self._catalog.symbols:
            age = self._prices.age_s(symbol)
            if age is not None:
                self._metrics.md_age.labels(symbol).set(age)
        async with self._engine.connect() as conn:
            counts = (await conn.execute(text("SELECT status, count(*) AS n FROM orders WHERE status = ANY(:s) GROUP BY 1"), {"s": list(repo.OPEN_STATUSES)})).all()
            unsettled = (await conn.execute(text("SELECT count(*) FROM orders WHERE status = ANY(:t) AND closed_at IS NULL"), {"t": list(repo.TERMINAL)})).scalar()
        by_status = {r.status: r.n for r in counts}
        for status in repo.OPEN_STATUSES:
            self._metrics.by_status.labels(status).set(by_status.get(status, 0))
        self._metrics.unsettled.set(unsettled)
        if unsettled:
            log.warning("%s finished orders are waiting for a trade", unsettled)
        if now - self._last_purge >= PURGE_EVERY_MS:
            self._last_purge = now
            async with self._engine.begin() as conn:
                await conn.execute(text("DELETE FROM user_events WHERE created_at < :c"), {"c": now - self._settings.event_retention_s * 1000})
