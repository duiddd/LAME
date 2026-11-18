# match_engine docs

Service-specific documentation for the `match_engine` crate, Service B: the
matching core and the service around it. System-level design and decision records live in
[`../../docs/`](../../docs/) (`service_b_architecture.md`,
`service_b_tech_decisions.md`); these files describe what the crate itself
does today.

| File | Contents |
| --- | --- |
| [engine.md](engine.md) | Crate layout, data structures, command and output contract, matching rules, worked examples |
| [runtime.md](runtime.md) | The running service: components, channels, wire protocol, event store, recovery, market data, control port, shutdown, configuration |
| [testing.md](testing.md) | Test suites, how to run them, dataset regression |
| [status.md](status.md) | What is implemented, what is not yet, open points |

## Quick start

```bash
cargo build
cargo test
cargo clippy --all-targets -- -D warnings
```

The toolchain is pinned in `rust-toolchain.toml` (Rust 1.90.0, with rustfmt
and clippy).

In a container (see [runtime.md](runtime.md#container)):

```bash
docker compose -f compose.yaml up --build
```
