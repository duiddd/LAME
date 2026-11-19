"""Runtime settings (environment variables) and the static files D reads at start-up."""
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def _env(name: str, default, cast=str):
    return cast(os.getenv(name, default))


@dataclass
class Settings:
    port: int = _env("D_PORT", 8400, int)
    b_url: str = _env("B_CTRL_URL", "ws://service-b:11659/d")
    a_notify_url: str = _env("A_NOTIFY_URL", "http://service-a:8000/internal/order-events")
    data_dir: str = _env("DATA_DIR", str(HERE / "data"))
    linked_accounts_file: str = _env("LINKED_ACCOUNTS_FILE", str(HERE / "config/linked_accounts.json"))
    symbols_file: str = _env("SYMBOLS_FILE", str(HERE / "config/symbols.json"))
    rules_file: str = _env("RULES_FILE", str(HERE / "rules/v1.json"))
    # B's MAX_COMMAND_AGE is fixed at build time (1000 ms); D_TIMEOUT must not be smaller.
    max_command_age_ms: int = _env("MAX_COMMAND_AGE_MS", 1000, int)
    d_timeout_ms: int = _env("D_TIMEOUT_MS", 1000, int)
    cancel_margin_ms: int = _env("CANCEL_MARGIN_MS", 200, int)
    gate_deadline_ms: int = _env("GATE_DEADLINE_MS", 500, int)
    decision_queue_cap: int = _env("DECISION_QUEUE_CAP", 1000, int)
    # B sends no "resend finished" marker, so D counts itself caught up after this
    # long with a contiguous chain and nothing new arriving (see README, open item).
    catchup_quiet_ms: int = _env("CATCHUP_QUIET_MS", 300, int)
    gap_timeout_ms: int = _env("GAP_TIMEOUT_MS", 3000, int)
    faults_enabled: bool = _env("FAULTS_ENABLED", "false").lower() == "true"

    def __post_init__(self):
        if self.d_timeout_ms < self.max_command_age_ms:
            raise ValueError("D_TIMEOUT_MS must be >= MAX_COMMAND_AGE_MS")


@dataclass
class StaticConfig:
    symbols: dict           # symbol -> {"tick": int, "lot": int}
    groups: dict            # user_id -> frozenset of user_ids in the same linked group
    links_version: str
    rules: dict
    rule_version: str = field(default="")


def load_static(s: Settings) -> StaticConfig:
    # D's own file lists "symbols"; B's pairs.json lists "pairs". D reads only symbol, tick and lot.
    raw_symbols = json.loads(Path(s.symbols_file).read_text())
    symbols = {x["symbol"]: {"tick": x["tick"], "lot": x["lot"]}
               for x in raw_symbols.get("symbols") or raw_symbols["pairs"]}
    links = json.loads(Path(s.linked_accounts_file).read_text())
    groups = {}
    for g in links["groups"]:
        members = frozenset(int(u) for u in g["user_ids"])
        for u in members:
            if u in groups:
                raise ValueError(f"user {u} is in more than one linked group")
            groups[u] = members
    raw = Path(s.rules_file).read_bytes()
    version = f"{Path(s.rules_file).stem}-{hashlib.sha256(raw).hexdigest()[:6]}"
    return StaticConfig(symbols, groups, links["version"], json.loads(raw), version)
