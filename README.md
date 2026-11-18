# LAME — Simulated Spot Exchange & AI Market Analyst

**COMP41720 Distributed Systems · Option A (Traditional Distributed System + AI Component)**
**Team:** Pin-Yun Kuo (Service A & Frontend Shell) · Bo Wu (Service B) · Rushabh Mutha (Service C, Compose & Observability) · Zongyan Yang (Service D)

LAME is a containerized simulated spot exchange separating a strongly consistent, deterministic trading core (**A → D → B**) from an asynchronous, off-path **News & AI Market Analyst (Service C)** and a unified **Prometheus + Grafana + Web Observability Dashboard**.

---

## Architecture Overview

| Service | Owner | Core Question | Store & Model | Ports |
| --- | --- | --- | --- | --- |
| **Frontend (React + Nginx)** | Pin-Yun Kuo + Rushabh Mutha | Unified trader & analyst terminal | Static bundle + Nginx reverse proxy to `A:8000`; direct to `B:9999` & `C:8080` | `3000` (`/trade`, `/news-analyst`, `/dashboard`) |
| **Service A — Trading & Accounts** | Pin-Yun Kuo | Are balances and assets correct? | PostgreSQL (`CP` spot holds via `CHECK (available >= 0)`), JWT, SSE | `8000` (internal network; `/healthz`, `/metrics`) |
| **Service D — Risk Control** | Zongyan Yang | Should this order be allowed? | SQLite WAL (`d.db`), deterministic `R1` self-trade + `R2` wash-trade gate | `8400` (`/v1/orders`, `/healthz`, `/metrics`) |
| **Service B — Matching & Market Data** | Bo Wu | Who trades with whom, at what price? | Rust price-time matcher, daily SQLite `events-<day>.db`, write-ahead journal | `9999` (`/md/<pair>` WS), `11659` (`/d`, `/healthz`, `/metrics`) |
| **Service C — News & AI Analyst** | Rushabh Mutha | What is happening in the market, and why? | SQLite WAL + FTS5 BM25 (`news_c.db`), RabbitMQ + DLQ, Ollama (`llama3.2:1b`) | `8080` (`/api/*`, `/dashboard`, `/metrics`) |
| **Prometheus + Grafana** | Rushabh Mutha | What is the cluster's RED + AI token spend state? | Scrapes A, B, D, C every `5s`; pre-provisioned Grafana dashboard | `9090` (Prometheus), `3001` (Grafana) |

---

## One-Command Startup

Start the container stack from the repository root:

```bash
docker compose up --build
```

> **Note on Service A (`feature/trading`):** If `service_a/` is on Pin-Yun's `feature/trading` branch rather than your checked-out branch, either merge `feature/trading` and uncomment `- service_a/docker-compose.yml` in `docker-compose.yml`, or run Pin-Yun's combined Windows harness (`service_a/tools/run_local.ps1`). For purely local UI inspection without `service-a`, the frontend container (`:3000`) starts gracefully and supports mock trading telemetry alongside live Service C data.

Pull the local LLM weights once if running full live Ollama inference (otherwise Service C serves deterministic grounded fallback cleanly):

```bash
docker exec -it lame-ollama ollama pull llama3.2:1b
```

---

## Endpoints & Dashboards

- **Shared React Terminal:** `http://localhost:3000`
  - `/trade/BTC-USDT` — Live order book (`B`), candlesticks, order entry (`A`), docked Service C analyst widget
  - `/news-analyst` — Full-page grounded RAG analyst (`POST /api/chat`), FTS5 news feed, RabbitMQ `news.dlq` inspector
  - `/dashboard` — Unified **System & AI Observability Dashboard** (Request Rate, Errors, p50/p95/p99 Latency across A/B/D/C, plus AI Token Usage, Estimated Token Cost USD, Refusal Rate, and Gate Savings)
- **Grafana (Pre-Provisioned):** `http://localhost:3001` *(anonymous Viewer access enabled; default home dashboard: `LAME System & AI Observability`)*
- **Prometheus:** `http://localhost:9090`
- **Service C Standalone Dashboard & API:** `http://localhost:8080/dashboard`, `http://localhost:8080/api/observability`, `http://localhost:8080/metrics`
- **RabbitMQ Management UI:** `http://localhost:15672` (`guest` / `guest`)

---

## Verification & Tests

Run Service C unit tests & cross-service integration check (works offline without Docker via ASGI transport):

```bash
pytest -q service_c/tests/test_service_c.py
python -m service_c.tools.integration_check_with_a
```

Run Service D & Service B test suites:

```bash
pytest -q service_d/tests
cargo test --manifest-path match_engine/Cargo.toml
```

See [RUNBOOK.md](RUNBOOK.md) for failure-injection, observation, and recovery procedures.
