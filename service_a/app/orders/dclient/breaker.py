"""A small circuit breaker (D12): consecutive failures open it, and one trial closes it again."""
from ...ports import Clock

CLOSED, HALF_OPEN, OPEN = 0, 1, 2


class CircuitBreaker:
    def __init__(self, failures: int, open_s: float, clock: Clock):
        self._limit = failures
        self._open_ms = open_s * 1000
        self._clock = clock
        self._failures = 0
        self._opened_at = 0
        self.state = CLOSED

    def allow(self) -> bool:
        if self.state == CLOSED:
            return True
        if self.state == OPEN and self._clock.now_ms() - self._opened_at >= self._open_ms:
            self.state = HALF_OPEN
            return True
        return False

    def success(self) -> None:
        self._failures = 0
        self.state = CLOSED

    def failure(self) -> None:
        self._failures += 1
        if self.state == HALF_OPEN or self._failures >= self._limit:
            self.state = OPEN
            self._opened_at = self._clock.now_ms()
