"""Edge cases: B's output chain, end of day, B's rejections, D's own limits, restarts."""
import asyncio

from d.bclient import BClient
from d.faults import Faults
from tests.conftest import make_settings
from tests.fakes import wait_until
from tests.test_flow import BUY, SELL, ready_engine, req, types


# ---------------------------------------------------------------- B's output chain
def chain_client(tmp_path):
    applied = []
    c = BClient(make_settings(tmp_path, "ws://unused"), applied.append, lambda: None, Faults())
    return c, applied


def rec(day, seq, prev):
    return {"type": "order_event", "day": day, "seq": seq, "prev_seq": prev}


def test_interleaved_and_repeated_outputs_are_applied_once_in_chain_order(tmp_path):
    c, applied = chain_client(tmp_path)
    for r in [rec("d1", 5, 3), rec("d1", 3, 1), rec("d1", 5, 3), rec("d1", 1, 0), rec("d1", 8, 5), rec("d1", 3, 1)]:
        c._receive(r)
        c._drain()
    assert [r["seq"] for r in applied] == [1, 3, 5, 8]
    assert not c.pending


def test_a_gap_holds_later_outputs_back(tmp_path):
    c, applied = chain_client(tmp_path)
    for r in [rec("d1", 1, 0), rec("d1", 6, 4)]:      # seq 4 is missing
        c._receive(r)
        c._drain()
    assert [r["seq"] for r in applied] == [1]
    assert 4 in c.pending and c.pending_since is not None


def test_new_trading_day_restarts_the_chain(tmp_path):
    c, applied = chain_client(tmp_path)
    for r in [rec("d1", 1, 0), rec("d1", 2, 1), rec("d2", 1, 0), rec("d1", 3, 2), rec("d2", 2, 1)]:
        c._receive(r)
        c._drain()
    assert [(r["day"], r["seq"]) for r in applied] == [("d1", 1), ("d1", 2), ("d2", 1), ("d2", 2)]


def test_paused_apply_is_not_caught_up(tmp_path):
    c, applied = chain_client(tmp_path)
    c.faults.pause_b_apply = True
    c._receive(rec("d1", 1, 0))
    c._drain()
    c.last_arrival = 0
    c._check_caught_up()
    assert applied == [] and not c.caught_up


# ---------------------------------------------------------------- B's end of day and rejections
async def test_end_of_day_cancels_are_forwarded(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    e.submit(req("O1", 7, BUY))
    await wait_until(lambda: fake_a.of("O1", "ACCEPTED"))
    await fake_b.close_day()
    await wait_until(lambda: fake_a.of("O1", "ORDER_UPDATE"))
    upd = fake_a.of("O1", "ORDER_UPDATE")[0]
    assert (upd["status"], upd["reason"], upd["remaining_qty"]) == ("CANCELLED", "END_OF_DAY", 0)
    assert e.view.resting["BTC-USDT"] == {}


async def test_expired_at_b_becomes_status_expired(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    fake_b.age = -1                                  # every new order is too old for B
    e.submit(req("O1", 7, BUY))
    await wait_until(lambda: fake_a.of("O1", "ORDER_UPDATE"))
    upd = fake_a.of("O1", "ORDER_UPDATE")[0]
    assert (upd["status"], upd["reason"]) == ("EXPIRED", "EXPIRED")


async def test_self_trade_prevented_by_b_is_a_cancel(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine)
    e.submit(req("O1", 7, BUY))
    await wait_until(lambda: fake_a.of("O1", "ACCEPTED"))
    b = e._current_b("O1")
    await fake_b._event({"symbol": "BTC-USDT", "order_seq": b["order_seq"], "order_id": b["b_order_id"],
                         "user_id": 7, "filled": 0, "remaining": 0}, "CANCELLED", "SELF_TRADE_PREVENTED")
    await wait_until(lambda: fake_a.of("O1", "ORDER_UPDATE"))
    assert fake_a.of("O1", "ORDER_UPDATE")[0]["reason"] == "SELF_TRADE_PREVENTED"


async def test_cancel_after_fill_is_already_final(make_engine, fake_a):
    e = await ready_engine(make_engine)
    e.submit(req("S1", 7, SELL))
    await wait_until(lambda: fake_a.of("S1", "ACCEPTED"))
    e.submit(req("B1", 8, BUY))
    await wait_until(lambda: fake_a.of("S1", "ORDER_UPDATE"))
    assert e.cancel({"user_id": 7, "order_id": "S1"})[1]["action"] == "ALREADY_FINAL"
    await wait_until(lambda: fake_a.of("S1", "CANCEL_RESULT"))
    assert fake_a.of("S1", "CANCEL_RESULT")[0]["result"] == "ALREADY_FINAL"


# ---------------------------------------------------------------- D's own limits
async def test_cancel_while_waiting_for_a_decision(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine, gate_deadline_ms=2000)
    e.faults.decision_delay_ms = 300
    e.submit(req("O1", 7, BUY))
    assert e.cancel({"user_id": 7, "order_id": "O1"})[1]["action"] == "NOT_SENT_TO_B"
    await wait_until(lambda: fake_a.of("O1", "CANCEL_RESULT"))
    assert types(fake_a, "O1") == ["RISK_REJECTED", "CANCEL_RESULT"]
    assert fake_a.of("O1", "RISK_REJECTED")[0]["reason"] == "CANCELLED_BEFORE_SUBMIT"
    await asyncio.sleep(0.5)
    assert not [c for c in fake_b.commands if c["type"] == "new_order"]


async def test_slow_decision_is_risk_timeout(make_engine, fake_a, fake_b):
    e = await ready_engine(make_engine, gate_deadline_ms=100)
    e.faults.decision_delay_ms = 300
    e.submit(req("O1", 7, BUY))
    await wait_until(lambda: fake_a.of("O1"))
    assert fake_a.of("O1")[0]["reason"] == "RISK_TIMEOUT"
    assert not [c for c in fake_b.commands if c["type"] == "new_order"]


async def test_full_queue_is_risk_busy(make_engine):
    e = await ready_engine(make_engine, decision_queue_cap=1)
    e.faults.decision_delay_ms = 300
    e.submit(req("O1", 7, BUY))
    e.submit(req("O2", 7, BUY, price=99))
    assert e.submit(req("O3", 7, BUY, price=98)) == (503, {"error": "RISK_BUSY"})
    assert e.store.one("SELECT * FROM orders WHERE order_id='O3'") is None


async def test_orders_queued_before_a_crash_are_decided_after_restart(make_engine, fake_a):
    e = await ready_engine(make_engine, gate_deadline_ms=100)
    e.faults.decision_delay_ms = 5000
    e.submit(req("O1", 7, BUY))
    await e.stop()                                   # stopped before the decision
    e2 = await ready_engine(make_engine, gate_deadline_ms=100)
    await wait_until(lambda: fake_a.of("O1"))
    assert fake_a.of("O1")[0]["reason"] == "RISK_TIMEOUT"   # too late now: never sent to B
    assert e2.query("O1", 7)[1]["state"] == "REJECTED_BY_D"
