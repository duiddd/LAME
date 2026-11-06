import { useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../../shared/api";
import { formatTime, groupDecimal } from "../../shared/format";
import type { Order } from "../../shared/types";
import { CONFIRMING_NOTICE, isOpen, reasonText, statusLabel } from "./statusLabels";
import { mergeOrder } from "./merge";
import { ACCOUNT_KEY, ORDERS_KEY, useAccount, useFills, useOrders } from "./useOrders";

const TABS = ["Open orders", "Order history", "Trade history", "Holdings"] as const;
type Tab = (typeof TABS)[number];

const th = "px-3 py-1.5 font-normal text-muted text-left whitespace-nowrap";
const td = "px-3 py-1.5 whitespace-nowrap";

function sideClass(side: string) {
  return side === "BUY" ? "text-up" : "text-down";
}

function ModifyForm({ order, onDone }: { order: Order; onDone(message?: string): void }) {
  const client = useQueryClient();
  const [price, setPrice] = useState(order.price ?? "");
  const [qty, setQty] = useState(order.qty);

  async function submit(e: FormEvent) {
    e.preventDefault();
    try {
      const updated = await api<Order>(`/api/orders/${order.order_id}`, { method: "PUT", json: { price, qty } });
      client.setQueryData<Order[]>(ORDERS_KEY, (list) => mergeOrder(list, updated));
      onDone();
    } catch (err) {
      onDone(err instanceof ApiError && err.reason === "MODIFY_UNAVAILABLE" ? "Modifying orders is not available yet. Cancel the order and place a new one." : err instanceof Error ? err.message : "The order could not be modified.");
    }
  }

  return (
    <form onSubmit={submit} className="flex items-center gap-2">
      <input aria-label="New price" value={price} onChange={(e) => setPrice(e.target.value)} className="w-24 rounded border border-line bg-harbour px-2 py-0.5" />
      <input aria-label="New remaining amount" value={qty} onChange={(e) => setQty(e.target.value)} className="w-20 rounded border border-line bg-harbour px-2 py-0.5" />
      <button className="text-buoy">Save</button>
      <button type="button" onClick={() => onDone()} className="text-muted">
        Close
      </button>
    </form>
  );
}

function OpenOrders({ orders }: { orders: Order[] }) {
  const client = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function cancel(order: Order) {
    setMessage(null);
    try {
      const updated = await api<Order>(`/api/orders/${order.order_id}`, { method: "DELETE" });
      client.setQueryData<Order[]>(ORDERS_KEY, (list) => mergeOrder(list, updated));
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "The order could not be cancelled.");
    }
  }

  if (orders.length === 0) {
    return <p className="p-4 text-muted">No open orders. Orders you place appear here until they finish.</p>;
  }
  return (
    <>
      {message && (
        <p role="alert" className="px-3 pt-2 text-down">
          {message}
        </p>
      )}
      {orders.some((o) => o.status === "UNKNOWN") && (
        <p role="status" className="px-3 pt-2 text-buoy">
          {CONFIRMING_NOTICE}
        </p>
      )}
      <table className="w-full">
        <thead>
          <tr>
            <th className={th}>Time</th>
            <th className={th}>Pair</th>
            <th className={th}>Side</th>
            <th className={th}>Type</th>
            <th className={th}>Price</th>
            <th className={th}>Amount</th>
            <th className={th}>Status</th>
            <th className={th}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => {
            const canChange = !o.cancel_requested && !["PENDING", "UNKNOWN", "MODIFYING"].includes(o.status);
            return (
              <tr key={o.order_id} className="border-t border-line">
                <td className={td}>{formatTime(o.created_at)}</td>
                <td className={td}>{o.symbol}</td>
                <td className={`${td} ${sideClass(o.side)}`}>{o.side === "BUY" ? "Buy" : "Sell"}</td>
                <td className={td}>{o.type === "LIMIT" ? "Limit" : "Market"}</td>
                <td className={td}>{o.type === "MARKET" ? "Market" : groupDecimal(o.price ?? "")}</td>
                <td className={td}>{groupDecimal(o.qty)}</td>
                <td className={td}>{statusLabel(o)}</td>
                <td className={td}>
                  {editing === o.order_id ? (
                    <ModifyForm
                      order={o}
                      onDone={(m) => {
                        setEditing(null);
                        setMessage(m ?? null);
                      }}
                    />
                  ) : (
                    <span className="flex gap-3">
                      {canChange && o.type === "LIMIT" && (
                        <button type="button" onClick={() => setEditing(o.order_id)} className="text-muted hover:text-ink">
                          Modify
                        </button>
                      )}
                      {!o.cancel_requested && (
                        <button type="button" onClick={() => void cancel(o)} className="text-buoy hover:underline">
                          Cancel
                        </button>
                      )}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

function History({ orders }: { orders: Order[] }) {
  if (orders.length === 0) {
    return <p className="p-4 text-muted">No finished orders yet.</p>;
  }
  return (
    <table className="w-full">
      <thead>
        <tr>
          <th className={th}>Time</th>
          <th className={th}>Pair</th>
          <th className={th}>Side</th>
          <th className={th}>Type</th>
          <th className={th}>Price</th>
          <th className={th}>Average fill</th>
          <th className={th}>Filled</th>
          <th className={th}>Status</th>
        </tr>
      </thead>
      <tbody>
        {orders.map((o) => (
          <tr key={o.order_id} className="border-t border-line">
            <td className={td}>{formatTime(o.created_at)}</td>
            <td className={td}>{o.symbol}</td>
            <td className={`${td} ${sideClass(o.side)}`}>{o.side === "BUY" ? "Buy" : "Sell"}</td>
            <td className={td}>{o.type === "LIMIT" ? "Limit" : "Market"}</td>
            <td className={td}>{o.type === "MARKET" ? "Market" : groupDecimal(o.price ?? "")}</td>
            <td className={td}>{o.avg_fill_price ? groupDecimal(o.avg_fill_price) : "None"}</td>
            <td className={td}>
              {groupDecimal(o.filled_qty)} / {groupDecimal(o.qty)}
            </td>
            <td className={td}>
              {statusLabel(o)}
              {o.status === "REJECTED" && o.reason !== "END_OF_DAY" && reasonText(o.reason) ? `: ${reasonText(o.reason)}` : ""}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Trades() {
  const { data } = useFills(true);
  if (!data?.length) {
    return <p className="p-4 text-muted">No trades yet.</p>;
  }
  return (
    <table className="w-full">
      <thead>
        <tr>
          <th className={th}>Time</th>
          <th className={th}>Pair</th>
          <th className={th}>Side</th>
          <th className={th}>Price</th>
          <th className={th}>Amount</th>
          <th className={th}>Role</th>
        </tr>
      </thead>
      <tbody>
        {data.map((f) => (
          <tr key={`${f.b_day}-${f.b_seq}-${f.order_id}`} className="border-t border-line">
            <td className={td}>{formatTime(f.ts)}</td>
            <td className={td}>{f.symbol}</td>
            <td className={`${td} ${sideClass(f.side)}`}>{f.side === "BUY" ? "Buy" : "Sell"}</td>
            <td className={td}>{groupDecimal(f.price)}</td>
            <td className={td}>{groupDecimal(f.qty)}</td>
            <td className={td}>{f.role === "TAKER" ? "Taker" : "Maker"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Holdings() {
  const { data } = useAccount(true);
  if (!data?.balances.length) {
    return <p className="p-4 text-muted">No funds yet. Deposit simulated funds to start trading.</p>;
  }
  return (
    <table className="w-full">
      <thead>
        <tr>
          <th className={th}>Asset</th>
          <th className={th}>Available</th>
          <th className={th}>Held by orders</th>
          <th className={th}>Total</th>
        </tr>
      </thead>
      <tbody>
        {data.balances.map((b) => (
          <tr key={b.asset} className="border-t border-line">
            <td className={td}>{b.asset}</td>
            <td className={td}>{groupDecimal(b.available)}</td>
            <td className={td}>{groupDecimal(b.held)}</td>
            <td className={td}>{groupDecimal(b.total)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function OrderTabs({ signedIn }: { signedIn: boolean }) {
  const [tab, setTab] = useState<Tab>("Open orders");
  const { data: orders = [] } = useOrders(signedIn);

  return (
    <section className="flex h-full min-h-0 flex-col bg-panel" aria-label="Your orders and funds">
      <div role="tablist" className="flex gap-4 border-b border-line px-3">
        {TABS.map((name) => (
          <button
            key={name}
            role="tab"
            type="button"
            aria-selected={name === tab}
            onClick={() => setTab(name)}
            className={`-mb-px border-b-2 py-2 ${name === tab ? "border-buoy text-ink" : "border-transparent text-muted hover:text-ink"}`}
          >
            {name}
            {name === "Open orders" && signedIn ? ` (${orders.filter(isOpen).length})` : ""}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {!signedIn ? (
          <p className="p-4 text-muted">
            <Link to="/login" className="text-buoy hover:underline">
              Log in
            </Link>{" "}
            to see your orders and funds.
          </p>
        ) : tab === "Open orders" ? (
          <OpenOrders orders={orders.filter(isOpen)} />
        ) : tab === "Order history" ? (
          <History orders={orders.filter((o) => !isOpen(o))} />
        ) : tab === "Trade history" ? (
          <Trades />
        ) : (
          <Holdings />
        )}
      </div>
    </section>
  );
}

export { ACCOUNT_KEY };
