"""HTTP interface on :8400 (protocol doc section 2)."""
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel


class OrderIn(BaseModel):
    order_id: str
    user_id: int
    symbol: str
    side: Literal["BUY", "SELL"]
    type: Literal["LIMIT", "MARKET"]
    price: Optional[int] = None
    qty: int


class CancelIn(BaseModel):
    user_id: int
    order_id: str


class ModifyIn(BaseModel):
    user_id: int
    order_id: str
    price: Optional[int] = None
    qty: int


class RewindIn(BaseModel):
    from_seq: int


class FaultsIn(BaseModel):
    decision_delay_ms: Optional[int] = None
    pause_b_apply: Optional[bool] = None
    disconnect_after_send: Optional[bool] = None
    skip_next_notification: Optional[bool] = None


def create_app(engine, manage_lifecycle: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_):
        if manage_lifecycle:
            await engine.start()
        yield
        if manage_lifecycle:
            await engine.stop()

    app = FastAPI(title="LAME Service D (Risk Control)", lifespan=lifespan)

    def reply(result):
        status, body = result
        return JSONResponse(body, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def bad_request(_: Request, exc: RequestValidationError):
        return JSONResponse({"error": "INVALID_REQUEST", "message": str(exc.errors())}, status_code=400)

    @app.post("/v1/orders")
    async def place(o: OrderIn):
        return reply(engine.submit(o.model_dump()))

    @app.post("/v1/cancels")
    async def cancel(c: CancelIn):
        return reply(engine.cancel(c.model_dump()))

    @app.post("/v1/modifies")
    async def modify(m: ModifyIn):
        return reply(engine.modify(m.model_dump()))

    @app.get("/v1/orders/{order_id}")
    async def query(order_id: str, user_id: int):
        return reply(engine.query(order_id, user_id))

    @app.post("/v1/notifications/rewind")
    async def rewind(r: RewindIn):
        engine.notifier.rewind(r.from_seq)
        return {"delivered_seq": engine.notifier.delivered}

    @app.get("/healthz")
    async def healthz():
        h = engine.health()
        return JSONResponse(h, status_code=200 if h["ready"] else 503)

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(engine.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    if engine.s.faults_enabled:
        @app.post("/admin/faults")
        async def faults(f: FaultsIn):
            for k, v in f.model_dump(exclude_none=True).items():
                setattr(engine.faults, k, v)
            return vars(engine.faults)

    return app
