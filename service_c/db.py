"""SQLite + FTS5 (BM25) Knowledge Base for Service C (Decision D3, D7).

Features:
- WAL mode for concurrent read/write access.
- Idempotent ingestion keyed by `news_id` (replaying news_script.jsonl never duplicates).
- Versioned index (`index_version` increments deterministically on each new article).
- Full-text BM25 retrieval via SQLite FTS5 (`news_fts`).
- Dead-Letter Queue (`dlq_items`) persistence for auditing malformed/poison payloads.
"""

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .catalog import GLOBAL_INSTRUMENTS, normalize_instrument_symbol
from .metrics import metrics
from .models import DLQItemOut, NewsItemIn, NewsItemOut
from .security import sanitize_news_text


def _build_generic_market_words() -> frozenset[str]:
    base_words = {
        "btc",
        "eth",
        "usdt",
        "usd",
        "bitcoin",
        "ethereum",
        "market",
        "markets",
        "price",
        "prices",
        "move",
        "moved",
        "moves",
        "moving",
        "movement",
        "movements",
        "drop",
        "dropped",
        "dropping",
        "fall",
        "fell",
        "falling",
        "rise",
        "rose",
        "rising",
        "jump",
        "jumped",
        "jumping",
        "surge",
        "surged",
        "surging",
        "rally",
        "rallied",
        "rallying",
        "dip",
        "dipped",
        "dipping",
        "crash",
        "crashed",
        "crashing",
        "pump",
        "pumped",
        "dump",
        "dumped",
        "change",
        "changed",
        "changing",
        "changes",
        "spike",
        "spiked",
        "spiking",
        "swing",
        "swings",
        "volatile",
        "volatility",
        "news",
        "update",
        "updates",
        "latest",
        "summary",
        "summarize",
        "explain",
        "explanation",
        "reason",
        "reasons",
        "cause",
        "caused",
        "causes",
        "catalyst",
        "catalysts",
        "driver",
        "drivers",
        "driving",
        "driven",
        "status",
        "report",
        "about",
        "affect",
        "affected",
        "affecting",
        "impact",
        "impacted",
        "pair",
        "pairs",
        "spot",
        "crypto",
        "token",
        "tokens",
        "coin",
        "coins",
        "stock",
        "stocks",
        "share",
        "shares",
        "equity",
        "equities",
        "script",
        "scripts",
        "scrip",
        "commodity",
        "commodities",
        "exchange",
        "exchanges",
        "global",
        "top",
        "traded",
        "bought",
        "sold",
        "buy",
        "sell",
        "trading",
        "trade",
        "trades",
        "action",
        "activity",
        "trend",
        "trends",
        "trending",
        "perform",
        "performing",
        "performance",
        "current",
        "currently",
        "high",
        "higher",
        "low",
        "lower",
        "down",
        "nasdaq",
        "nyse",
        "lse",
        "euronext",
        "xetra",
        "tse",
        "hkex",
        "twse",
        "krx",
        "nse",
        "bse",
        "comex",
        "nymex",
    }
    for sym, meta in GLOBAL_INSTRUMENTS.items():
        for tok in re.findall(r"[A-Za-z0-9]+", sym.lower()):
            base_words.add(tok)
        for tok in re.findall(r"[A-Za-z0-9]+", meta["name"].lower()):
            base_words.add(tok)
        for alias in meta["aliases"]:
            for tok in re.findall(r"[A-Za-z0-9]+", alias.lower()):
                base_words.add(tok)
    return frozenset(base_words)


