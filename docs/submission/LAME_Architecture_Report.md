# LAME Architecture Report — Simulated Spot Exchange & AI Market Analyst

**COMP41720 Distributed Systems · Option A (Traditional Distributed System + AI Component)**
**Group:** Pin-Yun Kuo (`A`) · Bo Wu (`B`) · Rushabh Mutha (`C`) · Zongyan Yang (`D`) · **Date:** November 2026

---

## 1. Requirements, Constraints & Architectural Boundaries

**LAME** is a containerized spot exchange for crypto pairs (`BTC-USDT`, `ETH-USDT`) and multi-market reference instruments that combines a strongly consistent trading engine with an off-path generative AI market analyst (`Ollama llama3.2:1b` RAG). Running completely offline via `docker compose up --build`, it satisfies four load-bearing constraints:

1. **Zero Overdraft & Matching Finality:** Spot holds in `Service A` cannot be overdrawn (`CHECK (available >= 0)`). Once an order enters `Service B`, fills are final — so unvetted self-trading or wash-trading orders must be rejected **before** reaching `B`, and ambiguous timeouts must **never** optimistically release held funds.
2. **Deterministic Replay vs. Probabilistic AI:** `B`'s matching engine and `D`'s risk rules (`R1` self-trade, `R2` wash-trade) must replay deterministically bit-for-bit across crashes (`events-<day>.db`, `d.db`). Conversely, `Service C`'s LLM inference is probabilistic and slow (`0.4–3.0+ s`) and processes untrusted news/user text.
3. **Resilience Under Crash & Overload:** `A → D → B` fails closed when risk control is unavailable and sheds load at `A` when tail latency breaches budget, while `C` or `Ollama` failures add **0 ms** and **0 errors** to the trading path.
4. **End-to-End Observability:** Every service exports Prometheus `/metrics`; a centralized Prometheus + Grafana stack (`:9090` / `:3001`) and unified React `/dashboard` surface request rate, errors, `p50/p95/p99` latency, and AI token cost.

*Explicit non-goals:* real fiat custody, leverage/margin liquidation (deferred stretch goal), live internet order routing, and AI-based trade execution or investment advice.

---

## 2. System Architecture, Styles & Service Quanta (C4 View)

LAME is decomposed into **four independent architecture quanta** plus shared infrastructure — each service answers one business question and owns its private persistent store with **zero shared-database coupling**:

```
  [Trader Browser (:3000)]
       │  HTTP/SSE (Nginx /api/*)        │ WS top-3 /md/<pair>        │ REST /api/chat, /news, /observability
       ▼                                  │                            ▼
┌─────────────────────┐                   │                 ┌──────────────────────────────┐
│ Service A (:8000)   │◄──────────┐       │                 │ Service C (:8080) · Off-Path │
│ Trading & Accounts  │  ordered  │       │                 │ News & AI Analyst            │
│ Store: PostgreSQL   │  notify_  │       │                 │ Store: SQLite WAL+FTS5 v_i   │
└─────────┬───────────┘  seq      │       │                 └──┬──────────┬─────────────┬──┘
          │ POST /v1/orders (202) │       │                    │ AMQP     │ HTTP        │ WS /md
          ▼                       │       │                    ▼          ▼             │
┌─────────────────────┐           │       │             [RabbitMQ]   [Ollama LLM]       │
│ Service D (:8400)   ├───────────┘       │             news.dlq     llama3.2:1b        │
│ Pre-Trade Risk Gate │                   │                                             │
│ Store: SQLite d.db  │                   │                 ┌───────────────────────────┴──┐
└─────────┬───────────┘                   └────────────────►│ Service B (:11659 / :9999)   │
          │ single WS /d (cmds/events)                      │ Rust Price-Time Matcher      │
          └────────────────────────────────────────────────►│ Store: events-<day>.db (WAL) │
                                                            └──────────────────────────────┘
  [Prometheus (:9090)] ──scrapes /metrics (5s)──► A, B, D, C ──► [Grafana (:3001)] & [React /dashboard]
```

**Architectural styles & coordination (3C Cube):** On the critical order path, `A` uses **ACID/CP local transactions** for fund holds and communicates with single-instance `D` (orchestrated state machine + outbox) and single-writer `B` (sequential per-pair matchers fed by one journal thread), deliberately avoiding the forbidden *"Async + Atomic + Choreographed"* distributed-2PC quadrant. Settlement follows an **at-least-once ordered notification log** (`notify_seq`) with `SELECT ... FOR UPDATE` cursor locking. Meanwhile, `Service C` acts as an **AP CQRS analytical read quantum** via RabbitMQ + passive WebSocket consumption.

