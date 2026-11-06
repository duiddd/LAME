/**
 * Frontend logic that protects what the user sees and does.
 *
 * | Test | Purpose | Method |
 * | --- | --- | --- |
 * | status labels | Users read a plain label for each internal status | Order in each status -> expected label |
 * | delayed label | A stuck order says so | SENT for more than 10 s -> "Processing delayed" |
 * | merge keeps newer | A slow REST reply cannot undo a pushed update | Older event -> list unchanged |
 * | bars merge older | A resend after an outage can correct old candles | Incoming older time -> replaced in order |
 * | moving average | MA matches its definition | Known closes -> expected values, none before the window |
 * | feed coalesces | 1,000 messages do not mean 1,000 renders | Many messages, one tick -> one notification, newest kept |
 * | feed ignores old | An older snapshot cannot overwrite a newer one | Lower last_ms -> kept newest |
 * | heartbeat is fresh | A quiet market is not "interrupted" | Same last_ms keeps status live |
 * | silence interrupts | A dead feed is reported | No message for 3 s -> interrupted |
 * | refresh is shared | A refresh token can be spent only once | Two concurrent refreshes -> one request |
 * | stream reopens with ID | Events missed during a final close are replayed | Closed source -> refresh, new URL with last_event_id |
 * | market hours | The UI says when orders are refused | Times inside and outside the session |
 */
import { describe, expect, it, vi } from "vitest";
import { refreshSession } from "../shared/api";
import { isMarketOpen } from "../shared/format";
import { EventStream } from "../shared/stream";
import type { Order } from "../shared/types";
import { mergeBars, movingAverage, type Bar } from "./market/candles";
import { MarketFeed } from "./market/feed";
import { mergeOrder } from "./orders/merge";
import { statusLabel } from "./orders/statusLabels";

const NOW = 1_000_000;

function order(patch: Partial<Order> = {}): Order {
  return {
    order_id: "o1",
    client_order_id: "c1",
    symbol: "BTC-USDT",
    side: "BUY",
    type: "LIMIT",
    price: "50000.00",
    qty: "1.000",
    filled_qty: "0.000",
    settled_qty: "0.000",
    avg_fill_price: null,
    status: "SENT",
    reason: null,
    cancel_requested: false,
    created_at: NOW,
    updated_at: NOW,
    ...patch,
  };
}

function bar(time: number, close: number): Bar {
  return { time, open: close, high: close, low: close, close, volume: 1 };
}

describe("status labels", () => {
  it.each([
    [{ status: "SENT" }, "Sending"],
    [{ status: "UNKNOWN" }, "Confirming"],
    [{ status: "PROCESSING" }, "Open"],
    [{ status: "PARTIALLY_FILLED", filled_qty: "0.300" }, "Partially filled 0.300 / 1.000"],
    [{ status: "FILLED", filled_qty: "1.000" }, "Filled"],
    [{ status: "CANCELLED" }, "Cancelled"],
    [{ status: "CANCELLED", filled_qty: "0.300" }, "Cancelled, filled 0.300 / 1.000"],
    [{ status: "CANCELLED", type: "MARKET", filled_qty: "0.300" }, "Remainder cancelled, filled 0.300 / 1.000"],
    [{ status: "REJECTED", reason: "END_OF_DAY" }, "Market closed"],
    [{ status: "PROCESSING", cancel_requested: true }, "Cancelling"],
  ] as [Partial<Order>, string][])("%o -> %s", (patch, label) => {
    expect(statusLabel(order(patch), NOW)).toBe(label);
  });

  it("says when an order is stuck", () => {
    expect(statusLabel(order(), NOW + 10001)).toBe("Processing delayed");
  });
});

