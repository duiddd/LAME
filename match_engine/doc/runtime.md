# Runtime

How the matching core is wrapped into a running service: components, the
channels between them, the wire protocol, the event store, recovery, market
data, the control port and shutdown. System-level decisions are in
`../../docs/service_b_tech_decisions.md`; this file describes what is built.

## Components

```
D ──WebSocket /d──▶ [connection task]
                      read half │ commands          ▲ outputs (bounded queue)
                                ▼                   │
                       ┌── [journal thread] ────────┘
                       │   stamps ts, assigns seq, commits to SQLite
                       ▼
        [engine task × one per pair]  ── snapshot (watch) ──▶ [market-data task] ──WebSocket /md/<pair>──▶ clients
                       │
                       └─ outputs ──▶ back into the journal thread

[end-of-day timer] ─ CloseCheck ─▶ journal        [HTTP: /healthz /metrics, allow-list]
[start-up recovery] runs once, before anything above accepts work
```

| Component | Runs as | Module |
| --- | --- | --- |
| Connection task | tokio task per D connection, read and write halves independent | `conn` |
| Journal | one dedicated OS thread (SQLite writes block) | `journal`, `store` |
| Engine task | one tokio task per pair, in-memory computation only | `engine_task` |
| Market data | an accept loop plus one task per subscriber | `md` |
| Control port | axum router with an IP allow-list | `http` |
| End-of-day timer | one tokio task | `eod`, `session` |
| Start-up and recovery | blocking, once, before serving | `recovery` |
| Shutdown | `SIGTERM` / `SIGINT` handling | `main`, `service` |

`service::start` assembles all of it; `main` adds logging and signals.

## Channels and backpressure

- Into the journal and into each engine: tokio **unbounded** channels. No
  component can block another, so there is no cycle in which two components
  wait for each other. The journal backlog is exposed as a metric.
- Engine to market data: a `tokio::sync::watch` cell per pair holding the
  newest `Snapshot`. The engine never waits; a reader that falls behind simply
  sees only the newest value.
- Journal to the D connection: a **bounded live queue** (`OUTBOUND_QUEUE_CAP`).
  The journal uses `try_send`; when the queue is full it closes that connection
  instead of waiting. D reconnects and resumes from its own `from_seq`.
- Resend to the D connection: its **own bounded queue** of the same size, fed
  from the store by a blocking task. When it is full the resend waits. It never
  shares a queue with live outputs, so a long catch-up cannot make a live output
  find a full queue and close the connection.
- The engine tasks do no I/O. After 128 commands in a row an engine task
  yields to the scheduler.

## Ordering guarantees

1. A command is committed to the store before an engine sees it.
2. An output is committed before it is sent to D.

The journal handles messages in batches: one transaction per batch, and the
deliveries after the commit. If a commit fails, the journal thread ends with an
error, `/healthz` turns unhealthy and the process exits; B does not carry on
without a record.

## The D channel

One WebSocket at `/d` on the control port. One connection at a time: a new one
closes the old.

**Handshake.** The first message must be
`{"type":"resume","from_seq":N}`: send every output of today with `seq > N`
(0 = from the start of the day). It is refused with an `error` if it is missing
(`expected_resume`) or if `N` is beyond the last record of the day
(`resume_ahead_of_store`: B has lost data and a person must look).

After the handshake B registers for live outputs first, and only then reads the
store for the resend. So nothing is missed, but the two can **overlap**; D
deduplicates by `(day, seq)` (`seq` restarts every day). Each of the two is in
ascending order, but records from the two may interleave, so a live record can
arrive before an older one that is still being resent.

**When D was away and B was not.** B kept writing outputs to the store the
whole time. D connects and resumes from the last `seq` it has; the resend
reads the store from there, concurrently with the journal's writes, while the
connection's read half is already taking new orders and cancels, which go
through the journal and the engines as usual and come back as live records.
Nothing about the catch-up delays them.

**From D**

```json
{"type":"new_order","symbol":"AAA","order_id":7,"user_id":100,"side":"BUY",
 "order_type":"LIMIT","price":50,"qty":5,"origin":"USER","sent_at":1793786400000}
{"type":"new_order","symbol":"AAA","order_id":8,"user_id":100,"side":"SELL",
 "order_type":"MARKET","qty":5,"sent_at":1793786400000}
{"type":"cancel_order","symbol":"AAA","user_id":100,"order_id":7}
{"type":"cancel_order","symbol":"AAA","order_seq":42}
```

