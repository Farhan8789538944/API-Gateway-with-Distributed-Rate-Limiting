"""In-process tests of the full gateway pipeline.

The real Gateway class runs with fake authentication/limiter and an httpx
MockTransport as the upstream, so forwarding, limits, deadlines, overload
and cancellation can be tested deterministically without containers.
"""

import asyncio
import gzip
import logging

import httpx
import pytest
from starlette.requests import Request

from gateway.app import errors
from gateway.app.auth import Principal
from gateway.app.backends import BackendPool
from gateway.app.config import load_settings
from gateway.app.metrics import Metrics
from gateway.app.ratelimit import Decision, Policy, RateLimitUnavailable
from gateway.app.service import Gateway
from tests.unit.test_keys_routing_config import BASE_ENV

pytestmark = pytest.mark.anyio

VALID_KEY = "gwk_0123456789abcdef_" + "k" * 43
POLICY = Policy(1, "test", "token_bucket", 10, refill_tokens=1, refill_period_ms=1000)


class FakeAuth:
    def __init__(self):
        self.error = None

    async def authenticate(self, raw):
        if self.error:
            raise self.error
        if raw != VALID_KEY:
            raise errors.unauthorized("unknown_key")
        return Principal(42, "client-42", "0123456789abcdef", POLICY)


class FakeLimiter:
    def __init__(self):
        self.allow = True
        self.fail = False
        self.calls = 0

    async def check(self, client_id, policy):
        self.calls += 1
        if self.fail:
            raise RateLimitUnavailable("ConnectionError")
        return Decision(self.allow, 10, 9 if self.allow else 0, 2_500_000, policy.algorithm)


class _Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        if self.data:
            yield self.data


class Harness:
    def __init__(self, handler, **env):
        self.settings = load_settings({**BASE_ENV, **env})
        self.handler = handler
        self.auth = FakeAuth()
        self.limiter = FakeLimiter()
        self.metrics = Metrics()
        self.pools = {s.name: BackendPool(s, self.settings.health) for s in self.settings.services}
        for pool in self.pools.values():
            for inst in pool.instances:
                inst.record_success()
        transport = httpx.MockTransport(self._route)
        self.clients = {s.name: httpx.AsyncClient(transport=transport)
                        for s in self.settings.services}
        self.gateway = Gateway(self.settings, self.auth, self.limiter, self.pools,
                               self.clients, self.metrics)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.asgi),
                                        base_url="http://gw.test")

    async def _route(self, request: httpx.Request):
        response = await self.handler(request)
        if hasattr(response, "_content"):
            # Responses built from bytes are pre-read by httpx; real transports
            # always stream, so re-wrap to exercise the same code path.
            return httpx.Response(response.status_code, headers=response.headers.raw,
                                  stream=_Body(response.content))
        return response

    async def asgi(self, scope, receive, send):
        response = await self.gateway.handle(Request(scope, receive))
        await response(scope, receive, send)

    async def get(self, path, **kw):
        headers = {"X-API-Key": VALID_KEY, **kw.pop("headers", {})}
        return await self.client.get(path, headers=headers, **kw)

    def metric(self, name, **labels):
        return self.metrics.registry.get_sample_value(name, labels) or 0


async def echo(request: httpx.Request):
    body = await request.aread()
    return httpx.Response(200, json={
        "method": request.method,
        "host": request.url.host,
        "port": request.url.port,
        "raw_path": request.url.raw_path.decode(),
        "headers": [[k.decode(), v.decode()] for k, v in request.headers.raw],
        "body": body.decode(),
    })


def header_values(echoed, name):
    return [v for k, v in echoed["headers"] if k.lower() == name]


# ----------------------------------------------------------- forwarding
async def test_forwards_method_path_query_body_and_rebuilds_host():
    h = Harness(echo)
    r = await h.client.post(
        "/orders/7?x=1&x=2&q=a%20b", content=b'{"a":1}',
        headers={"X-API-Key": VALID_KEY, "Content-Type": "application/json"})
    assert r.status_code == 200
    data = r.json()
    assert data["method"] == "POST"
    assert data["raw_path"] == "/orders/7?x=1&x=2&q=a%20b"
    assert data["body"] == '{"a":1}'
    assert (data["host"], data["port"]) == ("orders", 8000)
    assert header_values(data, "host") == ["orders:8000"]
    assert header_values(data, "content-length") == ["7"]
    assert header_values(data, "content-type") == ["application/json"]


