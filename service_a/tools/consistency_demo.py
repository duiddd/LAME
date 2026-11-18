"""Shows A's consistency mechanisms working against the real D and B, then checks the books.

  python service_a/tools/consistency_demo.py [--workspace PATH]

Needs the services from run_local.ps1, with D started with FAULTS_ENABLED=true (fault_demo.py
restarts D that way). Windows only: D is frozen
with NtSuspendProcess and A is killed with taskkill. Run inside B's session (00:00-23:00 Dublin).

| Scenario | Mechanism shown | Injected | Passes when |
| --- | --- | --- | --- |
| idempotent_request | Idempotency-Key (client_order_id) | The same order sent twice at once | One order id, one order row, one hold |
| lost_notification | D's outbox + A's contiguous cursor | D skips one notify_seq | A answers 409 expected_seq, D resends, both orders settle, cursors equal |
| outbox_across_a_crash | Outbox + idempotent consumer | A killed while D's outcomes are pending | D's backlog grows while A is down, drains after restart; A ends in D's decision, rejected orders hold nothing; cursors equal |
| no_reply_resent | UNKNOWN, resend with the same order_id | D frozen 3 s during the call | Order UNKNOWN with the hold kept, then accepted; D holds one copy |
| no_reply_cancelled | Compensation: cancel after RESEND_WINDOW | D frozen 8 s | Order ends terminal with nothing held |
| books | Invariants over A's whole database | - | Ledger = balances, holds = open orders, nothing stuck, assets conserved, A cursor = D outbox |

Writes <workspace>/logs/consistency-<time>.json.
"""
import argparse
import asyncio
import ctypes
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx

A, D = "http://127.0.0.1:8000", "http://127.0.0.1:8400"
RUN = str(int(time.time()))[-6:]
WS: Path


# ------------------------------------------------------------------ helpers
async def metric(url, name, labels=""):
    """One sample from a Prometheus text page; 0 when the series does not exist yet."""
    async with httpx.AsyncClient(timeout=5) as c:
        body = (await c.get(f"{url}/metrics")).text
    prefix = f"{name}{{{labels}}} " if labels else f"{name} "
    for line in body.splitlines():
        if line.startswith(prefix):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def sql(query):
    out = subprocess.run(["docker", "exec", "lame-pg", "psql", "-U", "lame", "-d", "lame", "-At", "-F", "|", "-c", query],
                         capture_output=True, text=True, check=True).stdout.strip()
    return [row.split("|") for row in out.splitlines()] if out else []


def d_sql(query, *args):
    with sqlite3.connect(f"file:{WS / 'd-data' / 'd.db'}?mode=ro", uri=True) as c:
        return c.execute(query, args).fetchall()


def d_delivered_and_max():
    delivered = int(d_sql("SELECT value FROM meta WHERE key = 'delivered_seq'")[0][0])
    return delivered, d_sql("SELECT max(notify_seq) FROM outbox")[0][0]


def a_cursor():
    return int(sql("SELECT last_notify_seq FROM notify_cursor")[0][0])


def pids():
    return json.loads((WS / "pids.json").read_text(encoding="utf-8-sig"))


def suspend(pid, on):
    # Freezes or thaws D's process: its socket still accepts, but nothing answers.
    # 凍結或恢復 D 的行程:socket 仍會接受連線,但不會有回應。
    ntdll, kernel = ctypes.WinDLL("ntdll"), ctypes.WinDLL("kernel32")
    handle = kernel.OpenProcess(0x0800, False, pid)          # PROCESS_SUSPEND_RESUME
    if not handle:
        raise OSError(f"cannot open D's process {pid}")
    try:
        status = (ntdll.NtSuspendProcess if on else ntdll.NtResumeProcess)(handle)
        if status != 0:
            raise OSError(f"NtSuspend/ResumeProcess({pid}) returned {status:#x}")
    finally:
        kernel.CloseHandle(handle)


def d_process_pid():
    # pids.json may hold a launcher; the python child is the one listening on 8400.
    # pids.json 可能記的是啟動器;真正監聽 8400 的是 python 子行程。
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(":8400") and parts[3] == "LISTENING":
            return int(parts[4])
    raise RuntimeError("nothing listens on 8400")


