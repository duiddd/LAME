"""Orders, settled trades and the notification cursor."""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE orders (
        order_id TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users,
        client_order_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        type TEXT NOT NULL,
        wire_type TEXT NOT NULL,
        price BIGINT,
        hold_price BIGINT NOT NULL,
        qty BIGINT NOT NULL,
        filled_qty BIGINT NOT NULL DEFAULT 0,
        settled_qty BIGINT NOT NULL DEFAULT 0,
        spent BIGINT NOT NULL DEFAULT 0,
        held_amount BIGINT NOT NULL CHECK (held_amount >= 0),
        status TEXT NOT NULL,
        reason TEXT,
        b_day TEXT,
        b_order_seq BIGINT,
        modify_no INT NOT NULL DEFAULT 0,
        cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
        cancel_wanted BOOLEAN NOT NULL DEFAULT FALSE,
        cancel_acked BOOLEAN NOT NULL DEFAULT FALSE,
        attempts INT NOT NULL DEFAULT 0,
        next_attempt_at BIGINT NOT NULL DEFAULT 0,
        queries INT NOT NULL DEFAULT 0,
        next_query_at BIGINT,
        closed_at BIGINT,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        UNIQUE (user_id, client_order_id)
    )""")
    op.execute("CREATE INDEX orders_user ON orders (user_id, created_at DESC)")
    op.execute("CREATE INDEX orders_open ON orders (status) WHERE closed_at IS NULL")
    op.execute("""
    CREATE TABLE trades (
        b_day TEXT NOT NULL,
        b_seq BIGINT NOT NULL,
        symbol TEXT NOT NULL,
        price BIGINT NOT NULL,
        qty BIGINT NOT NULL,
        taker_order_id TEXT NOT NULL,
        maker_order_id TEXT NOT NULL,
        ts BIGINT NOT NULL,
        PRIMARY KEY (b_day, b_seq)
    )""")
    op.execute("CREATE INDEX trades_symbol_ts ON trades (symbol, ts)")
    op.execute("CREATE INDEX trades_taker ON trades (taker_order_id)")
    op.execute("CREATE INDEX trades_maker ON trades (maker_order_id)")
    op.execute("""
    CREATE TABLE notify_cursor (
        id INT PRIMARY KEY CHECK (id = 1),
        last_notify_seq BIGINT NOT NULL
    )""")
    op.execute("INSERT INTO notify_cursor (id, last_notify_seq) VALUES (1, 0)")
