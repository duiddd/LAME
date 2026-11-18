"""Trigger D's failure scenarios (architecture doc section 12, RUNBOOK.md).

  python -m tools.fault status
  python -m tools.fault d-slow 800          # extra 800 ms per decision; 0 turns it off
  python -m tools.fault stale-features on   # stop applying B's outputs; off to resume
  python -m tools.fault reply-lost          # drop the B connection right after the next new_order
  python -m tools.fault lose-notification   # skip the next notify_seq
  python -m tools.fault d-down              # docker kill D (compose restarts it)
  python -m tools.fault b-down              # docker kill B

D must run with FAULTS_ENABLED=true. D_URL (default http://localhost:8400),
D_CONTAINER (lame-service-d) and B_CONTAINER (lame-service-b) can be set in the environment.
"""
import json
import os
import re
import subprocess
import sys

import httpx

D_URL = os.getenv("D_URL", "http://localhost:8400")
CONTAINERS = {"d-down": os.getenv("D_CONTAINER", "lame-service-d"), "b-down": os.getenv("B_CONTAINER", "lame-service-b")}
WATCH = ("lame_d_ready", "lame_d_b_connected", "lame_d_decision_queue", "lame_d_timed_out_orders",
         "lame_d_cancels_waiting", "lame_d_outbox_backlog", "lame_d_refused_total", "lame_d_decisions_total")


def set_faults(**switches):
    r = httpx.post(f"{D_URL}/admin/faults", json=switches, timeout=3)
    r.raise_for_status()
    print(json.dumps(r.json(), indent=2))


def status():
    h = httpx.get(f"{D_URL}/healthz", timeout=3)
    print("healthz", h.status_code, json.dumps(h.json()))
    for line in httpx.get(f"{D_URL}/metrics", timeout=3).text.splitlines():
        if line.startswith(WATCH) and not re.search(r"_created|_bucket", line):
            print(" ", line)


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, arg = argv[0], (argv[1] if len(argv) > 1 else None)
    if cmd == "status":
        status()
    elif cmd == "d-slow":
        set_faults(decision_delay_ms=int(arg or 800))
    elif cmd == "stale-features":
        set_faults(pause_b_apply=(arg or "on") == "on")
    elif cmd == "reply-lost":
        set_faults(disconnect_after_send=True)
    elif cmd == "lose-notification":
        set_faults(skip_next_notification=True)
    elif cmd in CONTAINERS:
        subprocess.run(["docker", "kill", CONTAINERS[cmd]], check=True)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
