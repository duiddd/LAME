# Service C: News & AI Analyst — Architecture

**Owner:** Rushabh Mutha · **The one question it answers:** What is happening in the market, and why?

This document describes the design and system architecture of Service C. The technical rationale and alternatives considered for each decision are recorded in [service_c_tech_decisions.md](service_c_tech_decisions.md) (referencing decisions D1–D20).

---

## 1. Role and Scope

Service C is the generative AI intelligence and market explanation layer of LAME. It operates completely **off the critical trading path**: order placement, matching, and fund settlements proceed uninterrupted if Service C slows down, throws errors, or crashes completely.

- **In Scope:**
  - Replay of scripted market news items from a structured file via RabbitMQ with Dead Letter Queue (DLQ) support.
  - Parsing, validation, and deterministic versioned full-text indexing (SQLite FTS5) with symbol tags and temporal metadata.
  - Real-time market context acquisition (top-3 order book snapshot + `last_price` streamed from Service B's port `9999`).
  - Retrieval-Augmented Generation (RAG) market explanation Q&A with verifiable citations, data freshness labels, and strict "insufficient evidence" refusal triggers.
  - Multi-tier prompt injection defense (heuristic structural pre-filter + system role confinement).
  - System-wide Prometheus metrics exposition (request rates, latency, token usage, refusal rate, DLQ dead-letter counts).
- **Out of Scope:**
  - Real-world financial advice, recommendations, or predictive price speculation.
  - Live order routing, balance checks, or risk controls (owned strictly by A, B, and D).
  - Web scraping, uncurated external news APIs, or continuous training/fine-tuning.

---

## 2. System Architecture & Component Diagram

Service C is architectured as a decoupled asynchronous Python application powered by **FastAPI**, **RabbitMQ**, **SQLite (WAL + FTS5)**, and an **Ollama LLM runtime** (`llama3.2:1b` with deterministic greedy decoding).

```
                      ┌─────────────────────────────────────────┐
                      │             Scripted News File           │
                      │          (seed/news_script.jsonl)       │
                      └────────────────────┬────────────────────┘
                                           │ replay stream
                                           ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ Service C Container                                                                    │
│                                                                                        │
│   [News Replay Producer]                                                               │
│            │                                                                           │
│            ▼                                                                           │
│   [RabbitMQ Exchange: news.direct] ──(nack/fail)──► [DLX: news.dlx] ──► [DLQ: news.dlq]│
│            │ (durable, round-robin)                                                    │
│            ▼                                                                           │
│   [News Ingestion Consumer]                                                            │
│            │                                                                           │
│            ▼ (sanitize, validate)                                                      │
│   [SQLite News Database & FTS5 Index] (/data/news_c.db)                                │
│            │                                                                           │
│   ─────────┼─────────────────────────────────────────────────────────────────────────  │
│            │                                                                           │
│   [User / Frontend Terminal]                                                           │
│            │                                                                           │
│            ▼ HTTP POST /api/chat                                                       │
│   [FastAPI Gateway Task]                                                               │
│            │                                                                           │
│            ├─► [Injection Heuristic Filter] (Screens user query)                       │
│            │                                                                           │
│            ├─► [Market Price Cache] ◄──(WebSocket)── Service B (/md/<symbol>:9999)    │
│            │                                                                           │
│            ├─► [FTS5 BM25 Engine] ──► Query top-K relevant articles & timestamps       │
│            │                                                                           │
│            ├─► [Prompt Builder] (Strict JSON Schema + Grounded Context)                │
│            │                                                                           │
│            ├─► [Ollama Client] ──► POST /api/chat (llama3.2:1b, temperature=0.0)       │
│            │                                                                           │
│            └─► [Citation Verifier & Metrics Counter] ──► Prometheus /metrics           │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Core Subsystems

### 3.1 Scripted News Ingestion & DLQ Pipeline

1. **Replay Driver:** News is fed from a deterministic script file (`news_script.jsonl`). Each record contains:
   `{ "news_id": str, "timestamp_ms": int, "symbols": [str], "headline": str, "body": str }`.
2. **RabbitMQ Messaging Bus:**
   - Durable queue `news_tasks` bound to direct exchange `news.direct`.
   - Consumer parses and schema-validates incoming items.
   - If parsing fails, or an unrecoverable payload error is encountered, the message is rejected with `requeue=False`, automatically routing it via `news.dlx` into `news.dlq`.
3. **Storage & FTS5 Index:**
   - Valid items are written into SQLite (`/data/news_c.db`).
   - SQLite FTS5 extension creates a full-text inverted BM25 index on `headline` and `body`, tagged with associated `symbols` and `timestamp_ms`.
   - Index snapshots are tagged with an incrementing `index_version` hash for traceability.

### 3.2 Real-Time Price Ingestion (from Service B)

- Service C runs a background WebSocket client connecting to Service B at `ws://service-b:9999/md/<symbol>`.
- Consumes top-3 depth snapshots and `last_price`.
- Because Service B market snapshots are self-contained and timestamp-sequenced, Service C keeps an in-memory cache of the latest price state per symbol.
- Disconnections trigger exponential reconnection backoff; market price freshness timestamps are tracked to notify users if price feeds are stale.

### 3.3 Prompt Injection Defense

News items and user queries are treated as untrusted inputs:
1. **Pre-Ingestion Heuristic Scan:** Rejects or neutralizes common jailbreaks (e.g., `"ignore previous instructions"`, `"system override"`, prompt leak attempts).
2. **Structural Framing Isolation:** News articles injected into LLM context are strictly delimited inside `<retrieved_evidence id="N">` XML tags. System instructions command the model never to treat text inside `<retrieved_evidence>` as executive instructions.
3. **JSON Output Enforcement:** Ollama's `format` parameter enforces a strict Pydantic JSON schema, completely preventing raw text prompt hijacking from leaking outside structured response blocks.

### 3.4 Retrieval-Augmented Generation (RAG) Engine

When a user submits a query (e.g., *"Why did BTC drop over the last 10 minutes?"*):
1. **Search Query Extraction:** Extract target symbols and temporal boundaries.
2. **FTS5 BM25 Retrieval:** Fetch the top-$K$ ($K=3$) matching news items matching the symbol and time window.
3. **Evidence Sufficiency Check:**
   - If no news items match or BM25 rank score falls below a threshold ($S_{min}$), the pipeline bypasses LLM inference entirely and returns an explicit **"insufficient evidence"** refusal response.
4. **Structured LLM Invocation:**
   - Prompt provides: Current Price Context (from Service B), Retrieved News Text (from SQLite FTS5), and Grounding Instructions.
   - Query dispatched to Ollama with `temperature: 0.0` (greedy decoding for reproducible output).
5. **Post-Generation Citation Verification:**
   - The returned JSON citations are programmatically cross-checked against the retrieved IDs. Any invented citation results in the citation being stripped and flagged.

---

## 4. Communication & Interfaces

### 4.1 Input: HTTP REST API

Service C exposes FastAPI REST endpoints on port `8080`:

| Endpoint | Method | Payload / Query | Description |
|---|---|---|---|
| `/api/chat` (or `/ask`) | `POST` | `{"query": str, "symbol": Optional[str]}` | Ask market analyst question. Returns structured JSON answer, citations, freshness, and refusal flag. |
| `/api/news` | `GET` | `?symbol=BTC-USDT&limit=15` | Retrieve recent ingested news items (with `index_version` and `sanitized` flags). |
| `/api/dlq` | `GET` | `?limit=50` | Inspect current contents of the Dead Letter Queue (`news.dlq`). |
| `/api/market` | `GET` | — | Inspect latest cached Service B top-3 depth and `last_price` snapshots. |
| `/api/replay` | `POST` | — | Trigger idempotent scripted news replay from `seed/news_script.jsonl`. |
| `/api/metrics/summary` | `GET` | — | JSON summary of AI metrics (latency p50/p95, tokens, refusal rate, DLQ count). |
| `/` (or `/chatbot`) | `GET` | — | Dedicated full-page Service C AI Market Analyst & News Terminal HTML UI. |
| `/widget` | `GET` | — | Compact embeddable Chat Box HTML view built for Service A's frontend. |
| `/healthz` | `GET` | — | Health check (returns DB, RabbitMQ, Service B cache, and Ollama status). |
| `/metrics` | `GET` | — | Prometheus metrics scrape endpoint. |

#### Structured Chat Response Schema
```json
{
  "answer": "Current BTC-USDT snapshot from Service B is 64,250 USDT (best bid 64,245, best ask 64,255) (price freshness: 0.4s ago). BTC-USDT declined by 2.4% over the past 15 minutes after the regional financial authority announced stricter reserve audit timelines for offshore spot exchanges. [news_20261023_001]",
  "status": "ANSWERED",
  "refusal_reason": null,
  "citations": [
    {
      "news_id": "news_20261023_001",
      "headline": "Regional Financial Regulator Issues Compliance Warning to Unlicensed Spot Venues",
      "timestamp_ms": 1729692000000,
      "news_freshness_seconds": 12.4,
      "bm25_score": 2.4105
    }
  ],
  "market_context": {
    "symbol": "BTC-USDT",
    "last_price": 64250,
    "best_bid": 64245,
    "best_ask": 64255,
    "price_timestamp_ms": 1729692005000,
    "data_freshness_seconds": 0.45,
    "is_stale": false,
    "source": "service_b_ws"
  },
  "news_as_of_ms": 1729692000000,
  "news_freshness_seconds": 12.4,
  "index_version": 5,
  "telemetry": {
    "latency_ms": 18.4,
    "llm_inference_ms": 14.2,
    "prompt_tokens": 182,
    "eval_tokens": 46,
    "stripped_hallucinated_citations": 0,
    "engine": "ollama:llama3.2:1b"
  }
}
```

### 4.2 Downstream & Upstream Dependencies

- **Upstream (Service B):** WebSocket connection to `ws://service-b:9999/md/<symbol>` (read-only consumer).
- **Internal (RabbitMQ):** AMQP connection to `amqp://guest:guest@rabbitmq:5672/`.
- **Internal (Ollama):** HTTP connection to `http://ollama:11434/api/chat`.
- **Downstream (Frontend / Service A):** Serves the standalone chatbot terminal (`/`) and embeddable compact chat box (`/widget` and `/static/lame-chat-widget.js`) plus CORS-enabled JSON endpoints (`/api/chat`, `/api/news`). See [service_c_frontend_integration.md](service_c_frontend_integration.md) for Service A's integration guide.
- **Monitoring (Prometheus):** Scraped via HTTP `GET /metrics` on port `8080`.

### 4.3 Service A Frontend Integration Summary

Service A (Pin-Yun) owns the main trading terminal shell and integrates Service C's **Small Chat Box** and **News Feed** via any of three supported patterns (full guide in [service_c_frontend_integration.md](service_c_frontend_integration.md)):

1. **Drop-in JS Widget (`lame-chat-widget.js`):**
   ```html
   <div id="lame-ai-chatbox" data-api-url="http://localhost:8080" data-symbol="BTC-USDT"></div>
   <script src="http://localhost:8080/static/lame-chat-widget.js"></script>
   ```
2. **Compact Iframe (`/widget`):**
   ```html
   <iframe src="http://localhost:8080/widget" width="340" height="420" style="border:none;border-radius:10px;"></iframe>
   ```
3. **Direct REST API Fetch (`POST /api/chat` & `GET /api/news`):**
   CORS is enabled (`Access-Control-Allow-Origin: *`) on Service C port `8080`, allowing Service A's frontend or gateway to call `POST http://localhost:8080/api/chat` and `GET http://localhost:8080/api/news` directly.

---

## 5. Failure Model & Consistency Guarantees

- **Decoupled Isolation:** If Service C fails or crashes, Service A, B, and D experience **zero impact**. Order matching and ledger updates remain at full speed.
- **Eventual Consistency:** News and price explanations are eventually consistent. Answers explicitly declare the exact timestamp and freshness age of both the news evidence and the price snapshot used.
- **At-Least-Once Replay & Idempotent Ingestion:** Replaying the same news script produces deduplicated database rows (keyed by unique `news_id`).
- **Graceful Refusal:** If Ollama is offline or uncommunicative, `/api/chat` responds with a bounded 503 error stating AI services are unavailable, rather than hallucinating or blocking.

---

## 6. How the Design Meets Acceptance Criteria

| Proposal Acceptance Criteria | Architectural Solution |
|---|---|
| **Verifiable Citations without fabrication** | Every citation ID returned by the model is programmatically checked against the retrieved FTS5 article IDs before being served. |
| **Admit Insufficient Evidence** | If BM25 match count is 0 or relevance score is below $S_{min}$, the system immediately triggers the explicit refusal branch. |
| **Prompt Injection Resilience** | Pre-retrieval heuristic screening + structural XML context delimiters + enforced JSON schema generation. |
| **Failure Independence** | Service C runs in an isolated container; core trading services (A, B, D) maintain no synchronous RPC or blocking dependencies on C. |
| **System Monitoring Dashboard** | Service C exposes Prometheus metrics for request rate, error rate, p90/p99 latency, model token evaluation counts, refusal rate, and DLQ length. |

---

## 7. Open Items & Cross-Service Alignment

1. **Service B Price Feed Shape:** Confirmed that Service C consumes only top-3 depth + `last_price` from `9999/md/<symbol>`. No historical OHLCV/candles are expected from B; candle aggregations (if needed) are calculated locally in C from received price snapshots.
2. **Prometheus Scraping Allowlist:** Service B requires allowed CIDRs on port `11659`. Service C's monitoring stack (Prometheus container) must be granted access in Docker Compose network definitions.
