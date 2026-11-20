"""Prometheus metrics (architecture document, section 12). One registry per container."""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self):
        r = self.registry = CollectorRegistry()
        self.orders = Counter("lame_a_orders_total", "Orders by outcome", ["result"], registry=r)
        self.http = Histogram("lame_a_http_request_duration_seconds", "API latency; status gives the error rate", ["endpoint", "method", "status"], registry=r)
        self.d_ack = Histogram("lame_a_d_ack_latency_seconds", "Latency of D's acknowledgement", registry=r)
        self.completion = Histogram("lame_a_order_completion_seconds", "Placement to first outcome", registry=r)
        self.circuit = Gauge("lame_a_circuit_state", "0 closed, 1 half open, 2 open", registry=r)
        self.shedding = Gauge("lame_a_shedding_active", "1 while shedding", registry=r)
        self.by_status = Gauge("lame_a_orders_by_status", "Open orders by status", ["status"], registry=r)
        self.lag = Histogram("lame_a_notification_lag_seconds", "D generating a notification to settlement", registry=r)
        self.gaps = Counter("lame_a_notification_gaps_total", "Gaps answered with 409", registry=r)
        self.duplicates = Counter("lame_a_duplicates_ignored_total", "Duplicates by layer", ["layer"], registry=r)
        self.auth_failures = Counter("lame_a_auth_failures_total", "Failed logins", registry=r)
        self.notify_failures = Counter("lame_a_notify_failures_total", "Notifications refused with 422", ["reason"], registry=r)
        self.cursor_age = Gauge("lame_a_notify_cursor_age_seconds", "Time since the cursor last advanced", registry=r)
        self.unsettled = Gauge("lame_a_orders_unsettled", "Terminal orders not yet closed", registry=r)
        self.restarts = Counter("lame_a_bg_task_restarts_total", "Background task restarts", ["task"], registry=r)
        self.md_age = Gauge("lame_a_md_age_seconds", "Time since any message from B", ["symbol"], registry=r)
        self.md_bad = Counter("lame_a_md_bad_messages_total", "Malformed market-data messages skipped", ["symbol"], registry=r)
