# Failure injection and consistency — evidence (Service A's view)

Run on 20 Nov 2026, 00:43–00:55 UTC (Dublin time), against the system started by the root compose file
(`docker compose up`, with `FAULTS_ENABLED=true` so that D's fault switches exist), after the team
moved to B's pair grid (6 quantity decimals). It was observed through the team dashboard: Service C's
Grafana dashboard, fed by Prometheus, and the frontend's live `/dashboard` page. Every number below
comes from the files in `evidence/`; the scripts that produced them re-run in about eight minutes.

| File | What it is |
| --- | --- |
| `evidence/fault-demo-2026-11-20.json` | Timeline of injected faults with A's and D's health at each step; per-phase outcome counts |
| `evidence/fault-demo-2026-11-20-orders.jsonl` | All 458 order attempts: time, phase, status, reason, latency |
| `evidence/fault-demo-grafana.png` | Team Grafana dashboard over the fault run |
| `evidence/fault-demo-live/*.png` | The live `/dashboard` page, one capture per phase. Each is taken about 25–35 s into its phase (the browser needs time to render), so read the clock in the page's top-right corner rather than the file name |
| `evidence/consistency-2026-11-20.json` | The five consistency scenarios and the final books check, with requests, rows and metrics |
| `evidence/consistency-grafana.png` | Team Grafana dashboard over the consistency run |

## 1. Degrade and recover (`service_a/tools/fault_demo.py --compose`)

Steady traffic throughout: a limit order every 0.5 s (cancelled after 1 s), balance and market reads,
and a question to C every 5 s. Phases are 45 s each. No request failed at the connection level.

| Injected | Designed behaviour | Observed |
| --- | --- | --- |
| D's container killed (00:44:23) | Refuse new orders with `503 SYSTEM_BUSY` and hold nothing; stop calling D once it is known to be down | All 60 orders refused `SYSTEM_BUSY`. The first waited for the connect timeout (0.64 s); the rest were refused in 18–56 ms (median 29 ms). The live page shows D OFFLINE and A DEGRADED; Prometheus shows D down from 00:44:25 to 00:45:05 |
| D restarted (00:45:05) | The breaker lets a trial through; orders accepted again | First order accepted 2.7 s after D reported ready; 51 of 54 accepted in the phase |
| D slowed to 2 s per decision (00:45:41) | A sheds load before D's queue grows | Shedding refused new orders from 5.5 s after the fault (91 of 104 refused); Grafana shows A's order-completion p95 at about 2.4 s. D rejected the slow orders `RISK_TIMEOUT` (it fails closed past its 500 ms gate deadline), and those notifications released their holds |
| Delay removed (00:46:40) | Shedding ends once its 30 s window falls below 80% of the threshold | First order accepted 12.2 s later |
| B's container stopped (00:47:25) | No order priced against stale data; D stops accepting | `NO_FRESH_PRICE` from 2.2 s after the stop (51 of 52); D `b_connected: false` |
| B started (00:47:59) | B replays its journal; both reconnect | First order accepted 6.8 s after the start; 51 of 62 accepted in the phase |

## 2. Consistency mechanisms shown working (`service_a/tools/consistency_demo.py --compose`)

All six checks passed (00:53–00:55).

| Scenario | Mechanism | Injected | What happened |
| --- | --- | --- | --- |
| Double submit | Idempotency key = `client_order_id`, unique per user | The same order sent twice concurrently | Both answers `202` with the same `order_id`; one order row; one `HOLD` ledger entry |
| Lost notification | D's transactional outbox; A applies notifications in contiguous `notify_seq` order | D skipped `notify_seq` 689 | The order stayed `SENT`; when 690 arrived A answered `409 {"expected_seq": 689}`; D's log: "A expects notify_seq 689 (sent 690), resending from there"; both orders reached `PROCESSING`; cursors 690 = 690 |
| A crashes while outcomes are pending | The outbox survives the consumer's crash; idempotent consumer | 3 orders sent, then A's container killed; D decided while A was down | D's outbox held 3 undelivered notifications; A, started again 6 s after the kill, was ready at 20.9 s and the backlog was empty at 21.5 s; A's three orders matched D's decisions (`REJECTED RISK_TIMEOUT`, hold 0); cursors 697 = 697 |
| No reply, D back within the resend window | Order `UNKNOWN`, hold kept — never released on a timeout (ADR-002) | D's container paused 3 s during the call | A answered `202` after its 2 s timeout with status `UNKNOWN` and 47.5 USDT held; 1.7 s after the pause ended the order reached `PROCESSING`; D holds one copy |
| No reply past the resend window | Compensation: resend the same `order_id`, then cancel | D's container paused 8 s | A sent the order 3 times with the same `order_id`, then cancelled; the order ended `CANCELLED` with hold 0; D holds one copy and sent it to B once |
| Books after everything | Invariants over A's whole database (243 orders) | — | Balances explained by the ledger: 0 mismatches; holds equal open orders' holds: 0; closed orders still holding: 0; orders in doubt: 0; assets conserved across trades: 0 deviations; A's cursor = D's newest notification (703) |

The first run of this script reported the last scenario as failed although the books were right. A's
event log for that order showed `UNKNOWN` → `PROCESSING` (00:50:50.807, D processing the original
request after the pause) → `CANCELLED` (00:50:50.855, A's compensating cancel 48 ms later), and the
script had read the order in between. The script now waits for the scenario's final outcome; the
system's behaviour did not change.

## Reproduce

```bash
FAULTS_ENABLED=true docker compose up -d --build      # cmd: set FAULTS_ENABLED=true, then docker compose up -d --build
python service_a\tools\local_e2e.py
python service_a\tools\fault_demo.py --compose --shot-url http://127.0.0.1:8080/dashboard --shot-dir docs\submission\evidence\fault-demo-live
python service_a\tools\consistency_demo.py --compose
python service_a\tools\screenshot.py --url "http://127.0.0.1:3001/d/lame-overview/lame-overview?orgId=1&kiosk" --out docs\submission\evidence\fault-demo-grafana.png --from <start> --to <end>
```

The scripts need A's port 8000 published (the evidence run used a local override file for that, and
C on 8081 because port 8080 is taken on that laptop) and B's session (00:00–23:00 Dublin). The same
scripts run against `run_local.ps1`'s processes without `--compose`.

## What the dashboard does not show, and open points

- The team Grafana dashboard has no annotations, no "service up" panel and no D outbox backlog, so
  the moment of each injection and the backlog during A's crash are in the JSON records, not on the
  Grafana graphs.
- D's request-rate line is missing on Grafana until D has refused an order: its query adds
  `lame_d_refused_total`, which does not exist before that, and `x + (nothing)` is nothing in PromQL.
- The live `/dashboard` page marked A and B OFFLINE in the 00:44:58 capture while Prometheus scraped
  both successfully throughout: C's aggregator gives each scrape 0.35 s and the whole collection
  0.75 s, and reports the error of its fallback URL rather than the first failure.
- A's `/metrics` took up to 7.3 s at 00:44:15–00:44:20 and 3.4 s at 00:43:20, and five orders took
  0.8–4.8 s during the baseline phase, before any fault. The spikes coincide with the screenshot
  browser starting and with RabbitMQ using about 58% CPU in a 3.8 GB Docker VM on the laptop; that
  host contention caused them is likely but not proven.
- C answered every question with an `insufficient_evidence` refusal in this run, so no LLM call was
  made and token counts stay 0 (C counts the tokens saved instead). B has no latency metric.
