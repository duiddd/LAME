"""Runs real A against real D, over HTTP, with D's own fake of B and a small market-data server.

    D_SOURCE=/path/to/service_d TEST_DATABASE_URL=... python -m tools.integration_check [--serve]

D's code is only read and run from D_SOURCE (a checkout of feature/risk_control). Its FakeB matches
orders but publishes no market data, so this tool serves a fixed book on /md/<symbol>.

Scenarios: cross two users' orders and settle both; a D rejection (self trade) releases the hold;
a cancel; a market buy with its remainder cancelled; killing D gives SYSTEM_BUSY, not a false
failure of an order D already holds. With --serve it runs none of them and keeps the stack up on
port 8000, so that the frontend can use it.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import websockets
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

D_SOURCE = Path(os.environ["D_SOURCE"]).resolve()
sys.path.insert(0, str(D_SOURCE))

from tests.fakes import FakeB  # noqa: E402  (D's own test double, from D_SOURCE)

DB = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://lame@127.0.0.1:55432/lame_test")
SERVE = "--serve" in sys.argv
A_PORT, D_PORT, MD_PORT = (8000 if SERVE else 18000), 18400, 19999
A = f"http://127.0.0.1:{A_PORT}"
ROOT = Path(__file__).resolve().parent.parent
BOOK = {"bids": [{"price": 4990000, "qty": 5000}], "asks": [{"price": 5000000, "qty": 5000}], "last_price": 5000000, "volume": 0, "last_ms": 1}
PASSED: list[str] = []


def check(name: str, condition: bool, detail="") -> None:
    if not condition:
        raise SystemExit(f"FAILED: {name} {detail}")
    PASSED.append(name)
    print(f"ok  {name}")


async def serve_md():
    async def handler(ws):
        while True:
            await ws.send(json.dumps(BOOK))
            await asyncio.sleep(0.5)
    return await websockets.serve(handler, "127.0.0.1", MD_PORT)


async def reset_database():
    engine = create_async_engine(DB)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


def start(args, cwd, env):
    return subprocess.Popen(args, cwd=cwd, env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_http(url: str) -> None:
    async with httpx.AsyncClient() as client:
        for _ in range(100):
            try:
                if (await client.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise SystemExit(f"FAILED: {url} did not become ready")


async def until(fetch, condition, what: str, timeout=8.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        value = await fetch()
        if condition(value):
            return value
        if asyncio.get_event_loop().time() > deadline:
            raise SystemExit(f"FAILED: timed out waiting for {what}; last value: {value}")
        await asyncio.sleep(0.1)


class Trader:
    def __init__(self, name: str):
        self.client = httpx.AsyncClient(base_url=A, timeout=5)
        self.name = name
        self.n = 0

    async def sign_up(self):
        r = await self.client.post("/api/auth/register", json={"username": self.name, "password": "integration-pass-1"})
        assert r.status_code == 201, r.text
        for asset, amount in (("USDT", "100000"), ("BTC", "10")):
            r = await self.client.post("/api/accounts/deposits", json={"asset": asset, "amount": amount}, headers={"Idempotency-Key": asset})
            assert r.status_code == 201, r.text

    async def place(self, **kw):
        self.n += 1
        body = {"symbol": "BTC-USDT", "side": "BUY", "type": "LIMIT", "price": "50000.00", "qty": "1.000", **kw}
        return await self.client.post("/api/orders", json=body, headers={"Idempotency-Key": f"{self.name}-{self.n}"})

    async def order(self, order_id: str) -> dict:
        return (await self.client.get(f"/api/orders/{order_id}")).json()

    async def balance(self, asset: str) -> dict:
        balances = (await self.client.get("/api/accounts/me")).json()["balances"]
        return next(b for b in balances if b["asset"] == asset)


async def scenarios(d_process):
    amy, bob = Trader("amy_it"), Trader("bob_it")
    await amy.sign_up()
    await bob.sign_up()

    sell = await bob.place(side="SELL", price="49900.00", qty="1.000")
    check("order reaches D and gets 202", sell.status_code == 202, sell.text)
    sell_id = sell.json()["order_id"]
    await until(lambda: bob.order(sell_id), lambda o: o["status"] == "PROCESSING", "D's ACCEPTED")
    check("ACCEPTED arrives through D's notifier", True)

    buy = await amy.place(price="50000.00", qty="0.300")
    buy_id = buy.json()["order_id"]
    filled = await until(lambda: amy.order(buy_id), lambda o: o["status"] == "FILLED" and o["settled_qty"] == "0.300", "the trade")
    check("a real match settles", filled["avg_fill_price"] == "49900.00", filled)
    usdt = await amy.balance("USDT")
    check("buyer paid the trade price and has nothing held", usdt["available"] == "85030.00000" and usdt["held"] == "0.00000", usdt)
    check("buyer got the base", (await amy.balance("BTC"))["available"] == "10.300")
    check("seller was paid", (await bob.balance("USDT"))["available"] == "114970.00000")
    check("seller's rest stays held", (await bob.balance("BTC"))["held"] == "0.700")
    partial = await bob.order(sell_id)
    check("maker shows partially filled", partial["status"] == "PARTIALLY_FILLED" and partial["filled_qty"] == "0.300", partial)

    risky = await bob.place(price="50000.00", qty="0.100")
    risky_id = risky.json()["order_id"]
    rejected = await until(lambda: bob.order(risky_id), lambda o: o["status"] == "REJECTED", "D's rejection")
    check("D's self-trade rule rejects and A releases", rejected["reason"] == "SELF_TRADE" and (await bob.balance("USDT"))["held"] == "0.00000", rejected)

    cancelled = await bob.client.delete(f"/api/orders/{sell_id}")
    check("cancel is accepted", cancelled.status_code == 202, cancelled.text)
    done = await until(lambda: bob.order(sell_id), lambda o: o["status"] == "CANCELLED" and not o["cancel_requested"], "the cancel")
    check("cancel releases only the unfilled part", (await bob.balance("BTC"))["held"] == "0.000", done)

    resting = await bob.place(side="SELL", price="50000.00", qty="0.200")
    await until(lambda: bob.order(resting.json()["order_id"]), lambda o: o["status"] == "PROCESSING", "the resting sell")
    market = await amy.place(type="MARKET", price=None, qty="0.500")
    check("market buy is accepted", market.status_code == 202, market.text)
    market_id = market.json()["order_id"]
    finished = await until(lambda: amy.order(market_id), lambda o: o["status"] == "CANCELLED", "the market buy's remainder cancel")
    check("market buy fills, then its remainder is cancelled", finished["filled_qty"] == "0.200" and finished["type"] == "MARKET", finished)
    usdt = await amy.balance("USDT")
    check("market buy's hold fully returns", usdt["held"] == "0.00000" and usdt["available"] == "75030.00000", usdt)

    engine = create_async_engine(DB)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT a.available, a.held, coalesce(l.av, 0) av, coalesce(l.he, 0) he FROM accounts a LEFT JOIN
            (SELECT user_id, asset, sum(delta_available) av, sum(delta_held) he FROM ledger_entries GROUP BY 1, 2) l USING (user_id, asset)"""))).all()
        totals = dict((await conn.execute(text("SELECT asset, sum(available + held) FROM accounts GROUP BY asset"))).all())
        trades = (await conn.execute(text("SELECT count(*) FROM trades"))).scalar()
    await engine.dispose()
    check("ledger explains every balance", all((r.available, r.held) == (r.av, r.he) for r in rows))
    check("no money was created or lost", totals["USDT"] == 200000 * 100000 and totals["BTC"] == 20 * 1000, totals)
    candles = (await amy.client.get("/api/market/candles", params={"symbol": "BTC-USDT", "interval": "1m"})).json()
    check("candles come from those trades", trades == 2 and sum(float(c["v"]) for c in candles["candles"]) == 0.5, candles)

    d_process.kill()
    d_process.wait()
    down = await amy.place(price="50000.00", qty="0.100")
    check("with D gone, an order is refused as busy", down.status_code == 503 and down.json()["reason"] == "SYSTEM_BUSY", down.text)
    check("and its hold is released", (await amy.balance("USDT"))["held"] == "0.00000")
    health = (await amy.client.get("/healthz")).json()
    check("A itself stays up", health["ready"] is True, health)


