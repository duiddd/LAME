//! Prometheus metrics (D23). No per-order timing is taken anywhere on the
//! matching path, so there is no matching-latency histogram.

use prometheus::{
    Encoder, Gauge, IntCounter, IntCounterVec, IntGauge, Opts, Registry, TextEncoder,
};

pub struct Metrics {
    registry: Registry,
    /// Commands accepted from D and handed to the journal, by symbol.
    pub commands_total: IntCounterVec,
    /// Frames from D that were refused before reaching the journal.
    pub bad_frames_total: IntCounter,
    /// Output records written, by kind.
    pub outputs_total: IntCounterVec,
    /// Messages waiting for the journal thread.
    pub journal_backlog: IntGauge,
    /// Outputs queued for the current D connection.
    pub outbound_backlog: IntGauge,
    /// Times a D connection was closed because its queue was full.
    pub outbound_overflows_total: IntCounter,
    pub replay_duration_seconds: Gauge,
    pub replay_records: IntGauge,
    pub md_subscribers: IntGauge,
    /// 1 once the journal thread has stopped on a write error.
    pub journal_failed: IntGauge,
}

impl Metrics {
    pub fn new() -> Self {
        let registry = Registry::new();
        let counter_vec = |name: &str, help: &str, label: &str| {
            let c = IntCounterVec::new(Opts::new(name, help), &[label]).expect("valid metric");
            registry
                .register(Box::new(c.clone()))
                .expect("unique metric");
            c
        };
        let counter = |name: &str, help: &str| {
            let c = IntCounter::new(name, help).expect("valid metric");
            registry
                .register(Box::new(c.clone()))
                .expect("unique metric");
            c
        };
        let int_gauge = |name: &str, help: &str| {
            let g = IntGauge::new(name, help).expect("valid metric");
            registry
                .register(Box::new(g.clone()))
                .expect("unique metric");
            g
        };
        let gauge = |name: &str, help: &str| {
            let g = Gauge::new(name, help).expect("valid metric");
            registry
                .register(Box::new(g.clone()))
                .expect("unique metric");
            g
        };
        Self {
            commands_total: counter_vec("lame_b_commands_total", "Commands accepted", "symbol"),
            bad_frames_total: counter(
                "lame_b_bad_frames_total",
                "Frames refused before the journal",
            ),
            outputs_total: counter_vec("lame_b_outputs_total", "Output records written", "kind"),
            journal_backlog: int_gauge(
                "lame_b_journal_backlog",
                "Messages waiting for the journal",
            ),
            outbound_backlog: int_gauge("lame_b_outbound_backlog", "Outputs queued for D"),
            outbound_overflows_total: counter(
                "lame_b_outbound_overflows_total",
                "D connections closed because the queue was full",
            ),
            replay_duration_seconds: gauge(
                "lame_b_replay_duration_seconds",
                "Duration of the last replay",
            ),
            replay_records: int_gauge("lame_b_replay_records", "Records replayed at start-up"),
            md_subscribers: int_gauge("lame_b_md_subscribers", "Market-data subscribers"),
            journal_failed: int_gauge(
                "lame_b_journal_failed",
                "1 if the journal stopped on an error",
            ),
            registry,
        }
    }

    /// Prometheus text exposition.
    pub fn render(&self) -> String {
        let mut buf = Vec::new();
        TextEncoder::new()
            .encode(&self.registry.gather(), &mut buf)
            .expect("text encoding does not fail");
        String::from_utf8(buf).expect("prometheus text is utf-8")
    }
}

impl Default for Metrics {
    fn default() -> Self {
        Self::new()
    }
}
