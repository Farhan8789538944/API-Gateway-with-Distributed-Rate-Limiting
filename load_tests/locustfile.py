"""Locust workloads for the gateway benchmarks.

Selected by user class on the command line, configured by environment:
  API_KEY        key sent as X-API-Key (not needed for the direct baseline)
  WAIT_MODE      "none" (closed loop, no think time) or "throughput:<req/s per user>"
  RESULTS_FILE   where the outcome summary JSON is written at test stop

Locust's own CSV mixes failed/rejected latencies into its percentiles. This
file therefore classifies every response and keeps SUCCESSFUL latencies
separately (summary JSON): success, quota (429), capacity (503
capacity_exhausted), other errors.
"""

from __future__ import annotations

import json
import os
import random
import time
from collections import Counter

from locust import HttpUser, between, constant_throughput, events, task

API_KEY = os.environ.get("API_KEY", "")
WAIT_MODE = os.environ.get("WAIT_MODE", "none")
RESULTS_FILE = os.environ.get("RESULTS_FILE", "")
PRODUCT_COUNT = 1000

_success_ms: list[float] = []
_outcomes: Counter = Counter()
_started = time.time()


def _wait():
    if WAIT_MODE.startswith("throughput:"):
        return constant_throughput(float(WAIT_MODE.split(":", 1)[1]))
    return between(0, 0)


def _classify(response) -> str:
    status = response.status_code
    if status == 429:
        return "quota_rejected"
    if status == 503:
        try:
            code = response.json()["error"]["code"]
        except Exception:
            code = ""
        return "capacity_rejected" if code == "capacity_exhausted" else "error_503"
    if 200 <= status < 300:
        return "success"
    return f"error_{status}"


class _Base(HttpUser):
    abstract = True
    wait_time = _wait()

    def on_start(self):
        if API_KEY:
            self.client.headers["X-API-Key"] = API_KEY
        self.orders: list[int] = []

    def _request(self, method, path, name, **kwargs):
        with self.client.request(method, path, name=name, catch_response=True,
                                 **kwargs) as response:
            outcome = _classify(response)
            _outcomes[outcome] += 1
            if outcome == "success":
                _success_ms.append(response.request_meta["response_time"])
                response.success()
            else:
                response.failure(outcome)
            return response


class ProductReadUser(_Base):
    """Product-heavy reads: 90 % single product, 10 % first page."""

    @task(9)
    def product(self):
        self._request("GET", f"/products/{random.randint(1, PRODUCT_COUNT)}", "/products/{id}")

    @task(1)
    def page(self):
        self._request("GET", "/products?limit=20", "/products")


class MixedUser(_Base):
    """Product/order mix: 80 % product reads, 15 % order creation, 5 % order reads."""

    @task(16)
    def product(self):
        self._request("GET", f"/products/{random.randint(1, PRODUCT_COUNT)}", "/products/{id}")

    @task(3)
    def create_order(self):
        body = {"product_id": random.randint(1, PRODUCT_COUNT), "quantity": random.randint(1, 5)}
        r = self._request("POST", "/orders", "/orders", json=body)
        if r.status_code == 201:
            self.orders.append(r.json()["id"])

    @task(1)
    def read_order(self):
        if self.orders:
            self._request("GET", f"/orders/{random.choice(self.orders)}", "/orders/{id}")


@events.test_start.add_listener
def _reset(**_kwargs):
    global _started
    _success_ms.clear()
    _outcomes.clear()
    _started = time.time()


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return round(sorted_values[index], 2)


@events.test_stop.add_listener
def _write_summary(environment, **_kwargs):
    duration = time.time() - _started
    values = sorted(_success_ms)
    total = sum(_outcomes.values())
    summary = {
        "duration_s": round(duration, 1),
        # Configured user count (the live count is already 0 at test_stop).
        "users": getattr(environment.parsed_options, "num_users", None),
        "wait_mode": WAIT_MODE,
        "total_requests": total,
        "achieved_rps_total": round(total / duration, 1) if duration else None,
        "achieved_rps_success": round(len(values) / duration, 1) if duration else None,
        "outcomes": dict(_outcomes),
        "success_latency_ms": {
            "count": len(values),
            "p50": _percentile(values, 0.50),
            "p95": _percentile(values, 0.95),
            "p99": _percentile(values, 0.99),
            "max": round(values[-1], 2) if values else None,
        },
    }
    if RESULTS_FILE:
        os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
        with open(RESULTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
    print(json.dumps(summary))