| Component / Quantum | Primary Role | Data Store & Consistency | Wire Contract & Port |
| --- | --- | --- | --- |
| **A: Trading & Accounts** | Auth (Argon2id/JWT), balances, holds, order lifecycle, candles, React shell | PostgreSQL (**CP** holds; eventual settlement & SSE w/ `Last-Event-ID`) | HTTP/SSE via Nginx (`:3000` → `:8000`) |
| **D: Risk Control** | Pure deterministic `R1` (self-trade) & `R2` (wash trade); command proxy & outbox | SQLite WAL (`d.db`); fail-closed gate, ordered outbox | HTTP `:8400` (`A`), WS client to `B` `:11659/d` |
| **B: Matching Engine** | Integer tick/lot price-time matching, write-ahead journal, crash replay | Daily SQLite `events-<day>.db`; total `(day, seq)` order | WS `:11659/d` (`D` only), `:9999/md/<pair>` |
| **C: News & AI Analyst** | News replay + DLQ, FTS5 search, grounded RAG, `/api/observability` | SQLite WAL+FTS5 (`news_c.db`), **AP** `MarketDataCache` | HTTP `:8080`, AMQP `5672`, Ollama `11434` |

---

## 3. Consistency, Failure Handling & Resilience (With Observed Evidence)

Because `D`'s `POST /v1/orders` returns `202` before matching finishes, a timeout leaves order fate ambiguous. Per **ADR-002**, `A` transitions timed-out sends to `UNKNOWN` and keeps funds held, resending the same `order_id` (`5 s` `RESEND_WINDOW`) and issuing a `cancel` (tombstoned by `B` if unseen) before releasing funds. `D` records every `(decision_no, features_seq, rule_version)` atomically before writing to `B`, while `B` commits every command and event to `events-<day>.db` prior to network publish and refuses startup if crash-replay outputs disagree (`match_engine/doc/status.md`).

On market data (`/md/<pair>`), `B` emits self-contained top-3 snapshots (`bids`, `asks`, `last_price`, `last_ms`). Rather than sending a bespoke `"heartbeat": true` flag, `B` re-sends the current snapshot with the **same `last_ms`** every `1 s` when the book is quiet. Both `A` and `C` discard only strictly older timestamps (`ts_ms < existing`), treat equal `last_ms` resends as liveness signals, reset watermarks on reconnect, and flag staleness after `3.0 s` of silence.

**Measured Failure & Benchmark Evidence (`service_a/tools/measure_local.py`, `service_d/README.md`, `service_c` checks):**

| Injected Fault / Benchmark | System Behaviour Observed | Measured Evidence |
| --- | --- | --- |
| **Baseline Order Flow** (`40` orders, Windows laptop) | `A → D → B → D notify → A` live flow + `26` E2E checks (`local_e2e.py`) | `POST` p50 `160.4 ms` / p95 `215.9 ms`; placement→`PROCESSING` p50 `227.6 ms` / p95 `505.1 ms`; `29–46 notify/s` (`~10–15 trades/s`) |
| **Standalone Gate Throughput** (`service_d/tools/loadtest.py`) | Pure in-memory `R1`/`R2` queue (excluding `B`/network) | Decision `p99`: `1 ms` @ `200 orders/s`, `2 ms` @ `1,000 orders/s` |
| **Kill Service D** (`D` unavailable on path) | New orders rejected `503 SYSTEM_BUSY` with `0` held USDT; circuit opens after `5` timeouts | First `5` fails `580–598 ms`; breaker-open fails `15–18 ms`; `D` ready `4.15 s` after restart; `A` accepting at `8.99 s` (`15.1 s` total recovery) |
| **Kill / slow `D`, stop `B` under steady traffic** (`fault_demo.py --compose`, 503 orders) | `D` down: `SYSTEM_BUSY`, `0` held, live dashboard shows `D` OFFLINE and `A` DEGRADED; `D` slowed to `2 s`: `A` sheds load, `D` rejects `RISK_TIMEOUT` (fails closed); `B` down: `NO_FRESH_PRICE` | Refusals in `24 ms` (median) once `D` is known down; accepting `1.2 s` after `D` ready; shedding `4.8 s` after slow-down; `B` refusals after `2.6 s`, accepting `6.3 s` after restart |
| **Consistency shown working** (`consistency_demo.py`) | Double submit → one order, one hold; skipped `notify_seq` → `409` → outbox rewind; `A` killed with `3` outcomes in `D`'s outbox → drained after restart; `D` frozen `3 s` / `8 s` → `UNKNOWN` held, then accepted / compensated by cancel | Books check over `253` orders: ledger = balances, holds = open orders, assets conserved, `A` cursor = `D` outbox head (`730`) — all deviations `0` (`docs/submission/fault_injection.md`) |
| **Drop Notification / Stale Feed / Crash** | `409 expected_seq` rewind; `NO_FRESH_PRICE` after `3 s`; `UNKNOWN` resend reconciliation | Verified via `220` Service A tests (`test_concurrent_holds_never_overdraw`) + Service D test suite (`test_replay.py`, `test_flow.py`) |
| **Poison News / Prompt Injection / Ollama Stall** | Poison JSON routed `nack(requeue=False)` to `news.dlq`; injections/empty hits short-circuit pre-LLM (`0` tokens); `3.0 s` cap prevents pinning | `30/30` Service A↔C integration checks + `7/7` Service C unit tests pass; `A → D → B` experiences `0 ms` delta when `C` stops |

