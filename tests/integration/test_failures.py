"""Failure, overload and recovery tests (DISRUPTIVE, isolated test stack only).

Each test stops/slows containers of the apigw-test project only and restores
them in `finally`. Run with:  pytest -m disruptive
"""

import asyncio
import contextlib
import time
from collections import Counter

import asyncpg
import httpx
import pytest

from gateway import admin
from tests.integration.conftest import (
    ADMIN_DSN,
    BACKENDS,
    GATEWAYS,
    NGINX,
    NO_REFILL_MS,
    ApiClient,
    compose,
    unique,
    wait_until,
)

pytestmark = [pytest.mark.anyio, pytest.mark.disruptive]
GW1 = GATEWAYS["gateway-1"]


@pytest.fixture(autouse=True)
async def restore_stack(http):
    """After every disruptive test, wait until the stack is fully back:
    Nginx reaches both gateways and each gateway rotates over both products."""
    yield
    await clear_faults(http)
    # Fresh connection: the test may have restarted PostgreSQL.
    conn = await wait_until(lambda: asyncpg.connect(ADMIN_DSN, timeout=5), timeout=60,
                            what="postgres reachable")
    try:
        name = unique("restore")
        await admin.create_client(conn, name=name, policy="benchmark-unlimited")
        probe = ApiClient(name, 0, (await admin.issue_key(conn, client=name)).raw, "", "")
    finally:
        await conn.close()

    async def settled():
        gateways, products = set(), set()
        for _ in range(12):
            r = await http.get(f"{NGINX}/products/1", headers=probe.headers)
            if r.status_code != 200:
                return False
            gateways.add(r.headers["x-gateway-instance"])
            products.add(r.headers["x-upstream-instance"])
        return gateways == set(GATEWAYS) and products == {"product-1", "product-2"}

    await wait_until(settled, timeout=90, interval=1, what="stack fully restored")


async def set_faults(http, names, **faults):
    for name in names:
        r = await http.put(f"{BACKENDS[name]}/_dev/faults", json=faults)
        r.raise_for_status()


async def clear_faults(http):
    for name in BACKENDS:
        with contextlib.suppress(httpx.HTTPError):
            await http.delete(f"{BACKENDS[name]}/_dev/faults")


async def wait_all_ok(http, client, url=f"{NGINX}/products/1", n=4, timeout=60):
    async def ok():
        rs = [await http.get(url, headers=client.headers) for _ in range(n)]
        return all(r.status_code == 200 for r in rs)
    await wait_until(ok, timeout=timeout, what=f"{url} healthy again")


async def wait_backend_in_rotation(http, client, name, timeout=30):
    async def seen():
        rs = [await http.get(f"{GW1}/products/1", headers=client.headers) for _ in range(6)]
        return any(r.headers.get("x-upstream-instance") == name for r in rs)
    await wait_until(seen, timeout=timeout, what=f"{name} back in rotation")


async def test_total_upstream_deadline_returns_504(http, provision):
    client = await provision()
    try:
        await set_faults(http, ["product-1", "product-2"], delay_ms=8000)
        started = time.monotonic()
        r = await http.get(f"{GW1}/products/1", headers=client.headers)
        elapsed = time.monotonic() - started
        assert r.status_code == 504 and r.json()["error"]["code"] == "gateway_timeout"
        assert 4.5 <= elapsed <= 7.0  # UPSTREAM_DEADLINE_S=5 (+ overhead)
    finally:
        await clear_faults(http)


async def test_slow_backend_overload_rejects_immediately(http, provision):
    client = await provision(capacity=10_000, refill_tokens=10_000, refill_period_ms=1000)
    try:
        await set_faults(http, ["product-1", "product-2"], delay_ms=2000)
        timings = []

        async def one():
            started = time.monotonic()
            r = await http.get(f"{GW1}/products/1", headers=client.headers)
            timings.append((r.status_code, time.monotonic() - started,
                            r.json()["error"]["code"] if r.status_code == 503 else None))

        await asyncio.gather(*(one() for _ in range(80)))
        statuses = Counter(s for s, _, _ in timings)
        # PRODUCTS_MAX_IN_FLIGHT=50 per gateway process: the excess is shed.
        assert statuses[200] == 50, statuses
        assert statuses[503] == 30, statuses
        rejected = [t for s, t, code in timings if s == 503]
        assert all(code == "capacity_exhausted" for s, _, code in timings if s == 503)
        assert max(rejected) < 1.5  # rejected without waiting for the slow backend
    finally:
        await clear_faults(http)


