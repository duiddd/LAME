import { NavLink } from "react-router-dom";
import { formatUnits } from "../../shared/format";
import type { SymbolSummary } from "../../shared/types";
import { useFeed } from "./useFeed";

/** Change since the day's first trade; null until both prices exist. */
export function dayChange(lastUnits: number | null | undefined, dayOpen: string | null, priceDecimals: number): number | null {
  if (lastUnits == null || dayOpen == null || Number(dayOpen) === 0) {
    return null;
  }
  const open = Number(dayOpen);
  return (lastUnits / 10 ** priceDecimals - open) / open;
}

function MarketRow({ s }: { s: SymbolSummary }) {
  const { data } = useFeed(s.symbol);
  const change = dayChange(data?.last_price, s.day_open, s.price_decimals);
  return (
    <NavLink
      to={`/trade/${s.symbol}`}
      className={({ isActive }) => `grid grid-cols-3 px-3 py-1.5 hover:bg-raised ${isActive ? "bg-raised" : ""}`}
    >
      <span className="font-medium">
        {s.base}/{s.quote}
      </span>
      <span className="text-right">{data?.last_price != null ? formatUnits(data.last_price, s.price_decimals) : "None"}</span>
      <span className={`text-right ${change == null ? "text-muted" : change >= 0 ? "text-up" : "text-down"}`}>
        {change == null ? "None" : `${change >= 0 ? "+" : ""}${(change * 100).toFixed(2)}%`}
      </span>
    </NavLink>
  );
}

export function MarketList({ symbols }: { symbols: SymbolSummary[] }) {
  return (
    <section className="bg-panel" aria-label="Markets">
      <div className="grid grid-cols-3 border-b border-line px-3 py-2 text-[11px] text-muted">
        <span>Pair</span>
        <span className="text-right">Last price</span>
        <span className="text-right">Today</span>
      </div>
      {symbols.map((s) => (
        <MarketRow key={s.symbol} s={s} />
      ))}
    </section>
  );
}
