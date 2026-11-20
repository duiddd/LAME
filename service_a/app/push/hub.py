"""Wakes the streams of a user after a commit, and writes the event log they replay from (D14)."""
import asyncio
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..ports import Clock


class Hub:
    def __init__(self, clock: Clock):
        self._clock = clock
        self._waiters: dict[int, set[asyncio.Event]] = {}

    async def write(self, conn: AsyncConnection, user_id: int, event_type: str, payload: dict) -> None:
        """In the caller's transaction, so that an event exists exactly when its change does."""
        await conn.execute(text(
            "INSERT INTO user_events (user_id, type, payload, created_at) VALUES (:u, :t, :p, :n)"),
            {"u": user_id, "t": event_type, "p": json.dumps(payload), "n": self._clock.now_ms()})

    def wake(self, user_ids) -> None:
        """Called after the commit: a user must never see a change that was rolled back."""
        for user_id in user_ids:
            for waiter in self._waiters.get(user_id, ()):
                waiter.set()

    def register(self, user_id: int) -> asyncio.Event:
        waiter = asyncio.Event()
        self._waiters.setdefault(user_id, set()).add(waiter)
        return waiter

    def unregister(self, user_id: int, waiter: asyncio.Event) -> None:
        waiters = self._waiters.get(user_id)
        if waiters is not None:
            waiters.discard(waiter)
            if not waiters:
                del self._waiters[user_id]


async def latest_event_id(conn: AsyncConnection, user_id: int) -> int:
    row = (await conn.execute(text("SELECT coalesce(max(event_id), 0) AS m FROM user_events WHERE user_id = :u"), {"u": user_id})).first()
    return row.m


async def event_exists(conn: AsyncConnection, user_id: int, event_id: int) -> bool:
    row = await conn.execute(text("SELECT 1 FROM user_events WHERE user_id = :u AND event_id = :e"), {"u": user_id, "e": event_id})
    return row.first() is not None


async def events_after(conn: AsyncConnection, user_id: int, event_id: int, limit: int = 500):
    return (await conn.execute(text(
        "SELECT event_id, type, payload FROM user_events WHERE user_id = :u AND event_id > :e ORDER BY event_id LIMIT :l"),
        {"u": user_id, "e": event_id, "l": limit})).all()
