"""The composition root: everything with state or a connection pool is built here, once (D27)."""
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .accounts.service import AccountsService
from .auth.passwords import Passwords
from .auth.service import AuthService
from .auth.tokens import Tokens
from .config import Catalog, Settings, load_catalog
from .metrics import Metrics
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


def build_container(settings: Settings, clock: Clock | None = None, ids: IdGenerator | None = None) -> Container:
    clock = clock or SystemClock()
    catalog = load_catalog(settings.symbols_file)
    engine = create_async_engine(settings.database_url, pool_size=10, max_overflow=10)
    tokens = Tokens(settings.jwt_secret, settings.access_ttl_s, clock)
    passwords = Passwords(settings.hash_concurrency)
    auth = AuthService(engine, passwords, tokens, clock, settings.refresh_ttl_s, settings.refresh_grace_s)
    hub = Hub(clock)
    accounts = AccountsService(engine, catalog, clock, hub)
    return Container(settings, catalog, engine, clock, ids or UlidGenerator(), tokens, passwords, auth, accounts, Metrics(), hub)


def get_container(request: Request) -> Container:
    return request.app.state.container
