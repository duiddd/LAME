"""Shared steps for order tests."""
from sqlalchemy import text

from tests.conftest import sign_up

BOOK = {"bids": [{"price": 4990000, "qty": 100}], "asks": [{"price": 5000000, "qty": 100}], "last_price": 5000000, "volume": 0, "last_ms": 1}


def open_market(container, symbol: str = "BTC-USDT", **overrides) -> None:
    container.prices.update(symbol, {**BOOK, **overrides})


async def funded_user(client, username: str = "amy_trader", usdt: str = "100000", btc: str = "10") -> dict:
    """Signs up and deposits; the client stays signed in as this user."""
    client.cookies.clear()
    user = await sign_up(client, username)
    for asset, amount in (("USDT", usdt), ("BTC", btc)):
        r = await client.post("/api/accounts/deposits", json={"asset": asset, "amount": amount}, headers={"Idempotency-Key": f"seed-{asset}"})
        assert r.status_code == 201, r.text
    return user


async def place(client, key: str = "k1", side: str = "BUY", type: str = "LIMIT", price: str | None = "50000.00", qty: str = "1.000", symbol: str = "BTC-USDT"):
    return await client.post("/api/orders", json={"symbol": symbol, "side": side, "type": type, "price": price, "qty": qty}, headers={"Idempotency-Key": key})


async def balance(client, asset: str) -> dict:
    balances = (await client.get("/api/accounts/me")).json()["balances"]
    return next(b for b in balances if b["asset"] == asset)


async def order_row(container, order_id: str):
    async with container.engine.connect() as conn:
        return (await conn.execute(text("SELECT * FROM orders WHERE order_id = :o"), {"o": order_id})).one()


async def ledger_matches_accounts(container) -> None:
    """The audit invariant: every balance is explained by its ledger entries, and none is negative."""
    async with container.engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT a.user_id, a.asset, a.available, a.held, coalesce(l.av, 0) AS av, coalesce(l.he, 0) AS he
            FROM accounts a LEFT JOIN (SELECT user_id, asset, sum(delta_available) av, sum(delta_held) he
                                       FROM ledger_entries GROUP BY 1, 2) l USING (user_id, asset)"""))).all()
    for r in rows:
        assert (r.available, r.held) == (r.av, r.he), r
        assert r.available >= 0 and r.held >= 0, r
