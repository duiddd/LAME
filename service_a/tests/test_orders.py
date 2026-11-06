"""Placing an order: what is checked, what is held, and what the user is told.

| Test | Purpose | Method |
| --- | --- | --- |
| test_place_holds_and_sends | A hold and D's contract | Limit buy -> 202 SENT, price x qty held, wire fields as D's OrderIn |
| test_repeated_key_places_once | A double click cannot place twice | Same key twice -> one order, one hold, one send |
| test_rate_limit | One user cannot flood | 20 orders at once -> 10 pass, then RATE_LIMITED; refills with time |
| test_size_and_grid_refusals | Each violation has its own reason | Bad price, qty, symbol -> reason, nothing sent |
| test_notional_limit | The value limit applies | price x qty over the maximum -> NOTIONAL_TOO_LARGE |
| test_insufficient_balance | Overdraw is refused atomically | Too large -> refused; order rolled back |
| test_stale_market_refuses_both_types | No fresh price, no orders | 3 s silence -> NO_FRESH_PRICE for limit and market |
| test_heartbeat_keeps_market_fresh | A quiet market is not stale | Repeated last_ms keeps accepting |
| test_price_band | Obvious typos are stopped | Beyond 10% -> PRICE_OUT_OF_BAND; exactly 10% passes |
| test_first_order_of_the_day | An empty market can start | No price, fresh feed -> limit accepted, market NO_LIQUIDITY |
| test_market_buy_is_a_capped_limit | A market buy cannot overdraw | Wire LIMIT at ask x 1.05, held at the cap, API shows MARKET |
| test_market_sell_stays_market | A sell cannot overdraw | Wire MARKET without price, base held |
| test_d_unreachable_releases_at_once | Only a certain failure says failed | NOT_TAKEN -> 503, REJECTED D_UNAVAILABLE, hold released, not resent |
| test_invalid_at_d_releases | D's 400 keeps no record | INVALID -> 400, hold released |
| test_no_reply_keeps_hold | An unanswered order may be live | UNKNOWN or CONFLICT -> 202, status UNKNOWN, hold kept |
| test_open_breaker_refuses_before_holding | Fail fast | Open breaker -> 503, nothing written |
| test_only_owner_sees_order | Users are isolated | Another user -> 404 for read and cancel |
| test_cancel_is_sent_and_final_refused | Cancelling | DELETE -> cancel at D; finished order -> 409 |
| test_modify_unavailable_restores | D answers 501 today | 501 -> MODIFY_UNAVAILABLE, status and hold restored |
| test_modify_up_holds_first | No modify without funds | More funds held before sending; MODIFY_RESULT keeps them |
| test_modify_down_releases_only_after_the_result | The old size can still fill | Hold unchanged until MODIFY_RESULT |
| test_modify_result_without_price_keeps_it | D may leave out an unchanged price | price null -> old price stays |
| test_cancel_replace_mode | The alternative that needs no modify from D | Mode on -> old order cancelled, a new order placed |
| test_modify_refused_for_market_and_unknown | A resend must be identical | Market or UNKNOWN -> 409 |
"""
import pytest

from app.orders.dclient.port import Delivery, Reply
from tests.fakes import Notifier
from tests.helpers import balance, funded_user, open_market, order_row, place


@pytest.fixture
def market(container):
    open_market(container)


async def test_place_holds_and_sends(client, container, fake_d, market):
    # Purpose: the hold equals price x qty, and D receives exactly its contract's fields.
    user = await funded_user(client)
    r = await place(client)
    assert r.status_code == 202 and r.json()["status"] == "SENT"
    b = await balance(client, "USDT")
    assert b["held"] == "50000.00000" and b["available"] == "50000.00000"
    assert fake_d.placed == [{"order_id": r.json()["order_id"], "user_id": user["user_id"], "symbol": "BTC-USDT",
                              "side": "BUY", "type": "LIMIT", "price": 5000000, "qty": 1000}]


async def test_repeated_key_places_once(client, fake_d, market):
    # Purpose: the request layer of idempotency.
    await funded_user(client)
    first, second = await place(client), await place(client)
    assert first.json()["order_id"] == second.json()["order_id"]
    assert len(fake_d.placed) == 1
    assert (await balance(client, "USDT"))["held"] == "50000.00000"


async def test_rate_limit(client, container, clock, market):
    # Purpose: a burst of 10 passes, then the rate applies, and it refills over time.
    await funded_user(client, usdt="1000000")
    codes = [(await place(client, key=f"k{i}", qty="0.001")).status_code for i in range(20)]
    assert codes.count(202) == 10 and codes.count(429) == 10
    clock.advance(1000)
    open_market(container)
    assert (await place(client, key="later", qty="0.001")).status_code == 202


