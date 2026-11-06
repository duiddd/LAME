"""The composition root: everything with state or a connection pool is built here, once (D27)."""
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .accounts.service import AccountsService
from .auth.passwords import Passwords
from .auth.service import AuthService
from .auth.tokens import Tokens
from .config import Catalog, Settings, load_catalog
from .market.cache import PriceCache
from .metrics import Metrics
from .orders.dclient.breaker import CircuitBreaker
from .orders.dclient.http import HttpDClient
from .orders.dclient.port import DClient
from .orders.events.service import EventsService
from .orders.service import OrdersService
from .orders.views import OrderViews
from .ports import Clock, IdGenerator, SystemClock, UlidGenerator
from .push.hub import Hub


@dataclass
class Container:
    settings: Settings
    catalog: Catalog
    engine: AsyncEngine
    clock: Clock
    ids: IdGenerator
    tokens: Tokens
    passwords: Passwords
    auth: AuthService
    accounts: AccountsService
    metrics: Metrics
    hub: Hub
    prices: PriceCache
    breaker: CircuitBreaker
    d: DClient
    views: OrderViews
    orders: OrdersService
    events: EventsService


def build_container(settings: Settings, clock: Clock | None = None, ids: IdGenerator | None = None, d: DClient | None = None) -> Container:
    clock = clock or SystemClock()
    catalog = load_catalog(settings.symbols_file)
    engine = create_async_engine(settings.database_url, pool_size=10, max_overflow=10)
    metrics = Metrics()
    tokens = Tokens(settings.jwt_secret, settings.access_ttl_s, clock)
    passwords = Passwords(settings.hash_concurrency)
    auth = AuthService(engine, passwords, tokens, clock, settings.refresh_ttl_s, settings.refresh_grace_s)
    hub = Hub(clock)
    accounts = AccountsService(engine, catalog, clock, hub)
    prices = PriceCache(clock, settings.md_stale_s)
    breaker = CircuitBreaker(settings.breaker_failures, settings.breaker_open_s, clock)
    d = d or HttpDClient(settings.d_url, settings.d_connect_timeout_s, settings.d_total_timeout_s, breaker, metrics, clock)
    views = OrderViews(catalog, accounts, hub)
    ids = ids or UlidGenerator()
    orders = OrdersService(engine, settings, catalog, clock, ids, accounts, views, hub, prices, d, breaker, metrics)
    events = EventsService(engine, catalog, clock, accounts, views, hub, metrics)
    return Container(settings, catalog, engine, clock, ids, tokens, passwords, auth, accounts, metrics, hub, prices,
                     breaker, d, views, orders, events)


def get_container(request: Request) -> Container:
    return request.app.state.container