def start_a():
    env = dict(os.environ, DATABASE_URL="postgresql+asyncpg://lame:lame@127.0.0.1:5432/lame", D_URL=D,
               B_MD_URL="ws://127.0.0.1:9999", JWT_SECRET=(WS / "jwt_secret.txt").read_text(encoding="utf-8-sig").strip(),
               PYTHONUTF8="1")
    log = open(WS / "logs" / "a.log", "ab")
    proc = subprocess.Popen([str(WS / "venv-ad" / "Scripts" / "python.exe"), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                             "--port", "8000", "--workers", "1"], cwd=WS / "a" / "service_a", env=env, stdout=log,
                            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    p = pids()
    p["A"] = proc.pid
    (WS / "pids.json").write_text(json.dumps(p, indent=4), encoding="utf-8")


async def wait_for(check, timeout, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if await check():
                return True
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        await asyncio.sleep(step)
    return False


class User:
    def __init__(self, name):
        self.name, self.n = f"{name}_{RUN}", 0
        self.c = httpx.AsyncClient(base_url=A, timeout=15)

    async def sign_up(self):
        r = await self.c.post("/api/auth/register", json={"username": self.name, "password": "consistency-pass-1"})
        assert r.status_code == 201, r.text
        r = await self.c.post("/api/accounts/deposits", json={"asset": "USDT", "amount": "1000000"}, headers={"Idempotency-Key": "c"})
        assert r.status_code == 201, r.text
        self.id = int(sql(f"SELECT user_id FROM users WHERE username = '{self.name}'")[0][0])
        candles = (await self.c.get("/api/market/candles", params={"symbol": "BTC-USDT", "interval": "5m"})).json()["candles"]
        ref = int(float(candles[-1]["c"]) * 100) if candles else 5000000
        self.price = f"{ref * 95 // 100 / 100:.2f}"            # inside the band, does not trade

    async def place(self, key=None):
        self.n += 1
        body = {"symbol": "BTC-USDT", "side": "BUY", "type": "LIMIT", "price": self.price, "qty": "0.001"}
        r = await self.c.post("/api/orders", json=body, headers={"Idempotency-Key": key or f"c{RUN}-{self.n}"})
        return r

    async def order(self, order_id):
        return (await self.c.get(f"/api/orders/{order_id}")).json()

    async def held(self):
        return next(b["held"] for b in (await self.c.get("/api/accounts/me")).json()["balances"] if b["asset"] == "USDT")

    async def until(self, order_id, statuses, timeout=30):
        return await wait_for(lambda: self._is(order_id, statuses), timeout)

    async def _is(self, order_id, statuses):
        return (await self.order(order_id))["status"] in statuses

    async def cancel_all(self, ids):
        for oid in ids:
            await self.c.delete(f"/api/orders/{oid}")
        for oid in ids:
            await self.until(oid, {"CANCELLED", "FILLED", "REJECTED"}, 20)


async def breaker_closed():
    return await wait_for(lambda: _closed(), 30, 0.5)


async def _closed():
    async with httpx.AsyncClient(timeout=3) as c:
        h = (await c.get(f"{A}/healthz")).json()
    return h["circuit"] == 0 and not h["shedding"]


async def d_faults(**faults):
    async with httpx.AsyncClient(timeout=5) as c:
        (await c.post(f"{D}/admin/faults", json=faults)).raise_for_status()


# ------------------------------------------------------------------ scenarios
async def idempotent_request(u: User):
    key = f"same-{RUN}"
    r1, r2 = await asyncio.gather(u.place(key), u.place(key))
    ids = {r.json().get("order_id") for r in (r1, r2)}
    oid = r1.json()["order_id"]
    await u.until(oid, {"PROCESSING"})
    rows = sql(f"SELECT count(*) FROM orders WHERE user_id = {u.id} AND client_order_id = '{key}'")[0][0]
    holds = sql(f"SELECT count(*) FROM ledger_entries WHERE ref_id LIKE '{oid}:%' AND reason = 'HOLD'")[0][0]
    held = await u.held()
    await u.cancel_all([oid])
    result = {"responses": [[r1.status_code, r1.json().get("order_id")], [r2.status_code, r2.json().get("order_id")]],
              "order_rows": int(rows), "hold_ledger_entries": int(holds), "usdt_held": held,
              "held_after_cancel": await u.held()}
    result["pass"] = len(ids) == 1 and result["order_rows"] == 1 and result["hold_ledger_entries"] == 1
    return result


async def lost_notification(u: User):
    gaps_before = await metric(A, "lame_a_notification_gaps_total")
    await d_faults(skip_next_notification=True)
    r1 = await u.place()
    o1 = r1.json()["order_id"]
    await asyncio.sleep(1.5)
    stuck = (await u.order(o1))["status"]                 # its ACCEPTED was skipped
    r2 = await u.place()                                  # the next notification exposes the gap
    o2 = r2.json()["order_id"]
    settled = await u.until(o1, {"PROCESSING"}, 15) and await u.until(o2, {"PROCESSING"}, 15)
    gaps_after = await metric(A, "lame_a_notification_gaps_total")
    await asyncio.sleep(1)
    delivered, newest = d_delivered_and_max()
    cursor = a_cursor()
    log = (WS / "logs" / "d.log").read_text(encoding="utf-8", errors="replace").splitlines()
    d_lines = [l for l in log[-400:] if "skipping notify_seq" in l or "A expects notify_seq" in l][-2:]
    await u.cancel_all([o1, o2])
    result = {"o1_status_while_skipped": stuck, "both_processing": settled, "a_gaps_answered_409": gaps_after - gaps_before,
              "d_log": d_lines, "a_cursor": cursor, "d_delivered_seq": delivered, "d_newest_seq": newest}
    result["pass"] = stuck != "PROCESSING" and settled and result["a_gaps_answered_409"] >= 1 and cursor == newest
    return result


async def outbox_across_a_crash(u: User):
    await d_faults(decision_delay_ms=1500)          # past D's 500 ms gate deadline: D rejects RISK_TIMEOUT
    ids = []
    for _ in range(3):
        r = await u.place()
        assert r.status_code == 202, r.text
        ids.append(r.json()["order_id"])
    statuses_before = [(await u.order(i))["status"] for i in ids]
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pids()["A"])], capture_output=True)
    killed = time.time()
    await asyncio.sleep(6)                                # D decides while A is gone
    await d_faults(decision_delay_ms=0)
    delivered, newest = d_delivered_and_max()
    backlog_while_down = await metric(D, "lame_d_outbox_backlog")
    pending = d_sql("SELECT notify_seq, type, order_id FROM outbox WHERE notify_seq > ? ORDER BY notify_seq", delivered)
    start_a()
    up = await wait_for(lambda: _a_ready(), 60, 0.5)
    a_up_after = round(time.time() - killed, 1)
    drained = await wait_for(lambda: _d_backlog_zero(), 60, 0.5)
    drained_after = round(time.time() - killed, 1)
    statuses_after = [(await u.order(i))["status"] for i in ids]
    decisions = [d_sql("SELECT decision, reason FROM decisions WHERE order_id = ?", i)[0] for i in ids]
    held_rows = [int(sql(f"SELECT held_amount FROM orders WHERE order_id = '{i}'")[0][0]) for i in ids]
    delivered2, newest2 = d_delivered_and_max()
    cursor = a_cursor()
    dup = await metric(A, "lame_a_duplicates_ignored_total", 'layer="notification"')
    await u.cancel_all(ids)
    result = {"statuses_before_kill": statuses_before, "d_backlog_while_a_down": backlog_while_down,
              "d_pending_notifications": [list(p) for p in pending], "a_ready_s_after_kill": a_up_after if up else None,
              "d_backlog_zero_s_after_kill": drained_after if drained else None, "d_decisions": decisions,
              "statuses_after": statuses_after, "a_held_amounts_after": held_rows,
              "a_duplicates_ignored_after_restart": dup, "a_cursor": cursor, "d_delivered_seq": delivered2, "d_newest_seq": newest2}
    agree = all((d == "APPROVED" and st == "PROCESSING") or (d == "REJECTED" and st == "REJECTED" and h == 0)
                for (d, _), st, h in zip(decisions, statuses_after, held_rows))
    result["pass"] = backlog_while_down >= 1 and drained and agree and cursor == newest2
    return result


