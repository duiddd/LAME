"""D's view of B's book, built from B's outputs (RD5). Shared by the engine and the replay tool,
so a replay rebuilds exactly the view the engine decided on."""
from .rules import Opposite


class BookView:
    def __init__(self):
        self.resting = {}   # symbol -> {b_order_id: (side, user_id, price, remaining, order_seq)}
        self.by_seq = {}    # (day, order_seq) -> b_order_id

    def resolve(self, rec: dict, b_orders: dict):
        """The b_orders row an order_event is about, or None (trades name orders by seq)."""
        if rec["type"] != "order_event":
            return None
        b_id = rec.get("order_id")
        if b_id is None:
            b_id = self.by_seq.get((rec["day"], rec.get("order_seq")))
        return b_orders.get(b_id)

    def apply(self, rec: dict, b) -> None:
        if rec["type"] != "order_event" or b is None:
            return
        side = self.resting.setdefault(b["symbol"], {})
        status = rec["status"]
        if status == "ACCEPTED":
            self.by_seq[(rec["day"], rec["order_seq"])] = b["b_order_id"]
        if status in ("ACCEPTED", "PARTIALLY_FILLED") and rec["remaining"] > 0 and b["type"] == "LIMIT":
            side[b["b_order_id"]] = (b["side"], b["user_id"], b["price"], rec["remaining"], rec["order_seq"])
        elif not (status == "REJECTED" and rec.get("reason") == "ORDER_NOT_FOUND"):
            side.pop(b["b_order_id"], None)     # filled, cancelled or rejected: off the book

    def opposites(self, symbol: str, side: str):
        return [Opposite(user_id=u, price=p, remaining=rem, order_seq=seq, in_flight=False)
                for (s, u, p, rem, seq) in self.resting.get(symbol, {}).values() if s == side]
