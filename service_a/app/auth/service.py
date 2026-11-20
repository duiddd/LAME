"""Registration, login and refresh rotation with reuse detection (D20)."""
import secrets
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from ..errors import AppError
from ..ports import Clock
from . import repo
from .passwords import Passwords
from .tokens import Tokens, hash_refresh_token, new_refresh_token


@dataclass(frozen=True)
class Session:
    user_id: int
    username: str
    access_token: str
    refresh_token: str | None
    role: str = "USER"


class AuthService:
    def __init__(self, engine: AsyncEngine, passwords: Passwords, tokens: Tokens, clock: Clock,
                 refresh_ttl_s: int, refresh_grace_s: int):
        self._engine = engine
        self._passwords = passwords
        self._tokens = tokens
        self._clock = clock
        self._refresh_ttl_ms = refresh_ttl_s * 1000
        self._grace_ms = refresh_grace_s * 1000

    async def register(self, username: str, password: str) -> Session:
        password_hash = await self._passwords.hash(password)
        async with self._engine.begin() as conn:
            user_id = await repo.insert_user(conn, username, password_hash, self._clock.now_ms())
            if user_id is None:
                raise AppError(409, "USERNAME_TAKEN", "That username is taken")
            return await self._new_session(conn, user_id, username, "USER", secrets.token_hex(16))

    async def login(self, username: str, password: str) -> Session:
        async with self._engine.connect() as conn:
            user = await repo.find_user(conn, username)
        if not await self._passwords.verify(user.password_hash if user else None, password):
            raise AppError(401, "UNAUTHORIZED", "The username or password is wrong")
        async with self._engine.begin() as conn:
            return await self._new_session(conn, user.user_id, user.username, user.role, secrets.token_hex(16))

    async def refresh(self, token: str | None) -> Session:
        """Each refresh token works once. A second use means it was copied, so its family dies.

        Two tabs can refresh at once and spend the same token, so a reuse within the grace period
        only gets a new access token: the browser already holds the newest refresh token.
        """
        unauthorized = AppError(401, "UNAUTHORIZED", "Log in again")
        if not token:
            raise unauthorized
        now = self._clock.now_ms()
        revoked = False
        async with self._engine.begin() as conn:
            row = await repo.lock_refresh(conn, hash_refresh_token(token))
            if row is None or row.revoked_at is not None or row.expires_at <= now:
                raise unauthorized
            user = await repo.get_user(conn, row.user_id)
            if row.used_at is None:
                await repo.mark_refresh_used(conn, row.token_hash, now)
                return await self._new_session(conn, user.user_id, user.username, user.role, row.family_id)
            if now - row.used_at <= self._grace_ms:
                return Session(user.user_id, user.username, self._tokens.issue_access(user.user_id, user.username, user.role), None,
                               user.role)
            await repo.revoke_family(conn, row.family_id, now)
            revoked = True
        if revoked:
            raise unauthorized
        raise AssertionError("unreachable")

    async def logout(self, token: str | None) -> None:
        if not token:
            return
        async with self._engine.begin() as conn:
            row = await repo.lock_refresh(conn, hash_refresh_token(token))
            if row is not None:
                await repo.revoke_family(conn, row.family_id, self._clock.now_ms())

    async def _new_session(self, conn, user_id: int, username: str, role: str, family_id: str) -> Session:
        token = new_refresh_token()
        expires_at = self._clock.now_ms() + self._refresh_ttl_ms
        await repo.insert_refresh(conn, hash_refresh_token(token), family_id, user_id, expires_at)
        return Session(user_id, username, self._tokens.issue_access(user_id, username, role), token, role)
