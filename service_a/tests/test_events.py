"""Applying D's notifications: funds, order status, and the delivery contract.

| Test | Purpose | Method |
| --- | --- | --- |
| test_every_transition_has_a_verdict | The table is complete and the rules hold | Enumerate status x notification against the expected verdicts |
| test_partial_fill_then_cancel | The worked example of hold, settle and release | Buy 1 at 50000, fill 0.3 at 49900, cancel -> 85030 USDT, 0.3 BTC |
| test_cancel_before_trade | The other arrival order gives the same result | CANCELLED then TRADE -> same balances; closes only at the end |
| test_self_trade_after_partial_fill | One release rule | REJECTED SELF_TRADE_PREVENTED with filled > 0 releases the unfilled part |
| test_settlement_moves_between_users | Trades conserve funds | Both sides settle in one notification; totals unchanged |
| test_market_buy_cancels_remainder | Section 7.3 | ACCEPTED -> cancel wanted; fill at the ask; remainder released |
| test_notification_before_the_ack | The ack cannot undo a later state | ACCEPTED, then mark_sent -> stays PROCESSING |
| test_late_risk_rejected_after_tombstone | D's 8.5 is legal, not a poison | TOMBSTONED, then RISK_REJECTED -> 200, released once |
| test_cancel_results_on_terminal_orders | Late results have no effect | Any CANCEL_RESULT on a terminal order -> 200, no fund movement |
| test_not_delivered_then_not_found | One outcome, two notifications | Releases once |
| test_duplicate_is_ignored | At-least-once delivery | Same notify_seq twice -> one effect |
| test_gap_answers_expected_seq_at_top_level | D reads this key directly | seq + 2 -> 409 {"expected_seq": N} |
| test_concurrent_copies_apply_once | The cursor lock serialises resends | 10 copies at once -> one effect |
| test_crash_before_answer | Effectively once | Commit, resend -> duplicate; balances unchanged |
| test_illegal_transition_stops_the_line | Wrong books are worse than a pause | Poison -> 422, nothing applied, cursor unmoved, later ones wait |
| test_unknown_order_stops_the_line | Bots go through A, so this is a bug | Unknown order -> 422 |
| test_trade_beyond_filled_stops_the_line | A terminal order takes only what it reported | settled + qty > filled -> 422 |
| test_trade_above_hold_price_stops_the_line | Overdraw cannot happen silently | Price above the hold -> 422, no money moved |
| test_malformed_is_refused | Schema check first | No notify_seq -> 422 |
"""
import asyncio

import pytest
from sqlalchemy import text

from app.orders.events.transitions import ALL_STATUSES, BEFORE_OUTCOME, LIVE, TERMINAL, Verdict, judge
from tests.fakes import Notifier
from tests.helpers import balance, funded_user, ledger_matches_accounts, open_market, order_row, place

KINDS = ["RISK_REJECTED", "ACCEPTED", "TRADE", "ORDER_UPDATE:FILLED", "CANCEL_RESULT:CANCELLED", "CANCEL_RESULT:TOMBSTONED", "MODIFY_RESULT:APPLIED"]


@pytest.fixture
def market(container):
    open_market(container)


@pytest.mark.parametrize("status", sorted(ALL_STATUSES))
@pytest.mark.parametrize("kind", KINDS)
def test_every_transition_has_a_verdict(status, kind):
    # Purpose: section 5.6 written out, so that a change in the rules fails here.
    terminal, early = status in TERMINAL, status in BEFORE_OUTCOME
    expected = {
        "RISK_REJECTED": Verdict.IGNORE if terminal else (Verdict.APPLY if early else Verdict.ILLEGAL),
        "ACCEPTED": Verdict.ILLEGAL if terminal else Verdict.APPLY,
        "TRADE": Verdict.APPLY,
        "ORDER_UPDATE:FILLED": Verdict.ILLEGAL if terminal else Verdict.APPLY,
        "CANCEL_RESULT:CANCELLED": Verdict.IGNORE if terminal else Verdict.APPLY,
        "CANCEL_RESULT:TOMBSTONED": Verdict.IGNORE if terminal else Verdict.APPLY,
        "MODIFY_RESULT:APPLIED": Verdict.ILLEGAL if early else Verdict.APPLY,
    }[kind]
    assert judge(status, kind) is expected
    assert LIVE | TERMINAL | BEFORE_OUTCOME == ALL_STATUSES


