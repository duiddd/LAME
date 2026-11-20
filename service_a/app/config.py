"""Runtime settings from environment variables, and the symbol file A reads at start-up."""
import json
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic_settings import BaseSettings

HERE = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://lame:lame@postgres:5432/lame"
    d_url: str = "http://service-d:8400"
    b_md_url: str = "ws://service-b:9999"
    symbols_file: str = str(HERE / "config/symbols.json")
    linked_accounts_file: str = str(HERE / "config/linked_accounts.json")

    jwt_secret: str = "dev-only-secret-change-me-at-least-32-bytes"
    access_ttl_s: int = 900
    refresh_ttl_s: int = 7 * 24 * 3600
    refresh_grace_s: int = 5
    cookie_secure: bool = False
    allowed_origins: str = "http://localhost:3000,http://localhost:5173"
    hash_concurrency: int = 4

    rate_per_s: float = 5.0
    rate_burst: float = 10.0
    cancel_rate_per_s: float = 20.0
    cancel_burst: float = 40.0
    price_band: float = 0.10
    market_protection: float = 0.05
    md_stale_s: float = 3.0

    d_connect_timeout_s: float = 0.5
    d_total_timeout_s: float = 2.0
    resend_window_s: float = 5.0
    resend_base_s: float = 0.2
    breaker_failures: int = 5
    breaker_open_s: float = 10.0
    shed_threshold_s: float = 1.0
    shed_window_s: float = 30.0
    shed_breaches: int = 3
    shed_resume_ratio: float = 0.8
    reconcile_tick_s: float = 1.0
    event_retention_s: int = 24 * 3600
    modify_mode: str = "d_modify"
    seed_balances: bool = True
    background_tasks: bool = True

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def origins(self) -> set[str]:
        return {o.strip() for o in self.allowed_origins.split(",") if o.strip()}


@dataclass(frozen=True)
class Asset:
    name: str
    decimals: int
    max_deposit: int


@dataclass(frozen=True)
class Symbol:
    symbol: str
    base: str
    quote: str
    tick: int
    lot: int
    price_decimals: int
    qty_decimals: int
    min_qty: int
    max_qty: int
    max_notional: int
    quote_scale: int = 1          # price units x quantity units per quote unit; set by load_catalog


@dataclass(frozen=True)
class Catalog:
    assets: dict[str, Asset]
    symbols: dict[str, Symbol]

    def price_to_int(self, sym: Symbol, text: str) -> int:
        return to_units(text, sym.price_decimals)

    def qty_to_int(self, sym: Symbol, text: str) -> int:
        return to_units(text, sym.qty_decimals)


class NotOnGrid(ValueError):
    """A decimal string has more decimals than the unit allows, or is not a number."""


def to_units(text: str, decimals: int) -> int:
    """Converts a decimal string to integer units, refusing anything that would be rounded."""
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError) as e:
        raise NotOnGrid(f"not a decimal number: {text!r}") from e
    if not value.is_finite():
        raise NotOnGrid(f"not a finite number: {text!r}")
    units = value.scaleb(decimals)
    if units != units.to_integral_value():
        raise NotOnGrid(f"{text!r} has more than {decimals} decimals")
    return int(units)


def from_units(units: int, decimals: int) -> str:
    """Formats integer units as a plain decimal string with a fixed number of decimals."""
    value = Decimal(units).scaleb(-decimals)
    return format(value, "f") if decimals else str(units)


def quote_amount(sym: Symbol, price: int, qty: int) -> int:
    """price x qty in the quote asset's smallest units. Exact for any price on the tick and any
    quantity on the lot, which load_catalog checks; anything else is a bug, not a rounding case."""
    # Every hold, settlement and value check goes through here, so no amount is ever rounded.
    # 所有 hold、結算與名目金額檢查都經過這裡,金額從不四捨五入。
    value, rest = divmod(price * qty, sym.quote_scale)
    if rest:
        raise ValueError(f"{sym.symbol}: {price} x {qty} is not a whole number of {sym.quote} units")
    return value


def load_catalog(path: str) -> Catalog:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    assets = {}
    for name, a in raw["assets"].items():
        assets[name] = Asset(name, a["decimals"], to_units(a["max_deposit"], a["decimals"]))
    symbols = {}
    for s in raw["symbols"]:
        sym = Symbol(**s)
        if assets[sym.base].decimals != sym.qty_decimals:
            raise ValueError(f"{sym.symbol}: base decimals must equal qty_decimals")
        # The quote asset may have fewer decimals than price x qty, as long as one tick times one lot
        # is still a whole number of quote units; then every price x qty on the grid is exact.
        # 報價資產的小數位可少於 價格 x 數量,只要 1 tick x 1 lot 仍是整數個報價單位,網格上的金額就都精確。
        shift = sym.price_decimals + sym.qty_decimals - assets[sym.quote].decimals
        if shift < 0:
            raise ValueError(f"{sym.symbol}: quote decimals exceed price_decimals + qty_decimals")
        if (sym.tick * sym.lot) % 10 ** shift:
            raise ValueError(f"{sym.symbol}: one tick times one lot is not a whole number of {sym.quote} units")
        symbols[sym.symbol] = replace(sym, quote_scale=10 ** shift)
    return Catalog(assets, symbols)
