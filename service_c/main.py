"""FastAPI Application Entrypoint for Service C: News & AI Analyst.

Exposes:
- POST /api/chat (and alias POST /ask) : Grounded RAG market explanation Q&A
- GET  /api/news                       : Recent indexed news feed (auto-pulls for requested symbol)
- POST /api/activity                   : Record stock/crypto/commodity bought, sold, or searched on A/B/D
- GET  /api/activity                   : Active traded/searched symbols & global exchange catalog
- POST /api/news/seed-global           : Seed Top-10 global exchange stocks, crypto & commodities news
- GET  /api/dlq                        : Dead-Letter Queue (news.dlq) inspector
- GET  /api/market                     : Latest cached Service B & global exchange market snapshots
- POST /api/market/snapshot            : Push/update market snapshot (for testing)
- POST /api/replay                     : Trigger scripted news replay (news_script.jsonl)
- POST /api/news/inject                : Publish raw news payload (valid or poison) to pipeline
- GET  /api/metrics/summary            : JSON summary of AI metrics for UI & Service A
- GET  /healthz                        : Health check (SQLite FTS5, RabbitMQ, Ollama, Service B)
- GET  /metrics                        : Prometheus text exposition endpoint
- GET  / (and /chatbot)                : Dedicated Service C AI Chatbot & News Terminal Web UI
- GET  /widget                         : Compact embeddable Chat Box for Service A's frontend
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .catalog import GLOBAL_INSTRUMENTS
from .config import settings
from .db import NewsDatabase
from .ingestion import NewsIngestionPipeline
from .live_news import ActivityAndNewsManager
from .market_data import market_cache
from .metrics import metrics
from .models import ChatRequest, ChatResponse, DLQItemOut, NewsItemOut
from .observability import observability_aggregator
from .rag import RAGEngine

STATIC_DIR = Path(__file__).resolve().parent / "static"
REPO_ROOT = Path(__file__).resolve().parent.parent

db = NewsDatabase(settings.db_path)
ingestion_pipeline = NewsIngestionPipeline(db)
rag_engine = RAGEngine(db, market_cache)
activity_manager = ActivityAndNewsManager(db)


class RawInjectRequest(BaseModel):
    """Payload for injecting a raw line (valid JSON or malformed poison) into the news pipeline."""

    raw_payload: str = Field(..., description="Raw JSONL line to publish to the news ingestion pipeline")


class SymbolActivityRequest(BaseModel):
    """Payload notified when a user or peer service (A/B/D) buys, sells, views, or searches a symbol."""

    symbol: str = Field(..., description="Ticker or pair bought, sold, or searched (e.g. NVDA, AAPL, BTC-USDT, GOLD)")
    action: str = Field(default="TRADED", description="Action type: BUY, SELL, SEARCHED, VIEWED, LISTED")
    source: str = Field(default="service_a", description="Originating service (service_a, service_b, service_d, frontend)")
    qty: Optional[str | float | int] = Field(default=None, description="Optional order/fill quantity")
    price: Optional[str | float | int] = Field(default=None, description="Optional order/fill price")
    force_live_rss: bool = Field(default=False, description="Force live RSS news pull for this symbol")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initializes RabbitMQ topology, replays scripted news, discovers peer symbols, and starts WS consumer."""
    await ingestion_pipeline.init_broker()
    if settings.auto_replay_on_startup:
        await ingestion_pipeline.replay_script_file()
    activity_manager.discover_local_service_configs(REPO_ROOT)
    await market_cache.start_background_consumers()
    rag_engine.check_ollama_health()
    yield
    await market_cache.stop_background_consumers()
    await ingestion_pipeline.close()


app = FastAPI(
    title="LAME Service C — News & AI Market Analyst",
    description=(
        "Off-path RAG market explanation service with scripted news replay, global multi-exchange "
        "stock/crypto/commodity news ingestion, SQLite FTS5 BM25 indexing, verifiable citations, "
        "freshness labels, prompt-injection guardrails, and Prometheus AI observability."
    ),
    version="1.1.0",
    lifespan=lifespan,
)