async def test_readiness_failure_ejects_and_recovers_backend(http, provision):
    client = await provision()
    try:
        await set_faults(http, ["product-1"], fail_ready=True)

        async def only_product_2():
            rs = [await http.get(f"{GW1}/products/1", headers=client.headers) for _ in range(6)]
            return all(r.headers["x-upstream-instance"] == "product-2" for r in rs)

        await wait_until(only_product_2, timeout=10, what="product-1 ejected")
    finally:
        await clear_faults(http)
    await wait_backend_in_rotation(http, client, "product-1")


async def test_backend_container_loss_and_recovery(http, provision):
    client = await provision()
    try:
        compose("stop", "product-1")
        rs = [await http.get(f"{NGINX}/products/1", headers=client.headers)
              for _ in range(30)]
        # GETs that hit the dead instance fail to connect and are retried once
        # on the healthy instance, so clients see no errors.
        assert Counter(r.status_code for r in rs) == {200: 30}
        assert {r.headers["x-upstream-instance"] for r in rs} == {"product-2"}
        # POSTs are never retried; with product-1 gone they go to the orders
        # service anyway (separate pool), so they must still work.
        r = await http.post(f"{NGINX}/orders", json={"product_id": 1, "quantity": 1},
                            headers=client.headers)
        assert r.status_code == 201, (r.text, dict(r.headers))
    finally:
        compose("start", "product-1")
    await wait_backend_in_rotation(http, client, "product-1", timeout=60)


async def test_orders_backend_down_gives_502_or_503_without_replay(http, provision):
    client = await provision()
    try:
        compose("stop", "orders")
        r = await http.post(f"{GW1}/orders", json={"product_id": 1, "quantity": 1},
                            headers=client.headers)
        assert r.status_code in (502, 503)
    finally:
        compose("start", "orders")

    async def orders_ok():
        r = await http.post(f"{GW1}/orders", json={"product_id": 1, "quantity": 1},
                            headers=client.headers)
        return r.status_code == 201
    await wait_until(orders_ok, timeout=60, what="orders recovered")


async def test_redis_outage_fails_closed_and_recovers(http, provision):
    client = await provision()
    try:
        compose("stop", "redis")
        r = await http.get(f"{NGINX}/products/1", headers=client.headers)
        assert r.status_code == 503 and r.json()["error"]["code"] == "dependency_unavailable"
        assert r.headers["retry-after"] == "1"
    finally:
        compose("start", "redis")
    await wait_all_ok(http, client)


async def test_postgres_outage_fails_closed_and_recovers(http, provision):
    client = await provision()
    try:
        compose("stop", "postgres")
        r = await http.get(f"{NGINX}/products/1", headers=client.headers)
        assert r.status_code == 503
        assert r.json()["error"]["code"] in ("dependency_unavailable", "no_healthy_backend")
    finally:
        compose("start", "postgres")
    await wait_all_ok(http, client, timeout=90)


async def test_gateway_instance_loss_behind_nginx(http, provision):
    client = await provision()
    try:
        compose("stop", "gateway-1")
        rs = [await http.get(f"{NGINX}/products/1", headers=client.headers)
              for _ in range(30)]
        statuses = Counter(r.status_code for r in rs)
        # proxy_next_upstream is off (no replay), so attempts that Nginx sends
        # to the dead gateway fail until max_fails marks it down.
        assert statuses[200] >= 25, statuses
        assert set(statuses) <= {200, 502, 504}, statuses
        ok_from = {r.headers["x-gateway-instance"] for r in rs if r.status_code == 200}
        assert ok_from == {"gateway-2"}
    finally:
        compose("start", "gateway-1")

    async def both_gateways():
        rs = [await http.get(f"{NGINX}/products/1", headers=client.headers) for _ in range(10)]
        return {r.headers.get("x-gateway-instance") for r in rs} == set(GATEWAYS)
    await wait_until(both_gateways, timeout=60, what="gateway-1 back behind nginx")


async def test_quota_survives_gateway_and_redis_restarts(http, provision):
    client = await provision(capacity=5, refill_tokens=1, refill_period_ms=NO_REFILL_MS)
    for _ in range(5):
        assert (await http.get(f"{NGINX}/products/1", headers=client.headers)).status_code == 200
    assert (await http.get(f"{NGINX}/products/1", headers=client.headers)).status_code == 429
    compose("restart", "gateway-1", "gateway-2")
    await wait_until(lambda: _status(http, f"{NGINX}/products/1", client) , timeout=60,
                     what="gateways restarted")
    assert (await http.get(f"{NGINX}/products/1", headers=client.headers)).status_code == 429
    await asyncio.sleep(1.5)  # appendfsync everysec: let the AOF reach disk
    compose("restart", "redis")
    await wait_until(lambda: _status(http, f"{NGINX}/products/1", client), timeout=60,
                     what="redis restarted")
    assert (await http.get(f"{NGINX}/products/1", headers=client.headers)).status_code == 429


async def _status(http, url, client):
    r = await http.get(url, headers=client.headers)
    return r.status_code in (200, 429)
