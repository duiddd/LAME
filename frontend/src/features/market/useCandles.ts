import { useEffect, useRef, useState } from "react";
import { api } from "../../shared/api";
import { CANDLE_POLL_MS } from "../../shared/config";
import type { CandlesReply, Interval, Watermark } from "../../shared/types";
import { type Bar, mergeBars, toBar } from "./candles";

const DELAYED_AFTER_MS = 5000;

/** Polls the newest candles. The watermark makes A also return older candles a resend changed. */
export function useCandles(symbol: string, interval: Interval, feedVolume: number | undefined) {
  const [bars, setBars] = useState<Bar[]>([]);
  const [delayed, setDelayed] = useState(false);
  const watermark = useRef<Watermark | null>(null);
  const lastAdvance = useRef(Date.now());
  const volumeChanged = useRef(0);
  const lastVolume = useRef<number | undefined>(undefined);

  if (feedVolume !== undefined && feedVolume !== lastVolume.current) {
    lastVolume.current = feedVolume;
    volumeChanged.current = Date.now();
  }

  useEffect(() => {
    let cancelled = false;
    let busy = false;
    watermark.current = null;
    setBars([]);

    const poll = async () => {
      if (busy || document.hidden) {
        return;
      }
      busy = true;
      try {
        const params = new URLSearchParams({ symbol, interval });
        if (watermark.current) {
          params.set("after_day", watermark.current.b_day);
          params.set("after_seq", String(watermark.current.b_seq));
        }
        const reply = await api<CandlesReply>(`/api/market/candles?${params}`);
        if (cancelled) {
          return;
        }
        const advanced = reply.watermark?.b_seq !== watermark.current?.b_seq || reply.watermark?.b_day !== watermark.current?.b_day;
        if (reply.candles.length > 0 || reply.replace_all) {
          setBars((current) => mergeBars(current, reply.candles.map(toBar), reply.replace_all));
        }
        watermark.current = reply.watermark;
        if (advanced) {
          lastAdvance.current = Date.now();
        }
        // B reports trades that A has not heard about: the path through D is behind.
        setDelayed(volumeChanged.current - lastAdvance.current > 0 && Date.now() - lastAdvance.current > DELAYED_AFTER_MS);
      } catch {
        // The next poll retries; the chart keeps what it has.
      } finally {
        busy = false;
      }
    };

    void poll();
    const timer = setInterval(() => void poll(), CANDLE_POLL_MS);
    const onVisible = () => void poll();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [symbol, interval]);

  return { bars, delayed };
}
