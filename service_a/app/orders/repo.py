from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

TERMINAL = ("FILLED", "CANCELLED", "REJECTED", "EXPIRED")
OPEN_STATUSES = ("PENDING", "SENT", "UNKNOWN", "PROCESSING", "MODIFYING", "PARTIALLY_FILLED")


async def insert_order(conn: AsyncConnection, values: dict) -> bool:
    row = (await conn.execute(text("""
        INSERT INTO orders (order_id, user_id, client_order_id, symbol, side, type, wire_type, price, hold_price,
                            qty, held_amount, status, created_at, updated_at)
        VALUES (:order_id, :user_id, :client_order_id, :symbol, :side, :type, :wire_type, :price, :hold_price,
                :qty, :held_amount, 'PENDING', :now, :now)
        ON CONFLICT (user_id, client_order_id) DO NOTHING RETURNING order_id"""), values)).first()
    return row is not None


async def find_by_key(conn: AsyncConnection, user_id: int, key: str):
    return (await conn.execute(text("SELECT * FROM orders WHERE user_id = :u AND client_order_id = :k"), {"u": user_id, "k": key})).first()


async def get(conn: AsyncConnection, order_id: str):
    return (await conn.execute(text("SELECT * FROM orders WHERE order_id = :o"), {"o": order_id})).first()


async def lock(conn: AsyncConnection, order_id: str):
    return (await conn.execute(text("SELECT * FROM orders WHERE order_id = :o FOR UPDATE"), {"o": order_id})).first()


async def update(conn: AsyncConnection, order_id: str, now: int, **fields) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    await conn.execute(text(f"UPDATE orders SET {sets}, updated_at = :_now WHERE order_id = :_o"), {**fields, "_now": now, "_o": order_id})


async def for_user(conn: AsyncConnection, user_id: int, status: str | None, limit: int):
    where = ""
    if status == "open":
        where = "AND status = ANY(:statuses)"
    elif status == "history":
        where = "AND status = ANY(:statuses)"
    params: dict = {"u": user_id, "l": limit}
    if status:
        params["statuses"] = list(OPEN_STATUSES if status == "open" else TERMINAL)
    return (await conn.execute(text(f"SELECT * FROM orders WHERE user_id = :u {where} ORDER BY created_at DESC, order_id DESC LIMIT :l"), params)).all()


async def lock_cursor(conn: AsyncConnection) -> int:
    return (await conn.execute(text("SELECT last_notify_seq FROM notify_cursor WHERE id = 1 FOR UPDATE"))).scalar_one()


async def read_cursor(conn: AsyncConnection) -> int:
    return (await conn.execute(text("SELECT last_notify_seq FROM notify_cursor WHERE id = 1"))).scalar_one()


async def advance_cursor(conn: AsyncConnection, seq: int) -> None:
    await conn.execute(text("UPDATE notify_cursor SET last_notify_seq = :s WHERE id = 1"), {"s": seq})


async def insert_trade(conn: AsyncConnection, values: dict) -> bool:
    row = (await conn.execute(text("""
        INSERT INTO trades (b_day, b_seq, symbol, price, qty, taker_order_id, maker_order_id, ts)
        VALUES (:b_day, :b_seq, :symbol, :price, :qty, :taker_order_id, :maker_order_id, :ts)
        ON CONFLICT DO NOTHING RETURNING b_seq"""), values)).first()
    return row is not None


FILL_COLUMNS = """
    SELECT t.b_day, t.b_seq, o.order_id, t.symbol, o.side, t.price, t.qty, t.ts,
           CASE WHEN t.taker_order_id = o.order_id THEN 'TAKER' ELSE 'MAKER' END AS role
    FROM trades t JOIN orders o ON o.order_id IN (t.taker_order_id, t.maker_order_id)"""


async def fills_for_user(conn: AsyncConnection, user_id: int, limit: int):
    return (await conn.execute(text(f"{FILL_COLUMNS} WHERE o.user_id = :u ORDER BY t.ts DESC, t.b_seq DESC LIMIT :l"), {"u": user_id, "l": limit})).all()


async def fills_for_order(conn: AsyncConnection, order_id: str):
    return (await conn.execute(text(f"{FILL_COLUMNS} WHERE o.order_id = :o ORDER BY t.b_day, t.b_seq"), {"o": order_id})).all()
