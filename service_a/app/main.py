"""Builds the application. The routers are stubs until their milestone implements them."""
from fastapi import FastAPI

from .accounts.router import router as accounts_router
from .auth.router import router as auth_router
from .errors import install_handlers
from .market.router import router as market_router
from .orders.router import router as orders_router
from .push.router import router as push_router


def create_app() -> FastAPI:
    app = FastAPI(title="LAME Service A (Trading & Accounts)", version="0.1.0")
    install_handlers(app)
    for router in (auth_router, accounts_router, orders_router, market_router, push_router):
        app.include_router(router)
    return app


app = create_app()
