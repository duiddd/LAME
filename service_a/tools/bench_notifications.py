"""How many of D's notifications A can apply per second: the sequential path that limits settlement.

  TEST_DATABASE_URL=... python -m tools.bench_notifications [count]

Runs A in-process against a real PostgreSQL (it rebuilds the schema of TEST_DATABASE_URL, so never
point it at a database with data you need) and a fake D. Each notification goes through the HTTP
endpoint, takes the cursor lock and commits on its own, as in production.
"""
import asyncio
import os
import statistics
import sys
import time

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.container import build_container
from app.main import create_app
from tests.fakes import FakeD

URL = os.environ["TEST_DATABASE_URL"]
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 300
BOOK = {"bids": [{"price": 4990000, "qty": 10**9}], "asks": [{"price": 5000000, "qty": 10**9}], "last_price": 5000000, "volume": 0, "last_ms": 1}


async def reset():
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", URL)
    await asyncio.to_thread(command.upgrade, cfg, "head")


async def main():
    await reset()
    settings = Settings(database_url=URL, seed_balances=False, background_tasks=False, rate_per_s=1e9, rate_burst=1e9, _env_file=None)
    c = build_container(settings, d=FakeD())
    app = create_app(c)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bench") as client:
            c.prices.update("BTC-USDT", BOOK)
            await client.post("/api/auth/register", json={"username": "bench_buyer", "password": "bench-pass-1"})
            await client.post("/api/accounts/deposits", json={"asset": "USDT", "amount": "1000000"}, headers={"Idempotency-Key": "u"})
            ids = []
            for i in range(COUNT):
                c.prices.update("BTC-USDT", BOOK)          # keeps the price fresh while placing
                r = await client.post("/api/orders", json={"symbol": "BTC-USDT", "side": "BUY", "type": "LIMIT", "price": "50000.00", "qty": "0.001"}, headers={"Idempotency-Key": f"b{i}"})
                assert r.status_code == 202, r.text
                ids.append(r.json()["order_id"])
            seq, per = 0, {"ACCEPTED": [], "ORDER_UPDATE CANCELLED": []}
            for kind in per:
                start = time.perf_counter()
                for i, oid in enumerate(ids):
                    seq += 1
                    body = {"notify_seq": seq, "order_id": oid, "symbol": "BTC-USDT", "ts": 0}
                    if kind == "ACCEPTED":
                        body.update(type="ACCEPTED", b_day="2026-11-18", b_seq=seq, order_seq=i + 1)
                    else:
                        body.update(type="ORDER_UPDATE", status="CANCELLED", filled_qty=0, remaining_qty=0, b_day="2026-11-18", b_seq=seq)
                    t0 = time.perf_counter()
                    r = await client.post("/internal/order-events", json=body)
                    per[kind].append((time.perf_counter() - t0) * 1000)
                    assert r.status_code == 200, r.text
                total = time.perf_counter() - start
                ms = per[kind]
                print(f"{kind:24} n={len(ms)}  p50={statistics.median(ms):.2f} ms  p95={sorted(ms)[int(len(ms) * .95)]:.2f} ms  "
                      f"=> {len(ms) / total:.0f} notifications/s")


asyncio.run(main())