async def _a_ready():
    async with httpx.AsyncClient(timeout=2) as c:
        return (await c.get(f"{A}/healthz")).json()["ready"]


async def _d_backlog_zero():
    return await metric(D, "lame_d_outbox_backlog") == 0


async def no_reply(u: User, frozen_s: float):
    pid = d_process_pid()
    suspend(pid, True)
    try:
        start = time.perf_counter()
        r = await u.place()
        answer = {"status": r.status_code, "body": r.json(), "ms": round((time.perf_counter() - start) * 1000)}
        oid = r.json().get("order_id")
        seen = (await u.order(oid)) if oid else {}
        held_while_frozen = await u.held()
        await asyncio.sleep(max(0, frozen_s - (time.perf_counter() - start)))
    finally:
        suspend(pid, False)
    thawed = time.perf_counter()
    final = None
    if oid:
        await wait_for(lambda: _settled(u, oid), 60, 0.5)
        final = await u.order(oid)
    attempts = sql(f"SELECT attempts, status, held_amount, closed_at IS NOT NULL FROM orders WHERE order_id = '{oid}'")[0] if oid else None
    d_copies = d_sql("SELECT count(*) FROM orders WHERE order_id = ?", oid)[0][0] if oid else None
    d_b_orders = d_sql("SELECT count(*) FROM b_orders WHERE order_id = ?", oid)[0][0] if oid else None
    return {"frozen_s": frozen_s, "place_answer": answer, "status_while_frozen": seen.get("status"),
            "usdt_held_while_frozen": held_while_frozen, "final_status": final and final["status"],
            "settled_s_after_thaw": round(time.perf_counter() - thawed, 1), "a_row_attempts_status_held_closed": attempts,
            "d_order_rows": d_copies, "d_orders_sent_to_b": d_b_orders}, oid


