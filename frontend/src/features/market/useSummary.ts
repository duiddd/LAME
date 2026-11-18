import { useQuery } from "@tanstack/react-query";
import { api } from "../../shared/api";
import type { components } from "../../shared/api-types";

export function useSummary() {
  return useQuery({
    queryKey: ["market-summary"],
    queryFn: () => api<components["schemas"]["SummaryOut"]>("/api/market/summary"),
    refetchInterval: 30000,
  });
}
