"""Acceptance criterion 1: replaying the same orders gives the same decisions."""
import json

from d.replay import load_b_outputs, replay
from tests.fakes import wait_until
from tests.test_flow import BUY, SELL, ready_engine, req


async def run_mixed_session(e, fake_a, fake_b):
    """Approvals, self-trade, wash trade, in-flight rejections, trades and a market order."""
    e.submit(req("S1", 1024, SELL, price=100, qty=2))
    e.submit(req("S2", 2077, SELL, price=101, qty=1))
    await wait_until(lambda: len(fake_a.of("S2", "ACCEPTED")) == 1)
    e.submit(req("B1", 1024, BUY, price=100))          # SELF_TRADE
    e.submit(req("B2", 1025, BUY, price=105))          # WASH_TRADE
    e.submit(req("B3", 3001, BUY, price=100))          # trades with S1
    e.submit(req("M1", 2077, BUY, type_="MARKET", qty=1))   # reaches S1 first: allowed
    await wait_until(lambda: fake_a.of("M1", "ORDER_UPDATE"))
    fake_b.drop_new_orders = 1
    e.submit(req("S3", 4000, SELL, price=90))          # lost: stays in flight for a while
    await wait_until(lambda: e.store.one("SELECT state FROM orders WHERE order_id='S3'")["state"] == "IN_FLIGHT")
    e.submit(req("B4", 4000, BUY, price=95))           # SELF_TRADE against the in-flight S3
    await wait_until(lambda: fake_a.of("B4"))
    await wait_until(lambda: fake_a.of("S3", "ORDER_UPDATE"), timeout=4)


async def test_replay_matches_every_rule_decision(make_engine, fake_a, fake_b, tmp_path):
    e = await ready_engine(make_engine)
    await run_mixed_session(e, fake_a, fake_b)
    reasons = {r["order_id"]: r["reason"] for r in e.store.all("SELECT order_id, reason FROM decisions")}
    assert reasons["B1"] == "SELF_TRADE" and reasons["B2"] == "WASH_TRADE" and reasons["B4"] == "SELF_TRADE"
    assert reasons["M1"] is None

    out = tmp_path / "b_outputs.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in fake_b.outputs))
    report = replay(str(tmp_path / "d.db"), load_b_outputs([out]), e.cfg)
    assert report["mismatches"] == []
    assert report["checked"] == len(reasons) and report["matched"] == report["checked"]


async def test_replay_reports_a_changed_decision(make_engine, fake_a, fake_b, tmp_path):
    e = await ready_engine(make_engine)
    await run_mixed_session(e, fake_a, fake_b)
    e.store.q("UPDATE decisions SET reason = NULL, decision = 'APPROVED' WHERE order_id = 'B2'")
    out = tmp_path / "b_outputs.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in fake_b.outputs))
    report = replay(str(tmp_path / "d.db"), load_b_outputs([out]), e.cfg)
    assert [m["order_id"] for m in report["mismatches"]] == ["B2"]
    assert report["mismatches"][0]["replayed"] == "WASH_TRADE"


def test_reads_b_event_store_format(tmp_path):
    import sqlite3
    p = tmp_path / "events-2026-11-05.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY, kind TEXT, symbol TEXT, ts INTEGER, prev_seq INTEGER, "
               "payload TEXT)")
    db.execute("INSERT INTO events VALUES(1, 'NewOrder', 'BTC-USDT', 0, NULL, '{}')")
    db.execute("INSERT INTO events VALUES(2, 'OrderEvent', 'BTC-USDT', 0, 0, ?)",
               (json.dumps({"symbol": "BTC-USDT", "order_seq": 1, "order_id": 1, "user_id": 7,
                            "status": "ACCEPTED", "filled": 0, "remaining": 1, "reason": None}),))
    db.commit()
    [rec] = load_b_outputs([p])
    assert rec["type"] == "order_event" and rec["day"] == "2026-11-05" and rec["seq"] == 2 and rec["prev_seq"] == 0
