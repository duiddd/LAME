"""Keeps background tasks alive. An asyncio task that nobody awaits dies silently, so each one runs
under a loop that logs the crash, restarts with backoff and reports its state to /healthz.
"""
import asyncio
import logging
from typing import Awaitable, Callable

from .metrics import Metrics

log = logging.getLogger("a.supervisor")


class Supervisor:
    def __init__(self, metrics: Metrics):
        self._metrics = metrics
        self._tasks: list[asyncio.Task] = []
        self.states: dict[str, str] = {}

    def start(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        self.states[name] = "running"
        self._tasks.append(asyncio.create_task(self._run(name, factory)))

    async def _run(self, name: str, factory) -> None:
        backoff = 0.5
        while True:
            try:
                self.states[name] = "running"
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background task %s crashed; restarting in %.1fs", name, backoff)
                self.states[name] = "restarting"
                self._metrics.restarts.labels(name).inc()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
