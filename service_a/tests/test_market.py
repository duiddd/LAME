"""Candles, the day open and the market-data consumer.

| Test | Purpose | Method |
| --- | --- | --- |
| test_candles_match_the_trades | OHLCV comes from stored trades | Known trades -> expected open, high, low, close, volume, by interval |
| test_same_ts_uses_b_order | Trades of one command share ts | Same ts -> open and close by B's seq |
| test_watermark_returns_only_new_buckets | Polling is cheap | after_seq with nothing new -> empty; with new -> only touched buckets |
| test_late_trade_corrects_an_old_candle | A resend after an outage lands where it belongs | Old ts after the watermark -> reply starts at the old bucket |
| test_market_data_is_public | The chart works before login | No cookie -> 200 |
| test_summary_has_day_open_and_decimals | The market list can compute change | First trade of the latest day, and decimals |
| test_cache_shares_one_query | Many viewers cost one query | Same second -> cached; next second -> fresh |
| test_bad_market_message_is_skipped | One bad frame cannot stop the feed | Malformed -> counted, previous snapshot kept |
| test_older_snapshot_is_ignored_but_counts_as_fresh | last_ms orders; receipt freshens | Lower last_ms -> kept newest, age reset |
"""
from sqlalchemy import text

from app.market.feed import apply_message
from tests.helpers import open_market

T0 = 1_800_000_000_000


async def add_trades(container, trades):
    async with container.engine.begin() as conn:
        for seq, ts, price, qty in trades:
            await conn.execute(text(
                "INSERT INTO trades VALUES ('2026-11-06', :s, 'BTC-USDT', :p, :q, 'a', 'b', :t)"), {"s": seq, "t": ts, "p": price, "q": qty})


async def candles(client, **params):
    r = await client.get("/api/market/candles", params={"symbol": "BTC-USDT", "interval": "5s", **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_candles_match_the_trades(client, container):
    # Purpose: each candle is exactly what its trades say.
    await add_trades(container, [(1, T0, 5000000, 10), (2, T0 + 1000, 5100000, 20), (3, T0 + 2000, 4900000, 30), (4, T0 + 3000, 5050000, 40), (5, T0 + 5000, 5200000, 5)])
    reply = await candles(client)
    assert reply["replace_all"] is True and reply["watermark"] == {"b_day": "2026-11-06", "b_seq": 5}
    first, second = reply["candles"]
    assert first == {"t": T0, "o": "50000.00", "h": "51000.00", "l": "49000.00", "c": "50500.00", "v": "0.100"}
    assert second["t"] == T0 + 5000 and second["v"] == "0.005"
    wider = await candles(client, interval="1m")
    assert len(wider["candles"]) == 1 and wider["candles"][0]["v"] == "0.105"


async def test_same_ts_uses_b_order(client, container):
    # Purpose: ts cannot order trades of one command, and B's seq can.
    await add_trades(container, [(9, T0, 3000000, 1), (7, T0, 1000000, 1), (8, T0, 2000000, 1)])
    candle = (await candles(client))["candles"][0]
    assert candle["o"] == "10000.00" and candle["c"] == "30000.00"


async def test_watermark_returns_only_new_buckets(client, container, clock):
    # Purpose: a poll with nothing new returns nothing; with something new, only what it touched.
    await add_trades(container, [(1, T0, 5000000, 1), (2, T0 + 60000, 5000000, 1)])
    args = {"after_day": "2026-11-06", "after_seq": 2}
    assert (await candles(client, **args))["candles"] == []
    await add_trades(container, [(3, T0 + 61000, 5100000, 1)])
    clock.advance(1000)
    reply = await candles(client, **args)
    assert reply["replace_all"] is False and [c["t"] for c in reply["candles"]] == [T0 + 60000]
    assert reply["candles"][0]["v"] == "0.002" and reply["watermark"]["b_seq"] == 3


async def test_late_trade_corrects_an_old_candle(client, container, clock):
    # Purpose: after an outage, trades arrive late with old times, and old candles must change.
    await add_trades(container, [(1, T0, 5000000, 1), (2, T0 + 60000, 5000000, 1)])
    await add_trades(container, [(3, T0 + 1000, 6000000, 1)])
    reply = await candles(client, after_day="2026-11-06", after_seq=2)
    assert reply["candles"][0]["t"] == T0 and reply["candles"][0]["h"] == "60000.00"
    assert len(reply["candles"]) == 2


async def test_market_data_is_public(client):
    # Purpose: anyone can view the market before logging in.
    assert (await client.get("/api/market/summary")).status_code == 200
    assert (await candles(client))["candles"] == []


async def test_summary_has_day_open_and_decimals(client, container):
    # Purpose: the change since the day's first trade needs that trade's price.
    await add_trades(container, [(2, T0, 5100000, 1), (1, T0, 5000000, 1)])
    symbols = {s["symbol"]: s for s in (await client.get("/api/market/summary")).json()["symbols"]}
    btc = symbols["BTC-USDT"]
    assert btc["day_open"] == "50000.00" and btc["day"] == "2026-11-06" and btc["price_decimals"] == 2
    assert symbols["ETH-USDT"]["day_open"] is None


async def test_cache_shares_one_query(client, container, clock):
    # Purpose: within a second, every viewer gets the same answer without another query.
    await add_trades(container, [(1, T0, 5000000, 1)])
    assert (await candles(client))["watermark"]["b_seq"] == 1
    await add_trades(container, [(2, T0, 5000000, 1)])
    assert (await candles(client))["watermark"]["b_seq"] == 1
    clock.advance(1000)
    assert (await candles(client))["watermark"]["b_seq"] == 2


async def test_bad_market_message_is_skipped(container):
    # Purpose: a task that crashed on a bad frame would crash on it forever.
    open_market(container)
    apply_message(container.prices, container.metrics, "BTC-USDT", "{not json")
    apply_message(container.prices, container.metrics, "BTC-USDT", '{"bids": []}')
    assert container.prices.fresh("BTC-USDT").best_ask == 5000000
    assert container.metrics.md_bad.labels("BTC-USDT")._value.get() == 2


async def test_older_snapshot_is_ignored_but_counts_as_fresh(container, clock):
    # Purpose: last_ms orders snapshots, while any message proves the feed is alive.
    open_market(container, last_ms=100)
    clock.advance(2900)
    open_market(container, last_ms=50, last_price=1)
    clock.advance(2900)
    assert container.prices.fresh("BTC-USDT").last_price == 5000000
