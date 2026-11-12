import { useQuery } from "@tanstack/react-query";
import { C_URL, NEWS_REFRESH_MS } from "../../shared/config";
import type { NewsItem } from "../../shared/types";

/** C runs apart from trading: anything failing here only affects C's own panels. */
export function useServiceCReachable() {
  const { data, isLoading } = useQuery({
    queryKey: ["service-c"],
    retry: false,
    refetchInterval: NEWS_REFRESH_MS,
    queryFn: async () => {
      try {
        await fetch(`${C_URL}/healthz`, { signal: AbortSignal.timeout(3000) });
        return true;
      } catch {
        return false;
      }
    },
  });
  return { reachable: data === true, checking: isLoading };
}

export function useNews(symbol: string) {
  return useQuery({
    queryKey: ["news", symbol],
    refetchInterval: NEWS_REFRESH_MS,
    retry: false,
    queryFn: async (): Promise<NewsItem[]> => {
      const res = await fetch(`${C_URL}/api/news?symbol=${encodeURIComponent(symbol)}&include_global=true&limit=20`, { signal: AbortSignal.timeout(5000) });
      if (!res.ok) {
        throw new Error(`News request failed with status ${res.status}`);
      }
      return res.json();
    },
  });
}