async def test_identity_headers_are_overwritten_and_key_not_forwarded():
    h = Harness(echo)
    r = await h.get("/products/1", headers={
        "X-Client-Id": "999", "X-Client-Name": "admin", "X-Gateway-User": "root",
        "Authorization": "Bearer stolen"})
    data = r.json()
    assert header_values(data, "x-client-id") == ["42"]
    assert not header_values(data, "x-client-name")
    assert not header_values(data, "x-gateway-user")
    assert not header_values(data, "authorization")
    assert not header_values(data, "x-api-key")
    assert VALID_KEY not in r.text


async def test_response_status_and_repeated_headers_preserved():
    async def upstream(_):
        return httpx.Response(
            418, content=b"teapot",
            headers=[("set-cookie", "a=1"), ("set-cookie", "b=2"), ("x-custom", "v"),
                     ("connection", "x-secret"), ("x-secret", "hidden"), ("server", "up")])

    h = Harness(upstream)
    r = await h.get("/products")
    assert r.status_code == 418 and r.content == b"teapot"
    assert r.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert r.headers["x-custom"] == "v"
    assert "x-secret" not in r.headers and r.headers.get("server") != "up"
    assert r.headers["content-length"] == "6"
    assert r.headers["x-ratelimit-remaining"] == "9"


async def test_backend_5xx_is_relayed_unchanged():
    async def upstream(_):
        return httpx.Response(503, json={"error": "backend says no"})

    h = Harness(upstream)
    r = await h.get("/products/1")
    assert r.status_code == 503 and r.json() == {"error": "backend says no"}
    assert h.metric("gateway_requests_total", route="/products/{id}", status_class="5xx",
                    outcome="upstream_5xx") == 1


async def test_head_and_no_content_semantics():
    async def upstream(request):
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "123",
                                                "content-type": "application/json"})
        return httpx.Response(204)

    h = Harness(upstream)
    r = await h.client.head("/products/1", headers={"X-API-Key": VALID_KEY})
    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-length"] == "123"
    r = await h.client.delete("/orders/1", headers={"X-API-Key": VALID_KEY})
    assert r.status_code == 204 and r.content == b""
    assert "content-length" not in r.headers


