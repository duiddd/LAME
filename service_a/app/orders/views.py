"""Turns order and fill rows into API shapes, and writes the events that push them."""
from sqlalchemy.ext.asyncio import AsyncConnection

from ..accounts.service import AccountsService
from ..config import Catalog, from_units
from ..push.hub import Hub
from .schemas import FillOut, OrderOut


class OrderViews:
    def __init__(self, catalog: Catalog, accounts: AccountsService, hub: Hub):
        self._catalog = catalog
        self._accounts = accounts
        self._hub = hub

    def order_out(self, row) -> OrderOut:
        sym = self._catalog.symbols[row.symbol]
        average = None
        if row.settled_qty > 0:
            # spent is price units x quantity units, so this divides back to price units.
            average = from_units(round(row.spent / row.settled_qty), sym.price_decimals)
        return OrderOut(
            order_id=row.order_id,
            client_order_id=row.client_order_id,
            symbol=row.symbol,
            side=row.side,
            type=row.type,
            price=None if row.type == "MARKET" or row.price is None else from_units(row.price, sym.price_decimals),
            qty=from_units(row.qty, sym.qty_decimals),
            filled_qty=from_units(row.filled_qty, sym.qty_decimals),
            settled_qty=from_units(row.settled_qty, sym.qty_decimals),
            avg_fill_price=average,
            status=row.status,
            reason=row.reason,
            cancel_requested=row.cancel_requested,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def fill_out(self, row) -> FillOut:
        sym = self._catalog.symbols[row.symbol]
        return FillOut(
            b_day=row.b_day,
            b_seq=row.b_seq,
            order_id=row.order_id,
            symbol=row.symbol,
            side=row.side,
            role=row.role,
            price=from_units(row.price, sym.price_decimals),
            qty=from_units(row.qty, sym.qty_decimals),
            ts=row.ts,
        )

    async def emit_order(self, conn: AsyncConnection, row) -> None:
        await self._hub.write(conn, row.user_id, "order_update", {"order": self.order_out(row).model_dump()})

    async def emit_balances(self, conn: AsyncConnection, user_id: int, assets: list[str]) -> None:
        balances = await self._accounts.balances_out(conn, user_id, assets)
        await self._hub.write(conn, user_id, "balance_update", {"balances": [b.model_dump() for b in balances]})
