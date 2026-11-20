# Failure injection and consistency — evidence (Service A's view)

Run on 20 Nov 2026, 02:17–02:25 UTC (Dublin time), against the system started by the root compose file
(`docker compose up`, with `FAULTS_ENABLED=true` so that D's fault switches exist) on fresh data, after the
team moved to B's pair grid (6 quantity decimals, USDT 6 decimals). It was observed through the team
dashboard: the Grafana dashboard, fed by Prometheus, and the frontend's live `/dashboard` page, fed by
Service C's aggregator. Every number below comes from the files in `evidence/`; the scripts that produced
them re-run in about eight minutes.

| File | What it is |
| --- | --- |
| `evidence/fault-demo-2026-11-20.json` | Timeline of injected faults with A's and D's health at each step; per-phase outcome counts |
| `evidence/fault-demo-2026-11-20-orders.jsonl` | All 503 order attempts: time, phase, status, reason, latency |
| `evidence/fault-demo-grafana.png` | Team Grafana dashboard over the fault run |
| `evidence/fault-demo-live/*.png` | The live `/dashboard` page, one capture per phase. Each is taken about 25–35 s into its phase (the browser needs time to render), so read the clock in the page's top-right corner rather than the file name |
| `evidence/consistency-2026-11-20.json` | The five consistency scenarios and the final books check, with requests, rows and metrics |
| `evidence/consistency-grafana.png` | Team Grafana dashboard over the consistency run: A's outage on "Service Up" and D's outbox backlog on "Settlement Pipeline" |

## 1. Degrade and recover (`service_a/tools/fault_demo.py --compose`)

Steady traffic throughout: a limit order every 0.5 s (cancelled after 1 s), balance and market reads, and a
question to C every 5 s. Phases are 45 s each. No request failed at the connection level.

| Injected | Designed behaviour | Observed |
| --- | --- | --- |
| D's container killed (02:18:26) | Refuse new orders with `503 SYSTEM_BUSY` and hold nothing; stop calling D once it is known to be down | 79 of 80 orders refused `SYSTEM_BUSY` (the other was accepted the moment D was back). The first two waited for the connect timeout (0.62 and 0.68 s); the rest were refused in 11–48 ms (median 24 ms). Grafana: D red on "Service Up", A's "Orders Refused" rises |
| D restarted (02:19:15) | The breaker lets a trial through; orders accepted again | First order accepted 1.2 s after D reported ready; all 64 orders in the phase accepted |
| D slowed to 2 s per decision (02:19:55) | A sheds load before D's queue grows | Shedding refused new orders from 4.8 s after the fault (94 of 107 refused); A's order-completion p95 rises on Grafana. D rejected the slow orders `RISK_TIMEOUT` (it fails closed past its 500 ms gate deadline), and those notifications released their holds |
| Delay removed (02:20:55) | Shedding ends once its 30 s window falls below 80% of the threshold | First order accepted 10.8 s later |
| B's container stopped (02:21:40) | No order priced against stale data; D stops accepting | `NO_FRESH_PRICE` from 2.6 s after the stop (51 of 53); D `b_connected: false` |
| B started (02:22:13) | B replays its journal; both reconnect | First order accepted 6.3 s after the start; 60 of 67 accepted in the phase |

## 2. Consistency mechanisms shown working (`service_a/tools/consistency_demo.py --compose`)

All six checks passed (02:23–02:25).

| Scenario | Mechanism | Injected | What happened |
| --- | --- | --- | --- |
| Double submit | Idempotency key = `client_order_id`, unique per user | The same order sent twice concurrently | Both answers `202` with the same `order_id`; one order row; one `HOLD` ledger entry |
| Lost notification | D's transactional outbox; A applies notifications in contiguous `notify_seq` order | D skipped `notify_seq` 716 | The order stayed `SENT`; when 717 arrived A answered `409 {"expected_seq": 716}`; D's log: "A expects notify_seq 716 (sent 717), resending from there"; both orders reached `PROCESSING`; cursors 717 = 717 |
| A crashes while outcomes are pending | The outbox survives the consumer's crash; idempotent consumer | 3 orders sent, then A's container killed; D decided while A was down | D's outbox held 3 undelivered notifications (Grafana: 02:23:50–02:24:05); A, started again 6 s after the kill, was ready at 16.2 s and the backlog was empty at 20.1 s; A's three orders matched D's decisions (`REJECTED RISK_TIMEOUT`, hold 0); cursors 724 = 724 |
| No reply, D back within the resend window | Order `UNKNOWN`, hold kept — never released on a timeout (ADR-002) | D's container paused 3 s during the call | A answered `202` after its 2 s timeout with status `UNKNOWN` and 47.500000 USDT held; 1.3 s after the pause ended the order reached `PROCESSING`; D holds one copy |
| No reply past the resend window | Compensation: resend the same `order_id`, then cancel | D's container paused 8 s | A sent the order 3 times with the same `order_id`, then cancelled; 1.2 s after the pause ended the order was `CANCELLED` with hold 0; D holds one copy and sent it to B once |
| Books after everything | Invariants over A's whole database (253 orders) | — | Balances explained by the ledger: 0 mismatches; holds equal open orders' holds: 0; closed orders still holding: 0; orders in doubt: 0; assets conserved across trades: 0 deviations; A's cursor = D's newest notification (730) |

In an earlier run the script reported the last scenario as failed although the books were right: A's event
log showed `UNKNOWN` → `PROCESSING` (D processing the original request after the pause) → `CANCELLED` (A's
compensating cancel 48 ms later), and the script had read the order in between. The script now waits for
the scenario's final outcome; the system's behaviour did not change.

## Reproduce

```bash
FAULTS_ENABLED=true docker compose up -d --build      # cmd: set FAULTS_ENABLED=true, then docker compose up -d --build
python service_a\tools\local_e2e.py
python service_a\tools\fault_demo.py --compose --shot-url http://127.0.0.1:8080/dashboard --shot-dir docs\submission\evidence\fault-demo-live
python service_a\tools\consistency_demo.py --compose
python service_a\tools\screenshot.py --url "http://127.0.0.1:3001/d/lame-overview/lame-overview?orgId=1&kiosk" --out docs\submission\evidence\fault-demo-grafana.png --from <start> --to <end>
```

The scripts need A's port 8000 published (the evidence run used a local override file for that, and C on
8081 because port 8080 is taken on that laptop) and B's session (00:00–23:00 Dublin). The same scripts run
against `run_local.ps1`'s processes without `--compose`.

## Open points

- The live `/dashboard` page marked A, B and D OFFLINE while D was down, in both compose runs, although
  Prometheus scraped A and B successfully throughout. C's aggregator allows 0.15 s to connect and 0.35 s to
  read, and reports the error of its localhost fallback rather than the first failure; a slow name lookup
  for the stopped container is a likely cause, not proven. The Grafana dashboard is correct.
- The Grafana dashboard has no annotations, so the moment of each injection is in the JSON timeline.
- Before any fault, three orders took 0.7–6.2 s (02:17:42–02:18:21), around when the screenshot browser
  started; the laptop's Docker VM has 3.8 GB and RabbitMQ used about 58% CPU while idle. Host contention is
  likely, not proven.
- C answered every question with an `insufficient_evidence` refusal in this run, so no LLM call was made and
  token counts stay 0 (C counts the tokens saved instead). B has no latency metric.
