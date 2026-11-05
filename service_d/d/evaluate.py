"""Evaluate D's rules against the bots' labels (acceptance criterion 2, architecture doc 11).

Labels: one JSON object per line (or a JSON array),
  {"order_id": "<A's ULID>", "user_id": 1024, "label": "LEGIT" | "SELF_TRADE" | "WASH_TRADE", "scenario_id": "..."}
joined with D's decisions on (user_id, order_id).

Reported:
  - per rule: precision = rejected with this reason and labelled so / rejected with this reason;
              recall    = rejected with this reason and labelled so / labelled so
  - overall (any manipulation vs any rule rejection)
  - false positive rate = LEGIT orders rejected by a rule / all LEGIT orders
  - decision latency p50 / p99 (received -> decision written)
Orders that never got a decision (for example refused with 503) are counted as "not decided".

Usage: python -m d.evaluate --d-db data/d.db --labels labels.jsonl
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

RULES = ("SELF_TRADE", "WASH_TRADE")


def load_labels(path) -> list:
    text = Path(path).read_text().strip()
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _ratio(a, b):
    return round(a / b, 4) if b else None


def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def evaluate(d_db: str, labels: list) -> dict:
    db = sqlite3.connect(d_db)
    db.row_factory = sqlite3.Row
    decided = {}
    for r in db.execute("SELECT o.user_id, o.order_id, o.received_at, d.decision, d.reason, d.decided_at "
                        "FROM decisions d JOIN orders o ON o.order_id = d.order_id ORDER BY d.decision_no"):
        decided.setdefault((r["user_id"], r["order_id"]), dict(r))

    counts = Counter()
    by_scenario = defaultdict(Counter)
    for lab in labels:
        d = decided.get((int(lab["user_id"]), lab["order_id"]))
        truth = lab["label"]
        if d is None:
            counts[("not_decided", truth)] += 1
            continue
        got = d["reason"] if d["reason"] in RULES else ("OTHER_REJECT" if d["decision"] == "REJECTED" else "APPROVED")
        counts[(truth, got)] += 1
        by_scenario[lab.get("scenario_id")][(truth, got)] += 1

    def rule_stats(c, rule):
        tp = c[(rule, rule)]
        predicted = sum(n for (t, g), n in c.items() if g == rule and t != "not_decided")
        actual = sum(n for (t, g), n in c.items() if t == rule)
        return {"precision": _ratio(tp, predicted), "recall": _ratio(tp, actual),
                "true_positive": tp, "rejected_with_reason": predicted, "labelled": actual}

    def overall(c):
        manip = sum(n for (t, g), n in c.items() if t in RULES)
        caught = sum(n for (t, g), n in c.items() if t in RULES and g in RULES)
        flagged = sum(n for (t, g), n in c.items() if g in RULES)
        legit = sum(n for (t, g), n in c.items() if t == "LEGIT")
        legit_blocked = sum(n for (t, g), n in c.items() if t == "LEGIT" and g in RULES)
        return {"precision": _ratio(caught, flagged), "recall": _ratio(caught, manip),
                "false_positive_rate": _ratio(legit_blocked, legit), "legit_orders": legit,
                "legit_blocked": legit_blocked}

    latencies = [d["decided_at"] - d["received_at"] for d in decided.values()]
    return {
        "per_rule": {rule: rule_stats(counts, rule) for rule in RULES},
        "overall": overall(counts),
        "per_scenario": {str(s): overall(c) for s, c in sorted(by_scenario.items(), key=lambda x: str(x[0]))},
        "not_decided": sum(n for (t, g), n in counts.items() if t == "not_decided"),
        "other_rejections": sum(n for (t, g), n in counts.items() if g == "OTHER_REJECT"),
        "decision_latency_ms": {"p50": percentile(latencies, 50), "p99": percentile(latencies, 99),
                                "count": len(latencies)},
        "confusion": {f"{t} -> {g}": n for (t, g), n in sorted(counts.items())},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--d-db", required=True)
    ap.add_argument("--labels", required=True)
    args = ap.parse_args(argv)
    print(json.dumps(evaluate(args.d_db, load_labels(args.labels)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
