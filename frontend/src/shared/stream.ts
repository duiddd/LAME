/**
 * The user's event stream. EventSource reconnects by itself and sends Last-Event-ID, but it
 * stops for good on a non-200 reply, and a new EventSource does not send that header. So after
 * a final close this refreshes the session and reopens with the last ID in the URL.
 */
export interface StreamHandlers {
  onEvent(type: string, data: unknown): void;
  /** The server no longer holds the events after our last ID: refetch everything. */
  onReset(): void;
  refresh(): Promise<boolean>;
}

type SourceFactory = (url: string) => EventSource;

export const EVENT_TYPES = ["order_update", "balance_update"];

export class EventStream {
  private source: EventSource | null = null;
  private lastId: string | null = null;
  private stopped = false;
  private retry: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly handlers: StreamHandlers,
    private readonly factory: SourceFactory = (url) => new EventSource(url),
    private readonly retryMs = 3000,
  ) {}

  start(): void {
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    if (this.retry) {
      clearTimeout(this.retry);
    }
    this.source?.close();
    this.source = null;
  }

  private open(): void {
    const url = this.lastId === null ? "/api/stream" : `/api/stream?last_event_id=${encodeURIComponent(this.lastId)}`;
    const source = this.factory(url);
    this.source = source;
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (e) => {
        const message = e as MessageEvent<string>;
        if (message.lastEventId) {
          this.lastId = message.lastEventId;
        }
        this.handlers.onEvent(type, JSON.parse(message.data));
      });
    }
    source.addEventListener("reset", (e) => {
      const id = (e as MessageEvent<string>).lastEventId;
      if (id) {
        this.lastId = id;
      }
      this.handlers.onReset();
    });
    source.onerror = () => {
      if (source.readyState === 2 && !this.stopped) {
        void this.reopen();
      }
    };
  }

  private async reopen(): Promise<void> {
    this.source?.close();
    const ok = await this.handlers.refresh();
    if (this.stopped) {
      return;
    }
    this.retry = setTimeout(() => this.open(), ok ? 0 : this.retryMs);
  }
}
