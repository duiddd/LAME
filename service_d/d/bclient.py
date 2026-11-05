"""D's single WebSocket connection to B's /d (protocol doc section 4).

- First message is {"type":"resume","from_seq":N}; B resends today's outputs with
  seq > N while it also pushes new ones, so the two can interleave and repeat.
- Outputs are applied strictly in their prev_seq chain order, each exactly once.
- Caught up: the chain is contiguous and nothing new has arrived for
  CATCHUP_QUIET_MS. B sends no "resend finished" marker and does not mark which
  records are live, so this is the best D can do without B's help (open item).
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import websockets

log = logging.getLogger("d.bclient")
DUBLIN = ZoneInfo("Europe/Dublin")


def today() -> str:
    return datetime.now(DUBLIN).date().isoformat()


def now_ms() -> int:
    return int(time.time() * 1000)


class BClient:
    def __init__(self, settings, on_record, on_state, faults):
        self.s = settings
        self.on_record = on_record   # called once per output, in chain order
        self.on_state = on_state     # called when connected / caught_up change
        self.faults = faults
        self.ws = None
        self.connected = False
        self.caught_up = False
        self.day = None              # day of the last applied output
        self.last_seq = 0            # seq of the last applied output (0 = none today)
        self.pending = {}            # prev_seq -> record waiting for its predecessor
        self.pending_since = None
        self.last_arrival = 0
        self._stop = False

    # -- public ---------------------------------------------------------
    async def send(self, msg: dict) -> bool:
        if not self.connected or self.ws is None:
            return False
        try:
            await self.ws.send(json.dumps(msg))
            return True
        except websockets.ConnectionClosed:
            return False

    async def disconnect(self):
        if self.ws is not None:
            await self.ws.close()

    async def run(self):
        backoff = 0.2
        while not self._stop:
            try:
                async with websockets.connect(self.s.b_url, open_timeout=3) as ws:
                    backoff = 0.2
                    await self._session(ws)
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
                log.warning("B connection failed or closed: %s", e)
            finally:
                self.ws = None
                self._set(connected=False, caught_up=False)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5)

    def stop(self):
        self._stop = True

    # -- internals ------------------------------------------------------
    def _set(self, **kw):
        changed = any(getattr(self, k) != v for k, v in kw.items())
        for k, v in kw.items():
            setattr(self, k, v)
        if changed:
            self.on_state()

    async def _session(self, ws):
        if self.day != today():          # nothing applied today: start from the first output
            self.day, self.last_seq = None, 0
        self.pending.clear()
        self.pending_since = None
        await ws.send(json.dumps({"type": "resume", "from_seq": self.last_seq}))
        self.ws = ws
        self.last_arrival = now_ms()
        self._set(connected=True, caught_up=False)
        log.info("connected to B, resume from_seq=%s", self.last_seq)
        tick = self.s.catchup_quiet_ms / 1000 / 3
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=tick)
            except asyncio.TimeoutError:
                raw = None
            if raw is not None:
                self._receive(json.loads(raw))
            self._drain()
            self._check_caught_up()
            if self.pending and now_ms() - self.pending_since > self.s.gap_timeout_ms:
                log.error("gap in B's output chain did not fill, reconnecting")
                return

    def _receive(self, rec: dict):
        if rec.get("type") == "error":
            log.error("B error: %s %s", rec.get("code"), rec.get("message"))
            if rec.get("code") == "resume_ahead_of_store":
                self.day, self.last_seq = None, 0
            return
        self.last_arrival = now_ms()
        day, seq = rec["day"], rec["seq"]
        if self.day is not None and day < self.day:
            return
        if self.day is not None and day > self.day:      # a new trading day started
            self.day, self.last_seq = day, 0
            self.pending = {k: v for k, v in self.pending.items() if v["day"] == day}
        if self.day == day and seq <= self.last_seq:
            return                                        # already applied
        self.pending.setdefault(rec["prev_seq"], rec)
        if self.pending_since is None:
            self.pending_since = now_ms()

    def _drain(self):
        if self.faults.pause_b_apply:
            self._set(caught_up=False)
            return
        while self.last_seq in self.pending:
            rec = self.pending.pop(self.last_seq)
            self.on_record(rec)
            self.day, self.last_seq = rec["day"], rec["seq"]
        self.pending_since = now_ms() if self.pending else None

    def _check_caught_up(self):
        quiet = now_ms() - self.last_arrival >= self.s.catchup_quiet_ms
        if not self.caught_up and not self.pending and quiet and not self.faults.pause_b_apply:
            self._set(caught_up=True)
