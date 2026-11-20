from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def insert_user(conn: AsyncConnection, username: str, password_hash: str, now: int) -> int | None:
    """Returns the new ID, or None if the username is taken."""
    row = (await conn.execute(text(
        "INSERT INTO users (username, password_hash, created_at) VALUES (:u, :p, :n) "
        "ON CONFLICT (username) DO NOTHING RETURNING user_id"), {"u": username, "p": password_hash, "n": now})).first()
    return row.user_id if row else None


async def insert_seeded_user(conn: AsyncConnection, user_id: int, username: str, password_hash: str, now: int,
                             role: str = "USER") -> bool:
    row = (await conn.execute(text(
        "INSERT INTO users (user_id, username, password_hash, created_at, role) VALUES (:i, :u, :p, :n, :r) "
        "ON CONFLICT DO NOTHING RETURNING user_id"), {"i": user_id, "u": username, "p": password_hash, "n": now, "r": role})).first()
    return row is not None


async def user_exists(conn: AsyncConnection, user_id: int) -> bool:
    return (await conn.execute(text("SELECT 1 FROM users WHERE user_id = :i"), {"i": user_id})).first() is not None


async def find_user(conn: AsyncConnection, username: str):
    return (await conn.execute(text("SELECT user_id, username, password_hash, role FROM users WHERE username = :u"), {"u": username})).first()


async def get_user(conn: AsyncConnection, user_id: int):
    return (await conn.execute(text("SELECT user_id, username, role FROM users WHERE user_id = :i"), {"i": user_id})).first()


async def insert_refresh(conn: AsyncConnection, token_hash: str, family_id: str, user_id: int, expires_at: int) -> None:
    await conn.execute(text(
        "INSERT INTO refresh_tokens (token_hash, family_id, user_id, expires_at) VALUES (:h, :f, :u, :e)"),
        {"h": token_hash, "f": family_id, "u": user_id, "e": expires_at})


async def lock_refresh(conn: AsyncConnection, token_hash: str):
    """The row lock makes concurrent uses of one token take turns, so only one rotates it."""
    return (await conn.execute(text(
        "SELECT token_hash, family_id, user_id, expires_at, used_at, revoked_at FROM refresh_tokens "
        "WHERE token_hash = :h FOR UPDATE"), {"h": token_hash})).first()


async def mark_refresh_used(conn: AsyncConnection, token_hash: str, now: int) -> None:
    await conn.execute(text("UPDATE refresh_tokens SET used_at = :n WHERE token_hash = :h"), {"h": token_hash, "n": now})


async def revoke_family(conn: AsyncConnection, family_id: str, now: int) -> None:
    await conn.execute(text(
        "UPDATE refresh_tokens SET revoked_at = :n WHERE family_id = :f AND revoked_at IS NULL"), {"f": family_id, "n": now})
