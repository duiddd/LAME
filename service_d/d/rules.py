"""The two risk rules (architecture doc section 3). Pure functions: no clock, no I/O.

R1 SELF_TRADE: the new order could trade against the user's own opposite order.
R2 WASH_TRADE: the same check against the other accounts in the user's linked group.

"Opposite orders" are the user's resting orders in B's book plus orders D has
approved that B has not answered yet (in flight, RD4).
"""
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Opposite:
    user_id: int
    price: Optional[int]      # None for a market order (only possible while in flight)
    remaining: int
    order_seq: Optional[int]  # B's seq, None while in flight
    in_flight: bool


def _crosses(side: str, limit: int, opp: Opposite) -> bool:
    if opp.price is None:
        return True
    return opp.price <= limit if side == "BUY" else opp.price >= limit


def _hits(order: dict, opposites: list, owners: frozenset) -> bool:
    if not owners:
        return False
    if order["type"] == "LIMIT":
        return any(o.user_id in owners and _crosses(order["side"], order["price"], o) for o in opposites)
    # Market order: an owner's in-flight order could sit at the top of the book, so it counts.
    if any(o.in_flight and o.user_id in owners for o in opposites):
        return True
    # Otherwise walk the resting book in price-time priority until the quantity is used up (RD3).
    resting = [o for o in opposites if not o.in_flight]
    best_first = sorted(resting, key=lambda o: (o.price if order["side"] == "BUY" else -o.price, o.order_seq))
    left = order["qty"]
    for o in best_first:
        if left <= 0:
            break
        if o.user_id in owners:
            return True
        left -= o.remaining
    return False


def decide(order: dict, opposites: Iterable[Opposite], group: frozenset, rules: dict) -> Optional[str]:
    """Return None to approve, or the rejection reason. R1 is checked before R2."""
    opposites = list(opposites)
    me = frozenset({order["user_id"]})
    if rules.get("self_trade", True) and _hits(order, opposites, me):
        return "SELF_TRADE"
    if rules.get("wash_trade", True) and _hits(order, opposites, frozenset(group) - me):
        return "WASH_TRADE"
    return None
