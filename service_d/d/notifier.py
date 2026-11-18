"""Sends the outbox to A's POST /internal/order-events (protocol doc section 3.1, RD25).

One notification at a time, in notify_seq order; the same one is resent until A
answers 2xx. A answers 409 {"expected_seq": N} on a gap, and D resends from N.
"""
import asyncio
import json
import logging

import httpx

log = logging.getLogger("d.notifier")


class Notifier:
    def __init__(self, store, url: str, faults, metrics, transport=None):
        self.store = store
        self.url = url
        self.faults = faults
        self.metrics = metrics
        self.wake = asyncio.Event()
        self.client = httpx.AsyncClient(timeout=2.0, transport=transport)
        self._stop = False

    @property
    def delivered(self) -> int:
        return self.store.get_meta("delivered_seq", 0)

    def rewind(self, from_seq: int):
        self.store.set_meta("delivered_seq", max(0, from_seq - 1))
        self.wake.set()

    def backlog(self) -> int:
        row = self.store.one("SELECT COALESCE(MAX(notify_seq), 0) AS m FROM outbox")
        return row["m"] - self.delivered

    async def run(self):
        backoff = 0.1
        while not self._stop:
            row = self.store.one("SELECT notify_seq, payload FROM outbox WHERE notify_seq > ? "
                                 "ORDER BY notify_seq LIMIT 1", self.delivered)
            if row is None:
                self.wake.clear()
                await self.wake.wait()
                continue
            seq = row["notify_seq"]
            if self.faults.take("skip_next_notification"):
                log.warning("fault: skipping notify_seq %s", seq)
                self.store.set_meta("delivered_seq", seq)
                continue
            try:
                r = await self.client.post(self.url, json=json.loads(row["payload"]))
            except httpx.HTTPError as e:
                log.warning("A unreachable for notify_seq %s: %s", seq, e)
                r = None
            if r is not None and 200 <= r.status_code < 300:
                self.store.set_meta("delivered_seq", seq)
                self.metrics.notifications.inc()
                backoff = 0.1
            elif r is not None and r.status_code == 409:
                expected = int(r.json()["expected_seq"])
                log.warning("A expects notify_seq %s (sent %s), resending from there", expected, seq)
                self.store.set_meta("delivered_seq", expected - 1)
            else:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5)

    async def close(self):
        self._stop = True
        self.wake.set()
        await self.client.aclose()
