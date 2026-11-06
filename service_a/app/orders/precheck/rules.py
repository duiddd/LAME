"""The size and price checks. Pure functions: integer inputs, a result or a refusal (D10, D11)."""
from dataclasses import dataclass
from fractions import Fraction

from ...config import NotOnGrid, Symbol, to_units
from ...errors import AppError
from ...market.cache import Snapshot


def refuse(reason: str, message: str) -> AppError:
    return AppError(400, reason, message)


def parse_price(sym: Symbol, text: str) -> int:
    try:
        price = to_units(text, sym.price_decimals)
    except NotOnGrid as e:
        raise refuse("INVALID_TICK", f"The price can have at most {sym.price_decimals} decimals") from e
    if price <= 0 or price % sym.tick:
        raise refuse("INVALID_TICK", "The price must be positive and on the tick grid")
    return price


def parse_qty(sym: Symbol, text: str) -> int:
    try:
        qty = to_units(text, sym.qty_decimals)
    except NotOnGrid as e:
        raise refuse("INVALID_LOT", f"The amount can have at most {sym.qty_decimals} decimals") from e
    if qty <= 0 or qty < sym.min_qty:
        raise refuse("QTY_TOO_SMALL", "The amount is below the minimum for this pair")
    if qty % sym.lot:
        raise refuse("INVALID_LOT", "The amount must be on the lot grid")
    if qty > sym.max_qty:
        raise refuse("QTY_TOO_LARGE", "The amount is above the maximum for this pair")
    return qty


@dataclass(frozen=True)
class Priced:
    """What to send and what to hold. wire_* is what D and B see."""

    wire_type: str
    wire_price: int | None
    hold_price: int


def check_limit(sym: Symbol, price: int, snapshot: Snapshot | None, band: float) -> Priced:
    if snapshot is None:
        raise refuse("NO_FRESH_PRICE", "Market data is interrupted")
    reference = snapshot.reference
    # With a fresh feed and no price at all (start of day), the band is skipped, or nobody could
    # ever place the first order.
    if reference is not None and abs(Fraction(price - reference, reference)) > Fraction(str(band)):
        raise refuse("PRICE_OUT_OF_BAND", "The price is too far from the market price")
    return Priced("LIMIT", price, price)


def check_market(sym: Symbol, side: str, snapshot: Snapshot | None, protection: float) -> Priced:
    """A market buy goes out as a limit order at the cap, since B's market orders have no limit
    and a thin book could otherwise cost more than A held. A market sell holds base and cannot
    overdraw, so it stays a true market order.
    """
    if snapshot is None:
        raise refuse("NO_FRESH_PRICE", "Market data is interrupted")
    factor = Fraction(str(protection))
    if side == "BUY":
        if snapshot.best_ask is None:
            raise refuse("NO_LIQUIDITY", "There are no sell orders to trade against")
        cap = int(snapshot.best_ask * (1 + factor)) // sym.tick * sym.tick
        return Priced("LIMIT", cap, cap)
    if snapshot.best_bid is None:
        raise refuse("NO_LIQUIDITY", "There are no buy orders to trade against")
    return Priced("MARKET", None, int(snapshot.best_bid * (1 - factor)))


def check_notional(sym: Symbol, price: int, qty: int) -> None:
    if price * qty > sym.max_notional:
        raise refuse("NOTIONAL_TOO_LARGE", "The order's value is above the maximum for this pair")
