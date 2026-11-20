"""Direct tests of the product and order services (test-stack ports only)."""

import pytest

from tests.integration.conftest import BACKENDS

pytestmark = pytest.mark.anyio
P1, P2, ORDERS = BACKENDS["product-1"], BACKENDS["product-2"], BACKENDS["orders"]


async def test_product_pagination_is_bounded_and_keyset(http):
    r = await http.get(f"{P1}/products", params={"limit": 5})
    data = r.json()
    assert r.status_code == 200 and [p["id"] for p in data["items"]] == [1, 2, 3, 4, 5]
    assert data["next_after_id"] == 5
    r = await http.get(f"{P1}/products", params={"limit": 5, "after_id": 5})
    assert [p["id"] for p in r.json()["items"]] == [6, 7, 8, 9, 10]
    r = await http.get(f"{P1}/products", params={"limit": 100, "after_id": 950})
    assert len(r.json()["items"]) == 50 and r.json()["next_after_id"] is None
    for params in ({"limit": 0}, {"limit": 101}, {"after_id": -1}, {"limit": "x"}):
        assert (await http.get(f"{P1}/products", params=params)).status_code == 422


async def test_both_product_instances_serve_the_same_catalog(http):
    a = await http.get(f"{P1}/products/42")
    b = await http.get(f"{P2}/products/42")
    assert a.json() == b.json()
    assert a.headers["x-instance-id"] == "product-1" and b.headers["x-instance-id"] == "product-2"


async def test_product_not_found_and_invalid_ids(http):
    assert (await http.get(f"{P1}/products/1001")).status_code == 404
    assert (await http.get(f"{P1}/products/0")).status_code == 422
    assert (await http.get(f"{P1}/products/abc")).status_code == 422


async def test_health_endpoints(http):
    for base in BACKENDS.values():
        assert (await http.get(f"{base}/health/live")).json() == {"status": "ok"}
        assert (await http.get(f"{base}/health/ready")).json() == {"status": "ready"}


@pytest.mark.parametrize("body,status", [
    ({"product_id": 1, "quantity": 0}, 422),
    ({"product_id": 1, "quantity": -1}, 422),
    ({"product_id": 1, "quantity": 1001}, 422),
    ({"product_id": 1, "quantity": 1.5}, 422),
    ({"product_id": 1, "quantity": "2"}, 422),
    ({"product_id": 1, "quantity": True}, 422),
    ({"product_id": 1}, 422),
    ({"product_id": 1, "quantity": 1, "unit_price_cents": 1}, 422),
    ({"product_id": 1, "quantity": 1, "total_cents": 1}, 422),
    ({"product_id": 999999, "quantity": 1}, 422),
])
async def test_order_validation(http, body, status):
    r = await http.post(f"{ORDERS}/orders", json=body, headers={"X-Client-Id": "5"})
    assert r.status_code == status


async def test_order_price_snapshot_comes_from_catalog(http):
    product = (await http.get(f"{P1}/products/17")).json()
    r = await http.post(f"{ORDERS}/orders", json={"product_id": 17, "quantity": 3},
                        headers={"X-Client-Id": "77"})
    order = r.json()
    assert r.status_code == 201 and r.headers["location"] == f"/orders/{order['id']}"
    assert order["unit_price_cents"] == product["price_cents"]
    assert order["total_cents"] == 3 * product["price_cents"]
    assert order["currency"] == product["currency"] and order["client_id"] == 77


async def test_order_ownership_and_identity_required(http):
    created = (await http.post(f"{ORDERS}/orders", json={"product_id": 2, "quantity": 1},
                               headers={"X-Client-Id": "101"})).json()
    url = f"{ORDERS}/orders/{created['id']}"
    assert (await http.get(url, headers={"X-Client-Id": "101"})).status_code == 200
    assert (await http.get(url, headers={"X-Client-Id": "102"})).status_code == 404
    assert (await http.get(url)).status_code == 401
    assert (await http.get(url, headers={"X-Client-Id": "abc"})).status_code == 401
    assert (await http.get(url, headers={"X-Client-Id": "0"})).status_code == 401
    assert (await http.post(f"{ORDERS}/orders", json={"product_id": 2, "quantity": 1}))\
        .status_code == 401


async def test_dev_controls_enabled_only_in_test_stack(http):
    r = await http.get(f"{P1}/_dev/faults")
    assert r.status_code == 200 and r.json()["delay_ms"] == 0
