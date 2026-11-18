"""End-to-end check against the real services started by run_local.ps1: A (8000), D (8400), B (9999).

It only calls public APIs, so it changes nothing in the other services' code. Each run registers
new users, so it can run repeatedly. Run it with: run_local.ps1 -Check
"""
import asyncio
import json
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import websockets

A = "http://127.0.0.1:8000"
RUN = str(int(time.time()))[-6:]
PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("ok   " if condition else "FAIL ") + name + ("" if condition else f"  -> {str(detail)[:300]}"), flush=True)


async def until(fetch, condition, timeout=10.0):
    end = time.monotonic() + timeout
    while True:
        value = await fetch()
        if condition(value) or time.monotonic() > end:
            return value
        await asyncio.sleep(0.15)


class Trader:
    def __init__(self, name):
        self.c = httpx.AsyncClient(base_url=A, timeout=8)
        self.name, self.n = f"{name}_{RUN}", 0

    async def sign_up(self):
        r = await self.c.post("/api/auth/register", json={"username": self.name, "password": "local-e2e-pass-1"})
        assert r.status_code == 201, r.text
        for asset, amount in (("USDT", "200000"), ("BTC", "10")):
            r = await self.c.post("/api/accounts/deposits", json={"asset": asset, "amount": amount}, headers={"Idempotency-Key": asset})
            assert r.status_code == 201, r.text

    async def place(self, **kw):
        self.n += 1
        body = {"symbol": "BTC-USDT", "side": "BUY", "type": "LIMIT", "price": "50000.00", "qty": "1.000", **kw}
        return await self.c.post("/api/orders", json=body, headers={"Idempotency-Key": f"{self.name}-{self.n}"})

    async def order(self, oid):
        return (await self.c.get(f"/api/orders/{oid}")).json()

    async def bal(self, asset):
        return next(b for b in (await self.c.get("/api/accounts/me")).json()["balances"] if b["asset"] == asset)

    async def wait(self, oid, cond, timeout=10.0):
        return await until(lambda: self.order(oid), cond, timeout)


async def md_snapshot():
    async with websockets.connect("ws://127.0.0.1:9999/md/BTC-USDT", open_timeout=3) as ws:
        return json.loads(await asyncio.wait_for(ws.recv(), 3))


def market_open() -> bool:
    """B accepts orders 00:00-23:00 Dublin time (B's session.rs) and refuses them with END_OF_DAY otherwise."""
    return datetime.now(ZoneInfo("Europe/Dublin")).hour < 23


