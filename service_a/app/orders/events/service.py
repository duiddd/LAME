"""Applies D's notifications, one transaction each, in notify_seq order (section 5.6, D28)."""
import logging
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ...accounts.service import AccountsService, BalanceViolation, Ref
from ...config import Catalog, quote_amount
from ...metrics import Metrics
from ...ports import Clock
from ...push.hub import Hub
from .. import repo
from ..service import hold_amount, hold_asset
from ..views import OrderViews
from .transitions import TERMINAL, Verdict, judge

log = logging.getLogger("a.events")


class Poison(Exception):
    """A notification A cannot apply. The line stops, because a wrong ledger is worse than a pause."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


@dataclass
class Outcome:
    status: int
    body: dict


@dataclass
class Applied:
    users: set[int]
    finished_orders: set[str]
    cancel_orders: list[str]


class EventsService:
    def __init__(self, engine: AsyncEngine, catalog: Catalog, clock: Clock, accounts: AccountsService, views: OrderViews,
                 hub: Hub, metrics: Metrics):
        self._engine = engine
        self._catalog = catalog
        self._clock = clock
        self._accounts = accounts
        self._views = views
        self._hub = hub
        self._metrics = metrics
        self.last_advanced_ms = clock.now_ms()
        self.outcome_seen = lambda order_id: None
        self.cancel_needed = lambda: None

    async def handle(self, n: dict) -> Outcome:
        seq = n["notify_seq"]
        applied = Applied(set(), set(), [])
        try:
            async with self._engine.begin() as conn:
                last = await repo.lock_cursor(conn)
                if seq <= last:
                    self._metrics.duplicates.labels("notification").inc()
                    return Outcome(200, {"status": "duplicate"})
                if seq > last + 1:
                    self._metrics.gaps.inc()
                    return Outcome(409, {"expected_seq": last + 1})
                await self._apply(conn, n, applied)
                await repo.advance_cursor(conn, seq)
        except Poison as e:
            log.error("notify_seq %s (%s) stops the line: %s", seq, n.get("type"), e)
            self._metrics.notify_failures.labels(e.reason).inc()
            return Outcome(422, {"reason": e.reason, "message": str(e)})
        except BalanceViolation as e:
            log.error("notify_seq %s (%s) stops the line: %s", seq, n.get("type"), e)
            self._metrics.notify_failures.labels("BALANCE_VIOLATION").inc()
            return Outcome(422, {"reason": "BALANCE_VIOLATION", "message": str(e)})
        except IntegrityError as e:
            log.error("notify_seq %s (%s) stops the line: %s", seq, n.get("type"), e)
            self._metrics.notify_failures.labels("CONSTRAINT").inc()
            return Outcome(422, {"reason": "CONSTRAINT", "message": "A database constraint refused this notification"})
        except (OperationalError, InterfaceError, OSError) as e:
            log.warning("notify_seq %s (%s) not applied, D will resend: %s", seq, n.get("type"), e)
            return Outcome(503, {"reason": "TEMPORARY", "message": "Try again"})
        self.last_advanced_ms = self._clock.now_ms()
        if "ts" in n and isinstance(n["ts"], int):
            self._metrics.lag.observe(max(0, self.last_advanced_ms - n["ts"]) / 1000)
        self._hub.wake(applied.users)
        for order_id in applied.finished_orders:
            self.outcome_seen(order_id)
        if applied.cancel_orders:
            self.cancel_needed()
        return Outcome(200, {"status": "applied"})

    async def _apply(self, conn: AsyncConnection, n: dict, applied: Applied) -> None:
        kind = n["type"]
        if kind == "TRADE":
            await self._trade(conn, n, applied)
            return
        handler = {
            "RISK_REJECTED": self._risk_rejected,
            "ACCEPTED": self._accepted,
            "ORDER_UPDATE": self._order_update,
            "CANCEL_RESULT": self._cancel_result,
            "MODIFY_RESULT": self._modify_result,
        }.get(kind)
        if handler is None:
            raise Poison("UNKNOWN_TYPE", f"unknown notification type {kind!r}")
        row = await repo.lock(conn, n.get("order_id"))
        if row is None:
            raise Poison("UNKNOWN_ORDER", f"{kind} for unknown order {n.get('order_id')!r}")
        detail = f"{kind} {n.get('status') or n.get('result') or ''}".strip()
        verdict = judge(row.status, detail.replace(" ", ":"))
        if verdict is Verdict.IGNORE:
            return
        if verdict is Verdict.ILLEGAL:
            raise Poison("ILLEGAL_TRANSITION", f"{detail} on an order in {row.status}: {row.order_id}")
        await handler(conn, row, n, applied)

    # ------------------------------------------------------------ helpers
    async def _save(self, conn: AsyncConnection, row, applied: Applied, *assets, **fields) -> None:
        """Writes the fields, closes the order if it is finished, and records the events."""
        now = self._clock.now_ms()
        await repo.update(conn, row.order_id, now, **fields)
        updated = await repo.get(conn, row.order_id)
        if updated.status in TERMINAL and updated.closed_at is None and updated.settled_qty == updated.filled_qty:
            if updated.held_amount != 0:
                raise Poison("HOLD_AT_CLOSE", f"{updated.order_id} closes with {updated.held_amount} still held")
            await repo.update(conn, row.order_id, now, closed_at=now)
            updated = await repo.get(conn, row.order_id)
        await self._views.emit_order(conn, updated)
        if assets:
            await self._views.emit_balances(conn, updated.user_id, list(assets))
        applied.users.add(updated.user_id)

    def _sym(self, row):
        return self._catalog.symbols[row.symbol]

    async def _release_to(self, conn: AsyncConnection, row, target_held: int, ref_id: str) -> tuple[int, str]:
        """Releases down to target_held, so a second notification about one outcome moves nothing."""
        asset = hold_asset(self._sym(row), row.side)
        amount = row.held_amount - target_held
        if amount > 0:
            await self._accounts.release(conn, row.user_id, asset, amount, Ref("notify", ref_id))
            return target_held, asset
        return row.held_amount, asset

    # ------------------------------------------------------------ one handler per type
    async def _risk_rejected(self, conn, row, n, applied) -> None:
        held, asset = await self._release_to(conn, row, 0, str(n["notify_seq"]))
        await self._save(conn, row, applied, asset, status="REJECTED", reason=n.get("reason"), held_amount=held)
        applied.finished_orders.add(row.order_id)

    async def _accepted(self, conn, row, n, applied) -> None:
        fields = {"b_day": n.get("b_day"), "b_order_seq": n.get("order_seq")}
        if row.status in ("PENDING", "SENT", "UNKNOWN"):
            fields["status"] = "PROCESSING"
            applied.finished_orders.add(row.order_id)
        if row.type == "MARKET" and row.wire_type == "LIMIT":
            # A market buy is a capped limit order, and its remainder must not rest (section 7.3).
            fields.update(cancel_wanted=True, cancel_acked=False, next_attempt_at=0)
            applied.cancel_orders.append(row.order_id)
        await self._save(conn, row, applied, **fields)

    async def _order_update(self, conn, row, n, applied) -> None:
        status = n.get("status")
        filled = n.get("filled_qty", row.filled_qty)
        if filled < row.settled_qty:
            raise Poison("FILLED_BELOW_SETTLED", f"{row.order_id}: filled {filled} < settled {row.settled_qty}")
        if status in ("PARTIALLY_FILLED", "FILLED"):
            fields = {"filled_qty": filled}
            if status == "FILLED":
                fields["status"] = "FILLED"
                applied.finished_orders.add(row.order_id)
            elif row.status != "MODIFYING":
                fields["status"] = "PARTIALLY_FILLED"
            await self._save(conn, row, applied, **fields)
            return
        if status not in ("CANCELLED", "REJECTED", "EXPIRED"):
            raise Poison("UNKNOWN_STATUS", f"ORDER_UPDATE status {status!r}")
        # One rule for all three: REJECTED with SELF_TRADE_PREVENTED can follow a partial fill.
        keep = hold_amount(self._sym(row), row.side, row.hold_price, filled - row.settled_qty)
        held, asset = await self._release_to(conn, row, keep, str(n["notify_seq"]))
        await self._save(conn, row, applied, asset, status=status, reason=n.get("reason"), filled_qty=filled,
                         held_amount=held, cancel_requested=False, cancel_wanted=False)
        applied.finished_orders.add(row.order_id)

    async def _cancel_result(self, conn, row, n, applied) -> None:
        result = n.get("result")
        if result in ("TOMBSTONED", "ORDER_NOT_FOUND"):
            keep = hold_amount(self._sym(row), row.side, row.hold_price, row.filled_qty - row.settled_qty)
            held, asset = await self._release_to(conn, row, keep, str(n["notify_seq"]))
            await self._save(conn, row, applied, asset, status="CANCELLED", reason=result, held_amount=held,
                             cancel_requested=False, cancel_wanted=False)
            applied.finished_orders.add(row.order_id)
        else:
            await self._save(conn, row, applied, cancel_requested=False, cancel_wanted=False)

    async def _modify_result(self, conn, row, n, applied) -> None:
        """Adjusts the hold to the values D reports, or to the old values on a refusal."""
        unsettled = row.filled_qty - row.settled_qty
        terminal = row.status in TERMINAL
        if n.get("result") == "APPLIED" and not terminal:
            price = n.get("price") or row.price
            remaining = n["qty"]
            sym = self._sym(row)
            target = hold_amount(sym, row.side, price, remaining) + hold_amount(sym, row.side, row.hold_price, unsettled)
            held, asset = await self._release_to(conn, row, target, str(n["notify_seq"]))
            if held < target:
                raise Poison("MODIFY_EXCEEDS_HOLD", f"{row.order_id}: needs {target}, holds {held}")
            await self._save(conn, row, applied, asset, price=price, hold_price=price, qty=row.filled_qty + remaining,
                             held_amount=held, status="PARTIALLY_FILLED" if row.filled_qty else "PROCESSING")
            return
        remaining = 0 if terminal else row.qty - row.filled_qty
        target = hold_amount(self._sym(row), row.side, row.hold_price, remaining + unsettled)
        held, asset = await self._release_to(conn, row, target, str(n["notify_seq"]))
        fields = {"held_amount": held}
        if row.status == "MODIFYING":
            fields["status"] = "PARTIALLY_FILLED" if row.filled_qty else "PROCESSING"
        await self._save(conn, row, applied, asset, **fields)

    async def _trade(self, conn, n, applied) -> None:
        taker, maker = n.get("taker"), n.get("maker")
        if not taker or not maker:
            raise Poison("MALFORMED", "TRADE without taker and maker")
        day, seq = n.get("b_day"), n.get("b_seq")
        inserted = await repo.insert_trade(conn, {
            "b_day": day, "b_seq": seq, "symbol": n["symbol"], "price": n["price"], "qty": n["qty"],
            "taker_order_id": taker["order_id"], "maker_order_id": maker["order_id"], "ts": n.get("ts") or self._clock.now_ms(),
        })
        if not inserted:
            self._metrics.duplicates.labels("trade").inc()
            return
        # Always lock in one order, so that two trades can never deadlock.
        for side in sorted((taker, maker), key=lambda s: s["order_id"]):
            await self._settle_side(conn, side, n, f"{day}:{seq}", applied)

    async def _settle_side(self, conn, side: dict, n: dict, ref_id: str, applied) -> None:
        row = await repo.lock(conn, side["order_id"])
        if row is None:
            raise Poison("UNKNOWN_ORDER", f"TRADE for unknown order {side['order_id']!r}")
        qty, price = n["qty"], n["price"]
        if row.status in TERMINAL and row.settled_qty + qty > row.filled_qty:
            raise Poison("ILLEGAL_TRANSITION", f"TRADE beyond filled on {row.order_id}")
        sym = self._sym(row)
        ref = Ref("trade", ref_id)
        spend = hold_amount(sym, row.side, row.hold_price, qty)
        if row.side == "BUY":
            if price > row.hold_price:
                raise BalanceViolation(f"{row.order_id}: trade at {price} above the hold price {row.hold_price}")
            improvement = quote_amount(sym, row.hold_price - price, qty)
            await self._accounts.settle_spend(conn, row.user_id, sym.quote, spend, ref)
            if improvement:
                await self._accounts.credit(conn, row.user_id, sym.quote, improvement, "PRICE_IMPROVEMENT", ref)
            await self._accounts.credit(conn, row.user_id, sym.base, qty, "SETTLE_BUY", ref)
        else:
            await self._accounts.settle_spend(conn, row.user_id, sym.base, spend, ref)
            await self._accounts.credit(conn, row.user_id, sym.quote, quote_amount(sym, price, qty), "SETTLE_SELL", ref)
        fields = {
            "settled_qty": row.settled_qty + qty,
            "spent": row.spent + qty * price,
            "held_amount": row.held_amount - spend,
        }
        if row.status in ("PENDING", "SENT", "UNKNOWN", "PROCESSING"):
            fields["status"] = "PARTIALLY_FILLED"
        if row.status not in TERMINAL:
            fields["filled_qty"] = max(row.filled_qty, row.settled_qty + qty)
        await self._save(conn, row, applied, sym.base, sym.quote, **fields)
