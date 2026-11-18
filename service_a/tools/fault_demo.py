"""Fault-injection run for the dashboard: steady traffic, three injected failures, one log.

  python service_a/tools/fault_demo.py [--workspace PATH] [--phase SECONDS] [--grafana URL]
                                      [--shot-url URL [--shot-dir DIR]]

Needs the services started by run_local.ps1. Run inside B's session (00:00-23:00 Dublin time).

Timeline, with P = --phase seconds (default 45):
  0     baseline traffic
  1P    D's process killed                 -> A refuses with SYSTEM_BUSY, breaker opens
  2P    D restarted (with FAULTS_ENABLED)  -> breaker half-opens after 10 s and closes
  3P    D slowed: 2 s per risk decision    -> D rejects RISK_TIMEOUT (500 ms gate); A sheds load
  4.33P delay removed                      -> shedding ends
  5.33P B stopped                          -> no fresh price: A refuses NO_FRESH_PRICE, D loses B
  6P    B started                          -> both reconnect
  7P    end

Traffic: one trader places a limit buy 5% under the market every 0.5 s and cancels it after 1 s;
a reader polls the market summary and the balance; C is asked a question every 5 s.
Each fault start and end is posted to Grafana (--grafana, default http://127.0.0.1:3001; empty to
skip) as an annotation tagged "fault" on dashboard uid lame-overview, so it marks each injection.
With --shot-url, a live page (for example Service C's /dashboard) is captured in the middle of each
phase, because a live page keeps no history.
Writes <workspace>/logs/fault-demo-<time>.json (timeline and per-phase summary) and
<workspace>/logs/fault-demo-<time>.jsonl (every order attempt).
"""
import argparse
import asyncio
import json
import os
import statistics
import subprocess
import time
from collections import Counter
from pathlib import Path

import httpx

from screenshot import capture

A, C, D = "http://127.0.0.1:8000", "http://127.0.0.1:8081", "http://127.0.0.1:8400"
RUN = str(int(time.time()))[-6:]


class Run:
    def __init__(self, workspace: Path, grafana: str, shot_url: str, shot_dir: Path, phase_s: float):
        self.workspace, self.grafana = workspace, grafana
        self.shot_url, self.shot_dir, self.phase_s = shot_url, shot_dir, phase_s
        self.started = time.time()
        self.phase = "baseline"
        self.timeline, self.attempts = [], []
        self.a = httpx.AsyncClient(base_url=A, timeout=10)
        self.stop = asyncio.Event()

    def now(self):
        return round(time.time() - self.started, 1)

    async def mark(self, phase, text):
        # Records a phase change locally and as a Grafana annotation.
        # 在本地紀錄階段變化，並在 Grafana 打上 annotation。
        self.phase = phase
        entry = {"t_s": self.now(), "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "phase": phase, "event": text,
                 "a_health": await self.health(f"{A}/healthz"), "d_health": await self.health(f"{D}/healthz")}
        self.timeline.append(entry)
        print(json.dumps(entry), flush=True)
        if self.shot_url and phase != "end":
            asyncio.create_task(self.snapshot(phase))
        if not self.grafana:
            return
        try:
            async with httpx.AsyncClient(timeout=3) as g:
                await g.post(f"{self.grafana}/api/annotations",
                             json={"time": int(time.time() * 1000), "tags": ["fault", phase], "text": text,
                                   "dashboardUID": "lame-overview"})   # the team dashboard's uid: shown on it
        except httpx.HTTPError as e:
            print(f"annotation not posted: {e!r}", flush=True)

    async def snapshot(self, phase):
        # A live dashboard has no history: capture it while the phase is in effect.
        # 即時 dashboard 沒有歷史資料:要在該階段進行中截圖。
        await asyncio.sleep(self.phase_s * 0.6)
        path = self.shot_dir / f"{len(self.timeline):02d}-{phase}.png"
        try:
            await asyncio.to_thread(capture, self.shot_url, path, 1600, 1400)
        except (SystemExit, subprocess.SubprocessError) as e:
            print(f"snapshot of {phase} failed: {e}", flush=True)

    async def health(self, url):
        try:
            async with httpx.AsyncClient(timeout=2) as c:
                body = (await c.get(url)).json()
            keep = ("ready", "circuit", "shedding", "b_connected", "status")
            return {k: body[k] for k in keep if k in body}
        except (httpx.HTTPError, ValueError) as e:
            return {"error": type(e).__name__}


async def sign_up(run: Run):
    name = f"fault_{RUN}"
    r = await run.a.post("/api/auth/register", json={"username": name, "password": "fault-demo-pass-1"})
    assert r.status_code == 201, r.text
    r = await run.a.post("/api/accounts/deposits", json={"asset": "USDT", "amount": "1000000"}, headers={"Idempotency-Key": "f"})
    assert r.status_code == 201, r.text
    candles = (await run.a.get("/api/market/candles", params={"symbol": "BTC-USDT", "interval": "5m"})).json()["candles"]
    ref = int(float(candles[-1]["c"]) * 100) if candles else 5000000
    return f"{ref * 95 // 100 / 100:.2f}"          # inside the 10% band, does not trade


