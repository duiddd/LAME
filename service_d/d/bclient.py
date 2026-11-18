"""D's single WebSocket connection to B's /d (protocol doc section 4).

- First message is {"type":"resume","from_seq":N}; B resends today's outputs with
  seq > N while it also pushes new ones, so the two can interleave and repeat.
- Outputs are applied strictly in their prev_seq chain order, each exactly once.
- B answers the handshake first with {"type":"resume_ok","from_seq":N,"through_seq":M}:
  M is the last output stored when the handshake was served. D is caught up once
  its chain is contiguous up to M in the day being resent; later records are live.
- Fallback for a B without resume_ok: caught up when the chain is contiguous and
  nothing new has arrived for CATCHUP_QUIET_MS.
- B serves the resend from its current day, and B only moves to a new day when a
  command of the new day arrives (all commands come from D). So after a night away
  D may still be served the old day: D keeps its (day, seq) for deduplication and
  asks from seq 0 whenever its own day is not today's date.
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
        self.from_seq = 0            # what the current handshake asked for
        self.through_seq = None      # from B's resume_ok; None until it arrives
        self.resend_day = None       # day of the first output on this connection
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
        # Our day is not today: B may serve that day or a newer one, so ask for all of
        # it. Records we already applied are dropped by (day, seq) in _receive.
        self.from_seq = self.last_seq if self.day == today() else 0
        self.through_seq, self.resend_day = None, None
        self.pending.clear()
        self.pending_since = None
        await ws.send(json.dumps({"type": "resume", "from_seq": self.from_seq}))
        self.ws = ws
        self.connected_at = self.last_arrival = now_ms()
        self._set(connected=True, caught_up=False)
        log.info("connected to B, resume from_seq=%s (last applied %s/%s)", self.from_seq, self.day, self.last_seq)
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
        if rec.get("type") == "resume_ok":
            self.through_seq = rec["through_seq"]
            log.info("B will resend up to seq %s", self.through_seq)
            return
        self.last_arrival = now_ms()
        day, seq = rec["day"], rec["seq"]
        if self.resend_day is None:
            self.resend_day = day
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
        if self.caught_up or self.pending or self.faults.pause_b_apply:
            return
        if self.through_seq is not None:
            done = (self.through_seq <= self.from_seq                       # nothing to resend
                    or (self.resend_day is not None and self.day is not None
                        and (self.day, self.last_seq) >= (self.resend_day, self.through_seq)))
        else:                                                               # B without resume_ok
            done = now_ms() - self.last_arrival >= self.s.catchup_quiet_ms
        if done:
            self._set(caught_up=True)
