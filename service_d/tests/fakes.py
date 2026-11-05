"""Test doubles: a small fake of B's /d channel and a fake of A's notification endpoint.

The fake B follows B's wire format (match_engine/doc/runtime.md): commands take a
seq too, outputs carry prev_seq, a new order's ACCEPTED comes first, and a match
emits trade, taker order_event, maker order_event. Matching is simple
price-time priority, enough to drive D's tests.
"""
import asyncio
import json
import time

import httpx
import websockets

from d.bclient import today


def now_ms():
    return int(time.time() * 1000)


class FakeB:
    def __init__(self, max_command_age_ms=1000):
        self.age = max_command_age_ms
        self.day = today()
        self.seq = 0
        self.last_out = 0
        self.outputs = []
        self.book = []          # resting orders
        self.next_order_seq = None
        self.ws = None
        self.drop_new_orders = 0  # silently lose the next N new_order commands
        self.commands = []
        self.server = None
        self.port = None

    async def start(self):
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{self.port}/d"

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def kick(self):
        if self.ws:
            await self.ws.close()

    async def _handler(self, ws):
        first = json.loads(await ws.recv())
        assert first["type"] == "resume", first
        if self.ws:
            await self.ws.close()
        for rec in [r for r in self.outputs if r["seq"] > first["from_seq"]]:
            await ws.send(json.dumps(rec))
        self.ws = ws
        try:
            async for raw in ws:
                await self._command(json.loads(raw))
        except websockets.ConnectionClosed:
            pass
        finally:
            if self.ws is ws:
                self.ws = None

    async def close_day(self):
        """B's end of day: every resting order is cancelled with END_OF_DAY (B document D22)."""
        for o in list(self.book):
            self.book.remove(o)
            o["remaining"] = 0
            await self._event(o, "CANCELLED", "END_OF_DAY")

    async def _out(self, rec):
        self.seq += 1
        rec = {**rec, "day": self.day, "seq": self.seq, "prev_seq": self.last_out}
        self.last_out = self.seq
        self.outputs.append(rec)
        if self.ws:
            await self.ws.send(json.dumps(rec))

    async def _event(self, o, status, reason=None):
        await self._out({"type": "order_event", "symbol": o["symbol"], "order_seq": o.get("order_seq"),
                         "order_id": o.get("order_id"), "user_id": o.get("user_id"), "status": status,
                         "filled": o.get("filled", 0), "remaining": o.get("remaining", 0), "reason": reason})

    async def _command(self, c):
        self.commands.append(c)
        if c["type"] == "new_order" and self.drop_new_orders:
            self.drop_new_orders -= 1
            return
        self.seq += 1                       # the command itself is journaled with a seq
        cmd_seq = self.seq
        if c["type"] == "cancel_order":
            o = next((x for x in self.book if x["user_id"] == c["user_id"] and x["order_id"] == c["order_id"]), None)
            if o is None:
                return await self._event({"symbol": c["symbol"], "order_id": c["order_id"],
                                          "user_id": c["user_id"]}, "REJECTED", "ORDER_NOT_FOUND")
            self.book.remove(o)
            o["remaining"] = 0
            return await self._event(o, "CANCELLED")
        o = {"symbol": c["symbol"], "order_id": c["order_id"], "user_id": c["user_id"], "side": c["side"],
             "type": c["order_type"], "price": c.get("price"), "remaining": c["qty"], "filled": 0,
             "order_seq": cmd_seq}
        if now_ms() - c["sent_at"] > self.age:
            o["remaining"] = 0
            return await self._event(o, "REJECTED", "EXPIRED")
        await self._event(o, "ACCEPTED")
        opp = sorted((x for x in self.book if x["symbol"] == o["symbol"] and x["side"] != o["side"]),
                     key=lambda x: (x["price"] if o["side"] == "BUY" else -x["price"], x["order_seq"]))
        for m in opp:
            if o["remaining"] == 0:
                break
            if o["type"] == "LIMIT" and (m["price"] > o["price"] if o["side"] == "BUY" else m["price"] < o["price"]):
                break
            q = min(o["remaining"], m["remaining"])
            for x in (o, m):
                x["remaining"] -= q
                x["filled"] += q
            await self._out({"type": "trade", "symbol": o["symbol"], "price": m["price"], "qty": q,
                             "taker_seq": o["order_seq"], "maker_seq": m["order_seq"],
                             "taker_user_id": o["user_id"], "maker_user_id": m["user_id"]})
            await self._event(o, "FILLED" if o["remaining"] == 0 else "PARTIALLY_FILLED")
            await self._event(m, "FILLED" if m["remaining"] == 0 else "PARTIALLY_FILLED")
            if m["remaining"] == 0:
                self.book.remove(m)
        if o["remaining"] > 0:
            if o["type"] == "MARKET":
                o["remaining"] = 0
                await self._event(o, "CANCELLED")
            else:
                self.book.append(o)


class FakeA:
    """A's POST /internal/order-events with the contract in A doc 3.1."""

    def __init__(self):
        self.applied = []
        self.down = False

    @property
    def last(self):
        return self.applied[-1]["notify_seq"] if self.applied else 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        n = json.loads(request.content)
        if n["notify_seq"] <= self.last:
            return httpx.Response(200)                      # duplicate
        if n["notify_seq"] > self.last + 1:
            return httpx.Response(409, json={"expected_seq": self.last + 1})
        self.applied.append(n)
        return httpx.Response(200)

    def transport(self):
        return httpx.MockTransport(self.handler)

    def of(self, order_id, ntype=None):
        return [n for n in self.applied if (n.get("order_id") == order_id or
                                            order_id in (n.get("taker", {}).get("order_id"),
                                                         n.get("maker", {}).get("order_id")))
                and (ntype is None or n["type"] == ntype)]


async def wait_until(cond, timeout=5.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(step)
    raise AssertionError("condition not met in time")