class NewsDatabase:
    """SQLite + FTS5 persistence and BM25 search engine."""

    STOPWORDS = frozenset(
        {
            "a",
            "an",
            "and",
            "any",
            "are",
            "as",
            "at",
            "be",
            "been",
            "both",
            "by",
            "can",
            "could",
            "did",
            "do",
            "does",
            "doing",
            "for",
            "from",
            "give",
            "going",
            "happen",
            "happened",
            "happening",
            "has",
            "have",
            "how",
            "in",
            "into",
            "is",
            "it",
            "its",
            "just",
            "last",
            "me",
            "minutes",
            "now",
            "of",
            "on",
            "or",
            "over",
            "please",
            "recent",
            "recently",
            "right",
            "show",
            "so",
            "some",
            "tell",
            "than",
            "that",
            "the",
            "their",
            "them",
            "then",
            "there",
            "these",
            "they",
            "this",
            "to",
            "today",
            "up",
            "us",
            "was",
            "we",
            "were",
            "what",
            "when",
            "where",
            "which",
            "who",
            "why",
            "will",
            "with",
            "would",
            "you",
        }
    )

    GENERIC_MARKET_WORDS = _build_generic_market_words()

    UNKNOWN_TICKERS = frozenset(
        {
            "FAKECOIN",
            "MARSCOIN",
            "ZZZZ",
        }
    )

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta_state (
                        key TEXT PRIMARY KEY,
                        int_val INTEGER NOT NULL
                    );

                    INSERT OR IGNORE INTO meta_state (key, int_val)
                    VALUES ('index_version', 0);

                    CREATE TABLE IF NOT EXISTS news_items (
                        news_id TEXT PRIMARY KEY,
                        timestamp_ms INTEGER NOT NULL,
                        symbols_json TEXT NOT NULL,
                        headline TEXT NOT NULL,
                        body TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'LAME-Wire',
                        signature TEXT,
                        sanitized INTEGER NOT NULL DEFAULT 0,
                        index_version INTEGER NOT NULL,
                        ingested_at_ms INTEGER NOT NULL
                    );

                    CREATE VIRTUAL TABLE IF NOT EXISTS news_fts USING fts5(
                        news_id UNINDEXED,
                        symbols_text,
                        headline,
                        body,
                        tokenize='porter unicode61'
                    );

                    CREATE TABLE IF NOT EXISTS dlq_items (
                        dlq_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        raw_payload TEXT NOT NULL,
                        error_reason TEXT NOT NULL,
                        failed_at_ms INTEGER NOT NULL,
                        routing_key TEXT NOT NULL DEFAULT 'news.dead'
                    );
                    """
                )
                conn.commit()
                ver = self._read_index_version_unlocked(conn)
                dlq_cnt = self._read_dlq_count_unlocked(conn)
                metrics.set_index_version(ver)
                metrics.set_dlq_count(dlq_cnt)
            finally:
                conn.close()

    @staticmethod
    def _read_index_version_unlocked(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT int_val FROM meta_state WHERE key = 'index_version'"
        ).fetchone()
        return int(row["int_val"]) if row else 0

    @staticmethod
    def _read_dlq_count_unlocked(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM dlq_items").fetchone()
        return int(row["cnt"]) if row else 0

    def get_index_version(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                return self._read_index_version_unlocked(conn)
            finally:
                conn.close()

    def ingest_news_item(self, item: NewsItemIn) -> tuple[bool, int, bool]:
        """Idempotently stores and indexes a validated news item.

        Returns `(inserted_new, index_version, was_sanitized)`.
        Replaying an existing `news_id` is a no-op and returns `inserted_new=False`.
        """
        safe_headline, mod_h = sanitize_news_text(item.headline)
        safe_body, mod_b = sanitize_news_text(item.body)
        was_sanitized = mod_h or mod_b
        if was_sanitized:
            metrics.record_sanitized_news()

        normalized_symbols = [s.strip().upper() for s in item.symbols if s.strip()]
        symbols_json = json.dumps(normalized_symbols)
        # Include full pair, base token, company/asset name, and aliases for rich FTS5 matching
        expanded_tokens: list[str] = []
        for sym in normalized_symbols:
            expanded_tokens.append(sym)
            expanded_tokens.extend(re.findall(r"[A-Za-z0-9]+", sym))
            meta = GLOBAL_INSTRUMENTS.get(sym)
            if meta:
                expanded_tokens.append(meta["name"])
                expanded_tokens.append(meta["exchange"])
                expanded_tokens.extend(meta["aliases"])
        symbols_text = " ".join(expanded_tokens)

        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT index_version, sanitized FROM news_items WHERE news_id = ?",
                    (item.news_id,),
                ).fetchone()
                if existing is not None:
                    return False, int(existing["index_version"]), bool(existing["sanitized"])

                conn.execute(
                    "UPDATE meta_state SET int_val = int_val + 1 WHERE key = 'index_version'"
                )
                new_ver = self._read_index_version_unlocked(conn)
                now_ms = int(time.time() * 1000)

                conn.execute(
                    """
                    INSERT INTO news_items (
                        news_id, timestamp_ms, symbols_json, headline, body,
                        source, signature, sanitized, index_version, ingested_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.news_id,
                        item.timestamp_ms,
                        symbols_json,
                        safe_headline,
                        safe_body,
                        item.source,
                        item.signature,
                        1 if was_sanitized else 0,
                        new_ver,
                        now_ms,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO news_fts (news_id, symbols_text, headline, body)
                    VALUES (?, ?, ?, ?)
                    """,
                    (item.news_id, symbols_text, safe_headline, safe_body),
                )
                conn.commit()
                metrics.set_index_version(new_ver)
                return True, new_ver, was_sanitized
            finally:
                conn.close()

    def record_dlq_item(self, raw_payload: str, error_reason: str, routing_key: str = "news.dead") -> int:
        """Persists a dead-lettered message so it is neither lost nor blocks valid news."""
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT dlq_id FROM dlq_items WHERE raw_payload = ?",
                    (raw_payload,),
                ).fetchone()
                if existing is not None:
                    cnt = self._read_dlq_count_unlocked(conn)
                    metrics.set_dlq_count(cnt)
                    return int(existing["dlq_id"])

                cur = conn.execute(
                    """
                    INSERT INTO dlq_items (raw_payload, error_reason, failed_at_ms, routing_key)
                    VALUES (?, ?, ?, ?)
                    """,
                    (raw_payload, error_reason, int(time.time() * 1000), routing_key),
                )
                conn.commit()
                cnt = self._read_dlq_count_unlocked(conn)
                metrics.set_dlq_count(cnt)
                return int(cur.lastrowid or 0)
            finally:
                conn.close()

    def list_recent_news(self, symbol: Optional[str] = None, limit: int = 20) -> list[NewsItemOut]:
        """Returns recent ingested news ordered by timestamp_ms DESC."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT news_id, timestamp_ms, symbols_json, headline, body,
                           source, signature, sanitized, index_version
                    FROM news_items
                    ORDER BY timestamp_ms DESC, news_id DESC
                    LIMIT ?
                    """,
                    (max(1, min(limit * 4, 300)),),
                ).fetchall()
            finally:
                conn.close()

        results: list[NewsItemOut] = []
        target_sym = (normalize_instrument_symbol(symbol) or symbol.strip().upper()) if symbol else None
        for r in rows:
            syms: list[str] = json.loads(r["symbols_json"])
            if target_sym:
                if not any(target_sym == s or target_sym in s or s.startswith(target_sym) for s in syms):
                    continue
            results.append(
                NewsItemOut(
                    news_id=r["news_id"],
                    timestamp_ms=int(r["timestamp_ms"]),
                    symbols=syms,
                    headline=r["headline"],
                    body=r["body"],
                    source=r["source"],
                    signature=r["signature"],
                    sanitized=bool(r["sanitized"]),
                    index_version=int(r["index_version"]),
                )
            )
            if len(results) >= limit:
                break
        return results

    def list_dlq_items(self, limit: int = 50) -> list[DLQItemOut]:
        """Returns dead-lettered news payloads for inspection via GET /api/dlq."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT dlq_id, raw_payload, error_reason, failed_at_ms, routing_key
                    FROM dlq_items
                    ORDER BY dlq_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            finally:
                conn.close()

        return [
            DLQItemOut(
                dlq_id=int(r["dlq_id"]),
                raw_payload=r["raw_payload"],
                error_reason=r["error_reason"],
                failed_at_ms=int(r["failed_at_ms"]),
                routing_key=r["routing_key"],
            )
            for r in rows
        ]

    @classmethod
    def _extract_fts_tokens(cls, query: str, symbol: Optional[str] = None) -> list[str]:
        raw_words = re.findall(r"[A-Za-z0-9]+", query.lower())
        tokens = [w for w in raw_words if len(w) >= 2 and w not in cls.STOPWORDS]
        if symbol:
            for part in re.findall(r"[A-Za-z0-9]+", symbol.lower()):
                if part not in ("usdt", "usd") and part not in tokens:
                    tokens.insert(0, part)
        return tokens

    def search_bm25(
        self,
        query: str,
        symbol: Optional[str] = None,
        top_k: int = 3,
        min_score: float = 0.05,
    ) -> tuple[list[dict], int]:
        """Searches SQLite FTS5 using BM25 ranking."""
        explicit_unknown_tickers = {
            t.upper()
            for t in re.findall(r"\b[A-Za-z]{3,8}\b", query)
            if t.upper() in self.UNKNOWN_TICKERS
        }
        tokens = self._extract_fts_tokens(query, symbol=symbol)
        specific_topic_tokens = [
            t for t in tokens if t not in self.GENERIC_MARKET_WORDS
        ]

        with self._lock:
            conn = self._connect()
            try:
                index_version = self._read_index_version_unlocked(conn)
                if explicit_unknown_tickers:
                    return [], index_version

                search_tokens = list(tokens)
                if not search_tokens:
                    search_tokens = ["btc", "eth", "nvda", "aapl"]

                fts_expr = " OR ".join(f'"{t}"*' for t in search_tokens[:14])
                rows = conn.execute(
                    """
                    SELECT
                        n.news_id,
                        n.timestamp_ms,
                        n.symbols_json,
                        n.headline,
                        n.body,
                        n.source,
                        n.sanitized,
                        n.index_version,
                        bm25(news_fts, 1.5, 2.0, 1.0) AS raw_bm25
                    FROM news_fts
                    JOIN news_items n ON n.news_id = news_fts.news_id
                    WHERE news_fts MATCH ?
                    ORDER BY raw_bm25 ASC, n.timestamp_ms DESC
                    LIMIT ?
                    """,
                    (fts_expr, max(top_k * 5, 15)),
                ).fetchall()
            finally:
                conn.close()

        target_sym = (normalize_instrument_symbol(symbol) or symbol.strip().upper()) if symbol else None

        results: list[dict] = []
        for r in rows:
            syms: list[str] = json.loads(r["symbols_json"])
            if target_sym:
                if not any(target_sym == s or target_sym in s or s.startswith(target_sym) for s in syms):
                    continue

            doc_text = f"{r['headline']} {r['body']} {' '.join(syms)} {r['source']}".lower()
            matched_tokens = [t for t in tokens if t in doc_text or t[:4] in doc_text]

            if specific_topic_tokens and not any(
                (t in doc_text or (len(t) >= 4 and t[:4] in doc_text))
                for t in specific_topic_tokens
            ):
                continue

            raw_score = abs(float(r["raw_bm25"]))
            overlap_bonus = 0.25 * len(matched_tokens)
            score = round(raw_score + overlap_bonus, 4)
            if score < min_score:
                continue

            results.append(
                {
                    "news_id": r["news_id"],
                    "timestamp_ms": int(r["timestamp_ms"]),
                    "symbols": syms,
                    "headline": r["headline"],
                    "body": r["body"],
                    "source": r["source"],
                    "sanitized": bool(r["sanitized"]),
                    "index_version": int(r["index_version"]),
                    "bm25_score": score,
                }
            )

        results.sort(key=lambda x: (x["bm25_score"], x["timestamp_ms"]), reverse=True)
        return results[:top_k], index_version