async def _settled(u, oid):
    o = await u.order(oid)
    return o["status"] in ("PROCESSING", "CANCELLED", "REJECTED", "FILLED") and not o.get("cancel_requested")


async def no_reply_resent(u: User):
    result, oid = await no_reply(u, 3)
    if oid and result["final_status"] == "PROCESSING":
        await u.cancel_all([oid])
    result["pass"] = result["status_while_frozen"] == "UNKNOWN" and result["final_status"] == "PROCESSING" \
        and result["d_order_rows"] == 1 and float(result["usdt_held_while_frozen"]) > 0
    return result


async def no_reply_cancelled(u: User):
    result, oid = await no_reply(u, 8)
    if oid and result["final_status"] == "PROCESSING":
        await u.cancel_all([oid])
    row = result["a_row_attempts_status_held_closed"]
    result["pass"] = result["status_while_frozen"] == "UNKNOWN" and result["final_status"] in ("CANCELLED", "REJECTED") \
        and row is not None and row[2] == "0" and row[3] == "t" and (result["d_orders_sent_to_b"] or 0) <= 1
    return result


def books():
    checks = {
        "accounts_not_explained_by_ledger": """SELECT count(*) FROM accounts a LEFT JOIN (SELECT user_id, asset,
            sum(delta_available) av, sum(delta_held) h FROM ledger_entries GROUP BY 1, 2) l USING (user_id, asset)
            WHERE a.available <> coalesce(l.av, 0) OR a.held <> coalesce(l.h, 0)""",
        "held_not_matching_open_orders": """WITH o AS (SELECT user_id, CASE WHEN side = 'BUY' THEN split_part(symbol, '-', 2)
            ELSE split_part(symbol, '-', 1) END AS asset, sum(held_amount) h FROM orders GROUP BY 1, 2)
            SELECT count(*) FROM accounts a LEFT JOIN o USING (user_id, asset) WHERE a.held <> coalesce(o.h, 0)""",
        "closed_orders_still_holding": "SELECT count(*) FROM orders WHERE closed_at IS NOT NULL AND held_amount <> 0",
        "orders_in_doubt": "SELECT count(*) FROM orders WHERE status IN ('PENDING', 'UNKNOWN')",
        "assets_not_conserved": """SELECT count(*) FROM (SELECT asset, sum(delta_available + delta_held) s FROM ledger_entries
            WHERE ref_type NOT IN ('deposit', 'seed') GROUP BY asset) t WHERE s <> 0""",
    }
    result = {name: int(sql(q)[0][0]) for name, q in checks.items()}
    result["open_orders_by_status"] = dict((s, int(n)) for s, n in sql("SELECT status, count(*) FROM orders WHERE closed_at IS NULL GROUP BY 1"))
    result["orders_total"] = int(sql("SELECT count(*) FROM orders")[0][0])
    result["trades_total"] = int(sql("SELECT count(*) FROM trades")[0][0])
    delivered, newest = d_delivered_and_max()
    result.update({"a_cursor": a_cursor(), "d_delivered_seq": delivered, "d_newest_seq": newest})
    result["pass"] = all(result[k] == 0 for k in checks) and result["a_cursor"] == newest
    return result


async def main():
    global WS
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[3]))
    WS = Path(parser.parse_args().workspace)
    u = User("consistency")
    await u.sign_up()
    results = {}
    for name, scenario in [("idempotent_request", idempotent_request), ("lost_notification", lost_notification),
                           ("outbox_across_a_crash", outbox_across_a_crash), ("no_reply_resent", no_reply_resent),
                           ("no_reply_cancelled", no_reply_cancelled)]:
        await breaker_closed()
        print(f"== {name}", flush=True)
        try:
            results[name] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **await scenario(u)}
        except Exception as e:                            # record and go on: the books check still runs
            results[name] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pass": False, "error": repr(e)}
        print(json.dumps(results[name], default=str), flush=True)
    await asyncio.sleep(3)
    print("== books", flush=True)
    results["books"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **books()}
    print(json.dumps(results["books"]), flush=True)
    out = WS / "logs" / f"consistency-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print("\n".join(f"{'PASS' if r['pass'] else 'FAIL'}  {n}" for n, r in results.items()))
    print("written", out)


asyncio.run(main())
