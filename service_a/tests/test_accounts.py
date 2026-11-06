"""Balances, the ledger and deposits.

| Test | Purpose | Method |
| --- | --- | --- |
| test_new_user_starts_empty | New users have no funds | Register -> no balances |
| test_deposit_credits_at_once | Simulated funds arrive immediately | Deposit -> available and total grow, ledger entry written |
| test_deposit_is_idempotent | A retried deposit credits once | Same key twice -> one credit, same deposit ID |
| test_deposit_limits | Bad amounts are refused with a reason | Negative, too many decimals, too large, unknown asset |
| test_concurrent_holds_never_overdraw | Holds cannot race past the balance | 100 concurrent holds of 10 on a balance of 300 -> exactly 30 succeed |
| test_release_beyond_held_is_a_violation | A wrong release is detected, not absorbed | Release more than held -> BalanceViolation |
| test_ledger_matches_accounts | The books can be audited | Sum of ledger entries equals account balances |
| test_seed_uses_linked_account_ids | D's rule sees the same users | Seeded IDs include 1024 and 3001, with balances, twice is harmless |
"""
import asyncio

import pytest
from sqlalchemy import text

from app.accounts.service import BalanceViolation, Ref
from app.seed import seed
from tests.conftest import sign_up


async def deposit(client, asset="USDT", amount="100.5", key="k1"):
    return await client.post("/api/accounts/deposits", json={"asset": asset, "amount": amount}, headers={"Idempotency-Key": key})


async def test_new_user_starts_empty(client):
    # Purpose: funds come only from deposits.
    await sign_up(client)
    assert (await client.get("/api/accounts/me")).json()["balances"] == []


async def test_deposit_credits_at_once(client, container):
    # Purpose: a deposit is immediately available and leaves a ledger entry.
    await sign_up(client)
    r = await deposit(client)
    assert r.status_code == 201
    balance = (await client.get("/api/accounts/me")).json()["balances"][0]
    assert balance == {"asset": "USDT", "available": "100.50000", "held": "0.00000", "total": "100.50000"}
    async with container.engine.connect() as conn:
        entry = (await conn.execute(text("SELECT reason, delta_available FROM ledger_entries"))).one()
    assert entry.reason == "DEPOSIT" and entry.delta_available == 10050000


async def test_deposit_is_idempotent(client):
    # Purpose: a double click or network retry must not create money.
    await sign_up(client)
    first = (await deposit(client)).json()
    second = (await deposit(client)).json()
    assert first["deposit_id"] == second["deposit_id"]
    assert (await client.get("/api/accounts/me")).json()["balances"][0]["available"] == "100.50000"


@pytest.mark.parametrize("asset,amount,reason", [
    ("USDT", "-1", "INVALID_AMOUNT"),
    ("USDT", "0", "INVALID_AMOUNT"),
    ("BTC", "0.0001", "INVALID_AMOUNT"),
    ("USDT", "1000001", "DEPOSIT_TOO_LARGE"),
    ("DOGE", "1", "INVALID_ASSET"),
])
async def test_deposit_limits(client, asset, amount, reason):
    # Purpose: each invalid deposit is refused with its own reason, and nothing is credited.
    await sign_up(client)
    r = await deposit(client, asset, amount)
    assert r.status_code == 422 and r.json()["reason"] == reason
    assert (await client.get("/api/accounts/me")).json()["balances"] == []


async def test_concurrent_holds_never_overdraw(client, container):
    # Purpose: the conditional update makes the balance check and the hold one atomic step.
    user = await sign_up(client)
    await deposit(client, "USDT", "300")
    amount = 10 * 100000

    async def hold(i):
        async with container.engine.begin() as conn:
            return await container.accounts.hold(conn, user["user_id"], "USDT", amount, Ref("order", str(i)))

    results = await asyncio.gather(*(hold(i) for i in range(100)))
    assert sum(results) == 30
    balance = (await client.get("/api/accounts/me")).json()["balances"][0]
    assert balance["available"] == "0.00000" and balance["held"] == "300.00000"


async def test_release_beyond_held_is_a_violation(client, container):
    # Purpose: if A's books disagree with an outcome, that surfaces instead of moving money.
    user = await sign_up(client)
    await deposit(client, "USDT", "1")
    with pytest.raises(BalanceViolation):
        async with container.engine.begin() as conn:
            await container.accounts.release(conn, user["user_id"], "USDT", 1, Ref("order", "x"))


async def test_ledger_matches_accounts(client, container):
    # Purpose: every balance is explained by its ledger entries.
    user = await sign_up(client)
    await deposit(client, "USDT", "50", "a")
    await deposit(client, "USDT", "25", "b")
    async with container.engine.begin() as conn:
        await container.accounts.hold(conn, user["user_id"], "USDT", 1000000, Ref("order", "o1"))
        await container.accounts.release(conn, user["user_id"], "USDT", 400000, Ref("order", "o1"))
    async with container.engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT a.available, a.held, l.av, l.he FROM accounts a JOIN (SELECT user_id, asset, sum(delta_available) av, "
            "sum(delta_held) he FROM ledger_entries GROUP BY 1, 2) l USING (user_id, asset)"))).one()
    assert (row.available, row.held) == (row.av, row.he)
    assert row.held == 600000


async def test_seed_uses_linked_account_ids(container):
    # Purpose: D's wash-trading rule and A must mean the same users; seeding twice changes nothing.
    for _ in range(2):
        await seed(container.engine, container.accounts, container.passwords, container.catalog, container.clock,
                   container.settings.linked_accounts_file, "seed-password-1", True)
    async with container.engine.connect() as conn:
        ids = (await conn.execute(text("SELECT user_id FROM users"))).scalars().all()
        usdt = (await conn.execute(text("SELECT available FROM accounts WHERE user_id = 1024 AND asset = 'USDT'"))).scalar()
    assert {1024, 1025, 3001, 3002, 3003, 1001} <= set(ids)
    assert usdt == 1000000 * 100000
