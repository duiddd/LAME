"""Tests run against a real PostgreSQL, because correctness rests on its locks and constraints.

Set TEST_DATABASE_URL; the default is a private local instance on port 55432.
"""
import os
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import Settings
from app.container import build_container
from app.main import create_app
from tests.fakes import FakeD

URL = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://lame@127.0.0.1:55432/lame_test")
ROOT = Path(__file__).resolve().parent.parent
TABLES = ["user_events", "trades", "orders", "ledger_entries", "deposits", "accounts", "refresh_tokens", "users"]


class FakeClock:
    def __init__(self, t: int = 1_800_000_000_000):
        self.t = t

    def now_ms(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


@pytest.fixture(scope="session")
def migrated():
    """Rebuilds the schema once per run, so that the migrations themselves are tested."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    async def drop():
        engine = create_async_engine(URL)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(drop())
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", URL)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def migrated_once(migrated):
    """For hypothesis tests, whose function-scoped fixtures would not reset between examples."""


def make_settings(**overrides) -> Settings:
    defaults = dict(database_url=URL, jwt_secret="test-secret-test-secret-test-secret-0", seed_balances=False, background_tasks=False, _env_file=None)
    return Settings(**{**defaults, **overrides})


@pytest_asyncio.fixture
async def clock():
    return FakeClock()


@pytest_asyncio.fixture
async def fake_d():
    return FakeD()


@pytest_asyncio.fixture
async def container(migrated, clock, fake_d):
    c = build_container(make_settings(), clock=clock, d=fake_d)
    async with c.engine.begin() as conn:
        existing = (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars().all()
        present = [t for t in TABLES if t in existing]
        await conn.execute(text(f"TRUNCATE {', '.join(present)} RESTART IDENTITY CASCADE"))
        await conn.execute(text("UPDATE notify_cursor SET last_notify_seq = 0"))
    yield c
    await c.engine.dispose()


@pytest_asyncio.fixture
async def client(container):
    app = create_app(container)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


async def sign_up(client: httpx.AsyncClient, username: str = "amy_trader", password: str = "correct-horse-1") -> dict:
    r = await client.post("/api/auth/register", json={"username": username, "password": password})
    assert r.status_code == 201, r.text
    return r.json()
