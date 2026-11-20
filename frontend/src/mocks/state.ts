import type { Balance, Fill, MarketData, Order, User } from "../shared/types";

/** The fake backend's memory for mock mode. Prices are in cents and quantities in thousandths. */

export interface FakeMarket {
  last: number;
  volume: number;
  dayOpen: number;
  trades: { t: number; price: number; qty: number; seq: number }[];
}

const DAY = new Date().toISOString().slice(0, 10);
let seq = 0;

function seedMarket(start: number): FakeMarket {
  const market: FakeMarket = { last: start, volume: 0, dayOpen: start, trades: [] };
  const now = Date.now();
  let price = start;
  for (let t = now - 3 * 3600 * 1000; t < now; t += 2500) {
    price = Math.max(100, price + Math.round((Math.random() - 0.495) * start * 0.0009));
    const qty = (1 + Math.floor(Math.random() * 40)) * 1000; // 0.001-0.040 at 6 decimals
    market.trades.push({ t, price, qty, seq: ++seq });
    market.volume += qty;
  }
  market.last = price;
  return market;
}

export const markets: Record<string, FakeMarket> = {
  "BTC-USDT": seedMarket(6425000),
  "ETH-USDT": seedMarket(345000),
  "SOL-USDT": seedMarket(17840),
  "NVDA-USDT": seedMarket(13860),
  "AAPL-USDT": seedMarket(22840),
  "ASML-USDT": seedMarket(68250),
  "TSM-USDT": seedMarket(19280),
  "RELIANCE-USDT": seedMarket(134000),
  "GOLD-USDT": seedMarket(274500),
};

export function day(): string {
  return DAY;
}

export function marketFor(symbol: string): MarketData {
  const m = markets[symbol];
  const step = Math.max(1, Math.round(m.last * 0.0002));
  const depth = () => (5 + Math.floor(Math.random() * 400)) * 1000; // 6 quantity decimals
  return {
    bids: [1, 2, 3].map((i) => ({ price: m.last - i * step, qty: depth() })),
    asks: [1, 2, 3].map((i) => ({ price: m.last + i * step, qty: depth() })),
    last_price: m.last,
    volume: m.volume,
    last_ms: Date.now(),
  };
}

export function onTrade(symbol: string): void {
  const m = markets[symbol];
  const price = Math.max(100, m.last + Math.round((Math.random() - 0.5) * m.last * 0.0009));
  const qty = (1 + Math.floor(Math.random() * 40)) * 1000; // 0.001-0.040 at 6 decimals
  m.trades.push({ t: Date.now(), price, qty, seq: ++seq });
  m.last = price;
  m.volume += qty;
}

export const user: User = { user_id: 100001, username: "demo", role: "USER" };
export const session = { signedIn: false };
export const orders: Order[] = [];
export const fills: Fill[] = [];
export const balances: Balance[] = [];
export const pushed: { id: number; event: string; data: unknown }[] = [];
export const listeners = new Set<() => void>();

let eventId = 0;

export function push(event: string, data: unknown): void {
  pushed.push({ id: ++eventId, event, data });
  listeners.forEach((l) => l());
}
