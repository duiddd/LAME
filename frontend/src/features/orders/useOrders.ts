import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api, refreshSession } from "../../shared/api";
import { EventStream } from "../../shared/stream";
import type { Account, BalanceUpdateEvent, Fill, Order, OrderUpdateEvent } from "../../shared/types";
import { mergeOrder } from "./merge";

export const ORDERS_KEY = ["orders"];
export const FILLS_KEY = ["fills"];
export const ACCOUNT_KEY = ["account"];

export function useOrders(enabled: boolean) {
  return useQuery({
    queryKey: ORDERS_KEY,
    enabled,
    queryFn: async () => (await api<{ orders: Order[] }>("/api/orders?limit=200")).orders,
  });
}

export function useFills(enabled: boolean) {
  return useQuery({
    queryKey: FILLS_KEY,
    enabled,
    queryFn: async () => (await api<{ fills: Fill[] }>("/api/fills?limit=200")).fills,
  });
}

export function useAccount(enabled: boolean) {
  return useQuery({ queryKey: ACCOUNT_KEY, enabled, queryFn: () => api<Account>("/api/accounts/me") });
}

/** Keeps the caches current from the user's event stream while signed in. */
export function useUserStream(enabled: boolean) {
  const client = useQueryClient();
  const fillsTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!enabled) {
      return;
    }
    const stream = new EventStream({
      refresh: () => refreshSession(),
      onReset: () => void client.invalidateQueries(),
      onEvent: (type, data) => {
        if (type === "order_update") {
          const { order } = data as OrderUpdateEvent;
          void client.cancelQueries({ queryKey: ORDERS_KEY });
          client.setQueryData<Order[]>(ORDERS_KEY, (list) => mergeOrder(list, order));
          fillsTimer.current ??= setTimeout(() => {
            fillsTimer.current = null;
            void client.invalidateQueries({ queryKey: FILLS_KEY });
          }, 200);
        } else if (type === "balance_update") {
          const { balances } = data as BalanceUpdateEvent;
          void client.cancelQueries({ queryKey: ACCOUNT_KEY });
          client.setQueryData<Account>(ACCOUNT_KEY, (account) => {
            if (!account) {
              return account;
            }
            const byAsset = new Map(account.balances.map((b) => [b.asset, b]));
            balances.forEach((b) => byAsset.set(b.asset, b));
            return { ...account, balances: [...byAsset.values()] };
          });
        }
      },
    });
    stream.start();
    return () => stream.stop();
  }, [enabled, client]);
}
