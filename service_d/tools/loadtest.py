"""Load test for D's gate (acceptance criterion 5): N orders per second for T seconds.

Two modes:
  --local           start a fake B, a fake A and D in this process (no Docker needed)
  --url URL --d-db  hit a running D, then read decision latency from its database

Reports the latency of D's 202 reply, and the decision latency (received -> decision
written), which is what D's share of the latency budget is about.

  python -m tools.loadtest --local --rate 200 --seconds 10
  python -m tools.loadtest --url http://localhost:8400 --d-db data/d.db --rate 200 --seconds 10
"""
import argparse
import asyncio
import json
import random
import sqlite3
import tempfile
import time
from collections import Counter

import httpx

from d.evaluate import percentile

USERS = [1024, 1025, 3001, 3002, 3003] + list(range(5000, 5050))
SYMBOLS = {"BTC-USDT": 100, "ETH-USDT": 1000}   # symbol -> lot, as in config/symbols.json


def make_order(n: int, rnd: random.Random) -> dict:
    market = rnd.random() < 0.05
    symbol = rnd.choice(list(SYMBOLS))
    return {"order_id": f"LT{int(time.time())}{n:07d}", "user_id": rnd.choice(USERS), "symbol": symbol,
            "side": rnd.choice(["BUY", "SELL"]), "type": "MARKET" if market else "LIMIT",
            "price": None if market else rnd.randint(95, 105), "qty": rnd.randint(1, 5) * SYMBOLS[symbol]}


async def drive(client: httpx.AsyncClient, rate: int, seconds: float, seed: int):
    rnd = random.Random(seed)
    latencies, statuses, tasks = [], Counter(), []

    async def one(o):
        t0 = time.perf_counter()
        r = await client.post("/v1/orders", json=o)
        latencies.append((time.perf_counter() - t0) * 1000)
        statuses[r.status_code] += 1

    start = time.perf_counter()
    for n in range(int(rate * seconds)):
        due = start + n / rate
        if (wait := due - time.perf_counter()) > 0:
            await asyncio.sleep(wait)
        tasks.append(asyncio.create_task(one(make_order(n, rnd))))
    await asyncio.gather(*tasks)
    return latencies, statuses, time.perf_counter() - start


def decision_stats(d_db: str, since_ms: int) -> dict:
    db = sqlite3.connect(d_db)
    rows = db.execute("SELECT d.decided_at - o.received_at, d.reason FROM decisions d JOIN orders o "
                      "ON o.order_id = d.order_id WHERE o.received_at >= ?", (since_ms,)).fetchall()
    lat = [r[0] for r in rows]
    return {"decisions": len(rows), "p50_ms": percentile(lat, 50), "p99_ms": percentile(lat, 99),
            "max_ms": max(lat) if lat else None, "by_reason": dict(Counter(r[1] or "APPROVED" for r in rows))}


async def run_local(rate, seconds, seed):
    from d.api import create_app
    from d.config import Settings, load_static
    from d.engine import Engine
    from tests.fakes import FakeA, FakeB, wait_until

    b, a = FakeB(), FakeA()
    url = await b.start()
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(b_url=url, data_dir=tmp, a_notify_url="http://a/internal/order-events")
        e = Engine(s, load_static(s), a_transport=a.transport())
        await e.start()
        await wait_until(lambda: e.ready)
        since = int(time.time() * 1000)
        app = create_app(e, manage_lifecycle=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://d") as c:
            lat, statuses, took = await drive(c, rate, seconds, seed)
        await wait_until(lambda: e.queue.qsize() == 0, timeout=30)
        await asyncio.sleep(0.2)
        result = report(lat, statuses, took, decision_stats(f"{tmp}/d.db", since))
        result["notifications_acknowledged_by_fake_a"] = len(a.applied)
        await e.stop()
    await b.stop()
    return result


def report(lat, statuses, took, decisions):
    return {"sent": len(lat), "seconds": round(took, 2), "achieved_rate": round(len(lat) / took, 1),
            "http_status": dict(statuses), "reply_p50_ms": round(percentile(lat, 50), 2),
            "reply_p99_ms": round(percentile(lat, 99), 2), "decision": decisions}


async def run_remote(url, d_db, rate, seconds, seed):
    since = int(time.time() * 1000)
    async with httpx.AsyncClient(base_url=url, timeout=5) as c:
        lat, statuses, took = await drive(c, rate, seconds, seed)
    await asyncio.sleep(1)
    return report(lat, statuses, took, decision_stats(d_db, since))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--url")
    ap.add_argument("--d-db")
    ap.add_argument("--rate", type=int, default=100)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    if a.local:
        result = asyncio.run(run_local(a.rate, a.seconds, a.seed))
    else:
        if not (a.url and a.d_db):
            ap.error("--url and --d-db are needed without --local")
        result = asyncio.run(run_remote(a.url, a.d_db, a.rate, a.seconds, a.seed))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
