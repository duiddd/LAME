"""A fake of D's HTTP API that fails on demand, and helpers that speak D's notifications.

Its behaviour follows D's code on feature/risk_control. Replies are queued per command.
"""
from app.orders.dclient.port import Delivery, OrderQuery, Reply


class FakeD:
    def __init__(self):
        self.placed: list[dict] = []
        self.cancels: list[str] = []
        self.modifies: list[dict] = []
        self.rewinds: list[int] = []
        self.place_replies: list[Reply] = []
        self.cancel_replies: list[Reply] = []
        self.modify_reply = Reply(Delivery.ACK)
        self.queries: dict[str, OrderQuery] = {}

    async def place(self, order: dict) -> Reply:
        self.placed.append(order)
        return self.place_replies.pop(0) if self.place_replies else Reply(Delivery.ACK)

    async def cancel(self, user_id: int, order_id: str) -> Reply:
        self.cancels.append(order_id)
        return self.cancel_replies.pop(0) if self.cancel_replies else Reply(Delivery.ACK)

    async def modify(self, user_id: int, order_id: str, price: int | None, qty: int) -> Reply:
        self.modifies.append({"order_id": order_id, "price": price, "qty": qty})
        return self.modify_reply

    async def query(self, user_id: int, order_id: str) -> OrderQuery | None:
        return self.queries.get(order_id)

    async def rewind(self, from_seq: int) -> bool:
        self.rewinds.append(from_seq)
        return True

    async def close(self) -> None:
        pass


class Notifier:
    """Sends notifications in D's shape with a contiguous notify_seq."""

    def __init__(self, client, day: str = "2026-11-06"):
        self.client = client
        self.day = day
        self.seq = 0
        self.b_seq = 0

    async def send(self, ntype: str, order_id: str | None, user_id: int | None = None, symbol: str = "BTC-USDT", b: bool = False, **fields):
        self.seq += 1
        self.b_seq += 1
        body = {"notify_seq": self.seq, "type": ntype, "order_id": order_id, "user_id": user_id, "symbol": symbol,
                "b_day": self.day if b else None, "b_seq": self.b_seq if b else None, "ts": 1_800_000_000_000, **fields}
        r = await self.client.post("/internal/order-events", json=body)
        assert r.status_code == 200, r.text
        return r

    async def accepted(self, order_id: str, order_seq: int = 7):
        return await self.send("ACCEPTED", order_id, b=True, order_seq=order_seq, rule_version="v1")

    async def trade(self, taker: tuple[str, int, str], maker: tuple[str, int, str], price: int, qty: int):
        side = lambda s: {"order_id": s[0], "user_id": s[1], "side": s[2]}
        return await self.send("TRADE", None, b=True, price=price, qty=qty, taker=side(taker), maker=side(maker))

    async def update(self, order_id: str, status: str, filled: int, remaining: int = 0, reason: str | None = None):
        return await self.send("ORDER_UPDATE", order_id, b=True, status=status, reason=reason, filled_qty=filled, remaining_qty=remaining)

    async def cancel_result(self, order_id: str, result: str):
        return await self.send("CANCEL_RESULT", order_id, result=result)

    async def rejected(self, order_id: str, reason: str = "WASH_TRADE"):
        return await self.send("RISK_REJECTED", order_id, reason=reason, rule_version="v1")
