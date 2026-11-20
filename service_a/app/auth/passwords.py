"""Argon2id hashing, off the event loop and with a cap on concurrent hashes.

One hash takes tens of milliseconds and tens of megabytes. On the event loop it would stall every
order; without a cap, a login flood could exhaust the CPU and memory order handling needs.
"""
import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from ..errors import AppError


class Passwords:
    def __init__(self, max_concurrent: int):
        self._hasher = PasswordHasher()
        self._slots = asyncio.Semaphore(max_concurrent)
        self._dummy = self._hasher.hash("timing-equaliser")

    def _take_slot(self) -> None:
        if self._slots.locked():
            raise AppError(429, "RATE_LIMITED", "Too many login attempts. Try again shortly.")

    async def hash(self, password: str) -> str:
        self._take_slot()
        async with self._slots:
            return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, stored_hash: str | None, password: str) -> bool:
        """A missing user still costs one hash, so that timing does not reveal who exists."""
        self._take_slot()
        async with self._slots:
            return await asyncio.to_thread(self._check, stored_hash or self._dummy, password) and stored_hash is not None

    def _check(self, stored_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(stored_hash, password)
        except (VerificationError, InvalidHashError):
            return False
