/** Three candles, the middle one short: a lame candle that still stands. */
export function Logo({ large = false }: { large?: boolean }) {
  const size = large ? 32 : 22;
  return (
    <span className="inline-flex items-center gap-2 font-semibold" aria-label="LAME Exchange">
      <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden>
        <rect x="3" y="4" width="5" height="16" rx="1" fill="#f2b84b" />
        <rect x="10" y="13" width="5" height="7" rx="1" fill="#f2b84b" opacity=".55" />
        <rect x="17" y="2" width="5" height="18" rx="1" fill="#f2b84b" />
      </svg>
      <span className={large ? "text-xl tracking-wide" : "text-base tracking-wide"}>LAME</span>
    </span>
  );
}
