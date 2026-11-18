"""Prometheus metrics (architecture doc section 13). One registry per engine so tests stay independent."""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self):
        r = self.registry = CollectorRegistry()
        self.decision_seconds = Histogram("lame_d_decision_seconds", "Received to decision written", registry=r,
                                          buckets=(.001, .002, .005, .01, .02, .05, .1, .2, .5, 1))
        self.decisions = Counter("lame_d_decisions_total", "Decisions", ["decision", "reason"], registry=r)
        self.refused = Counter("lame_d_refused_total", "Orders refused with 503", ["reason"], registry=r)
        self.queue = Gauge("lame_d_decision_queue", "Orders waiting for a decision", registry=r)
        self.timed_out = Gauge("lame_d_timed_out_orders", "Orders sent to B with no output yet past D_TIMEOUT", registry=r)
        self.cancels_waiting = Gauge("lame_d_cancels_waiting", "Cancels not yet sent to B", registry=r)
        self.outbox_backlog = Gauge("lame_d_outbox_backlog", "Notifications not yet acknowledged by A", registry=r)
        self.notifications = Counter("lame_d_notifications_total", "Notifications acknowledged by A", registry=r)
        self.b_connected = Gauge("lame_d_b_connected", "1 when connected to B", registry=r)
        self.ready = Gauge("lame_d_ready", "1 when D accepts new orders", registry=r)
        self.feature_lag = Gauge("lame_d_feature_lag_seconds", "Now minus arrival of the last applied B output", registry=r)
