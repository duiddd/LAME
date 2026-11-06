import type { Candle } from "../../shared/types";

export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

const MAX_BARS = 1000;

export function toBar(c: Candle): Bar {
  return {
    time: Math.floor(c.t / 1000),
    open: Number(c.o),
    high: Number(c.h),
    low: Number(c.l),
    close: Number(c.c),
    volume: Number(c.v),
  };
}

/** Merges by time: a resend after an outage can change older candles, not only the newest. */
export function mergeBars(current: Bar[], incoming: Bar[], replaceAll: boolean): Bar[] {
  if (replaceAll) {
    return incoming.slice(-MAX_BARS);
  }
  const byTime = new Map(current.map((b) => [b.time, b]));
  for (const bar of incoming) {
    byTime.set(bar.time, bar);
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time).slice(-MAX_BARS);
}

/** Simple moving average of closes; bars before the first full window have no value. */
export function movingAverage(bars: Bar[], period: number): { time: number; value: number }[] {
  const out: { time: number; value: number }[] = [];
  let sum = 0;
  bars.forEach((bar, i) => {
    sum += bar.close;
    if (i >= period) {
      sum -= bars[i - period].close;
    }
    if (i >= period - 1) {
      out.push({ time: bar.time, value: sum / period });
    }
  });
  return out;
}
