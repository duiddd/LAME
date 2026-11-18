/** Where the browser reaches B and C directly; A is always same-origin under /api. */
const host = typeof location === "undefined" ? "localhost" : location.hostname;

export const B_MD_URL: string = import.meta.env.VITE_B_MD_URL ?? `ws://${host}:9999`;
export const C_URL: string = import.meta.env.VITE_C_URL ?? `http://${host}:8080`;

/** One minute before the 15-minute access token expires. */
export const TOKEN_REFRESH_MS = 14 * 60 * 1000;
export const STALE_AFTER_MS = 3000;
export const VIEW_UPDATE_MS = 100;
export const NEWS_REFRESH_MS = 15000;
export const CANDLE_POLL_MS = 1000;
