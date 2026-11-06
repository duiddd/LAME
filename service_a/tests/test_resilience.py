"""What happens when D is slow, silent or gone.

| Test | Purpose | Method |
| --- | --- | --- |
| test_breaker_opens_and_recovers | Fail fast, then try again | 5 failures -> closed to orders; after 10 s one trial; success closes |
| test_http_client_classifies_every_answer | The answer decides what the user is told | Each status and network failure -> expected Delivery |
| test_unknown_is_resent_then_acknowledged | A lost reply heals itself | UNKNOWN, resend identical content, ACK -> SENT |
| test_resend_503_stays_unknown | A resend's refusal proves nothing | 503 on resend -> UNKNOWN, hold kept |
| test_unknown_past_window_is_cancelled | No order long after the user asked | Past RESEND_WINDOW -> cancel at D; TOMBSTONED releases |
| test_pending_after_a_crash_becomes_unknown | A crash between hold and send | Old PENDING -> UNKNOWN |
| test_failed_cancel_is_retried | Withdrawing is always retried | Cancel fails -> resent until acknowledged |
| test_market_remainder_cancel_survives_a_crash | Section 7.3 after a crash | cancel_wanted and unsent -> reconcile sends it |
| test_sent_order_is_queried_with_backoff | A lost notification is found | 10 s, then 20 s, then 40 s, then 60 s |
| test_query_finding_a_gap_rewinds | D resends from A's cursor | last_notify_seq above the cursor -> rewind(cursor + 1) |
| test_query_never_moves_funds | One way for a release | A query saying rejected changes nothing |
| test_unknown_to_d_is_cancelled | D lost the order | 404 -> cancel |
| test_shedding_needs_three_breaches_and_recovers_with_hysteresis | Protect the gate without flapping | Slow samples -> active on the third; resumes below 80% |
| test_stalled_orders_trigger_shedding | No outcomes must not look healthy | Waiting order past the threshold counts |
| test_shedding_refuses_before_holding | Refuse at the door | Active -> 503, nothing held, cancel still works |
| test_supervisor_restarts_a_crashed_task | A dead task is not silent | Crash -> restarted, counted, reported |
| test_healthz_and_metrics | Operators can see all this | /healthz shows tasks, /metrics has A's series |
| test_old_events_are_purged | The event log stays bounded | Older than the retention -> deleted |
"""
import asyncio

import httpx
import pytest
from sqlalchemy import text

from app.orders.dclient.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from app.orders.dclient.http import HttpDClient
from app.orders.dclient.port import Delivery, OrderQuery, Reply
from app.metrics import Metrics
from app.shedding import Shedder
from app.supervisor import Supervisor
from tests.conftest import FakeClock
from tests.fakes import Notifier
from tests.helpers import balance, funded_user, open_market, order_row, place


@pytest.fixture
def market(container):
    open_market(container)


def test_breaker_opens_and_recovers():
    # Purpose: D's failures stop new orders for a while, and one trial decides.
    clock = FakeClock()
    b = CircuitBreaker(5, 10, clock)
    for _ in range(5):
        assert b.allow()
        b.failure()
    assert b.state == OPEN and not b.allow()
    clock.advance(10000)
    assert b.allow() and b.state == HALF_OPEN and not b.allow()
    b.failure()
    assert b.state == OPEN
    clock.advance(10000)
    assert b.allow()
    b.success()
    assert b.state == CLOSED and b.allow()


@pytest.mark.parametrize("status,body,expected", [
    (202, {"order_id": "x", "status": "RECEIVED"}, Delivery.ACK),
    (503, {"error": "RISK_UNAVAILABLE"}, Delivery.NOT_TAKEN),
    (503, {"error": "RISK_BUSY"}, Delivery.NOT_TAKEN),
    (400, {"error": "INVALID_REQUEST"}, Delivery.INVALID),
    (409, {"error": "ORDER_ID_CONFLICT"}, Delivery.CONFLICT),
    (501, {"error": "NOT_IMPLEMENTED"}, Delivery.NOT_IMPLEMENTED),
    (500, {}, Delivery.UNKNOWN),
    (503, {}, Delivery.UNKNOWN),
])
async def test_http_client_classifies_every_answer(status, body, expected):
    # Purpose: only D's own 503 proves it kept no record; any other 5xx may have been processed.
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json=body))
    clock = FakeClock()
    client = HttpDClient("http://d", 0.1, 0.1, CircuitBreaker(5, 10, clock), Metrics(), clock, transport=transport)
    assert (await client.place({"order_id": "x"})).delivery is expected
    await client.close()


