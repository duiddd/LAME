"""Rejects new orders when the path through D is slow (D13).

D's 202 is always fast and hides queueing inside D, so this measures from sending an order to its
first outcome. An order still waiting counts at its current age, so stalled notifications also
trigger it. Hysteresis keeps it from flapping around the threshold.
"""
from collections import deque

from .metrics import Metrics
from .ports import Clock


class Shedder:
    def __init__(self, threshold_s: float, window_s: float, breaches: int, resume_ratio: float, clock: Clock, metrics: Metrics):
        self._threshold = threshold_s
        self._window_ms = window_s * 1000
        self._breaches = breaches
        self._resume = threshold_s * resume_ratio
        self._clock = clock
        self._metrics = metrics
        self._samples: deque[tuple[int, float]] = deque()
        self._waiting: dict[str, int] = {}
        self._streak = 0
        self.active = False

    def started(self, order_id: str) -> None:
        self._waiting[order_id] = self._clock.now_ms()

    def finished(self, order_id: str) -> None:
        started = self._waiting.pop(order_id, None)
        if started is not None:
            seconds = (self._clock.now_ms() - started) / 1000
            self._samples.append((self._clock.now_ms(), seconds))
            self._metrics.completion.observe(seconds)

    def busy(self) -> None:
        """D answered RISK_BUSY: its queue is full, whatever the recent latencies say."""
        self._samples.append((self._clock.now_ms(), self._threshold * 2))

    def p99(self) -> float:
        now = self._clock.now_ms()
        while self._samples and now - self._samples[0][0] > self._window_ms:
            self._samples.popleft()
        values = sorted([s for _, s in self._samples] + [(now - t) / 1000 for t in self._waiting.values()])
        return values[min(len(values) - 1, int(len(values) * 0.99))] if values else 0.0

    def evaluate(self) -> None:
        """Called once a second."""
        p99 = self.p99()
        if self.active:
            if p99 < self._resume:
                self.active = False
                self._streak = 0
        elif p99 > self._threshold:
            self._streak += 1
            self.active = self._streak >= self._breaches
        else:
            self._streak = 0
        self._metrics.shedding.set(int(self.active))