@pytest.mark.parametrize("kw,reason", [
    ({"price": "50000.001"}, "INVALID_TICK"),
    ({"price": "-1"}, "INVALID_TICK"),
    ({"price": None}, "INVALID_TICK"),
    ({"qty": "0.0001"}, "INVALID_LOT"),
    ({"qty": "0"}, "QTY_TOO_SMALL"),
    ({"qty": "100.001"}, "QTY_TOO_LARGE"),
    ({"symbol": "DOGE-USDT"}, "INVALID_SYMBOL"),
])
async def test_size_and_grid_refusals(client, fake_d, market, kw, reason):
    # Purpose: each violation names its reason, and nothing is sent.
    await funded_user(client)
    r = await place(client, **kw)
    assert r.status_code == 400 and r.json()["reason"] == reason
    assert fake_d.placed == []


async def test_notional_limit(client, fake_d, market):
    # Purpose: the value limit applies to price x qty.
    await funded_user(client, usdt="1000000")
    r = await place(client, price="50000.00", qty="20.001")
    assert r.status_code == 400 and r.json()["reason"] == "NOTIONAL_TOO_LARGE"


async def test_insufficient_balance(client, fake_d, market):
    # Purpose: the order and the hold commit together, or neither does.
    await funded_user(client, usdt="10")
    r = await place(client)
    assert r.status_code == 400 and r.json()["reason"] == "INSUFFICIENT_BALANCE"
    assert (await client.get("/api/orders")).json()["orders"] == [] and fake_d.placed == []


@pytest.mark.parametrize("kw", [{"type": "LIMIT"}, {"type": "MARKET", "price": None}])
async def test_stale_market_refuses_both_types(client, clock, market, kw):
    # Purpose: with a silent feed, neither order type can pass the price check.
    await funded_user(client)
    clock.advance(3001)
    r = await place(client, **kw)
    assert r.status_code == 400 and r.json()["reason"] == "NO_FRESH_PRICE"


async def test_heartbeat_keeps_market_fresh(client, container, clock, market):
    # Purpose: a heartbeat repeats last_ms, and must still count as fresh.
    await funded_user(client)
    clock.advance(2500)
    open_market(container)
    clock.advance(2500)
    assert (await place(client)).status_code == 202


async def test_price_band(client, market):
    # Purpose: more than 10% from the reference price is refused; exactly 10% passes.
    await funded_user(client, usdt="1000000")
    r = await place(client, price="55000.01", qty="0.001")
    assert r.status_code == 400 and r.json()["reason"] == "PRICE_OUT_OF_BAND"
    assert (await place(client, key="k2", price="55000.00", qty="0.001")).status_code == 202


async def test_first_order_of_the_day(client, container):
    # Purpose: with an empty book and no trades, a limit order can still start the market.
    await funded_user(client)
    open_market(container, bids=[], asks=[], last_price=None)
    assert (await place(client, price="50000.00")).status_code == 202
    r = await place(client, key="k2", type="MARKET", price=None)
    assert r.status_code == 400 and r.json()["reason"] == "NO_LIQUIDITY"


async def test_market_buy_is_a_capped_limit(client, fake_d, market):
    # Purpose: section 7.3: D sees a limit at the cap, the user sees a market order.
    await funded_user(client, usdt="100000")
    r = await place(client, type="MARKET", price=None, qty="1.000")
    assert r.status_code == 202 and r.json()["type"] == "MARKET" and r.json()["price"] is None
    wire = fake_d.placed[0]
    assert wire["type"] == "LIMIT" and wire["price"] == 5250000
    assert (await balance(client, "USDT"))["held"] == "52500.00000"


async def test_market_sell_stays_market(client, fake_d, market):
    # Purpose: a market sell holds base and goes out as a true market order.
    await funded_user(client)
    assert (await place(client, side="SELL", type="MARKET", price=None)).status_code == 202
    assert fake_d.placed[0]["type"] == "MARKET" and fake_d.placed[0]["price"] is None
    assert (await balance(client, "BTC"))["held"] == "1.000"


async def test_d_unreachable_releases_at_once(client, fake_d, market):
    # Purpose: D certainly has no such order, so it is safe to say failed and release.
    await funded_user(client)
    fake_d.place_replies = [Reply(Delivery.NOT_TAKEN)]
    r = await place(client)
    assert r.status_code == 503 and r.json()["reason"] == "SYSTEM_BUSY"
    assert (await balance(client, "USDT"))["held"] == "0.00000"
    order = (await client.get("/api/orders")).json()["orders"][0]
    assert order["status"] == "REJECTED" and order["reason"] == "D_UNAVAILABLE"
    assert len(fake_d.placed) == 1


async def test_invalid_at_d_releases(client, fake_d, market):
    # Purpose: D's 400 keeps no record, so the hold can go back.
    await funded_user(client)
    fake_d.place_replies = [Reply(Delivery.INVALID)]
    assert (await place(client)).status_code == 400
    assert (await balance(client, "USDT"))["held"] == "0.00000"


@pytest.mark.parametrize("delivery", [Delivery.UNKNOWN, Delivery.CONFLICT])
async def test_no_reply_keeps_hold(client, fake_d, market, delivery):
    # Purpose: D may hold the order, so saying failed could produce a second order.
    await funded_user(client)
    fake_d.place_replies = [Reply(delivery)]
    r = await place(client)
    assert r.status_code == 202 and r.json()["status"] == "UNKNOWN"
    assert (await balance(client, "USDT"))["held"] == "50000.00000"


