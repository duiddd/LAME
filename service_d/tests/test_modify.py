"""Modify (RD27): cancel + new order inside D under the same order_id, with A's rules (A document 5.4)."""
from d.replay import replay
from tests.fakes import wait_until
from tests.test_flow import BUY, SELL, ready_engine, req, types


def mod(oid, user, qty, price=None):
    return {"order_id": oid, "user_id": user, "qty": qty, "price": price}


def b_cmds(fake_b, kind):
    return [c for c in fake_b.commands if c["type"] == kind]


async def resting(e, fake_a, r):
    e.submit(r)
    await wait_until(lambda: fake_a.of(r["order_id"], "ACCEPTED"))


async def test_modify_price_and_qty(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    await resting(e, fake_a, req("S1", 2077, SELL, price=105, qty=5))
    status, body = e.modify(mod("S1", 2077, qty=3, price=102))
    assert status == 202 and body["action"] == "CANCELLING"
    await wait_until(lambda: fake_a.of("S1", "MODIFY_RESULT"))
    res = fake_a.of("S1", "MODIFY_RESULT")[0]
    assert (res["result"], res["price"], res["qty"]) == ("APPLIED", 102, 3)
    assert types(fake_a, "S1") == ["ACCEPTED", "MODIFY_RESULT"]        # no ORDER_UPDATE for the old one
    new = b_cmds(fake_b, "new_order")[-1]
    assert (new["price"], new["qty"]) == (102, 3) and new["order_id"] != b_cmds(fake_b, "new_order")[0]["order_id"]
    assert e.query("S1", 2077)[1]["state"] == "LIVE"

    e.submit(req("B1", 1024, BUY, price=102, qty=3))                   # trades on the new b order
    await wait_until(lambda: fake_a.of("S1", "ORDER_UPDATE"))
    assert fake_a.of("B1", "TRADE")[0]["maker"]["order_id"] == "S1"
    upd = fake_a.of("S1", "ORDER_UPDATE")[-1]
    assert (upd["status"], upd["filled_qty"]) == ("FILLED", 3)


async def test_fill_during_the_cancel_reduces_the_new_qty(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    await resting(e, fake_a, req("S1", 2077, SELL, price=100, qty=5))
    fake_b.hold_cancels = True
    e.modify(mod("S1", 2077, qty=4))                                   # remaining 4, same price
    await wait_until(lambda: fake_b.held)
    e.submit(req("B1", 1024, BUY, price=100, qty=2))                   # fills 2 of the old order meanwhile
    await wait_until(lambda: fake_a.of("S1", "ORDER_UPDATE"))
    await fake_b.release_cancels()
    await wait_until(lambda: fake_a.of("S1", "MODIFY_RESULT"))
    res = fake_a.of("S1", "MODIFY_RESULT")[0]
    assert (res["result"], res["qty"], res["price"]) == ("APPLIED", 2, 100)   # 4 - 2 filled meanwhile
    assert b_cmds(fake_b, "new_order")[-1]["qty"] == 2
    assert types(fake_a, "S1") == ["ACCEPTED", "TRADE", "ORDER_UPDATE", "MODIFY_RESULT"]


async def test_old_order_filled_during_the_cancel(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    await resting(e, fake_a, req("S1", 2077, SELL, price=100, qty=2))
    fake_b.hold_cancels = True
    e.modify(mod("S1", 2077, qty=2, price=101))
    await wait_until(lambda: fake_b.held)
    e.submit(req("B1", 1024, BUY, price=100, qty=2))
    await wait_until(lambda: fake_a.of("S1", "MODIFY_RESULT"))
    await fake_b.release_cancels()
    res = fake_a.of("S1", "MODIFY_RESULT")[0]
    assert (res["result"], res["reason"]) == ("REJECTED", "ALREADY_FINAL")
    assert fake_a.of("S1", "ORDER_UPDATE")[-1]["status"] == "FILLED"
    assert len(b_cmds(fake_b, "new_order")) == 2                        # no replacement was sent


async def test_modify_of_a_final_order(make_engine, fake_a):
    e = await ready_engine(make_engine)
    await resting(e, fake_a, req("S1", 2077, SELL, price=100))
    e.cancel({"user_id": 2077, "order_id": "S1"})
    await wait_until(lambda: fake_a.of("S1", "CANCEL_RESULT"))
    assert e.modify(mod("S1", 2077, qty=1))[0] == 202
    await wait_until(lambda: fake_a.of("S1", "MODIFY_RESULT"))
    assert fake_a.of("S1", "MODIFY_RESULT")[0]["reason"] == "ALREADY_FINAL"


async def test_replacement_refused_by_the_rules(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    await resting(e, fake_a, req("S1", 1024, SELL, price=100))
    await resting(e, fake_a, req("B1", 1025, BUY, price=90))           # 1025 is linked to 1024
    e.modify(mod("B1", 1025, qty=1, price=100))                        # would now cross 1024's sell
    await wait_until(lambda: fake_a.of("B1", "MODIFY_RESULT"))
    assert types(fake_a, "B1") == ["ACCEPTED", "ORDER_UPDATE", "MODIFY_RESULT"]
    upd, res = fake_a.of("B1", "ORDER_UPDATE")[0], fake_a.of("B1", "MODIFY_RESULT")[0]
    assert (upd["status"], upd["reason"]) == ("CANCELLED", "WASH_TRADE")
    assert (res["result"], res["reason"]) == ("REJECTED", "WASH_TRADE")
    assert len(b_cmds(fake_b, "new_order")) == 2
    assert e.query("B1", 1025)[1]["state"] == "CANCELLED"


async def test_replacement_never_reaches_b(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    await resting(e, fake_a, req("S1", 2077, SELL, price=100, qty=3))
    fake_b.drop_new_orders = 1                                         # the replacement is lost
    e.modify(mod("S1", 2077, qty=3, price=101))
    await wait_until(lambda: fake_a.of("S1", "MODIFY_RESULT"), timeout=6)
    upd, res = fake_a.of("S1", "ORDER_UPDATE")[0], fake_a.of("S1", "MODIFY_RESULT")[0]
    assert (upd["status"], upd["reason"]) == ("REJECTED", "NOT_DELIVERED")
    assert (res["result"], res["reason"]) == ("REJECTED", "NOT_DELIVERED")


async def test_resent_and_conflicting_modifies(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    await resting(e, fake_a, req("S1", 2077, SELL, price=100, qty=3))
    fake_b.hold_cancels = True
    assert e.modify(mod("S1", 2077, qty=2))[0] == 202
    assert e.modify(mod("S1", 2077, qty=2))[0] == 202                  # the same one again
    assert e.modify(mod("S1", 2077, qty=1)) == (409, {"error": "MODIFY_IN_PROGRESS"})
    assert e.modify(mod("S1", 1, qty=1))[0] == 404
    await fake_b.release_cancels()
    await wait_until(lambda: fake_a.of("S1", "MODIFY_RESULT"))
    assert len(fake_a.of("S1", "MODIFY_RESULT")) == 1


async def test_cancel_during_a_modify(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    await resting(e, fake_a, req("S1", 2077, SELL, price=100, qty=3))
    fake_b.hold_cancels = True
    e.modify(mod("S1", 2077, qty=2))
    await wait_until(lambda: fake_b.held)
    e.cancel({"user_id": 2077, "order_id": "S1"})
    await fake_b.release_cancels()
    await wait_until(lambda: fake_a.of("S1", "CANCEL_RESULT"))
    assert types(fake_a, "S1") == ["ACCEPTED", "MODIFY_RESULT", "ORDER_UPDATE", "CANCEL_RESULT"]
    assert fake_a.of("S1", "MODIFY_RESULT")[0]["reason"] == "CANCELLED_BY_USER"
    assert fake_a.of("S1", "CANCEL_RESULT")[0]["result"] == "CANCELLED"
    assert len(b_cmds(fake_b, "new_order")) == 1


async def test_market_orders_cannot_be_modified(make_engine, fake_a):
    e = await ready_engine(make_engine)
    e.submit(req("M1", 7, BUY, type_="MARKET"))
    await wait_until(lambda: fake_a.of("M1"))
    assert e.modify(mod("M1", 7, qty=1))[0] == 400
    assert e.modify(mod("nope", 7, qty=1))[0] == 404


async def test_replay_covers_modify_decisions(make_engine, fake_a, fake_b, tmp_path):
    e = await ready_engine(make_engine)
    await resting(e, fake_a, req("S1", 1024, SELL, price=100))
    await resting(e, fake_a, req("B1", 1025, BUY, price=90))
    await resting(e, fake_a, req("B2", 2077, BUY, price=80))
    e.modify(mod("B1", 1025, qty=1, price=100))                        # refused: WASH_TRADE
    e.modify(mod("B2", 2077, qty=1, price=85))                         # approved
    await wait_until(lambda: fake_a.of("B1", "MODIFY_RESULT") and fake_a.of("B2", "MODIFY_RESULT"))
    report = replay(str(tmp_path / "d.db"), list(fake_b.outputs), e.cfg)
    assert report["checked"] == 5 and report["mismatches"] == []