async def buy(client, qty="1.000", key="k1"):
    return (await place(client, key=key, qty=qty)).json()["order_id"]


async def resting_sell(client, container, buyer_cookies, price="49900.00", qty="1.000"):
    """A resting sell placed as another user; the client returns to the buyer."""
    await funded_user(client, "sam_seller")
    sell = (await place(client, key="s1", side="SELL", price=price, qty=qty)).json()["order_id"]
    row = await order_row(container, sell)
    client.cookies.clear()
    client.cookies.update(buyer_cookies)
    return sell, row.user_id


async def matched(client, container, **sell_kw):
    """A buyer with an accepted buy order, and an accepted sell from another user."""
    user = await funded_user(client)
    cookies = dict(client.cookies)
    order_id = await buy(client)
    sell, seller_id = await resting_sell(client, container, cookies, **sell_kw)
    n = Notifier(client)
    await n.accepted(order_id)
    await n.accepted(sell, order_seq=8)
    return n, order_id, user["user_id"], sell, seller_id


async def test_partial_fill_then_cancel(client, container, market):
    # Purpose: the worked example: everything held returns except what the fill cost.
    n, order_id, buyer, sell, seller = await matched(client, container)
    await n.trade((order_id, buyer, "BUY"), (sell, seller, "SELL"), price=4990000, qty=300)
    await n.update(order_id, "PARTIALLY_FILLED", 300, 700)
    b = await balance(client, "USDT")
    assert b["held"] == "35000.00000" and b["available"] == "50030.00000"
    assert (await balance(client, "BTC"))["available"] == "10.300"
    await n.update(order_id, "CANCELLED", 300)
    await n.cancel_result(order_id, "CANCELLED")
    b = await balance(client, "USDT")
    assert b["held"] == "0.00000" and b["available"] == "85030.00000"
    row = await order_row(container, order_id)
    assert row.status == "CANCELLED" and row.closed_at is not None
    detail = (await client.get(f"/api/orders/{order_id}")).json()
    assert detail["avg_fill_price"] == "49900.00" and len(detail["fills"]) == 1
    assert (await client.get("/api/fills")).json()["fills"][0]["role"] == "TAKER"
    await ledger_matches_accounts(container)


async def test_cancel_before_trade(client, container, market):
    # Purpose: if the terminal update arrives first, the filled part's hold waits for its trade.
    n, order_id, buyer, sell, seller = await matched(client, container)
    await n.update(order_id, "CANCELLED", 300)
    assert (await balance(client, "USDT"))["held"] == "15000.00000"
    assert (await order_row(container, order_id)).closed_at is None
    await n.trade((order_id, buyer, "BUY"), (sell, seller, "SELL"), price=4990000, qty=300)
    b = await balance(client, "USDT")
    assert b["held"] == "0.00000" and b["available"] == "85030.00000"
    assert (await order_row(container, order_id)).closed_at is not None


async def test_self_trade_after_partial_fill(client, container, market):
    # Purpose: REJECTED can follow a fill; only the unfilled part is released.
    n, order_id, buyer, sell, seller = await matched(client, container)
    await n.trade((order_id, buyer, "BUY"), (sell, seller, "SELL"), price=5000000, qty=400)
    await n.update(order_id, "PARTIALLY_FILLED", 400, 600)
    await n.update(order_id, "REJECTED", 400, 600, reason="SELF_TRADE_PREVENTED")
    b = await balance(client, "USDT")
    assert b["held"] == "0.00000" and b["available"] == "80000.00000"
    assert (await balance(client, "BTC"))["available"] == "10.400"
    assert (await order_row(container, order_id)).closed_at is not None