async def test_content_encoding_bytes_relayed_with_consistent_length():
    original = b'{"items":[' + b"1," * 500 + b"1]}"
    compressed = gzip.compress(original)

    async def upstream(request):
        assert request.headers["accept-encoding"] == "gzip"
        return httpx.Response(200, stream=_Body(compressed),
                              headers={"content-encoding": "gzip",
                                       "content-type": "application/json",
                                       "content-length": str(len(compressed))})

    h = Harness(upstream)
    r = await h.get("/products", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["content-length"] == str(len(compressed))
    assert r.content == original  # httpx decoded the relayed gzip bytes


async def test_redirects_are_not_followed():
    async def upstream(_):
        return httpx.Response(302, headers={"location": "http://elsewhere/"})

    h = Harness(upstream)
    r = await h.get("/products")
    assert r.status_code == 302 and r.headers["location"] == "http://elsewhere/"


async def test_request_id_validated_or_generated():
    h = Harness(echo)
    r = await h.get("/products", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    assert header_values(r.json(), "x-request-id") == ["abc-123"]
    r = await h.get("/products", headers={"X-Request-ID": "bad id\x01" + "x" * 100})
    assert r.headers["x-request-id"] != "bad id" and len(r.headers["x-request-id"]) == 32


# ------------------------------------------------------- gateway errors
async def test_unknown_route_and_unsafe_paths():
    h = Harness(echo)
    assert (await h.get("/products-other")).status_code == 404
    assert (await h.get("/")).status_code == 404
    assert (await h.get("/_dev/faults")).status_code == 404
    r = await h.client.get("/products/%2e%2e/_dev/faults", headers={"X-API-Key": VALID_KEY})
    assert r.status_code == 400
    assert h.limiter.calls == 0


async def test_auth_failures_and_multiple_keys():
    h = Harness(echo)
    r = await h.client.get("/products")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert "www-authenticate" in r.headers
    r = await h.client.get("/products", headers=[("X-API-Key", VALID_KEY),
                                                 ("X-API-Key", VALID_KEY)])
    assert r.status_code == 401
    assert h.limiter.calls == 0


async def test_quota_rejection_has_retry_after():
    h = Harness(echo)
    h.limiter.allow = False
    r = await h.get("/products")
    assert r.status_code == 429
    assert r.headers["retry-after"] == "3"  # 2.5 s rounded up
    assert r.headers["x-ratelimit-remaining"] == "0"


async def test_redis_and_postgres_failures_fail_closed():
    h = Harness(echo)
    h.limiter.fail = True
    r = await h.get("/products")
    assert r.status_code == 503 and r.json()["error"]["code"] == "dependency_unavailable"
    assert h.metric("gateway_dependency_failures_total", dependency="redis") == 1
    h.limiter.fail = False
    h.auth.error = errors.dependency_unavailable("postgres")
    r = await h.get("/products")
    assert r.status_code == 503
    assert h.metric("gateway_dependency_failures_total", dependency="postgres") == 1


async def test_no_healthy_backend():
    h = Harness(echo)
    for inst in h.pools["products"].instances:
        inst.record_failure("x")
        inst.record_failure("x")
    r = await h.get("/products/1")
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_healthy_backend"
    assert (await h.get("/orders/1")).status_code == 200


# ---------------------------------------------------------- body limits
async def test_declared_oversized_body_rejected_before_auth_and_quota():
    h = Harness(echo, MAX_REQUEST_BODY_BYTES="100")
    r = await h.client.post("/orders", content=b"x" * 101, headers={"X-API-Key": VALID_KEY})
    assert r.status_code == 413
    assert h.limiter.calls == 0


async def test_chunked_body_limit_enforced_while_streaming():
    h = Harness(echo, MAX_REQUEST_BODY_BYTES="100")
    sent = 0

    async def body(n_chunks):
        nonlocal sent
        for _ in range(n_chunks):
            sent += 1
            yield b"x" * 30

    r = await h.client.post("/orders", content=body(1000), headers={"X-API-Key": VALID_KEY})
    assert r.status_code == 413
    assert sent < 10  # did not read the whole stream
    sent = 0
    r = await h.client.post("/orders", content=body(3), headers={"X-API-Key": VALID_KEY})
    assert r.status_code == 200 and r.json()["body"] == "x" * 90
    assert header_values(r.json(), "content-length") == ["90"]
    assert not header_values(r.json(), "transfer-encoding")


async def test_oversized_upstream_response_rejected():
    async def streamed(_):
        async def chunks():
            for _ in range(100):
                yield b"y" * 100
        return httpx.Response(200, content=chunks())

    h = Harness(streamed, MAX_RESPONSE_BODY_BYTES="1000")
    r = await h.get("/products")
    assert r.status_code == 502

    async def declared(_):
        return httpx.Response(200, content=b"z" * 2000)

    h = Harness(declared, MAX_RESPONSE_BODY_BYTES="1000")
    assert (await h.get("/products")).status_code == 502


# --------------------------------------------------- deadlines / failures
async def test_total_deadline_returns_504_promptly():
    async def slow(_):
        await asyncio.sleep(5)
        return httpx.Response(200)

    h = Harness(slow, UPSTREAM_DEADLINE_S="0.2", UPSTREAM_READ_TIMEOUT_S="0.2")
    loop = asyncio.get_running_loop()
    started = loop.time()
    r = await h.get("/products/1")
    assert r.status_code == 504
    assert loop.time() - started < 1.5
    assert h.metric("gateway_upstream_timeouts_total", service="products") == 1
    assert h.gateway.global_admission.in_use == 0


async def test_slow_body_trickle_is_bounded_by_total_deadline():
    async def trickle(_):
        async def chunks():
            for _ in range(100):
                await asyncio.sleep(0.05)
                yield b"."
        return httpx.Response(200, content=chunks())

    h = Harness(trickle, UPSTREAM_DEADLINE_S="0.3", UPSTREAM_READ_TIMEOUT_S="0.3")
    assert (await h.get("/products")).status_code == 504


async def test_get_connect_failure_retries_other_instance_once():
    calls = []

    async def upstream(request):
        calls.append(request.url.host)
        if request.url.host == "product-1":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"from": request.url.host})

    env = {"PRODUCTS_UPSTREAMS": "product-1=http://product-1:8000,"
                                 "product-2=http://product-2:8000"}
    h = Harness(upstream, **env)
    results = [await h.get("/products/1") for _ in range(2)]
    assert all(r.status_code == 200 and r.json()["from"] == "product-2" for r in results)
    first = h.pools["products"].instances[0]
    assert first.consecutive_failures >= 1


async def test_post_connect_failure_is_not_retried():
    calls = []

    async def upstream(request):
        calls.append(request.url.host)
        raise httpx.ConnectError("refused", request=request)

    h = Harness(upstream)
    r = await h.client.post("/orders", json={"product_id": 1, "quantity": 1},
                            headers={"X-API-Key": VALID_KEY})
    assert r.status_code == 502 and calls == ["orders"]