# Enable unrestricted CORS so Service A's frontend (on any host/port) can fetch directly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def prometheus_http_middleware(request: Request, call_next):
    """Records HTTP RED request counters per endpoint and status code."""
    response: Response = await call_next(request)
    path = request.url.path
    if not path.startswith("/static"):
        metrics.record_http_request(path, request.method, response.status_code)
    return response


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/chatbot", response_class=HTMLResponse, include_in_schema=False)
async def serve_chatbot_page():
    """Serves the dedicated full-page AI Market Analyst Chatbot & News Terminal."""
    html_path = STATIC_DIR / "chatbot.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="chatbot.html not found")
    return FileResponse(html_path, media_type="text/html")


@app.get("/widget", response_class=HTMLResponse, include_in_schema=False)
async def serve_widget_page():
    """Serves the compact embeddable Chat Box designed for Service A's frontend."""
    widget_path = STATIC_DIR / "widget.html"
    if not widget_path.exists():
        raise HTTPException(status_code=404, detail="widget.html not found")
    return FileResponse(widget_path, media_type="text/html")


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
@app.get("/observability", response_class=HTMLResponse, include_in_schema=False)
async def serve_observability_dashboard():
    """Serves the standalone LAME Observability Dashboard HTML page."""
    dash_path = STATIC_DIR / "dashboard.html"
    if not dash_path.exists():
        raise HTTPException(status_code=404, detail="dashboard.html not found")
    return FileResponse(dash_path, media_type="text/html")


@app.post("/api/chat", response_model=ChatResponse)
@app.post("/ask", response_model=ChatResponse)
async def chat_endpoint(payload: ChatRequest, response: Response):
    """Answers a user's market question using grounded FTS5 retrieval + live market snapshot."""
    # Ensure any stock, crypto, or commodity mentioned in the query or symbol field has its news pulled
    activity_manager.db = db
    activity_manager.record_query_activity(payload.query, payload.symbol, source="chat")
    result = rag_engine.answer_question(payload)
    if result.status == "SERVICE_UNAVAILABLE":
        response.status_code = 503
    return result


@app.get("/api/news", response_model=list[NewsItemOut])
async def get_news(
    symbol: Optional[str] = Query(default=None, description="Optional symbol filter, e.g. BTC-USDT, NVDA, AAPL, GOLD"),
    limit: int = Query(default=20, ge=1, le=100),
    include_global: bool = Query(default=False, description="Seed global multi-exchange top-10 stocks & commodities news"),
):
    """Returns recent verified news items from the SQLite FTS5 knowledge base."""
    activity_manager.db = db
    if include_global:
        activity_manager.seed_global_exchange_news()
    if symbol and symbol.strip():
        activity_manager.record_symbol_activity(symbol, action="viewed", source="news_feed")
        activity_manager.ensure_symbol_news(symbol)
    return db.list_recent_news(symbol=symbol, limit=limit)


@app.post("/api/activity")
async def record_activity(req: SymbolActivityRequest):
    """Notifies Service C that a stock, crypto, script, or commodity was bought, sold, or searched on A/B/D."""
    activity_manager.db = db
    canonical = activity_manager.record_symbol_activity(
        req.symbol,
        action=req.action,
        source=req.source,
        qty=req.qty,
        price=req.price,
    )
    if req.price is not None:
        try:
            p_int = round(float(req.price))
            market_cache.update_snapshot(
                {"symbol": canonical, "last_price": p_int, "timestamp_ms": int(__import__("time").time() * 1000)},
                source="service_b_ws",
            )
        except Exception:
            pass
    news_count = activity_manager.ensure_symbol_news(canonical, force_live_rss=req.force_live_rss)
    preview = [item.model_dump() for item in db.list_recent_news(symbol=canonical, limit=5)]
    return {
        "status": "ok",
        "symbol": canonical,
        "news_available": news_count,
        "news_preview": preview,
        "index_version": db.get_index_version(),
        "active_symbols": activity_manager.get_active_symbols(limit=15),
    }


@app.get("/api/activity")
async def get_activity(limit: int = Query(default=30, ge=1, le=100)):
    """Returns actively bought/sold/searched symbols across services and the global multi-exchange catalog."""
    activity_manager.db = db
    by_exchange: dict[str, list[dict]] = {}
    for sym, meta in GLOBAL_INSTRUMENTS.items():
        exch = meta["exchange"]
        by_exchange.setdefault(exch, []).append(
            {
                "symbol": sym,
                "name": meta["name"],
                "exchange": exch,
                "asset_class": meta["asset_class"],
                "price": meta["price"],
            }
        )
    return {
        "active_symbols": activity_manager.get_active_symbols(limit=limit),
        "exchanges": by_exchange,
        "total_catalog_instruments": len(GLOBAL_INSTRUMENTS),
        "global_catalog_size": len(GLOBAL_INSTRUMENTS),
        "index_version": db.get_index_version(),
    }


