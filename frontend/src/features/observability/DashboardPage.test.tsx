import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DashboardPage } from "./DashboardPage";
import { FALLBACK_MOCK_OBSERVABILITY } from "./useObservability";

describe("DashboardPage observability", () => {
  it("renders request rate, errors, latency, and AI token cost across all services", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => FALLBACK_MOCK_OBSERVABILITY,
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <DashboardPage />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText("LAME Cluster & AI Observability")).toBeInTheDocument();
    });

    expect(screen.getByText("40.50 req/s")).toBeInTheDocument();
    expect(screen.getByText("0.55%")).toBeInTheDocument();
    expect(screen.getByText("$0.00287")).toBeInTheDocument();
    expect(screen.getByText("1.5 ms / 380.0 ms")).toBeInTheDocument();
    expect(screen.getAllByText(/Service A · Trading & Accounts/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/Service B · Matching Engine/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/Service C · News & AI Analyst/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/Service D · Risk Control/).length).toBeGreaterThanOrEqual(2);

    vi.unstubAllGlobals();
  });
});