async def test_read_error_after_send_is_502_and_not_retried():
    calls = []

    async def upstream(request):
        calls.append(request.url.host)
        raise httpx.ReadError("reset", request=request)

    h = Harness(upstream)
    r = await h.get("/products/1")
    assert r.status_code == 502 and len(calls) == 1


async def test_upstream_pool_timeout_maps_to_503():
    async def upstream(request):
        raise httpx.PoolTimeout("pool", request=request)

    h = Harness(upstream)
    r = await h.get("/products/1")
    assert r.status_code == 503 and r.json()["error"]["code"] == "capacity_exhausted"


# -------------------------------------------------------- overload/cancel
async def test_global_overload_rejects_immediately_and_recovers():
    release = asyncio.Event()
    entered = 0

    async def blocking(_):
        nonlocal entered
        entered += 1
        await release.wait()
        return httpx.Response(200)

    h = Harness(blocking, GLOBAL_MAX_IN_FLIGHT="2", PRODUCTS_MAX_IN_FLIGHT="2",
                ORDERS_MAX_IN_FLIGHT="2")
    held = [asyncio.create_task(h.get("/products/1")) for _ in range(2)]
    while entered < 2:
        await asyncio.sleep(0.01)
    loop = asyncio.get_running_loop()
    started = loop.time()
    r = await h.get("/orders/1")
    assert r.status_code == 503 and r.json()["error"]["code"] == "capacity_exhausted"
    assert loop.time() - started < 0.5
    assert h.metric("gateway_in_flight_requests") == 2
    release.set()
    results = await asyncio.wait_for(asyncio.gather(*held), 2)
    assert [r.status_code for r in results] == [200, 200]
    assert h.gateway.global_admission.in_use == 0
    assert h.metric("gateway_in_flight_requests") == 0
    assert h.metric("gateway_capacity_rejections_total", scope="global") == 1


async def test_service_admission_is_per_service():
    release = asyncio.Event()

    async def blocking(request):
        if request.url.host.startswith("p"):
            await release.wait()
        return httpx.Response(200)

    h = Harness(blocking, PRODUCTS_MAX_IN_FLIGHT="1")
    held = asyncio.create_task(h.get("/products/1"))
    while h.gateway.service_admission["products"].in_use < 1:
        await asyncio.sleep(0.01)
    assert (await h.get("/products/2")).status_code == 503
    assert (await h.get("/orders/1")).status_code == 200
    release.set()
    assert (await held).status_code == 200


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, gate: asyncio.Event):
        self.gate = gate
        self.closed = False

    async def __aiter__(self):
        yield b"partial"
        await self.gate.wait()
        yield b"rest"

    async def aclose(self):
        self.closed = True


async def test_cancellation_releases_slots_and_closes_upstream_response():
    gate = asyncio.Event()
    stream = TrackingStream(gate)

    async def upstream(_):
        return httpx.Response(200, stream=stream)

    h = Harness(upstream)
    task = asyncio.create_task(h.get("/products/1"))
    while h.gateway.service_admission["products"].in_use < 1:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    assert h.gateway.global_admission.in_use == 0
    assert h.gateway.service_admission["products"].in_use == 0
    assert h.metric("gateway_in_flight_requests") == 0
    assert h.metric("gateway_upstream_in_flight_requests", service="products") == 0


# ------------------------------------------------------------ telemetry
async def test_logs_and_metrics_never_contain_keys_queries_or_raw_paths(caplog):
    h = Harness(echo)
    caplog.set_level(logging.INFO, logger="gateway.request")
    await h.get("/products/987654321?token=supersecret")
    records = [r for r in caplog.records if r.getMessage() == "request.completed"]
    assert records
    fields = records[-1].fields
    assert fields["route"] == "/products/{id}" and fields["client_id"] == 42
    assert fields["status"] == 200 and fields["rate_limit"] == "allowed"
    dump = repr(fields)
    assert "supersecret" not in dump and "987654321" not in dump and VALID_KEY not in dump

    from prometheus_client import generate_latest
    exposition = generate_latest(h.metrics.registry).decode()
    assert "987654321" not in exposition and "supersecret" not in exposition
    assert 'client' not in exposition.lower().replace("client_disconnected", "")
    assert h.metric("gateway_success_latency_seconds_count", route="/products/{id}") == 1


async def test_rejected_requests_do_not_enter_success_latency():
    h = Harness(echo)
    h.limiter.allow = False
    await h.get("/products/1")
    assert h.metric("gateway_success_latency_seconds_count", route="/products/{id}") == 0
    assert h.metric("gateway_requests_total", route="/products/{id}", status_class="4xx",
                    outcome="quota_rejected") == 1
