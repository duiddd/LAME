import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect } from "react";
import { api, ApiError, refreshSession } from "../../shared/api";
import { TOKEN_REFRESH_MS } from "../../shared/config";
import type { User } from "../../shared/types";

export function useAuth() {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["me"],
    retry: false,
    staleTime: Infinity,
    queryFn: async () => {
      try {
        return await api<User>("/api/auth/me");
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          return null;
        }
        throw e;
      }
    },
  });
  const user = query.data ?? null;

  // Refresh before expiry, so that the stream's reconnect after the server closes it succeeds.
  useEffect(() => {
    if (!user) {
      return;
    }
    const timer = setInterval(() => void refreshSession(), TOKEN_REFRESH_MS);
    return () => clearInterval(timer);
  }, [user]);

  const signIn = useCallback(
    async (mode: "login" | "register", username: string, password: string) => {
      const signedIn = await api<User>(`/api/auth/${mode}`, { method: "POST", json: { username, password } });
      client.setQueryData(["me"], signedIn);
    },
    [client],
  );

  const signOut = useCallback(async () => {
    await api("/api/auth/logout", { method: "POST" }).catch(() => undefined);
    client.clear();
    client.setQueryData(["me"], null);
  }, [client]);

  return { user, loading: query.isLoading, signIn, signOut };
}