async def main():
    if not market_open():
        print("B is closed: it accepts orders 00:00-23:00 Dublin time. Run the check after 00:00 Dublin time.")
        sys.exit(2)
    snap = await md_snapshot()
    check("B /md/BTC-USDT sends the documented fields", set(snap) == {"bids", "asks", "last_price", "volume", "last_ms"}, snap)
    print("     book at start:", json.dumps(snap)[:200])

    amy, bob = Trader("amy"), Trader("bob")
    await amy.sign_up()
    await bob.sign_up()
    usdt0 = float((await amy.bal("USDT"))["available"])

    # Prices near the last trade if the day already has one, so the +-10% band accepts them.
    ref = snap["last_price"] or (snap["bids"][0]["price"] if snap["bids"] else None) or (snap["asks"][0]["price"] if snap["asks"] else 5000000)
    px = lambda cents_delta=0: f"{(ref + cents_delta) / 100:.2f}"

    sell = await bob.place(side="SELL", price=px(-10000), qty="1.000")
    check("limit order accepted by A and D (202)", sell.status_code == 202, sell.text)
    if sell.status_code != 202:
        return
    sid = sell.json()["order_id"]
    o = await bob.wait(sid, lambda o: o["status"] in ("PROCESSING", "PARTIALLY_FILLED", "FILLED", "REJECTED"))
    check("real B accepts it; ACCEPTED reaches A through D", o["status"] == "PROCESSING", o)
    snap = await md_snapshot()
    check("the resting order shows in B's book", any(l["price"] == ref - 10000 for l in snap["asks"]), snap)

    buy = await amy.place(price=px(0), qty="0.300")
    bid = buy.json()["order_id"]
    o = await amy.wait(bid, lambda o: o["status"] == "FILLED" and o["settled_qty"] == "0.300")
    check("real match: taker FILLED and settled", o["status"] == "FILLED" and o["avg_fill_price"] == px(-10000), o)
    b = await amy.bal("USDT")
    cost = 0.3 * (ref - 10000) / 100
    check("buyer paid the maker's price, nothing left held", abs(usdt0 - float(b["available"]) - cost) < 1e-6 and float(b["held"]) == 0, b)
    check("buyer received BTC", (await amy.bal("BTC"))["available"] == "10.300")
    o = await bob.wait(sid, lambda o: o["status"] == "PARTIALLY_FILLED")
    check("maker PARTIALLY_FILLED 0.300", o["filled_qty"] == "0.300", o)

    self_buy = await bob.place(price=px(0), qty="0.100")
    check("self-trade order accepted for D's decision (202)", self_buy.status_code == 202, f"{self_buy.status_code} {self_buy.text}")
    if self_buy.status_code == 202:
        o = await bob.wait(self_buy.json()["order_id"], lambda o: o["status"] == "REJECTED")
        check("D rejects a self-trade; hold released", o.get("reason") == "SELF_TRADE" and float((await bob.bal("USDT"))["held"]) == 0, o)

    # Modify changes the remaining quantity only, down or up; the price stays (team decision).
    priced = await bob.c.put(f"/api/orders/{sid}", json={"price": px(-5000), "qty": "0.500"})
    check("a modify that changes the price is refused (422)", priced.status_code == 422, priced.text)
    for remaining in ("0.500", "0.600"):
        m = await bob.c.put(f"/api/orders/{sid}", json={"qty": remaining})
        check(f"modify to {remaining} accepted (202)", m.status_code == 202, m.text)
        if m.status_code != 202:
            continue
        o = await bob.wait(sid, lambda o: o["status"] != "MODIFYING", 12)
        held = (await bob.bal("BTC"))["held"]
        check(f"modify applied: remaining {remaining}, same price, hold follows",
              o["price"] == px(-10000) and o["status"] == "PARTIALLY_FILLED" and held == remaining, {"order": o, "held": held})

    c = await bob.c.delete(f"/api/orders/{sid}")
    check("cancel accepted", c.status_code == 202, c.text)
    o = await bob.wait(sid, lambda o: o["status"] == "CANCELLED" and not o["cancel_requested"])
    check("cancel through real B releases the rest", o["status"] == "CANCELLED" and (await bob.bal("BTC"))["held"] == "0.000", o)

    rest = await bob.place(side="SELL", price=px(0), qty="0.200")
    check("resting sell accepted (202)", rest.status_code == 202, f"{rest.status_code} {rest.text}")
    if rest.status_code == 202:
        await bob.wait(rest.json()["order_id"], lambda o: o["status"] == "PROCESSING")
    # Other runs may have left orders on B's book, so the expected fill comes from the book itself.
    book = await md_snapshot()
    cap = book["asks"][0]["price"] * 105 // 100 if book["asks"] else 0
    expected = min(500, sum(l["qty"] for l in book["asks"] if l["price"] <= cap))
    mk = await amy.place(type="MARKET", price=None, qty="0.500")
    check("market buy accepted", mk.status_code == 202, mk.text)
    if mk.status_code == 202:
        o = await amy.wait(mk.json()["order_id"], lambda o: o["status"] in ("CANCELLED", "FILLED", "REJECTED"), 12)
        want = "FILLED" if expected == 500 else "CANCELLED"
        check(f"market buy fills what the book offers ({expected / 1000:.3f}), remainder cancelled",
              o["status"] == want and o["filled_qty"] == f"{expected / 1000:.3f}", o)
        check("market buy leaves nothing held", float((await amy.bal("USDT"))["held"]) == 0, await amy.bal("USDT"))

    candles = (await amy.c.get("/api/market/candles", params={"symbol": "BTC-USDT", "interval": "1m"})).json()
    check("candles exist from the real trades", len(candles["candles"]) >= 1 and candles["watermark"] is not None, candles)
    summary = (await amy.c.get("/api/market/summary")).json()
    check("summary has a day open", next(s for s in summary["symbols"] if s["symbol"] == "BTC-USDT")["day_open"] is not None, summary)
    fills = (await amy.c.get("/api/fills")).json()["fills"]
    check("trade history lists the buyer's fills", len(fills) >= 2, fills)

    metrics = (await amy.c.get("/metrics")).text
    line = lambda name: next((l for l in metrics.splitlines() if l.startswith(name)), "")
    check("no notification gaps or refused notifications", line("lame_a_notification_gaps_total").endswith(" 0.0") and "lame_a_notify_failures_total{" not in metrics, line("lame_a_notification_gaps_total"))
    health = (await amy.c.get("/healthz")).json()
    check("A healthy, breaker closed", health["ready"] and health["circuit"] == 0 and not health["shedding"], health)
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


asyncio.run(main())
