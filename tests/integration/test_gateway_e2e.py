"""End-to-end tests through Nginx and both gateway instances (test stack)."""

import asyncio
from collections import Counter

import httpx
import pytest

from tests.integration.conftest import GATEWAYS, NGINX, NO_REFILL_MS

pytestmark = pytest.mark.anyio
GW1, GW2 = GATEWAYS["gateway-1"], GATEWAYS["gateway-2"]


async def test_authenticated_product_read_and_forwarding(http, provision):
    client = await provision()
    r = await http.get(f"{NGINX}/products/1", headers=client.headers)
    assert r.status_code == 200 and r.json()["sku"] == "SKU-00001"
    assert r.headers["x-gateway-instance"] in GATEWAYS
    assert r.headers["x-upstream-instance"] in ("product-1", "product-2")
    assert r.headers["x-instance-id"] == r.headers["x-upstream-instance"]
    r = await http.get(f"{NGINX}/products", params={"limit": 3, "after_id": 10},
                       headers=client.headers)
    assert [p["id"] for p in r.json()["items"]] == [11, 12, 13]
    # A legitimate backend 404 is preserved (not converted by the gateway).
    r = await http.get(f"{NGINX}/products/5000", headers=client.headers)
    assert r.status_code == 404 and r.json()["error"]["code"] == "product_not_found"
    r = await http.get(f"{NGINX}/products/abc", headers=client.headers)
    assert r.status_code == 422


async def test_head_request(http, provision):
    client = await provision()
    get = await http.get(f"{NGINX}/products/2", headers=client.headers)
    head = await http.head(f"{NGINX}/products/2", headers=client.headers)
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == get.headers["content-length"]


async def test_gateway_routes_and_unknown_paths(http, provision):
    client = await provision()
    for path in ("/products-other", "/product", "/", "/admin", "/_dev/faults",
                 "/orders2/1"):
        r = await http.get(f"{NGINX}{path}", headers=client.headers)
        assert r.status_code == 404, path
        assert r.json()["error"]["code"] == "route_not_found"
    # Gateway health is not public through Nginx.
    assert (await http.get(f"{NGINX}/health/ready")).status_code == 404
    # Development controls are unreachable through every public route shape.
    for path in ("/products/%2e%2e/_dev/faults", "/products%2f..%2f_dev/faults",
                 "/products/..%5c_dev/faults"):
        r = await http.put(f"{NGINX}{path}", json={"delay_ms": 60000}, headers=client.headers)
        assert r.status_code in (400, 404), path
    assert (await http.get(f"{GW1}/_dev/faults", headers=client.headers)).status_code == 404


@pytest.mark.parametrize("headers", [
    {}, {"X-API-Key": "garbage"},
    {"X-API-Key": "gwk_0123456789abcdef_" + "A" * 43},
])
async def test_invalid_credentials_rejected(http, headers):
    r = await http.get(f"{NGINX}/products/1", headers=headers)
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


async def test_revoked_and_inactive_end_to_end(http, provision, admin_conn):
    from gateway import admin
    client = await provision()
    assert (await http.get(f"{NGINX}/products/1", headers=client.headers)).status_code == 200
    await admin.revoke_key(admin_conn, key_id=client.key_id)
    assert (await http.get(f"{NGINX}/products/1", headers=client.headers)).status_code == 401
    other = await provision()
    await admin.set_client_active(admin_conn, client=other.name, active=False)
    assert (await http.get(f"{NGINX}/products/1", headers=other.headers)).status_code == 401


async def test_orders_end_to_end_with_ownership_and_spoofing(http, provision):
    alice, bob = await provision(), await provision()
    spoofed = {**alice.headers, "X-Client-Id": str(bob.id), "X-Client-Name": bob.name}
    r = await http.post(f"{NGINX}/orders", json={"product_id": 5, "quantity": 4},
                        headers=spoofed)
    assert r.status_code == 201
    order = r.json()
    assert order["client_id"] == alice.id  # spoofed identity header was overwritten
    url = f"{NGINX}/orders/{order['id']}"
    assert (await http.get(url, headers=alice.headers)).json() == order
    assert (await http.get(url, headers=bob.headers)).status_code == 404
    # Bob cannot read Alice's order by spoofing her id either.
    spoof_read = {**bob.headers, "X-Client-Id": str(alice.id)}
    assert (await http.get(url, headers=spoof_read)).status_code == 404
    bad = await http.post(f"{NGINX}/orders", headers=alice.headers,
                          json={"product_id": 5, "quantity": 1, "unit_price_cents": 1})
    assert bad.status_code == 422


