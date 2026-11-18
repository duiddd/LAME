import { Navigate, useParams } from "react-router-dom";
import { useAuth } from "../features/auth/useAuth";
import { ChatBox, NewsPanel } from "../features/analyst/Panels";
import { CandleChart } from "../features/market/CandleChart";
import { dayChange, MarketList } from "../features/market/MarketList";
import { OrderBook } from "../features/market/OrderBook";
import { useFeed } from "../features/market/useFeed";
import { useSummary } from "../features/market/useSummary";
import { OrderTabs } from "../features/orders/OrderTabs";
import { OrderForm } from "../features/trade/OrderForm";
import { formatUnits } from "../shared/format";
import type { SymbolSummary } from "../shared/types";

function PairHeader({ s }: { s: SymbolSummary }) {
  const { data } = useFeed(s.symbol);
  const change = dayChange(data?.last_price, s.day_open, s.price_decimals);
  const tone = change == null ? "" : change >= 0 ? "text-up" : "text-down";
  return (
    <header className="flex flex-wrap items-baseline gap-x-5 gap-y-1 bg-panel px-4 py-2">
      <h1 className="text-base font-semibold">
        {s.base}/{s.quote}
      </h1>
      <p className={`text-2xl font-semibold ${tone}`}>{data?.last_price != null ? formatUnits(data.last_price, s.price_decimals) : "No trades yet"}</p>
      {change != null && <p className={tone}>{`${change >= 0 ? "+" : ""}${(change * 100).toFixed(2)}% today`}</p>}
      {data && (
        <p className="text-muted">
          Volume {formatUnits(data.volume, s.qty_decimals)} {s.base}
        </p>
      )}
    </header>
  );
}

export function TradePage() {
  const { symbol = "" } = useParams();
  const { user } = useAuth();
  const { data, isError } = useSummary();
  const summary = data?.symbols.find((s) => s.symbol === symbol);
  const view = useFeed(symbol);

  if (isError) {
    return <main className="p-6 text-down">The trading service cannot be reached. This page retries by itself.</main>;
  }
  if (!data) {
    return <main className="p-6 text-muted">Loading markets</main>;
  }
  if (!summary) {
    return <Navigate to={`/trade/${data.symbols[0].symbol}`} replace />;
  }

  return (
    <main
      className="grid grid-cols-[minmax(0,1fr)] gap-px bg-line pb-px lg:h-full lg:min-h-0 lg:grid-cols-[250px_minmax(0,1fr)_320px] lg:grid-rows-[auto_minmax(0,1.5fr)_250px_minmax(150px,0.8fr)]"
      style={{ gridAutoRows: "minmax(0, auto)" }}
    >
      <div className="lg:col-span-3">
        <PairHeader s={summary} />
      </div>
      <div className="lg:col-start-1 lg:row-start-2 lg:row-span-2">
        <OrderBook view={view} summary={summary} />
      </div>
      <div className="h-[420px] lg:col-start-2 lg:row-start-2 lg:h-auto lg:min-h-0">
        <CandleChart symbol={symbol} feedVolume={view.data?.volume} decimals={summary.price_decimals} />
      </div>
      <div className="lg:col-start-2 lg:row-start-3 lg:min-h-0">
        <OrderForm summary={summary} signedIn={user !== null} />
      </div>
      <div className="grid gap-px bg-line lg:col-start-3 lg:row-start-2 lg:row-span-3 lg:min-h-0 lg:grid-rows-[auto_minmax(0,1.2fr)_minmax(0,1fr)]">
        <MarketList symbols={data.symbols} />
        <div className="h-[360px] lg:h-auto lg:min-h-0">
          <ChatBox symbol={symbol} />
        </div>
        <div className="h-[280px] lg:h-auto lg:min-h-0">
          <NewsPanel symbol={symbol} />
        </div>
      </div>
      <div className="min-h-[220px] lg:col-span-2 lg:col-start-1 lg:row-start-4 lg:min-h-0">
        <OrderTabs signedIn={user !== null} />
      </div>
    </main>
  );
}