@app.post("/api/news/seed-global")
async def seed_global_news():
    """Seeds the full Top-10 stocks per major global exchange, crypto, and commodities news corpus."""
    activity_manager.db = db
    inserted = activity_manager.seed_global_exchange_news()
    return {
        "inserted_new": inserted,
        "index_version": db.get_index_version(),
        "total_instruments": len(GLOBAL_INSTRUMENTS),
    }


@app.get("/api/dlq", response_model=list[DLQItemOut])
async def get_dlq(limit: int = Query(default=50, ge=1, le=200)):
    """Returns malformed/poison messages isolated in the Dead-Letter Queue (news.dlq)."""
    return db.list_dlq_items(limit=limit)


@app.get("/api/market")
async def get_market_snapshots():
    """Returns latest cached Service B & global exchange market snapshots."""
    return {
        "symbols": market_cache.get_all_snapshots(),
        "stale_threshold_seconds": settings.market_stale_threshold_seconds,
    }


@app.get("/api/market/snapshot")
async def get_single_market_snapshot(symbol: str = Query(default="BTC-USDT")):
    """Returns the top-3 order book and last price snapshot for any crypto, stock, or commodity."""
    activity_manager.db = db
    canonical = activity_manager.record_symbol_activity(symbol, action="viewed", source="terminal_ob")
    return market_cache.get_market_context(canonical).model_dump()


@app.post("/api/market/snapshot")
async def push_market_snapshot(payload: dict):
    """Allows pushing a market snapshot (e.g., during integration testing or from Service B/A)."""
    sym = payload.get("symbol", "BTC-USDT")
    activity_manager.db = db
    activity_manager.record_symbol_activity(sym, action="market_tick", source=payload.get("source", "service_b_ws"))
    accepted = market_cache.update_snapshot(payload, source=payload.get("source", "service_b_ws"))
    return {
        "accepted": accepted,
        "snapshot": market_cache.get_market_context(sym).model_dump(),
    }


@app.post("/api/replay")
async def trigger_news_replay():
    """Replays `seed/news_script.jsonl` idempotently through the ingestion pipeline."""
    return await ingestion_pipeline.replay_script_file()


@app.post("/api/news/inject")
async def inject_raw_news(req: RawInjectRequest):
    """Publishes a raw JSONL string to test live news ingestion or DLQ poison isolation."""
    outcome = await ingestion_pipeline.publish_raw_line(req.raw_payload)
    return {
        "outcome": outcome,
        "index_version": db.get_index_version(),
        "dlq_count": len(db.list_dlq_items()),
    }


@app.get("/api/metrics/summary")
async def get_metrics_summary():
    """Returns structured JSON summary of AI metrics (latency, tokens, refusal rate, DLQ)."""
    return metrics.summary_dict()


@app.get("/api/observability")
async def get_system_observability():
    """Aggregates RED metrics (rate, errors, latency) across A/B/D/C plus AI token cost."""
    return await observability_aggregator.collect()


@app.get("/healthz")
async def healthcheck():
    """Returns component health for Docker Compose and Service A integration."""
    index_version = db.get_index_version()
    dlq_count = len(db.list_dlq_items())
    return {
        "status": "ok",
        "service": settings.service_name,
        "sqlite_fts5": {
            "healthy": True,
            "db_path": str(db.db_path),
            "index_version": index_version,
        },
        "rabbitmq": {
            "broker_mode": ingestion_pipeline.broker_mode,
            "connected": ingestion_pipeline.rabbitmq_connected,
            "dlq_count": dlq_count,
        },
        "ollama": {
            "base_url": settings.ollama_base_url,
            "model": settings.ollama_model,
            "reachable": rag_engine.ollama_reachable,
            "deterministic_fallback_enabled": settings.allow_deterministic_fallback,
        },
        "market_data": market_cache.get_all_snapshots(),
        "active_symbols_count": len(activity_manager.get_active_symbols()),
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """Prometheus scrape endpoint exposing RED + AI observability metrics."""
    return PlainTextResponse(
        content=metrics.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