@pytest.mark.parametrize("error,expected", [
    (httpx.ConnectError("no"), Delivery.NOT_TAKEN),
    (httpx.ConnectTimeout("no"), Delivery.NOT_TAKEN),
    (httpx.PoolTimeout("no"), Delivery.NOT_TAKEN),
    (httpx.ReadTimeout("no"), Delivery.UNKNOWN),
    (httpx.RemoteProtocolError("no"), Delivery.UNKNOWN),
])
async def test_http_client_classifies_network_failures(error, expected):
    # Purpose: never connected means certainly not received; sent without a reply means unknown.
    def fail(_):
        raise error
    clock = FakeClock()
    breaker = CircuitBreaker(1, 10, clock)
    client = HttpDClient("http://d", 0.1, 0.1, breaker, Metrics(), clock, transport=httpx.MockTransport(fail))
    assert (await client.place({"order_id": "x"})).delivery is expected
    assert breaker.state == OPEN
    await client.close()


async def unknown_order(client, fake_d):
    await funded_user(client)
    fake_d.place_replies = [Reply(Delivery.UNKNOWN)]
    return (await place(client)).json()["order_id"]


async def test_unknown_is_resent_then_acknowledged(client, container, fake_d, clock, market):
    # Purpose: D deduplicates, and rejects different content, so the resend must be identical.
    order_id = await unknown_order(client, fake_d)
    clock.advance(1000)
    await container.reconciler.tick()
    assert fake_d.placed[0] == fake_d.placed[1]
    assert (await order_row(container, order_id)).status == "SENT"


async def test_resend_503_stays_unknown(client, container, fake_d, clock, market):
    # Purpose: the first attempt may still be on its way to D, so this proves nothing.
    order_id = await unknown_order(client, fake_d)
    fake_d.place_replies = [Reply(Delivery.NOT_TAKEN)]
    clock.advance(1000)
    await container.reconciler.tick()
    assert (await order_row(container, order_id)).status == "UNKNOWN"
    assert (await balance(client, "USDT"))["held"] == "50000.00000"


async def test_unknown_past_window_is_cancelled(client, container, fake_d, clock, market):
    # Purpose: after RESEND_WINDOW the order must not take effect; the tombstone ends it.
    order_id = await unknown_order(client, fake_d)
    clock.advance(int(container.settings.resend_window_s * 1000) + 1)
    await container.reconciler.tick()
    await container.reconciler.tick()
    assert fake_d.cancels == [order_id]
    await Notifier(client).cancel_result(order_id, "TOMBSTONED")
    assert (await balance(client, "USDT"))["held"] == "0.00000"
    assert (await order_row(container, order_id)).status == "CANCELLED"


async def test_pending_after_a_crash_becomes_unknown(client, container, clock, market):
    # Purpose: a crash between the hold and the send leaves an order that may or may not be at D.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    async with container.engine.begin() as conn:
        await conn.execute(text("UPDATE orders SET status = 'PENDING' WHERE order_id = :o"), {"o": order_id})
    await container.reconciler.tick()
    assert (await order_row(container, order_id)).status == "PENDING"
    clock.advance(5000)
    await container.reconciler.tick()
    assert (await order_row(container, order_id)).status == "UNKNOWN"


async def test_failed_cancel_is_retried(client, container, fake_d, clock, market):
    # Purpose: a user can always withdraw, even while D is failing.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    fake_d.cancel_replies = [Reply(Delivery.UNKNOWN)]
    await client.delete(f"/api/orders/{order_id}")
    assert (await order_row(container, order_id)).cancel_acked is False
    clock.advance(10000)
    await container.reconciler.tick()
    assert fake_d.cancels == [order_id, order_id]
    assert (await order_row(container, order_id)).cancel_acked is True


async def test_market_remainder_cancel_survives_a_crash(client, container, fake_d, market):
    # Purpose: the wish to cancel is committed with ACCEPTED, so reconcile can always send it.
    await funded_user(client)
    order_id = (await place(client, type="MARKET", price=None, qty="0.100")).json()["order_id"]
    await Notifier(client).accepted(order_id)
    assert fake_d.cancels == []
    await container.reconciler.tick()
    assert fake_d.cancels == [order_id]
    await container.reconciler.tick()
    assert fake_d.cancels == [order_id]


