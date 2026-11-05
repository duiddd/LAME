"""Replay D's rule decisions and check they come out the same (acceptance criterion 1, architecture doc 9).

For each decision, in decision_no order:
  1. apply B's outputs up to the decision's features_seq to a fresh book view;
  2. take as in flight every earlier approved order with no output applied yet;
  3. run the same rules again and compare with the recorded decision.

RISK_TIMEOUT and CANCELLED_BEFORE_SUBMIT are not rule decisions (they depend on time
and on A's requests), so they are counted but not recomputed.

Usage:
  python -m d.replay --d-db data/d.db --b-events /data/events-2026-11-05.db [...]
B's event stores are SQLite files events-<day>.db (table events); a .jsonl file with
one output per line is also accepted.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .book import BookView
from .config import Settings, load_static
from .rules import Opposite, decide

NOT_RULE_DECISIONS = {"RISK_TIMEOUT", "CANCELLED_BEFORE_SUBMIT"}
OPPOSITE = {"BUY": "SELL", "SELL": "BUY"}


def load_b_outputs(paths) -> list:
    """B's outputs (order_event and trade), sorted by (day, seq)."""
    out = []
    for p in map(Path, paths):
        if p.suffix == ".jsonl":
            out += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
            continue
        day = p.stem.removeprefix("events-")
        db = sqlite3.connect(p)
        for seq, kind, prev_seq, payload in db.execute(
                "SELECT seq, kind, prev_seq, payload FROM events WHERE kind IN ('OrderEvent', 'Trade') ORDER BY seq"):
            rec = json.loads(payload)
            rec.setdefault("type", "order_event" if kind == "OrderEvent" else "trade")
            rec.setdefault("day", day)
            rec.setdefault("seq", seq)
            rec.setdefault("prev_seq", prev_seq)
            out.append(rec)
    return sorted(out, key=lambda r: (r["day"], r["seq"]))


def replay(d_db: str, b_outputs: list, static) -> dict:
    db = sqlite3.connect(d_db)
    db.row_factory = sqlite3.Row
    orders = {r["order_id"]: dict(r) for r in db.execute("SELECT * FROM orders")}
    b_orders = {r["b_order_id"]: dict(r) for r in db.execute("SELECT * FROM b_orders")}
    b_by_order = {}
    for b in b_orders.values():
        b_by_order.setdefault(b["order_id"], []).append(b)
    decisions = [dict(r) for r in db.execute("SELECT * FROM decisions ORDER BY decision_no")]

    view, seen, approved = BookView(), set(), []
    i = 0
    report = {"checked": 0, "matched": 0, "skipped": 0, "mismatches": [], "other_rule_version": 0}
    for d in decisions:
        target = (d["features_day"] or "", d["features_seq"] or 0)
        while i < len(b_outputs) and (b_outputs[i]["day"], b_outputs[i]["seq"]) <= target:
            rec = b_outputs[i]
            b = view.resolve(rec, b_orders)
            view.apply(rec, b)
            if b is not None:
                seen.add(b["b_order_id"])
            i += 1
        if d["reason"] in NOT_RULE_DECISIONS:
            report["skipped"] += 1
        elif d["rule_version"] != static.rule_version:
            report["other_rule_version"] += 1
        else:
            o = orders[d["order_id"]]
            opp = view.opposites(o["symbol"], OPPOSITE[o["side"]])
            opp += [Opposite(b["user_id"], b["price"], b["qty"], None, True) for b in approved
                    if b["b_order_id"] not in seen and b["symbol"] == o["symbol"] and b["side"] == OPPOSITE[o["side"]]]
            group = static.groups.get(o["user_id"], frozenset({o["user_id"]}))
            again = decide(o, opp, group, static.rules)
            report["checked"] += 1
            if again == d["reason"]:
                report["matched"] += 1
            else:
                report["mismatches"].append({"decision_no": d["decision_no"], "order_id": d["order_id"],
                                             "recorded": d["reason"] or "APPROVED", "replayed": again or "APPROVED"})
        if d["decision"] == "APPROVED":
            approved += sorted(b_by_order.get(d["order_id"], []), key=lambda b: b["b_order_id"])[:1]
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--d-db", required=True)
    ap.add_argument("--b-events", nargs="+", required=True)
    args = ap.parse_args(argv)
    s = Settings()
    report = replay(args.d_db, load_b_outputs(args.b_events), load_static(s))
    print(json.dumps(report, indent=2))
    return 0 if not report["mismatches"] else 1


if __name__ == "__main__":
    sys.exit(main())