**Scaling bottleneck honestly identified:** `A` applies `D`'s notifications one DB transaction at a time (`18–31 ms`), capping settlement around `10–15 trades/s` on a laptop. Batching contiguous `notify_seq` events in a single transaction preserves strict order while multiplying throughput.

---

## 4. Trade-Off Analysis (Week 2 & Week 10 Method)

Following the 3-step trade-off method (*entangled dimensions → coupling points → scenario pricing*), three foundational trade-offs shaped LAME without relying on invented weighted scores:

### 4.1 Hold Funds on Timeout vs. Optimistic Release (`ADR-002`)
- **Entangled Dimensions & Coupling:** *Fund Liquidity / UX* vs. *Spot Ledger Invariant (`available >= 0`)* across `A`'s PostgreSQL balance lock and `D/B`'s async execution boundary.
- **Concrete Scenarios:** If `A` releases holds after a `2 s` HTTP timeout to `D`, liquidity unlocks immediately — yet an order already accepted by `B` can still fill against re-spent funds, violating `CHECK (available >= 0)` and halting settlement globally. Holding until `D`'s notification (`UNKNOWN` → resend/cancel tombstone) freezes balances during outages (`15.1 s` measured on `D` restart; max `5 s` resend window under live recovery), which recovers automatically with zero human reconciliation.
- **Verdict:** Chose **hold-until-notification**; automated recovery dominates corrupt ledger repair. *(Three things made worse: `UNKNOWN` UI confusion ("Confirming"), temporary fund lock during outages, and extra reconciliation state tests.)*

### 4.2 Synchronous Fail-Closed Risk Gate vs. Async Post-Trade or Shed-to-R1 (`ADR-001`)
- **Entangled Dimensions & Coupling:** *Manipulation Prevention + Replay Determinism* vs. *Availability + Tail Latency* across the single `A → D → B` path.
- **Concrete Scenarios:** Async post-trade checking removes `D` from order `p99`, but self/wash fills settle irreversibly before detection. Weakening `D` under overload by dropping cross-account `R2` while keeping `R1` boosts availability precisely when an attacker can flood the gate to sneak wash trades through. Instead, keeping `D` fast (`1–2 ms` gate `p99`) and shedding new orders at `A` preserves both rules.
- **Verdict:** Chose **fail-closed `R1`+`R2` gate + `A` load shedding**. *(Makes worse: total unavailability of new orders, cancels, and settlements whenever `D` stops; conservative crossing-limit false positives; extra hop latency.)*

