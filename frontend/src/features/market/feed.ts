import { STALE_AFTER_MS, VIEW_UPDATE_MS } from "../../shared/config";
import type { MarketData } from "../../shared/types";

export type FeedStatus = "connecting" | "live" | "interrupted";

export interface FeedView {
  data: MarketData | null;
  status: FeedStatus;
}

export type SocketFactory = (url: string) => Pick<WebSocket, "close" | "onmessage" | "onclose" | "onerror" | "onopen">;

/**
 * One pair's connection to B. B can send about 1,000 snapshots a second and each is complete, so
 * only the newest is kept and subscribers are told at most every VIEW_UPDATE_MS. Freshness comes
 * from receiving any message: a heartbeat repeats last_ms, so last_ms only orders snapshots.
 */
export class MarketFeed {
  private view: FeedView = { data: null, status: "connecting" };
  private latest: MarketData | null = null;
  private lastReceived = 0;
  private newestMs = -1;
  private dirty = false;
  private socket: ReturnType<SocketFactory> | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private reconnect: ReturnType<typeof setTimeout> | null = null;
  private backoff = 250;
  private readonly listeners = new Set<() => void>();

  constructor(
    private readonly url: string,
    private readonly factory: SocketFactory,
    private readonly now: () => number = Date.now,
  ) {}

  /** For useSyncExternalStore: the first subscriber connects and the last one disconnects. */
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    if (this.listeners.size === 1) {
      this.start();
    }
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) {
        this.stop();
      }
    };
  };

  getView = (): FeedView => this.view;

  /** Called on a timer, and when a hidden tab comes back. */
  tick = (): void => {
    const stale = this.now() - this.lastReceived > STALE_AFTER_MS;
    const status: FeedStatus = this.lastReceived === 0 ? "connecting" : stale ? "interrupted" : "live";
    if (this.dirty || status !== this.view.status) {
      this.dirty = false;
      this.view = { data: this.latest, status };
      this.listeners.forEach((l) => l());
    }
  };

  receive(text: string): void {
    const message = JSON.parse(text) as MarketData;
    this.lastReceived = this.now();
    if (message.last_ms < this.newestMs) {
      return;
    }
    if (message.last_ms > this.newestMs || this.latest === null) {
      this.newestMs = message.last_ms;
      this.latest = message;
      this.dirty = true;
    }
  }

  private start(): void {
    this.timer = setInterval(this.tick, VIEW_UPDATE_MS);
    this.connect();
  }

  private stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
    }
    if (this.reconnect) {
      clearTimeout(this.reconnect);
    }
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onclose = null;
      socket.close();
    }
  }

  private connect(): void {
    // B may have restarted with a new day, whose last_ms starts again from 0.
    this.newestMs = -1;
    const socket = this.factory(this.url);
    this.socket = socket;
    socket.onopen = () => {
      this.backoff = 250;
    };
    socket.onmessage = (e: MessageEvent<string>) => this.receive(e.data);
    socket.onerror = () => undefined;
    socket.onclose = () => {
      this.reconnect = setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 5000);
    };
  }
}
