"""Measures A against the real D and B started by run_local.ps1, and injects D's failure.

  python service_a/tools/measure_local.py [--workspace PATH]

1. Latency: sequential limit orders -> time for POST /api/orders, and from the POST until D's
   ACCEPTED makes the order PROCESSING. Then cancels, timed until CANCELLED.
2. D killed: new orders while D is down -> status, reason, latency, and whether any hold is left.
3. D restarted: time until D is ready, and until A accepts an order again (the breaker reopens).

It restarts D the way run_local.ps1 does and writes the results to <workspace>/logs/measure-*.json.
Run it inside B's session (00:00-23:00 Dublin time).
"""
import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

A = "http://127.0.0.1:8000"
D = "http://127.0.0.1:8400"
RUN = str(int(time.time()))[-6:]


def pct(values, p):
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 1) if ordered else None


def summary(values):
    return {"n": len(values), "p50_ms": pct(values, 0.5), "p95_ms": pct(values, 0.95), "max_ms": round(max(values), 1) if values else None}


class Trader:
    def __init__(self, name):
        self.c = httpx.AsyncClient(base_url=A, timeout=10)
        self.name, self.n = f"{name}_{RUN}", 0

    async def sign_up(self, usdt="1000000"):
        r = await self.c.post("/api/auth/register", json={"username": self.name, "password": "measure-pass-1"})
        assert r.status_code == 201, r.text
        r = await self.c.post("/api/accounts/deposits", json={"asset": "USDT", "amount": usdt}, headers={"Idempotency-Key": "u"})
        assert r.status_code == 201, r.text

    async def place(self, price, qty="0.001"):
        self.n += 1
        body = {"symbol": "BTC-USDT", "side": "BUY", "type": "LIMIT", "price": price, "qty": qty}
        start = time.perf_counter()
        r = await self.c.post("/api/orders", json=body, headers={"Idempotency-Key": f"{self.name}-{self.n}"})
        return r, (time.perf_counter() - start) * 1000

    async def wait_status(self, order_id, statuses, timeout=10.0):
        start = time.perf_counter()
        while time.perf_counter() - start < timeout:
            o = (await self.c.get(f"/api/orders/{order_id}")).json()
            if o["status"] in statuses and not o["cancel_requested"]:
                return o, (time.perf_counter() - start) * 1000
            await asyncio.sleep(0.005)
        return o, None

    async def held_usdt(self):
        balances = (await self.c.get("/api/accounts/me")).json()["balances"]
        return next(b["held"] for b in balances if b["asset"] == "USDT")


async def reference_price(t: Trader) -> int:
    summary_ = (await t.c.get("/api/market/summary")).json()
    candles = (await t.c.get("/api/market/candles", params={"symbol": "BTC-USDT", "interval": "5m"})).json()["candles"]
    if candles:
        return int(float(candles[-1]["c"]) * 100)
    return 5000000


async def latency(t: Trader, price: str, count: int):
    post_ms, accept_ms, cancel_ms, ids = [], [], [], []
    for _ in range(count):
        r, ms = await t.place(price)
        assert r.status_code == 202, r.text
        post_ms.append(ms)
        order, waited = await t.wait_status(r.json()["order_id"], {"PROCESSING"})
        if waited is not None:
            accept_ms.append(ms + waited)
        ids.append(r.json()["order_id"])
        await asyncio.sleep(0.25)          # stays under the 5 orders/s rate limit
    for oid in ids:
        start = time.perf_counter()
        await t.c.delete(f"/api/orders/{oid}")
        _, waited = await t.wait_status(oid, {"CANCELLED"})
        if waited is not None:
            cancel_ms.append((time.perf_counter() - start) * 1000)
    return {"post": summary(post_ms), "post_to_accepted": summary(accept_ms), "cancel_to_cancelled": summary(cancel_ms)}


def kill(pid):
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def start_d(workspace: Path) -> int:
    env = dict(os.environ, B_CTRL_URL="ws://127.0.0.1:11659/d", A_NOTIFY_URL=f"{A}/internal/order-events",
               DATA_DIR=str(workspace / "d-data"), PYTHONUTF8="1")
    log = open(workspace / "logs" / "d.log", "ab")
    proc = subprocess.Popen([str(workspace / "venv-ad" / "Scripts" / "python.exe"), "-m", "d.main"], cwd=workspace / "d" / "service_d",
                            env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return proc.pid


async def d_down(t: Trader, price: str, workspace: Path):
    pids_file = workspace / "pids.json"
    pids = json.loads(pids_file.read_text(encoding="utf-8-sig"))
    kill(pids["D"])
    killed_at = time.perf_counter()
    await asyncio.sleep(0.5)
    attempts = []
    for _ in range(8):
        r, ms = await t.place(price)
        attempts.append({"status": r.status_code, "reason": r.json().get("reason"), "ms": round(ms, 1)})
        await asyncio.sleep(0.3)
    held = await t.held_usdt()
    health = (await t.c.get("/healthz")).json()

    pids["D"] = start_d(workspace)
    pids_file.write_text(json.dumps(pids, indent=4), encoding="utf-8")
    restarted = time.perf_counter()
    d_ready = None
    async with httpx.AsyncClient(timeout=2) as c:
        while time.perf_counter() - restarted < 60:
            try:
                if (await c.get(f"{D}/healthz")).json().get("ready"):
                    d_ready = (time.perf_counter() - restarted) * 1000
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    accepted_again, retries = None, 0
    while time.perf_counter() - restarted < 60:
        r, _ = await t.place(price)
        retries += 1
        if r.status_code == 202:
            accepted_again = (time.perf_counter() - restarted) * 1000
            await t.c.delete(f"/api/orders/{r.json()['order_id']}")
            break
        await asyncio.sleep(0.5)
    return {
        "attempts_while_down": attempts,
        "usdt_held_while_down": held,
        "a_health_while_down": {"ready": health["ready"], "circuit": health["circuit"]},
        "d_ready_after_restart_ms": round(d_ready, 0) if d_ready else None,
        "a_accepts_again_after_restart_ms": round(accepted_again, 0) if accepted_again else None,
        "orders_tried_until_accepted": retries,
        "seconds_from_kill_to_recovery": round(time.perf_counter() - killed_at, 1),
    }


async def main():
    parser = argparse.ArgumentParser()
    here = Path(__file__).resolve()
    parser.add_argument("--workspace", default=str(here.parents[3]))
    args = parser.parse_args()
    workspace = Path(args.workspace)
    t = Trader("measure")
    await t.sign_up()
    ref = await reference_price(t)
    price = f"{ref * 95 // 100 / 100:.2f}"          # 5% under the market: inside the band, does not trade
    results = {"reference_price": ref / 100, "order_price": price}
    print("latency ...", flush=True)
    results["latency"] = await latency(t, price, 40)
    print(json.dumps(results["latency"]), flush=True)
    print("D killed ...", flush=True)
    results["d_failure"] = await d_down(t, price, workspace)
    print(json.dumps(results["d_failure"], indent=1), flush=True)
    out = workspace / "logs" / f"measure-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("written", out)


asyncio.run(main())
