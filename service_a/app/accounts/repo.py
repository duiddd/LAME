from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def ensure_account(conn: AsyncConnection, user_id: int, asset: str) -> None:
    await conn.execute(text(
        "INSERT INTO accounts (user_id, asset) VALUES (:u, :a) ON CONFLICT DO NOTHING"), {"u": user_id, "a": asset})


async def move(conn: AsyncConnection, user_id: int, asset: str, delta_available: int, delta_held: int) -> bool:
    """One conditional update, so no read-then-write race. False means a balance would go negative."""
    row = (await conn.execute(text(
        "UPDATE accounts SET available = available + :da, held = held + :dh "
        "WHERE user_id = :u AND asset = :a AND available + :da >= 0 AND held + :dh >= 0 RETURNING user_id"),
        {"u": user_id, "a": asset, "da": delta_available, "dh": delta_held})).first()
    return row is not None


async def insert_ledger(conn: AsyncConnection, user_id: int, asset: str, delta_available: int, delta_held: int,
                        reason: str, ref_type: str, ref_id: str, now: int) -> None:
    await conn.execute(text(
        "INSERT INTO ledger_entries (user_id, asset, delta_available, delta_held, reason, ref_type, ref_id, created_at) "
        "VALUES (:u, :a, :da, :dh, :r, :rt, :ri, :n)"),
        {"u": user_id, "a": asset, "da": delta_available, "dh": delta_held, "r": reason, "rt": ref_type, "ri": ref_id, "n": now})


async def balances(conn: AsyncConnection, user_id: int, assets: list[str] | None = None):
    query = "SELECT asset, available, held FROM accounts WHERE user_id = :u"
    params: dict = {"u": user_id}
    if assets is not None:
        query += " AND asset = ANY(:assets)"
        params["assets"] = assets
    return (await conn.execute(text(query + " ORDER BY asset"), params)).all()


async def insert_deposit(conn: AsyncConnection, user_id: int, key: str, asset: str, amount: int, now: int) -> int | None:
    row = (await conn.execute(text(
        "INSERT INTO deposits (user_id, client_request_id, asset, amount, created_at) VALUES (:u, :k, :a, :m, :n) "
        "ON CONFLICT (user_id, client_request_id) DO NOTHING RETURNING deposit_id"),
        {"u": user_id, "k": key, "a": asset, "m": amount, "n": now})).first()
    return row.deposit_id if row else None


async def find_deposit(conn: AsyncConnection, user_id: int, key: str):
    return (await conn.execute(text(
        "SELECT deposit_id, asset, amount FROM deposits WHERE user_id = :u AND client_request_id = :k"),
        {"u": user_id, "k": key})).first()
