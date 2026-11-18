# Failure injection and consistency — evidence (Service A's view)

Run on 18 Nov 2026, 15:32–15:39, against the real services on one Windows laptop: A, C and D as
local processes, B and PostgreSQL in Docker, started by `service_a/tools/run_local.ps1`, observed
through the team dashboard (Service C's Grafana dashboard and its live `/dashboard` page). Every
number below comes from the files in `evidence/`; the scripts that produced them re-run in about
seven minutes.

| File | What it is |
| --- | --- |
| `evidence/fault-demo-2026-11-18.json` | Timeline of injected faults with A's and D's health at each step; per-phase outcome counts |
| `evidence/fault-demo-2026-11-18-orders.jsonl` | All 487 order attempts: time, phase, status, reason, latency |
| `evidence/fault-demo-grafana.png` | Team Grafana dashboard over the fault run |
| `evidence/fault-demo-live/01-…07-*.png` | The live `/dashboard` page captured in the middle of each phase (D shown OFFLINE, A DEGRADED with its breaker OPEN, …) |
| `evidence/consistency-2026-11-18.json` | The five consistency scenarios and the final books check, with requests, rows and metrics |
| `evidence/consistency-grafana.png` | Team Grafana dashboard over the consistency run |

## 1. Degrade and recover (`service_a/tools/fault_demo.py`)

Steady traffic throughout: a limit order every 0.5 s (cancelled after 1 s), balance and market
reads, and a question to C every 5 s. Phases are 45 s each.

| Injected | Designed behaviour | Observed |
| --- | --- | --- |
| D's process killed (15:33:33) | Refuse new orders with `503 SYSTEM_BUSY` and hold nothing; stop calling D once it is known to be down | 88 of 89 orders refused `SYSTEM_BUSY` (the other was accepted just after D's restart). The first three waited for the 0.5 s connect timeout (0.6–0.7 s); from the fourth on A refused without calling D, in 17–23 ms. Live page: D OFFLINE, A DEGRADED, breaker OPEN (`fault-demo-live/02-d_down.png`) |
| D restarted (15:34:25) | Breaker lets a trial through; orders accepted again | First order accepted 0.5 s after D reported ready; all 52 orders in the phase accepted |
| D slowed to 2 s per decision (15:35:04) | A sheds load before D's queue grows | Shedding refused new orders from 4.0 s after the fault (98 of 109 refused); A's order-completion p95 rose to about 2.4 s on the dashboard. D rejected the slow orders `RISK_TIMEOUT` (it fails closed past its 500 ms gate deadline) and those notifications released their holds |
| Delay removed (15:36:03) | Shedding ends once its 30 s window falls below 80% of the threshold | First order accepted 10.9 s later |
| B stopped (15:36:49) | No order priced against stale data; D stops accepting | `NO_FRESH_PRICE` from 2.5 s after the stop (51 of 54); D `b_connected: false` |
| B started (15:37:20) | B replays its journal; both reconnect | First order accepted 4.1 s after the start |

After the run every order was closed and nothing was held; A's notification cursor equalled D's
newest notification. No request failed at the connection level.

## 2. Consistency mechanisms shown working (`service_a/tools/consistency_demo.py`)

All six checks passed (15:38–15:39).

| Scenario | Mechanism | Injected | What happened |
| --- | --- | --- | --- |
| Double submit | Idempotency key = `client_order_id`, unique per user | The same order sent twice concurrently | Both answers `202` with the same `order_id`; one order row; one `HOLD` ledger entry |
| Lost notification | D's transactional outbox; A applies notifications in contiguous `notify_seq` order | D skipped `notify_seq` 1677 | The order stayed `SENT`; when 1678 arrived A answered `409 {"expected_seq": 1677}`; D's log: "A expects notify_seq 1677 (sent 1678), resending from there"; both orders reached `PROCESSING`; cursors 1678 = 1678 |
| A crashes while outcomes are pending | The outbox survives the consumer's crash; idempotent consumer | 3 orders sent, then A killed; D decided while A was down | D's outbox held 3 undelivered notifications; A, restarted 6 s after the kill, was ready at 13.5 s and the backlog was empty at 15.0 s; A's three orders matched D's decisions (`REJECTED RISK_TIMEOUT`, hold 0); cursors 1685 = 1685 |
| No reply, D back within the resend window | Order `UNKNOWN`, hold kept — never released on a timeout (ADR-002) | D frozen 3 s during the call | A answered `202` after its 2 s timeout with status `UNKNOWN`, funds held; after the thaw D processed the original request and the order reached `PROCESSING`; D holds one copy |
| No reply past the resend window | Compensation: resend the same `order_id`, then cancel | D frozen 8 s | A sent the order 3 times with the same `order_id`, then cancelled; D holds one copy and sent it to B once; the order ended `CANCELLED` with hold 0 |
| Books after everything | Invariants over A's whole database (591 orders) | — | Balances explained by the ledger: 0 mismatches; holds equal open orders' holds: 0; closed orders still holding: 0; orders in doubt: 0; assets conserved across trades: 0 deviations; A's cursor = D's newest notification (1691) |

## Reproduce

```bash
powershell -ExecutionPolicy Bypass -File service_a\tools\run_local.ps1
python service_a\tools\fault_demo.py --shot-url http://127.0.0.1:8081/dashboard --shot-dir docs\submission\evidence\fault-demo-live
python service_a\tools\consistency_demo.py
python service_a\tools\screenshot.py --url "http://127.0.0.1:3001/d/lame-overview/lame-overview?orgId=1&kiosk" --out docs\submission\evidence\fault-demo-grafana.png --from <start> --to <end>
```

`fault_demo.py` restarts D with `FAULTS_ENABLED=true`, which `consistency_demo.py` needs. Both need
B's session (00:00–23:00 Dublin) and Windows (process freeze and kill).

## Gaps in what the dashboard shows

- The team Grafana dashboard has no annotations, no "service up" panel and no D outbox backlog, so
  the moment of each injection and the backlog during A's crash are in the JSON records and the live
  page shots, not on the Grafana graphs. `fault_demo.py` already posts each injection as an
  annotation on dashboard uid `lame-overview`; enabling built-in annotations would show them.
- D's request-rate line is missing on Grafana: its query adds `lame_d_refused_total`, which does not
  exist until D refuses something, and `x + (nothing)` is nothing in PromQL.
- C answered every question with an `insufficient_evidence` refusal in this run, so no LLM call was
  made and token counts stay 0 (C counts the tokens saved instead).
- B has no latency metric.
