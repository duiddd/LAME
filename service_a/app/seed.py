"""Demo users and the users in linked_accounts.json, with starting balances, and the admin. Deterministic.

Bots log in as these users through the public API. Seeded users keep the IDs in the shared file,
because D's wash-trading rule reads the same IDs.
"""
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from .accounts.service import AccountsService, Ref
from .auth import repo as auth_repo
from .auth.passwords import Passwords
from .config import Catalog, to_units
from .ports import Clock

DEMO_IDS = [1001, 1002, 1003, 1004]
# The operator account that may see the system dashboard; it trades nothing, so it gets no balances.
# If "admin" was registered by someone first, the seed skips it: a name never grants the role.
# 可看系統 dashboard 的維運帳號;不交易,所以沒有餘額。若 "admin" 已被他人註冊,seed 會略過:名稱不會帶來權限。
ADMIN_ID, ADMIN_NAME = 1000, "admin"
START_BALANCES = {"USDT": "1000000", "BTC": "100", "ETH": "1000"}


def seeded_users(linked_accounts_file: str) -> list[int]:
    groups = json.loads(Path(linked_accounts_file).read_text(encoding="utf-8"))["groups"]
    linked = [int(u) for g in groups for u in g["user_ids"]]
    return sorted(set(DEMO_IDS + linked))


async def seed(engine: AsyncEngine, accounts: AccountsService, passwords: Passwords, catalog: Catalog, clock: Clock,
               linked_accounts_file: str, password: str, with_balances: bool) -> None:
    missing = []
    async with engine.connect() as conn:
        for user_id in seeded_users(linked_accounts_file):
            if not await auth_repo.user_exists(conn, user_id):
                missing.append(user_id)
        admin_missing = not await auth_repo.user_exists(conn, ADMIN_ID)
    if not missing and not admin_missing:
        return
    password_hash = await passwords.hash(password)
    async with engine.begin() as conn:
        if admin_missing:
            await auth_repo.insert_seeded_user(conn, ADMIN_ID, ADMIN_NAME, password_hash, clock.now_ms(), role="ADMIN")
        for user_id in missing:
            if await auth_repo.insert_seeded_user(conn, user_id, f"user{user_id}", password_hash, clock.now_ms()) and with_balances:
                for asset, amount in START_BALANCES.items():
                    if asset in catalog.assets:
                        units = to_units(amount, catalog.assets[asset].decimals)
                        await accounts.credit(conn, user_id, asset, units, "SEED", Ref("seed", str(user_id)))