### 4.3 Off-Path FTS5 AI Analyst vs. Embedded RAG or Vector Embeddings (`ADR-003`)
- **Entangled Dimensions & Coupling:** *Explanation Richness + Low Container Count* vs. *Trading Latency Isolation + Reproducibility + Trust Containment*.
- **Concrete Scenarios:** Embedding RAG directly inside `A` removes a container and CORS surface, but mixes non-deterministic `0.4–3.0+ s` LLM work, prompt-injection payloads, and news surges into the balance-hold service. Likewise, dense vector embeddings add separate model inference latency, VRAM overhead, and drift across index rebuilds compared to **SQLite FTS5 BM25** inside a single atomic `news_c.db` (`index_version`).
- **Verdict:** Chose **off-path Service C + deterministic FTS5 BM25**. *(Makes worse: eventual consistency up to `3.0 s` snapshot lag + news replay delay; extra RabbitMQ/Ollama containers; keyword sensitivity mitigated via ticker normalization.)*

---

## 5. AI Components and Their Architectural Implications

Integrating local `Ollama (llama3.2:1b)` behind `Service C` introduces four distinct architectural failure modes highlighted in COMP41720 (**Weeks 7, 8, 10** — *4 AI Axes & 5 Omission Clusters*):

1. **Autonomy vs. Safety & Trust Boundary (Tenancy/Trust Cluster):** Both user queries and external scripted/fetched news feeds are untrusted text capable of prompt injection or fabricated advice. `Service C` enforces a three-layer defence (`security.py` + `rag.py`): (a) regex pre-screening blocks injection overrides and investment-advice requests pre-LLM (`REJECTED_UNSAFE`), and sanitizes embedded injections on ingestion; (b) retrieved news is wrapped inside `<retrieved_evidence id="...">` XML delimiters explicitly labelled untrusted data; and (c) a post-generation verifier strips any `citations` ID not returned by FTS5.
2. **Determinism vs. Creativity (Change Cluster):** Exchanges demand traceable evidence. `Service C` pins `temperature = 0.0`, passes `LLMStructuredOutput` JSON Schema via Ollama's constrained `format` parameter, stamps every reply with FTS5 `index_version`, and runs a deterministic BM25 gate (`bm25_min_score = 0.05`) that short-circuits to `INSUFFICIENT_EVIDENCE` when relevant news is absent.
3. **Accuracy vs. Latency (Failure & Invalidation Clusters):** LLM brownouts (*"slow is worse than dead"*) can exhaust FastAPI worker pools. `C` bounds Ollama HTTP calls to `3.0 s`, serves either an honest `HTTP 503` or configurable deterministic synthesis fallback on timeout, and surfaces `data_freshness_seconds`, `news_freshness_seconds`, and `is_stale` on every answer.
4. **Spend Is Load (Cost Cluster):** Even local models consume bounded CPU/GPU budget, and cloud parity requires explicit unit economics. `Service C` short-circuits empty/unsafe queries **before** invoking Ollama (tracking `lame_c_tokens_saved_by_gates_total` at `~350 tokens/request` avoided) and exports `lame_c_tokens_total{type="prompt|eval"}` plus `lame_c_estimated_token_cost_usd` (`$0.10 / 1M` input, `$0.30 / 1M` output reference baseline alongside `$0.00` local Ollama bill).

---

## 6. Cost, Deployment & Observability View

Everything launches via one root `docker-compose.yml` spanning dedicated volumes per quantum (`service_a_pgdata`, `service_b_data`, `service_d_data`, `service_c_data`, `ollama_models`, `prometheus_data`, `grafana_data`). Running `llama3.2:1b` locally eliminates per-token cloud bills during bursty load tests at the cost of local container RAM (`~2 GB` Ollama, `512 MB` capped for `service-b`).

`Prometheus (:9090)` scrapes `/metrics` from all four services every `5 s` into a pre-provisioned `Grafana (:3001)` dashboard (`LAME System & AI Observability`), mirrored in the shared React UI at `/dashboard` (via Service C's `/api/observability` aggregator):
- **Traffic & Errors:** Per-service `req/s`, cumulative counts, HTTP `4xx/5xx`, `A` circuit/shedding (`lame_a_circuit_state`, `lame_a_shedding_active`), `B` bad frames/overflows, `D` `503` refusals, and `C` RabbitMQ `news.dlq` depth.
- **Latency:** `A` HTTP & order-completion histograms, `D` gate decision histograms, `C` end-to-end chat & pure LLM inference histograms (`B` omits per-order timers per D23 to protect hot-path overhead, exporting startup replay duration instead).
- **AI Telemetry:** Prompt/eval token counts, estimated equivalent & direct USD token spend, pre-LLM gate token savings, refusal rate breakdown, and stripped hallucinated citations.
