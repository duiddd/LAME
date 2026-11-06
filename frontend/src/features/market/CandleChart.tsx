import {
  CandlestickSeries,
  ColorType,
  createChart,
  HistogramSeries,
  type IChartApi,
  type ISeriesApi,
  LineSeries,
  type Time,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import type { Interval } from "../../shared/types";
import { type Bar, movingAverage } from "./candles";
import { useCandles } from "./useCandles";

const INTERVALS: Interval[] = ["5s", "15s", "1m", "5m"];
const AVERAGES = [
  { period: 7, color: "#f2b84b" },
  { period: 25, color: "#7fb7e8" },
  { period: 99, color: "#c79be8" },
];
const UP = "#35c495";
const DOWN = "#ef6f84";

interface Series {
  candles: ISeriesApi<"Candlestick">;
  volume: ISeriesApi<"Histogram">;
  averages: ISeriesApi<"Line">[];
}

function draw(series: Series, bars: Bar[]) {
  series.candles.setData(bars.map((b) => ({ ...b, time: b.time as Time })));
  series.volume.setData(
    bars.map((b) => ({
      time: b.time as Time,
      value: b.volume,
      color: b.close >= b.open ? "rgba(53,196,149,.35)" : "rgba(239,111,132,.35)",
    })),
  );
  AVERAGES.forEach(({ period }, i) => {
    series.averages[i].setData(movingAverage(bars, period).map((p) => ({ time: p.time as Time, value: p.value })));
  });
}

export function CandleChart({ symbol, feedVolume, decimals }: { symbol: string; feedVolume?: number; decimals: number }) {
  const [interval, setInterval_] = useState<Interval>("15s");
  const { bars, delayed } = useCandles(symbol, interval, feedVolume);
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<Series | null>(null);

  useEffect(() => {
    const c = createChart(host.current!, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "#142330" },
        textColor: "#8ba2b2",
        fontSize: 11,
      },
      grid: { vertLines: { color: "#1b2e3e" }, horzLines: { color: "#1b2e3e" } },
      rightPriceScale: { borderColor: "#263b4c", scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: { borderColor: "#263b4c", timeVisible: true, secondsVisible: true, rightOffset: 4 },
    });
    const candles = c.addSeries(CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
      borderVisible: false,
      priceFormat: { type: "price", precision: decimals, minMove: 10 ** -decimals },
    });
    const volume = c.addSeries(HistogramSeries, { priceScaleId: "", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const averages = AVERAGES.map(({ color }) =>
      c.addSeries(LineSeries, { color, lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false }),
    );
    chart.current = c;
    series.current = { candles, volume, averages };
    return () => {
      c.remove();
      chart.current = null;
      series.current = null;
    };
  }, [decimals]);

  useEffect(() => {
    if (series.current) {
      draw(series.current, bars);
    }
  }, [bars]);

  return (
    <section className="flex h-full min-h-0 flex-col bg-panel" aria-label="Price chart">
      <div className="flex items-center gap-1 border-b border-line px-3 py-1.5">
        <div role="group" aria-label="Candle interval" className="flex gap-1">
          {INTERVALS.map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={value === interval}
              onClick={() => setInterval_(value)}
              className={`rounded px-2 py-0.5 ${value === interval ? "bg-raised text-buoy" : "text-muted hover:text-ink"}`}
            >
              {value}
            </button>
          ))}
        </div>
        <div className="ml-3 flex gap-3 text-[11px]">
          {AVERAGES.map(({ period, color }) => (
            <span key={period} style={{ color }}>
              MA({period})
            </span>
          ))}
        </div>
        {delayed && <span className="ml-auto text-[11px] text-buoy">Trade data delayed</span>}
      </div>
      <div className="relative min-h-0 flex-1">
        <div ref={host} className="absolute inset-0" />
        {bars.length === 0 && (
          <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-muted">Candles appear after the first trade</p>
        )}
      </div>
    </section>
  );
}
