"""The production pair grid: one integer unit must mean the same thing to A, B and D.

On 18 Nov B's pair file (lot 100, 6 quantity decimals) and A's (lot 1, 3 decimals) disagreed, and B
rejected A's orders with INVALID_QTY; quantities would also have been read 1000 times apart. The
team settled on B's grid on 19 Nov. These tests pin it and compare A's file with B's and D's when
they are in the same checkout (on main).

| Test | Purpose | Method |
| --- | --- | --- |
| test_production_grid_is_bs | The decided grid | Load config/symbols.json -> 2 price decimals, 6 quantity decimals, BTC lot 100 (0.0001), ETH lot 1000 (0.001), USDT 6 decimals |
| test_grid_matches_b_and_d | A, B and D agree | B's pairs.json and D's symbols.json, when present -> same symbols, tick and lot; B's price, quantity and asset decimals equal A's |
| test_grid_must_keep_amounts_exact | No rounding, ever | USDT 6 decimals with a lot of 0.000001 BTC -> the loader refuses the file |
| test_settlement_is_exact_on_the_production_grid | Amounts at the smallest units | Buy 0.0001 BTC at 50000.01, fill at 50000.00 -> 5.000001 held, 0.000001 back, seller gets 5.000000, ledger balances |
| test_order_units_follow_the_grid | What crosses the wire | Production grid: 0.00005 BTC -> QTY_TOO_SMALL, 0.00015 -> INVALID_LOT, nothing sent; 0.0001 BTC -> qty 100 sent to D |
"""
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import load_catalog
from app.container import build_container
from app.main import create_app
from tests.conftest import ROOT, TABLES, make_settings
from tests.fakes import Notifier
from tests.helpers import funded_user, ledger_matches_accounts, open_market, place

PRODUCTION = ROOT / "config" / "symbols.json"
REPO = ROOT.parent
B_PAIRS = REPO / "match_engine" / "config" / "pairs.json"
D_SYMBOLS = REPO / "service_d" / "config" / "symbols.json"


def test_production_grid_is_bs():
    # Purpose: the units the team decided on, in human terms, so a silent change fails here.
    c = load_catalog(str(PRODUCTION))
    btc, eth = c.symbols["BTC-USDT"], c.symbols["ETH-USDT"]
    assert (btc.price_decimals, btc.qty_decimals, btc.tick, btc.lot) == (2, 6, 1, 100)
    assert (eth.price_decimals, eth.qty_decimals, eth.tick, eth.lot) == (2, 6, 1, 1000)
    assert c.qty_to_int(btc, "0.0001") == btc.min_qty == btc.lot
    assert c.qty_to_int(eth, "0.001") == eth.min_qty == eth.lot
    assert c.assets["USDT"].decimals == 6                     # B's, and exact: 0.01 x 0.0001 = 0.000001


def test_grid_matches_b_and_d():
    # Purpose: the shared config is a contract; any drift between the three files fails here.
    if not B_PAIRS.exists():
        pytest.skip("B's pairs.json is not in this checkout (only on main)")
    mine = load_catalog(str(PRODUCTION)).symbols
    b = {p["symbol"]: p for p in json.loads(B_PAIRS.read_text(encoding="utf-8"))["pairs"]}
    assert set(b) == set(mine)
    for name, p in b.items():
        s = mine[name]
        assert (p["tick"], p["lot"]) == (s.tick, s.lot), name
        assert (p["price_decimals"], p["qty_decimals"]) == (s.price_decimals, s.qty_decimals), name
    b_assets = json.loads(B_PAIRS.read_text(encoding="utf-8"))["assets"]
    mine_assets = load_catalog(str(PRODUCTION)).assets
    assert {a: v["decimals"] for a, v in b_assets.items()} == {a: mine_assets[a].decimals for a in b_assets}
    if D_SYMBOLS.exists():
        d = {x["symbol"]: x for x in json.loads(D_SYMBOLS.read_text(encoding="utf-8"))["symbols"]}
        assert {n: (x["tick"], x["lot"]) for n, x in d.items()} == {n: (s.tick, s.lot) for n, s in mine.items()}


def test_grid_must_keep_amounts_exact(tmp_path):
    # Purpose: a grid on which price x qty could fall between two quote units does not load.
    raw = json.loads(PRODUCTION.read_text(encoding="utf-8"))
    raw["symbols"][0]["lot"] = raw["symbols"][0]["min_qty"] = 1          # 0.000001 BTC x 0.01 = 0.00000001 USDT
    bad = tmp_path / "symbols.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="whole number of USDT units"):
        load_catalog(str(bad))


@pytest_asyncio.fixture
async def production_client(migrated, clock, fake_d):
    c = build_container(make_settings(symbols_file=str(PRODUCTION)), clock=clock, d=fake_d)
    async with c.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
        await conn.execute(text("UPDATE notify_cursor SET last_notify_seq = 0"))
    app = create_app(c)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield c, client
    await c.engine.dispose()


async def test_order_units_follow_the_grid(production_client, fake_d):
    # Purpose: with the production grid, A sends B's units and refuses what B would refuse.
    c, client = production_client
    open_market(c, bids=[{"price": 4990000, "qty": 100}], asks=[{"price": 5000000, "qty": 100}])
    await funded_user(client)
    r = await place(client, key="small", qty="0.00005")
    assert r.status_code == 400 and r.json()["reason"] == "QTY_TOO_SMALL"
    r = await place(client, key="off-grid", qty="0.00015")
    assert r.status_code == 400 and r.json()["reason"] == "INVALID_LOT"
    assert fake_d.placed == []
    r = await place(client, key="min", qty="0.0001")
    assert r.status_code == 202, r.text
    assert fake_d.placed[0]["qty"] == 100 and fake_d.placed[0]["price"] == 5000000
    held = next(b for b in (await client.get("/api/accounts/me")).json()["balances"] if b["asset"] == "USDT")["held"]
    assert held == "5.000000"                                 # 50000.00 x 0.0001


async def test_settlement_is_exact_on_the_production_grid(production_client):
    # Purpose: with USDT at 6 decimals, holds, price improvement and the seller's proceeds come out exact.
    c, client = production_client
    open_market(c)
    buyer = await funded_user(client, "amy_trader")
    buy = (await place(client, key="b", price="50000.01", qty="0.0001")).json()["order_id"]
    seller = await funded_user(client, "bob_trader")
    sell = (await place(client, key="s", side="SELL", price="50000.00", qty="0.0001")).json()["order_id"]
    n = Notifier(client)
    await n.accepted(buy)
    await n.accepted(sell, order_seq=8)
    await n.trade((buy, buyer["user_id"], "BUY"), (sell, seller["user_id"], "SELL"), price=5000000, qty=100)
    await n.update(buy, "FILLED", 100)
    await n.update(sell, "FILLED", 100)
    async with c.engine.connect() as conn:
        rows = (await conn.execute(text("SELECT user_id, available, held FROM accounts WHERE asset = 'USDT'"))).all()
    usdt = {r.user_id: (r.available, r.held) for r in rows}
    assert usdt[buyer["user_id"]] == (100000_000000 - 5_000000, 0)       # held 5.000001, 0.000001 returned
    assert usdt[seller["user_id"]] == (100000_000000 + 5_000000, 0)
    await ledger_matches_accounts(c)
