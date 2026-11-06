"""SQLite storage. D runs in one asyncio loop, so this one connection is the only writer.

What is stored (architecture doc section 5): A's orders, decisions, the id map to
B's integer order ids, cancel intents and tombstones, the notification outbox and
two cursors, and each order's latest modify. B's own outputs are not stored: B's event store is the source of truth.
"""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL, type TEXT NOT NULL, price INTEGER, qty INTEGER NOT NULL,
    state TEXT NOT NULL, reason TEXT, filled_qty INTEGER NOT NULL DEFAULT 0,
    remaining_qty INTEGER NOT NULL DEFAULT 0, received_at INTEGER NOT NULL,
    last_notify_seq INTEGER
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_no INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL,
    decision TEXT NOT NULL, reason TEXT, rule_version TEXT NOT NULL,
    features_day TEXT, features_seq INTEGER, decided_at INTEGER NOT NULL,
    price INTEGER, qty INTEGER, b_order_id INTEGER   -- what was decided; set for every decision
);
-- AUTOINCREMENT: b_order_id is never reused, so it never repeats for a user within a day (B D2).
CREATE TABLE IF NOT EXISTS b_orders (
    b_order_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL,
    user_id INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    type TEXT NOT NULL, price INTEGER, qty INTEGER NOT NULL, sent_at INTEGER NOT NULL,
    state TEXT NOT NULL, filled INTEGER NOT NULL DEFAULT 0,
    b_day TEXT, order_seq INTEGER, seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cancels (
    order_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, origin TEXT NOT NULL, action TEXT,
    tombstone INTEGER NOT NULL DEFAULT 0, b_order_id INTEGER,
    send_not_before INTEGER, sent_at INTEGER, result TEXT, received_at INTEGER NOT NULL
);
-- One row per order: its latest modify (RD27). state: CANCELLING (old order being
-- cancelled at B), SENT (replacement sent), DONE (MODIFY_RESULT written).
CREATE TABLE IF NOT EXISTS modifies (
    order_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, price INTEGER, qty INTEGER NOT NULL,
    state TEXT NOT NULL, old_b_order_id INTEGER, filled_at_request INTEGER,
    new_b_order_id INTEGER, new_price INTEGER, new_qty INTEGER,
    result TEXT, reason TEXT, received_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    notify_seq INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT,
    type TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, data_dir: str):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(data_dir) / "d.db", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for col in ("price INTEGER", "qty INTEGER", "b_order_id INTEGER"):   # databases made before 11-06
            try:
                self.db.execute(f"ALTER TABLE decisions ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self.write_ok = True

    # -- transactions ---------------------------------------------------
    def tx(self):
        return _Tx(self)

    def q(self, sql: str, *args):
        return self.db.execute(sql, args)

    def one(self, sql: str, *args):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, *args):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # -- meta -----------------------------------------------------------
    def get_meta(self, key: str, default=None):
        row = self.one("SELECT value FROM meta WHERE key = ?", key)
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value):
        self.q("INSERT INTO meta(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, json.dumps(value))

    # -- outbox ---------------------------------------------------------
    def add_notification(self, ntype: str, order_id, payload: dict, now: int) -> int:
        cur = self.q("INSERT INTO outbox(order_id, type, payload, created_at) VALUES(?, ?, ?, ?)",
                     order_id, ntype, "{}", now)
        seq = cur.lastrowid
        body = {"notify_seq": seq, "type": ntype, **payload}
        self.q("UPDATE outbox SET payload = ? WHERE notify_seq = ?", json.dumps(body), seq)
        if order_id:
            self.q("UPDATE orders SET last_notify_seq = ? WHERE order_id = ?", seq, order_id)
        return seq


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT; a failed commit marks the store unhealthy (fail closed)."""

    def __init__(self, store: Store):
        self.store = store

    def __enter__(self):
        self.store.db.execute("BEGIN IMMEDIATE")
        return self.store

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.store.db.execute("ROLLBACK")
            return False
        try:
            self.store.db.execute("COMMIT")
            self.store.write_ok = True
        except sqlite3.Error:
            self.store.write_ok = False
            raise
        return False
