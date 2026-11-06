import { groupDecimal } from "../../shared/format";
import type { Order } from "../../shared/types";

const OPEN = new Set<Order["status"]>(["PENDING", "SENT", "UNKNOWN", "PROCESSING", "MODIFYING", "PARTIALLY_FILLED"]);
const DELAYED_AFTER_MS = 10000;

export const CONFIRMING_NOTICE = "Network problem. We are confirming this order; funds stay held. Do not place it again.";

const REASONS: Record<string, string> = {
  END_OF_DAY: "Market closed",
  SELF_TRADE: "Would trade with your own order",
  SELF_TRADE_PREVENTED: "Would trade with your own order",
  WASH_TRADE: "Would trade with a linked account",
  RISK_TIMEOUT: "Risk check timed out",
  CANCELLED_BEFORE_SUBMIT: "Cancelled before it was sent",
  D_UNAVAILABLE: "System busy; the order was not sent",
  D_TIMEOUT: "Cancelled after a timeout",
  NOT_DELIVERED: "Could not reach the matching engine",
  EXPIRED: "Expired before it could be matched",
  INVALID_PRICE: "Invalid price",
  INVALID_QTY: "Invalid quantity",
};

export function isOpen(order: Order): boolean {
  return OPEN.has(order.status);
}

export function reasonText(reason: string | null): string | null {
  return reason ? (REASONS[reason] ?? reason) : null;
}

/** The label a user sees; internal statuses stay complete because each moves funds differently. */
export function statusLabel(order: Order, now: number = Date.now()): string {
  const filled = `${groupDecimal(order.filled_qty)} / ${groupDecimal(order.qty)}`;
  const anyFilled = Number(order.filled_qty) > 0;
  if (order.cancel_requested && OPEN.has(order.status)) {
    return "Cancelling";
  }
  switch (order.status) {
    case "PENDING":
    case "SENT":
      return now - order.created_at > DELAYED_AFTER_MS ? "Processing delayed" : "Sending";
    case "UNKNOWN":
      return "Confirming";
    case "PROCESSING":
      return "Open";
    case "MODIFYING":
      return "Modifying";
    case "PARTIALLY_FILLED":
      return `Partially filled ${filled}`;
    case "FILLED":
      return "Filled";
    case "CANCELLED":
      if (order.type === "MARKET" && anyFilled) {
        return `Remainder cancelled, filled ${filled}`;
      }
      return anyFilled ? `Cancelled, filled ${filled}` : "Cancelled";
    case "REJECTED":
      return order.reason === "END_OF_DAY" ? "Market closed" : "Rejected";
    case "EXPIRED":
      return "Expired";
  }
}
