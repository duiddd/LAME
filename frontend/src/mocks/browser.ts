import { delay, getResponse, http, HttpResponse } from "msw";
import { C_URL } from "../shared/config";
import type { Balance, Order } from "../shared/types";
import { balances, day, fills, listeners, markets, orders, push, pushed, session, user } from "./state";

const fixed = (units: number, decimals: number) => (units / 10 ** decimals).toFixed(decimals);
const INTERVALS: Record<string, number> = { "5s": 5000, "15s": 15000, "1m": 60000, "5m": 300000 };
// The team grid (B's pairs.json): 6 quantity decimals, so a quote asset has 2 + 6 = 8.
const QD = 6;
const LOT: Record<string, string> = { "BTC-USDT": "0.0001", "ETH-USDT": "0.001" };
const DEC: Record<string, number> = { USDT: 8, BTC: QD, ETH: QD, SOL: QD, NVDA: QD, AAPL: QD, ASML: QD, TSM: QD, RELIANCE: QD, GOLD: QD };

function balance(asset: string): Balance {
  let b = balances.find((x) => x.asset === asset);
  if (!b) {
    const d = DEC[asset] ?? QD;
    b = { asset, available: fixed(0, d), held: fixed(0, d), total: fixed(0, d) };
    balances.push(b);
  }
  return b;
}

function adjust(asset: string, available: number, held: number) {
  const b = balance(asset);
  const d = DEC[asset] ?? QD;
  b.available = (Number(b.available) + available).toFixed(d);
  b.held = (Number(b.held) + held).toFixed(d);
  b.total = (Number(b.available) + Number(b.held)).toFixed(d);
  push("balance_update", { balances: [b] });
}

function emit(order: Order, patch: Partial<Order>) {
  Object.assign(order, patch, { updated_at: Date.now() });
  push("order_update", { order: { ...order } });
}

/** Walks an order through the same steps as A: held, sent, accepted, filled. */
function run(order: Order) {
  const [base, quote] = order.symbol.split("-");
  const lastPrice = (markets[order.symbol].last / 100).toFixed(2);
  const price = Number(order.price ?? lastPrice);
  const qty = Number(order.qty);
  setTimeout(() => emit(order, { status: "PROCESSING" }), 500);
  setTimeout(() => {
    if (order.cancel_requested || order.status !== "PROCESSING") {
      return;
    }
    const part = order.type === "MARKET" ? qty : Number((qty / 2).toFixed(QD));
    const fillPrice = order.type === "MARKET" ? lastPrice : order.price!;
    if (order.side === "BUY") {
      adjust(quote, 0, -part * price);
      adjust(base, part, 0);
    } else {
      adjust(base, 0, -part);
      adjust(quote, part * Number(fillPrice), 0);
    }
    fills.unshift({ b_day: day(), b_seq: fills.length + 1, order_id: order.order_id, symbol: order.symbol, side: order.side, role: "TAKER", price: fillPrice, qty: part.toFixed(QD), ts: Date.now() });
    emit(order, {
      status: part === qty ? "FILLED" : "PARTIALLY_FILLED",
      filled_qty: part.toFixed(QD),
      settled_qty: part.toFixed(QD),
      avg_fill_price: fillPrice,
    });
  }, 1500);
}

