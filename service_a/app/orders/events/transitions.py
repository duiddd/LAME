"""Which notification may act on which status. Tests enumerate every combination (section 5.6)."""
from enum import Enum

TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
BEFORE_OUTCOME = {"PENDING", "SENT", "UNKNOWN"}
LIVE = {"PROCESSING", "PARTIALLY_FILLED", "MODIFYING"}
ALL_STATUSES = TERMINAL | BEFORE_OUTCOME | LIVE


class Verdict(Enum):
    APPLY = "apply"
    IGNORE = "ignore"     # legal and has no effect
    ILLEGAL = "illegal"   # A and D disagree: stop the line


def judge(status: str, kind: str) -> Verdict:
    """`kind` is the notification type, plus the status or result where that decides the case."""
    terminal = status in TERMINAL
    if kind == "RISK_REJECTED":
        # D's protocol 8.5: a late order after a tombstone is rejected, and A has already finished it.
        if terminal:
            return Verdict.IGNORE
        return Verdict.APPLY if status in BEFORE_OUTCOME else Verdict.ILLEGAL
    if kind == "ACCEPTED":
        return Verdict.ILLEGAL if terminal else Verdict.APPLY
    if kind == "TRADE":
        return Verdict.APPLY      # on a terminal order it is checked against filled_qty
    if kind.startswith("CANCEL_RESULT"):
        return Verdict.IGNORE if terminal else Verdict.APPLY
    if kind.startswith("ORDER_UPDATE"):
        return Verdict.ILLEGAL if terminal else Verdict.APPLY
    if kind.startswith("MODIFY_RESULT"):
        return Verdict.ILLEGAL if status in BEFORE_OUTCOME else Verdict.APPLY
    return Verdict.ILLEGAL
