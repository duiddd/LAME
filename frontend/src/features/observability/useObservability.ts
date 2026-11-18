import { useQuery } from "@tanstack/react-query";
import { C_URL } from "../../shared/config";

export interface LatencyStats {
  p50: number;
  p95: number;
  p99: number;
  avg: number;
  count: number;
  note?: string;
  order_completion_p95_ms?: number;
  order_completion_p99_ms?: number;
  llm_p50?: number;
  llm_p95?: number;
  llm_p99?: number;
}

export interface ServiceMetricSnapshot {
  id: "service-a" | "service-b" | "service-c" | "service-d";
  name: string;
  owner: string;
  reachable: boolean;
  status: "up" | "degraded" | "offline";
  scrape_url?: string | null;
  error?: string | null;
  request_rate_rps: number;
  requests_total: number;
  errors_total: number;
  error_rate_pct: number;
  latency_ms: LatencyStats;
  key_gauges: Record<string, string | number | boolean>;
}

export interface AiObservability {
  model: string;
  execution_mode: string;
  prompt_tokens: number;
  eval_tokens: number;
  total_tokens: number;
  prompt_cost_per_1m_usd: number;
  eval_cost_per_1m_usd: number;
  estimated_equivalent_cost_usd: number;
  direct_api_spend_usd: number;
  cost_per_1k_requests_usd: number;
  tokens_saved_by_gates: number;
  estimated_cost_saved_usd: number;
  total_chat_requests: number;
  answered_total: number;
  refusal_rate: number;
  refusal_total: Record<string, number>;
  chat_latency_ms: LatencyStats;
  llm_inference_ms: LatencyStats;
  hallucinated_citations_stripped_total: number;
  sanitized_news_items_total: number;
  dlq_messages_count: number;
  index_version: number;
}

export interface ObservabilitySnapshot {
  generated_at_ms: number;
  uptime_seconds: number;
  services_up: number;
  services_total: number;
  cluster: {
    request_rate_rps: number;
    requests_total: number;
    errors_total: number;
    error_rate_pct: number;
  };
  services: ServiceMetricSnapshot[];
  ai: AiObservability;
  links: {
    grafana_url: string;
    prometheus_url: string;
    service_c_metrics_url: string;
  };
}

export const SIMULATED_PEER_BASELINE: Record<
  "service-a" | "service-b" | "service-d",
  ServiceMetricSnapshot
> = {
  "service-a": {
    id: "service-a",
    name: "Service A · Trading & Accounts",
    owner: "Pin-Yun Kuo",
    reachable: true,
    status: "up",
    scrape_url: "mock://service-a:8000/metrics",
    request_rate_rps: 14.2,
    requests_total: 1280,
    errors_total: 4,
    error_rate_pct: 0.31,
    latency_ms: { p50: 18.0, p95: 42.5, p99: 68.0, avg: 21.4, count: 1280, order_completion_p95_ms: 505 },
    key_gauges: { circuit_breaker: "CLOSED", shedding_active: false, orders_unsettled: 0, notify_cursor_age_s: 0.2 },
  },
  "service-b": {
    id: "service-b",
    name: "Service B · Matching Engine",
    owner: "Bo Wu",
    reachable: true,
    status: "up",
    scrape_url: "mock://service-b:11659/metrics",
    request_rate_rps: 11.8,
    requests_total: 940,
    errors_total: 1,
    error_rate_pct: 0.11,
    latency_ms: {
      p50: 1.4,
      p95: 1.4,
      p99: 1.4,
      avg: 1.4,
      count: 940,
      note: "Startup replay shown; no per-order timer per B D23",
    },
    key_gauges: { md_subscribers: 3, journal_backlog: 0, outbound_backlog: 0, replay_records: 940, journal_failed: false },
  },
  "service-d": {
    id: "service-d",
    name: "Service D · Risk Control",
    owner: "Zongyan Yang",
    reachable: true,
    status: "up",
    scrape_url: "mock://service-d:8400/metrics",
    request_rate_rps: 12.1,
    requests_total: 952,
    errors_total: 12,
    error_rate_pct: 1.26,
    latency_ms: { p50: 0.6, p95: 1.5, p99: 2.0, avg: 0.8, count: 952 },
    key_gauges: { ready: true, b_connected: true, decision_queue: 0, outbox_backlog: 0, feature_lag_s: 0.004 },
  },
};

