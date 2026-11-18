import { formatUnits } from "../../shared/format";
import type { MarketData, SymbolSummary } from "../../shared/types";
import type { FeedView } from "./feed";

type Level = MarketData["bids"][number];

function Row({ level, max, side, s }: { level: Level; max: number; side: "bid" | "ask"; s: SymbolSummary }) {
  return (
    <div className="relative grid grid-cols-2 px-3 py-1">
      <div
        aria-hidden
        className={`absolute inset-y-0 right-0 ${side === "bid" ? "bg-up/15" : "bg-down/15"}`}
        style={{ width: `${(level.qty / max) * 100}%` }}
      />
      <span className={`relative ${side === "bid" ? "text-up" : "text-down"}`}>{formatUnits(level.price, s.price_decimals)}</span>
      <span className="relative text-right">{formatUnits(level.qty, s.qty_decimals)}</span>
    </div>
  );
}

export function OrderBook({ view, summary }: { view: FeedView; summary: SymbolSummary }) {
  const data = view.data;
  const bids = data?.bids ?? [];
  const asks = data?.asks ?? [];
  const max = Math.max(1, ...bids.map((l) => l.qty), ...asks.map((l) => l.qty));
  const interrupted = view.status !== "live";

  return (
    <section className="flex h-full flex-col bg-panel" aria-label="Order book">
      <h2 className="border-b border-line px-3 py-2 font-medium">Order book</h2>
      <div className="grid grid-cols-2 px-3 py-1 text-[11px] text-muted">
        <span>Price ({summary.quote})</span>
        <span className="text-right">Amount ({summary.base})</span>
      </div>
      <div className={interrupted ? "opacity-40" : ""}>
        <div className="flex min-h-[72px] flex-col justify-end">
          {[...asks].reverse().map((l) => (
            <Row key={l.price} level={l} max={max} side="ask" s={summary} />
          ))}
        </div>
        <div className="border-y border-line px-3 py-2 text-lg font-semibold">
          {data?.last_price != null ? formatUnits(data.last_price, summary.price_decimals) : "No trades yet"}
        </div>
        <div className="min-h-[72px]">
          {bids.map((l) => (
            <Row key={l.price} level={l} max={max} side="bid" s={summary} />
          ))}
        </div>
      </div>
      {view.status === "interrupted" && (
        <p role="status" className="px-3 py-2 text-buoy">
          Market data interrupted. New orders are paused until it returns.
        </p>
      )}
      {view.status === "connecting" && <p className="px-3 py-2 text-muted">Connecting to market data</p>}
    </section>
  );
}
