/** The one client for A's API: same-origin cookies, and one shared refresh on a 401. */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly reason: string,
    message: string,
  ) {
    super(message);
  }
}

let refreshing: Promise<boolean> | null = null;

/** Concurrent 401s share one refresh, because a refresh token can be spent only once. */
export function refreshSession(fetchImpl: typeof fetch = fetch): Promise<boolean> {
  refreshing ??= fetchImpl("/api/auth/refresh", { method: "POST" })
    .then((r) => r.ok)
    .catch(() => false)
    .finally(() => {
      refreshing = null;
    });
  return refreshing;
}

async function toError(res: Response): Promise<ApiError> {
  const body = await res.json().catch(() => null);
  return new ApiError(res.status, body?.reason ?? "UNKNOWN", body?.message ?? `The request failed with status ${res.status}.`);
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, ...rest } = init;
  const options: RequestInit = { ...rest };
  if (json !== undefined) {
    options.body = JSON.stringify(json);
    options.headers = { "Content-Type": "application/json", ...rest.headers };
  }
  let res = await fetch(path, options);
  if (res.status === 401 && path !== "/api/auth/login" && (await refreshSession())) {
    res = await fetch(path, options);
  }
  if (!res.ok) {
    throw await toError(res);
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}
