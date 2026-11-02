//! Regression against the reference datasets: event and trade counts must match
//! `manifest.json`. The CSVs are too large to commit, so point
//! `LAME_DATASET_DIR` at the directory holding them; without it these tests
//! are skipped.

mod common;

use std::fs::File;
use std::io::{BufRead, BufReader};
use std::time::Instant;

use common::{apply_line, Harness, LineEvent};

fn dataset_dir() -> Option<std::path::PathBuf> {
    std::env::var_os("LAME_DATASET_DIR").map(Into::into)
}

#[test]
fn datasets_match_manifest() {
    let Some(dir) = dataset_dir() else {
        eprintln!("LAME_DATASET_DIR not set; skipping");
        return;
    };
    let manifest: serde_json::Value =
        serde_json::from_reader(File::open(dir.join("manifest.json")).unwrap()).unwrap();
    for ds in manifest["datasets"].as_array().unwrap() {
        let file = ds["file"].as_str().unwrap();
        let want_events = ds["expected_output_events"].as_u64().unwrap();
        let want_trades = ds["expected_trades"].as_u64().unwrap();

        let reader = BufReader::new(File::open(dir.join(file)).unwrap());
        let mut engine = Harness::new();
        let (mut events, mut trades, mut rejected) = (0u64, 0u64, 0u64);
        let started = Instant::now();
        for line in reader.lines() {
            if !apply_line(&mut engine, &line.unwrap()) {
                rejected += 1;
            }
            for e in engine.drain_lines() {
                events += 1;
                trades += matches!(e, LineEvent::Trade { .. }) as u64;
            }
        }
        eprintln!(
            "{file}: {events} events, {trades} trades in {:?}",
            started.elapsed()
        );
        assert_eq!(rejected, 0, "{file}: rejected requests");
        assert_eq!(events, want_events, "{file}: events");
        assert_eq!(trades, want_trades, "{file}: trades");
    }
}
