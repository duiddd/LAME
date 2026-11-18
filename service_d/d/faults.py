"""Fault-injection switches (RD21). The admin endpoint exists only when FAULTS_ENABLED=true."""
from dataclasses import dataclass


@dataclass
class Faults:
    decision_delay_ms: int = 0          # extra delay before each decision ("D slow")
    pause_b_apply: bool = False         # stop applying B's outputs ("stale features")
    disconnect_after_send: bool = False # drop the B connection right after a new_order
    skip_next_notification: bool = False  # lose one notify_seq ("dropped event")

    def take(self, name: str) -> bool:
        """Read a one-shot switch and turn it off."""
        on = getattr(self, name)
        setattr(self, name, False)
        return on
