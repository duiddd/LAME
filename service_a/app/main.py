"""Builds the application and runs its background tasks."""
import asyncio
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from .accounts.router import router as accounts_router
from .auth.router import router as auth_router
from .config import Settings
from .container import Container, build_container
from .errors import install_handlers
from .market.feed import follow_market
from .market.router import router as market_router
from .orders.events.router import router as events_router
from .orders.router import router as orders_router
from .push.router import router as push_router
from .seed import seed

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def evaluate_shedding(c: Container) -> None:
    while True:
        await asyncio.sleep(1)
        c.shedder.evaluate()


def start_background(c: Container) -> None:
    for symbol in c.catalog.symbols:
        c.supervisor.start(f"market:{symbol}", lambda s=symbol: follow_market(c.settings.b_md_url, s, c.prices, c.metrics))
    c.supervisor.start("reconcile", c.reconciler.run)
    c.supervisor.start("shedding", lambda: evaluate_shedding(c))


def create_app(container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = container or build_container(Settings())
        app.state.container = c
        await seed(c.engine, c.accounts, c.passwords, c.catalog, c.clock, c.settings.linked_accounts_file,
                   os.getenv("SEED_PASSWORD", "lame-demo-password"), c.settings.seed_balances)
        if c.settings.background_tasks:
            start_background(c)
        yield
        await c.supervisor.stop()
        await c.d.close()
        await c.engine.dispose()

    app = FastAPI(title="LAME Service A (Trading & Accounts)", version="0.1.0", lifespan=lifespan)
    install_handlers(app)

    @app.middleware("http")
    async def protect_and_measure(request: Request, call_next):
        """SameSite does not stop another port on this host, so check Origin on browser writes."""
        path = request.url.path
        if request.method in WRITE_METHODS and path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and not _origin_allowed(origin, request):
                return JSONResponse({"reason": "FORBIDDEN_ORIGIN", "message": "This origin may not call the API"}, status_code=403)
            content_type = request.headers.get("content-type", "")
            if int(request.headers.get("content-length") or 0) > 0 and not content_type.startswith("application/json"):
                return JSONResponse({"reason": "INVALID_REQUEST", "message": "Send the body as application/json"}, status_code=415)
        if path == "/api/stream":
            return await call_next(request)
        c: Container = request.app.state.container
        started = c.clock.now_ms()
        response = await call_next(request)
        route = request.scope.get("route")
        c.metrics.http.labels(getattr(route, "path", "other"), request.method, str(response.status_code)).observe((c.clock.now_ms() - started) / 1000)
        return response

    @app.get("/healthz", tags=["ops"])
    async def healthz(request: Request):
        c: Container = request.app.state.container
        try:
            async with c.engine.connect() as conn:
                await conn.execute(text("SELECT 1 FROM alembic_version"))
        except Exception as e:
            return JSONResponse({"ready": False, "database": type(e).__name__}, status_code=503)
        return {"ready": True, "tasks": c.supervisor.states, "circuit": c.breaker.state, "shedding": c.shedder.active}

    @app.get("/metrics", tags=["ops"])
    async def metrics(request: Request):
        return Response(generate_latest(request.app.state.container.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    for router in (auth_router, accounts_router, orders_router, events_router, market_router, push_router):
        app.include_router(router)
    return app


def _origin_allowed(origin: str, request: Request) -> bool:
    if urlsplit(origin).netloc == request.headers.get("host"):
        return True
    return origin in request.app.state.container.settings.origins


app = create_app()
