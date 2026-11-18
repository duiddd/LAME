"""End-to-end tests of D against the fake B and the fake A."""
import httpx

from d.api import create_app
from tests.fakes import wait_until

BUY, SELL = "BUY", "SELL"


def req(oid, user, side, price=100, qty=1, type_="LIMIT", symbol="BTC-USDT"):
    return {"order_id": oid, "user_id": user, "symbol": symbol, "side": side, "type": type_,
            "price": price if type_ == "LIMIT" else None, "qty": qty}


async def ready_engine(make_engine, **kw):
    e = await make_engine(**kw)
    await wait_until(lambda: e.ready)
    return e


def types(fake_a, oid):
    return [n["type"] for n in fake_a.of(oid)]


async def test_trade_between_two_users(make_engine, fake_a):
    e = await ready_engine(make_engine)
    assert e.submit(req("S1", 2077, SELL, price=100, qty=2)) == (202, {"order_id": "S1", "status": "RECEIVED"})
    await wait_until(lambda: "ACCEPTED" in types(fake_a, "S1"))
    e.submit(req("B1", 1024, BUY, price=101, qty=1))
    await wait_until(lambda: len(fake_a.of("B1", "ORDER_UPDATE")) == 1)

    trade = fake_a.of("B1", "TRADE")[0]
    assert trade["price"] == 100 and trade["qty"] == 1
    assert trade["taker"] == {"order_id": "B1", "user_id": 1024, "side": BUY}
    assert trade["maker"] == {"order_id": "S1", "user_id": 2077, "side": SELL}
    assert trade["b_day"] and trade["b_seq"]
    upd = fake_a.of("S1", "ORDER_UPDATE")[-1]
    assert upd["status"] == "PARTIALLY_FILLED" and upd["filled_qty"] == 1 and upd["remaining_qty"] == 1
    seqs = [n["notify_seq"] for n in fake_a.applied]
    assert seqs == list(range(1, len(seqs) + 1))                     # contiguous, in order


