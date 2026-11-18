# ADR-003: Decouple the AI Market Analyst (Service C) Off the Critical Trading Path via Asynchronous Ingestion and Read-Only Snapshots

**Status:** Accepted
**Date:** 2026-11-18
**Owner:** Rushabh Mutha (Service C), aligned with Pin-Yun Kuo (A), Bo Wu (B), and Zongyan Yang (D)

---

## Context

LAME pairs a spot exchange (`A: Trading & Accounts` → `D: Risk Control` → `B: Matching Engine`) with a generative AI Market Analyst (`C`) that ingests news and answers user questions via RAG over local `Ollama` (`llama3.2:1b`). Three load-bearing constraints govern this boundary:

1. **Latency & Determinism:** `A → D → B` requires deterministic crash replay and low `p99` order latency, whereas LLM inference shows high variance (`0.4 s` to `>3.0 s` timeout).
2. **Heterogeneous Consistency:** Balance holds in `A` require **CP** invariants (`available >= 0`), whereas market commentary tolerates **AP** eventual consistency with explicit freshness labels.
3. **Trust & Fault Isolation:** News streams and user queries carry untrusted text (prompt-injection and malformed JSON risks) that cannot share a failure domain or schema with `A`'s PostgreSQL ledger or `B`'s `events-<day>.db`.

*Flip conditions:* Revisit only if business scope adds AI pre-trade risk scoring to `D`'s critical path, or mandates blocking spot orders when news ingestion lags.

---

## Decision

We isolate **Service C (`:8080`)** as a **separate architecture quantum off the critical trading path**, enforcing zero synchronous calls from `A`, `D`, or `B` into `C`:

1. **Private Search Store:** `C` owns an isolated SQLite WAL database (`/data/news_c.db`) with FTS5 BM25 indexing and atomic `index_version`, sharing zero tables with peers.
2. **Async Ingestion & DLQ:** News payloads travel via RabbitMQ (`news.direct` → `news_tasks`), deduplicated idempotently on `news_id`; malformed payloads route via `nack(requeue=False)` (`news.dlx` → `news.dlq`).
3. **Passive Snapshot Stream:** `C` subscribes read-only to `B`'s `/md/<symbol>` WebSocket into `MarketDataCache`, dropping `ts_ms < existing`, refreshing liveness on unchanged snapshot resends (same `last_ms`, no `heartbeat` flag), resetting watermarks on reconnect, and flagging `is_stale = true` after `3.0 s` of silence.
4. **Bounded Guardrails & Spend:** `A`'s UI calls `C` asynchronously via REST/widget with fallback; `C` caps Ollama at `3.0 s` and short-circuits pre-LLM on injections (`REJECTED_UNSAFE`) or empty FTS5 hits (`INSUFFICIENT_EVIDENCE`).

---

## Consequences

**Positive (assessed against constraints)**
- **Zero trading-path tax:** Killing `service-c` or `ollama` adds **0 ms** latency to `A → D → B`; matching and settlement stay **100% available**.
- **Bounded failure & token spend:** Poison JSON isolates in `news.dlq` (`0` stalls); `3.0 s` caps prevent worker starvation, and pre-LLM refusal gates avoid unnecessary token evaluation (`lame_c_tokens_saved_by_gates_total`).
- **Contained blast radius:** Prompts and injection regexes (`security.py`) evolve independently, confining injection attempts to read-only answers (`30/30` integration checks pass).

**Negative (knowingly accepted trade-offs)**
- **Eventual consistency lag:** Explanations lag live trades by ingestion delay plus up to `3.0 s` snapshot staleness (mitigated via `data_freshness_seconds`, `news_freshness_seconds`, and `index_version`).
- **Operational footprint:** Adds SQLite FTS5, RabbitMQ, Ollama containers, and duplicated symbol normalization rather than shared libraries.

---

## Alternatives Considered

- **Embed RAG in Service A:** Eliminates a container, but mixes `3 s` LLM threads and untrusted inputs with PostgreSQL balance holds.
- **Synchronous REST from C to A/B per question:** Avoids `MarketDataCache`, but couples chat traffic bursts onto `B` and `A`.
- **Orchestrated Saga on trading path:** Unneeded coordination cost for read-only explanations with no compensating writes.