async def test_settlement_moves_between_users(client, container, market):
    # Purpose: spot trades only move assets between users.
    n, order_id, buyer, sell, seller = await matched(client, container)
    await n.trade((order_id, buyer, "BUY"), (sell, seller, "SELL"), price=4990000, qty=1000)
    await n.update(order_id, "FILLED", 1000)
    await n.update(sell, "FILLED", 1000)
    async with container.engine.connect() as conn:
        totals = dict((await conn.execute(text("SELECT asset, sum(available + held) FROM accounts GROUP BY asset"))).all())
        held = (await conn.execute(text("SELECT sum(held) FROM accounts"))).scalar()
    assert totals["USDT"] == 200000 * 100000 and totals["BTC"] == 20 * 1000 and held == 0
    await ledger_matches_accounts(container)


async def test_market_buy_cancels_remainder(client, container, market):
    # Purpose: section 7.3: the remainder is cancelled once accepted, and its hold goes back.
    user = await funded_user(client)
    cookies = dict(client.cookies)
    order_id = (await place(client, key="m", type="MARKET", price=None, qty="1.000")).json()["order_id"]
    sell, seller_id = await resting_sell(client, container, cookies, price="50000.00", qty="0.200")
    n = Notifier(client)
    await n.accepted(order_id)
    row = await order_row(container, order_id)
    assert row.cancel_wanted is True and row.cancel_acked is False
    await n.accepted(sell, order_seq=8)
    await n.trade((order_id, user["user_id"], "BUY"), (sell, seller_id, "SELL"), price=5000000, qty=200)
    await n.update(order_id, "CANCELLED", 200)
    b = await balance(client, "USDT")
    assert b["held"] == "0.00000" and b["available"] == "90000.00000"


async def test_notification_before_the_ack(client, container, market):
    # Purpose: D's sender runs independently, so a late ack must not undo a newer status.
    await funded_user(client)
    order_id = await buy(client)
    await Notifier(client).accepted(order_id)
    await container.orders.mark_sent(order_id)
    assert (await order_row(container, order_id)).status == "PROCESSING"


async def test_late_risk_rejected_after_tombstone(client, container, market):
    # Purpose: D's protocol 8.5 is a legal sequence: no second release and no stopped line.
    await funded_user(client)
    order_id = await buy(client)
    n = Notifier(client)
    await n.cancel_result(order_id, "TOMBSTONED")
    assert (await balance(client, "USDT"))["available"] == "100000.00000"
    await n.rejected(order_id, "CANCELLED_BEFORE_SUBMIT")
    assert (await balance(client, "USDT"))["available"] == "100000.00000"
    row = await order_row(container, order_id)
    assert row.status == "CANCELLED" and row.reason == "TOMBSTONED"
    await ledger_matches_accounts(container)


@pytest.mark.parametrize("result", ["CANCELLED", "NOT_SENT_TO_B", "ALREADY_FINAL", "ORDER_NOT_FOUND", "TOMBSTONED"])
async def test_cancel_results_on_terminal_orders(client, container, market, result):
    # Purpose: a late cancel result changes nothing, whatever it says.
    await funded_user(client)
    order_id = await buy(client)
    n = Notifier(client)
    await n.rejected(order_id)
    await n.cancel_result(order_id, result)
    assert (await balance(client, "USDT"))["available"] == "100000.00000"
    assert (await order_row(container, order_id)).status == "REJECTED"


async def test_not_delivered_then_not_found(client, container, market):
    # Purpose: one outcome reported twice releases once.
    await funded_user(client)
    order_id = await buy(client)
    n = Notifier(client)
    await n.update(order_id, "REJECTED", 0, reason="NOT_DELIVERED")
    await n.cancel_result(order_id, "ORDER_NOT_FOUND")
    assert (await balance(client, "USDT"))["available"] == "100000.00000"
    await ledger_matches_accounts(container)


async def test_duplicate_is_ignored(client, container, market):
    # Purpose: the notification layer of idempotency.
    await funded_user(client)
    order_id = await buy(client)
    body = {"notify_seq": 1, "type": "RISK_REJECTED", "order_id": order_id, "reason": "WASH_TRADE"}
    for _ in range(2):
        assert (await client.post("/internal/order-events", json=body)).status_code == 200
    assert (await balance(client, "USDT"))["available"] == "100000.00000"
    await ledger_matches_accounts(container)


