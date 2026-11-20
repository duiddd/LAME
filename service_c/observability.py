"""Cross-service Prometheus scraper & system observability aggregator (Brief §1.3).

Collects RED signals (Request rate, Errors, Latency percentiles) across Services A, B, D, and C,
plus AI Golden Signal telemetry (model inference latency, prompt/eval token counts, token cost,
pre-LLM gate token savings, refusal rate, citation sanitization, and RabbitMQ DLQ depth).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import re
import time
from typing import Any, Optional

import httpx

from .config import settings
from .metrics import metrics

_METRIC_LINE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|NaN|[+-]?Inf)\s*(?:\d+)?$"
)
_LABEL_PAIR_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


@dataclass
class ParsedPrometheus:
    """Structured representation of Prometheus exposition text."""

    samples: dict[str, list[tuple[dict[str, str], float]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def sum_metric(
        self,
        name: str,
        label_filter: Optional[dict[str, str | set[str]]] = None,
    ) -> float:
        total = 0.0
        for labels, val in self.samples.get(name, []):
            if val != val:  # skip NaN
                continue
            if label_filter:
                matched = True
                for k, expected in label_filter.items():
                    actual = labels.get(k, "")
                    if isinstance(expected, set):
                        if actual not in expected:
                            matched = False
                            break
                    elif actual != expected:
                        matched = False
                        break
                if not matched:
                    continue
            total += val
        return total

    def gauge_value(self, name: str, default: float = 0.0) -> float:
        entries = self.samples.get(name, [])
        if not entries:
            return default
        return float(sum(v for _, v in entries if v == v))

    def histogram_quantiles_ms(self, base_name: str) -> dict[str, Any]:
        """Aggregates cumulative histogram buckets across labels and computes p50/p95/p99 in ms."""
        bucket_counts: dict[float, float] = defaultdict(float)
        inf_total = 0.0
        for labels, val in self.samples.get(f"{base_name}_bucket", []):
            le_raw = labels.get("le", "")
            if le_raw == "+Inf":
                inf_total += val
            else:
                try:
                    bucket_counts[float(le_raw)] += val
                except ValueError:
                    continue

        count_total = self.sum_metric(f"{base_name}_count")
        sum_total = self.sum_metric(f"{base_name}_sum")
        n = count_total if count_total > 0 else inf_total
        avg_ms = round((sum_total / n) * 1000.0, 2) if n > 0 else 0.0

        if not bucket_counts or n <= 0:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "avg": avg_ms, "count": int(n)}

        sorted_bounds = sorted(bucket_counts.keys())

        def _quantile(q: float) -> float:
            target = q * n
            prev_bound = 0.0
            prev_cnt = 0.0
            for bound in sorted_bounds:
                cnt = bucket_counts[bound]
                if cnt >= target:
                    delta_cnt = max(cnt - prev_cnt, 1e-9)
                    frac = (target - prev_cnt) / delta_cnt
                    return round((prev_bound + frac * (bound - prev_bound)) * 1000.0, 2)
                prev_bound = bound
                prev_cnt = cnt
            return round(sorted_bounds[-1] * 1000.0, 2)

        return {
            "p50": _quantile(0.50),
            "p95": _quantile(0.95),
            "p99": _quantile(0.99),
            "avg": avg_ms,
            "count": int(n),
        }


def parse_prometheus_text(raw: str) -> ParsedPrometheus:
    """Parses Prometheus text exposition format (0.0.4) into structured samples."""
    parsed = ParsedPrometheus()
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        match = _METRIC_LINE_RE.match(s)
        if not match:
            continue
        metric_name, raw_labels, raw_val = match.groups()
        try:
            value = float(raw_val)
        except ValueError:
            continue
        labels: dict[str, str] = {}
        if raw_labels:
            for k, v in _LABEL_PAIR_RE.findall(raw_labels):
                labels[k] = v
        parsed.samples[metric_name].append((labels, value))
    return parsed


class ObservabilityAggregator:
    """Fetches & aggregates Prometheus metrics across Services A, B, D, and C."""

    def __init__(self) -> None:
        self._rate_history: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=60)
        )
        self._start_mono = time.monotonic()

    def _update_rate_rps(self, service_id: str, cumulative_requests: float, now_mono: float) -> float:
        hist = self._rate_history[service_id]
        if hist and cumulative_requests < hist[-1][1]:
            # Peer restarted and reset its Prometheus counter.
            hist.clear()
        hist.append((now_mono, cumulative_requests))
        if len(hist) >= 2:
            oldest_t, oldest_reqs = hist[0]
            dt = now_mono - oldest_t
            if dt >= 0.5:
                delta = max(0.0, cumulative_requests - oldest_reqs)
                return round(delta / dt, 3)
        elapsed = max(1.0, now_mono - self._start_mono)
        return round(cumulative_requests / elapsed, 3)

    @staticmethod
    def _candidate_urls(configured_base: str, localhost_port: int) -> list[str]:
        urls = [f"{configured_base.rstrip('/')}/metrics"]
        local_url = f"http://127.0.0.1:{localhost_port}/metrics"
        if local_url not in urls:
            urls.append(local_url)
        return urls

    async def _scrape_one(
        self,
        client: httpx.AsyncClient,
        urls: list[str],
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        last_err: Optional[str] = None
        for url in urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and ("# " in resp.text or "lame_" in resp.text):
                    return resp.text, url, None
                last_err = f"HTTP {resp.status_code} from {url}"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {url}"
        return None, urls[0] if urls else None, last_err

    @staticmethod
    def _normalize_service_a(raw: Optional[str], scrape_url: Optional[str], err: Optional[str]) -> dict:
        if raw is None:
            return {
                "id": "service-a",
                "name": "Service A · Trading & Accounts",
                "owner": "Pin-Yun Kuo",
                "reachable": False,
                "status": "offline",
                "scrape_url": scrape_url,
                "error": err,
                "requests_total": 0,
                "errors_total": 0,
                "error_rate_pct": 0.0,
                "latency_ms": {"p50": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0, "count": 0},
                "key_gauges": {},
            }
        p = parse_prometheus_text(raw)
        http_samples = p.samples.get("lame_a_http_request_duration_seconds_count", [])
        http_reqs = p.sum_metric("lame_a_http_request_duration_seconds_count")
        order_reqs = p.sum_metric("lame_a_orders_total")
        reqs = int(http_reqs if http_reqs > 0 else order_reqs)
        order_refused = sum(
            val
            for labels, val in p.samples.get("lame_a_orders_total", [])
            if val == val and labels.get("result", "") != "accepted"
        )
        has_http_status = any("status" in labels for labels, _ in http_samples)
        http_4xx = 0.0
        if has_http_status:
            # 5xx are server-side errors; 4xx are client-side errors (surfaced separately).
            http_5xx = sum(
                val
                for labels, val in http_samples
                if val == val and labels.get("status", "").isdigit() and int(labels["status"]) >= 500
            )
            http_4xx = sum(
                val
                for labels, val in http_samples
                if val == val
                and labels.get("status", "").isdigit()
                and 400 <= int(labels["status"]) < 500
            )
            errs = int(http_5xx + p.sum_metric("lame_a_md_bad_messages_total"))
        else:
            server_order_errs = p.sum_metric("lame_a_orders_total", {"result": "SYSTEM_BUSY"})
            errs = int(
                server_order_errs
                + p.sum_metric("lame_a_notify_failures_total")
                + p.sum_metric("lame_a_md_bad_messages_total")
            )
        lat = p.histogram_quantiles_ms("lame_a_http_request_duration_seconds")
        order_lat = p.histogram_quantiles_ms("lame_a_order_completion_seconds")
        circuit_val = int(p.gauge_value("lame_a_circuit_state"))
        circuit_label = {0: "CLOSED", 1: "HALF-OPEN", 2: "OPEN"}.get(circuit_val, str(circuit_val))
        return {
            "id": "service-a",
            "name": "Service A · Trading & Accounts",
            "owner": "Pin-Yun Kuo",
            "reachable": True,
            "status": "degraded" if p.gauge_value("lame_a_shedding_active") > 0 or circuit_val == 2 else "up",
            "scrape_url": scrape_url,
            "error": None,
            "requests_total": reqs,
            "errors_total": errs,
            "error_rate_pct": round((errs / reqs) * 100.0, 2) if reqs > 0 else 0.0,
            "latency_ms": {
                **lat,
                "order_completion_p95_ms": order_lat["p95"],
                "order_completion_p99_ms": order_lat["p99"],
            },
            "key_gauges": {
                "circuit_breaker": circuit_label,
                "shedding_active": bool(p.gauge_value("lame_a_shedding_active") > 0),
                "client_4xx": int(http_4xx),
                "orders_refused": int(order_refused),
                "orders_unsettled": int(p.gauge_value("lame_a_orders_unsettled")),
                "notify_cursor_age_s": round(p.gauge_value("lame_a_notify_cursor_age_seconds"), 2),
            },
        }

    @staticmethod
    def _normalize_service_b(raw: Optional[str], scrape_url: Optional[str], err: Optional[str]) -> dict:
        if raw is None:
            return {
                "id": "service-b",
                "name": "Service B · Matching Engine",
                "owner": "Bo Wu",
                "reachable": False,
                "status": "offline",
                "scrape_url": scrape_url,
                "error": err,
                "requests_total": 0,
                "errors_total": 0,
                "error_rate_pct": 0.0,
                "latency_ms": {
                    "p50": 0.0,
                    "p95": 0.0,
                    "p99": 0.0,
                    "avg": 0.0,
                    "count": 0,
                    "note": "No per-order matching latency histogram per Service B D23",
                },
                "key_gauges": {},
            }
        p = parse_prometheus_text(raw)
        reqs = int(p.sum_metric("lame_b_commands_total"))
        errs = int(
            p.sum_metric("lame_b_bad_frames_total")
            + p.sum_metric("lame_b_outbound_overflows_total")
        )
        journal_failed = int(p.gauge_value("lame_b_journal_failed"))
        replay_ms = round(p.gauge_value("lame_b_replay_duration_seconds") * 1000.0, 2)
        return {
            "id": "service-b",
            "name": "Service B · Matching Engine",
            "owner": "Bo Wu",
            "reachable": True,
            "status": "degraded" if journal_failed > 0 else "up",
            "scrape_url": scrape_url,
            "error": None,
            "requests_total": reqs,
            "errors_total": errs,
            "error_rate_pct": round((errs / reqs) * 100.0, 2) if reqs > 0 else 0.0,
            "latency_ms": {
                "p50": replay_ms,
                "p95": replay_ms,
                "p99": replay_ms,
                "avg": replay_ms,
                "count": int(p.gauge_value("lame_b_replay_records")),
                "note": "Startup replay duration shown; no per-order timer per B D23",
            },
            "key_gauges": {
                "md_subscribers": int(p.gauge_value("lame_b_md_subscribers")),
                # Track OrderEvent outputs only (not Trade), as order status lives only on OrderEvent.
                "order_events": int(p.sum_metric("lame_b_outputs_total", {"kind": "OrderEvent"})),
                "journal_backlog": int(p.gauge_value("lame_b_journal_backlog")),
                "outbound_backlog": int(p.gauge_value("lame_b_outbound_backlog")),
                "replay_records": int(p.gauge_value("lame_b_replay_records")),
                "journal_failed": bool(journal_failed > 0),
            },
        }

    @staticmethod
    def _normalize_service_d(raw: Optional[str], scrape_url: Optional[str], err: Optional[str]) -> dict:
        if raw is None:
            return {
                "id": "service-d",
                "name": "Service D · Risk Control",
                "owner": "Zongyan Yang",
                "reachable": False,
                "status": "offline",
                "scrape_url": scrape_url,
                "error": err,
                "requests_total": 0,
                "errors_total": 0,
                "error_rate_pct": 0.0,
                "latency_ms": {"p50": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0, "count": 0},
                "key_gauges": {},
            }
        p = parse_prometheus_text(raw)
        decisions = p.sum_metric("lame_d_decisions_total")
        refused = p.sum_metric("lame_d_refused_total")
        reqs = int(decisions + refused)
        approved = p.sum_metric("lame_d_decisions_total", {"decision": "APPROVED"})
        rejected = p.sum_metric("lame_d_decisions_total", {"decision": "REJECTED"})
        errs = int(refused + rejected)
        lat = p.histogram_quantiles_ms("lame_d_decision_seconds")
        ready = int(p.gauge_value("lame_d_ready"))
        b_conn = int(p.gauge_value("lame_d_b_connected"))
        return {
            "id": "service-d",
            "name": "Service D · Risk Control",
            "owner": "Zongyan Yang",
            "reachable": True,
            "status": "up" if ready == 1 and b_conn == 1 else "degraded",
            "scrape_url": scrape_url,
            "error": None,
            "requests_total": reqs,
            "errors_total": errs,
            "error_rate_pct": round((errs / reqs) * 100.0, 2) if reqs > 0 else 0.0,
            "latency_ms": lat,
            "key_gauges": {
                "ready": bool(ready == 1),
                "b_connected": bool(b_conn == 1),
                "approved_decisions": int(approved),
                "rejected_decisions": int(rejected),
                "decision_queue": int(p.gauge_value("lame_d_decision_queue")),
                "timed_out_orders": int(p.gauge_value("lame_d_timed_out_orders")),
                "outbox_backlog": int(p.gauge_value("lame_d_outbox_backlog")),
                "feature_lag_s": round(p.gauge_value("lame_d_feature_lag_seconds"), 3),
            },
        }

    def _build_service_c(self, c_summary: dict) -> dict:
        chat_lat = c_summary["chat_latency_ms"]
        llm_lat = c_summary["llm_inference_ms"]
        return {
            "id": "service-c",
            "name": "Service C · News & AI Analyst",
            "owner": "Rushabh Mutha",
            "reachable": True,
            "status": "up",
            "scrape_url": "/metrics",
            "error": None,
            "requests_total": int(c_summary["http_requests_total"]),
            "errors_total": int(c_summary["http_errors_total"]),
            "error_rate_pct": float(c_summary["error_rate_pct"]),
            "request_rate_rps": float(c_summary["request_rate_rps"]),
            "latency_ms": {
                "p50": chat_lat["p50"],
                "p95": chat_lat["p95"],
                "p99": chat_lat["p99"],
                "avg": round((chat_lat["p50"] + chat_lat["p95"]) / 2.0, 2)
                if chat_lat["count"] > 0
                else 0.0,
                "count": chat_lat["count"],
                "llm_p50": llm_lat["p50"],
                "llm_p95": llm_lat["p95"],
                "llm_p99": llm_lat["p99"],
            },
            "key_gauges": {
                "index_version": c_summary["index_version"],
                "client_4xx": c_summary["http_4xx_total"],
                "dlq_messages": c_summary["dlq_messages_count"],
                "refusal_rate_pct": round(c_summary["refusal_rate"] * 100.0, 1),
                "stripped_citations": c_summary["hallucinated_citations_stripped_total"],
            },
        }

    async def collect(
        self,
        override_scrapes: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Collects metrics from all 4 LAME services + AI cost metrics in parallel."""
        c_summary = metrics.summary_dict()
        now_mono = time.monotonic()

        if override_scrapes is not None:
            raw_a = override_scrapes.get("service-a")
            raw_b = override_scrapes.get("service-b")
            raw_d = override_scrapes.get("service-d")
            res_a = (raw_a, "override://service-a/metrics", None if raw_a else "not provided")
            res_b = (raw_b, "override://service-b/metrics", None if raw_b else "not provided")
            res_d = (raw_d, "override://service-d/metrics", None if raw_d else "not provided")
        else:
            timeout = httpx.Timeout(0.8, connect=0.25)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    res_a, res_b, res_d = await asyncio.wait_for(
                        asyncio.gather(
                            self._scrape_one(
                                client, self._candidate_urls(settings.service_a_url, 8000)
                            ),
                            self._scrape_one(
                                client, self._candidate_urls(settings.service_b_ctrl_url, 11659)
                            ),
                            self._scrape_one(
                                client, self._candidate_urls(settings.service_d_url, 8400)
                            ),
                        ),
                        timeout=1.2,
                    )
            except Exception as exc:
                err_msg = f"Scrape timeout: {type(exc).__name__}"
                res_a = (None, f"{settings.service_a_url}/metrics", err_msg)
                res_b = (None, f"{settings.service_b_ctrl_url}/metrics", err_msg)
                res_d = (None, f"{settings.service_d_url}/metrics", err_msg)

        svc_a = self._normalize_service_a(*res_a)
        svc_b = self._normalize_service_b(*res_b)
        svc_d = self._normalize_service_d(*res_d)
        svc_c = self._build_service_c(c_summary)

        for svc in (svc_a, svc_b, svc_d):
            if svc["reachable"]:
                svc["request_rate_rps"] = self._update_rate_rps(
                    svc["id"], float(svc["requests_total"]), now_mono
                )
            else:
                svc["request_rate_rps"] = 0.0

        services = [svc_a, svc_b, svc_d, svc_c]
        total_requests = sum(s["requests_total"] for s in services)
        total_errors = sum(s["errors_total"] for s in services)
        cluster_rps = round(sum(s["request_rate_rps"] for s in services), 3)
        services_up = sum(1 for s in services if s["reachable"])

        return {
            "generated_at_ms": int(time.time() * 1000),
            "uptime_seconds": c_summary["uptime_seconds"],
            "services_up": services_up,
            "services_total": len(services),
            "cluster": {
                "request_rate_rps": cluster_rps,
                "requests_total": total_requests,
                "errors_total": total_errors,
                "error_rate_pct": round((total_errors / total_requests) * 100.0, 2)
                if total_requests > 0
                else 0.0,
            },
            "services": services,
            "ai": {
                **c_summary["token_cost"],
                "total_chat_requests": c_summary["total_chat_requests"],
                "answered_total": c_summary["answered_total"],
                "refusal_rate": c_summary["refusal_rate"],
                "refusal_total": c_summary["refusal_total"],
                "chat_latency_ms": c_summary["chat_latency_ms"],
                "llm_inference_ms": c_summary["llm_inference_ms"],
                "hallucinated_citations_stripped_total": c_summary[
                    "hallucinated_citations_stripped_total"
                ],
                "sanitized_news_items_total": c_summary["sanitized_news_items_total"],
                "dlq_messages_count": c_summary["dlq_messages_count"],
                "index_version": c_summary["index_version"],
            },
            "links": {
                "grafana_url": settings.grafana_url,
                "prometheus_url": settings.prometheus_url,
                "service_c_metrics_url": "/metrics",
            },
        }


observability_aggregator = ObservabilityAggregator()
