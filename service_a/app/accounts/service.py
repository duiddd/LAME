"""The only module that writes balances. Every change leaves one ledger entry (D6, D7).

Callers pass the connection, so that a change commits together with whatever caused it.
"""
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..config import Catalog, NotOnGrid, from_units, to_units
from ..errors import AppError
from ..ports import Clock
from ..push.hub import Hub
from . import repo
from .schemas import BalanceOut


@dataclass(frozen=True)
class Ref:
    """What caused a balance change. It keeps each effect unique in the ledger."""

    type: str
    id: str


class AccountsService:
    def __init__(self, engine: AsyncEngine, catalog: Catalog, clock: Clock, hub: Hub):
        self._engine = engine
        self._catalog = catalog
        self._clock = clock
        self._hub = hub

    def to_out(self, asset: str, available: int, held: int) -> BalanceOut:
        decimals = self._catalog.assets[asset].decimals
        return BalanceOut(
            asset=asset,
            available=from_units(available, decimals),
            held=from_units(held, decimals),
            total=from_units(available + held, decimals),
        )

    async def balances_out(self, conn: AsyncConnection, user_id: int, assets: list[str] | None = None) -> list[BalanceOut]:
        return [self.to_out(r.asset, r.available, r.held) for r in await repo.balances(conn, user_id, assets)]

    async def my_balances(self, user_id: int) -> list[BalanceOut]:
        async with self._engine.connect() as conn:
            return await self.balances_out(conn, user_id)

    async def _apply(self, conn: AsyncConnection, user_id: int, asset: str, delta_available: int, delta_held: int,
                     reason: str, ref: Ref) -> bool:
        await repo.ensure_account(conn, user_id, asset)
        if not await repo.move(conn, user_id, asset, delta_available, delta_held):
            return False
        await repo.insert_ledger(conn, user_id, asset, delta_available, delta_held, reason, ref.type, ref.id, self._clock.now_ms())
        return True

    async def credit(self, conn: AsyncConnection, user_id: int, asset: str, amount: int, reason: str, ref: Ref) -> None:
        await self._apply(conn, user_id, asset, amount, 0, reason, ref)

    async def hold(self, conn: AsyncConnection, user_id: int, asset: str, amount: int, ref: Ref) -> bool:
        """Moves funds from available to held; False means there is not enough available."""
        return await self._apply(conn, user_id, asset, -amount, amount, "HOLD", ref)

    async def release(self, conn: AsyncConnection, user_id: int, asset: str, amount: int, ref: Ref) -> None:
        if amount > 0 and not await self._apply(conn, user_id, asset, amount, -amount, "RELEASE", ref):
            raise BalanceViolation(f"release of {amount} {asset} for user {user_id} exceeds the held balance")

    async def settle_spend(self, conn: AsyncConnection, user_id: int, asset: str, held_amount: int, ref: Ref) -> None:
        if not await self._apply(conn, user_id, asset, 0, -held_amount, "SETTLE", ref):
            raise BalanceViolation(f"settling {held_amount} {asset} for user {user_id} exceeds the held balance")

    async def deposit(self, user_id: int, asset: str, amount_text: str, key: str) -> tuple[int, str, int, BalanceOut]:
        spec = self._catalog.assets.get(asset)
        if spec is None:
            raise AppError(422, "INVALID_ASSET", f"{asset!r} cannot be deposited")
        try:
            amount = to_units(amount_text, spec.decimals)
        except NotOnGrid as e:
            raise AppError(422, "INVALID_AMOUNT", f"The amount can have at most {spec.decimals} decimals") from e
        if amount <= 0:
            raise AppError(422, "INVALID_AMOUNT", "The amount must be positive")
        if amount > spec.max_deposit:
            raise AppError(422, "DEPOSIT_TOO_LARGE", f"One deposit can be at most {from_units(spec.max_deposit, spec.decimals)} {asset}")
        async with self._engine.begin() as conn:
            deposit_id = await repo.insert_deposit(conn, user_id, key, asset, amount, self._clock.now_ms())
            if deposit_id is None:
                existing = await repo.find_deposit(conn, user_id, key)
                deposit_id, asset, amount = existing.deposit_id, existing.asset, existing.amount
            else:
                await self.credit(conn, user_id, asset, amount, "DEPOSIT", Ref("deposit", str(deposit_id)))
            rows = await self.balances_out(conn, user_id, [asset])
            await self._hub.write(conn, user_id, "balance_update", {"balances": [b.model_dump() for b in rows]})
        self._hub.wake([user_id])
        return deposit_id, asset, amount, rows[0]


class BalanceViolation(Exception):
    """A settlement or release would break a balance constraint: A's books disagree with D's."""
