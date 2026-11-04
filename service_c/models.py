"""Pydantic schemas and data contracts for Service C (D1, D5, D7)."""

from typing import Literal, Optional
from pydantic import BaseModel, Field


class NewsItemIn(BaseModel):
    """Schema for a scripted news event from news_script.jsonl / RabbitMQ."""

    news_id: str = Field(..., min_length=1, description="Globally unique news identifier")
    timestamp_ms: int = Field(..., gt=0, description="Event timestamp in Unix ms")
    symbols: list[str] = Field(..., min_length=1, description="Associated trading pairs, e.g. ['BTC-USDT']")
    headline: str = Field(..., min_length=3, description="Short news headline")
    body: str = Field(..., min_length=5, description="Full news body text")
    source: str = Field(default="LAME-Wire", description="Wire source label")
    signature: Optional[str] = Field(default=None, description="Optional integrity tag")


class NewsItemOut(NewsItemIn):
    """Stored news record returned by GET /api/news."""

    index_version: int = Field(..., description="Knowledge-base version when indexed")
    sanitized: bool = Field(
        default=False,
        description="True if embedded prompt-injection patterns were neutralized on ingest",
    )


class DLQItemOut(BaseModel):
    """Dead-letter queue record returned by GET /api/dlq."""

    dlq_id: int
    raw_payload: str
    error_reason: str
    failed_at_ms: int
    routing_key: str = "news.dead"


class OrderBookLevel(BaseModel):
    """Single price-qty level in Service B's top-3 market snapshot."""

    price: int
    qty: int


class MarketSnapshot(BaseModel):
    """Market snapshot from Service B (/md/<symbol>:9999) (Decision D8)."""

    symbol: str
    timestamp_ms: int
    last_price: Optional[int] = None
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)
    source: Literal["service_b_ws", "simulated_fallback"] = "simulated_fallback"


class MarketContextOut(BaseModel):
    """Freshness-labelled market context attached to every /api/chat response."""

    symbol: str
    last_price: Optional[int] = None
    best_bid: Optional[int] = None
    best_ask: Optional[int] = None
    price_timestamp_ms: int
    data_freshness_seconds: float
    is_stale: bool = False
    source: str = "simulated_fallback"


class CitationOut(BaseModel):
    """Verified citation pointing to a retrieved FTS5 news item."""

    news_id: str
    headline: str
    timestamp_ms: int
    news_freshness_seconds: float
    bm25_score: float


class LLMStructuredOutput(BaseModel):
    """Strict JSON schema enforced on Ollama /api/chat output (Decision D5)."""

    answer: str = Field(
        ...,
        description="Factual explanation of market activity grounded strictly in retrieved evidence and market snapshot. Never give investment advice.",
    )
    cited_news_ids: list[str] = Field(
        default_factory=list,
        description="Exact news_id strings from <retrieved_evidence> used to support the answer.",
    )
    sufficient_evidence: bool = Field(
        ...,
        description="True only if the retrieved evidence directly answers the user's question.",
    )


class ChatRequest(BaseModel):
    """Payload for POST /api/chat (and alias POST /ask)."""

    query: str = Field(..., min_length=1, max_length=1000, description="User question about market movement")
    symbol: Optional[str] = Field(default=None, description="Optional trading pair context, e.g. BTC-USDT")


class ChatResponse(BaseModel):
    """Structured response returned by POST /api/chat to Service A & Chatbot UI."""

    answer: str
    status: Literal["ANSWERED", "INSUFFICIENT_EVIDENCE", "REJECTED_UNSAFE", "SERVICE_UNAVAILABLE"]
    refusal_reason: Optional[str] = None
    citations: list[CitationOut] = Field(default_factory=list)
    market_context: Optional[MarketContextOut] = None
    news_as_of_ms: Optional[int] = None
    news_freshness_seconds: Optional[float] = None
    index_version: int
    telemetry: dict = Field(
        default_factory=dict,
        description="Per-request latency_ms, llm_inference_ms, prompt_tokens, eval_tokens, engine",
    )
