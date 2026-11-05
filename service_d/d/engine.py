"""D's core: takes A's commands, decides, talks to B, and turns B's outputs into notifications.

Everything runs in one asyncio loop. SQLite calls are synchronous and short, and
every state change commits together with the notifications it produces (RD8, RD25).
"""
import asyncio
import logging

from .bclient import BClient, now_ms
from .book import BookView
from .faults import Faults
from .metrics import Metrics
from .notifier import Notifier
from .rules import Opposite, decide
from .store import Store

log = logging.getLogger("d.engine")

FINAL = {"FILLED", "CANCELLED", "REJECTED_BY_B", "NOT_DELIVERED"}
WAITING_FOR_B = {"IN_FLIGHT", "TIMED_OUT"}
OPPOSITE = {"BUY": "SELL", "SELL": "BUY"}


class Engine:
    def __init__(self, settings, static, a_transport=None):
        self.s = settings
        self.cfg = static
        self.store = Store(settings.data_dir)
        self.faults = Faults()
        self.metrics = Metrics()
        self.b_client = BClient(settings, self.on_record, self.on_b_state, self.faults)
        self.notifier = Notifier(self.store, settings.a_notify_url, self.faults, self.metrics, a_transport)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready_event = asyncio.Event()
        self.connected_at = 0
        self.b = {}          # b_order_id -> row of b_orders (kept in sync with the table)
        self.view = BookView()
        self.cursor = tuple(self.store.get_meta("b_cursor", ["", 0]))  # last B output already turned into notifications
        self.tasks = []

    # ================================================================ lifecycle
    def load(self):
        for row in self.store.all("SELECT * FROM b_orders"):
            self.b[row["b_order_id"]] = row
            if row["order_seq"] is not None:
                self.view.by_seq[(row["b_day"], row["order_seq"])] = row["b_order_id"]
        for row in self.store.all("SELECT order_id FROM orders WHERE state = 'RECEIVED' ORDER BY received_at"):
            self.queue.put_nowait(row["order_id"])

    async def start(self):
        self.load()
        self.tasks = [asyncio.create_task(t) for t in
                      (self.b_client.run(), self.notifier.run(), self._decide_loop(), self._schedule_loop())]

    async def stop(self):
        self.b_client.stop()
        await self.b_client.disconnect()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.notifier.close()
        self.store.db.close()

    @property
    def ready(self) -> bool:
        return self.b_client.connected and self.b_client.caught_up and self.store.write_ok

    def health(self) -> dict:
        return {"ready": self.ready, "config_loaded": True, "b_connected": self.b_client.connected,
                "caught_up": self.b_client.caught_up, "storage_ok": self.store.write_ok,
                "links_version": self.cfg.links_version, "rule_version": self.cfg.rule_version}

    def on_b_state(self):
        if self.b_client.connected and not self.connected_at:
            self.connected_at = now_ms()
        if not self.b_client.connected:
            self.connected_at = 0
        self.metrics.b_connected.set(int(self.b_client.connected))
        self.metrics.ready.set(int(self.ready))
        (self.ready_event.set if self.ready else self.ready_event.clear)()

    # ================================================================ A -> D: orders
    def validate(self, r: dict):
        sym = self.cfg.symbols.get(r.get("symbol"))
        if sym is None:
            return "unknown symbol"
        if r.get("side") not in ("BUY", "SELL") or r.get("type") not in ("LIMIT", "MARKET"):
            return "side must be BUY/SELL and type LIMIT/MARKET"
        qty = r.get("qty")
        if not isinstance(qty, int) or qty <= 0 or qty % sym["lot"]:
            return "qty must be a positive multiple of the lot"
        price = r.get("price")
        if r["type"] == "LIMIT" and (not isinstance(price, int) or price <= 0 or price % sym["tick"]):
            return "a limit order needs a positive price on the tick grid"
        if r["type"] == "MARKET" and price is not None:
            return "a market order has no price"
        return None

    def submit(self, r: dict):
        if err := self.validate(r):
            return 400, {"error": "INVALID_REQUEST", "message": err}
        old = self.store.one("SELECT * FROM orders WHERE order_id = ?", r["order_id"])
        if old:
            same = all(old[k] == r.get(k) for k in ("user_id", "symbol", "side", "type", "price", "qty"))
            if not same:
                return 409, {"error": "ORDER_ID_CONFLICT"}
            return 202, {"order_id": old["order_id"], "status": old["state"]}
        if not self.ready:
            self.metrics.refused.labels("RISK_UNAVAILABLE").inc()
            return 503, {"error": "RISK_UNAVAILABLE"}
        if self.queue.qsize() >= self.s.decision_queue_cap:
            self.metrics.refused.labels("RISK_BUSY").inc()
            return 503, {"error": "RISK_BUSY"}
        with self.store.tx() as st:
            st.q("INSERT INTO orders(order_id, user_id, symbol, side, type, price, qty, state, remaining_qty, received_at) "
                 "VALUES(?, ?, ?, ?, ?, ?, ?, 'RECEIVED', ?, ?)", r["order_id"], r["user_id"], r["symbol"], r["side"],
                 r["type"], r.get("price"), r["qty"], r["qty"], now_ms())
        self.queue.put_nowait(r["order_id"])
        self.metrics.queue.set(self.queue.qsize())
        return 202, {"order_id": r["order_id"], "status": "RECEIVED"}

    async def _decide_loop(self):
        while True:
            order_id = await self.queue.get()
            self.metrics.queue.set(self.queue.qsize())
            try:
                await self._decide(order_id)
            except Exception:
                log.exception("decision failed for %s", order_id)

    async def _decide(self, order_id: str):
        o = self.store.one("SELECT * FROM orders WHERE order_id = ?", order_id)
        if o is None or o["state"] != "RECEIVED":
            return                                   # cancelled while queued
        deadline = o["received_at"] + self.s.gate_deadline_ms
        if self.faults.decision_delay_ms:
            await asyncio.sleep(self.faults.decision_delay_ms / 1000)
        while not self.ready and now_ms() < deadline:  # fail closed: never decide on stale features
            try:
                await asyncio.wait_for(self.ready_event.wait(), (deadline - now_ms()) / 1000)
            except asyncio.TimeoutError:
                break
        o = self.store.one("SELECT * FROM orders WHERE order_id = ?", order_id)
        if o["state"] != "RECEIVED":
            return
        if self.store.one("SELECT 1 FROM cancels WHERE order_id = ? AND tombstone = 1", order_id):
            reason = "CANCELLED_BEFORE_SUBMIT"
        elif now_ms() > deadline or not self.ready:
            reason = "RISK_TIMEOUT"
        else:
            group = self.cfg.groups.get(o["user_id"], frozenset({o["user_id"]}))
            reason = decide(o, self._opposites(o), group, self.cfg.rules)
        if reason:
            with self.store.tx():
                self._reject_by_d(o, reason)
        else:
            await self._approve_and_send(o)
        self.metrics.decision_seconds.observe((now_ms() - o["received_at"]) / 1000)
        self.metrics.decisions.labels("REJECTED" if reason else "APPROVED", reason or "").inc()

    def _opposites(self, o: dict):
        side = OPPOSITE[o["side"]]
        out = self.view.opposites(o["symbol"], side)
        for b in self.b.values():
            if b["state"] in WAITING_FOR_B and b["symbol"] == o["symbol"] and b["side"] == side:
                out.append(Opposite(b["user_id"], b["price"], b["qty"] - b["filled"], None, True))
        return out

    def _decision_row(self, order_id, decision, reason):
        self.store.q("INSERT INTO decisions(order_id, decision, reason, rule_version, features_day, features_seq, "
                     "decided_at) VALUES(?, ?, ?, ?, ?, ?, ?)", order_id, decision, reason, self.cfg.rule_version,
                     self.b_client.day, self.b_client.last_seq, now_ms())

    def _reject_by_d(self, o: dict, reason: str):
        """Must be called inside a transaction."""
        self._decision_row(o["order_id"], "REJECTED", reason)
        self.store.q("UPDATE orders SET state = 'REJECTED_BY_D', reason = ?, remaining_qty = 0 WHERE order_id = ?",
                     reason, o["order_id"])
        self._notify("RISK_REJECTED", o, {"reason": reason, "rule_version": self.cfg.rule_version})

    async def _approve_and_send(self, o: dict):
        sent_at = now_ms()
        with self.store.tx() as st:
            self._decision_row(o["order_id"], "APPROVED", None)
            cur = st.q("INSERT INTO b_orders(order_id, user_id, symbol, side, type, price, qty, sent_at, state) "
                       "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'IN_FLIGHT')", o["order_id"], o["user_id"], o["symbol"],
                       o["side"], o["type"], o["price"], o["qty"], sent_at)
            st.q("UPDATE orders SET state = 'IN_FLIGHT' WHERE order_id = ?", o["order_id"])
        b_id = cur.lastrowid
        self.b[b_id] = self.store.one("SELECT * FROM b_orders WHERE b_order_id = ?", b_id)
        msg = {"type": "new_order", "symbol": o["symbol"], "order_id": b_id, "user_id": o["user_id"],
               "side": o["side"], "order_type": o["type"], "qty": o["qty"], "origin": "USER", "sent_at": sent_at}
        if o["price"] is not None:
            msg["price"] = o["price"]
        if not await self.b_client.send(msg):
            log.warning("new_order for b_order_id %s not sent (B disconnected); it will time out", b_id)
        if self.faults.take("disconnect_after_send"):
            await self.b_client.disconnect()

    # ================================================================ A -> D: cancels
    def cancel(self, r: dict):
        order_id, user_id = r["order_id"], r["user_id"]
        old = self.store.one("SELECT * FROM cancels WHERE order_id = ?", order_id)
        if old and old["origin"] == "A":
            return 202, self._cancel_body(old)
        if old and old["result"] is not None:      # D's own timeout cancel, already finished
            self.store.q("DELETE FROM cancels WHERE order_id = ?", order_id)
            old = None
        o = self.store.one("SELECT * FROM orders WHERE order_id = ?", order_id)
        if o and o["user_id"] != user_id:
            return 409, {"error": "ORDER_ID_CONFLICT"}
        now = now_ms()
        with self.store.tx() as st:
            if o is None:
                self._cancel_row(order_id, user_id, "TOMBSTONED", tombstone=1, result="TOMBSTONED")
                self._notify("CANCEL_RESULT", {"order_id": order_id, "user_id": user_id, "symbol": None},
                             {"result": "TOMBSTONED"})
            elif o["state"] in ("RECEIVED", "REJECTED_BY_D"):
                if o["state"] == "RECEIVED":
                    self._reject_by_d(o, "CANCELLED_BEFORE_SUBMIT")
                self._cancel_row(order_id, user_id, "NOT_SENT_TO_B", result="NOT_SENT_TO_B")
                self._notify("CANCEL_RESULT", o, {"result": "NOT_SENT_TO_B"})
            elif o["state"] in FINAL:
                self._cancel_row(order_id, user_id, "ALREADY_FINAL", result="ALREADY_FINAL")
                self._notify("CANCEL_RESULT", o, {"result": "ALREADY_FINAL"})
            else:
                b = self._current_b(order_id)
                if b["state"] in WAITING_FOR_B:
                    action, not_before = "SCHEDULED", b["sent_at"] + self.s.max_command_age_ms + self.s.cancel_margin_ms
                else:
                    action, not_before = ("SENT" if self.ready else "QUEUED"), now
                if old:   # D's own timeout cancel is already planned: A now owns it
                    st.q("UPDATE cancels SET origin = 'A', action = ? WHERE order_id = ?", action, order_id)
                else:
                    self._cancel_row(order_id, user_id, action, b_order_id=b["b_order_id"], not_before=not_before)
        return 202, self._cancel_body(self.store.one("SELECT * FROM cancels WHERE order_id = ?", order_id))

    def _cancel_row(self, order_id, user_id, action, tombstone=0, result=None, b_order_id=None, not_before=None,
                    origin="A"):
        self.store.q("INSERT INTO cancels(order_id, user_id, origin, action, tombstone, b_order_id, send_not_before, "
                     "result, received_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)", order_id, user_id, origin, action,
                     tombstone, b_order_id, not_before, result, now_ms())

    @staticmethod
    def _cancel_body(row: dict) -> dict:
        body = {"user_id": row["user_id"], "order_id": row["order_id"], "action": row["action"]}
        if row["action"] == "SCHEDULED":
            body["send_not_before"] = row["send_not_before"]
        return body

    def _current_b(self, order_id: str) -> dict:
        ids = [k for k, v in self.b.items() if v["order_id"] == order_id]
        return self.b[max(ids)]

    # ================================================================ A -> D: query, modify, rewind
    def query(self, order_id: str, user_id: int):
        o = self.store.one("SELECT * FROM orders WHERE order_id = ? AND user_id = ?", order_id, user_id)
        if o is None:
            return 404, {"error": "ORDER_NOT_FOUND"}
        d = self.store.one("SELECT decision FROM decisions WHERE order_id = ? ORDER BY decision_no DESC LIMIT 1",
                           order_id)
        return 200, {"order_id": order_id, "user_id": user_id, "state": o["state"],
                     "decision": d["decision"] if d else None, "reason": o["reason"],
                     "filled_qty": o["filled_qty"], "remaining_qty": o["remaining_qty"],
                     "last_notify_seq": o["last_notify_seq"]}

    def modify(self, r: dict):
        # TODO(11-06): modify as cancel + new order inside D (protocol doc 2.3, RD27).
        # Open with Pin-Yun: the new quantity when the old order fills during the cancel,
        # the reply when the old order is already filled, and the loss of time priority.
        return 501, {"error": "NOT_IMPLEMENTED", "message": "modify is pending the 11-06 team discussion"}

    # ================================================================ D -> A: notifications
    def _notify(self, ntype: str, o: dict, fields: dict, b_rec: dict = None):
        """Must be called inside a transaction. `o` needs order_id, user_id and symbol."""
        payload = {"order_id": o.get("order_id"), "user_id": o.get("user_id"), "symbol": o.get("symbol"),
                   "b_day": b_rec["day"] if b_rec else None, "b_seq": b_rec["seq"] if b_rec else None,
                   "ts": now_ms(), **fields}
        self.store.add_notification(ntype, o.get("order_id"), payload, now_ms())
        self.notifier.wake.set()

    # ================================================================ B -> D: outputs
    def on_record(self, rec: dict):
        """Called by the B client once per output, in chain order."""
        replay = (rec["day"], rec["seq"]) <= self.cursor
        b = self.view.resolve(rec, self.b)
        self.view.apply(rec, b)
        if replay:
            return     # already turned into notifications before a restart (RD15)
        with self.store.tx() as st:
            if b is not None or rec["type"] == "trade":
                self._apply(rec, b)
            else:
                log.error("output for an order D does not know: %s", rec)
            self.cursor = (rec["day"], rec["seq"])
            st.set_meta("b_cursor", list(self.cursor))

    def _set_b(self, b: dict, **fields):
        b.update(fields)
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.store.q(f"UPDATE b_orders SET {sets} WHERE b_order_id = ?", *fields.values(), b["b_order_id"])

    def _set_order(self, order_id: str, **fields):
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.store.q(f"UPDATE orders SET {sets} WHERE order_id = ?", *fields.values(), order_id)

    def _order(self, order_id: str) -> dict:
        return self.store.one("SELECT * FROM orders WHERE order_id = ?", order_id)

    def _apply(self, rec: dict, b):
        if rec["type"] == "trade":
            return self._apply_trade(rec)
        o = self._order(b["order_id"])
        status, reason = rec["status"], rec.get("reason")
        intent = self.store.one("SELECT * FROM cancels WHERE order_id = ? AND result IS NULL", o["order_id"])
        if status == "REJECTED" and reason == "ORDER_NOT_FOUND":
            return self._cancel_not_found(o, b, intent, rec)
        first = not b["seen"]
        self._set_b(b, seen=1, filled=rec["filled"])
        filled_total = sum(x["filled"] for x in self.b.values() if x["order_id"] == o["order_id"])
        if status == "ACCEPTED":
            self._set_b(b, state="LIVE", b_day=rec["day"], order_seq=rec["order_seq"])
            self._set_order(o["order_id"], state="LIVE", remaining_qty=rec["remaining"])
            self._notify("ACCEPTED", o, {"order_seq": rec["order_seq"], "rule_version": self.cfg.rule_version}, rec)
            if intent and intent["origin"] == "TIMEOUT":       # B was only slow: drop the automatic cancel
                self.store.q("DELETE FROM cancels WHERE order_id = ?", o["order_id"])
            elif intent:                                        # A's cancel can go now
                self.store.q("UPDATE cancels SET send_not_before = ? WHERE order_id = ?", now_ms(), o["order_id"])
            return
        if first:
            log.warning("first output for b_order_id %s was %s, not ACCEPTED", b["b_order_id"], status)
        update = {"status": status, "reason": reason, "filled_qty": filled_total, "remaining_qty": rec["remaining"]}
        if status in ("PARTIALLY_FILLED", "FILLED"):
            state = "FILLED" if status == "FILLED" else "LIVE"
            self._set_b(b, state=state)
            self._set_order(o["order_id"], state=state, filled_qty=filled_total, remaining_qty=rec["remaining"])
            self._notify("ORDER_UPDATE", o, update, rec)
            if status == "FILLED" and intent:
                self._resolve_cancel(o, intent, "ALREADY_FINAL")
        elif status == "CANCELLED":
            if reason is None and intent and intent["origin"] == "TIMEOUT":
                update["reason"] = "D_TIMEOUT"
            self._set_b(b, state="CANCELLED")
            self._set_order(o["order_id"], state="CANCELLED", reason=update["reason"], filled_qty=filled_total,
                            remaining_qty=0)
            self._notify("ORDER_UPDATE", o, update, rec)
            if intent:
                self._resolve_cancel(o, intent, "CANCELLED")
        elif status == "REJECTED":       # B rejected the new order itself
            update["status"] = "EXPIRED" if reason == "EXPIRED" else "REJECTED"
            self._set_b(b, state="REJECTED_BY_B")
            self._set_order(o["order_id"], state="REJECTED_BY_B", reason=reason, remaining_qty=0)
            self._notify("ORDER_UPDATE", o, update, rec)
            if intent:
                self._resolve_cancel(o, intent, "ALREADY_FINAL")

    def _cancel_not_found(self, o, b, intent, rec):
        """B has no such order. D tells "never reached B" from "already final" by what it has seen (RD13)."""
        if b["seen"]:
            if intent:
                self._resolve_cancel(o, intent, "ALREADY_FINAL")
            return
        self._set_b(b, state="NOT_DELIVERED")
        self._set_order(o["order_id"], state="NOT_DELIVERED", reason="NOT_DELIVERED", remaining_qty=0)
        self._notify("ORDER_UPDATE", o, {"status": "REJECTED", "reason": "NOT_DELIVERED",
                                         "filled_qty": 0, "remaining_qty": 0}, rec)
        if intent:
            self._resolve_cancel(o, intent, "ORDER_NOT_FOUND")

    def _resolve_cancel(self, o, intent, result):
        self.store.q("UPDATE cancels SET result = ? WHERE order_id = ?", result, o["order_id"])
        if intent["origin"] == "A":
            self._notify("CANCEL_RESULT", o, {"result": result})

    def _apply_trade(self, rec: dict):
        sides = {}
        for role in ("taker", "maker"):
            b = self.b.get(self.view.by_seq.get((rec["day"], rec[f"{role}_seq"])))
            if b is None:
                log.error("trade refers to an order D does not know: %s", rec)
                return
            sides[role] = {"order_id": b["order_id"], "user_id": b["user_id"], "side": b["side"]}
        self._notify("TRADE", {"order_id": None, "user_id": None, "symbol": rec["symbol"]},
                     {"price": rec["price"], "qty": rec["qty"], **sides}, rec)

    # ================================================================ timers: timeouts and cancels
    async def _schedule_loop(self):
        while True:
            await asyncio.sleep(0.05)
            try:
                await self._tick()
            except Exception:
                log.exception("scheduler tick failed")

    async def _tick(self):
        now = now_ms()
        if self.ready:
            for b in list(self.b.values()):
                if b["state"] == "IN_FLIGHT" and now >= b["sent_at"] + self.s.d_timeout_ms:
                    self._time_out(b)
            await self._send_due_cancels(now)
        self.metrics.timed_out.set(sum(1 for b in self.b.values() if b["state"] == "TIMED_OUT"))
        self.metrics.cancels_waiting.set(
            self.store.one("SELECT COUNT(*) AS n FROM cancels WHERE result IS NULL AND sent_at IS NULL")["n"])
        self.metrics.outbox_backlog.set(self.notifier.backlog())

    def _time_out(self, b: dict):
        """No output from B within D_TIMEOUT: cancel after the age limit (RD13)."""
        with self.store.tx():
            self._set_b(b, state="TIMED_OUT")
            self._set_order(b["order_id"], state="TIMED_OUT")
            if not self.store.one("SELECT 1 FROM cancels WHERE order_id = ?", b["order_id"]):
                self._cancel_row(b["order_id"], b["user_id"], "SCHEDULED", b_order_id=b["b_order_id"],
                                 not_before=b["sent_at"] + self.s.max_command_age_ms + self.s.cancel_margin_ms,
                                 origin="TIMEOUT")
        log.warning("b_order_id %s timed out; cancel scheduled", b["b_order_id"])

    async def _send_due_cancels(self, now: int):
        # A cancel sent on an earlier connection and still unanswered after catching up was never processed.
        self.store.q("UPDATE cancels SET sent_at = NULL WHERE result IS NULL AND sent_at < ?", self.connected_at)
        due = self.store.all("SELECT * FROM cancels WHERE result IS NULL AND tombstone = 0 AND sent_at IS NULL "
                             "AND send_not_before <= ?", now)
        for c in due:
            b = self.b[c["b_order_id"]]
            if c["origin"] == "TIMEOUT" and b["seen"]:
                self.store.q("DELETE FROM cancels WHERE order_id = ?", c["order_id"])
                continue
            msg = {"type": "cancel_order", "symbol": b["symbol"], "user_id": b["user_id"], "order_id": b["b_order_id"]}
            if await self.b_client.send(msg):
                self.store.q("UPDATE cancels SET sent_at = ? WHERE order_id = ?", now, c["order_id"])
