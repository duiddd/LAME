import { useState } from "react";
import { C_URL } from "../../shared/config";
import { applySimulatedPeerBaseline, useObservability, type ServiceMetricSnapshot } from "./useObservability";

function StatusBadge({ status }: { status: ServiceMetricSnapshot["status"] }) {
  const styles =
    status === "up"
      ? "border-up/40 bg-up/15 text-up"
      : status === "degraded"
        ? "border-buoy/40 bg-buoy/15 text-buoy"
        : "border-down/40 bg-down/15 text-down";
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase ${styles}`}>
      {status}
    </span>
  );
}

function MetricCard({
  label,
  value,
  sub,
  tone = "ink",
}: {
  label: string;
  value: string;
  sub: string;
  tone?: "ink" | "buoy" | "up" | "down";
}) {
  const toneCls =
    tone === "buoy"
      ? "text-buoy"
      : tone === "up"
        ? "text-up"
        : tone === "down"
          ? "text-down"
          : "text-ink";
  return (
    <div className="rounded border border-line bg-panel p-3.5">
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted">{label}</div>
      <div className={`mt-1 text-2xl font-bold ${toneCls}`}>{value}</div>
      <div className="mt-1 text-[11px] text-muted">{sub}</div>
    </div>
  );
}

function RatioBar({
  label,
  valueText,
  pct,
  color = "bg-buoy",
}: {
  label: string;
  valueText: string;
  pct: number;
  color?: string;
}) {
  const clamped = Math.min(100, Math.max(0, pct));
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-xs">
        <span className="text-ink">{label}</span>
        <span className="text-muted">{valueText}</span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded bg-raised">
        <div className={`h-full rounded ${color}`} style={{ width: `${clamped}%` }} />
      </div>
    </div>
  );
}

export function DashboardPage() {
  const { data, isLoading, isError, refetch, isFetching } = useObservability();
  const isMockEnv = import.meta.env.VITE_MOCK === "1";
  const [fillOfflinePeers, setFillOfflinePeers] = useState(isMockEnv);

  if (isLoading) {
    return (
      <main className="flex h-full items-center justify-center p-6 text-muted">
        Loading system &amp; AI observability metrics…
      </main>
    );
  }

  if (isError || !data) {
    return (
      <main className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
        <p className="text-down">Observability endpoint ({C_URL}/api/observability) is unreachable.</p>
        <p className="max-w-md text-xs text-muted">
          Core spot trading continues independently of Service C. Start Service C or check Grafana (:3001) / Prometheus
          (:9090).
        </p>
        <button
          type="button"
          onClick={() => void refetch()}
          className="rounded border border-line bg-panel px-3 py-1.5 text-ink hover:bg-raised"
        >
          Retry
        </button>
      </main>
    );
  }

  const snapshot = fillOfflinePeers ? applySimulatedPeerBaseline(data.snapshot) : data.snapshot;
  const { cluster, services, ai, links } = snapshot;
  const hasOfflinePeers = data.snapshot.services.some((s) => !s.reachable);
  const serviceC = services.find((s) => s.id === "service-c");
  const serviceD = services.find((s) => s.id === "service-d");
  const maxRps = Math.max(1, ...services.map((s) => s.request_rate_rps));

  return (
    <main className="h-full overflow-auto bg-harbour p-4" aria-label="System Observability Dashboard">
      <div className="mx-auto flex max-w-7xl flex-col gap-4">
        <header className="flex flex-wrap items-center justify-between gap-3 rounded border border-line bg-panel px-4 py-3">
          <div>
            <h1 className="text-base font-semibold text-ink">LAME Cluster &amp; AI Observability</h1>
            <p className="text-xs text-muted">
              Golden signals across Services A, B, D, C (request rate, errors, latency) + AI token spend &amp; guardrails
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="rounded border border-line bg-raised px-2 py-1 text-muted">
              Up:{" "}
              <strong className="text-up">
                {snapshot.services_up}/{snapshot.services_total}
              </strong>
              {" · "}
              {data.sourceMode === "mock-fallback"
                ? "Offline Mock Preview"
                : fillOfflinePeers && hasOfflinePeers
                  ? "Live C + Simulated Peers (VITE_MOCK)"
                  : "Live Scrape"}
            </span>
            {isMockEnv && hasOfflinePeers && (
              <button
                type="button"
                onClick={() => setFillOfflinePeers((v) => !v)}
                className="rounded border border-line bg-raised px-2.5 py-1 text-ink hover:border-buoy"
              >
                {fillOfflinePeers ? "Show Raw Peer State" : "Preview Simulated Peers"}
              </button>
            )}
            <a
              href={links.grafana_url}
              target="_blank"
              rel="noreferrer"
              className="rounded border border-line bg-raised px-2.5 py-1 text-buoy hover:border-buoy"
            >
              Grafana (:3001)
            </a>
            <a
              href={`${C_URL}/metrics`}
              target="_blank"
              rel="noreferrer"
              className="rounded border border-line bg-raised px-2.5 py-1 text-ink hover:border-buoy"
            >
              C /metrics
            </a>
            <button
              type="button"
              onClick={() => void refetch()}
              className="rounded bg-buoy px-3 py-1 font-medium text-harbour"
            >
              {isFetching ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </header>

        <section className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4" aria-label="Cluster KPIs">
          <MetricCard
            label="Request Rate"
            value={`${cluster.request_rate_rps.toFixed(2)} req/s`}
            sub={`${cluster.requests_total} total requests across ${snapshot.services_up} active services`}
          />
          <MetricCard
            label="Errors & Rejections"
            value={`${cluster.error_rate_pct.toFixed(2)}%`}
            sub={`${cluster.errors_total} errors/503s/rejections observed`}
            tone={cluster.error_rate_pct > 5 ? "down" : "ink"}
          />
          <MetricCard
            label="Critical Gate & AI p95 Latency"
            value={`${(serviceD?.latency_ms.p95 ?? 0).toFixed(1)} ms / ${ai.llm_inference_ms.p95.toFixed(1)} ms`}
            sub={`Risk Gate D p95 vs ${ai.model} LLM inference p95`}
            tone="up"
          />
          <MetricCard
            label="AI Token Cost"
            value={`$${ai.estimated_equivalent_cost_usd.toFixed(5)}`}
            sub={`$${ai.direct_api_spend_usd.toFixed(2)} billed (${ai.execution_mode}) · ${ai.total_tokens.toLocaleString()} toks`}
            tone="buoy"
          />
        </section>

        <section className="rounded border border-line bg-panel p-4" aria-label="Per-Service RED Metrics">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-ink">Per-Service RED Breakdown (Request Rate · Errors · Latency)</h2>
            <span className="text-[11px] text-muted">
              Updated {new Date(snapshot.generated_at_ms).toLocaleTimeString()} · Scrape interval 5s
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-line text-[11px] uppercase text-muted">
                  <th className="py-2 pr-3">Service</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Request Rate</th>
                  <th className="py-2 pr-3">Requests</th>
                  <th className="py-2 pr-3">Errors</th>
                  <th className="py-2 pr-3">Error %</th>
                  <th className="py-2 pr-3">Latency (p50 / p95 / p99)</th>
                  <th className="py-2">Resilience &amp; Saturation Gauges</th>
                </tr>
              </thead>
              <tbody>
                {services.map((svc) => {
                  const lat = svc.latency_ms;
                  const gaugeEntries = Object.entries(svc.key_gauges);
                  return (
                    <tr key={svc.id} className="border-b border-line/70 last:border-0">
                      <td className="py-2.5 pr-3">
                        <div className="font-medium text-ink">{svc.name}</div>
                        <div className="text-[11px] text-muted">{svc.owner}</div>
                      </td>
                      <td className="py-2.5 pr-3">
                        <StatusBadge status={svc.status} />
                      </td>
                      <td className="py-2.5 pr-3 font-medium">{svc.request_rate_rps.toFixed(2)} /s</td>
                      <td className="py-2.5 pr-3">{svc.requests_total.toLocaleString()}</td>
                      <td className="py-2.5 pr-3">{svc.errors_total.toLocaleString()}</td>
                      <td className="py-2.5 pr-3">
                        <span className={svc.error_rate_pct > 5 ? "text-down" : "text-ink"}>
                          {svc.error_rate_pct.toFixed(2)}%
                        </span>
                      </td>
                      <td className="py-2.5 pr-3">
                        <div>
                          {lat.p50.toFixed(1)} / {lat.p95.toFixed(1)} / {lat.p99.toFixed(1)} ms
                        </div>
                        {lat.note && <div className="text-[10px] text-muted">{lat.note}</div>}
                      </td>
                      <td className="py-2.5 text-[11px] text-muted">
                        {gaugeEntries.length > 0
                          ? gaugeEntries.map(([k, v]) => `${k}: ${String(v)}`).join(" · ")
                          : svc.error ?? "Not reachable from Service C host"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <section className="grid grid-cols-1 gap-4 lg:grid-cols-2" aria-label="Throughput, Latency Budget & AI Spend">
          <div className="flex flex-col gap-3 rounded border border-line bg-panel p-4">
            <h2 className="text-sm font-semibold text-ink">Traffic &amp; Latency Budget Utilization</h2>
            <div className="flex flex-col gap-3">
              {services.map((svc) => (
                <RatioBar
                  key={svc.id}
                  label={svc.name}
                  valueText={`${svc.request_rate_rps.toFixed(2)} req/s (${svc.requests_total} total)`}
                  pct={(svc.request_rate_rps / maxRps) * 100}
                  color={svc.status === "offline" ? "bg-down/60" : "bg-up"}
                />
              ))}
              <div className="mt-1 border-t border-line pt-3">
                <RatioBar
                  label="Service D Gate p95 vs 500ms budget"
                  valueText={`${(serviceD?.latency_ms.p95 ?? 0).toFixed(1)} / 500 ms`}
                  pct={((serviceD?.latency_ms.p95 ?? 0) / 500) * 100}
                  color="bg-up"
                />
                <div className="mt-2.5">
                  <RatioBar
                    label="Service C Ollama p95 vs 3000ms timeout"
                    valueText={`${ai.llm_inference_ms.p95.toFixed(1)} / 3000 ms`}
                    pct={(ai.llm_inference_ms.p95 / 3000) * 100}
                    color="bg-buoy"
                  />
                </div>
              </div>
            </div>
          </div>

          <div className="flex flex-col gap-3 rounded border border-line bg-panel p-4">
            <h2 className="text-sm font-semibold text-ink">AI Component Token Economics &amp; Safety Rails</h2>
            <div className="grid grid-cols-2 gap-3 text-xs">
              <div className="rounded border border-line bg-raised p-3">
                <div className="text-[11px] text-muted">Token Split (Prompt / Eval)</div>
                <div className="mt-1 text-base font-semibold text-ink">
                  {ai.prompt_tokens.toLocaleString()} / {ai.eval_tokens.toLocaleString()}
                </div>
                <div className="mt-0.5 text-[11px] text-muted">
                  Baseline ${ai.prompt_cost_per_1m_usd}/1M in · ${ai.eval_cost_per_1m_usd}/1M out
                </div>
              </div>
              <div className="rounded border border-line bg-raised p-3">
                <div className="text-[11px] text-muted">Unit Spend &amp; Gate Savings</div>
                <div className="mt-1 text-base font-semibold text-buoy">
                  ${ai.cost_per_1k_requests_usd.toFixed(4)} / 1k chats
                </div>
                <div className="mt-0.5 text-[11px] text-up">
                  {ai.tokens_saved_by_gates.toLocaleString()} tokens avoided pre-LLM (${ai.estimated_cost_saved_usd.toFixed(5)})
                </div>
              </div>
              <div className="rounded border border-line bg-raised p-3">
                <div className="text-[11px] text-muted">Refusal Rate &amp; Breakdown</div>
                <div className="mt-1 text-base font-semibold text-ink">{(ai.refusal_rate * 100).toFixed(1)}%</div>
                <div className="mt-0.5 text-[11px] text-muted">
                  Insuff: {ai.refusal_total.insufficient_evidence ?? 0} · Injection: {ai.refusal_total.unsafe_injection ?? 0} ·
                  Advice: {ai.refusal_total.investment_advice ?? 0}
                </div>
              </div>
              <div className="rounded border border-line bg-raised p-3">
                <div className="text-[11px] text-muted">RAG Traceability &amp; DLQ</div>
                <div className="mt-1 text-base font-semibold text-ink">FTS5 v{ai.index_version}</div>
                <div className="mt-0.5 text-[11px] text-muted">
                  Stripped citations: {ai.hallucinated_citations_stripped_total} · DLQ: {ai.dlq_messages_count} · C Reqs:{" "}
                  {serviceC?.requests_total ?? 0}
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}
