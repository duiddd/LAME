"""D's HTTP API (protocol document, section 2), with every outcome classified for the caller."""
import logging

import httpx

from ...metrics import Metrics
from ...ports import Clock
from .breaker import CircuitBreaker
from .port import Delivery, OrderQuery, Reply

log = logging.getLogger("a.dclient")

# The request never left A, so D certainly did not receive it.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class HttpDClient:
    def __init__(self, base_url: str, connect_timeout_s: float, total_timeout_s: float, breaker: CircuitBreaker,
                 metrics: Metrics, clock: Clock, transport: httpx.AsyncBaseTransport | None = None):
        timeout = httpx.Timeout(total_timeout_s, connect=connect_timeout_s)
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._breaker = breaker
        self._metrics = metrics
        self._clock = clock
        self.on_busy = lambda: None

    async def _command(self, path: str, body: dict, operation: str) -> Reply:
        started = self._clock.now_ms()
        try:
            r = await self._client.post(path, json=body)
        except NOT_SENT as e:
            self._breaker.failure()
            log.warning("%s for %s: no connection to D: %s", operation, body.get("order_id"), type(e).__name__)
            return Reply(Delivery.NOT_TAKEN, type(e).__name__)
        except httpx.HTTPError as e:
            self._breaker.failure()
            log.warning("%s for %s: sent, no reply from D: %s", operation, body.get("order_id"), type(e).__name__)
            return Reply(Delivery.UNKNOWN, type(e).__name__)
        return self._classify(r, started, operation, body.get("order_id"))

    def _classify(self, r: httpx.Response, started: int, operation: str, order_id: str | None) -> Reply:
        error = _error_code(r)
        if r.status_code == 202:
            self._breaker.success()
            self._metrics.d_ack.observe((self._clock.now_ms() - started) / 1000)
            return Reply(Delivery.ACK)
        if r.status_code == 503 and error in ("RISK_UNAVAILABLE", "RISK_BUSY"):
            if error == "RISK_BUSY":
                # D answered, so the link works: overload is load shedding's concern.
                self._breaker.success()
                self.on_busy()
            else:
                self._breaker.failure()
            return Reply(Delivery.NOT_TAKEN, error)
        if r.status_code in (400, 409, 501):
            # An answer about this request says the link works, or a half-open breaker would stick.
            self._breaker.success()
        if r.status_code == 400:
            log.error("%s for %s: D refused it as invalid: %s", operation, order_id, r.text)
            return Reply(Delivery.INVALID, r.text)
        if r.status_code == 409:
            log.error("%s for %s: D reports a conflict: %s", operation, order_id, r.text)
            return Reply(Delivery.CONFLICT, r.text)
        if r.status_code == 501:
            return Reply(Delivery.NOT_IMPLEMENTED, r.text)
        self._breaker.failure()
        log.warning("%s for %s: unexpected status %s from D", operation, order_id, r.status_code)
        return Reply(Delivery.UNKNOWN, f"status {r.status_code}")

    async def place(self, order: dict) -> Reply:
        return await self._command("/v1/orders", order, "place")

    async def cancel(self, user_id: int, order_id: str) -> Reply:
        return await self._command("/v1/cancels", {"user_id": user_id, "order_id": order_id}, "cancel")

    async def modify(self, user_id: int, order_id: str, price: int | None, qty: int) -> Reply:
        return await self._command("/v1/modifies", {"user_id": user_id, "order_id": order_id, "price": price, "qty": qty}, "modify")

    async def query(self, user_id: int, order_id: str) -> OrderQuery | None:
        try:
            r = await self._client.get(f"/v1/orders/{order_id}", params={"user_id": user_id})
        except httpx.HTTPError as e:
            log.warning("query for %s failed: %s", order_id, type(e).__name__)
            return None
        if r.status_code == 404:
            return OrderQuery(None, None, False)
        if r.status_code != 200:
            return None
        body = r.json()
        return OrderQuery(body.get("state"), body.get("last_notify_seq"), True)

    async def rewind(self, from_seq: int) -> bool:
        try:
            r = await self._client.post("/v1/notifications/rewind", json={"from_seq": from_seq})
        except httpx.HTTPError as e:
            log.warning("rewind from %s failed: %s", from_seq, type(e).__name__)
            return False
        return r.status_code == 200

    async def close(self) -> None:
        await self._client.aclose()


def _error_code(r: httpx.Response) -> str | None:
    try:
        return r.json().get("error")
    except ValueError:
        return None
