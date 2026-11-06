import { useSyncExternalStore } from "react";
import { B_MD_URL } from "../../shared/config";
import { type FeedView, MarketFeed, type SocketFactory } from "./feed";

const feeds = new Map<string, MarketFeed>();
let socketFactory: SocketFactory = (url) => new WebSocket(url);

/** Mock mode replaces B's WebSocket before the app renders. */
export function setSocketFactory(factory: SocketFactory): void {
  socketFactory = factory;
}

function feedFor(symbol: string): MarketFeed {
  let feed = feeds.get(symbol);
  if (!feed) {
    feed = new MarketFeed(`${B_MD_URL}/md/${symbol}`, socketFactory);
    feeds.set(symbol, feed);
  }
  return feed;
}

export function useFeed(symbol: string): FeedView {
  const feed = feedFor(symbol);
  return useSyncExternalStore(feed.subscribe, feed.getView);
}