const handlers = [
  http.get("/api/auth/me", () => (session.signedIn ? HttpResponse.json(user) : HttpResponse.json({ reason: "UNAUTHORIZED", message: "Log in first" }, { status: 401 }))),
  http.post("/api/auth/refresh", () => new HttpResponse(null, { status: session.signedIn ? 204 : 401 })),
  http.post("/api/auth/login", async ({ request }) => {
    const body = (await request.json()) as { username: string };
    session.signedIn = true;
    user.username = body.username;
    return HttpResponse.json(user);
  }),
  http.post("/api/auth/register", async ({ request }) => {
    const body = (await request.json()) as { username: string };
    session.signedIn = true;
    user.username = body.username;
    return HttpResponse.json(user, { status: 201 });
  }),
  http.post("/api/auth/logout", () => {
    session.signedIn = false;
    return new HttpResponse(null, { status: 204 });
  }),
  http.get("/api/accounts/me", () => HttpResponse.json({ ...user, balances })),
  http.post("/api/accounts/deposits", async ({ request }) => {
    const body = (await request.json()) as { asset: string; amount: string };
    adjust(body.asset, Number(body.amount), 0);
    return HttpResponse.json({ deposit_id: 1, asset: body.asset, amount: body.amount, balance: balance(body.asset) }, { status: 201 });
  }),
  http.get("/api/orders", () => HttpResponse.json({ orders })),
  http.post("/api/orders", async ({ request }) => {
    const body = (await request.json()) as { symbol: string; side: "BUY" | "SELL"; type: "LIMIT" | "MARKET"; price: string | null; qty: string };
    const [base, quote] = body.symbol.split("-");
    const lastPrice = markets[body.symbol].last / 100;
    const holdPrice = body.type === "MARKET" ? lastPrice * 1.05 : Number(body.price);
    const need = body.side === "BUY" ? Number(body.qty) * holdPrice : Number(body.qty);
    const asset = body.side === "BUY" ? quote : base;
    if (Number(balance(asset).available) < need) {
      return HttpResponse.json({ reason: "INSUFFICIENT_BALANCE", message: "Not enough funds" }, { status: 400 });
    }
    adjust(asset, -need, need);
    const now = Date.now();
    const order: Order = {
      order_id: crypto.randomUUID(),
      client_order_id: request.headers.get("Idempotency-Key") ?? "",
      symbol: body.symbol,
      side: body.side,
      type: body.type,
      price: body.price,
      qty: body.qty,
      filled_qty: (0).toFixed(QD),
      settled_qty: (0).toFixed(QD),
      avg_fill_price: null,
      status: "SENT",
      reason: null,
      cancel_requested: false,
      created_at: now,
      updated_at: now,
    };
    orders.unshift(order);
    run(order);
    try {
      fetch(`${C_URL}/api/activity`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: body.symbol,
          action: body.side,
          qty: Number(body.qty),
          price: Number(body.price ?? lastPrice),
          source: "service_a",
        }),
      }).catch(() => {});
    } catch {
      /* Service C is off the trading path; never block order placement */
    }
    await delay(60);
    return HttpResponse.json(order, { status: 202 });
  }),
  http.delete("/api/orders/:id", ({ params }) => {
    const order = orders.find((o) => o.order_id === params.id);
    if (!order) {
      return HttpResponse.json({ reason: "NOT_FOUND", message: "No such order" }, { status: 404 });
    }
    emit(order, { cancel_requested: true });
    setTimeout(() => {
      const [base, quote] = order.symbol.split("-");
      const rest = Number(order.qty) - Number(order.filled_qty);
      if (order.side === "BUY") {
        const amount = rest * Number(order.price ?? markets[order.symbol].last / 100);
        adjust(quote, amount, -amount);
      } else {
        adjust(base, rest, -rest);
      }
      emit(order, { status: "CANCELLED", cancel_requested: false });
    }, 500);
    return HttpResponse.json(order, { status: 202 });
  }),
  http.put("/api/orders/:id", () => HttpResponse.json({ reason: "MODIFY_UNAVAILABLE", message: "Modify is not available" }, { status: 501 })),
  http.get("/api/fills", () => HttpResponse.json({ fills })),
  http.get("/api/market/summary", () =>
    HttpResponse.json({
      symbols: Object.entries(markets).map(([symbol, m]) => {
        const [base, quote] = symbol.split("-");
        return { symbol, base, quote, tick: "0.01", lot: LOT[symbol] ?? "0.0001", price_decimals: 2, qty_decimals: QD, day: day(), day_open: fixed(m.dayOpen, 2) };
      }),
    }),
  ),
  http.get("/api/market/candles", ({ request }) => {
    const url = new URL(request.url);
    const symbol = url.searchParams.get("symbol")!;
    const interval = url.searchParams.get("interval") ?? "15s";
    const afterSeq = Number(url.searchParams.get("after_seq") ?? 0);
    const size = INTERVALS[interval];
    const market = markets[symbol];
    const trades = market.trades;
    const last = trades[trades.length - 1];
    const from = afterSeq ? Math.floor((trades.find((t) => t.seq > afterSeq)?.t ?? last.t) / size) * size : 0;
    const buckets = new Map<number, { t: number; o: number; h: number; l: number; c: number; v: number }>();
    for (const t of trades) {
      if (t.t < from) {
        continue;
      }
      const key = Math.floor(t.t / size) * size;
      const b = buckets.get(key);
      if (b) {
        b.h = Math.max(b.h, t.price);
        b.l = Math.min(b.l, t.price);
        b.c = t.price;
        b.v += t.qty;
      } else {
        buckets.set(key, { t: key, o: t.price, h: t.price, l: t.price, c: t.price, v: t.qty });
      }
    }
    const candles = [...buckets.values()].slice(-300).map((b) => ({ t: b.t, o: fixed(b.o, 2), h: fixed(b.h, 2), l: fixed(b.l, 2), c: fixed(b.c, 2), v: fixed(b.v, QD) }));
    return HttpResponse.json({ symbol, interval, candles, watermark: { b_day: day(), b_seq: last.seq }, replace_all: !afterSeq });
  }),
];

/** Answers A's API inside the page, so that mock mode needs no service worker. */
class FakeEventSource extends EventTarget {
  readyState = 1;
  onerror: (() => void) | null = null;
  private sent = pushed.length;
  private readonly flush = () => {
    for (; this.sent < pushed.length; this.sent++) {
      const e = pushed[this.sent];
      this.dispatchEvent(new MessageEvent(e.event, { data: JSON.stringify(e.data), lastEventId: String(e.id) }));
    }
  };

  constructor() {
    super();
    listeners.add(this.flush);
  }

  close(): void {
    this.readyState = 2;
    listeners.delete(this.flush);
  }
}

export function installMockApi(): void {
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const request = new Request(input, init);
    const url = new URL(request.url);
    if (url.origin === location.origin && url.pathname.startsWith("/api/")) {
      const response = await getResponse(handlers, request);
      if (response) {
        return response;
      }
    }
    return realFetch(input, init);
  };
  window.EventSource = FakeEventSource as unknown as typeof EventSource;
}
