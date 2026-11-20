# ADR-003 — The AI analyst (C) stays off the trading path, fed by asynchronous ingestion and read-only snapshots

**Status:** accepted

**Context.** LAME integrates the exchange (A → D → B) with an AI market analyst (C) that ingests news and generates responses.
Questions are answered using retrieval-augmented generation via a local Ollama model (`llama3.2:1b`). The trading path requires
deterministic replay and low order latency, whereas a large language model (LLM) call ranges from 0.4 seconds up to a 3-second timeout. Balance
holds in A are critical for consistency (`available >= 0`). Market commentary may be delayed, provided the extent of the lag is specified. News
items and user questions are considered untrusted text (e.g., prompt injection, malformed JSON) and must not share a failure
domain or a schema with A’s ledger or B’s journal.

This decision would be reconsidered if the AI were required to score orders within D’s pre-trade gate, or if orders needed to be
blocked during periods of delayed news ingestion.

**Decision.** Verdict: accept. Rushabh Mutha decided that C (`:8080`) is a separate architecture quantum off the
trading path: A, D and B never call C synchronously. C owns its store (SQLite WAL with an FTS5 BM25 index and an
atomic `index_version`). News arrives through RabbitMQ (`news.direct` → `news_tasks`), deduplicated on
`news_id`, with malformed items sent to a dead-letter queue (`news.dlq`) instead of being retried. C follows
B’s `/md/<symbol>` snapshots read-only and marks its data stale after 3 s of silence. The frontend embeds C with
a fallback mechanism, and C limits each model call to three seconds, rejecting prompt injections and unsupported questions
before invoking the model. The trade-off is fault isolation over a smaller deployment. Embedding RAG in A was rejected because it
would place three-second model calls and untrusted text adjacent to the balance holds. Having C call A and B synchronously for each
question because a burst of chat would load the trading services; a saga across the trading path because C
does not perform any writes that would require compensation.

**Consequences.**

- The trading path operates independently of C, maintaining its latency budget. Stopping C or Ollama introduces neither additional latency nor
  errors to A → D → B. On 20 November, all three trading faults degraded and recovered while C continued to respond.
- A poison news item does not halt processing, in accordance with the requirement that ingestion must remain continuous. Malformed payloads are directed to
  `news.dlq` (one occurred during the 20 November runs) while ingestion proceeds uninterrupted.
- Model resource usage and pinned workers remain bounded, meeting zero-cost and responsiveness constraints. The three-second cap
  ensures a worker is released in the worst-case scenario, and refusals prior to model invocation are counted
  (`lame_c_tokens_saved_by_gates_total`: 4,900 since C was initiated, as of 02:19 on 20 November, with every question refused).
- Answers may lag behind the market, provided that commentary specifies its age. The delay can be up to the ingestion delay plus
  three seconds of snapshot staleness (reported as `data_freshness_seconds`, `news_freshness_seconds`, and `index_version`).
- The deployment size increases beyond a laptop-sized footprint, as RabbitMQ, Ollama (approximately 2 GB of memory), and C’s own
  store are required, with symbol normalization duplicated in C instead of being shared.
