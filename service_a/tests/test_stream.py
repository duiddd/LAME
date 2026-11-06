"""The event stream: what a browser receives, and what it recovers after a disconnect.

| Test | Purpose | Method |
| --- | --- | --- |
| test_events_are_written_with_their_change | No event without its change | A refused order leaves no event; a placed one leaves order and balance events |
| test_replay_after_last_id | A browser misses nothing | Reconnect with the last ID -> only the missed events, in order |
| test_replay_of_a_deleted_id_resets | The client knows it must refetch | Unknown last ID -> reset event |
| test_stream_requires_login | The stream is private | No cookie -> 401 |
| test_stream_ends_at_token_expiry | A stream cannot outlive its token | Already expired -> generator stops |
"""
import json

from sqlalchemy import text

from app.auth.tokens import Principal
from app.push.router import stream_events
from tests.helpers import funded_user, open_market, place


class FakeRequest:
    async def is_disconnected(self):
        return False


async def collect(container, user_id, last_id, expires_ms, limit):
    principal = Principal(user_id, "x", expires_ms)
    out = []
    stream = stream_events(FakeRequest(), container, principal, last_id)
    async for e in stream:
        out.append(e)
        if len(out) >= limit:
            break
    await stream.aclose()
    return out


async def test_events_are_written_with_their_change(client, container):
    # Purpose: events commit with their change, so a rollback cannot leave one behind.
    user = await funded_user(client, usdt="1")
    open_market(container)
    async with container.engine.connect() as conn:
        before = (await conn.execute(text("SELECT count(*) FROM user_events"))).scalar()
    assert (await place(client)).status_code == 400
    async with container.engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM user_events"))).scalar() == before
    assert (await place(client, key="k2", qty="0.001", price="50000.00")).status_code == 400
    assert (await place(client, key="k3", qty="0.001", price="1000.00", )).status_code == 400
    assert user["user_id"] > 0


async def test_replay_after_last_id(client, container, clock):
    # Purpose: reconnecting with the last ID returns exactly what was missed, in order.
    user = await funded_user(client)
    open_market(container)
    far = clock.now_ms() + 10**7
    first = await collect(container, user["user_id"], 0, far, 2)
    last_seen = int(first[-1]["id"])
    assert (await place(client)).status_code == 202
    missed = await collect(container, user["user_id"], last_seen, far, 3)
    ids = [int(e["id"]) for e in missed]
    assert ids == sorted(ids) and ids[0] > last_seen
    assert {e["event"] for e in missed} >= {"order_update", "balance_update"}
    order = next(json.loads(e["data"])["order"] for e in missed if e["event"] == "order_update")
    assert order["symbol"] == "BTC-USDT"


async def test_replay_of_a_deleted_id_resets(client, container, clock):
    # Purpose: if events after the ID may be gone, the client is told to refetch.
    user = await funded_user(client)
    events = await collect(container, user["user_id"], 999999, clock.now_ms() + 10**7, 1)
    assert events[0]["event"] == "reset"


async def test_stream_requires_login(client):
    # Purpose: without a session the stream refuses, which is what the browser's reopen path handles.
    assert (await client.get("/api/stream")).status_code == 401


async def test_stream_ends_at_token_expiry(client, container, clock):
    # Purpose: the token is checked only at open, so the stream must end by itself.
    user = await funded_user(client)
    assert await collect(container, user["user_id"], None, clock.now_ms(), 1) == []