async def test_self_trade_and_wash_trade_are_rejected(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    e.submit(req("S1", 1024, SELL, price=100))
    await wait_until(lambda: "ACCEPTED" in types(fake_a, "S1"))
    e.submit(req("B1", 1024, BUY, price=100))       # own order
    e.submit(req("B2", 1025, BUY, price=105))       # 1025 is linked to 1024
    e.submit(req("B3", 1024, BUY, price=99))        # does not cross
    await wait_until(lambda: fake_a.of("B2") and "ACCEPTED" in types(fake_a, "B3"))
    assert fake_a.of("B1")[0]["reason"] == "SELF_TRADE"
    assert fake_a.of("B2")[0]["reason"] == "WASH_TRADE"
    assert len([c for c in fake_b.commands if c["type"] == "new_order"]) == 2   # rejected orders never reach B


async def test_in_flight_orders_count(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    fake_b.drop_new_orders = 1                      # the sell never gets an answer: it stays in flight
    e.submit(req("S1", 1024, SELL, price=100))
    await wait_until(lambda: e.store.one("SELECT state FROM orders WHERE order_id='S1'")["state"] == "IN_FLIGHT")
    e.submit(req("B1", 1024, BUY, price=100))
    await wait_until(lambda: fake_a.of("B1"))
    assert fake_a.of("B1")[0]["reason"] == "SELF_TRADE"


async def test_idempotency_and_conflict(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    r = req("O1", 7, BUY)
    e.submit(r)
    await wait_until(lambda: "ACCEPTED" in types(fake_a, "O1"))
    assert e.submit(r) == (202, {"order_id": "O1", "status": "LIVE"})
    assert e.submit({**r, "qty": 2})[0] == 409
    assert len([c for c in fake_b.commands if c["type"] == "new_order"]) == 1


async def test_validation(make_engine):
    e = await ready_engine(make_engine)
    assert e.submit(req("X", 1, BUY, symbol="DOGE-USDT"))[0] == 400
    assert e.submit(req("X", 1, BUY, qty=0))[0] == 400
    assert e.submit({**req("X", 1, BUY, type_="MARKET"), "price": 5})[0] == 400


async def test_not_ready_refuses_without_a_record(tmp_path, fake_a):
    from d.config import load_static
    from d.engine import Engine
    from tests.conftest import make_settings
    s = make_settings(tmp_path, "ws://127.0.0.1:1/d")       # nothing listens there
    e = Engine(s, load_static(s), a_transport=fake_a.transport())
    await e.start()
    try:
        assert e.submit(req("O1", 7, BUY)) == (503, {"error": "RISK_UNAVAILABLE"})
        assert e.store.one("SELECT * FROM orders WHERE order_id='O1'") is None
    finally:
        await e.stop()


async def test_cancel_live_order(make_engine, fake_a):
    e = await ready_engine(make_engine)
    e.submit(req("O1", 7, BUY))
    await wait_until(lambda: "ACCEPTED" in types(fake_a, "O1"))
    status, body = e.cancel({"user_id": 7, "order_id": "O1"})
    assert status == 202 and body["action"] == "SENT"
    await wait_until(lambda: "CANCEL_RESULT" in types(fake_a, "O1"))
    assert types(fake_a, "O1") == ["ACCEPTED", "ORDER_UPDATE", "CANCEL_RESULT"]
    assert fake_a.of("O1", "ORDER_UPDATE")[0]["status"] == "CANCELLED"
    assert fake_a.of("O1", "CANCEL_RESULT")[0]["result"] == "CANCELLED"
    assert e.cancel({"user_id": 7, "order_id": "O1"})[1]["action"] == "SENT"    # repeated cancel: same answer


async def test_cancel_before_order_leaves_a_tombstone(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    assert e.cancel({"user_id": 7, "order_id": "O1"})[1]["action"] == "TOMBSTONED"
    assert e.submit(req("O1", 7, BUY))[0] == 202
    await wait_until(lambda: "RISK_REJECTED" in types(fake_a, "O1"))
    assert fake_a.of("O1", "CANCEL_RESULT")[0]["result"] == "TOMBSTONED"
    assert fake_a.of("O1", "RISK_REJECTED")[0]["reason"] == "CANCELLED_BEFORE_SUBMIT"
    assert not [c for c in fake_b.commands if c["type"] == "new_order"]


async def test_lost_order_is_cancelled_after_the_age_limit(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    fake_b.drop_new_orders = 1
    e.submit(req("O1", 7, BUY))
    await wait_until(lambda: fake_a.of("O1", "ORDER_UPDATE"), timeout=4)
    upd = fake_a.of("O1", "ORDER_UPDATE")[0]
    assert upd["status"] == "REJECTED" and upd["reason"] == "NOT_DELIVERED"
    cancel = [c for c in fake_b.commands if c["type"] == "cancel_order"][0]
    assert cancel["order_id"] == 1
    sent_at = [c for c in fake_b.commands if c["type"] == "new_order"][0]["sent_at"]
    assert e.store.one("SELECT sent_at FROM cancels WHERE order_id='O1'")["sent_at"] >= sent_at + 1100


async def test_a_cancel_of_a_lost_order(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    fake_b.drop_new_orders = 1
    e.submit(req("O1", 7, BUY))
    await wait_until(lambda: e.store.one("SELECT state FROM orders WHERE order_id='O1'")["state"] == "IN_FLIGHT")
    assert e.cancel({"user_id": 7, "order_id": "O1"})[1]["action"] == "SCHEDULED"
    await wait_until(lambda: fake_a.of("O1", "CANCEL_RESULT"), timeout=4)
    assert fake_a.of("O1", "CANCEL_RESULT")[0]["result"] == "ORDER_NOT_FOUND"


async def test_reply_lost_but_order_in_b(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    e.faults.disconnect_after_send = True
    e.submit(req("O1", 7, BUY))
    await wait_until(lambda: "ACCEPTED" in types(fake_a, "O1"), timeout=4)
    await wait_until(lambda: e.ready)
    await __import__("asyncio").sleep(1.5)          # past the timeout: no automatic cancel was sent
    assert not [c for c in fake_b.commands if c["type"] == "cancel_order"]
    assert e.store.one("SELECT state FROM orders WHERE order_id='O1'")["state"] == "LIVE"


async def test_gap_in_notifications_is_recovered(make_engine, fake_a):
    e = await ready_engine(make_engine)
    e.faults.skip_next_notification = True
    e.submit(req("O1", 7, BUY))
    e.submit(req("O2", 8, SELL, price=200))
    await wait_until(lambda: len(fake_a.applied) >= 2)
    seqs = [n["notify_seq"] for n in fake_a.applied]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_a_down_then_back(make_engine, fake_a):
    e = await ready_engine(make_engine)
    fake_a.down = True
    e.submit(req("O1", 7, BUY))
    await wait_until(lambda: e.notifier.backlog() >= 1)
    fake_a.down = False
    await wait_until(lambda: fake_a.of("O1"), timeout=8)


async def test_restart_rebuilds_book_and_sends_no_duplicates(make_engine, fake_a):
    e = await ready_engine(make_engine)
    e.submit(req("S1", 1024, SELL, price=100))
    await wait_until(lambda: "ACCEPTED" in types(fake_a, "S1"))
    sent = len(fake_a.applied)
    await e.stop()

    e2 = await ready_engine(make_engine)            # same data dir, resume from 0
    assert len(fake_a.applied) == sent              # replayed outputs made no new notifications
    e2.submit(req("B1", 1025, BUY, price=100))      # the rebuilt book still has 1024's sell
    await wait_until(lambda: fake_a.of("B1"))
    assert fake_a.of("B1")[0]["reason"] == "WASH_TRADE"
    assert fake_a.applied[-1]["notify_seq"] == sent + 1


async def test_http_api(make_engine, fake_a):
    e = await ready_engine(make_engine)
    app = create_app(e, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://d") as c:
        r = await c.post("/v1/orders", json=req("O1", 7, BUY))
        assert r.status_code == 202
        assert (await c.post("/v1/orders", json={"order_id": "x"})).status_code == 400
        await wait_until(lambda: "ACCEPTED" in types(fake_a, "O1"))
        q = (await c.get("/v1/orders/O1", params={"user_id": 7})).json()
        assert q["state"] == "LIVE" and q["decision"] == "APPROVED" and q["last_notify_seq"] == 1
        assert (await c.get("/v1/orders/nope", params={"user_id": 7})).status_code == 404
        r = await c.post("/v1/modifies", json={"user_id": 7, "order_id": "O1", "qty": 2})
        assert r.status_code == 202 and r.json()["action"] == "CANCELLING"
        await wait_until(lambda: fake_a.of("O1", "MODIFY_RESULT"))
        assert (await c.get("/healthz")).json()["ready"] is True
        assert b"lame_d_decisions_total" in (await c.get("/metrics")).content
        assert (await c.post("/admin/faults", json={"decision_delay_ms": 5})).json()["decision_delay_ms"] == 5
        n = len(fake_a.applied)
        await c.post("/v1/notifications/rewind", json={"from_seq": 1})
        await __import__("asyncio").sleep(0.2)
        assert len(fake_a.applied) == n             # A treats the resent ones as duplicates
