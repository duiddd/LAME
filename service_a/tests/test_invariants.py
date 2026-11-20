"""The books hold whatever the fills, prices and arrival order.

| Test | Purpose | Method |
| --- | --- | --- |
| test_random_sequences_keep_the_books | The invariants over any outcome order | hypothesis: random fills, outcome and order -> ledger equals accounts, no negatives, held 0 at close, totals conserved |
"""
import asyncio

import httpx
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from app.container import build_container
from app.main import create_app
from tests.conftest import FakeClock, make_settings
from tests.fakes import FakeD, Notifier
from tests.helpers import funded_user, ledger_matches_accounts, open_market, order_row, place

QTY = 20

fills_strategy = st.lists(st.tuples(st.integers(1, 8), st.integers(0, 5)), max_size=5)


async def scenario(fills, outcome, trades_first):
    c = build_container(make_settings(), clock=FakeClock(), d=FakeD())
    async with c.engine.begin() as conn:
        await conn.execute(text("TRUNCATE user_events, trades, orders, ledger_entries, deposits, accounts, refresh_tokens, users RESTART IDENTITY CASCADE"))
        await conn.execute(text("UPDATE notify_cursor SET last_notify_seq = 0"))
    app = create_app(c)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            open_market(c)
            buyer = await funded_user(client, "buyer_one")
            cookies = dict(client.cookies)
            buy_id = (await place(client, key="b", qty="0.020")).json()["order_id"]
            seller = await funded_user(client, "seller_one")
            sell_id = (await place(client, key="s", side="SELL", price="49000.00", qty="0.020")).json()["order_id"]
            client.cookies.clear()
            client.cookies.update(cookies)

            trades, remaining = [], QTY
            for qty, step in fills:
                take = min(qty, remaining)
                if take:
                    trades.append((take, 4900000 + step * 10000))
                    remaining -= take
            if outcome == "FILLED" and remaining:
                trades.append((remaining, 4900000))
                remaining = 0
            filled = QTY - remaining

            n = Notifier(client)
            await n.accepted(buy_id)
            await n.accepted(sell_id, order_seq=8)

            async def send_trades():
                for take, price in trades:
                    await n.trade((buy_id, buyer["user_id"], "BUY"), (sell_id, seller["user_id"], "SELL"), price, take)

            if trades_first:
                await send_trades()
                await n.update(buy_id, outcome, filled)
            else:
                await n.update(buy_id, outcome, filled)
                await send_trades()

            await ledger_matches_accounts(c)
            row = await order_row(c, buy_id)
            assert row.settled_qty == row.filled_qty == filled
            assert row.held_amount == 0 and row.closed_at is not None
            async with c.engine.connect() as conn:
                totals = dict((await conn.execute(text("SELECT asset, sum(available + held) FROM accounts GROUP BY asset"))).all())
            assert totals["USDT"] == 200000 * 100000 and totals["BTC"] == 20 * 1000
    return True


@settings(max_examples=25, deadline=None)
@given(fills=fills_strategy, outcome=st.sampled_from(["CANCELLED", "REJECTED", "EXPIRED", "FILLED"]), trades_first=st.booleans())
def test_random_sequences_keep_the_books(migrated_once, fills, outcome, trades_first):
    # Purpose: whatever happens, money is neither created nor lost, and no hold is left behind.
    assert asyncio.run(scenario(fills, outcome, trades_first))
