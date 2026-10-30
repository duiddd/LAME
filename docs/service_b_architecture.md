# Service B: Matching & Market Data — Architecture

**Owner:** Bo Wu · **The one question it answers:** Who trades with whom, at what price?

This document describes the design of Service B. The rationale behind each choice is recorded in [service_b_tech_decisions.md](service_b_tech_decisions.md) (decision IDs such as D1 are referenced below).

## 1. Role and scope

B is the matching core of the exchange and the only publisher of market data. It knows about orders and trades, not accounts, funds or risk.

- **In scope:** price-time matching of limit and market orders and cancels, per trading pair; deterministic event record and crash recovery; market data snapshots; end-of-day cancellation.
- **Out of scope:** balance and risk checks (A, D), news and Q&A (C), authentication, other order types, auctions.
- **Bots and load tests** are part of B's workload but ship as a separate image and are not covered here.

## 2. Interfaces

All messages are JSON. Prices are integers (multiples of the pair's tick) and quantities are integers (multiples of its lot), so there is no floating-point error (D5, D17).

### 2.1 Input: commands from D

B's only upstream is D, over one WebSocket (D13).

| Command | Fields |
| --- | --- |
| `NewOrder` | `order_id`, `symbol`, `user_id`, `side`, `type` (LIMIT / MARKET), `price` (absent for MARKET), `qty`, `origin` (USER / LIQUIDATION), `sent_at` (D's send time, Unix ms) |
| `CancelOrder` | the target as `(user_id, order_id)` **or** the order's seq of the day, plus `symbol`, `origin` |

Contract with D: a `user_id` never reuses an `order_id` within a trading day. B does not check this (D2). A market order's unfilled remainder is cancelled; it never rests.

### 2.2 Output: events to D

Every output record carries `day`, `seq` (its own sequence number) and `prev_seq` (the `seq` of the previous *output* record that day; 0 for the first).

| Record | Key fields |
| --- | --- |
| `OrderEvent` | `order_seq`, `order_id`, `symbol`, `user_id`, `status` (ACCEPTED, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED), filled and remaining qty, `reason` |
| `Trade` | `symbol`, `price`, `qty`, `taker_seq`, `maker_seq`, taker and maker `user_id` |

- An order is identified by `(day, seq)` of its `NewOrder` record; there is no separate trade id. `OrderEvent` always carries `order_seq`, so the sender can cancel by seq (D2, D3).
- `user_id` is passed through untouched (D4).
- B-issued rejections: `EXPIRED` (see 6.3), `ORDER_NOT_FOUND` / `ORDER_ALREADY_FINAL` for a cancel of an unknown or finished order (D7), and invalid commands (unknown symbol, price or qty off the tick/lot grid).
- B guarantees accuracy and low latency of what it emits. Deduplication and use of events by consumers is theirs (D8).

### 2.2.1 Self-trade prevention (D24)

As the atomic backstop for D's self-trade rule, the matcher never matches an incoming order against a resting order of the same `user_id`. It stops there and cancels the incoming order's remainder (`OrderEvent` `CANCELLED`, `reason = SELF_TRADE_PREVENTED`); the resting order is untouched. Fills against other users before that point stand. Linked accounts are D's concern (R2), not B's.

### 2.3 Output: market data to clients

Clients connect to `/md/<symbol>` on port 9999 and receive full snapshots of the top three bid and ask levels plus `last_price` (D6). See section 7.

## 3. Architecture

B is a single process (D19).

```
D ──WebSocket── [connection task: read half | write half]
                      │ command
                      ▼
          [journal thread]  1. stamp ts, assign seq, commit command to SQLite
                      │     2. hand to the engine of that symbol
                      ▼
          [engine task × one per symbol, pure computation]
                      │ OrderEvent / Trade (+ top-3 snapshot)
                      ├──────────────► [lock-free queue] ► [market-data publisher] ► clients
                      ▼
          [journal thread]  3. assign seq, prev_seq, commit output to SQLite
                      │     4. hand to the connection write half
                      ▼
              [connection task, write half] ── WebSocket ── D
```

- **Engine tasks** hold the in-memory order book (price-time priority) and do no I/O at all, so they cannot be stalled by disk or network. They run on tokio; there is no busy-waiting (D18).
- **The journal thread** is a dedicated OS thread because SQLite writes are blocking. It is the only writer of the event store and owns the seq counter. It outlives any WebSocket connection (D19).
- **The connection task** has independent read and write halves, so a slow reader on D's side does not block receiving commands.
- **The market-data publisher** is separate from the journal path (section 7).

Two ordering rules make the design safe:

1. A command is committed to the event store **before** it reaches the engine.
2. An output is committed **before** it is sent to D.

So nothing D sees is missing from the store, and nothing the engine acts on is unrecorded.

## 4. Sequencing and event store

- Each trading day has one SQLite database, `/data/events-<day>.db`, shared by all symbols. It holds the day's commands, the internal `DayClose` record, and all outputs (D9–D11).
- The journal thread assigns `seq` from one counter to every record as it is written. `seq` therefore totals the order of the day across symbols. It restarts at 1 each day, so `(day, seq)` is the global key (D1).
- `ts` is read from the clock when a command enters the journal thread and stored with it. The engine never reads the clock.
- SQLite runs in WAL mode with `synchronous=OFF`. One write connection (journal thread) and separate read-only connections (resend) can work concurrently. A `flock` on `/data/b.lock` guarantees a single writer instance.
- If a commit fails, B stops accepting commands (D9). The event store is written directly with `rusqlite`, not through `tracing`, because the writer must see the error (D20).
- Previous days' databases are kept for audit only.

**Failure model (D12).** B survives process crashes, `docker kill` and container restarts without losing any order it has acknowledged. It does not claim to survive power loss or a killed Docker VM; it does not call fsync.

## 5. Determinism and recovery

The engine is a pure function of the ordered commands and their stored `ts`. It reads no clock and no randomness, and does not assign seqs.

On start-up B replays the day's commands from the event store and compares the recomputed outputs with the stored ones (D10):

- Comparison is by content (status, price, qty, `taker_seq`, `maker_seq`, …). The outputs' own `seq` and `prev_seq` are ignored, because they depend on how commands and outputs interleaved at write time.
- **Mismatch → B refuses to resume and stops with an error.** Continuing to match from a state that disagrees with what was already published would be unfair to participants.
- Commands that have no stored output (crash between commit and output) get their outputs written and sent after replay.
- `/healthz` reports unhealthy during replay; no commands are accepted and no snapshots are published until it completes.
- Recovery needs only the current day's database, because every day starts with an empty book (section 8).

## 6. Interaction with D

### 6.1 Reconnect and resend (D13, D21)

B does not track what D has received. D's first message on every connection is:

```json
{"type": "resume", "from_seq": 12345}
```

meaning "send me today's outputs with `seq` greater than 12345" (`0` = from the start of the day). Only the current day can be resumed.

- B keeps one D connection; a new one replaces the old.
- After the handshake B keeps sending new outputs live **and** concurrently queries the event store for `seq > from_seq` outputs and sends them. The two streams may interleave and overlap; each is ascending by `seq`.
- D is responsible for stitching them together: it deduplicates by `seq` and uses `prev_seq` to check that its chain of outputs has no gaps.
- A `from_seq` beyond the day's last seq is rejected (D is ahead of B, meaning B lost data; a human must look).

### 6.2 Slow consumer (D16, D21)

B never lets D's speed block matching. The outbound queue is bounded (`OUTBOUND_QUEUE_CAP`). When it fills, B closes the connection, keeps matching and journalling, and D resumes by handshake. WebSocket pings (about 10 s timeout) detect half-open connections sooner; correctness never depends on them.

### 6.3 Stale commands (D15)

If `ts − sent_at` exceeds `MAX_COMMAND_AGE`, a `NewOrder` (liquidation orders included) is rejected with `EXPIRED`. This stops an order that D already treated as failed from executing after a delay. `MAX_COMMAND_AGE` must not exceed D's own timeout. It cannot cover an order B accepted in time whose reply was late; D must cancel on timeout. A `CancelOrder` is never expired.

## 7. Market data (D6, D14)

- Snapshots are self-contained, so a lost one costs nothing and clients only keep the newest. There are no deltas, no gap detection and no resynchronisation.
- The sequence number is the Unix-millisecond timestamp taken **when the snapshot is sent**, kept strictly increasing. Clients drop any snapshot not newer than the last they processed.
- At most one snapshot per symbol per millisecond; none if the top three levels and `last_price` are unchanged. If nothing was sent for one second, the current snapshot is resent as a heartbeat. A client that hears nothing for about three seconds should treat B as unavailable.
- The engine pushes snapshots into a bounded single-producer single-consumer lock-free queue and never blocks. If the queue is full the snapshot is dropped and counted. The publisher drains the queue each round and sends only the newest. Each subscriber has its own bounded buffer, so a slow one loses only its own stale snapshots.
- Market data is a derived view, outside the determinism guarantee. Nothing is published during recovery.

## 8. End of day (D22)

When the trading day ends, B itself cancels every resting order, publishing `OrderEvent` `CANCELLED` with reason `END_OF_DAY` to D. This is not a command from D.

So that replay reproduces these cancels, the journal thread first writes a `DayClose` record, and then the engine cancels in order of seq. After the last cancel, B opens a new day's database with `seq` reset to 1 and an empty book.

## 9. Deployment (D23)

One image, built in two stages (pinned Rust toolchain, `Cargo.lock`, SQLite compiled in via `bundled`), running on `distroless/cc` as a non-root user.

| Aspect | Design |
| --- | --- |
| Ports | **9999**: market data, public. **11659**: D channel (`/d`), `/healthz`, `/metrics`, restricted by IP allow-list (`CTRL_ALLOWED_CIDRS`). The allow-list limits source only; it is not authentication |
| Storage | Named volume at `/data` (event databases, lock file). Root filesystem read-only. Never run `docker compose down -v` |
| Config | Mounted file for symbols with tick/lot; env vars `MD_ADDR`, `CTRL_ADDR`, `CTRL_ALLOWED_CIDRS`, `EVENT_DB_DIR`, `MAX_COMMAND_AGE`, `OUTBOUND_QUEUE_CAP`, `RUST_LOG` |
| Logging | Run-time logs only, via `tracing` to stdout as JSON; Docker `json-file` with size limits. The event store is separate and never goes through `tracing` |
| Lifecycle | `restart: unless-stopped`; `SIGTERM` is handled for a tidy exit, but correctness does not depend on it; `--healthcheck` subcommand; healthy only after replay; D waits on `service_healthy` |
| Resources | No CPU limit; generous memory limit |
| Metrics | Matching latency, outbound backlog, dropped snapshots, replay duration |

**To verify when running:** (1) the source IP B sees through published ports may be rewritten by Docker, which affects the allow-list (inside one compose network it sees container IPs); (2) the read-only root plus non-root user plus named-volume permission combination. The first test to pass is: start → `docker kill` → automatic restart → consistent state.

## 10. How the design meets the acceptance criteria

| Criterion | Mechanism |
| --- | --- |
| Replaying the same orders twice gives identical trades | The engine is a pure function of stored commands and `ts`; trades compared by content (section 5) |
| After a forced kill and restart the book matches and no trade is lost or duplicated | Commit-before-match, commit-before-send, replay with verification, and missing outputs completed after replay |
| A slow or disconnected client affects neither matching nor other clients | The engine does no I/O; all queues are bounded; per-subscriber buffers; D is disconnected rather than awaited |
| Under bot load, labelled manipulation data for D's evaluation | Separate bots image, not yet specified |

## 11. Needed from other services

- **D:** never repeat an `order_id` for a user within a day; send `sent_at`; cancel on timeout; implement the resume handshake, deduplicate by `seq` and verify `prev_seq`; forward B's `OrderEvent` / `Trade` to A; subscribe to market data on 9999 for liquidation prices; its network must be in `CTRL_ALLOWED_CIDRS`.
- **A:** accept that settlement depends on D forwarding B's events; key the ledger on `(day, seq)`; release holds on `END_OF_DAY` cancels; convert to and from tick/lot; the frontend uses `/md/<symbol>` as in section 7; B's service snippet goes into A's compose file.
- **C:** price information is limited to top-3 snapshots plus `last_price` (no trade history or candles); Prometheus scrapes `/metrics` on 11659.
- **Proposal text to update:** B talks only to D, with A receiving events via D and C using snapshots; market data is full snapshots ordered by timestamp, not sequence numbers with gap detection; no candles or trade history in market data.

## 12. Open items

1. **Trading day.** Who defines it and its format; how B learns that it ended (if B decides, B owns the day clock, which the whole system must agree on); whether B emits a `DayEnd` record after the cancels so D can run its overnight check; how commands arriving during the end-of-day cancels are handled.
2. **Timeouts.** Concrete values of `MAX_COMMAND_AGE` and D's timeout, and D's cancel-on-timeout behaviour, including D receiving fills for orders it already treated as failed.
3. **Bots and load tests.** Design not yet written.
