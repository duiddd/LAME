const cache = new Map<number, Intl.NumberFormat>();

function formatter(decimals: number): Intl.NumberFormat {
  let f = cache.get(decimals);
  if (!f) {
    f = new Intl.NumberFormat("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    cache.set(decimals, f);
  }
  return f;
}

/** Formats B's integer units for display. */
export function formatUnits(units: number, decimals: number): string {
  return formatter(decimals).format(units / 10 ** decimals);
}

/** Groups thousands in a decimal string from A without going through a lossy float. */
export function groupDecimal(text: string): string {
  const [whole, fraction] = text.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
}

export function formatTime(ms: number): string {
  return new Date(ms).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** B's trading session in Dublin time (B's `session.rs`); B changed it to 00:00-23:00 on 11-06. */
export const SESSION_OPEN = "00:00";
export const SESSION_CLOSE = "23:00";

const toMinutes = (hhmm: string) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5));

export function isMarketOpen(now: Date = new Date()): boolean {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Dublin",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(now);
  const hour = Number(parts.find((p) => p.type === "hour")?.value);
  const minute = Number(parts.find((p) => p.type === "minute")?.value);
  const minutes = hour * 60 + minute;
  return minutes >= toMinutes(SESSION_OPEN) && minutes < toMinutes(SESSION_CLOSE);
}