export const FALLBACK_MOCK_OBSERVABILITY: ObservabilitySnapshot = {
  generated_at_ms: Date.now(),
  uptime_seconds: 120,
  services_up: 4,
  services_total: 4,
  cluster: {
    request_rate_rps: 40.5,
    requests_total: 3292,
    errors_total: 18,
    error_rate_pct: 0.55,
  },
  services: [
    SIMULATED_PEER_BASELINE["service-a"],
    SIMULATED_PEER_BASELINE["service-b"],
    SIMULATED_PEER_BASELINE["service-d"],
    {
      id: "service-c",
      name: "Service C · News & AI Analyst",
      owner: "Rushabh Mutha",
      reachable: true,
      status: "up",
      scrape_url: "mock://service-c:8080/metrics",
      request_rate_rps: 2.4,
      requests_total: 120,
      errors_total: 1,
      error_rate_pct: 0.83,
      latency_ms: { p50: 145, p95: 420, p99: 680, avg: 210, count: 48, llm_p50: 120, llm_p95: 380, llm_p99: 620 },
      key_gauges: { index_version: 5, dlq_messages: 1, refusal_rate_pct: 18.8, stripped_citations: 0 },
    },
  ],
  ai: {
    model: "llama3.2:1b",
    execution_mode: "local-ollama",
    prompt_tokens: 14200,
    eval_tokens: 4850,
    total_tokens: 19050,
    prompt_cost_per_1m_usd: 0.1,
    eval_cost_per_1m_usd: 0.3,
    estimated_equivalent_cost_usd: 0.002875,
    direct_api_spend_usd: 0,
    cost_per_1k_requests_usd: 0.0599,
    tokens_saved_by_gates: 3150,
    estimated_cost_saved_usd: 0.000315,
    total_chat_requests: 48,
    answered_total: 39,
    refusal_rate: 0.1875,
    refusal_total: { insufficient_evidence: 5, unsafe_injection: 3, investment_advice: 1 },
    chat_latency_ms: { p50: 145, p95: 420, p99: 680, avg: 210, count: 48 },
    llm_inference_ms: { p50: 120, p95: 380, p99: 620, avg: 185, count: 39 },
    hallucinated_citations_stripped_total: 0,
    sanitized_news_items_total: 1,
    dlq_messages_count: 1,
    index_version: 5,
  },
  links: {
    grafana_url: "http://localhost:3001",
    prometheus_url: "http://localhost:9090",
    service_c_metrics_url: `${C_URL}/metrics`,
  },
};

export interface ObservabilityResult {
  snapshot: ObservabilitySnapshot;
  sourceMode: "live" | "mock-fallback";
}

export function applySimulatedPeerBaseline(snapshot: ObservabilitySnapshot): ObservabilitySnapshot {
  const services = snapshot.services.map((svc) => {
    if (!svc.reachable && svc.id !== "service-c") {
      return { ...SIMULATED_PEER_BASELINE[svc.id] };
    }
    return svc;
  });
  const totalReq = services.reduce((acc, s) => acc + s.requests_total, 0);
  const totalErr = services.reduce((acc, s) => acc + s.errors_total, 0);
  const clusterRps = Number(services.reduce((acc, s) => acc + s.request_rate_rps, 0).toFixed(3));
  return {
    ...snapshot,
    services,
    services_up: services.filter((s) => s.reachable).length,
    cluster: {
      request_rate_rps: clusterRps,
      requests_total: totalReq,
      errors_total: totalErr,
      error_rate_pct: totalReq > 0 ? Number(((totalErr / totalReq) * 100).toFixed(2)) : 0,
    },
  };
}

export function useObservability() {
  const isMockEnv = import.meta.env.VITE_MOCK === "1";
  return useQuery<ObservabilityResult>({
    queryKey: ["observability"],
    refetchInterval: 5000,
    retry: false,
    queryFn: async () => {
      try {
        const res = await fetch(`${C_URL}/api/observability`, {
          signal: AbortSignal.timeout(4000),
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const data = (await res.json()) as ObservabilitySnapshot;
        return { snapshot: data, sourceMode: "live" };
      } catch (err) {
        if (isMockEnv) {
          return {
            snapshot: { ...FALLBACK_MOCK_OBSERVABILITY, generated_at_ms: Date.now() },
            sourceMode: "mock-fallback",
          };
        }
        throw err;
      }
    },
  });
}
