"""Prometheus observability metrics for Service C (Decision D9 & Prof. Avishek Point 7).

Exposes:
- lame_c_http_requests_total{endpoint, method, status}
- lame_c_chat_latency_seconds (Histogram + quantiles)
- lame_c_llm_inference_duration_seconds (Histogram + summary)
- lame_c_tokens_total{type="prompt|eval"}
- lame_c_refusal_total{reason="insufficient_evidence|unsafe_injection|investment_advice"}
- lame_c_hallucinated_citations_stripped_total
- lame_c_rabbitmq_dlq_messages_count
- lame_c_index_version
"""

import threading
from collections import defaultdict


class MetricsRegistry:
    """Thread-safe Prometheus text-format metrics collector and summary provider."""

    LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.http_requests_total: dict[tuple[str, str, int], int] = defaultdict(int)
        self.tokens_total: dict[str, int] = {"prompt": 0, "eval": 0}
        self.refusal_total: dict[str, int] = {
            "insufficient_evidence": 0,
            "unsafe_injection": 0,
            "investment_advice": 0,
        }
        self.answered_total: int = 0
        self.hallucinated_citations_stripped_total: int = 0
        self.sanitized_news_items_total: int = 0
        self.dlq_messages_count: int = 0
        self.index_version: int = 0

        self._chat_latencies: list[float] = []
        self._llm_latencies: list[float] = []

    def record_http_request(self, endpoint: str, method: str, status: int) -> None:
        with self._lock:
            self.http_requests_total[(endpoint, method, status)] += 1

    def record_chat_latency(self, seconds: float) -> None:
        with self._lock:
            self._chat_latencies.append(max(0.0, seconds))
            if len(self._chat_latencies) > 2000:
                self._chat_latencies = self._chat_latencies[-2000:]

    def record_llm_inference(self, seconds: float, prompt_tokens: int, eval_tokens: int) -> None:
        with self._lock:
            self._llm_latencies.append(max(0.0, seconds))
            if len(self._llm_latencies) > 2000:
                self._llm_latencies = self._llm_latencies[-2000:]
            self.tokens_total["prompt"] += max(0, int(prompt_tokens))
            self.tokens_total["eval"] += max(0, int(eval_tokens))

    def record_refusal(self, reason: str) -> None:
        with self._lock:
            if reason not in self.refusal_total:
                self.refusal_total[reason] = 0
            self.refusal_total[reason] += 1

    def record_answered(self) -> None:
        with self._lock:
            self.answered_total += 1

    def record_stripped_citation(self, count: int = 1) -> None:
        with self._lock:
            self.hallucinated_citations_stripped_total += count

    def record_sanitized_news(self) -> None:
        with self._lock:
            self.sanitized_news_items_total += 1

    def set_dlq_count(self, count: int) -> None:
        with self._lock:
            self.dlq_messages_count = max(0, count)

    def set_index_version(self, version: int) -> None:
        with self._lock:
            self.index_version = max(0, version)

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
        return sorted_vals[idx]

    def summary_dict(self) -> dict:
        """Returns structured metrics snapshot for UI telemetry bars and testing."""
        with self._lock:
            total_refusals = sum(self.refusal_total.values())
            total_chats = self.answered_total + total_refusals
            refusal_rate = (total_refusals / total_chats) if total_chats > 0 else 0.0
            return {
                "total_chat_requests": total_chats,
                "answered_total": self.answered_total,
                "refusal_total": dict(self.refusal_total),
                "refusal_rate": round(refusal_rate, 4),
                "tokens_total": {
                    "prompt": self.tokens_total["prompt"],
                    "eval": self.tokens_total["eval"],
                    "total": self.tokens_total["prompt"] + self.tokens_total["eval"],
                },
                "chat_latency_ms": {
                    "p50": round(self._percentile(self._chat_latencies, 0.50) * 1000, 2),
                    "p95": round(self._percentile(self._chat_latencies, 0.95) * 1000, 2),
                    "p99": round(self._percentile(self._chat_latencies, 0.99) * 1000, 2),
                    "count": len(self._chat_latencies),
                },
                "llm_inference_ms": {
                    "p50": round(self._percentile(self._llm_latencies, 0.50) * 1000, 2),
                    "p95": round(self._percentile(self._llm_latencies, 0.95) * 1000, 2),
                    "p99": round(self._percentile(self._llm_latencies, 0.99) * 1000, 2),
                    "count": len(self._llm_latencies),
                },
                "hallucinated_citations_stripped_total": self.hallucinated_citations_stripped_total,
                "sanitized_news_items_total": self.sanitized_news_items_total,
                "dlq_messages_count": self.dlq_messages_count,
                "index_version": self.index_version,
            }

    def render_prometheus(self) -> str:
        """Renders Prometheus exposition format text (0.0.4)."""
        lines: list[str] = []
        with self._lock:
            lines.append("# HELP lame_c_http_requests_total Total HTTP requests handled by Service C.")
            lines.append("# TYPE lame_c_http_requests_total counter")
            for (endpoint, method, status), count in sorted(self.http_requests_total.items()):
                lines.append(
                    f'lame_c_http_requests_total{{endpoint="{endpoint}",method="{method}",status="{status}"}} {count}'
                )

            lines.append("# HELP lame_c_tokens_total Total LLM tokens processed by type (prompt vs eval).")
            lines.append("# TYPE lame_c_tokens_total counter")
            for tok_type, count in sorted(self.tokens_total.items()):
                lines.append(f'lame_c_tokens_total{{type="{tok_type}"}} {count}')

            lines.append("# HELP lame_c_refusal_total Total queries refused by guardrail reason.")
            lines.append("# TYPE lame_c_refusal_total counter")
            for reason, count in sorted(self.refusal_total.items()):
                lines.append(f'lame_c_refusal_total{{reason="{reason}"}} {count}')

            lines.append("# HELP lame_c_answered_total Total grounded answers served.")
            lines.append("# TYPE lame_c_answered_total counter")
            lines.append(f"lame_c_answered_total {self.answered_total}")

            lines.append(
                "# HELP lame_c_hallucinated_citations_stripped_total Citations stripped by post-generation verifier."
            )
            lines.append("# TYPE lame_c_hallucinated_citations_stripped_total counter")
            lines.append(
                f"lame_c_hallucinated_citations_stripped_total {self.hallucinated_citations_stripped_total}"
            )

            lines.append("# HELP lame_c_rabbitmq_dlq_messages_count Current messages in news.dlq.")
            lines.append("# TYPE lame_c_rabbitmq_dlq_messages_count gauge")
            lines.append(f"lame_c_rabbitmq_dlq_messages_count {self.dlq_messages_count}")

            lines.append("# HELP lame_c_index_version Current SQLite FTS5 knowledge base index version.")
            lines.append("# TYPE lame_c_index_version gauge")
            lines.append(f"lame_c_index_version {self.index_version}")

            # Chat latency histogram
            lines.append("# HELP lame_c_chat_latency_seconds End-to-end RAG chat latency in seconds.")
            lines.append("# TYPE lame_c_chat_latency_seconds histogram")
            for b in self.LATENCY_BUCKETS:
                cnt = sum(1 for v in self._chat_latencies if v <= b)
                lines.append(f'lame_c_chat_latency_seconds_bucket{{le="{b}"}} {cnt}')
            lines.append(
                f'lame_c_chat_latency_seconds_bucket{{le="+Inf"}} {len(self._chat_latencies)}'
            )
            lines.append(f"lame_c_chat_latency_seconds_sum {sum(self._chat_latencies):.6f}")
            lines.append(f"lame_c_chat_latency_seconds_count {len(self._chat_latencies)}")

            # LLM inference duration histogram
            lines.append(
                "# HELP lame_c_llm_inference_duration_seconds Pure LLM inference duration in seconds."
            )
            lines.append("# TYPE lame_c_llm_inference_duration_seconds histogram")
            for b in self.LATENCY_BUCKETS:
                cnt = sum(1 for v in self._llm_latencies if v <= b)
                lines.append(f'lame_c_llm_inference_duration_seconds_bucket{{le="{b}"}} {cnt}')
            lines.append(
                f'lame_c_llm_inference_duration_seconds_bucket{{le="+Inf"}} {len(self._llm_latencies)}'
            )
            lines.append(f"lame_c_llm_inference_duration_seconds_sum {sum(self._llm_latencies):.6f}")
            lines.append(f"lame_c_llm_inference_duration_seconds_count {len(self._llm_latencies)}")

        return "\n".join(lines) + "\n"


metrics = MetricsRegistry()
