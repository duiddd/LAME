"""Builds the application. Routers still answering 501 belong to later work."""
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .accounts.router import router as accounts_router
from .auth.router import router as auth_router
from .config import Settings
from .container import Container, build_container
from .errors import install_handlers
from .market.router import router as market_router
from .orders.router import router as orders_router
from .push.router import router as push_router
from .seed import seed

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SEED_PASSWORD_ENV_DEFAULT = "lame-demo-password"


def create_app(container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = container or build_container(Settings())
        app.state.container = c
        await seed(c.engine, c.accounts, c.passwords, c.catalog, c.clock, c.settings.linked_accounts_file,
                   SEED_PASSWORD_ENV_DEFAULT, c.settings.seed_balances)
        yield
        await c.engine.dispose()

    app = FastAPI(title="LAME Service A (Trading & Accounts)", version="0.1.0", lifespan=lifespan)
    install_handlers(app)

    @app.middleware("http")
    async def protect_writes(request: Request, call_next):
        """SameSite does not stop another port on this host, so check Origin on browser writes."""
        if request.method in WRITE_METHODS and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and not _origin_allowed(origin, request):
                return JSONResponse({"reason": "FORBIDDEN_ORIGIN", "message": "This origin may not call the API"}, status_code=403)
            content_type = request.headers.get("content-type", "")
            if int(request.headers.get("content-length") or 0) > 0 and not content_type.startswith("application/json"):
                return JSONResponse({"reason": "INVALID_REQUEST", "message": "Send the body as application/json"}, status_code=415)
        return await call_next(request)

    @app.get("/healthz", tags=["ops"])
    async def healthz(request: Request):
        c: Container = request.app.state.container
        try:
            async with c.engine.connect() as conn:
                await conn.execute(text("SELECT 1 FROM alembic_version"))
        except Exception as e:
            return JSONResponse({"ready": False, "database": str(type(e).__name__)}, status_code=503)
        return {"ready": True}

    for router in (auth_router, accounts_router, orders_router, market_router, push_router):
        app.include_router(router)
    return app


def _origin_allowed(origin: str, request: Request) -> bool:
    if urlsplit(origin).netloc == request.headers.get("host"):
        return True
    return origin in request.app.state.container.settings.origins


app = create_app()