async def test_nginx_balances_gateways_and_gateways_balance_products(http, provision):
    client = await provision()
    via_nginx = Counter()
    for _ in range(20):
        r = await http.get(f"{NGINX}/products/3", headers=client.headers)
        via_nginx[r.headers["x-gateway-instance"]] += 1
    assert set(via_nginx) == {"gateway-1", "gateway-2"}
    for gateway in (GW1, GW2):
        backends = Counter()
        for _ in range(10):
            r = await http.get(f"{gateway}/products/3", headers=client.headers)
            backends[r.headers["x-upstream-instance"]] += 1
        assert backends == {"product-1": 5, "product-2": 5}  # round robin per gateway


async def test_shared_token_bucket_across_both_gateways(http, provision):
    capacity = 20
    client = await provision(capacity=capacity, refill_tokens=1, refill_period_ms=NO_REFILL_MS)
    urls = [f"{GW1 if i % 2 else GW2}/products/1" for i in range(60)]
    responses = await asyncio.gather(*(http.get(u, headers=client.headers) for u in urls))
    allowed = [r for r in responses if r.status_code == 200]
    rejected = [r for r in responses if r.status_code == 429]
    assert len(allowed) + len(rejected) == 60
    # Refill is 1 token/hour, so at most one extra token can appear.
    assert capacity <= len(allowed) <= capacity + 1
    assert {r.headers["x-gateway-instance"] for r in allowed} == {"gateway-1", "gateway-2"}
    assert all(int(r.headers["retry-after"]) >= 1 for r in rejected)
    assert all(r.json()["error"]["code"] == "rate_limited" for r in rejected)


async def test_shared_sliding_window_through_nginx(http, provision):
    client = await provision(algorithm="sliding_window", capacity=15, window_ms=NO_REFILL_MS)
    responses = await asyncio.gather(*(http.get(f"{NGINX}/products/1", headers=client.headers)
                                       for _ in range(40)))
    statuses = Counter(r.status_code for r in responses)
    assert statuses == {200: 15, 429: 25}
    retry = {int(r.headers["retry-after"]) for r in responses if r.status_code == 429}
    assert all(3500 <= v <= 3600 for v in retry)  # next expiry is ~1 hour away


async def test_quota_consumed_even_when_backend_rejects(http, provision):
    client = await provision(capacity=3, refill_tokens=1, refill_period_ms=NO_REFILL_MS)
    for _ in range(3):  # backend 404s still consume allowance
        assert (await http.get(f"{NGINX}/products/9999", headers=client.headers))\
            .status_code == 404
    assert (await http.get(f"{NGINX}/products/1", headers=client.headers)).status_code == 429


async def test_body_limits_nginx_and_gateway(http, provision):
    client = await provision()
    big = b"x" * (100 * 1024)

    async def chunked():
        for i in range(0, len(big), 8192):
            yield big[i:i + 8192]

    r = await http.post(f"{NGINX}/orders", content=big, headers=client.headers)
    assert r.status_code == 413  # Nginx client_max_body_size (declared length)
    r = await http.post(f"{NGINX}/orders", content=chunked(), headers=client.headers)
    assert r.status_code == 413  # Nginx also bounds chunked bodies
    # Directly at the gateway (no Nginx): gateway's own streaming limit.
    r = await http.post(f"{GW1}/orders", content=chunked(), headers=client.headers)
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    r = await http.post(f"{GW1}/orders", content=big, headers=client.headers)
    assert r.status_code == 413


async def test_request_id_propagation(http, provision):
    client = await provision()
    r = await http.get(f"{NGINX}/products/1", headers={**client.headers,
                                                       "X-Request-ID": "e2e-trace-1"})
    assert r.headers["x-request-id"] == "e2e-trace-1"
    r = await http.get(f"{NGINX}/products/1", headers=client.headers)
    assert len(r.headers["x-request-id"]) == 32  # generated by Nginx


async def test_metrics_exposed_on_internal_port_only(http, provision):
    client = await provision()
    await http.get(f"{GW1}/products/1", headers=client.headers)
    text = (await http.get("http://127.0.0.1:19101/metrics")).text
    assert "gateway_requests_total" in text and "gateway_redis_decision_seconds" in text
    assert client.key not in text and client.key_id not in text
    assert f'"{client.id}"' not in text and client.name not in text  # no client labels
    assert (await http.get(f"{NGINX}/metrics", headers=client.headers)).status_code == 404


async def test_backend_error_status_preserved(http, provision):
    client = await provision()
    async with httpx.AsyncClient() as dev:
        from tests.integration.conftest import BACKENDS
        try:
            for name in ("product-1", "product-2"):
                await dev.put(f"{BACKENDS[name]}/_dev/faults", json={"error_status": 503})
            r = await http.get(f"{GW1}/products/1", headers=client.headers)
            assert r.status_code == 503 and r.json()["error"]["code"] == "injected_fault"
        finally:
            for name in ("product-1", "product-2"):
                await dev.delete(f"{BACKENDS[name]}/_dev/faults")
