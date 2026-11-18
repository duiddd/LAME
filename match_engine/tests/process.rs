//! The real binary: SIGTERM, SIGKILL and restart, the `--healthcheck`
//! subcommand, and refusing to start twice. These use the system clock, so
//! they only count what was recorded, never whether an order was in session.

mod support;

use std::io::{BufRead, BufReader};
use std::net::SocketAddr;
use std::path::Path;
use std::process::{Child, ChildStdout, Command, Stdio};

use match_engine::store::{EventStore, Kind};
use serde_json::Value;
use support::*;

const BIN: &str = env!("CARGO_BIN_EXE_match_engine");

fn command(dir: &Path) -> Command {
    let pairs = dir.join("pairs.json");
    std::fs::write(
        &pairs,
        r#"{"version":1,"pairs":[{"symbol":"AAA","tick":1,"lot":1},{"symbol":"BBB","tick":5,"lot":10}]}"#,
    )
    .unwrap();
    let mut cmd = Command::new(BIN);
    cmd.env("PAIRS_FILE", &pairs)
        .env("EVENT_DB_DIR", dir)
        .env("CTRL_ADDR", "127.0.0.1:0")
        .env("MD_ADDR", "127.0.0.1:0")
        .env("CTRL_ALLOWED_CIDRS", "127.0.0.0/8")
        .stdout(Stdio::piped());
    cmd
}

struct Proc {
    child: Child,
    ctrl: SocketAddr,
    logs: std::io::Lines<BufReader<ChildStdout>>,
}

fn field(line: &str, name: &str) -> Option<String> {
    let v: Value = serde_json::from_str(line).ok()?;
    v["fields"][name].as_str().map(str::to_string)
}

/// Starts the service and waits for its "ready" log line. The child is
/// handed to the caller, who stops and waits for it.
#[allow(clippy::zombie_processes)]
fn spawn(dir: &Path) -> Proc {
    let mut child = command(dir).spawn().expect("starts");
    let mut logs = BufReader::new(child.stdout.take().unwrap()).lines();
    loop {
        let line = logs
            .next()
            .expect("the process exited before it was ready")
            .unwrap();
        if field(&line, "message").as_deref() == Some("ready") {
            let ctrl = field(&line, "ctrl_addr").unwrap().parse().unwrap();
            return Proc { child, ctrl, logs };
        }
    }
}

impl Proc {
    fn signal(&self, name: &str) {
        let status = Command::new("kill")
            .arg(format!("-{name}"))
            .arg(self.child.id().to_string())
            .status()
            .unwrap();
        assert!(status.success());
    }

    /// Waits for the process to exit; returns whether it succeeded and what it
    /// logged on the way out.
    fn finish(mut self) -> (bool, Vec<String>) {
        let rest: Vec<String> = self.logs.by_ref().map_while(Result::ok).collect();
        (self.child.wait().unwrap().success(), rest)
    }
}

/// `(commands, outputs, day closes)` recorded across every day's store.
fn totals(dir: &Path) -> (usize, usize, usize) {
    let (mut commands, mut outputs, mut closes) = (0, 0, 0);
    for entry in std::fs::read_dir(dir).unwrap() {
        let name = entry.unwrap().file_name().into_string().unwrap();
        let Some(day) = name
            .strip_prefix("events-")
            .and_then(|n| n.strip_suffix(".db"))
        else {
            continue;
        };
        let store = EventStore::open(dir, day).unwrap();
        store
            .scan(0, |r| {
                match r.kind {
                    k if k.is_output() => outputs += 1,
                    Kind::DayClose => closes += 1,
                    _ => commands += 1,
                }
                Ok(())
            })
            .unwrap();
    }
    (commands, outputs, closes)
}

