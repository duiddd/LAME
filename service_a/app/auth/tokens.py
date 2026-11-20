"""Access tokens are short-lived JWTs checked without the database; refresh tokens are random."""
import hashlib
import secrets
from dataclasses import dataclass

import jwt

from ..ports import Clock

ALGORITHM = "HS256"
ROLES = ("USER", "ADMIN")


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    expires_ms: int
    role: str = "USER"


class Tokens:
    def __init__(self, secret: str, access_ttl_s: int, clock: Clock):
        self._secret = secret
        self._ttl_s = access_ttl_s
        self._clock = clock

    def issue_access(self, user_id: int, username: str, role: str = "USER") -> str:
        now_s = self._clock.now_ms() // 1000
        claims = {"sub": str(user_id), "name": username, "role": role, "iat": now_s, "exp": now_s + self._ttl_s}
        return jwt.encode(claims, self._secret, algorithm=ALGORITHM)

    def verify_access(self, token: str) -> Principal | None:
        """Expiry is checked against the injected clock, so that tests can control it."""
        try:
            claims = jwt.decode(token, self._secret, algorithms=[ALGORITHM], options={"verify_exp": False, "verify_iat": False})
            expires_ms = int(claims["exp"]) * 1000
            if expires_ms <= self._clock.now_ms():
                return None
            # A token issued before roles existed, or with an unknown role, is a plain user's.
            # 角色出現前簽發、或角色不明的 token,一律視為一般使用者。
            role = claims.get("role") if claims.get("role") in ROLES else "USER"
            return Principal(int(claims["sub"]), claims["name"], expires_ms, role)
        except (jwt.PyJWTError, KeyError, ValueError):
            return None


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    """Only the hash is stored, so that a database leak gives no usable token."""
    return hashlib.sha256(token.encode()).hexdigest()