async def main():
    await reset_database()
    fake_b = FakeB()
    b_url = await fake_b.start()
    md = await serve_md()
    python = sys.executable
    data = tempfile.mkdtemp(prefix="lame-d-")
    # The tests' fixed grid (3 quantity decimals), which the amounts below are written in.
    env_a = {"DATABASE_URL": DB, "D_URL": f"http://127.0.0.1:{D_PORT}", "B_MD_URL": f"ws://127.0.0.1:{MD_PORT}", "SEED_BALANCES": "false",
             "SYMBOLS_FILE": str(ROOT / "tests" / "symbols_test.json")}
    subprocess.run([python, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env={**os.environ, **env_a}, check=True, capture_output=True)
    a = start([python, "-m", "uvicorn", "app.main:app", "--port", str(A_PORT), "--workers", "1"], ROOT, env_a)
    runner = "import uvicorn; from d.config import Settings, load_static; from d.engine import Engine; from d.api import create_app; s = Settings(); uvicorn.run(create_app(Engine(s, load_static(s))), port=%d)" % D_PORT
    d = start([python, "-c", runner], D_SOURCE, {"B_CTRL_URL": b_url, "A_NOTIFY_URL": f"{A}/internal/order-events", "DATA_DIR": data})
    try:
        await wait_http(f"{A}/healthz")
        await wait_http(f"http://127.0.0.1:{D_PORT}/healthz")
        await asyncio.sleep(1.0)
        if SERVE:
            print("serving: A on 8000, market data on 19999", flush=True)
            await asyncio.Event().wait()
        await scenarios(d)
    finally:
        for process in (a, d):
            process.kill()
        md.close()
        await fake_b.stop()
    print(f"\nALL {len(PASSED)} CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