/// Sends `n` buy orders that cannot cross, and returns their `n` outputs.
async fn send_orders(d: &mut Client, first_id: u64, n: u64) -> Vec<Value> {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    for id in first_id..first_id + n {
        d.send(limit("AAA", id, id, "BUY", 10, 1, now)).await;
    }
    d.recv_n(n as usize).await
}

#[tokio::test]
async fn sigterm_finishes_what_is_queued_and_exits_cleanly() {
    let dir = tempfile::tempdir().unwrap();
    let proc = spawn(dir.path());
    let mut d = Client::connect(proc.ctrl, 0).await;
    send_orders(&mut d, 1, 20).await;
    drop(d);

    proc.signal("TERM");
    let (ok, logs) = proc.finish();
    assert!(ok, "exit status after SIGTERM");
    assert!(logs
        .iter()
        .any(|l| field(l, "message").as_deref() == Some("stopped")));

    // Every command has its output, and nothing else was lost.
    let (commands, outputs, _) = totals(dir.path());
    assert_eq!((commands, outputs), (20, 20));
}

#[tokio::test]
async fn a_killed_process_comes_back_with_everything_it_had_acknowledged() {
    let dir = tempfile::tempdir().unwrap();
    let mut first = spawn(dir.path());
    let mut d = Client::connect(first.ctrl, 0).await;
    let before = send_orders(&mut d, 1, 10).await;
    drop(d);
    // No chance to clean up: this is `docker kill`.
    first.child.kill().unwrap();
    first.child.wait().unwrap();

    let second = spawn(dir.path());
    let mut d = Client::connect(second.ctrl, 0).await;
    // The same ten records, byte for byte, then the numbering carries on.
    assert_eq!(d.recv_n(10).await, before);
    let more = send_orders(&mut d, 11, 1).await;
    assert_eq!(more[0]["prev_seq"], before[9]["seq"]);
    assert!(more[0]["seq"].as_u64().unwrap() > before[9]["seq"].as_u64().unwrap());
    drop(d);

    second.signal("TERM");
    assert!(second.finish().0);
    let (commands, outputs, _) = totals(dir.path());
    assert_eq!((commands, outputs), (11, 11));
}

#[tokio::test]
async fn the_healthcheck_subcommand_reports_readiness() {
    let dir = tempfile::tempdir().unwrap();
    let proc = spawn(dir.path());
    let check = |addr: SocketAddr| {
        Command::new(BIN)
            .arg("--healthcheck")
            .env("CTRL_ADDR", addr.to_string())
            .status()
            .unwrap()
            .success()
    };
    assert!(check(proc.ctrl));
    let addr = proc.ctrl;
    proc.signal("TERM");
    assert!(proc.finish().0);
    assert!(!check(addr), "nothing is listening any more");
}

#[test]
fn a_second_process_on_the_same_directory_refuses_to_start() {
    let dir = tempfile::tempdir().unwrap();
    let first = spawn(dir.path());

    let mut second = command(dir.path()).spawn().unwrap();
    let logs: Vec<String> = BufReader::new(second.stdout.take().unwrap())
        .lines()
        .map_while(Result::ok)
        .collect();
    assert!(!second.wait().unwrap().success());
    assert!(
        logs.iter()
            .any(|l| field(l, "error").is_some_and(|e| e.contains("another instance"))),
        "{logs:?}"
    );

    first.signal("TERM");
    assert!(first.finish().0);
}

#[test]
fn start_up_without_the_pair_config_file_fails_with_a_reason() {
    let dir = tempfile::tempdir().unwrap();
    let mut cmd = command(dir.path());
    cmd.env("PAIRS_FILE", dir.path().join("missing.json"));
    let mut child = cmd.spawn().unwrap();
    let logs: Vec<String> = BufReader::new(child.stdout.take().unwrap())
        .lines()
        .map_while(Result::ok)
        .collect();
    assert!(!child.wait().unwrap().success());
    assert!(logs
        .iter()
        .any(|l| field(l, "error").is_some_and(|e| e.contains("missing.json"))));
}
