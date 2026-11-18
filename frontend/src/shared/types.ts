import type { components } from "./api-types";

type Schemas = components["schemas"];

export type Order = Schemas["OrderOut"];
export type OrderDetail = Schemas["OrderDetailOut"];
export type Fill = Schemas["FillOut"];
export type Balance = Schemas["BalanceOut"];
export type Account = Schemas["AccountOut"];
export type User = Schemas["UserOut"];
export type SymbolSummary = Schemas["SymbolSummaryOut"];
export type Candle = Schemas["CandleOut"];
export type CandlesReply = Schemas["CandlesOut"];
export type Watermark = Schemas["Watermark"];
export type Interval = CandlesReply["interval"];
export type OrderInput = Schemas["OrderIn"];

/** One message on B's `/md/<symbol>`. Prices and quantities are integer units. */
export interface MarketData {
  bids: { price: number; qty: number }[];
  asks: { price: number; qty: number }[];
  last_price: number | null;
  volume: number;
  last_ms: number;
}

/** The SSE events, which OpenAPI cannot describe. */
export interface OrderUpdateEvent {
  order: Order;
}
export interface BalanceUpdateEvent {
  balances: Balance[];
}

export interface NewsItem {
  news_id: string;
  timestamp_ms: number;
  symbols: string[];
  headline: string;
  body: string;
  source: string;
  sanitized?: boolean;
  index_version?: number;
}
