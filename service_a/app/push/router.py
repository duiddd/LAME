import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from ..auth.deps import current_user
from ..auth.tokens import Principal
from ..container import Container, get_container
from . import hub as events

router = APIRouter(prefix="/api", tags=["push"])

WAIT_S = 5.0


async def stream_events(request: Request, c: Container, user: Principal, last_id: int | None):
    """Replays what the client missed, then follows new events until the access token expires.

    The token is checked only when the stream opens, so the stream ends at its expiry; otherwise it
    would outlive the token's validity. The browser reconnects with its refreshed cookie.
    """
    waiter = c.hub.register(user.user_id)
    try:
        async with c.engine.connect() as conn:
            if last_id is None:
                cursor = await events.latest_event_id(conn, user.user_id)
            elif last_id == 0 or await events.event_exists(conn, user.user_id, last_id):
                cursor = last_id
            else:
                # The events after this ID may have been deleted: the client must refetch.
                cursor = await events.latest_event_id(conn, user.user_id)
                yield {"event": "reset", "id": str(cursor), "data": "{}"}
        while c.clock.now_ms() < user.expires_ms and not await request.is_disconnected():
            waiter.clear()
            async with c.engine.connect() as conn:
                rows = await events.events_after(conn, user.user_id, cursor)
            for row in rows:
                cursor = row.event_id
                yield {"event": row.type, "id": str(row.event_id), "data": row.payload}
            if not rows:
                try:
                    await asyncio.wait_for(waiter.wait(), WAIT_S)
                except asyncio.TimeoutError:
                    pass
    finally:
        c.hub.unregister(user.user_id, waiter)


@router.get("/stream")
async def stream(request: Request, last_event_id: Optional[int] = None, user: Principal = Depends(current_user),
                 c: Container = Depends(get_container)):
    """Server-Sent Events with the user's own `order_update` and `balance_update`."""
    header = request.headers.get("last-event-id")
    last_id = int(header) if header and header.isdigit() else last_event_id
    return EventSourceResponse(stream_events(request, c, user, last_id), ping=15)