async def test_open_breaker_refuses_before_holding(client, container, fake_d, market):
    # Purpose: while D is down, orders fail fast and nothing is held.
    await funded_user(client)
    for _ in range(container.settings.breaker_failures):
        container.breaker.failure()
    r = await place(client)
    assert r.status_code == 503 and fake_d.placed == []
    assert (await balance(client, "USDT"))["held"] == "0.00000"


async def test_only_owner_sees_order(client, market):
    # Purpose: orders are private.
    await funded_user(client, "amy_trader")
    order_id = (await place(client)).json()["order_id"]
    await funded_user(client, "bob_trader")
    assert (await client.get(f"/api/orders/{order_id}")).status_code == 404
    assert (await client.delete(f"/api/orders/{order_id}")).status_code == 404


async def test_cancel_is_sent_and_final_refused(client, fake_d, market):
    # Purpose: a cancel goes to D and funds wait for the result; a finished order cannot be cancelled.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    r = await client.delete(f"/api/orders/{order_id}")
    assert r.status_code == 202 and r.json()["cancel_requested"] is True
    assert fake_d.cancels == [order_id]
    assert (await balance(client, "USDT"))["held"] == "50000.00000"
    n = Notifier(client)
    await n.accepted(order_id)
    await n.update(order_id, "CANCELLED", 0)
    assert (await client.delete(f"/api/orders/{order_id}")).status_code == 409


async def test_modify_unavailable_restores(client, container, fake_d, market):
    # Purpose: D answers 501 today, which must leave the order and the hold as they were.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    await Notifier(client).accepted(order_id)
    fake_d.modify_reply = Reply(Delivery.NOT_IMPLEMENTED)
    r = await client.put(f"/api/orders/{order_id}", json={"price": "51000.00", "qty": "1.000"})
    assert r.status_code == 501 and r.json()["reason"] == "MODIFY_UNAVAILABLE"
    row = await order_row(container, order_id)
    assert row.status == "PROCESSING" and row.held_amount == 5000000000
    assert (await balance(client, "USDT"))["held"] == "50000.00000"


async def test_modify_up_holds_first(client, container, market):
    # Purpose: more funds are held before sending; the result fixes the price and hold.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    n = Notifier(client)
    await n.accepted(order_id)
    r = await client.put(f"/api/orders/{order_id}", json={"price": "51000.00", "qty": "1.000"})
    assert r.status_code == 202 and r.json()["status"] == "MODIFYING"
    assert (await balance(client, "USDT"))["held"] == "51000.00000"
    await n.send("MODIFY_RESULT", order_id, result="APPLIED", price=5100000, qty=1000)
    row = await order_row(container, order_id)
    assert row.status == "PROCESSING" and row.hold_price == 5100000


async def test_modify_down_releases_only_after_the_result(client, market):
    # Purpose: the old order may still fill at its old size until D confirms.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    n = Notifier(client)
    await n.accepted(order_id)
    await client.put(f"/api/orders/{order_id}", json={"price": "50000.00", "qty": "0.500"})
    assert (await balance(client, "USDT"))["held"] == "50000.00000"
    await n.send("MODIFY_RESULT", order_id, result="APPLIED", price=5000000, qty=500)
    assert (await balance(client, "USDT"))["held"] == "25000.00000"


async def test_modify_result_without_price_keeps_it(client, container, market):
    # Purpose: an absent or null price means unchanged.
    await funded_user(client)
    order_id = (await place(client)).json()["order_id"]
    n = Notifier(client)
    await n.accepted(order_id)
    await client.put(f"/api/orders/{order_id}", json={"qty": "0.500"})
    await n.send("MODIFY_RESULT", order_id, result="APPLIED", price=None, qty=500)
    row = await order_row(container, order_id)
    assert row.price == 5000000 and row.held_amount == 2500000000


async def test_cancel_replace_mode(client, container, fake_d, market):
    # Purpose: D24 keeps this switchable, so that the team's decision only changes configuration.
    container.settings.modify_mode = "cancel_replace"
    await funded_user(client)
    order_id = (await place(client, qty="0.100")).json()["order_id"]
    await Notifier(client).accepted(order_id)
    r = await client.put(f"/api/orders/{order_id}", json={"price": "50100.00", "qty": "0.100"})
    assert r.status_code == 202 and r.json()["order_id"] != order_id and r.json()["price"] == "50100.00"
    assert fake_d.cancels == [order_id] and fake_d.modifies == []


async def test_modify_refused_for_market_and_unknown(client, fake_d, market):
    # Purpose: a market order cannot change, and an UNKNOWN order's resend must stay identical.
    await funded_user(client)
    market_id = (await place(client, key="m", type="MARKET", price=None, qty="0.100")).json()["order_id"]
    fake_d.place_replies = [Reply(Delivery.UNKNOWN)]
    unknown_id = (await place(client, key="u", qty="0.100")).json()["order_id"]
    for order_id in (market_id, unknown_id):
        r = await client.put(f"/api/orders/{order_id}", json={"price": "50000.00", "qty": "0.100"})
        assert r.status_code == 409 and r.json()["reason"] == "ORDER_NOT_MODIFIABLE"
