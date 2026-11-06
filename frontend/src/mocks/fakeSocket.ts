import type { MarketData } from "../shared/types";
import { marketFor, onTrade } from "./state";

/** Stands in for B's `/md/<symbol>` in mock mode: a snapshot on connect, then changes and heartbeats. */
export class FakeSocket {
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  private readonly timer: ReturnType<typeof setInterval>;

  constructor(url: string) {
    const symbol = url.slice(url.lastIndexOf("/") + 1);
    const send = () => {
      const data: MarketData = marketFor(symbol);
      this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
    };
    setTimeout(send, 0);
    this.timer = setInterval(() => {
      onTrade(symbol);
      send();
    }, 500);
  }

  close(): void {
    clearInterval(this.timer);
  }
}
