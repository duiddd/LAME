import { useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../../shared/api";
import { groupDecimal, isMarketOpen } from "../../shared/format";
import type { Order, SymbolSummary } from "../../shared/types";
import { mergeOrder } from "../orders/merge";
import { ACCOUNT_KEY, ORDERS_KEY, useAccount } from "../orders/useOrders";

const REASONS: Record<string, string> = {
  RATE_LIMITED: "You are placing orders too quickly. Wait a moment and try again.",
  INVALID_TICK: "The price is not on the allowed price steps.",
  INVALID_LOT: "The amount is not on the allowed amount steps.",
  QTY_TOO_SMALL: "The amount is below the minimum for this pair.",
  QTY_TOO_LARGE: "The amount is above the maximum for this pair.",
  NOTIONAL_TOO_LARGE: "The order's value is above the maximum for this pair.",
  PRICE_OUT_OF_BAND: "The price is more than 10% from the market price.",
  NO_FRESH_PRICE: "Market data is interrupted. Try again when it returns.",
  NO_LIQUIDITY: "There are no orders to trade against. Place a limit order instead.",
  INSUFFICIENT_BALANCE: "You do not have enough available funds. Deposit more or reduce the amount.",
  SYSTEM_BUSY: "The system is busy and the order was not sent. Try again shortly.",
};

export function OrderForm({ summary, signedIn }: { summary: SymbolSummary; signedIn: boolean }) {
  const client = useQueryClient();
  const { data: account } = useAccount(signedIn);
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [type, setType] = useState<"LIMIT" | "MARKET">("LIMIT");
  const [price, setPrice] = useState("");
  const [qty, setQty] = useState("");
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  // Kept across an unanswered attempt, so that a retry cannot place the order twice.
  const attemptKey = useRef<string | null>(null);

  const payAsset = side === "BUY" ? summary.quote : summary.base;
  const available = account?.balances.find((b) => b.asset === payAsset)?.available ?? "0";
  const open = isMarketOpen();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage(null);
    attemptKey.current ??= crypto.randomUUID();
    try {
      const order = await api<Order>("/api/orders", {
        method: "POST",
        headers: { "Idempotency-Key": attemptKey.current },
        json: { symbol: summary.symbol, side, type, price: type === "LIMIT" ? price : null, qty },
      });
      attemptKey.current = null;
      client.setQueryData<Order[]>(ORDERS_KEY, (list) => mergeOrder(list, order));
      void client.invalidateQueries({ queryKey: ACCOUNT_KEY });
      setMessage({ tone: "ok", text: order.status === "UNKNOWN" ? "Confirming the order. Do not place it again." : "Order sent." });
    } catch (err) {
      if (err instanceof ApiError) {
        // The server answered, so that attempt is finished and a new one needs a new key.
        attemptKey.current = null;
        setMessage({ tone: "error", text: REASONS[err.reason] ?? err.message });
      } else {
        setMessage({ tone: "error", text: "No reply from the server. Trying again is safe: it cannot place the order twice." });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex h-full flex-col gap-2 overflow-auto bg-panel p-3" aria-label="Place an order">
      <div className="grid grid-cols-2 gap-1" role="group" aria-label="Side">
        {(["BUY", "SELL"] as const).map((s) => (
          <button
            key={s}
            type="button"
            aria-pressed={side === s}
            onClick={() => setSide(s)}
            className={`rounded py-1.5 font-medium ${side === s ? (s === "BUY" ? "bg-up text-harbour" : "bg-down text-harbour") : "bg-raised text-muted"}`}
          >
            {s === "BUY" ? "Buy" : "Sell"} {summary.base}
          </button>
        ))}
      </div>
      <div className="flex gap-3" role="group" aria-label="Order type">
        {(["LIMIT", "MARKET"] as const).map((t) => (
          <button key={t} type="button" aria-pressed={type === t} onClick={() => setType(t)} className={type === t ? "text-buoy" : "text-muted hover:text-ink"}>
            {t === "LIMIT" ? "Limit" : "Market"}
          </button>
        ))}
        {signedIn && (
          <span className="ml-auto text-muted">
            Available {groupDecimal(available)} {payAsset}
          </span>
        )}
      </div>
      {type === "LIMIT" ? (
        <label className="flex items-center gap-2 rounded border border-line bg-harbour px-2">
          <span className="w-14 text-muted">Price</span>
          <input required inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} className="min-w-0 flex-1 bg-transparent py-1.5 text-right outline-none" />
          <span className="text-muted">{summary.quote}</span>
        </label>
      ) : (
        <p className="rounded border border-line bg-harbour px-2 py-1.5 text-muted">Trades at the best available price</p>
      )}
      <label className="flex items-center gap-2 rounded border border-line bg-harbour px-2">
        <span className="w-14 text-muted">Amount</span>
        <input required inputMode="decimal" value={qty} onChange={(e) => setQty(e.target.value)} className="min-w-0 flex-1 bg-transparent py-1.5 text-right outline-none" />
        <span className="text-muted">{summary.base}</span>
      </label>
      {!open && <p className="text-buoy">Market closed. Orders are accepted 09:30 to 16:00 Dublin time.</p>}
      {message && (
        <p role={message.tone === "error" ? "alert" : "status"} className={message.tone === "error" ? "text-down" : "text-up"}>
          {message.text}
        </p>
      )}
      <div className="mt-auto">
        {signedIn ? (
          <button disabled={busy} className={`w-full rounded py-2 font-semibold text-harbour disabled:opacity-50 ${side === "BUY" ? "bg-up" : "bg-down"}`}>
            {side === "BUY" ? "Buy" : "Sell"} {summary.base}
          </button>
        ) : (
          <Link to="/login" className="block rounded bg-buoy py-2 text-center font-semibold text-harbour">
            Log in to trade
          </Link>
        )}
      </div>
    </form>
  );
}