async def test_gap_answers_expected_seq_at_top_level(client):
    # Purpose: D's notifier reads r.json()["expected_seq"] with no fallback.
    r = await client.post("/internal/order-events", json={"notify_seq": 3, "type": "ACCEPTED", "order_id": "x"})
    assert r.status_code == 409 and r.json() == {"expected_seq": 1}


async def test_concurrent_copies_apply_once(client, container, market):
    # Purpose: D resends while A is working; the row lock makes the copies take turns.
    await funded_user(client)
    order_id = await buy(client)
    body = {"notify_seq": 1, "type": "RISK_REJECTED", "order_id": order_id, "reason": "WASH_TRADE"}
    replies = await asyncio.gather(*(client.post("/internal/order-events", json=body) for _ in range(10)))
    assert {r.status_code for r in replies} == {200}
    async with container.engine.connect() as conn:
        releases = (await conn.execute(text("SELECT count(*) FROM ledger_entries WHERE reason = 'RELEASE'"))).scalar()
    assert releases == 1
    assert (await balance(client, "USDT"))["available"] == "100000.00000"


async def test_crash_before_answer(client, container, market):
    # Purpose: the cursor commits with the effect, so a resend after a crash is a duplicate.
    await funded_user(client)
    order_id = await buy(client)
    body = {"notify_seq": 1, "type": "RISK_REJECTED", "order_id": order_id, "reason": "WASH_TRADE"}
    assert (await container.events.handle(body)).status == 200
    r = await client.post("/internal/order-events", json=body)
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert (await balance(client, "USDT"))["available"] == "100000.00000"


async def test_illegal_transition_stops_the_line(client, container, market):
    # Purpose: A and D disagree: nothing is applied and later notifications wait.
    await funded_user(client)
    order_id = await buy(client)
    await Notifier(client).rejected(order_id)
    bad = {"notify_seq": 2, "type": "ORDER_UPDATE", "order_id": order_id, "status": "FILLED", "filled_qty": 1000}
    r = await client.post("/internal/order-events", json=bad)
    assert r.status_code == 422 and r.json()["reason"] == "ILLEGAL_TRANSITION"
    assert (await client.post("/internal/order-events", json={**bad, "notify_seq": 3})).status_code == 409
    async with container.engine.connect() as conn:
        assert (await conn.execute(text("SELECT last_notify_seq FROM notify_cursor"))).scalar() == 1


async def test_unknown_order_stops_the_line(client):
    # Purpose: every order passes through A, so an unknown one means the books have diverged.
    r = await client.post("/internal/order-events", json={"notify_seq": 1, "type": "ACCEPTED", "order_id": "nope"})
    assert r.status_code == 422 and r.json()["reason"] == "UNKNOWN_ORDER"


def trade_body(seq, order_id, buyer, sell, seller, price, qty):
    return {"notify_seq": seq, "type": "TRADE", "symbol": "BTC-USDT", "b_day": "d", "b_seq": 999, "price": price, "qty": qty,
            "taker": {"order_id": order_id, "user_id": buyer, "side": "BUY"},
            "maker": {"order_id": sell, "user_id": seller, "side": "SELL"}}


async def test_trade_beyond_filled_stops_the_line(client, container, market):
    # Purpose: a terminal order can only take the trades it reported.
    n, order_id, buyer, sell, seller = await matched(client, container)
    await n.update(order_id, "CANCELLED", 100)
    r = await client.post("/internal/order-events", json=trade_body(n.seq + 1, order_id, buyer, sell, seller, 5000000, 200))
    assert r.status_code == 422


async def test_trade_above_hold_price_stops_the_line(client, container, market):
    # Purpose: this is the overdraw market buys are capped against; it must not move money.
    n, order_id, buyer, sell, seller = await matched(client, container)
    r = await client.post("/internal/order-events", json=trade_body(n.seq + 1, order_id, buyer, sell, seller, 5000001, 100))
    assert r.status_code == 422 and r.json()["reason"] == "BALANCE_VIOLATION"
    assert (await balance(client, "BTC"))["available"] == "10.000"
    async with container.engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM trades"))).scalar() == 0


async def test_malformed_is_refused(client):
    # Purpose: a body without the contract's head never reaches the books.
    assert (await client.post("/internal/order-events", json={"type": "ACCEPTED"})).status_code == 422