`side` is `BUY`/`SELL`, `order_type` is `LIMIT`/`MARKET`, `origin` is optional
(`USER` by default; `LIQUIDATION` is accepted but unused). A limit order needs
`price`. A cancel names either `order_seq` alone or `user_id` with `order_id`.
`type` is the message kind and `order_type` the order kind, so the two do not
clash.

**To D**: output records exactly as stored.

```json
{"type":"order_event","day":"2026-11-04","seq":4,"prev_seq":2,"symbol":"AAA",
 "order_seq":3,"order_id":8,"user_id":200,"status":"ACCEPTED","filled":0,
 "remaining":3,"reason":null}
{"type":"trade","day":"2026-11-04","seq":5,"prev_seq":4,"symbol":"AAA",
 "price":50,"qty":3,"taker_seq":3,"maker_seq":1,"taker_user_id":200,"maker_user_id":100}
```

`prev_seq` is the `seq` of the previous *output* (0 for the first of the day),
so D can check its chain of outputs has no gap. Statuses are `ACCEPTED`,
`PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`. Reasons are
`ORDER_NOT_FOUND`, `SELF_TRADE_PREVENTED`, `EXPIRED`, `END_OF_DAY`,
`INVALID_PRICE`, `INVALID_QTY`.

**Errors.** `{"type":"error","code":...,"message":...}` for a frame that could
not be turned into a command: `bad_message` (not JSON, a limit order without a
price, an ambiguous cancel, a second `resume`) and `unknown_symbol`. These are
not records: they have no `seq` and are not journaled, because they never
became commands. The connection stays open.

Keep-alive: B pings every 5 s and closes a connection that has been silent for
15 s.

## The event store

One SQLite database per trading day, `events-<day>.db` in `EVENT_DB_DIR`,
shared by all pairs, with a lock file `b.lock` held for the life of the process
so a second instance refuses to start.

```
events(seq INTEGER PRIMARY KEY, kind TEXT, symbol TEXT, ts INTEGER, prev_seq INTEGER, payload TEXT)
```

`kind` is `NewOrder`, `CancelOrder`, `DayClose`, `OrderEvent` or `Trade`;
`payload` is JSON. For commands it is the frame as received. WAL mode,
`synchronous=OFF`: a process crash or `docker kill` loses nothing that was
acknowledged; a power cut is not covered. Resend uses separate read-only
connections.

The `day` is the Dublin local date at the record's timestamp, so it names both
the file and the trading day.

## The journal

The only writer of the store and the only reader of the clock for commands.

- A command gets `ts` from the clock when the journal picks it up, then the
  next `seq`, is committed, and is then handed to its engine.
- Outputs from an engine get the next `seq` and the `prev_seq` of the previous
  output, are committed, and are then queued for D.
- **Day close.** When a timestamp is at or after 23:00 Dublin time and the day
  has not been closed, `DayClose` is written first and every engine is told to
  close. Each engine answers with its outputs and then a done message.
- **Day rollover.** When a timestamp falls on a later day, the journal opens
  that day's store with `seq` restarting at 1. If the old day was never closed,
  it is closed first, and commands that arrive meanwhile wait until all engines
  have reported, so yesterday's cancels are written into yesterday's store.
- **Shutdown.** See below.

## Recovery

Before any port accepts work, `recovery::recover` replays today's store:

1. Every command is replayed in `seq` order with its stored `seq` and `ts`
   (never the current clock), and `DayClose` closes every engine.
2. The outputs the engines produce are compared, per pair and in order, with
   the stored outputs, ignoring those outputs' own `seq` and `prev_seq`. The
   chain of `prev_seq` values is also checked.
3. Outcomes:
   - all match: the engines hold exactly the state they had, and numbering
     continues from the last stored record;
   - the store has commands whose outputs were never written (a crash between
     the two): those outputs are written now, with the numbering they would
     have had, and D collects them with its resume handshake;
   - a stored output differs, has no command behind it, the chain is broken, or
     the store names a pair that is no longer configured: **B refuses to start**
     and says why, so that it never matches from a state that disagrees with
     what was already published.

While this runs the control port answers `/healthz` with 503 and refuses `/d`;
the market-data port refuses subscribers. Afterwards the journal is asked to
check the close once, so a start after 23:00 closes the day before any order.

## Market data

