"""Fixtures for tests against the ISOLATED test stack (Compose project apigw-test).

Start it first:   .\\scripts\\test_stack.ps1 up
Everything here targets 127.0.0.1 ports published by compose.test.yaml only.
If the stack is not reachable the tests are reported as SKIPPED (never passed).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import httpx
import pytest

from gateway import admin

ROOT = Path(__file__).resolve().parents[2]
PROJECT = os.environ.get("TEST_COMPOSE_PROJECT", "apigw-test")


def _dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


ENV = {**_dotenv(), **os.environ}
DB = ENV.get("POSTGRES_DB", "apigw")
ADMIN_DSN = (f"postgresql://{ENV.get('POSTGRES_USER', 'apigw_admin')}:"
             f"{ENV.get('POSTGRES_PASSWORD', '')}@127.0.0.1:15432/{DB}")
GATEWAY_DSN = f"postgresql://gateway_app:{ENV.get('GATEWAY_DB_PASSWORD', '')}@127.0.0.1:15432/{DB}"
REDIS_URL = "redis://127.0.0.1:16379/0"
NGINX = "http://127.0.0.1:18080"
GATEWAYS = {"gateway-1": "http://127.0.0.1:18001", "gateway-2": "http://127.0.0.1:18002"}
BACKENDS = {"product-1": "http://127.0.0.1:18081", "product-2": "http://127.0.0.1:18082",
            "orders": "http://127.0.0.1:18083"}
NO_REFILL_MS = 3_600_000  # 1 token per hour: refill is negligible during a test


def _stack_reachable(timeout_s: float = 30.0) -> str | None:
    try:
        httpx.get(f"{NGINX}/nginx-health", timeout=2).raise_for_status()
        for url in [*GATEWAYS.values(), *BACKENDS.values()]:
            httpx.get(f"{url}/health/live", timeout=2).raise_for_status()
    except Exception as exc:
        return f"test stack not reachable ({type(exc).__name__}); run .\\scripts\\test_stack.ps1 up"
    # After gateway containers are recreated, Nginx needs a few seconds to
    # re-resolve their names (resolver valid=5s). Wait until it reaches one.
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if httpx.get(f"{NGINX}/products/1", timeout=2).status_code == 401:
            return None
        time.sleep(0.5)
    return "Nginx cannot reach the gateways (still 502 after 30 s)"


_REASON = _stack_reachable()


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
            if _REASON:
                item.add_marker(pytest.mark.skip(reason=_REASON))


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-p", PROJECT, "-f", str(ROOT / "compose.yaml"),
           "-f", str(ROOT / "compose.test.yaml"), *args]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=check,
                          timeout=180)


async def wait_until(predicate, timeout: float = 30.0, interval: float = 0.25,
                     what: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = await predicate()
            if last:
                return last
        except Exception as exc:
            last = exc
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}; last={last!r}")


def unique(prefix: str = "t") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class ApiClient:
    name: str
    id: int
    key: str
    key_id: str
    policy: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.key}


@pytest.fixture
async def admin_conn():
    conn = await asyncpg.connect(ADMIN_DSN, timeout=5)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def provision(admin_conn):
    """Create a fresh policy + client + key: await provision(capacity=5, ...)."""

    async def _provision(*, algorithm: str = "token_bucket", capacity: int = 1000,
                         refill_tokens: int | None = 1000, refill_period_ms: int | None = 1000,
                         window_ms: int | None = None, **key_kwargs) -> ApiClient:
        policy = unique("p")
        if algorithm == "sliding_window":
            refill_tokens = refill_period_ms = None
        await admin.create_policy(admin_conn, name=policy, algorithm=algorithm,
                                  capacity=capacity, refill_tokens=refill_tokens,
                                  refill_period_ms=refill_period_ms, window_ms=window_ms)
        name = unique("c")
        client_id = await admin.create_client(admin_conn, name=name, policy=policy)
        key = await admin.issue_key(admin_conn, client=name, **key_kwargs)
        return ApiClient(name, client_id, key.raw, key.key_id, policy)

    return _provision


@pytest.fixture
async def http():
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    async with httpx.AsyncClient(timeout=15, limits=limits, trust_env=False) as client:
        yield client
