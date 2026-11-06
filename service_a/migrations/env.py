"""Runs migrations over the application's async driver, with the URL from Settings."""
import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings


def run(connection):
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def main():
    url = context.config.get_main_option("sqlalchemy.url") or Settings().database_url
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        await conn.run_sync(run)
        await conn.commit()
    await engine.dispose()


asyncio.run(main())
