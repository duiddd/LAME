"""Placing, cancelling and modifying orders (architecture document, sections 5, 7 and 10)."""
import logging

from sqlalchemy.ext.asyncio import AsyncEngine

from ..accounts.service import AccountsService, Ref
from ..config import Catalog, Settings, Symbol, from_units, quote_amount
from ..errors import AppError
from ..market.cache import PriceCache
from ..metrics import Metrics
from ..ports import Clock, IdGenerator
from ..push.hub import Hub
from . import repo
from .dclient.breaker import CircuitBreaker
from .dclient.port import DClient, Delivery
from .precheck import rules
from .precheck.ratelimit import TokenBuckets
from .schemas import ModifyIn, OrderIn
from .views import OrderViews

log = logging.getLogger("a.orders")

SYSTEM_BUSY = AppError(503, "SYSTEM_BUSY", "The system is busy and the order was not sent. Try again shortly.")


def hold_asset(sym: Symbol, side: str) -> str:
    return sym.quote if side == "BUY" else sym.base


def hold_amount(sym: Symbol, side: str, hold_price: int, qty: int) -> int:
    return quote_amount(sym, hold_price, qty) if side == "BUY" else qty


class OrdersService:
    def __init__(self, engine: AsyncEngine, settings: Settings, catalog: Catalog, clock: Clock, ids: IdGenerator,
                 accounts: AccountsService, views: OrderViews, hub: Hub, prices: PriceCache, d: DClient,
                 breaker: CircuitBreaker, metrics: Metrics):
        self._engine = engine
        self._settings = settings
        self._catalog = catalog
        self._clock = clock
        self._ids = ids
        self._accounts = accounts
        self._views = views
        self._hub = hub
        self._prices = prices
        self._d = d
        self._breaker = breaker
        self._metrics = metrics
        self._order_limit = TokenBuckets(settings.rate_per_s, settings.rate_burst, clock)
        self._cancel_limit = TokenBuckets(settings.cancel_rate_per_s, settings.cancel_burst, clock)
        self.shedding_active = lambda: False
        self.order_sent = lambda order_id: None

    # ------------------------------------------------------------ placing
    async def place(self, user_id: int, body: OrderIn, key: str):
        sym = self._symbol(body.symbol)
        async with self._engine.connect() as conn:
            existing = await repo.find_by_key(conn, user_id, key)
        if existing is not None:
            self._metrics.duplicates.labels("request").inc()
            return existing
        if not self._order_limit.take(user_id):
            raise self._refused(AppError(429, "RATE_LIMITED", "You are placing orders too quickly"))
        try:
            values = self._prechecked(user_id, sym, body, key)
        except AppError as e:
            raise self._refused(e) from e
        if self.shedding_active() or not self._breaker.allow():
            self._metrics.orders.labels("SYSTEM_BUSY").inc()
            raise SYSTEM_BUSY
        row = await self._hold_and_insert(user_id, sym, values, key)
        if row.order_id != values["order_id"]:
            return row      # a concurrent request with the same key won
        return await self._first_send(row)

    def _symbol(self, name: str) -> Symbol:
        sym = self._catalog.symbols.get(name)
        if sym is None:
            raise AppError(400, "INVALID_SYMBOL", f"{name!r} is not a trading pair")
        return sym

    def _refused(self, error: AppError) -> AppError:
        self._metrics.orders.labels(error.reason).inc()
        return error

    def _prechecked(self, user_id: int, sym: Symbol, body: OrderIn, key: str) -> dict:
        qty = rules.parse_qty(sym, body.qty)
        snapshot = self._prices.fresh(sym.symbol)
        if body.type == "LIMIT":
            if body.price is None:
                raise rules.refuse("INVALID_TICK", "A limit order needs a price")
            priced = rules.check_limit(sym, rules.parse_price(sym, body.price), snapshot, self._settings.price_band)
        else:
            if body.price is not None:
                raise rules.refuse("INVALID_TICK", "A market order has no price")
            priced = rules.check_market(sym, body.side, snapshot, self._settings.market_protection)
        rules.check_notional(sym, priced.hold_price, qty)
        return {
            "order_id": self._ids.order_id(),
            "user_id": user_id,
            "client_order_id": key,
            "symbol": sym.symbol,
            "side": body.side,
            "type": body.type,
            "wire_type": priced.wire_type,
            "price": priced.wire_price,
            "hold_price": priced.hold_price,
            "qty": qty,
            "held_amount": hold_amount(sym, body.side, priced.hold_price, qty),
            "now": self._clock.now_ms(),
        }

    async def _hold_and_insert(self, user_id: int, sym: Symbol, values: dict, key: str):
        asset = hold_asset(sym, values["side"])
        async with self._engine.begin() as conn:
            if not await repo.insert_order(conn, values):
                self._metrics.duplicates.labels("request").inc()
                return await repo.find_by_key(conn, user_id, key)
            ref = Ref("order", f"{values['order_id']}:0")
            if not await self._accounts.hold(conn, user_id, asset, values["held_amount"], ref):
                self._metrics.orders.labels("INSUFFICIENT_BALANCE").inc()
                raise AppError(400, "INSUFFICIENT_BALANCE", "Not enough available funds")
            row = await repo.get(conn, values["order_id"])
            await self._views.emit_order(conn, row)
            await self._views.emit_balances(conn, user_id, [asset])
        self._hub.wake([user_id])
        return row

    @staticmethod
    def wire_order(row) -> dict:
        """What D sees. A resend must be identical, because D answers 409 to different content."""
        return {
            "order_id": row.order_id,
            "user_id": row.user_id,
            "symbol": row.symbol,
            "side": row.side,
            "type": row.wire_type,
            "price": row.price,
            "qty": row.qty,
        }

    async def _first_send(self, row):
        reply = await self._d.place(self.wire_order(row))
        if reply.delivery in (Delivery.ACK,):
            self.order_sent(row.order_id)
            self._metrics.orders.labels("accepted").inc()
            return await self.mark_sent(row.order_id)
        if reply.delivery is Delivery.NOT_TAKEN:
            await self._reject_unsent(row.order_id, "D_UNAVAILABLE")
            self._metrics.orders.labels("SYSTEM_BUSY").inc()
            raise SYSTEM_BUSY
        if reply.delivery is Delivery.INVALID:
            await self._reject_unsent(row.order_id, "INVALID_REQUEST")
            self._metrics.orders.labels("INVALID_REQUEST").inc()
            raise AppError(400, "INVALID_REQUEST", "The order was refused as invalid")
        # Sent with no usable reply, or D reports a conflict: D may hold the order, so the hold stays.
        self.order_sent(row.order_id)
        return await self.mark_unknown(row.order_id)

    async def mark_sent(self, order_id: str):
        """Only from PENDING or UNKNOWN: a notification may already have overtaken this reply."""
        async with self._engine.begin() as conn:
            row = await repo.lock(conn, order_id)
            if row.status in ("PENDING", "UNKNOWN"):
                now = self._clock.now_ms()
                await repo.update(conn, order_id, now, status="SENT", next_query_at=now + 10000, queries=0)
                row = await repo.get(conn, order_id)
                await self._views.emit_order(conn, row)
        self._hub.wake([row.user_id])
        return row

    async def mark_unknown(self, order_id: str):
        async with self._engine.begin() as conn:
            row = await repo.lock(conn, order_id)
            if row.status == "PENDING":
                now = self._clock.now_ms()
                await repo.update(conn, order_id, now, status="UNKNOWN", attempts=1, next_attempt_at=now + int(self._settings.resend_base_s * 1000))
                row = await repo.get(conn, order_id)
                await self._views.emit_order(conn, row)
        self._hub.wake([row.user_id])
        return row

    async def _reject_unsent(self, order_id: str, reason: str) -> None:
        """D certainly did not take this order, so nothing else can ever happen to it."""
        async with self._engine.begin() as conn:
            row = await repo.lock(conn, order_id)
            if row.status != "PENDING":
                return
            sym = self._catalog.symbols[row.symbol]
            asset = hold_asset(sym, row.side)
            await self._accounts.release(conn, row.user_id, asset, row.held_amount, Ref("order", f"{order_id}:unsent"))
            now = self._clock.now_ms()
            await repo.update(conn, order_id, now, status="REJECTED", reason=reason, held_amount=0, closed_at=now)
            await self._views.emit_order(conn, await repo.get(conn, order_id))
            await self._views.emit_balances(conn, row.user_id, [asset])
        self._hub.wake([row.user_id])

    # ------------------------------------------------------------ cancelling
    async def cancel(self, user_id: int, order_id: str):
        if not self._cancel_limit.take(user_id):
            raise AppError(429, "RATE_LIMITED", "You are cancelling orders too quickly")
        async with self._engine.begin() as conn:
            row = await repo.lock(conn, order_id)
            if row is None or row.user_id != user_id:
                raise AppError(404, "ORDER_NOT_FOUND", "No such order")
            if row.status in repo.TERMINAL:
                raise AppError(409, "ORDER_ALREADY_FINAL", "This order has already finished")
            await repo.update(conn, order_id, self._clock.now_ms(), cancel_requested=True, cancel_wanted=True, cancel_acked=False, next_attempt_at=0)
            row = await repo.get(conn, order_id)
            await self._views.emit_order(conn, row)
        self._hub.wake([user_id])
        await self.send_cancel(row)
        return row

    async def send_cancel(self, row) -> None:
        """Bypasses the breaker: withdrawing an order must always be tried. Retried by reconcile."""
        reply = await self._d.cancel(row.user_id, row.order_id)
        now = self._clock.now_ms()
        async with self._engine.begin() as conn:
            if reply.delivery is Delivery.ACK:
                await repo.update(conn, row.order_id, row.updated_at, cancel_acked=True)
            else:
                attempts = row.attempts + 1
                backoff = min(self._settings.resend_base_s * 2 ** attempts, 5.0)
                await repo.update(conn, row.order_id, row.updated_at, attempts=attempts, next_attempt_at=now + int(backoff * 1000))

    # ------------------------------------------------------------ modifying
    async def modify(self, user_id: int, order_id: str, body: ModifyIn):
        async with self._engine.connect() as conn:
            row = await repo.get(conn, order_id)
        if row is None or row.user_id != user_id:
            raise AppError(404, "ORDER_NOT_FOUND", "No such order")
        if row.type != "LIMIT" or row.status not in ("PROCESSING", "PARTIALLY_FILLED") or row.cancel_requested:
            raise AppError(409, "ORDER_NOT_MODIFIABLE", "This order cannot be modified now")
        sym = self._catalog.symbols[row.symbol]
        qty = rules.parse_qty(sym, body.qty)
        # The price does not change, so the price band does not apply again: the order already
        # rests at a price that passed it. The value limit still does, since qty can grow.
        rules.check_notional(sym, row.price, qty)
        if self._settings.modify_mode == "cancel_replace":
            return await self._cancel_replace(user_id, row, sym, qty)
        return await self._modify_at_d(row, sym, qty)

    async def _cancel_replace(self, user_id: int, row, sym: Symbol, qty: int):
        """The alternative that needs no modify from D: the replacement is a new order (D24)."""
        await self.cancel(user_id, row.order_id)
        body = OrderIn(symbol=sym.symbol, side=row.side, type="LIMIT", price=from_units(row.price, sym.price_decimals), qty=from_units(qty, sym.qty_decimals))
        return await self.place(user_id, body, f"{row.client_order_id}:r{self._ids.order_id()}")

    async def _modify_at_d(self, row, sym: Symbol, qty: int):
        price = row.price
        asset = hold_asset(sym, row.side)
        restore_status = row.status
        async with self._engine.begin() as conn:
            current = await repo.lock(conn, row.order_id)
            needed = hold_amount(sym, row.side, price, qty) + hold_amount(sym, row.side, current.hold_price, current.filled_qty - current.settled_qty)
            extra = max(0, needed - current.held_amount)
            modify_no = current.modify_no + 1
            if extra and not await self._accounts.hold(conn, row.user_id, asset, extra, Ref("order", f"{row.order_id}:{modify_no}")):
                raise AppError(400, "INSUFFICIENT_BALANCE", "Not enough available funds")
            await repo.update(conn, row.order_id, self._clock.now_ms(), status="MODIFYING", modify_no=modify_no, held_amount=current.held_amount + extra)
            await self._views.emit_order(conn, await repo.get(conn, row.order_id))
        # No price: D's protocol 2.3 keeps the order's price when it is left out.
        reply = await self._d.modify(row.user_id, row.order_id, None, qty)
        if reply.delivery is Delivery.ACK:
            return await self._fresh(row.order_id)
        # D did not take the modification: the old order stands and the extra hold goes back.
        async with self._engine.begin() as conn:
            current = await repo.lock(conn, row.order_id)
            if current.status == "MODIFYING":
                if extra:
                    await self._accounts.release(conn, row.user_id, asset, extra, Ref("order", f"{row.order_id}:{current.modify_no}:undo"))
                await repo.update(conn, row.order_id, self._clock.now_ms(), status=restore_status, held_amount=current.held_amount - extra)
                await self._views.emit_order(conn, await repo.get(conn, row.order_id))
        self._hub.wake([row.user_id])
        if reply.delivery is Delivery.NOT_IMPLEMENTED:
            raise AppError(501, "MODIFY_UNAVAILABLE", "Modifying orders is not available yet")
        raise SYSTEM_BUSY

    async def _fresh(self, order_id: str):
        async with self._engine.connect() as conn:
            return await repo.get(conn, order_id)

    # ------------------------------------------------------------ reading
    async def get_owned(self, user_id: int, order_id: str):
        row = await self._fresh(order_id)
        if row is None or row.user_id != user_id:
            raise AppError(404, "ORDER_NOT_FOUND", "No such order")
        async with self._engine.connect() as conn:
            fills = await repo.fills_for_order(conn, order_id)
        return row, fills

    async def list_for_user(self, user_id: int, status: str | None, limit: int):
        async with self._engine.connect() as conn:
            return await repo.for_user(conn, user_id, status, limit)

    async def fills_for_user(self, user_id: int, limit: int):
        async with self._engine.connect() as conn:
            return await repo.fills_for_user(conn, user_id, limit)

