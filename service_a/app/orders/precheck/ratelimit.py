"""Per-user token buckets, in memory because A is one process (D9)."""
from ...ports import Clock


class TokenBuckets:
    def __init__(self, rate_per_s: float, burst: float, clock: Clock):
        self._rate = rate_per_s
        self._burst = burst
        self._clock = clock
        self._buckets: dict[int, tuple[float, int]] = {}

    def take(self, user_id: int) -> bool:
        now = self._clock.now_ms()
        tokens, last = self._buckets.get(user_id, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) / 1000 * self._rate)
        allowed = tokens >= 1
        self._buckets[user_id] = (tokens - 1 if allowed else tokens, now)
        return allowed