`/md/<pair>` on `MD_ADDR`, public. A new subscriber is sent the current view at
once. After that, per subscriber:

- at most one message per millisecond; of several snapshots in that time only
  the newest is sent;
- a snapshot whose view is the same as the last one sent (ignoring `last_ms`)
  is skipped;
- after one second without sending, the **last message is sent again,
  unchanged** (same `last_ms`). Clients should keep a message whose `last_ms` is
  not older than the newest they hold and treat an equal one as a heartbeat.

```json
{"bids":[{"price":9,"qty":4}],"asks":[{"price":10,"qty":3}],
 "last_price":10,"volume":2,"last_ms":1793786400001}
```

A subscriber that does not read cannot slow the engine or the others.

## Control port

`CTRL_ADDR` serves `/d`, `/healthz` and `/metrics` behind an allow-list
(`CTRL_ALLOWED_CIDRS`; it limits the source, it is not authentication).

- `/healthz`: 200 when recovery has finished, the journal is writing and no
  stop has begun; otherwise 503.
- `/metrics`: Prometheus text. `lame_b_commands_total{symbol}`,
  `lame_b_bad_frames_total`, `lame_b_outputs_total{kind}`,
  `lame_b_journal_backlog`, `lame_b_outbound_backlog`,
  `lame_b_outbound_overflows_total`, `lame_b_replay_duration_seconds`,
  `lame_b_replay_records`, `lame_b_md_subscribers`, `lame_b_journal_failed`.
  There is no matching-latency histogram: nothing on the matching path takes
  timings.
- `match_engine --healthcheck` asks `127.0.0.1:<CTRL_ADDR port>/healthz` and
  exits 0 or 1.

## End-of-day timer

Sleeps until the next 23:00 Dublin time (`session::next_close`) and sends the
journal a `CloseCheck`. It decides nothing: the journal checks its own
timestamp. If the timer wakes marginally early the journal does nothing and the
timer sleeps again.

## Shutdown

`SIGTERM` or `SIGINT`: no new connections or commands are accepted, the engines
finish what is queued, the journal writes everything they produced, and the
process exits 0. A stop that has not finished in 8 s is abandoned. None of this
is needed for correctness; a kill is recovered by replay.

## Configuration

| Variable | Meaning | Default |
| --- | --- | --- |
| `CTRL_ADDR` | control port | `0.0.0.0:11659` |
| `MD_ADDR` | market data | `0.0.0.0:9999` |
| `CTRL_ALLOWED_CIDRS` | sources allowed on the control port | loopback and private ranges |
| `EVENT_DB_DIR` | event stores and lock file | `/data` |
| `OUTBOUND_QUEUE_CAP` | queue per D connection | `10000` |
| `SYMBOLS` | pairs, `NAME:tick:lot,...`. **Temporary**, to be replaced by the symbol config file (see below) | required |
| `RUST_LOG` | log level | `info` |
| `MAX_COMMAND_AGE` | **build time**, ms | `1000` |

## Trading pairs: a temporary arrangement

The pairs, with their tick and lot, are meant to come from a mounted config
file (D23). The format of that file is not decided, so for now they come from
one environment variable:

```
SYMBOLS=AAA:1:1,BBB:5:10        # NAME:tick:lot, comma separated
```

- It is required; the process refuses to start without it.
- Names must be unique, tick and lot must be positive integers.
- The order listed is the order engines are created in; it has no other effect.
- Everything downstream (engines, market-data cells, the check on incoming
  symbols, recovery) takes its pairs from `Config::symbols`, so replacing the
  source means changing only `Config::from_lookup` in `src/config.rs`, where a
  `TODO` marks the spot.
- **To be replaced by the config file.** When that happens, `SYMBOLS` goes away
  and this section should be removed.

## Choices to be aware of

- The session check (00:00 to 23:00 Dublin) lives in the engine as a pure
  predicate on the command timestamp, not in the journal. That keeps the
  `END_OF_DAY` rejection in the pair's own stream of outputs, in the same order
  on replay as live.
- Frames that cannot become commands (not JSON, unknown pair) are answered with
  an `error` message and not journaled.
- The market-data hand-over is a `watch` cell rather than a lock-free queue:
  same effect (only the newest snapshot survives, the engine never waits), and
  a subscriber task can await it.
- If a day was never closed by the timer, its end-of-day cancels are still
  written into that day's store, as described under the journal.
