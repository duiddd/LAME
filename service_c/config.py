"""Configuration settings for Service C (News & AI Analyst)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    """Environment-driven configuration aligned with service_c_architecture.md."""

    service_name: str = "service-c-news-ai-analyst"
    host: str = os.getenv("SERVICE_C_HOST", "0.0.0.0")
    port: int = int(os.getenv("SERVICE_C_PORT", "8080"))

    # SQLite + FTS5 Knowledge Base
    db_path: str = os.getenv(
        "SERVICE_C_DB_PATH",
        str(BASE_DIR / "data" / "news_c.db"),
    )
    seed_news_path: str = os.getenv(
        "SERVICE_C_SEED_NEWS_PATH",
        str(BASE_DIR / "seed" / "news_script.jsonl"),
    )

    # RabbitMQ / AMQP settings (D2)
    rabbitmq_url: str = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    rabbitmq_enabled: bool = os.getenv("RABBITMQ_ENABLED", "true").lower() in ("1", "true", "yes")
    news_exchange: str = "news.direct"
    news_queue: str = "news_tasks"
    news_routing_key: str = "news.item"
    dlx_exchange: str = "news.dlx"
    dlq_queue: str = "news.dlq"
    dlq_routing_key: str = "news.dead"

    # Service B Market Data WebSocket settings (D8)
    service_b_ws_base: str = os.getenv("SERVICE_B_WS_URL", "ws://localhost:9999/md")
    supported_symbols: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            s.strip()
            for s in os.getenv("SUPPORTED_SYMBOLS", "BTC-USDT,ETH-USDT").split(",")
            if s.strip()
        )
    )
    market_stale_threshold_seconds: float = float(
        os.getenv("MARKET_STALE_THRESHOLD_SECONDS", "3.0")
    )

    # Ollama Local LLM Runtime settings (D4, D5)
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
    ollama_timeout_seconds: float = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "15.0"))
    allow_deterministic_fallback: bool = os.getenv(
        "ALLOW_DETERMINISTIC_LLM_FALLBACK", "true"
    ).lower() in ("1", "true", "yes")

    # RAG & BM25 Retrieval settings (D3, D7)
    rag_top_k: int = int(os.getenv("RAG_TOP_K", "3"))
    bm25_min_score: float = float(os.getenv("BM25_MIN_SCORE", "0.05"))
    auto_replay_on_startup: bool = os.getenv(
        "AUTO_REPLAY_ON_STARTUP", "true"
    ).lower() in ("1", "true", "yes")


settings = Settings()
