# Contribution statement — Pin-Yun Kuo

**Services owned:** Service A (Trading & Accounts) and the frontend application shell with the
trading page (the News & Analyst page and the chat box inside it are Service C's).

**Within A:** registration and login (Argon2id, short-lived JWT with rotating refresh tokens),
balances, holds and the ledger, simulated deposits, order placement with rate, size and price
checks, the D client with failure classification, circuit breaker and load shedding, applying D's
notifications effectively once, reconciliation of unanswered orders, candles and the market
summary, the per-user event stream, metrics, and A's container and compose file.

**Decisions owned:** ADR-002 (funds held until D's notification settles the order); A's technical
decisions in `docs/service_a_architecture.md` section 8, including sending a market buy as a capped
limit order and limiting a modify to the remaining quantity.

**Integration and testing:** the contract A requires from D's notifications (A architecture
document §3.1), checked against D's and B's code; `run_local.ps1`, which runs all five parts
locally from each owner's branch; `local_e2e.py` against the real D and B; A's 210 tests;
the failure and load measurements in the report.

**Report sections:** A's part of the consistency and resilience sections, and the hold-release and
modify trade-offs.
