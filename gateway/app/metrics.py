"""Prometheus metrics with strictly bounded label values.

Labels only ever take values from fixed sets: route templates from config,
outcome names from errors.OUTCOMES, status classes, algorithm names and the
configured backend instance names. Client ids, request ids and raw paths are
never used as labels.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1, 2, 5, 10)
REDIS_BUCKETS = (0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1)


def status_class(status: int) -> str:
    return f"{status // 100}xx" if 100 <= status <= 599 else "other"


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.requests = Counter(
            "gateway_requests_total", "Completed requests by bounded route, status class, outcome",
            ["route", "status_class", "outcome"], registry=r)
        self.success_latency = Histogram(
            "gateway_success_latency_seconds",
            "Total gateway latency of successfully relayed responses (status < 500)",
            ["route"], buckets=LATENCY_BUCKETS, registry=r)
        self.upstream_latency = Histogram(
            "gateway_upstream_latency_seconds",
            "Upstream exchange duration for relayed responses", ["service"],
            buckets=LATENCY_BUCKETS, registry=r)
        self.rate_limit_decisions = Counter(
            "gateway_rate_limit_decisions_total", "Quota decisions",
            ["algorithm", "result"], registry=r)
        self.redis_decision_latency = Histogram(
            "gateway_redis_decision_seconds", "Latency of one Redis quota decision (script call)",
            ["algorithm"], buckets=REDIS_BUCKETS, registry=r)
        self.capacity_rejections = Counter(
            "gateway_capacity_rejections_total", "Requests rejected by admission control",
            ["scope"], registry=r)
        self.dependency_failures = Counter(
            "gateway_dependency_failures_total", "Failures of required dependencies",
            ["dependency"], registry=r)
        self.upstream_timeouts = Counter(
            "gateway_upstream_timeouts_total", "Upstream deadline/read timeouts",
            ["service"], registry=r)
        self.in_flight = Gauge(
            "gateway_in_flight_requests", "Requests holding a global admission slot",
            registry=r)
        self.upstream_in_flight = Gauge(
            "gateway_upstream_in_flight_requests", "Requests holding a service slot",
            ["service"], registry=r)
        self.backend_healthy = Gauge(
            "gateway_backend_healthy", "1 if the instance is selectable",
            ["service", "instance"], registry=r)
