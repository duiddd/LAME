//! Service B: matching and market data.
//!
//! `match_engine` runs the service. `match_engine --healthcheck` asks a running
//! instance on this machine whether it is ready and exits 0 or 1; the image has
//! no `curl` (D23).

use std::process::ExitCode;
use std::time::Duration;

use anyhow::Result;
use match_engine::config::Config;
use match_engine::runtime::system_clock;
use match_engine::{healthcheck, service};
use tokio::signal::unix::{signal, SignalKind};
use tracing_subscriber::EnvFilter;

/// A stop that has not finished by now is abandoned: the guarantee of D12
/// holds for a kill as well, and the container's own grace period is longer.
const STOP_LIMIT: Duration = Duration::from_secs(8);

fn main() -> ExitCode {
    if std::env::args().any(|arg| arg == "--healthcheck") {
        return if healthcheck::check(healthcheck::local_target()) {
            ExitCode::SUCCESS
        } else {
            ExitCode::FAILURE
        };
    }

    // Run-time logs only, as JSON on stdout (D9). The event store never goes
    // through `tracing`.
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("cannot start the runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    match runtime.block_on(run()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!(error = %format!("{e:#}"), "stopping");
            ExitCode::FAILURE
        }
    }
}

async fn run() -> Result<()> {
    let config = Config::from_env()?;
    let mut svc = service::start(config, system_clock()).await?;
    tracing::info!(
        ctrl_addr = %svc.ctrl_addr,
        md_addr = %svc.md_addr,
        "ready"
    );

    let mut terminate = signal(SignalKind::terminate())?;
    let mut interrupt = signal(SignalKind::interrupt())?;
    tokio::select! {
        _ = terminate.recv() => tracing::info!("SIGTERM: stopping"),
        _ = interrupt.recv() => tracing::info!("SIGINT: stopping"),
        // The journal stops by itself only when it cannot write: B must not
        // keep taking orders without a record (D9).
        result = svc.journal_stopped() => {
            return result.and(Err(anyhow::anyhow!("the journal stopped unexpectedly")));
        }
    }

    // Stop taking commands, let the engines and the journal finish what they
    // already hold, then exit. Nothing here is needed for correctness: the
    // guarantees of D12 hold after a kill as well.
    match tokio::time::timeout(STOP_LIMIT, svc.shutdown()).await {
        Ok(result) => result?,
        Err(_) => tracing::warn!("stop took too long; exiting anyway"),
    }
    tracing::info!("stopped");
    Ok(())
}
