# ADR-003 — The AI analyst (C) stays off the trading path, fed by asynchronous ingestion and read-only snapshots

**Status:** accepted

**Context.** LAME pairs the exchange (A → D → B) with an AI market analyst (C) that ingests news and answers
questions by retrieval-augmented generation over a local Ollama model (`llama3.2:1b`). The trading path needs
deterministic replay and a low order latency, while an LLM call takes from 0.4 s to past a 3 s timeout. Balance
holds in A are consistency-critical (`available >= 0`); market commentary can lag if it says by how much. News
items and user questions are untrusted text (prompt injection, malformed JSON) and must not share a failure
domain or a schema with A's ledger or B's journal.

This decision would reopen if the AI were to score orders inside D's pre-trade gate, or if orders had to be
blocked while news ingestion lags.

**Decision.** Verdict: accept. Rushabh Mutha decided that C (`:8080`) is a separate architecture quantum off the
trading path: A, D and B never call C synchronously. C owns its store (SQLite WAL with an FTS5 BM25 index and an
atomic `index_version`). News arrives through RabbitMQ (`news.direct` → `news_tasks`), deduplicated on
`news_id`, with malformed items sent to a dead-letter queue (`news.dlq`) instead of being retried. C follows
B's `/md/<symbol>` snapshots read-only and marks its data stale after 3 s of silence. The frontend embeds C with
a fallback, and C caps each model call at 3 s and refuses injections and evidence-free questions before calling
the model. The trade-off is fault isolation over a smaller deployment. Embedding RAG in A was rejected because it
would put 3 s model calls and untrusted text next to the balance holds; C calling A and B synchronously per
question because a burst of chat would load the trading services; a saga across the trading path because C
writes nothing that would need compensating.

**Consequences.**

- The trading path does not depend on C, against its latency budget: stopping C or Ollama adds no latency and no
  errors to A → D → B, and on 20 Nov all three trading faults degraded and recovered with C answering throughout.
- A poison news item stops nothing, against the rule that ingestion must keep flowing: malformed payloads land in
  `news.dlq` (one there during the 20 Nov runs) while ingestion continues.
- Model spend and pinned workers stay bounded, against the zero-cost and responsiveness constraints: the 3 s cap
  frees a worker in the worst case, and refusals before the model are counted as tokens saved
  (`lame_c_tokens_saved_by_gates_total`: 4,900 since C started, by 02:19 on 20 Nov, with every question refused).
- Answers can lag the market, against the requirement that commentary states its age: up to ingestion delay plus
  3 s of snapshot staleness, reported as `data_freshness_seconds`, `news_freshness_seconds` and `index_version`.
- The deployment grows, against a laptop-sized footprint: RabbitMQ, Ollama (about 2 GB of memory) and C's own
  store, plus symbol normalisation duplicated in C rather than shared.