describe("merging", () => {
  it("keeps the newer order", () => {
    const newer = order({ status: "FILLED", updated_at: NOW + 5 });
    expect(mergeOrder([newer], order({ status: "PROCESSING", updated_at: NOW }))[0].status).toBe("FILLED");
    expect(mergeOrder([order()], newer)[0].status).toBe("FILLED");
  });

  it("corrects older candles in order", () => {
    const merged = mergeBars([bar(10, 1), bar(30, 3)], [bar(20, 2), bar(10, 9)], false);
    expect(merged.map((b) => [b.time, b.close])).toEqual([[10, 9], [20, 2], [30, 3]]);
  });

  it("computes a moving average from a full window only", () => {
    const ma = movingAverage([bar(1, 2), bar(2, 4), bar(3, 6), bar(4, 8)], 3);
    expect(ma).toEqual([{ time: 3, value: 4 }, { time: 4, value: 6 }]);
  });
});

describe("market feed", () => {
  const message = (last_ms: number, last_price = 100) => JSON.stringify({ bids: [], asks: [], last_price, volume: 0, last_ms });

  function feed() {
    const clock = { t: 5000 };
    const f = new MarketFeed("ws://b/md/X", () => ({ close() {} }) as never, () => clock.t);
    const notified = vi.fn();
    f.subscribe(notified);
    return { f, clock, notified };
  }

  it("coalesces many messages into one update and keeps the newest", () => {
    const { f, notified } = feed();
    for (let i = 1; i <= 1000; i++) {
      f.receive(message(i, i));
    }
    f.tick();
    expect(notified).toHaveBeenCalledTimes(1);
    expect(f.getView().data?.last_price).toBe(1000);
  });

  it("ignores an older snapshot", () => {
    const { f } = feed();
    f.receive(message(50, 500));
    f.receive(message(40, 400));
    f.tick();
    expect(f.getView().data?.last_price).toBe(500);
  });

  it("treats a heartbeat as fresh even though last_ms repeats", () => {
    const { f, clock } = feed();
    f.receive(message(50));
    clock.t += 2500;
    f.receive(message(50));
    clock.t += 2500;
    f.tick();
    expect(f.getView().status).toBe("live");
  });

  it("reports silence as interrupted", () => {
    const { f, clock } = feed();
    f.receive(message(50));
    f.tick();
    clock.t += 3001;
    f.tick();
    expect(f.getView().status).toBe("interrupted");
  });
});

describe("session", () => {
  it("shares one refresh between concurrent callers", async () => {
    const fetchImpl = vi.fn(async () => new Response(null, { status: 204 }));
    const [a, b] = await Promise.all([refreshSession(fetchImpl as never), refreshSession(fetchImpl as never)]);
    expect([a, b]).toEqual([true, true]);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("reopens the stream with the last event ID after a final close", async () => {
    const opened: string[] = [];
    const sources: { readyState: number; onerror: (() => void) | null; listeners: Record<string, (e: unknown) => void> }[] = [];
    const factory = (url: string) => {
      opened.push(url);
      const s = {
        readyState: 1,
        onerror: null as (() => void) | null,
        listeners: {} as Record<string, (e: unknown) => void>,
        addEventListener(type: string, fn: (e: unknown) => void) {
          this.listeners[type] = fn;
        },
        close() {},
      };
      sources.push(s);
      return s as unknown as EventSource;
    };
    const refresh = vi.fn(async () => true);
    const stream = new EventStream({ onEvent: () => {}, onReset: () => {}, refresh }, factory, 0);
    stream.start();
    sources[0].listeners.order_update({ lastEventId: "1042", data: "{}" });
    sources[0].readyState = 2;
    sources[0].onerror?.();
    await vi.waitFor(() => expect(opened).toHaveLength(2));
    expect(refresh).toHaveBeenCalledOnce();
    expect(opened[1]).toBe("/api/stream?last_event_id=1042");
    stream.stop();
  });

  it("knows the trading session", () => {
    expect(isMarketOpen(new Date("2026-11-06T10:00:00Z"))).toBe(true);
    expect(isMarketOpen(new Date("2026-11-06T09:29:00Z"))).toBe(false);
    expect(isMarketOpen(new Date("2026-11-06T16:00:00Z"))).toBe(false);
    expect(isMarketOpen(new Date("2026-07-06T09:00:00Z"))).toBe(true);
  });
});
