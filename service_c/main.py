"""FastAPI Application Entrypoint for Service C: News & AI Analyst.

Exposes:
- POST /api/chat (and alias POST /ask) : Grounded RAG market explanation Q&A
- GET  /api/news                       : Recent indexed news feed
- GET  /api/dlq                        : Dead-Letter Queue (news.dlq) inspector
- GET  /api/market                     : Latest cached Service B market snapshots
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

from .config import settings
from .db import NewsDatabase
from .ingestion import NewsIngestionPipeline
from .market_data import market_cache
from .metrics import metrics
from .models import ChatRequest, ChatResponse, DLQItemOut, NewsItemOut
from .rag import RAGEngine

STATIC_DIR = Path(__file__).resolve().parent / "static"

db = NewsDatabase(settings.db_path)
ingestion_pipeline = NewsIngestionPipeline(db)
rag_engine = RAGEngine(db, market_cache)


class RawInjectRequest(BaseModel):
    """Payload for injecting a raw line (valid JSON or malformed poison) into the news pipeline."""

    raw_payload: str = Field(..., description="Raw JSONL line to publish to the news ingestion pipeline")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initializes RabbitMQ topology, replays scripted news, and starts Service B WS consumer."""
    await ingestion_pipeline.init_broker()
    if settings.auto_replay_on_startup:
        await ingestion_pipeline.replay_script_file()
    await market_cache.start_background_consumers()
    rag_engine.check_ollama_health()
    yield
    await market_cache.stop_background_consumers()
    await ingestion_pipeline.close()


app = FastAPI(
    title="LAME Service C — News & AI Market Analyst",
    description=(
        "Off-path RAG market explanation service with scripted news replay, SQLite FTS5 BM25 "
        "indexing, verifiable citations, freshness labels, prompt-injection guardrails, and "
        "Prometheus AI observability."
    ),
    version="1.0.0",
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


@app.post("/api/chat", response_model=ChatResponse)
@app.post("/ask", response_model=ChatResponse)
async def chat_endpoint(payload: ChatRequest, response: Response):
    """Answers a user's market question using grounded FTS5 retrieval + Service B snapshot."""
    result = rag_engine.answer_question(payload)
    if result.status == "SERVICE_UNAVAILABLE":
        response.status_code = 503
    return result


@app.get("/api/news", response_model=list[NewsItemOut])
async def get_news(
    symbol: Optional[str] = Query(default=None, description="Optional symbol filter, e.g. BTC-USDT"),
    limit: int = Query(default=15, ge=1, le=100),
):
    """Returns recent verified news items from the SQLite FTS5 knowledge base."""
    return db.list_recent_news(symbol=symbol, limit=limit)


@app.get("/api/dlq", response_model=list[DLQItemOut])
async def get_dlq(limit: int = Query(default=50, ge=1, le=200)):
    """Returns malformed/poison messages isolated in the Dead-Letter Queue (news.dlq)."""
    return db.list_dlq_items(limit=limit)


@app.get("/api/market")
async def get_market_snapshots():
    """Returns latest cached Service B market snapshots (top-3 bids/asks + last_price)."""
    return {
        "symbols": market_cache.get_all_snapshots(),
        "stale_threshold_seconds": settings.market_stale_threshold_seconds,
    }


@app.post("/api/market/snapshot")
async def push_market_snapshot(payload: dict):
    """Allows pushing a market snapshot (e.g., during integration testing without Service B)."""
    accepted = market_cache.update_snapshot(payload, source=payload.get("source", "service_b_ws"))
    return {
        "accepted": accepted,
        "snapshot": market_cache.get_market_context(payload.get("symbol", "BTC-USDT")).model_dump(),
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
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """Prometheus scrape endpoint exposing RED + AI observability metrics."""
    return PlainTextResponse(
        content=metrics.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