async def test_sent_order_is_queried_with_backoff(client, container, fake_d, clock, market):
    # Purpose: dense at first, then slower; the delays are the documented ones.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    calls = []
    original = fake_d.query

    async def query(user_id, oid):
        calls.append(clock.now_ms())
        return await original(user_id, oid)
    fake_d.query = query
    start = clock.now_ms()
    for _ in range(140):
        clock.advance(1000)
        await container.reconciler.tick()
    assert [(c - start) // 1000 for c in calls] == [10, 30, 70, 130]
    assert (await order_row(container, order_id)).status == "SENT"


async def test_query_finding_a_gap_rewinds(client, container, fake_d, clock, market):
    # Purpose: if D has notified further than A's cursor, A asks for a resend from its cursor.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    fake_d.queries[order_id] = OrderQuery("LIVE", 5, True)
    clock.advance(10000)
    await container.reconciler.tick()
    assert fake_d.rewinds == [1]


async def test_query_never_moves_funds(client, container, fake_d, clock, market):
    # Purpose: funds move on notifications only, so there is one way for a release to happen.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    fake_d.queries[order_id] = OrderQuery("REJECTED_BY_D", None, True)
    clock.advance(10000)
    await container.reconciler.tick()
    assert (await balance(client, "USDT"))["held"] == "50000.00000"


async def test_unknown_to_d_is_cancelled(client, container, fake_d, clock, market):
    # Purpose: if D has no record, a cancel leaves a tombstone, and its result releases the hold.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    fake_d.queries[order_id] = OrderQuery(None, None, False)
    clock.advance(10000)
    await container.reconciler.tick()
    await container.reconciler.tick()
    assert fake_d.cancels == [order_id]


def make_shedder(clock):
    return Shedder(1.0, 30, 3, 0.8, clock, Metrics())


def test_shedding_needs_three_breaches_and_recovers_with_hysteresis():
    # Purpose: one bad second does not shed, and a value just under the threshold does not resume.
    clock = FakeClock()
    s = make_shedder(clock)

    def sample(seconds):
        s.started("o")
        clock.advance(int(seconds * 1000))
        s.finished("o")
    sample(2)
    s.evaluate()
    s.evaluate()
    assert not s.active
    s.evaluate()
    assert s.active
    clock.advance(31000)
    sample(0.9)
    s.evaluate()
    assert s.active
    clock.advance(31000)
    sample(0.5)
    s.evaluate()
    assert not s.active


def test_stalled_orders_trigger_shedding():
    # Purpose: with notifications stalled there are no samples, and that must not look healthy.
    clock = FakeClock()
    s = make_shedder(clock)
    s.started("stuck")
    clock.advance(5000)
    for _ in range(3):
        s.evaluate()
    assert s.active


async def test_shedding_refuses_before_holding(client, container, fake_d, market):
    # Purpose: overload is refused at the door, while a user can still withdraw orders.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    container.shedder.active = True
    r = await place(client, key="k2")
    assert r.status_code == 503 and r.json()["reason"] == "SYSTEM_BUSY"
    assert len(fake_d.placed) == 1
    assert (await client.delete(f"/api/orders/{order_id}")).status_code == 202


async def test_supervisor_restarts_a_crashed_task():
    # Purpose: an unawaited task would die silently; this one restarts and is counted.
    metrics = Metrics()
    s = Supervisor(metrics)
    runs = []
    finished = asyncio.Event()

    async def task():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("boom")
        finished.set()
    s.start("t", task)
    await asyncio.wait_for(finished.wait(), 3)
    assert len(runs) == 2 and metrics.restarts.labels("t")._value.get() == 1
    await s.stop()


async def test_healthz_and_metrics(client):
    # Purpose: operators can see readiness and A's series.
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json()["ready"] is True and "tasks" in r.json()
    body = (await client.get("/metrics")).text
    assert "lame_a_orders_total" in body and "lame_a_notification_gaps_total" in body


async def test_old_events_are_purged(client, container, clock):
    # Purpose: the event log keeps only what a reconnect can need.
    await funded_user(client)
    await container.reconciler.tick()
    clock.advance(container.settings.event_retention_s * 1000 + 60001)
    await container.reconciler.tick()
    async with container.engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM user_events"))).scalar() == 0
