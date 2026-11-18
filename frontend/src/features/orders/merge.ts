import type { Order } from "../../shared/types";

/**
 * Applies a pushed order to the cached list. An event carries the order's full state and is
 * applied only if it is not older than the cache, so a slow REST reply cannot undo it.
 */
export function mergeOrder(list: Order[] | undefined, incoming: Order): Order[] {
  const current = list ?? [];
  const index = current.findIndex((o) => o.order_id === incoming.order_id);
  if (index < 0) {
    return [incoming, ...current];
  }
  if (current[index].updated_at > incoming.updated_at) {
    return current;
  }
  const next = current.slice();
  next[index] = incoming;
  return next;
}
