import { C_URL } from "../../shared/config";
import { formatTime } from "../../shared/format";
import { useNews, useServiceCReachable } from "./useServiceC";

const OFFLINE = "AI analyst offline. Trading is not affected.";

export function ChatBox() {
  const { reachable, checking } = useServiceCReachable();
  return (
    <section className="flex h-full min-h-0 flex-col bg-panel" aria-label="AI analyst chat">
      {reachable ? (
        <iframe title="AI market analyst" src={`${C_URL}/widget`} className="min-h-0 flex-1 border-0" />
      ) : (
        <p className="p-3 text-muted">{checking ? "Connecting to the AI analyst" : OFFLINE}</p>
      )}
    </section>
  );
}

export function NewsPanel({ symbol }: { symbol: string }) {
  const { data, isError, isLoading } = useNews(symbol);
  return (
    <section className="flex h-full min-h-0 flex-col bg-panel" aria-label={`News for ${symbol}`}>
      <h2 className="border-b border-line px-3 py-2 font-medium">Market news</h2>
      <div className="min-h-0 flex-1 overflow-auto">
        {isError && <p className="p-3 text-muted">News is unavailable. Trading is not affected.</p>}
        {isLoading && <p className="p-3 text-muted">Loading news</p>}
        {data?.length === 0 && <p className="p-3 text-muted">No news for this pair yet.</p>}
        {data?.map((item) => (
          <article key={item.news_id} className="border-b border-line px-3 py-2 last:border-0">
            <h3 className="leading-snug">{item.headline}</h3>
            <p className="mt-0.5 text-[11px] text-muted">
              {item.source}, {formatTime(item.timestamp_ms)}
            </p>
          </article>
        ))}
      </div>
    </section>
  );
}

export function AnalystPage() {
  const { reachable, checking } = useServiceCReachable();
  if (!reachable) {
    return (
      <main className="flex h-full items-center justify-center p-6 text-muted">
        {checking ? "Connecting to the AI analyst" : OFFLINE}
      </main>
    );
  }
  return <iframe title="News and AI analyst" src={`${C_URL}/`} className="h-full w-full border-0" />;
}