async def trader(run: Run, price: str):
    n = 0
    while not run.stop.is_set():
        n += 1
        body = {"symbol": "BTC-USDT", "side": "BUY", "type": "LIMIT", "price": price, "qty": "0.001"}
        start = time.perf_counter()
        try:
            r = await run.a.post("/api/orders", json=body, headers={"Idempotency-Key": f"f{RUN}-{n}"})
            status, reason = r.status_code, (r.json().get("reason") if r.status_code != 202 else None)
            if status == 202:
                asyncio.create_task(cancel_later(run, r.json()["order_id"]))
        except httpx.HTTPError as e:
            status, reason = 0, type(e).__name__
        run.attempts.append({"t_s": run.now(), "phase": run.phase, "status": status, "reason": reason,
                             "ms": round((time.perf_counter() - start) * 1000, 1)})
        await asyncio.sleep(0.5)


async def cancel_later(run: Run, order_id: str):
    await asyncio.sleep(1)
    try:
        await run.a.delete(f"/api/orders/{order_id}")
    except httpx.HTTPError:
        pass                                          # reconciliation cancels what is left


async def reader(run: Run):
    while not run.stop.is_set():
        try:
            await run.a.get("/api/market/summary")
            await run.a.get("/api/accounts/me")
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)


async def analyst(run: Run):
    async with httpx.AsyncClient(base_url=C, timeout=30) as c:
        while not run.stop.is_set():
            try:
                await c.post("/api/chat", json={"query": "Why did BTC move in the last hour?", "symbol": "BTC-USDT"})
            except httpx.HTTPError:
                pass
            await asyncio.sleep(5)


def start_d(workspace: Path) -> int:
    # Starts D the way run_local.ps1 does, with the fault switches on.
    # 以 run_local.ps1 相同方式啟動 D，並開啟故障開關。
    env = dict(os.environ, B_CTRL_URL="ws://127.0.0.1:11659/d", A_NOTIFY_URL=f"{A}/internal/order-events",
               DATA_DIR=str(workspace / "d-data"), PYTHONUTF8="1", FAULTS_ENABLED="true")
    log = open(workspace / "logs" / "d.log", "ab")
    proc = subprocess.Popen([str(workspace / "venv-ad" / "Scripts" / "python.exe"), "-m", "d.main"],
                            cwd=workspace / "d" / "service_d", env=env, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return proc.pid


async def d_faults(**faults):
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.post(f"{D}/admin/faults", json=faults)
        r.raise_for_status()


async def wait_d_ready(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            async with httpx.AsyncClient(timeout=2) as c:
                if (await c.get(f"{D}/healthz")).json().get("ready"):
                    return True
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(0.5)
    return False


def summarise(attempts):
    by_phase = {}
    for phase in dict.fromkeys(a["phase"] for a in attempts):
        rows = [a for a in attempts if a["phase"] == phase]
        ms = sorted(a["ms"] for a in rows)
        by_phase[phase] = {
            "orders": len(rows),
            "outcomes": dict(Counter("accepted" if a["status"] == 202 else f"{a['status']} {a['reason']}" for a in rows)),
            "post_ms_p50": round(statistics.median(ms), 1),
            "post_ms_max": ms[-1],
        }
    return by_phase


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument("--phase", type=float, default=45)
    parser.add_argument("--grafana", default="http://127.0.0.1:3001")
    parser.add_argument("--shot-url", default="", help="a live dashboard page to capture in the middle of each phase")
    parser.add_argument("--shot-dir", default="")
    args = parser.parse_args()
    workspace, P = Path(args.workspace), args.phase
    shot_dir = Path(args.shot_dir or workspace / "logs" / f"fault-demo-shots-{time.strftime('%Y%m%d-%H%M%S')}")
    shot_dir.mkdir(parents=True, exist_ok=True)
    run = Run(workspace, args.grafana, args.shot_url, shot_dir, P)
    price = await sign_up(run)
    tasks = [asyncio.create_task(f(run, price) if f is trader else f(run)) for f in (trader, reader, analyst)]
    pids_file = workspace / "pids.json"

    async def at(seconds):
        await asyncio.sleep(max(0, run.started + seconds - time.time()))

    await run.mark("baseline", "Baseline traffic")
    await at(1 * P)
    pids = json.loads(pids_file.read_text(encoding="utf-8-sig"))
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pids["D"])], capture_output=True)
    await run.mark("d_down", "FAULT: D's process killed")
    await at(2 * P)
    pids["D"] = start_d(workspace)
    pids_file.write_text(json.dumps(pids, indent=4), encoding="utf-8")
    ready = await wait_d_ready()
    await run.mark("d_recovering", f"RECOVER: D restarted (ready={ready})")
    await at(3 * P)
    await d_faults(decision_delay_ms=2000)
    await run.mark("d_slow", "FAULT: D slowed to 2 s per risk decision")
    await at(4.33 * P)
    await d_faults(decision_delay_ms=0)
    await run.mark("d_slow_recovering", "RECOVER: D's delay removed")
    await at(5.33 * P)
    subprocess.run(["docker", "stop", "lame-b"], capture_output=True)
    await run.mark("b_down", "FAULT: B (matching engine) stopped")
    await at(6 * P)
    subprocess.run(["docker", "start", "lame-b"], capture_output=True)
    await run.mark("b_recovering", "RECOVER: B started")
    await at(7 * P)
    await run.mark("end", "End of run")
    run.stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = workspace / "logs" / f"fault-demo-{stamp}"
    out.with_suffix(".jsonl").write_text("\n".join(json.dumps(a) for a in run.attempts) + "\n", encoding="utf-8")
    result = {"phase_seconds": P, "order_price": price, "timeline": run.timeline, "by_phase": summarise(run.attempts)}
    out.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["by_phase"], indent=1))
    print("written", out.with_suffix(".json"))


asyncio.run(main())
